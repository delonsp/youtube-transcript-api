#!/usr/bin/env python3
"""
Script para gerenciar documento de resumos de lives no Google Docs.

Funcionalidades:
- Ler documento e identificar lives já documentadas
- Buscar lives do canal a partir de uma data
- Criar novas entradas no documento para lives sem resumo
"""

import os
import re
import pickle
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Escopos necessários
SCOPES = [
    'https://www.googleapis.com/auth/documents',  # Google Docs
    'https://www.googleapis.com/auth/youtube.force-ssl',  # YouTube
]

# ID do documento (extraído da URL)
DOCUMENT_ID = '1wUM7wHVIK5C46Tqp30e-DqMSy1gqg5wt72Ppi701U4g'

# ID do canal (será detectado automaticamente)
CHANNEL_ID = None


class GoogleDocsManager:
    """Gerenciador de documento de resumos de lives."""

    def __init__(self):
        self.creds = None
        self.docs_service = None
        self.youtube_service = None
        self.token_path = Path('token_docs.pickle')
        self.client_secrets_path = Path('client_secrets.json')

    def authenticate(self):
        """Autentica com Google APIs (Docs + YouTube)."""

        # Tentar carregar token existente
        if self.token_path.exists():
            with open(self.token_path, 'rb') as token:
                self.creds = pickle.load(token)

        # Se não tem credenciais válidas, autenticar
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                logger.info("Refreshing expired token...")
                self.creds.refresh(Request())
            else:
                if not self.client_secrets_path.exists():
                    raise FileNotFoundError(
                        f"Arquivo {self.client_secrets_path} não encontrado!\n"
                        "Baixe em: Google Cloud Console → APIs & Services → Credentials"
                    )

                logger.info("Starting OAuth flow...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.client_secrets_path),
                    SCOPES
                )
                self.creds = flow.run_local_server(port=8080)

            # Salvar token
            with open(self.token_path, 'wb') as token:
                pickle.dump(self.creds, token)
            logger.info(f"Token saved to {self.token_path}")

        # Criar serviços
        self.docs_service = build('docs', 'v1', credentials=self.creds)
        self.youtube_service = build('youtube', 'v3', credentials=self.creds)

        logger.info("✅ Authenticated successfully!")
        return True

    def read_document(self) -> dict:
        """Lê o documento e retorna seu conteúdo."""
        logger.info(f"Reading document {DOCUMENT_ID}...")

        doc = self.docs_service.documents().get(documentId=DOCUMENT_ID).execute()

        title = doc.get('title', 'Untitled')
        logger.info(f"Document title: {title}")

        return doc

    def extract_text_from_doc(self, doc: dict) -> str:
        """Extrai texto puro do documento."""
        text_parts = []

        content = doc.get('body', {}).get('content', [])

        for element in content:
            if 'paragraph' in element:
                for elem in element['paragraph'].get('elements', []):
                    if 'textRun' in elem:
                        text_parts.append(elem['textRun'].get('content', ''))

        return ''.join(text_parts)

    def extract_hyperlinks_from_doc(self, doc: dict) -> list[dict]:
        """
        Extrai hyperlinks do documento.

        Returns:
            Lista de dicts com text e url
        """
        hyperlinks = []
        content = doc.get('body', {}).get('content', [])

        for element in content:
            if 'paragraph' in element:
                for elem in element['paragraph'].get('elements', []):
                    if 'textRun' in elem:
                        text_run = elem['textRun']
                        text = text_run.get('content', '').strip()
                        style = text_run.get('textStyle', {})
                        link = style.get('link', {})
                        url = link.get('url', '')

                        if url:
                            hyperlinks.append({'text': text, 'url': url})

        return hyperlinks

    def find_documented_lives_from_doc(self, doc: dict) -> list[dict]:
        """
        Encontra lives já documentadas através dos hyperlinks.

        As datas no documento são hyperlinks para as lives do YouTube.

        Returns:
            Lista de dicts com video_id e url
        """
        documented = []

        # Extrair hyperlinks
        hyperlinks = self.extract_hyperlinks_from_doc(doc)

        # Padrões de URL do YouTube para extrair video ID
        url_patterns = [
            r'youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
            r'youtu\.be/([a-zA-Z0-9_-]{11})',
            r'youtube\.com/live/([a-zA-Z0-9_-]{11})',
        ]

        video_ids = set()
        for hyperlink in hyperlinks:
            url = hyperlink['url']
            for pattern in url_patterns:
                match = re.search(pattern, url)
                if match:
                    video_ids.add(match.group(1))
                    break

        logger.info(f"Found {len(video_ids)} video IDs in document hyperlinks")

        for video_id in video_ids:
            documented.append({
                'video_id': video_id,
                'url': f'https://youtube.com/watch?v={video_id}'
            })

        return documented

    def find_documented_lives(self, doc_text: str) -> list[dict]:
        """
        Encontra lives já documentadas no texto (fallback).

        Procura por padrões como:
        - URLs do YouTube (youtube.com/watch?v=, youtu.be/)

        Returns:
            Lista de dicts com informações das lives encontradas
        """
        documented = []

        # Padrões de URL do YouTube
        url_patterns = [
            r'youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
            r'youtu\.be/([a-zA-Z0-9_-]{11})',
            r'youtube\.com/live/([a-zA-Z0-9_-]{11})',
        ]

        video_ids = set()
        for pattern in url_patterns:
            matches = re.findall(pattern, doc_text)
            video_ids.update(matches)

        logger.info(f"Found {len(video_ids)} video IDs in document text")

        for video_id in video_ids:
            documented.append({
                'video_id': video_id,
                'url': f'https://youtube.com/watch?v={video_id}'
            })

        return documented

    def get_channel_lives(self, since_date: datetime, max_results: int = 100) -> list[dict]:
        """
        Busca lives do canal a partir de uma data.

        Args:
            since_date: Data inicial para buscar
            max_results: Máximo de resultados

        Returns:
            Lista de lives encontradas
        """
        logger.info(f"Searching for lives since {since_date.strftime('%Y-%m-%d')}...")

        # Primeiro, obter o canal autenticado
        channels_response = self.youtube_service.channels().list(
            part='snippet,contentDetails',
            mine=True
        ).execute()

        if not channels_response.get('items'):
            raise ValueError("No channel found for authenticated user")

        channel = channels_response['items'][0]
        channel_id = channel['id']
        channel_name = channel['snippet']['title']
        uploads_playlist = channel['contentDetails']['relatedPlaylists']['uploads']

        logger.info(f"Channel: {channel_name} ({channel_id})")

        # Buscar vídeos do canal
        lives = []
        next_page_token = None

        while len(lives) < max_results:
            playlist_response = self.youtube_service.playlistItems().list(
                part='snippet,contentDetails',
                playlistId=uploads_playlist,
                maxResults=min(50, max_results - len(lives)),
                pageToken=next_page_token
            ).execute()

            video_ids = [
                item['contentDetails']['videoId']
                for item in playlist_response.get('items', [])
            ]

            if not video_ids:
                break

            # Obter detalhes dos vídeos
            videos_response = self.youtube_service.videos().list(
                part='snippet,liveStreamingDetails,contentDetails',
                id=','.join(video_ids)
            ).execute()

            for video in videos_response.get('items', []):
                # Verificar se é uma live
                if 'liveStreamingDetails' not in video:
                    continue

                # Verificar data
                published_at = video['snippet']['publishedAt']
                published_date = datetime.fromisoformat(published_at.replace('Z', '+00:00'))

                if published_date.replace(tzinfo=None) < since_date:
                    # Chegamos em vídeos anteriores à data, podemos parar
                    logger.info(f"Reached videos before {since_date.strftime('%Y-%m-%d')}, stopping")
                    return lives

                lives.append({
                    'video_id': video['id'],
                    'title': video['snippet']['title'],
                    'published_at': published_at,
                    'url': f"https://youtube.com/watch?v={video['id']}",
                    'duration': video['contentDetails'].get('duration', ''),
                    'thumbnail': video['snippet']['thumbnails'].get('high', {}).get('url', ''),
                })

            next_page_token = playlist_response.get('nextPageToken')
            if not next_page_token:
                break

        logger.info(f"Found {len(lives)} lives since {since_date.strftime('%Y-%m-%d')}")
        return lives

    def find_missing_lives(self, doc: dict, since_date: datetime) -> list[dict]:
        """
        Encontra lives que não estão documentadas.

        Args:
            doc: Documento do Google Docs
            since_date: Data inicial para buscar

        Returns:
            Lista de lives não documentadas
        """
        # Lives já documentadas (via hyperlinks)
        documented = self.find_documented_lives_from_doc(doc)
        documented_ids = {d['video_id'] for d in documented}

        logger.info(f"Document has {len(documented_ids)} lives documented")

        # Lives do canal
        channel_lives = self.get_channel_lives(since_date)

        # Filtrar lives não documentadas
        missing = [
            live for live in channel_lives
            if live['video_id'] not in documented_ids
        ]

        logger.info(f"Found {len(missing)} lives NOT documented")

        return missing

    def format_date_portuguese(self, dt: datetime) -> str:
        """Formata data em português extenso (ex: 15 de Setembro de 2022)."""
        meses = [
            'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
            'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro'
        ]
        return f"{dt.day} de {meses[dt.month - 1]} de {dt.year}"

    def create_entry_text(self, live: dict) -> str:
        """
        Cria texto de entrada para uma live no formato do documento.

        Formato:
        [Data em português]

        <summary>
        [Resumo - a ser preenchido]
        </summary>

        <qa_list>
        - Pergunta: [pergunta]
          Resposta: [resposta]
        </qa_list>

        Args:
            live: Dict com informações da live

        Returns:
            Texto formatado para inserir no documento
        """
        published = datetime.fromisoformat(live['published_at'].replace('Z', '+00:00'))
        date_str = self.format_date_portuguese(published)

        # Formato igual ao documento existente
        entry = f"""{date_str}
{live['title']}
{live['url']}

<summary>
[Resumo a ser preenchido]
</summary>

<qa_list>
- Pergunta: [Pergunta 1]
  Resposta: [Resposta 1]
</qa_list>

"""
        return entry

    def append_to_document(self, text: str):
        """
        Adiciona texto ao final do documento.

        Args:
            text: Texto a ser adicionado
        """
        # Obter documento para saber o índice final
        doc = self.docs_service.documents().get(documentId=DOCUMENT_ID).execute()

        # Encontrar o índice final
        end_index = doc.get('body', {}).get('content', [])[-1].get('endIndex', 1) - 1

        # Inserir texto
        requests = [
            {
                'insertText': {
                    'location': {'index': end_index},
                    'text': text
                }
            }
        ]

        self.docs_service.documents().batchUpdate(
            documentId=DOCUMENT_ID,
            body={'requests': requests}
        ).execute()

        logger.info(f"Added {len(text)} characters to document")

    def append_live_entry(self, live: dict):
        """
        Adiciona uma entrada de live ao documento com a data como hyperlink.

        Args:
            live: Dict com informações da live
        """
        # Obter documento para saber o índice final
        doc = self.docs_service.documents().get(documentId=DOCUMENT_ID).execute()
        end_index = doc.get('body', {}).get('content', [])[-1].get('endIndex', 1) - 1

        # Formatar data
        published = datetime.fromisoformat(live['published_at'].replace('Z', '+00:00'))
        date_str = self.format_date_portuguese(published)

        # Texto da entrada (sem a data, que será inserida como hyperlink)
        entry_text = f"""

<summary>
[Resumo a ser preenchido]
</summary>

<qa_list>
- Pergunta: [Pergunta 1]
  Resposta: [Resposta 1]
</qa_list>

"""

        # Requests para inserir texto e aplicar hyperlink
        requests = [
            # 1. Inserir a data
            {
                'insertText': {
                    'location': {'index': end_index},
                    'text': date_str + '\n'
                }
            },
            # 2. Aplicar hyperlink na data
            {
                'updateTextStyle': {
                    'range': {
                        'startIndex': end_index,
                        'endIndex': end_index + len(date_str)
                    },
                    'textStyle': {
                        'link': {
                            'url': live['url']
                        }
                    },
                    'fields': 'link'
                }
            },
            # 3. Inserir o resto do texto
            {
                'insertText': {
                    'location': {'index': end_index + len(date_str) + 1},
                    'text': entry_text
                }
            }
        ]

        self.docs_service.documents().batchUpdate(
            documentId=DOCUMENT_ID,
            body={'requests': requests}
        ).execute()

        logger.info(f"Added entry for: {date_str}")

    def add_missing_lives_entries(self, since_date: datetime, dry_run: bool = True) -> list[dict]:
        """
        Adiciona entradas para lives não documentadas.

        Args:
            since_date: Data inicial para buscar lives
            dry_run: Se True, apenas mostra o que seria adicionado sem modificar

        Returns:
            Lista de lives que foram (ou seriam) adicionadas
        """
        # Ler documento
        doc = self.read_document()

        # Encontrar lives faltando (usando hyperlinks do documento)
        missing = self.find_missing_lives(doc, since_date)

        if not missing:
            logger.info("✅ All lives are documented!")
            return []

        # Ordenar por data (mais antiga primeiro)
        missing.sort(key=lambda x: x['published_at'])

        print(f"\n📋 Lives não documentadas ({len(missing)}):\n")
        for i, live in enumerate(missing, 1):
            published = datetime.fromisoformat(live['published_at'].replace('Z', '+00:00'))
            print(f"{i}. [{published.strftime('%d/%m/%Y')}] {live['title']}")
            print(f"   {live['url']}\n")

        if dry_run:
            print("\n⚠️  Modo DRY RUN - nenhuma alteração foi feita")
            print("   Execute com --add para adicionar as entradas")
            return missing

        # Criar e adicionar entradas (uma por uma, com hyperlink na data)
        print("\n📝 Adicionando entradas ao documento...")

        for i, live in enumerate(missing, 1):
            try:
                self.append_live_entry(live)
                published = datetime.fromisoformat(live['published_at'].replace('Z', '+00:00'))
                print(f"   ✅ {i}/{len(missing)}: {self.format_date_portuguese(published)}")
            except Exception as e:
                logger.error(f"Error adding entry for {live['video_id']}: {e}")
                print(f"   ❌ {i}/{len(missing)}: Erro - {e}")

        print(f"\n✅ {len(missing)} entradas adicionadas ao documento!")
        return missing


def main():
    """Função principal."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Gerenciador de documento de resumos de lives'
    )
    parser.add_argument(
        '--since',
        type=str,
        default='2024-11-20',
        help='Data inicial para buscar lives (formato: YYYY-MM-DD). Default: 2024-11-20'
    )
    parser.add_argument(
        '--add',
        action='store_true',
        help='Adicionar entradas para lives faltando (sem isso, apenas lista)'
    )
    parser.add_argument(
        '--list-only',
        action='store_true',
        help='Apenas listar lives documentadas no documento'
    )

    args = parser.parse_args()

    # Parse date
    since_date = datetime.strptime(args.since, '%Y-%m-%d')

    # Criar gerenciador
    manager = GoogleDocsManager()

    # Autenticar
    print("🔐 Autenticando com Google APIs...")
    manager.authenticate()

    if args.list_only:
        # Apenas ler e listar
        doc = manager.read_document()
        doc_text = manager.extract_text_from_doc(doc)
        documented = manager.find_documented_lives(doc_text)

        print(f"\n📄 Lives documentadas ({len(documented)}):\n")
        for d in documented:
            print(f"  - {d['url']}")
        return

    # Verificar e adicionar lives faltando
    manager.add_missing_lives_entries(
        since_date=since_date,
        dry_run=not args.add
    )


if __name__ == '__main__':
    main()
