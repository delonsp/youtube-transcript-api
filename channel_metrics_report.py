#!/usr/bin/env python3
"""
Daily channel metrics report (V2).

Fetches channel metrics from the YouTube Analytics API (exact daily values,
~48-72h delay) plus public counters from the YouTube Data API, persists the
history in SQLite and sends a daily digest via Telegram.

V2 adds, on top of the daily snapshot:
  - a 7-day rollup block with week-over-week comparison
  - per-video metrics over the week (views, watch hours, NET SUBSCRIBERS, and
    average-view-percentage retention), ranked
  - a retention-curve summary (hook / middle / end + relative performance) for
    the week's #1 video
  - thumbnail impressions & CTR from the Reporting API (fail-soft: omitted until
    that API is enabled and its first ~48h CSV is ready) — see youtube_reporting
  - a 30-day views trend chart sent as a photo

Reference day is D-3 (data already consolidated). The last REWRITE_WINDOW_DAYS
days are re-upserted on every run because YouTube adjusts recent numbers
retroactively for up to ~72h.

Usage:
    python channel_metrics_report.py                  # full run (DB + Telegram)
    python channel_metrics_report.py --dry-run        # no Telegram, prints digest
    python channel_metrics_report.py --date 2026-06-04  # override reference day
    python channel_metrics_report.py --backfill 90    # force backfill window
    python channel_metrics_report.py --no-chart       # skip the trend image
"""

import argparse
import html
import io
import logging
import os
import pickle
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import youtube_reporting
from telegram_utils import send_telegram, send_telegram_photo

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

SCOPES = [
    'https://www.googleapis.com/auth/yt-analytics.readonly',
    'https://www.googleapis.com/auth/youtube.readonly',
]
TOKEN_FILE = 'token_analytics.pickle'
DB_PATH = os.environ.get('METRICS_DB', 'metrics/metrics.db')

# Daily channel metrics (dimensions=day). averageViewPercentage = retention.
# NOTE: thumbnail impressions/CTR do NOT exist here — they come from the
# Reporting API (youtube_reporting.py).
DAILY_METRICS = (
    'views,engagedViews,estimatedMinutesWatched,averageViewDuration,'
    'averageViewPercentage,subscribersGained,subscribersLost,likes,comments,shares'
)
# Per-video metrics over a window (dimensions=video, sort required, <=200 rows).
VIDEO_METRICS = (
    'views,estimatedMinutesWatched,subscribersGained,subscribersLost,'
    'averageViewPercentage'
)

REPORT_LAG_DAYS = 3        # digest reports D-3 (consolidated data)
REWRITE_WINDOW_DAYS = 7    # re-upsert this many trailing days each run
CONSOLIDATED_AFTER_DAYS = 3  # a day is final once it is >= 72h old
DEFAULT_BACKFILL_DAYS = 365  # history loaded on first run (empty DB)
MAX_WINDOW_DAYS = 366      # single-query ceiling (no startIndex pagination here)
WEEK_DAYS = 7              # rollup / per-video window length
TOP_VIDEOS_STORED = 15     # stored per window
TOP_VIDEOS_IN_DIGEST = 5
RETENTION_CURVE_WINDOW = 30  # days of data for the curve of the #1 video
TREND_CHART_DAYS = 30
ANOMALY_Z_THRESHOLD = 2.5
ANOMALY_DROP_PCT = 0.40    # drop > 40% vs 7d average
BASELINE_DAYS = 28

WEEKDAYS_PT = ['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom']


# --------------------------------------------------------------------------
# Auth / API clients
# --------------------------------------------------------------------------

def authenticate():
    """Load token_analytics.pickle (refresh if needed) and build clients.
    Returns (creds, analytics, youtube). The interactive flow only runs locally;
    in Docker the token arrives ready via TOKEN_ANALYTICS_B64."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'rb') as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, 'wb') as f:
            pickle.dump(creds, f)

    analytics = build('youtubeAnalytics', 'v2', credentials=creds)
    youtube = build('youtube', 'v3', credentials=creds)
    return creds, analytics, youtube


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _add_column_if_missing(conn, table, column, decl):
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    if column not in cols:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')


def init_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_daily (
            date TEXT PRIMARY KEY,
            views INTEGER,
            engaged_views INTEGER,
            watch_minutes INTEGER,
            avg_view_duration_sec REAL,
            subs_gained INTEGER,
            subs_lost INTEGER,
            likes INTEGER,
            comments INTEGER,
            shares INTEGER,
            consolidated INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_daily (
            date TEXT,
            video_id TEXT,
            title TEXT,
            views INTEGER,
            watch_minutes INTEGER,
            PRIMARY KEY (date, video_id)
        )
    """)
    # Per-video metrics aggregated over a window (period_end = ref_day).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_window (
            period_end TEXT,
            video_id TEXT,
            title TEXT,
            views INTEGER,
            watch_minutes INTEGER,
            subs_gained INTEGER,
            subs_lost INTEGER,
            avg_view_percentage REAL,
            PRIMARY KEY (period_end, video_id)
        )
    """)
    # Thumbnail impressions / CTR per day (Reporting API).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_reach (
            date TEXT PRIMARY KEY,
            thumbnail_impressions INTEGER,
            thumbnail_ctr REAL,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reporting_jobs (
            report_type TEXT PRIMARY KEY,
            job_id TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_snapshot (
            date TEXT PRIMARY KEY,
            subscriber_count INTEGER,
            total_views INTEGER,
            video_count INTEGER
        )
    """)
    # Migration: V1 channel_daily had no retention column.
    _add_column_if_missing(conn, 'channel_daily', 'avg_view_percentage', 'REAL')
    conn.commit()
    return conn


def upsert_channel_daily(conn, rows, headers, today):
    """UPSERT daily rows from the Analytics API response."""
    idx = {name: i for i, name in enumerate(headers)}
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    for row in rows:
        day = row[idx['day']]
        day_age = (today - datetime.strptime(day, '%Y-%m-%d').date()).days
        consolidated = 1 if day_age >= CONSOLIDATED_AFTER_DAYS else 0
        conn.execute(
            """
            INSERT INTO channel_daily
                (date, views, engaged_views, watch_minutes, avg_view_duration_sec,
                 avg_view_percentage, subs_gained, subs_lost, likes, comments,
                 shares, consolidated, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                views=excluded.views,
                engaged_views=excluded.engaged_views,
                watch_minutes=excluded.watch_minutes,
                avg_view_duration_sec=excluded.avg_view_duration_sec,
                avg_view_percentage=excluded.avg_view_percentage,
                subs_gained=excluded.subs_gained,
                subs_lost=excluded.subs_lost,
                likes=excluded.likes,
                comments=excluded.comments,
                shares=excluded.shares,
                consolidated=excluded.consolidated,
                updated_at=excluded.updated_at
            """,
            (
                day,
                row[idx['views']],
                row[idx['engagedViews']],
                row[idx['estimatedMinutesWatched']],
                row[idx['averageViewDuration']],
                row[idx['averageViewPercentage']],
                row[idx['subscribersGained']],
                row[idx['subscribersLost']],
                row[idx['likes']],
                row[idx['comments']],
                row[idx['shares']],
                consolidated,
                now,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Data fetching
# --------------------------------------------------------------------------

def fetch_daily_metrics(analytics, start_date, end_date) -> dict:
    """Query channel daily metrics. The API silently truncates endDate to the
    last day for which ALL requested metrics are available."""
    requested_days = (
        datetime.strptime(end_date, '%Y-%m-%d').date()
        - datetime.strptime(start_date, '%Y-%m-%d').date()
    ).days + 1
    response = analytics.reports().query(
        ids='channel==MINE',
        startDate=start_date,
        endDate=end_date,
        metrics=DAILY_METRICS,
        dimensions='day',
        sort='day',
        maxResults=requested_days,
    ).execute()
    headers = [c['name'] for c in response.get('columnHeaders', [])]
    rows = response.get('rows') or []  # 'rows' is absent when there is no data

    if rows:
        day_idx = headers.index('day')
        last_day = rows[-1][day_idx]
        expected_until_last = (
            datetime.strptime(last_day, '%Y-%m-%d').date()
            - datetime.strptime(start_date, '%Y-%m-%d').date()
        ).days + 1
        if len(rows) < expected_until_last:
            logger.error(
                f'Analytics API returned {len(rows)} days but {expected_until_last} were '
                f'expected through {last_day} — gaps INSIDE the window, investigate'
            )
        elif last_day < end_date:
            logger.info(
                f'Data available through {last_day} (requested through {end_date} — '
                f'recent days not consolidated yet, expected behavior)'
            )
    elif requested_days > 0:
        logger.warning(f'Analytics API returned no rows for {start_date} → {end_date}')

    return {'headers': headers, 'rows': rows}


def fetch_top_videos_window(analytics, start_date, end_date, max_results=TOP_VIDEOS_STORED):
    """Top videos over [start_date, end_date] with per-video net subscribers and
    retention. NOTE: per-video subscribersGained/Lost count only (un)subscribes
    originating on that video's watch page, so they do NOT sum to the channel net
    — do not "reconcile" the discrepancy."""
    response = analytics.reports().query(
        ids='channel==MINE',
        startDate=start_date,
        endDate=end_date,
        metrics=VIDEO_METRICS,
        dimensions='video',
        sort='-views',          # required for this report
        maxResults=max_results,  # <= 200
    ).execute()
    headers = [c['name'] for c in response.get('columnHeaders', [])]
    idx = {h: i for i, h in enumerate(headers)}
    out = []
    for r in (response.get('rows') or []):
        out.append({
            'video_id': r[idx['video']],
            'views': r[idx['views']],
            'watch_minutes': r[idx['estimatedMinutesWatched']],
            'subs_gained': r[idx['subscribersGained']],
            'subs_lost': r[idx['subscribersLost']],
            'net_subs': r[idx['subscribersGained']] - r[idx['subscribersLost']],
            'avg_view_pct': r[idx['averageViewPercentage']],
        })
    return out


def fetch_retention_curve(analytics, video_id, start_date, end_date):
    """Audience-retention curve for a single video. Returns a list of
    (elapsed_ratio, audience_watch_ratio, relative_performance) or [] on any
    failure (members-only / low-view videos can return no rows)."""
    try:
        response = analytics.reports().query(
            ids='channel==MINE',
            startDate=start_date,
            endDate=end_date,
            metrics='audienceWatchRatio,relativeRetentionPerformance',
            dimensions='elapsedVideoTimeRatio',
            filters=f'video=={video_id}',
            sort='elapsedVideoTimeRatio',
        ).execute()
    except Exception as e:
        logger.info(f'Retention curve unavailable for {video_id} ({e})')
        return []
    return [(r[0], r[1], r[2]) for r in (response.get('rows') or [])]


def fetch_video_titles(youtube, conn, video_ids) -> dict:
    """Resolve video titles, using stored titles as cache (1 quota unit/call)."""
    titles = {}
    missing = []
    for vid in video_ids:
        row = conn.execute(
            'SELECT title FROM video_window WHERE video_id = ? AND title IS NOT NULL '
            'UNION SELECT title FROM video_daily WHERE video_id = ? AND title IS NOT NULL '
            'LIMIT 1',
            (vid, vid),
        ).fetchone()
        if row:
            titles[vid] = row[0]
        else:
            missing.append(vid)

    for i in range(0, len(missing), 50):
        batch = missing[i:i + 50]
        response = youtube.videos().list(part='snippet', id=','.join(batch)).execute()
        for item in response.get('items', []):
            titles[item['id']] = item['snippet']['title']

    return titles


def fetch_channel_snapshot(youtube) -> dict:
    """Public counters snapshot (subscriberCount is rounded DOWN to 3
    significant figures even for the authenticated owner)."""
    response = youtube.channels().list(part='statistics', mine=True).execute()
    stats = response['items'][0]['statistics']
    return {
        'subscriber_count': int(stats['subscriberCount']),
        'total_views': int(stats['viewCount']),
        'video_count': int(stats['videoCount']),
    }


# --------------------------------------------------------------------------
# Reporting API (thumbnail impressions / CTR) — fail-soft
# --------------------------------------------------------------------------

def collect_reach(creds, conn, ref_day):
    """Ensure the reach job exists, fetch recent reports and upsert per-day
    impressions/CTR. Never raises — impressions are optional."""
    try:
        service = youtube_reporting.build_reporting(creds)
        job_id = youtube_reporting.ensure_reach_job(service, conn)
        if not job_id:
            return
        # List reports from ~10 days before ref_day to cover the weekly window.
        created_after = (
            datetime.strptime(ref_day, '%Y-%m-%d')
            .replace(tzinfo=timezone.utc) - timedelta(days=10)
        ).isoformat()
        by_day = youtube_reporting.fetch_reach_by_day(service, creds, job_id, created_after)
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        for date, vals in by_day.items():
            conn.execute(
                'INSERT INTO channel_reach (date, thumbnail_impressions, thumbnail_ctr, updated_at) '
                'VALUES (?, ?, ?, ?) '
                'ON CONFLICT(date) DO UPDATE SET '
                'thumbnail_impressions=excluded.thumbnail_impressions, '
                'thumbnail_ctr=excluded.thumbnail_ctr, updated_at=excluded.updated_at',
                (date, vals['impressions'], vals['ctr'], now),
            )
        conn.commit()
        if by_day:
            logger.info(f'Stored reach data for {len(by_day)} day(s)')
    except Exception as e:
        logger.warning(f'Reach collection failed ({e}); impressions omitted from digest')


def get_reach_window(conn, start, end):
    """Impressions-weighted CTR + total impressions over [start, end].
    Returns dict or None."""
    rows = conn.execute(
        'SELECT thumbnail_impressions, thumbnail_ctr FROM channel_reach '
        'WHERE date BETWEEN ? AND ? AND thumbnail_impressions IS NOT NULL',
        (start, end),
    ).fetchall()
    if not rows:
        return None
    total_impr = sum(r[0] for r in rows)
    if not total_impr:
        return None
    total_clicks = sum(r[0] * (r[1] or 0) for r in rows)
    return {'impressions': total_impr, 'ctr': total_clicks / total_impr}


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def get_day(conn, day):
    return conn.execute(
        'SELECT views, engaged_views, watch_minutes, avg_view_duration_sec, '
        'avg_view_percentage, subs_gained, subs_lost, likes, comments, shares '
        'FROM channel_daily WHERE date = ?',
        (day,),
    ).fetchone()


def get_window_sum(conn, start, end):
    """Sum channel_daily over [start, end]. Returns (sums dict, fully_consolidated)."""
    rows = conn.execute(
        'SELECT views, watch_minutes, subs_gained, subs_lost, consolidated '
        'FROM channel_daily WHERE date BETWEEN ? AND ?',
        (start, end),
    ).fetchall()
    expected = (datetime.strptime(end, '%Y-%m-%d').date()
                - datetime.strptime(start, '%Y-%m-%d').date()).days + 1
    sums = {
        'views': sum(r[0] or 0 for r in rows),
        'watch_minutes': sum(r[1] or 0 for r in rows),
        'subs_gained': sum(r[2] or 0 for r in rows),
        'subs_lost': sum(r[3] or 0 for r in rows),
    }
    fully = len(rows) == expected and all(r[4] == 1 for r in rows)
    return sums, fully


def get_baseline(conn, before_day, n_days, column):
    """Values of a column for the N consolidated days before before_day (oldest first)."""
    rows = conn.execute(
        f'SELECT {column} FROM channel_daily '
        'WHERE date < ? AND consolidated = 1 ORDER BY date DESC LIMIT ?',
        (before_day, n_days),
    ).fetchall()
    return [r[0] for r in reversed(rows)]


def detect_anomaly(value, baseline, label):
    """z-score vs baseline + percentage-drop fallback. Returns message or None."""
    if len(baseline) < BASELINE_DAYS:
        return None

    mean = sum(baseline) / len(baseline)
    variance = sum((x - mean) ** 2 for x in baseline) / len(baseline)
    std = variance ** 0.5

    if std > 0:
        z = (value - mean) / std
        if abs(z) >= ANOMALY_Z_THRESHOLD:
            direction = '📈 acima' if z > 0 else '📉 abaixo'
            return f'⚠️ <b>Anomalia em {label}</b>: {direction} do padrão (z={z:+.1f} vs 28d)'

    last7 = baseline[-7:]
    avg7 = sum(last7) / len(last7) if last7 else 0
    if avg7 > 0 and value < avg7 * (1 - ANOMALY_DROP_PCT):
        pct = (1 - value / avg7) * 100
        return f'⚠️ <b>Anomalia em {label}</b>: queda de {pct:.0f}% vs média 7d'

    return None


# --------------------------------------------------------------------------
# Trend chart
# --------------------------------------------------------------------------

def generate_trend_chart(conn, ref_day, days=TREND_CHART_DAYS):
    """30-day views line chart ending at ref_day. Returns PNG bytes or None."""
    start = (datetime.strptime(ref_day, '%Y-%m-%d').date() - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        'SELECT date, views FROM channel_daily WHERE date BETWEEN ? AND ? ORDER BY date',
        (start, ref_day),
    ).fetchall()
    if len(rows) < 7:
        return None
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        dates = [datetime.strptime(r[0], '%Y-%m-%d') for r in rows]
        views = [r[1] or 0 for r in rows]

        fig, ax = plt.subplots(figsize=(8, 3.2), dpi=120)
        ax.plot(dates, views, color='#cc0000', linewidth=2)
        ax.fill_between(dates, views, color='#cc0000', alpha=0.12)
        ax.set_title(f'Views diárias — últimos {len(rows)} dias', fontsize=11)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
        ax.grid(True, alpha=0.25)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        fig.autofmt_xdate(rotation=45)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format='png')
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f'Trend chart generation failed ({e}); skipping image')
        return None


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------

def fmt_int(n) -> str:
    return f'{int(n):,}'.replace(',', '.')


def fmt_signed(n) -> str:
    """Signed integer with pt-BR thousands separator, e.g. +2.266 / -283."""
    return ('+' if n >= 0 else '-') + fmt_int(abs(n))


def fmt_pct_delta(value, reference, suffix) -> str:
    if not reference:
        return ''
    delta = (value / reference - 1) * 100
    return f' ({delta:+.0f}% {suffix})'


def day_label_pt(day) -> str:
    d = datetime.strptime(day, '%Y-%m-%d').date()
    return f'{WEEKDAYS_PT[d.weekday()]} {d.strftime("%d/%m")}'


def short_date(day) -> str:
    return datetime.strptime(day, '%Y-%m-%d').strftime('%d/%m')


def retention_emoji(pct) -> str:
    if pct is None:
        return ''
    if pct >= 50:
        return ' 🔥'
    if pct < 25:
        return ' ⚠️'
    return ''


def short_label(title, limit=12) -> str:
    """Short keyword label for the table's Vídeo column: whole words up to
    `limit` chars (e.g. 'PÂNCREAS pedindo...' -> 'PÂNCREAS')."""
    title = (title or '').strip()
    out = ''
    for word in title.split():
        cand = (out + ' ' + word).strip()
        if len(cand) > limit:
            if not out:           # first word already too long
                return cand[:limit]
            break
        out = cand
    out = out.rstrip(' -–—:·.')
    return out or title[:limit]


def build_video_table(top_videos, titles) -> list:
    """Top videos as a monospace <pre> table: Vídeo | Views | Inscr | Conv | Ret.
    Conv = net subscribers / views (subscriber conversion)."""
    rows = []
    for v in top_videos[:TOP_VIDEOS_IN_DIGEST]:
        views = v['views'] or 0
        conv = (v['net_subs'] / views * 100) if views else 0
        pct = v['avg_view_pct'] or 0
        rows.append({
            'video': short_label(titles.get(v['video_id'], v['video_id'])),
            'views': fmt_int(views),
            'inscr': fmt_signed(v['net_subs']),
            'conv': f'~{conv:.1f}%'.replace('.', ','),
            'ret': f'{pct:.0f}%{retention_emoji(pct)}',
        })

    headers = {'video': 'Vídeo', 'views': 'Views', 'inscr': 'Inscr',
               'conv': 'Conv', 'ret': 'Ret'}
    # Column widths from data; 'ret' is last (trailing emoji) so it isn't padded.
    w = {k: max(len(headers[k]), *(len(r[k]) for r in rows))
         for k in ('video', 'views', 'inscr', 'conv')}

    def fmt_row(get):
        return (f"{get('video'):<{w['video']}}  {get('views'):>{w['views']}}  "
                f"{get('inscr'):>{w['inscr']}}  {get('conv'):>{w['conv']}}  {get('ret')}")

    table = [fmt_row(headers.__getitem__)]
    table += [fmt_row(r.__getitem__) for r in rows]
    return ['🏆 <b>Top vídeos (7d)</b>', f'<pre>{html.escape(chr(10).join(table))}</pre>']


def build_digest(conn, ref_day, week, top_videos, titles, retention,
                 reach, snapshot, anomalies) -> str:
    # Origin tag so this is never confused with other bots on the same chat
    lines = ['<i>[vindo do módulo YT da VPS]</i>', '']

    # Anomalies first (visible in the push notification)
    for anomaly in anomalies:
        lines.append(anomaly)
    if anomalies:
        lines.append('')

    lines.append(f'📊 <b>Canal Dr. Alain — {day_label_pt(ref_day)}</b>')

    # 7-day rollup (the actionable signal for a large channel)
    wk_start = (datetime.strptime(ref_day, '%Y-%m-%d').date()
                - timedelta(days=WEEK_DAYS - 1)).isoformat()
    sums, wow = week['sums'], week['wow']
    net_wk = sums['subs_gained'] - sums['subs_lost']
    lines.append('')
    lines.append(f'🗓 <b>Últimos 7 dias</b> ({short_date(wk_start)}–{short_date(ref_day)})')
    lines.append(
        f'👁 {fmt_int(sums["views"])} views'
        + fmt_pct_delta(sums['views'], wow.get('views'), 'vs semana ant.')
    )
    lines.append(
        f'⏱ {fmt_int(sums["watch_minutes"] // 60)}h assistidas'
        + fmt_pct_delta(sums['watch_minutes'], wow.get('watch_minutes'), 'vs sem. ant.')
    )
    lines.append(
        f'👥 Inscritos: net {fmt_signed(net_wk)} (+{fmt_int(sums["subs_gained"])}'
        f'/−{fmt_int(sums["subs_lost"])})'
    )
    if reach:
        lines.append(
            f'🖼 {fmt_int(reach["impressions"])} impressões · CTR {reach["ctr"] * 100:.1f}%'
        )

    # Daily snapshot (D-3)
    (views, _eng, watch_min, _dur, avg_pct,
     gained, lost, likes, comments, shares) = get_day(conn, ref_day)
    views_7d = get_baseline(conn, ref_day, 7, 'views')
    avg_views_7d = sum(views_7d) / len(views_7d) if views_7d else 0
    ret_d = f'{avg_pct:.0f}%' if avg_pct is not None else 'n/d'
    lines.append('')
    lines.append(f'📅 <b>Dia {day_label_pt(ref_day)}</b> (D-3)')
    lines.append(f'👁 {fmt_int(views)}' + fmt_pct_delta(views, avg_views_7d, 'vs média 7d'))
    lines.append(f'⏱ {fmt_int(watch_min // 60)}h · retenção média {ret_d}')
    lines.append(
        f'👥 net {fmt_signed(gained - lost)} (+{fmt_int(gained)}/−{fmt_int(lost)}) · '
        f'❤️ {fmt_int(likes)} 💬 {fmt_int(comments)} 🔁 {fmt_int(shares)}'
    )

    # Top videos of the week (monospace table)
    if top_videos:
        lines.append('')
        lines.extend(build_video_table(top_videos, titles))

    # Retention-curve summary for the #1 video
    if retention:
        lines.append('')
        lines.append(
            f'🎬 <b>Retenção #1</b>: início {retention["hook"]:.0f}% · '
            f'meio {retention["mid"]:.0f}% · fim {retention["end"]:.0f}% · '
            f'{retention["vs_similar"]}'
        )

    # Totals
    if snapshot:
        lines.append('')
        lines.append(
            f'📈 ~{fmt_int(snapshot["subscriber_count"])} inscritos · '
            f'{fmt_int(snapshot["total_views"])} views totais'
        )

    return '\n'.join(lines)


def summarize_retention(curve):
    """Reduce a retention curve to hook/mid/end percentages + a similar-videos
    verdict. Returns dict or None."""
    if not curve:
        return None

    def at(ratio):
        i = min(range(len(curve)), key=lambda k: abs(curve[k][0] - ratio))
        return curve[i][1] * 100

    rel_values = [c[2] for c in curve if c[2] is not None]
    rel = sum(rel_values) / len(rel_values) if rel_values else None
    if rel is None:
        verdict = 'sem comparativo'
    elif rel >= 0.55:
        verdict = 'retém melhor que similares'
    elif rel <= 0.45:
        verdict = 'retém pior que similares'
    else:
        verdict = 'na média de similares'

    return {'hook': at(0.0), 'mid': at(0.5), 'end': at(0.99), 'vs_similar': verdict}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run(args) -> int:
    today = datetime.now(timezone.utc).date()
    conn = init_db(args.db)

    creds, analytics, youtube = authenticate()

    # Fetch window: backfill on first run (empty DB), trailing window otherwise
    has_history = conn.execute('SELECT COUNT(*) FROM channel_daily').fetchone()[0] > 0
    if args.backfill:
        window_days = args.backfill
    elif not has_history:
        window_days = DEFAULT_BACKFILL_DAYS
        logger.info(f'Empty DB — backfilling {window_days} days of history')
    else:
        window_days = REWRITE_WINDOW_DAYS
        # Self-heal gaps after downtime: re-fetch since the last consolidated day
        last_final = conn.execute(
            'SELECT MAX(date) FROM channel_daily WHERE consolidated = 1'
        ).fetchone()[0]
        if last_final:
            gap_days = (today - datetime.strptime(last_final, '%Y-%m-%d').date()).days
            if gap_days > REWRITE_WINDOW_DAYS:
                logger.warning(
                    f'Gap since last consolidated day {last_final} — '
                    f'expanding fetch window to {gap_days} days'
                )
                window_days = gap_days

    if window_days > MAX_WINDOW_DAYS:
        logger.warning(
            f'Fetch window capped at {MAX_WINDOW_DAYS} days (single-query limit); '
            f'run again with --backfill {MAX_WINDOW_DAYS} for older chunks if needed'
        )
        window_days = MAX_WINDOW_DAYS

    start_date = (today - timedelta(days=window_days)).isoformat()
    end_date = (today - timedelta(days=1)).isoformat()

    logger.info(f'Fetching daily metrics {start_date} → {end_date}')
    daily = fetch_daily_metrics(analytics, start_date, end_date)
    if daily['rows']:
        upsert_channel_daily(conn, daily['rows'], daily['headers'], today)
        logger.info(f'Upserted {len(daily["rows"])} days')

    # Reference day: D-3, falling back to the most recent day available.
    # An explicit --date never falls back.
    if args.date:
        ref_day = args.date
        if not get_day(conn, ref_day):
            raise RuntimeError(f'No data in DB for requested --date {ref_day}')
    else:
        ref_day = (today - timedelta(days=REPORT_LAG_DAYS)).isoformat()
        if not get_day(conn, ref_day):
            latest = conn.execute('SELECT MAX(date) FROM channel_daily').fetchone()[0]
            if not latest:
                raise RuntimeError('No data returned by the Analytics API')
            logger.warning(f'No data for {ref_day}, falling back to {latest}')
            ref_day = latest

    ref_date = datetime.strptime(ref_day, '%Y-%m-%d').date()
    wk_start = (ref_date - timedelta(days=WEEK_DAYS - 1)).isoformat()
    prev_start = (ref_date - timedelta(days=2 * WEEK_DAYS - 1)).isoformat()
    prev_end = (ref_date - timedelta(days=WEEK_DAYS)).isoformat()

    # Weekly rollup + week-over-week (WoW only if previous week fully consolidated)
    sums, _ = get_window_sum(conn, wk_start, ref_day)
    prev_sums, prev_full = get_window_sum(conn, prev_start, prev_end)
    wow = prev_sums if prev_full else {}
    week = {'sums': sums, 'wow': wow}

    # Top videos of the week (per-video net subs + retention)
    logger.info(f'Fetching weekly top videos {wk_start} → {ref_day}')
    top_videos = fetch_top_videos_window(analytics, wk_start, ref_day)
    titles = fetch_video_titles(youtube, conn, [v['video_id'] for v in top_videos])
    for v in top_videos:
        conn.execute(
            'INSERT INTO video_window '
            '(period_end, video_id, title, views, watch_minutes, subs_gained, '
            ' subs_lost, avg_view_percentage) VALUES (?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(period_end, video_id) DO UPDATE SET '
            'title=excluded.title, views=excluded.views, watch_minutes=excluded.watch_minutes, '
            'subs_gained=excluded.subs_gained, subs_lost=excluded.subs_lost, '
            'avg_view_percentage=excluded.avg_view_percentage',
            (ref_day, v['video_id'], titles.get(v['video_id']), v['views'],
             v['watch_minutes'], v['subs_gained'], v['subs_lost'], v['avg_view_pct']),
        )
    conn.commit()

    # Retention curve for the week's #1 video (fail-soft)
    retention = None
    if top_videos:
        curve_start = (ref_date - timedelta(days=RETENTION_CURVE_WINDOW - 1)).isoformat()
        curve = fetch_retention_curve(analytics, top_videos[0]['video_id'], curve_start, ref_day)
        retention = summarize_retention(curve)

    # Thumbnail impressions / CTR (fail-soft; omitted until Reporting API ready)
    collect_reach(creds, conn, ref_day)
    reach = get_reach_window(conn, wk_start, ref_day)

    # Public counters snapshot
    snapshot = fetch_channel_snapshot(youtube)
    conn.execute(
        'INSERT OR REPLACE INTO channel_snapshot (date, subscriber_count, total_views, video_count) '
        'VALUES (?, ?, ?, ?)',
        (today.isoformat(), snapshot['subscriber_count'],
         snapshot['total_views'], snapshot['video_count']),
    )
    conn.commit()

    # Anomaly detection (consolidated days only)
    drow = get_day(conn, ref_day)
    anomalies = []
    for value, column, label in (
        (drow[0], 'views', 'views'),
        (drow[5] - drow[6], 'subs_gained - subs_lost', 'inscritos (net)'),
    ):
        baseline = get_baseline(conn, ref_day, BASELINE_DAYS, column)
        anomaly = detect_anomaly(value, baseline, label)
        if anomaly:
            anomalies.append(anomaly)

    digest = build_digest(conn, ref_day, week, top_videos, titles,
                          retention, reach, snapshot, anomalies)

    chart = None if args.no_chart else generate_trend_chart(conn, ref_day)

    if args.dry_run:
        logger.info('Dry run — digest below (not sent):')
        print('\n' + digest + '\n')
        if chart:
            logger.info(f'Trend chart generated ({len(chart)} bytes, not sent)')
        sent = True
    else:
        sent = send_telegram(digest)
        if not sent:
            logger.error('Digest was computed and stored but could not be delivered')
        if chart:
            send_telegram_photo(chart, caption='📈 Tendência de views (30 dias)')

    conn.close()
    return 0 if sent else 1


def iso_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f'invalid date {value!r} (expected YYYY-MM-DD)')


def main():
    parser = argparse.ArgumentParser(description='Daily YouTube channel metrics report')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print digest instead of sending to Telegram')
    parser.add_argument('--date', type=iso_date, help='Override reference day (YYYY-MM-DD)')
    parser.add_argument('--backfill', type=int, metavar='N',
                        help=f'Force fetching the last N days (capped at {MAX_WINDOW_DAYS})')
    parser.add_argument('--no-chart', action='store_true', help='Skip the trend chart image')
    parser.add_argument('--db', default=DB_PATH, help='SQLite path')
    args = parser.parse_args()

    try:
        sys.exit(run(args))
    except Exception as e:
        logger.exception('Metrics job failed')
        if not args.dry_run:
            send_telegram(
                '🚨 <b>youtube-adm: job de métricas FALHOU</b>\n\n'
                f'<code>{html.escape(str(e)[:500])}</code>\n\n'
                'Verificar log do container cron (job das 11h UTC).\n'
                'Se for token: rode <code>python channel_metrics_report.py --dry-run</code> '
                'no Mac e atualize <code>TOKEN_ANALYTICS_B64</code> no Dokploy.'
            )
        sys.exit(1)


if __name__ == '__main__':
    main()
