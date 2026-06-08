#!/usr/bin/env python3
"""Health check for YouTube authentication. Tests Captions API (primary) and cookies (fallback)."""

import os
import sys
import pickle
import subprocess
import logging

from telegram_utils import send_telegram

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Members-only video to test authentication
TEST_VIDEO_ID = '53Ft9fLaiCE'  # Estudos Avancados - Live 4


def check_captions_api() -> bool:
    """Test if YouTube Captions API can access a members-only video via OAuth. Returns True if OK."""
    token_file = 'token_captions.pickle'
    if not os.path.exists(token_file):
        logger.error(f'{token_file} not found')
        return False

    try:
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        with open(token_file, 'rb') as f:
            creds = pickle.load(f)

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_file, 'wb') as f:
                pickle.dump(creds, f)
            logger.info('OAuth token refreshed')

        youtube = build('youtube', 'v3', credentials=creds)
        response = youtube.captions().list(part='snippet', videoId=TEST_VIDEO_ID).execute()
        items = response.get('items', [])

        if items:
            logger.info(f'Captions API OK - {len(items)} caption tracks found')
            return True
        else:
            logger.warning('Captions API returned no caption tracks')
            return False

    except Exception as e:
        logger.error(f'Captions API check failed: {e}')
        return False


def check_analytics_api() -> bool:
    """Test the YouTube Analytics API token with a minimal 1-day query. Returns True if OK."""
    token_file = 'token_analytics.pickle'
    if not os.path.exists(token_file):
        logger.warning(f'{token_file} not found (metrics job will fail)')
        return False

    try:
        from datetime import date, timedelta
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        with open(token_file, 'rb') as f:
            creds = pickle.load(f)

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_file, 'wb') as f:
                pickle.dump(creds, f)
            logger.info('Analytics OAuth token refreshed')

        analytics = build('youtubeAnalytics', 'v2', credentials=creds)
        day = (date.today() - timedelta(days=7)).isoformat()
        analytics.reports().query(
            ids='channel==MINE', startDate=day, endDate=day, metrics='views',
        ).execute()
        logger.info('Analytics API OK')
        return True

    except Exception as e:
        logger.error(f'Analytics API check failed: {e}')
        return False


def check_cookies() -> bool:
    """Test if cookies can access a members-only video. Returns True if OK."""
    cookies_file = 'youtube_cookies.txt'
    if not os.path.exists(cookies_file):
        logger.warning(f'{cookies_file} not found (optional fallback)')
        return False

    try:
        result = subprocess.run(
            [
                'yt-dlp',
                '--cookies', cookies_file,
                '--skip-download',
                '--print', 'title',
                f'https://www.youtube.com/watch?v={TEST_VIDEO_ID}',
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info(f'Cookies OK - video title: {result.stdout.strip()}')
            return True
        else:
            logger.warning(f'Cookie check failed: {result.stderr.strip()[:200]}')
            return False
    except subprocess.TimeoutExpired:
        logger.warning('Cookie check timed out')
        return False
    except Exception as e:
        logger.error(f'Cookie check error: {e}')
        return False


def main():
    logger.info('Starting auth health check...')

    api_ok = check_captions_api()
    cookies_ok = check_cookies()
    analytics_ok = check_analytics_api()

    if not analytics_ok:
        logger.warning('Analytics API token FAILED (metrics job will fail)')
        send_telegram(
            '⚠️ <b>youtube-adm: token de Analytics com problema!</b>\n\n'
            'O job diario de metricas do canal (11h UTC) vai falhar.\n\n'
            '<b>Para corrigir (no Mac):</b>\n'
            '1. Rode: <code>python channel_metrics_report.py --dry-run</code>\n'
            '2. Siga o fluxo OAuth no browser\n'
            '3. Atualize token_analytics.pickle no Dokploy:\n'
            '<code>cat token_analytics.pickle | base64</code>\n'
            '→ Dokploy → Environment → TOKEN_ANALYTICS_B64'
        )

    if api_ok and cookies_ok:
        logger.info('Captions API and cookies healthy')
    elif api_ok and not cookies_ok:
        # Captions API works, cookies expired — informational only
        logger.info('Captions API OK, cookies expired (no action needed)')
        send_telegram(
            'ℹ️ <b>youtube-adm: cookies expiraram</b>\n\n'
            'Captions API (OAuth) funcionando normalmente.\n'
            'Cookies sao apenas fallback — <b>nenhuma acao necessaria</b>.\n\n'
            'Os jobs de transcricao continuam funcionando via OAuth.'
        )
    elif not api_ok and cookies_ok:
        # Captions API broken but cookies work — urgent, needs attention
        logger.warning('Captions API FAILED, cookies still working')
        send_telegram(
            '⚠️ <b>youtube-adm: Captions API com problema!</b>\n\n'
            'OAuth token pode estar expirado ou revogado.\n'
            'Cookies ainda funcionam como fallback.\n\n'
            '<b>Para corrigir (no Mac):</b>\n'
            '1. Rode: <code>python download_via_api.py --max 1</code>\n'
            '2. Siga o fluxo OAuth no browser\n'
            '3. Atualize token_captions.pickle no Dokploy:\n'
            '<code>cat token_captions.pickle | base64</code>\n'
            '→ Dokploy → Environment → TOKEN_CAPTIONS_B64'
        )
        sys.exit(1)
    else:
        # Both broken — critical
        logger.error('ALL auth methods FAILED')
        send_telegram(
            '🚨 <b>youtube-adm: TODOS os metodos de auth falharam!</b>\n\n'
            'Captions API (OAuth) E cookies estao quebrados.\n'
            'Os jobs de transcricao members-only VAO FALHAR.\n\n'
            '<b>Prioridade 1 — Corrigir OAuth:</b>\n'
            '1. Rode: <code>python download_via_api.py --max 1</code>\n'
            '2. Siga o fluxo OAuth no browser\n'
            '3. Atualize token_captions.pickle no Dokploy:\n'
            '<code>cat token_captions.pickle | base64</code>\n'
            '→ Dokploy → Environment → TOKEN_CAPTIONS_B64\n\n'
            '<b>Prioridade 2 (opcional) — Renovar cookies:</b>\n'
            '1. Abra video members-only no Chrome\n'
            '2. Extensao "Get cookies.txt LOCALLY" → Export\n'
            '3. <code>cat ~/Downloads/youtube.com_cookies.txt | base64</code>\n'
            '→ Dokploy → Environment → YOUTUBE_COOKIES'
        )
        sys.exit(1)

    if not analytics_ok:
        sys.exit(1)


if __name__ == '__main__':
    main()
