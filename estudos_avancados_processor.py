#!/usr/bin/env python3
"""
Estudos Avancados Processor
===========================

Processador dedicado para lives de membros nivel 2 (Estudos Avancados).
Usa DeepSeek para gerar resumos detalhados e timestamps precisos.

Padrao de titulo: "Estudos Avancados - Live #XX"

Caracteristicas:
- Lives mais longas (1-3 horas)
- Conteudo aprofundado para membros
- Resumos mais detalhados
- Timestamps com descricoes completas

Uso:
    python estudos_avancados_processor.py VIDEO_ID
    python estudos_avancados_processor.py VIDEO_ID --dry-run
    python estudos_avancados_processor.py --list-pending

Dependencias:
    - anthropic
    - youtube-transcript-api
    - yt-dlp
    - google-api-python-client
    - keyring
"""

import os
import json
import argparse
import logging
import re
import pickle
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Importar classes base do transcript_processor
from transcript_processor import (
    TranscriptDownloader,
    YouTubeManager,
    format_timestamp
)

# Configuracao de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Modelo DeepSeek (rapido e economico)
DEEPSEEK_MODEL = "deepseek-chat"

# Google Docs - Documento de resumos Estudos Avancados
ESTUDOS_AVANCADOS_DOC_ID = '1fpGZwBDDwT4aZ7NMNo_X2etuX95FSOxDf1EmgaaRhpA'

# Escopos para Google APIs
SCOPES = [
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/youtube.force-ssl',
]


class GoogleDocsManager:
    """Gerenciador para salvar resumos no Google Docs."""

    def __init__(self, token_path: str = 'token_estudos_avancados.pickle'):
        self.token_path = Path(token_path)
        self.client_secrets_path = Path('client_secrets.json')
        self.creds = None
        self.docs_service = None

    def authenticate(self):
        """Autentica com Google Docs API."""
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
        logger.info("Google Docs API authenticated")

    def format_date_portuguese(self, dt: datetime) -> str:
        """Formata data em portugues extenso."""
        meses = [
            'Janeiro', 'Fevereiro', 'Marco', 'Abril', 'Maio', 'Junho',
            'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro'
        ]
        return f"{dt.day} de {meses[dt.month - 1]} de {dt.year}"

    def is_video_documented(self, video_id: str) -> bool:
        """Verifica se o video ja esta documentado."""
        doc = self.docs_service.documents().get(documentId=ESTUDOS_AVANCADOS_DOC_ID).execute()

        url_patterns = [
            rf'youtube\.com/watch\?v={video_id}',
            rf'youtu\.be/{video_id}',
            rf'youtube\.com/live/{video_id}',
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
                            if re.search(pattern, url):
                                return True
        return False

    def append_entry(self, video_id: str, video_url: str, video_title: str,
                     published_at: str, analysis: Dict):
        """
        Adiciona uma entrada formatada ao documento.

        Formato:
        [Data como hyperlink] - Titulo

        <summary>
        [Resumo detalhado]
        </summary>

        <key_topics>
        - Topico 1
        - Topico 2
        </key_topics>

        <qa_list>
        - Pergunta: ...
          Resposta: ...
        </qa_list>
        """
        doc = self.docs_service.documents().get(documentId=ESTUDOS_AVANCADOS_DOC_ID).execute()
        end_index = doc.get('body', {}).get('content', [])[-1].get('endIndex', 1) - 1

        published = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
        date_str = self.format_date_portuguese(published)

        # Formatar topicos
        topics_text = ""
        for topic in analysis.get('key_topics', []):
            topics_text += f"- {topic}\n"

        # Formatar Q&A
        qa_text = ""
        for qa in analysis.get('qa_list', []):
            qa_text += f"- Pergunta: {qa['pergunta']}\n  Resposta: {qa['resposta']}\n\n"

        # Texto completo da entrada
        entry_text = f"""

<summary>
{analysis.get('summary', '[Resumo nao gerado]')}
</summary>

<key_topics>
{topics_text}</key_topics>

<qa_list>
{qa_text}</qa_list>


"""

        # Titulo com data
        header_text = f"{date_str} - {video_title}\n"

        # Requests para inserir
        requests = [
            # 1. Inserir o header (data + titulo)
            {
                'insertText': {
                    'location': {'index': end_index},
                    'text': header_text
                }
            },
            # 2. Aplicar estilo HEADING_1 no header
            {
                'updateParagraphStyle': {
                    'range': {
                        'startIndex': end_index,
                        'endIndex': end_index + len(header_text)
                    },
                    'paragraphStyle': {
                        'namedStyleType': 'HEADING_1'
                    },
                    'fields': 'namedStyleType'
                }
            },
            # 3. Aplicar hyperlink no header (apenas na data)
            {
                'updateTextStyle': {
                    'range': {
                        'startIndex': end_index,
                        'endIndex': end_index + len(date_str)
                    },
                    'textStyle': {
                        'link': {'url': video_url}
                    },
                    'fields': 'link'
                }
            },
            # 4. Inserir o conteudo
            {
                'insertText': {
                    'location': {'index': end_index + len(header_text)},
                    'text': entry_text
                }
            }
        ]

        self.docs_service.documents().batchUpdate(
            documentId=ESTUDOS_AVANCADOS_DOC_ID,
            body={'requests': requests}
        ).execute()

        logger.info(f"Added entry to Google Docs: {date_str} - {video_title}")


class DeepSeekProcessor:
    """Processador AI usando DeepSeek para analises detalhadas."""

    def __init__(self):
        self.client = None
        self._init_client()

    def _init_client(self):
        """Inicializa cliente DeepSeek (API compativel com OpenAI)."""
        from openai import OpenAI

        api_key = None
        try:
            import keyring
            api_key = keyring.get_password('deepseek', 'api_key')
        except Exception:
            pass
        api_key = api_key or os.getenv('DEEPSEEK_API_KEY')
        if not api_key:
            raise ValueError(
                "DeepSeek API key not found. Set it with:\n"
                "python -c \"import keyring; keyring.set_password('deepseek', 'api_key', 'sk-...')\""
            )

        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        logger.info(f"Initialized DeepSeek ({DEEPSEEK_MODEL})")

    def generate_detailed_analysis(self, transcript_data: Dict, video_title: str) -> Dict:
        """
        Gera analise completa da live usando DeepSeek.

        Retorna:
        {
            'timestamps': [...],  # Lista de timestamps com descricoes
            'summary': '...',     # Resumo detalhado (3-5 paragrafos)
            'key_topics': [...],  # Topicos principais abordados
            'qa_list': [...]      # Perguntas e respostas identificadas
        }
        """
        snippets = transcript_data['snippets']
        video_duration = snippets[-1]['start'] + snippets[-1]['duration'] if snippets else 0

        # Formatar transcricao com timestamps
        formatted_transcript = self._format_transcript(snippets)

        duration_str = f"{int(video_duration // 3600)}h{int((video_duration % 3600) // 60):02d}m" if video_duration >= 3600 else f"{int(video_duration // 60)}m{int(video_duration % 60):02d}s"

        prompt = f"""Voce e um especialista em analise de conteudo educacional em portugues brasileiro.

Analise esta transcricao de uma live chamada "{video_title}" (duracao: {duration_str}, {int(video_duration)} segundos).

Esta e uma live de "Estudos Avancados" - conteudo aprofundado para membros do canal. Por isso, sua analise deve ser DETALHADA e COMPLETA.

TRANSCRICAO:
{formatted_transcript}

---

Gere uma analise COMPLETA em JSON com:

1. "timestamps": Array de objetos com marcacoes de tempo para navegacao do video.
   - Identifique TODOS os topicos relevantes (esperamos 10-20 timestamps para uma live longa)
   - Cada objeto: {{"timestamp": segundos, "title": "titulo curto", "description": "descricao detalhada do que e discutido"}}
   - IMPORTANTE: Todos timestamps devem estar DENTRO da duracao do video (maximo {int(video_duration)} segundos)
   - Primeiro timestamp deve ser proximo de 0

2. "summary": String com resumo DETALHADO (3-5 paragrafos).
   - Descreva os principais conceitos abordados
   - Mencione exemplos praticos citados
   - Destaque insights importantes
   - Escreva em portugues brasileiro formal

3. "key_topics": Array de strings com os topicos principais (5-10 itens)
   - Liste os temas centrais discutidos

4. "qa_list": Array de objetos com perguntas e respostas identificadas na live.
   - {{"pergunta": "pergunta feita", "resposta": "resposta dada", "timestamp": segundos}}
   - Inclua perguntas dos espectadores respondidas pelo apresentador
   - Minimo 5 perguntas, maximo 15

Responda APENAS com JSON valido, sem texto adicional antes ou depois."""

        logger.info("Sending request to DeepSeek...")

        response = self.client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            max_tokens=16000,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        response_text = response.choices[0].message.content

        # Extrair JSON da resposta
        try:
            # Tentar parse direto
            result = json.loads(response_text)
        except json.JSONDecodeError:
            # Tentar encontrar JSON na resposta
            start = response_text.find('{')
            end = response_text.rfind('}') + 1
            if start >= 0 and end > start:
                result = json.loads(response_text[start:end])
            else:
                raise ValueError("Could not parse JSON from Claude response")

        # Validar e filtrar timestamps
        valid_timestamps = []
        for ts in result.get('timestamps', []):
            if ts['timestamp'] <= video_duration:
                valid_timestamps.append(ts)
            else:
                logger.warning(f"Removed invalid timestamp: {ts['timestamp']}s - {ts['title']}")

        result['timestamps'] = valid_timestamps

        logger.info(f"Analysis complete: {len(valid_timestamps)} timestamps, {len(result.get('qa_list', []))} Q&As")

        return result

    def _format_transcript(self, snippets: List[Dict]) -> str:
        """Formata transcricao com timestamps para envio ao AI."""
        lines = []
        for snippet in snippets:
            timestamp = self._seconds_to_timestamp(snippet['start'])
            lines.append(f"[{timestamp}] {snippet['text']}")
        return '\n'.join(lines)

    def _seconds_to_timestamp(self, seconds: float) -> str:
        """Converte segundos para formato HH:MM:SS ou MM:SS."""
        total = int(seconds)
        hours = total // 3600
        minutes = (total % 3600) // 60
        secs = total % 60

        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"


def format_detailed_comment(analysis: Dict) -> str:
    """Formata analise como comentario detalhado para YouTube."""
    lines = ["Timestamps:"]
    lines.append("")

    for ts in analysis['timestamps']:
        timestamp_str = format_timestamp(ts['timestamp'])
        lines.append(f"{timestamp_str} - {ts['title']}")
        if ts.get('description'):
            lines.append(f"   {ts['description']}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("Topicos abordados:")
    for topic in analysis.get('key_topics', []):
        lines.append(f"- {topic}")

    return '\n'.join(lines)


def format_detailed_description_timestamps(analysis: Dict) -> str:
    """Formata timestamps para descricao do YouTube (formato compativel com chapters)."""
    lines = ["\n\nTimestamps:"]

    # Garantir que comeca em 0:00
    timestamps = analysis['timestamps']
    if not timestamps or timestamps[0]['timestamp'] > 0:
        lines.append("0:00 Inicio")

    for ts in timestamps:
        timestamp_str = format_timestamp(ts['timestamp'])
        lines.append(f"{timestamp_str} {ts['title']}")

    return '\n'.join(lines)


class EstudosAvancadosManager:
    """Gerenciador para lives de Estudos Avancados."""

    def __init__(self, credentials_file: str = 'client_secrets.json'):
        self.transcript_downloader = TranscriptDownloader(
            captions_token_file='token_captions.pickle'
        )
        self.ai_processor = DeepSeekProcessor()
        self.youtube_manager = YouTubeManager(credentials_file=credentials_file)
        self.docs_manager = GoogleDocsManager()

    def process_video(self, video_id: str, dry_run: bool = False) -> Dict:
        """
        Processa uma live de Estudos Avancados.

        Args:
            video_id: ID do video YouTube
            dry_run: Se True, nao posta no YouTube

        Returns:
            Dict com resultados do processamento
        """
        logger.info("=" * 60)
        logger.info("ESTUDOS AVANCADOS PROCESSOR")
        logger.info(f"Video ID: {video_id}")
        logger.info(f"AI Model: DeepSeek")
        logger.info("=" * 60)

        # 1. Baixar transcricao
        logger.info("\n[1/5] Baixando transcricao...")
        transcript_data = self.transcript_downloader.download(video_id, languages=['pt', 'pt-BR', 'en'])

        snippets_count = len(transcript_data['snippets'])
        duration = transcript_data['snippets'][-1]['start'] if snippets_count > 0 else 0
        logger.info(f"   {snippets_count} snippets baixados")
        logger.info(f"   Duracao: {int(duration // 60)} minutos")
        logger.info(f"   Metodo: {transcript_data['method']}")

        # 2. Gerar analise com DeepSeek
        logger.info("\n[2/5] Gerando analise com DeepSeek...")

        # Obter titulo do video
        self.youtube_manager.authenticate()
        video_info = self.youtube_manager.youtube.videos().list(
            part="snippet",
            id=video_id
        ).execute()

        video_title = video_info['items'][0]['snippet']['title'] if video_info['items'] else f"Video {video_id}"

        analysis = self.ai_processor.generate_detailed_analysis(transcript_data, video_title)

        logger.info(f"   {len(analysis['timestamps'])} timestamps identificados")
        logger.info(f"   {len(analysis.get('key_topics', []))} topicos principais")
        logger.info(f"   {len(analysis.get('qa_list', []))} Q&As identificados")

        # 3. Mostrar preview
        logger.info("\n[3/5] Preview da analise:")
        logger.info("-" * 40)

        print("\nTIMESTAMPS:")
        for ts in analysis['timestamps'][:10]:
            print(f"  {format_timestamp(ts['timestamp'])} - {ts['title']}")
        if len(analysis['timestamps']) > 10:
            print(f"  ... e mais {len(analysis['timestamps']) - 10} timestamps")

        print("\nRESUMO (preview):")
        summary_preview = analysis.get('summary', '')[:500]
        print(f"  {summary_preview}...")

        print("\nTOPICOS PRINCIPAIS:")
        for topic in analysis.get('key_topics', [])[:5]:
            print(f"  - {topic}")

        logger.info("-" * 40)

        # 4. Atualizar YouTube
        if dry_run:
            logger.info("\n[4/5] DRY RUN - Nenhuma alteracao feita no YouTube")
        else:
            logger.info("\n[4/5] Atualizando YouTube...")

            # Converter timestamps para formato esperado pelo YouTubeManager
            topics_for_youtube = [
                {'timestamp': ts['timestamp'], 'title': ts['title']}
                for ts in analysis['timestamps']
            ]

            # Atualizar descricao
            logger.info("   Atualizando descricao...")
            self.youtube_manager.update_video_description(video_id, topics_for_youtube, append=True)

            # Postar comentario (se nao existir)
            if not self.youtube_manager.has_timestamp_comment(video_id):
                logger.info("   Postando comentario...")
                comment_text = format_detailed_comment(analysis)
                self.youtube_manager.post_comment(video_id, comment_text)
            else:
                logger.info("   Video ja tem comentario com timestamps")

        # 5. Salvar resumo no Google Docs
        if dry_run:
            logger.info("\n[5/5] DRY RUN - Resumo NAO foi salvo no Google Docs")
        else:
            logger.info("\n[5/5] Salvando resumo no Google Docs...")

            # Autenticar com Google Docs
            self.docs_manager.authenticate()

            # Verificar se ja esta documentado
            if self.docs_manager.is_video_documented(video_id):
                logger.info("   Video ja esta documentado no Google Docs")
            else:
                # Obter data de publicacao
                published_at = video_info['items'][0]['snippet']['publishedAt'] if video_info['items'] else datetime.now().isoformat()

                # Salvar no documento
                self.docs_manager.append_entry(
                    video_id=video_id,
                    video_url=f"https://youtube.com/watch?v={video_id}",
                    video_title=video_title,
                    published_at=published_at,
                    analysis=analysis
                )
                logger.info("   Resumo adicionado ao Google Docs!")

        logger.info("\n" + "=" * 60)
        logger.info("PROCESSAMENTO CONCLUIDO!")
        logger.info("=" * 60)

        return {
            'video_id': video_id,
            'title': video_title,
            'analysis': analysis,
            'dry_run': dry_run
        }

    def list_pending_estudos_avancados(self) -> List[Dict]:
        """Lista lives de Estudos Avancados que ainda nao foram processadas."""
        self.youtube_manager.authenticate()

        # Buscar videos do canal
        channels_response = self.youtube_manager.youtube.channels().list(
            part='contentDetails', mine=True
        ).execute()

        if not channels_response.get('items'):
            return []

        uploads_playlist = channels_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']

        pending = []
        next_page_token = None

        # Padrao do titulo
        pattern = re.compile(r'estudos\s+avan[cç]ados.*live\s*#?\s*\d*', re.IGNORECASE)

        while True:
            playlist_response = self.youtube_manager.youtube.playlistItems().list(
                part='snippet,contentDetails',
                playlistId=uploads_playlist,
                maxResults=50,
                pageToken=next_page_token
            ).execute()

            video_ids = [item['contentDetails']['videoId'] for item in playlist_response.get('items', [])]
            if not video_ids:
                break

            videos_response = self.youtube_manager.youtube.videos().list(
                part='snippet,contentDetails',
                id=','.join(video_ids)
            ).execute()

            for video in videos_response.get('items', []):
                title = video['snippet']['title']

                # Verificar se e "Estudos Avancados"
                if pattern.search(title):
                    description = video['snippet'].get('description', '')

                    # Verificar se ja tem timestamps
                    timestamp_pattern = re.compile(r'\b\d{1,2}:\d{2}(?::\d{2})?\b')
                    has_timestamps = len(timestamp_pattern.findall(description)) >= 3

                    if not has_timestamps:
                        pending.append({
                            'video_id': video['id'],
                            'title': title,
                            'published_at': video['snippet']['publishedAt'],
                            'url': f"https://youtube.com/watch?v={video['id']}"
                        })

            next_page_token = playlist_response.get('nextPageToken')
            if not next_page_token:
                break

        return pending


def main():
    parser = argparse.ArgumentParser(
        description='Processador de lives Estudos Avancados (membros nivel 2)',
        epilog='''
Exemplos:
  python estudos_avancados_processor.py VIDEO_ID           # Processa video
  python estudos_avancados_processor.py VIDEO_ID --dry-run # Preview sem alterar
  python estudos_avancados_processor.py --list-pending     # Lista pendentes
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('video_id', nargs='?', help='YouTube video ID')
    parser.add_argument('--dry-run', action='store_true', help='Preview sem postar no YouTube')
    parser.add_argument('--list-pending', action='store_true', help='Lista videos pendentes de processamento')
    parser.add_argument('--credentials', default='client_secrets.json', help='Path para client_secrets.json')

    args = parser.parse_args()

    if args.list_pending:
        print("=" * 60)
        print("ESTUDOS AVANCADOS - Videos Pendentes")
        print("=" * 60)

        manager = EstudosAvancadosManager(credentials_file=args.credentials)
        pending = manager.list_pending_estudos_avancados()

        if not pending:
            print("\nNenhum video pendente encontrado!")
        else:
            print(f"\n{len(pending)} videos pendentes:\n")
            for i, video in enumerate(pending, 1):
                published = datetime.fromisoformat(video['published_at'].replace('Z', '+00:00'))
                print(f"{i}. [{published.strftime('%d/%m/%Y')}] {video['title']}")
                print(f"   {video['url']}")
                print()

        return 0

    if not args.video_id:
        parser.print_help()
        return 1

    try:
        manager = EstudosAvancadosManager(credentials_file=args.credentials)
        result = manager.process_video(args.video_id, dry_run=args.dry_run)
        return 0
    except Exception as e:
        logger.error(f"Erro: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    exit(main())
