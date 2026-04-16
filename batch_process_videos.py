#!/usr/bin/env python3
"""
Script para processar em batch vídeos de membros sem timestamps

Este script:
1. Lista todos os vídeos do canal
2. Filtra vídeos de membros (lives)
3. Verifica se já tem timestamps na descrição ou comentários
4. Processa automaticamente os que não têm timestamps
"""

import os
import sys
import logging
import subprocess
from typing import List, Dict, Optional
from datetime import datetime
import pickle

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class YouTubeBatchProcessor:
    """Processa vídeos em batch"""

    def __init__(self, credentials_file: str = 'client_secrets.json'):
        self.credentials_file = credentials_file
        self.youtube = None

    def authenticate(self):
        """Autenticar com YouTube Data API"""
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        SCOPES = ['https://www.googleapis.com/auth/youtube.force-ssl']

        creds = None
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)

            with open('token.pickle', 'wb') as token:
                pickle.dump(creds, token)

        self.youtube = build('youtube', 'v3', credentials=creds)
        logger.info("✅ Autenticado com YouTube Data API")

    def get_channel_id(self) -> str:
        """Obter ID do canal autenticado"""
        request = self.youtube.channels().list(
            part="id",
            mine=True
        )
        response = request.execute()

        if not response['items']:
            raise Exception("Nenhum canal encontrado para esta conta")

        channel_id = response['items'][0]['id']
        logger.info(f"📺 Canal ID: {channel_id}")
        return channel_id

    def list_all_videos(self, channel_id: str, max_results: int = 500) -> List[Dict]:
        """Listar todos os vídeos do canal usando uploads playlist (inclui members-only)"""
        logger.info(f"🔍 Buscando vídeos do canal...")

        # Primeiro, obter o ID da playlist de uploads
        request = self.youtube.channels().list(
            part="contentDetails",
            id=channel_id
        )
        response = request.execute()

        if not response['items']:
            raise Exception("Canal não encontrado")

        uploads_playlist_id = response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        logger.info(f"📋 Playlist de uploads: {uploads_playlist_id}")

        videos = []
        page_token = None

        while len(videos) < max_results:
            # Buscar vídeos da playlist de uploads
            # IMPORTANTE: Usar playlistItems em vez de search inclui vídeos members-only!
            request = self.youtube.playlistItems().list(
                part="snippet",
                playlistId=uploads_playlist_id,
                maxResults=min(50, max_results - len(videos)),
                pageToken=page_token
            )
            response = request.execute()

            for item in response['items']:
                snippet = item['snippet']
                video_id = snippet['resourceId']['videoId']

                videos.append({
                    'video_id': video_id,
                    'title': snippet['title'],
                    'published_at': snippet['publishedAt'],
                    'description': snippet['description']
                })

            page_token = response.get('nextPageToken')
            if not page_token:
                break

            logger.info(f"  Encontrados {len(videos)} vídeos até agora...")

        logger.info(f"✅ Total de vídeos encontrados: {len(videos)}")
        return videos

    def is_live_video(self, video_id: str) -> bool:
        """Verificar se vídeo é uma live (transmissão ao vivo)"""
        try:
            request = self.youtube.videos().list(
                part="snippet,liveStreamingDetails",
                id=video_id
            )
            response = request.execute()

            if not response['items']:
                return False

            video = response['items'][0]
            snippet = video['snippet']

            # 1. Verificar se tem liveStreamingDetails (foi uma live)
            has_live_details = 'liveStreamingDetails' in video

            # 2. Verificar liveBroadcastContent
            live_broadcast = snippet.get('liveBroadcastContent', 'none')
            is_live = live_broadcast in ['live', 'upcoming', 'completed']

            return has_live_details or is_live

        except Exception as e:
            logger.error(f"Erro ao verificar se é live {video_id}: {e}")
            return False

    def is_members_only_video(self, video_id: str) -> bool:
        """
        Verificar se vídeo é de membros.

        A API do YouTube não expõe diretamente se é members-only,
        então tentamos acessar a transcrição - se falhar com erro de
        "Join this channel", sabemos que é members-only.
        """
        try:
            from youtube_transcript_api import YouTubeTranscriptApi

            ytt_api = YouTubeTranscriptApi()

            # Tentar buscar transcrição
            try:
                ytt_api.list(video_id)
                # Se conseguiu listar, não é members-only
                return False
            except Exception as e:
                error_msg = str(e)

                # Se erro contém "Join this channel", é members-only
                if "Join this channel" in error_msg or "members-only" in error_msg:
                    return True

                # Outros erros (sem transcrição, etc) = não é members-only
                return False

        except Exception as e:
            logger.error(f"Erro ao verificar members-only {video_id}: {e}")
            return False

    def has_timestamps(self, video_id: str, description: str) -> bool:
        """Verificar se vídeo já tem timestamps na descrição ou comentários"""
        import re

        # Timestamp pattern: 0:00, 00:00, 1:23:45, etc.
        timestamp_pattern = re.compile(r'\b\d{1,2}:\d{2}(?::\d{2})?\b')

        # 1. Verificar descrição
        timestamps_in_desc = timestamp_pattern.findall(description)
        if len(timestamps_in_desc) >= 3:  # Pelo menos 3 timestamps
            logger.info(f"  ✅ {video_id}: Tem timestamps na descrição")
            return True

        # 2. Verificar comentários do dono do canal
        try:
            # Primeiro, pegar o channel_id do vídeo
            video_response = self.youtube.videos().list(
                part="snippet",
                id=video_id
            ).execute()

            if not video_response['items']:
                logger.info(f"  ❌ {video_id}: Sem timestamps")
                return False

            channel_id = video_response['items'][0]['snippet']['channelId']

            # Buscar comentários
            request = self.youtube.commentThreads().list(
                part="snippet",
                videoId=video_id,
                maxResults=30,  # Aumentar para pegar mais comentários
                order="relevance"
            )
            response = request.execute()

            # Keywords que indicam comentários com timestamps
            timestamp_keywords = [
                'timestamp', 'timestamps', 'marcações', 'marcacoes',
                'key points', 'pontos chave', 'navigation', 'navegação',
                'índice', 'indice', 'chapters', 'capítulos', 'capitulos',
                '📌', '🎯', '⏰', '🕐'
            ]

            for item in response.get('items', []):
                comment = item['snippet']['topLevelComment']['snippet']
                author_channel_id = comment.get('authorChannelId', {}).get('value', '')
                comment_text = comment['textDisplay']
                comment_text_lower = comment_text.lower()

                # Só verificar comentários do dono do canal
                if author_channel_id == channel_id:
                    timestamps_found = timestamp_pattern.findall(comment_text)

                    # Se tem 3+ timestamps, considera que já tem marcações
                    if len(timestamps_found) >= 3:
                        logger.info(f"  ✅ {video_id}: Tem timestamps em comentário do canal")
                        return True

                    # Se tem pelo menos 1 timestamp + keyword
                    if len(timestamps_found) >= 1:
                        for keyword in timestamp_keywords:
                            if keyword in comment_text_lower or keyword in comment_text:
                                logger.info(f"  ✅ {video_id}: Tem timestamps em comentário do canal")
                                return True

        except Exception as e:
            # Se comentários desabilitados ou erro, ignorar
            logger.debug(f"  ⚠️  Erro ao verificar comentários de {video_id}: {e}")
            pass

        logger.info(f"  ❌ {video_id}: Sem timestamps")
        return False

    def filter_members_only_lives(self, videos: List[Dict]) -> List[Dict]:
        """Filtrar apenas lives de membros"""
        logger.info(f"\n🔍 Filtrando lives de membros em {len(videos)} vídeos...\n")

        members_only_lives = []

        for i, video in enumerate(videos, 1):
            video_id = video['video_id']
            title = video['title'][:60]

            logger.info(f"[{i}/{len(videos)}] {title}...")

            # Verificar se é live
            if not self.is_live_video(video_id):
                logger.info(f"  ⏭️  Não é live - pulando")
                continue

            logger.info(f"  ✅ É live")

            # Verificar se é members-only
            if not self.is_members_only_video(video_id):
                logger.info(f"  ⏭️  Não é members-only - pulando")
                continue

            logger.info(f"  🔒 É members-only")

            members_only_lives.append(video)

        logger.info(f"\n✅ Lives de membros encontradas: {len(members_only_lives)}/{len(videos)}")
        return members_only_lives

    def group_sibling_videos(self, videos: List[Dict]) -> List[List[Dict]]:
        """Agrupar vídeos irmãos (mesma live em diferentes formatos)"""
        from datetime import datetime, timedelta

        logger.info(f"\n🔗 Agrupando vídeos irmãos (mesma live, formatos diferentes)...\n")

        # Obter detalhes de live para todos os vídeos
        video_details = {}
        for video in videos:
            video_id = video['video_id']

            try:
                request = self.youtube.videos().list(
                    part="liveStreamingDetails",
                    id=video_id
                )
                response = request.execute()

                if response['items'] and 'liveStreamingDetails' in response['items'][0]:
                    live_details = response['items'][0]['liveStreamingDetails']
                    video_details[video_id] = {
                        'video': video,
                        'start': live_details.get('actualStartTime'),
                        'end': live_details.get('actualEndTime')
                    }
            except Exception as e:
                logger.error(f"Erro ao obter detalhes de {video_id}: {e}")

        # Agrupar vídeos com mesmo horário de início (diferença < 10 segundos)
        groups = []
        used = set()

        for vid_id, details in video_details.items():
            if vid_id in used:
                continue

            group = [details['video']]
            used.add(vid_id)

            if not details['start']:
                groups.append(group)
                continue

            start_time = datetime.fromisoformat(details['start'].replace('Z', '+00:00'))

            # Procurar irmãos (mesmo horário de início)
            for other_id, other_details in video_details.items():
                if other_id in used or not other_details['start']:
                    continue

                other_start = datetime.fromisoformat(other_details['start'].replace('Z', '+00:00'))
                time_diff = abs((start_time - other_start).total_seconds())

                # Se começaram com menos de 10 segundos de diferença, são irmãos
                if time_diff < 10:
                    # Verificar se títulos são similares (ignorando emoji)
                    title1 = details['video']['title'].replace('📱', '').strip()
                    title2 = other_details['video']['title'].replace('📱', '').strip()

                    if title1 == title2:
                        group.append(other_details['video'])
                        used.add(other_id)
                        logger.info(f"  🔗 Vídeos irmãos detectados:")
                        logger.info(f"     - {details['video']['video_id']}: {details['video']['title'][:50]}")
                        logger.info(f"     - {other_id}: {other_details['video']['title'][:50]}")

            groups.append(group)

        logger.info(f"\n✅ {len(videos)} vídeos agrupados em {len(groups)} grupos")
        return groups

    def filter_videos_without_timestamps(self, videos: List[Dict]) -> List[Dict]:
        """Filtrar vídeos que NÃO têm timestamps"""
        logger.info(f"\n🔍 Verificando timestamps em {len(videos)} vídeos...\n")

        videos_without_timestamps = []

        for i, video in enumerate(videos, 1):
            logger.info(f"[{i}/{len(videos)}] {video['title'][:60]}...")

            if not self.has_timestamps(video['video_id'], video['description']):
                videos_without_timestamps.append(video)
                logger.info(f"  ❌ Sem timestamps")
            else:
                logger.info(f"  ✅ Já tem timestamps")

        logger.info(f"\n✅ Vídeos SEM timestamps: {len(videos_without_timestamps)}/{len(videos)}")
        return videos_without_timestamps

    def process_video(self, video_id: str, dry_run: bool = False) -> bool:
        """Processar um vídeo com local_workflow.py"""
        logger.info(f"\n{'🔍 [DRY RUN]' if dry_run else '🚀'} Processando vídeo: {video_id}")

        cmd = [
            'python',
            'transcript_processor.py',
            '--cookies', 'chrome' if not os.path.exists('/.dockerenv') else 'youtube_cookies.txt',
            '--captions-token', 'token_captions.pickle',
            '--',  # Separador para IDs que começam com hífen (ex: -YMooVl3oms)
            video_id
        ]

        if dry_run:
            cmd.append('--dry-run')

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600  # 10 minutos timeout
            )

            if result.returncode == 0:
                logger.info(f"✅ Vídeo {video_id} processado com sucesso!")
                return True
            else:
                logger.error(f"❌ Erro ao processar {video_id}")
                logger.error(result.stderr)
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"❌ Timeout ao processar {video_id}")
            return False
        except Exception as e:
            logger.error(f"❌ Erro ao processar {video_id}: {e}")
            return False

    def process_video_group(self, video_group: List[Dict], dry_run: bool = False) -> bool:
        """Processar grupo de vídeos irmãos (mesma transcrição, aplicar em todos)"""

        if len(video_group) == 1:
            # Apenas um vídeo, processar normalmente
            return self.process_video(video_group[0]['video_id'], dry_run)

        # Múltiplos vídeos irmãos - processar primeiro, aplicar em todos
        logger.info(f"\n🔗 Processando grupo de {len(video_group)} vídeos irmãos:")
        for vid in video_group:
            logger.info(f"   - {vid['video_id']}: {vid['title'][:50]}")

        primary_video = video_group[0]
        video_ids = ','.join([v['video_id'] for v in video_group])

        logger.info(f"\n📝 Usando {primary_video['video_id']} como vídeo principal para transcrição")
        logger.info(f"📋 Aplicando timestamps em todos os {len(video_group)} vídeos")

        cmd = [
            'python',
            'transcript_processor.py',
            '--cookies', 'chrome' if not os.path.exists('/.dockerenv') else 'youtube_cookies.txt',
            '--captions-token', 'token_captions.pickle',
            '--sibling-videos',
            video_ids,
            '--',  # Separador para IDs que começam com hífen (ex: -YMooVl3oms)
            primary_video['video_id']
        ]

        if dry_run:
            cmd.append('--dry-run')

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600  # 10 minutos timeout
            )

            if result.returncode == 0:
                logger.info(f"✅ Grupo de {len(video_group)} vídeos processado com sucesso!")
                return True
            else:
                logger.error(f"❌ Erro ao processar grupo")
                logger.error(result.stderr)
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"❌ Timeout ao processar grupo")
            return False
        except Exception as e:
            logger.error(f"❌ Erro ao processar grupo: {e}")
            return False


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Processar em batch vídeos de membros sem timestamps',
        epilog='Exemplos:\n'
               '  # Listar vídeos sem timestamps (sem processar):\n'
               '  python batch_process_videos.py --list-only\n\n'
               '  # Processar em dry-run (testar sem postar):\n'
               '  python batch_process_videos.py --dry-run\n\n'
               '  # Processar de verdade:\n'
               '  python batch_process_videos.py\n\n'
               '  # Limitar número de vídeos:\n'
               '  python batch_process_videos.py --max-videos 10',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        '--max-videos',
        type=int,
        default=500,
        help='Máximo de vídeos para buscar (default: 500)'
    )
    parser.add_argument(
        '--list-only',
        action='store_true',
        help='Apenas listar vídeos sem timestamps (não processar)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Processar em modo dry-run (não posta comentários/descrição)'
    )
    parser.add_argument(
        '--credentials',
        default='client_secrets.json',
        help='Path to YouTube OAuth2 credentials (default: client_secrets.json)'
    )
    parser.add_argument(
        '--start-from',
        type=int,
        default=0,
        help='Começar do vídeo N (para continuar processamento interrompido)'
    )

    args = parser.parse_args()

    try:
        processor = YouTubeBatchProcessor(credentials_file=args.credentials)

        # Autenticar
        logger.info("=" * 60)
        logger.info("STEP 1: Autenticando com YouTube")
        logger.info("=" * 60)
        processor.authenticate()

        # Obter canal
        logger.info("")
        logger.info("=" * 60)
        logger.info("STEP 2: Obtendo ID do canal")
        logger.info("=" * 60)
        channel_id = processor.get_channel_id()

        # Listar vídeos
        logger.info("")
        logger.info("=" * 60)
        logger.info("STEP 3: Listando vídeos do canal")
        logger.info("=" * 60)
        videos = processor.list_all_videos(channel_id, max_results=args.max_videos)

        # Filtrar apenas lives de membros
        logger.info("")
        logger.info("=" * 60)
        logger.info("STEP 4: Filtrando lives de membros")
        logger.info("=" * 60)
        members_lives = processor.filter_members_only_lives(videos)

        if not members_lives:
            logger.info("\n❌ Nenhuma live de membros encontrada.")
            return 0

        # Listar TODOS os vídeos de membros (com e sem timestamps) para debug
        logger.info("")
        logger.info("=" * 60)
        logger.info("STEP 5: Listando TODOS os vídeos de membros")
        logger.info("=" * 60)
        logger.info(f"\n📋 Total de lives de membros: {len(members_lives)}\n")

        for i, video in enumerate(members_lives, 1):
            published = datetime.fromisoformat(video['published_at'].replace('Z', '+00:00'))
            logger.info(f"{i:3d}. [{published.strftime('%Y-%m-%d')}] {video['title'][:70]}")
            logger.info(f"     ID: {video['video_id']}")

        # Agora filtrar vídeos sem timestamps
        logger.info("")
        logger.info("=" * 60)
        logger.info("STEP 6: Verificando timestamps")
        logger.info("=" * 60)
        videos_to_process = processor.filter_videos_without_timestamps(members_lives)

        if not videos_to_process:
            logger.info("\n🎉 Todos os vídeos já têm timestamps! Nada a fazer.")
            return 0

        # Agrupar vídeos irmãos
        logger.info("")
        logger.info("=" * 60)
        logger.info("STEP 7: Agrupando vídeos irmãos")
        logger.info("=" * 60)
        video_groups = processor.group_sibling_videos(videos_to_process)

        # Listar grupos
        logger.info(f"\n📋 {len(videos_to_process)} vídeos agrupados em {len(video_groups)} grupos:\n")
        for i, group in enumerate(video_groups, 1):
            if len(group) == 1:
                video = group[0]
                published = datetime.fromisoformat(video['published_at'].replace('Z', '+00:00'))
                logger.info(f"{i:3d}. [{published.strftime('%Y-%m-%d')}] {video['title'][:70]}")
                logger.info(f"     ID: {video['video_id']}")
            else:
                published = datetime.fromisoformat(group[0]['published_at'].replace('Z', '+00:00'))
                logger.info(f"{i:3d}. [{published.strftime('%Y-%m-%d')}] 🔗 GRUPO com {len(group)} vídeos:")
                for video in group:
                    logger.info(f"     - {video['video_id']}: {video['title'][:60]}")

        if args.list_only:
            logger.info(f"\n✅ Listagem completa. Total: {len(video_groups)} grupos ({len(videos_to_process)} vídeos)")
            return 0

        # Confirmar processamento
        logger.info("")
        logger.info("=" * 60)
        logger.info("STEP 8: Processando grupos")
        logger.info("=" * 60)

        if not args.dry_run:
            logger.info(f"\n⚠️  Você está prestes a processar {len(video_groups)} grupos ({len(videos_to_process)} vídeos)!")
            logger.info("⚠️  Isso vai:")
            logger.info("   - Baixar transcrições (1 por grupo)")
            logger.info("   - Processar com DeepSeek (1 por grupo)")
            logger.info("   - Atualizar descrições dos vídeos (todos do grupo)")
            logger.info("   - Postar comentários (todos do grupo)")
            response = input("\nDeseja continuar? (sim/não): ")
            if response.lower() not in ['sim', 's', 'yes', 'y']:
                logger.info("❌ Cancelado pelo usuário")
                return 0

        # Processar grupos
        success_count = 0
        failed_count = 0

        for i, group in enumerate(video_groups[args.start_from:], args.start_from + 1):
            logger.info(f"\n{'=' * 60}")
            if len(group) == 1:
                logger.info(f"Processando {i}/{len(video_groups)}: {group[0]['title'][:50]}...")
            else:
                logger.info(f"Processando {i}/{len(video_groups)}: GRUPO com {len(group)} vídeos")
            logger.info(f"{'=' * 60}")

            if processor.process_video_group(group, dry_run=args.dry_run):
                success_count += 1
            else:
                failed_count += 1

            logger.info(f"\n📊 Progresso: {success_count} sucesso, {failed_count} falhas")

        logger.info("")
        logger.info("=" * 60)
        logger.info("RESUMO FINAL")
        logger.info("=" * 60)
        logger.info(f"✅ Grupos processados com sucesso: {success_count}")
        logger.info(f"❌ Grupos com falhas: {failed_count}")
        logger.info(f"📊 Total de grupos: {len(video_groups)}")
        logger.info(f"📹 Total de vídeos: {len(videos_to_process)}")

        if not args.dry_run:
            logger.info("\n🎉 Processamento em batch concluído!")
        else:
            logger.info("\n🔍 Dry-run concluído. Execute sem --dry-run para processar de verdade.")

        return 0

    except KeyboardInterrupt:
        logger.info("\n\n⚠️  Interrompido pelo usuário")
        logger.info("💡 Dica: Use --start-from N para continuar de onde parou")
        return 1
    except Exception as e:
        logger.error(f"\n❌ Erro: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    exit(main())
