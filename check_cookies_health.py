#!/usr/bin/env python3
"""Health check for YouTube cookies. Sends Telegram alert if cookies are expired."""

import os
import sys
import json
import urllib.request
import urllib.parse
import subprocess
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8451611143:AAEpRSapS0mcfRez1stz9W9UaFOCCFllR1c')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '1727040437')

# Members-only video to test authentication
TEST_VIDEO_ID = '53Ft9fLaiCE'  # Estudos Avançados - Live 4


def send_telegram(message: str):
    """Send a message via Telegram bot."""
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    data = urllib.parse.urlencode({
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML',
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        logger.info('Telegram alert sent')
    except Exception as e:
        logger.error(f'Failed to send Telegram alert: {e}')


def check_cookies() -> bool:
    """Test if cookies can access a members-only video. Returns True if OK."""
    cookies_file = 'youtube_cookies.txt'
    if not os.path.exists(cookies_file):
        logger.error(f'{cookies_file} not found')
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
    logger.info('Starting cookie health check...')
    if check_cookies():
        logger.info('Cookies are valid')
        # Optionally send success message (uncomment if desired)
        # send_telegram('✅ youtube-adm: cookies válidos')
    else:
        send_telegram(
            '🚨 <b>youtube-adm: cookies expirados!</b>\n\n'
            'Os cookies do YouTube não estão funcionando.\n'
            'Os jobs de transcrição de vídeos members-only vão falhar.\n\n'
            '<b>Para renovar (no Mac):</b>\n\n'
            '1. Abra um vídeo members-only no Chrome\n'
            '2. Clique na extensão "Get cookies.txt LOCALLY"\n'
            '3. Clique no botão azul <b>"Export"</b> (NÃO o amarelo "Export All Cookies")\n'
            '4. Converta para base64:\n'
            '<code>cat ~/Downloads/youtube.com_cookies.txt | base64</code>\n\n'
            '5. Copie o output e cole em:\n'
            '   Dokploy → youtube-adm → Environment → <b>YOUTUBE_COOKIES</b>'
        )
        sys.exit(1)


if __name__ == '__main__':
    main()
