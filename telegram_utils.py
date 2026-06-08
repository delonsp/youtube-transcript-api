#!/usr/bin/env python3
"""Shared Telegram notification helper for cron jobs."""

import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4096
RETRY_DELAYS = (0, 5, 15)  # seconds before each attempt
_KNOWN_TAGS = ('b', 'i', 'u', 's', 'code', 'pre', 'a')


def _truncate_html(message: str, limit: int = TELEGRAM_MAX_LENGTH) -> str:
    """Truncate without breaking HTML: never cut inside a tag, and close any
    tags left open (an unbalanced tag makes Telegram reject the message)."""
    if len(message) <= limit:
        return message

    cut = message[:limit - 40]
    lt, gt = cut.rfind('<'), cut.rfind('>')
    if lt > gt:  # cut landed inside a tag
        cut = cut[:lt]

    stack = []
    for m in re.finditer(r'<(/?)([a-z]+)[^>]*>', cut):
        closing, tag = m.group(1), m.group(2)
        if tag not in _KNOWN_TAGS:
            continue
        if closing:
            if stack and stack[-1] == tag:
                stack.pop()
        else:
            stack.append(tag)

    cut += '\n[...truncado]'
    for tag in reversed(stack):
        cut += f'</{tag}>'
    return cut


def send_telegram(message: str) -> bool:
    """Send a message via Telegram bot. Reads env vars lazily so importing
    this module never crashes when TELEGRAM_* are absent (e.g. local dev).
    Retries transient failures; on HTML parse errors (HTTP 400) falls back to
    plain text so an alert is never lost. Returns True if sent."""
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not bot_token or not chat_id:
        logger.warning('TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set, skipping Telegram send')
        return False

    message = _truncate_html(message)
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    use_html = True

    for delay in RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        payload = {'chat_id': chat_id, 'text': message}
        if use_html:
            payload['parse_mode'] = 'HTML'
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode(payload).encode())
            urllib.request.urlopen(req, timeout=10)
            logger.info('Telegram message sent')
            return True
        except urllib.error.HTTPError as e:
            if e.code == 400 and use_html:
                logger.warning('Telegram rejected HTML (400), retrying as plain text')
                use_html = False
                continue
            logger.error(f'Telegram send failed (HTTP {e.code}), retrying...')
        except Exception as e:
            logger.error(f'Telegram send failed ({e}), retrying...')

    logger.error('Telegram send failed after all retries')
    return False


def send_telegram_photo(photo_bytes: bytes, caption: str = '') -> bool:
    """Send a photo (e.g. a trend chart) via Telegram. Uses requests for the
    multipart upload. Caption is limited to 1024 chars by the Bot API. Returns
    True if sent; failures are logged, never raised."""
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not bot_token or not chat_id:
        logger.warning('TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set, skipping photo send')
        return False

    try:
        import requests
        url = f'https://api.telegram.org/bot{bot_token}/sendPhoto'
        files = {'photo': ('trend.png', photo_bytes, 'image/png')}
        data = {'chat_id': chat_id, 'caption': caption[:1024], 'parse_mode': 'HTML'}
        resp = requests.post(url, files=files, data=data, timeout=30)
        resp.raise_for_status()
        logger.info('Telegram photo sent')
        return True
    except Exception as e:
        logger.error(f'Telegram photo send failed: {e}')
        return False
