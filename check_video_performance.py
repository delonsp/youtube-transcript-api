#!/usr/bin/env python3
"""
One-shot performance check for the NÓDULOS DE TIREOIDE video (the dashboard's
AI suggestion #4), scheduled via crontab for 2026-06-19 14h UTC — by then the
Analytics API has consolidated the first ~3 days. Reports via Telegram whether
the video confirmed the prediction, then is harmless to leave in the crontab
(date guard makes it a no-op outside the check window; June 19 only recurs
yearly anyway).

Runs inside the cron container, reusing the existing OAuth token + Telegram env.
"""

import logging
import sqlite3
from datetime import date, timedelta

from channel_metrics_report import authenticate
from telegram_utils import send_telegram

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

VIDEO_ID = '1NmmSvszN_4'
VIDEO_TITLE = 'NÓDULOS DE TIREOIDE: protocolo dos primeiros 90 dias'
PUBLISHED = '2026-06-16'
DB_PATH = 'metrics/metrics.db'

# Channel baseline (top200/365d) and the dashboard/AI prediction for this theme.
BASE_RETENTION = 45.0    # %
BASE_CONVERSION = 0.54   # %
PRED_CTR = 9.7           # %
PRED_RETENTION = 50.0    # %
PRED_CONVERSION = 1.37   # %

# Only act in this window so a yearly cron re-fire stays a silent no-op.
CHECK_START = date(2026, 6, 19)
CHECK_END = date(2026, 6, 23)


def verdict(actual, predicted, baseline, higher_is_better=True):
    if actual is None:
        return 'sem dados'
    if actual >= predicted:
        return '✅ bateu/superou a previsão'
    if actual >= baseline:
        return '🟡 acima da média do canal, abaixo da previsão'
    return '🔴 abaixo da média do canal'


def main():
    today = date.today()
    if not (CHECK_START <= today <= CHECK_END):
        logger.info(f'Fora da janela de checagem ({today}); no-op.')
        return

    creds, analytics, youtube = authenticate()
    end = (today - timedelta(days=1)).isoformat()
    days_live = (date.fromisoformat(end) - date.fromisoformat(PUBLISHED)).days + 1

    r = analytics.reports().query(
        ids='channel==MINE', startDate=PUBLISHED, endDate=end,
        metrics='views,estimatedMinutesWatched,averageViewPercentage,'
                'averageViewDuration,subscribersGained,subscribersLost,'
                'likes,comments,shares',
        filters=f'video=={VIDEO_ID}',
    ).execute()
    rows = r.get('rows')

    if not rows:
        send_telegram(
            f'⏳ <b>Checagem do vídeo de NÓDULOS DE TIREOIDE</b>\n\n'
            f'Ainda sem dados consolidados na Analytics API (publicado {PUBLISHED}). '
            f'Tente de novo em 1-2 dias.'
        )
        return

    h = [c['name'] for c in r['columnHeaders']]
    d = dict(zip(h, rows[0]))
    views = d['views']
    net = d['subscribersGained'] - d['subscribersLost']
    conv = (net / views * 100) if views else 0.0
    ret = d['averageViewPercentage']
    watch_h = d['estimatedMinutesWatched'] / 60

    conn = sqlite3.connect(DB_PATH)
    rc = conn.execute(
        'SELECT SUM(thumbnail_impressions), SUM(thumbnail_impressions*thumbnail_ctr) '
        'FROM video_reach WHERE video_id = ?', (VIDEO_ID,)
    ).fetchone()
    ctr = (rc[1] / rc[0] * 100) if rc and rc[0] else None
    impr = int(rc[0]) if rc and rc[0] else 0

    msg = (
        f'<i>[vindo do módulo YT da VPS]</i>\n\n'
        f'🎯 <b>Checagem: {VIDEO_TITLE}</b>\n'
        f'(sugestão #4 da IA · publicado {PUBLISHED} · ~{days_live} dias de dados)\n\n'
        f'👁 Views: {views:,}\n'.replace(',', '.') +
        f'⏱ Watch time: {watch_h:.0f}h\n'
        f'📉 Retenção: <b>{ret:.0f}%</b> — {verdict(ret, PRED_RETENTION, BASE_RETENTION)}\n'
        f'   (previsão {PRED_RETENTION:.0f}% · média do canal {BASE_RETENTION:.0f}%)\n'
        f'👥 Conversão: <b>{conv:.2f}%</b> — {verdict(conv, PRED_CONVERSION, BASE_CONVERSION)}\n'
        f'   (previsão {PRED_CONVERSION:.2f}% · média {BASE_CONVERSION:.2f}%) · net {net:+d}\n'
    )
    if ctr is not None:
        msg += (f'🖼 CTR thumb: <b>{ctr:.1f}%</b> — '
                f'{verdict(ctr, PRED_CTR, 0)} (previsão {PRED_CTR:.1f}%) · '
                f'{impr:,} impr\n'.replace(',', '.'))
    else:
        msg += '🖼 CTR thumb: ainda sem dados de reach\n'

    hits = sum([
        ret is not None and ret >= PRED_RETENTION,
        conv >= PRED_CONVERSION,
        ctr is not None and ctr >= PRED_CTR,
    ])
    msg += (f'\n<b>Veredito</b>: {hits}/3 métricas-chave bateram a previsão do Fable. '
            + ('A aposta se confirmou. 🎯' if hits >= 2
               else 'Resultado misto — vale comparar com a próxima análise semanal.'))

    send_telegram(msg)
    logger.info(f'Checagem enviada: {hits}/3 métricas confirmaram a previsão')


if __name__ == '__main__':
    main()
