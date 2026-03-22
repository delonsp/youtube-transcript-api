#!/usr/bin/env python3
"""
Script para preencher resumos de lives no Google Docs.

Funcionalidades:
- Lista lives não documentadas
- Agrupa siblings (📱 + normal = mesma live)
- Baixa transcrição de cada live
- Gera resumo + Q&A com DeepSeek
- Insere entrada formatada no documento (1 por live, não duplica siblings)
"""

import os
import re
import pickle
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from collections import defaultdict

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Importar do transcript_processor para reusar funções
from transcript_processor import TranscriptDownloader

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Escopos necessários
SCOPES = [
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/youtube.force-ssl',
]

# ID do documento
DOCUMENT_ID = '1wUM7wHVIK5C46Tqp30e-DqMSy1gqg5wt72Ppi701U4g'

# Títulos que NÃO pertencem ao doc do Nível 1
EXCLUDED_TITLE_PATTERNS = [
    r'estudos?\s+avan[cç]ad',
    r'sa[úu]de\s+integrativa',
    r'sa[úu]de\s+cerebral',
]

def _is_excluded_live(title: str) -> bool:
    """Verifica se a live não pertence ao doc Nível 1 (Tira Dúvidas)."""
    title_lower = title.lower()
    for pattern in EXCLUDED_TITLE_PATTERNS:
        if re.search(pattern, title_lower):
            return True
    return False

# DeepSeek API - busca de keyring primeiro, fallback para env var
def get_deepseek_api_key():
    """Obtém API key do keyring ou variável de ambiente."""
    api_key = None
    try:
        import keyring
        api_key = keyring.get_password('deepseek', 'api_key')
    except Exception:
        pass
    return api_key or os.getenv('DEEPSEEK_API_KEY')


class DocSummaryFiller:
    """Preenche resumos de lives no Google Docs."""

    def __init__(self):
        self.creds = None
        self.docs_service = None
        self.youtube_service = None
        self.token_path = Path('token_docs.pickle')
        self.client_secrets_path = Path('client_secrets.json')
        self.transcript_downloader = TranscriptDownloader(
            captions_token_file='token_captions.pickle'
        )

    def authenticate(self):
        """Autentica com Google APIs."""
        if self.token_path.exists():
            with open(self.token_path, 'rb') as token:
                self.creds = pickle.load(token)

        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.client_secrets_path), SCOPES
                )
                self.creds = flow.run_local_server(port=8080)

            with open(self.token_path, 'wb') as token:
                pickle.dump(self.creds, token)

        self.docs_service = build('docs', 'v1', credentials=self.creds)
        self.youtube_service = build('youtube', 'v3', credentials=self.creds)
        logger.info("✅ Authenticated successfully!")

    def get_channel_lives(self, since_date: datetime, max_results: int = 200) -> List[Dict]:
        """Busca lives do canal a partir de uma data."""
        logger.info(f"Searching for lives since {since_date.strftime('%Y-%m-%d')}...")

        channels_response = self.youtube_service.channels().list(
            part='contentDetails', mine=True
        ).execute()

        if not channels_response.get('items'):
            raise ValueError("No channel found")

        uploads_playlist = channels_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']

        lives = []
        next_page_token = None

        while len(lives) < max_results:
            playlist_response = self.youtube_service.playlistItems().list(
                part='snippet,contentDetails',
                playlistId=uploads_playlist,
                maxResults=min(50, max_results - len(lives)),
                pageToken=next_page_token
            ).execute()

            video_ids = [item['contentDetails']['videoId'] for item in playlist_response.get('items', [])]
            if not video_ids:
                break

            videos_response = self.youtube_service.videos().list(
                part='snippet,liveStreamingDetails,contentDetails',
                id=','.join(video_ids)
            ).execute()

            for video in videos_response.get('items', []):
                if 'liveStreamingDetails' not in video:
                    continue

                published_at = video['snippet']['publishedAt']
                published_date = datetime.fromisoformat(published_at.replace('Z', '+00:00'))

                if published_date.replace(tzinfo=None) < since_date:
                    return lives

                lives.append({
                    'video_id': video['id'],
                    'title': video['snippet']['title'],
                    'published_at': published_at,
                    'url': f"https://youtube.com/watch?v={video['id']}",
                })

            next_page_token = playlist_response.get('nextPageToken')
            if not next_page_token:
                break

        return lives

    def get_documented_video_ids(self) -> set:
        """Retorna IDs de vídeos já documentados no documento."""
        doc = self.docs_service.documents().get(documentId=DOCUMENT_ID).execute()

        video_ids = set()
        url_patterns = [
            r'youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
            r'youtu\.be/([a-zA-Z0-9_-]{11})',
            r'youtube\.com/live/([a-zA-Z0-9_-]{11})',
        ]

        content = doc.get('body', {}).get('content', [])
        for element in content:
            if 'paragraph' in element:
                for elem in element['paragraph'].get('elements', []):
                    if 'textRun' in elem:
                        style = elem['textRun'].get('textStyle', {})
                        link = style.get('link', {})
                        url = link.get('url', '')

                        for pattern in url_patterns:
                            match = re.search(pattern, url)
                            if match:
                                video_ids.add(match.group(1))
                                break

        logger.info(f"Found {len(video_ids)} documented videos")
        return video_ids

    def group_siblings(self, lives: List[Dict]) -> List[List[Dict]]:
        """
        Agrupa lives que são siblings (mesma live em versões diferentes).

        Detecta por:
        - Mesma data
        - Títulos similares (ex: "Tira Dúvidas 25/11/25" e "Tira Dúvidas 25/11/25 📱")
        """
        # Agrupar por data
        by_date = defaultdict(list)
        for live in lives:
            date = live['published_at'][:10]  # YYYY-MM-DD
            by_date[date].append(live)

        groups = []
        for date, date_lives in by_date.items():
            if len(date_lives) == 1:
                groups.append(date_lives)
            else:
                # Tentar agrupar por título similar
                processed = set()
                for i, live1 in enumerate(date_lives):
                    if i in processed:
                        continue

                    group = [live1]
                    processed.add(i)

                    # Normalizar título (remover 📱 e espaços extras)
                    title1_normalized = re.sub(r'[📱\s]+', ' ', live1['title']).strip().lower()

                    for j, live2 in enumerate(date_lives):
                        if j in processed:
                            continue

                        title2_normalized = re.sub(r'[📱\s]+', ' ', live2['title']).strip().lower()

                        # Se títulos são muito similares, são siblings
                        if title1_normalized == title2_normalized or \
                           title1_normalized.startswith(title2_normalized) or \
                           title2_normalized.startswith(title1_normalized):
                            group.append(live2)
                            processed.add(j)

                    groups.append(group)

        # Ordenar grupos por data (mais antigas primeiro para ordem cronológica)
        groups.sort(key=lambda g: g[0]['published_at'])

        logger.info(f"Grouped {len(lives)} lives into {len(groups)} groups (sorted chronologically)")
        return groups

    def download_transcript(self, video_id: str) -> Optional[str]:
        """Baixa transcrição de um vídeo."""
        try:
            result = self.transcript_downloader.download(video_id, languages=['pt', 'pt-BR', 'en'])

            # Concatenar snippets
            full_text = ' '.join([s['text'] for s in result['snippets']])
            return full_text
        except Exception as e:
            logger.error(f"Failed to download transcript for {video_id}: {e}")
            return None

    def generate_summary_with_deepseek(self, transcript: str, title: str) -> Dict:
        """Gera resumo e Q&A usando DeepSeek API."""
        from openai import OpenAI

        api_key = get_deepseek_api_key()
        if not api_key:
            logger.warning("DeepSeek API key not found in keyring or env var, using placeholder")
            return {
                'summary': '[Resumo a ser preenchido manualmente]',
                'qa_list': [{'pergunta': '[Pergunta]', 'resposta': '[Resposta]'}]
            }

        prompt = f"""Analise a transcrição desta live "{title}" e gere:

1. Um RESUMO conciso (2-3 parágrafos) destacando os principais temas abordados.

2. Uma lista de PERGUNTAS E RESPOSTAS (5-10 itens) com as principais dúvidas respondidas na live.

Transcrição:
{transcript[:30000]}

Responda SOMENTE com JSON válido no formato:
{{
  "summary": "resumo aqui...",
  "qa_list": [
    {{"pergunta": "pergunta 1", "resposta": "resposta 1"}},
    {{"pergunta": "pergunta 2", "resposta": "resposta 2"}}
  ]
}}
"""

        try:
            # DeepSeek usa API compatível com OpenAI
            client = OpenAI(
                api_key=api_key,
                base_url="https://api.deepseek.com"
            )

            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Você é um assistente que analisa transcrições e retorna JSON. Responda APENAS com JSON válido, sem texto adicional."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=4000,
                response_format={"type": "json_object"}
            )

            content = response.choices[0].message.content

            # Parse JSON response
            import json
            result = json.loads(content)

            return {
                'summary': result.get('summary', '[Resumo não gerado]'),
                'qa_list': result.get('qa_list', [{'pergunta': '[Pergunta]', 'resposta': '[Resposta]'}])
            }

        except Exception as e:
            logger.error(f"DeepSeek API error: {e}")
            return {
                'summary': '[Erro ao gerar resumo]',
                'qa_list': [{'pergunta': '[Pergunta]', 'resposta': '[Resposta]'}]
            }

    def format_date_portuguese(self, dt: datetime) -> str:
        """Formata data em português extenso."""
        meses = [
            'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
            'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro'
        ]
        return f"{dt.day} de {meses[dt.month - 1]} de {dt.year}"

    def append_entry_to_document(self, live: Dict, summary_data: Dict, sibling_urls: List[str] = None):
        """
        Adiciona uma entrada formatada ao documento.

        Formato:
        [Data como hyperlink]

        <summary>
        [Resumo gerado]
        </summary>

        <qa_list>
        - Pergunta: ...
          Resposta: ...
        </qa_list>

        """
        doc = self.docs_service.documents().get(documentId=DOCUMENT_ID).execute()
        end_index = doc.get('body', {}).get('content', [])[-1].get('endIndex', 1) - 1

        published = datetime.fromisoformat(live['published_at'].replace('Z', '+00:00'))
        date_str = self.format_date_portuguese(published)

        # Formatar Q&A
        qa_text = ""
        for qa in summary_data['qa_list']:
            qa_text += f"- Pergunta: {qa['pergunta']}\n  Resposta: {qa['resposta']}\n\n"

        # Se tem siblings, adicionar links
        siblings_note = ""
        if sibling_urls and len(sibling_urls) > 1:
            siblings_note = f"\nVersões: {' | '.join(sibling_urls)}\n"

        # Texto completo da entrada (com espaços antes e depois)
        entry_text = f"""

<summary>
{summary_data['summary']}
</summary>
{siblings_note}
<qa_list>
{qa_text}</qa_list>


"""

        # Requests para inserir
        body_start = end_index + len(date_str) + 1
        body_end = body_start + len(entry_text)

        requests = [
            # 1. Inserir a data
            {
                'insertText': {
                    'location': {'index': end_index},
                    'text': date_str + '\n'
                }
            },
            # 2. Aplicar estilo HEADING_1 na data
            {
                'updateParagraphStyle': {
                    'range': {
                        'startIndex': end_index,
                        'endIndex': end_index + len(date_str) + 1
                    },
                    'paragraphStyle': {
                        'namedStyleType': 'HEADING_1'
                    },
                    'fields': 'namedStyleType'
                }
            },
            # 3. Aplicar hyperlink na data
            {
                'updateTextStyle': {
                    'range': {
                        'startIndex': end_index,
                        'endIndex': end_index + len(date_str)
                    },
                    'textStyle': {
                        'link': {'url': live['url']}
                    },
                    'fields': 'link'
                }
            },
            # 4. Inserir o conteúdo
            {
                'insertText': {
                    'location': {'index': body_start},
                    'text': entry_text
                }
            },
            # 5. Aplicar NORMAL_TEXT no corpo (evita herdar HEADING_1)
            {
                'updateParagraphStyle': {
                    'range': {
                        'startIndex': body_start,
                        'endIndex': body_end
                    },
                    'paragraphStyle': {
                        'namedStyleType': 'NORMAL_TEXT'
                    },
                    'fields': 'namedStyleType'
                }
            },
            # 6. Aplicar fonte Arial 11 no corpo
            {
                'updateTextStyle': {
                    'range': {
                        'startIndex': body_start,
                        'endIndex': body_end
                    },
                    'textStyle': {
                        'fontSize': {'magnitude': 11, 'unit': 'PT'},
                        'weightedFontFamily': {'fontFamily': 'Arial'},
                    },
                    'fields': 'fontSize,weightedFontFamily'
                }
            },
        ]

        self.docs_service.documents().batchUpdate(
            documentId=DOCUMENT_ID,
            body={'requests': requests}
        ).execute()

        logger.info(f"✅ Added entry for: {date_str}")

    def process_lives(self, since_date: datetime, max_lives: int = 10, dry_run: bool = True):
        """
        Processa lives e adiciona resumos ao documento.

        Args:
            since_date: Data inicial
            max_lives: Máximo de lives a processar
            dry_run: Se True, apenas mostra o que seria feito
        """
        # Buscar lives
        all_lives = self.get_channel_lives(since_date)

        # Filtrar lives que não pertencem a este doc (Estudos Avançados, Saúde Integrativa, etc.)
        tira_duvidas_lives = [l for l in all_lives if not _is_excluded_live(l['title'])]
        excluded_count = len(all_lives) - len(tira_duvidas_lives)
        if excluded_count:
            logger.info(f"Filtered out {excluded_count} non-Tira-Dúvidas lives")

        # Filtrar já documentadas
        documented_ids = self.get_documented_video_ids()
        undocumented = [l for l in tira_duvidas_lives if l['video_id'] not in documented_ids]

        logger.info(f"Found {len(undocumented)} undocumented lives")

        # Agrupar siblings
        groups = self.group_siblings(undocumented)

        print(f"\n📋 Lives não documentadas: {len(groups)} grupos ({len(undocumented)} vídeos)\n")

        for i, group in enumerate(groups[:max_lives], 1):
            main_live = group[0]  # Usar primeira versão
            published = datetime.fromisoformat(main_live['published_at'].replace('Z', '+00:00'))

            sibling_info = ""
            if len(group) > 1:
                sibling_info = f" (+ {len(group)-1} sibling)"

            print(f"{i}. [{published.strftime('%d/%m/%Y')}] {main_live['title']}{sibling_info}")
            print(f"   {main_live['url']}")

            if len(group) > 1:
                for sibling in group[1:]:
                    print(f"   └─ Sibling: {sibling['url']}")
            print()

        if dry_run:
            print(f"\n⚠️  Modo DRY RUN - nenhuma alteração foi feita")
            print(f"   Execute com --process para gerar resumos")
            return

        # Processar cada grupo
        print(f"\n📝 Processando {min(len(groups), max_lives)} lives...\n")

        for i, group in enumerate(groups[:max_lives], 1):
            main_live = group[0]
            published = datetime.fromisoformat(main_live['published_at'].replace('Z', '+00:00'))

            print(f"[{i}/{min(len(groups), max_lives)}] {self.format_date_portuguese(published)}")
            print(f"    Título: {main_live['title']}")

            # Baixar transcrição
            print(f"    📥 Baixando transcrição...")
            transcript = self.download_transcript(main_live['video_id'])

            if not transcript:
                # Tentar sibling
                for sibling in group[1:]:
                    print(f"    📥 Tentando sibling {sibling['video_id']}...")
                    transcript = self.download_transcript(sibling['video_id'])
                    if transcript:
                        break

            if not transcript:
                print(f"    ❌ Não foi possível baixar transcrição, pulando...")
                continue

            # Gerar resumo
            print(f"    🤖 Gerando resumo com DeepSeek...")
            summary_data = self.generate_summary_with_deepseek(transcript, main_live['title'])

            # Coletar URLs de todas as versões
            sibling_urls = [l['url'] for l in group]

            # Adicionar ao documento
            print(f"    📄 Adicionando ao documento...")
            self.append_entry_to_document(main_live, summary_data, sibling_urls if len(group) > 1 else None)

            print(f"    ✅ Concluído!\n")

            # Delay para não sobrecarregar APIs
            time.sleep(2)

        print(f"\n✅ Processamento concluído!")


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Preenche resumos de lives no Google Docs')
    parser.add_argument('--since', type=str, default='2024-11-20',
                        help='Data inicial (YYYY-MM-DD). Default: 2024-11-20')
    parser.add_argument('--max', type=int, default=10,
                        help='Máximo de lives a processar. Default: 10')
    parser.add_argument('--process', action='store_true',
                        help='Processar e gerar resumos (sem isso, apenas lista)')

    args = parser.parse_args()

    since_date = datetime.strptime(args.since, '%Y-%m-%d')

    filler = DocSummaryFiller()

    print("🔐 Autenticando...")
    filler.authenticate()

    filler.process_lives(
        since_date=since_date,
        max_lives=args.max,
        dry_run=not args.process
    )


if __name__ == '__main__':
    main()
