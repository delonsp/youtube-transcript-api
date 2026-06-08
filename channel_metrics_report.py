#!/usr/bin/env python3
"""
Daily channel metrics report.

Fetches channel metrics from the YouTube Analytics API (exact daily values,
~48-72h delay) plus public counters from the YouTube Data API, persists the
history in SQLite and sends a daily digest via Telegram.

Reference day is D-3 (data already consolidated). The last REWRITE_WINDOW_DAYS
days are re-upserted on every run because YouTube adjusts recent numbers
retroactively for up to ~72h.

Usage:
    python channel_metrics_report.py                  # full run (DB + Telegram)
    python channel_metrics_report.py --dry-run        # no Telegram, prints digest
    python channel_metrics_report.py --date 2026-06-04  # override reference day
    python channel_metrics_report.py --backfill 90    # force backfill window
"""

import argparse
import html
import logging
import os
import pickle
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from telegram_utils import send_telegram

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

SCOPES = [
    'https://www.googleapis.com/auth/yt-analytics.readonly',
    'https://www.googleapis.com/auth/youtube.readonly',
]
TOKEN_FILE = 'token_analytics.pickle'
DB_PATH = os.environ.get('METRICS_DB', 'metrics/metrics.db')

# Daily metrics queried from the Analytics API (dimensions=day).
# NOTE: thumbnail impressions/CTR do NOT exist in the Analytics API (targeted
# queries) — they require the Reporting API reach reports (v2 of this job).
DAILY_METRICS = (
    'views,engagedViews,estimatedMinutesWatched,averageViewDuration,'
    'subscribersGained,subscribersLost,likes,comments,shares'
)

REPORT_LAG_DAYS = 3        # digest reports D-3 (consolidated data)
REWRITE_WINDOW_DAYS = 7    # re-upsert this many trailing days each run
CONSOLIDATED_AFTER_DAYS = 3  # a day is final once it is >= 72h old
DEFAULT_BACKFILL_DAYS = 365  # history loaded on first run (empty DB)
MAX_WINDOW_DAYS = 366      # single-query ceiling (no startIndex pagination here)
TOP_VIDEOS = 10            # stored per day
TOP_VIDEOS_IN_DIGEST = 3
ANOMALY_Z_THRESHOLD = 2.5
ANOMALY_DROP_PCT = 0.40    # drop > 40% vs 7d average
BASELINE_DAYS = 28

WEEKDAYS_PT = ['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom']


# --------------------------------------------------------------------------
# Auth / API clients
# --------------------------------------------------------------------------

def authenticate():
    """Load token_analytics.pickle (refresh if needed) and build both clients.
    The interactive flow only runs locally; in Docker the token arrives ready
    via TOKEN_ANALYTICS_B64."""
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
    return analytics, youtube


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_snapshot (
            date TEXT PRIMARY KEY,
            subscriber_count INTEGER,
            total_views INTEGER,
            video_count INTEGER
        )
    """)
    conn.commit()
    return conn


def upsert_channel_daily(conn: sqlite3.Connection, rows: list, headers: list, today):
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
                 subs_gained, subs_lost, likes, comments, shares, consolidated, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                views=excluded.views,
                engaged_views=excluded.engaged_views,
                watch_minutes=excluded.watch_minutes,
                avg_view_duration_sec=excluded.avg_view_duration_sec,
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

def fetch_daily_metrics(analytics, start_date: str, end_date: str) -> dict:
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
        # Days present up to the API's truncation point (inclusive)
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


def fetch_top_videos(analytics, day: str, max_results: int = TOP_VIDEOS) -> list:
    """Top videos by views on a single day. Returns [(video_id, views, watch_minutes)]."""
    response = analytics.reports().query(
        ids='channel==MINE',
        startDate=day,
        endDate=day,
        metrics='views,estimatedMinutesWatched',
        dimensions='video',
        sort='-views',
        maxResults=max_results,
    ).execute()
    return [(r[0], r[1], r[2]) for r in (response.get('rows') or [])]


def fetch_video_titles(youtube, conn: sqlite3.Connection, video_ids: list) -> dict:
    """Resolve video titles, using video_daily as cache (1 quota unit per call)."""
    titles = {}
    missing = []
    for vid in video_ids:
        row = conn.execute(
            'SELECT title FROM video_daily WHERE video_id = ? AND title IS NOT NULL LIMIT 1',
            (vid,),
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
# Analysis
# --------------------------------------------------------------------------

def get_day(conn: sqlite3.Connection, day: str):
    return conn.execute(
        'SELECT views, engaged_views, watch_minutes, avg_view_duration_sec, '
        'subs_gained, subs_lost, likes, comments, shares '
        'FROM channel_daily WHERE date = ?',
        (day,),
    ).fetchone()


def get_baseline(conn: sqlite3.Connection, before_day: str, n_days: int, column: str) -> list:
    """Values of a column for the N consolidated days before before_day (oldest first)."""
    rows = conn.execute(
        f'SELECT {column} FROM channel_daily '
        'WHERE date < ? AND consolidated = 1 ORDER BY date DESC LIMIT ?',
        (before_day, n_days),
    ).fetchall()
    return [r[0] for r in reversed(rows)]


def detect_anomaly(value: float, baseline: list, label: str):
    """z-score vs baseline + percentage-drop fallback. Returns message or None."""
    if len(baseline) < BASELINE_DAYS:
        return None  # not enough consolidated history — stay quiet

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
# Digest
# --------------------------------------------------------------------------

def fmt_int(n) -> str:
    return f'{int(n):,}'.replace(',', '.')


def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f'{m}min{s:02d}s'


def fmt_pct_delta(value: float, reference: float) -> str:
    if not reference:
        return ''
    delta = (value / reference - 1) * 100
    return f' ({delta:+.0f}% vs média 7d)'


def day_label_pt(day: str) -> str:
    d = datetime.strptime(day, '%Y-%m-%d').date()
    return f'{WEEKDAYS_PT[d.weekday()]} {d.strftime("%d/%m")}'


def build_digest(conn, ref_day, top_videos, titles, snapshot, anomalies) -> str:
    row = get_day(conn, ref_day)
    (views, engaged, watch_min, avg_dur,
     gained, lost, likes, comments, shares) = row

    views_7d = get_baseline(conn, ref_day, 7, 'views')
    avg_views_7d = sum(views_7d) / len(views_7d) if views_7d else 0
    net_subs = gained - lost

    lines = [
        f'📊 <b>Métricas do canal — {day_label_pt(ref_day)}</b>',
        '',
        f'👁 Views: <b>{fmt_int(views)}</b>{fmt_pct_delta(views, avg_views_7d)}',
        f'⏱ Watch time: {fmt_int(watch_min // 60)}h | duração média {fmt_duration(avg_dur)}',
        f'👥 Inscritos: +{fmt_int(gained)} / -{fmt_int(lost)} (net {net_subs:+d})',
        f'❤️ {fmt_int(likes)} likes | 💬 {fmt_int(comments)} | 🔁 {fmt_int(shares)}',
    ]

    if top_videos:
        lines.append('')
        lines.append('🏆 <b>Top vídeos do dia:</b>')
        for i, (vid, v_views, _) in enumerate(top_videos[:TOP_VIDEOS_IN_DIGEST], 1):
            # Truncate BEFORE escaping so an HTML entity is never cut in half
            title = titles.get(vid, vid)
            if len(title) > 60:
                title = title[:57] + '...'
            lines.append(f'{i}. {html.escape(title)} ({fmt_int(v_views)})')

    for anomaly in anomalies:
        lines.append('')
        lines.append(anomaly)

    if snapshot:
        lines.append('')
        lines.append(
            f'📈 Total: ~{fmt_int(snapshot["subscriber_count"])} inscritos | '
            f'{fmt_int(snapshot["total_views"])} views'
        )

    return '\n'.join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run(args) -> int:
    today = datetime.now(timezone.utc).date()
    conn = init_db(args.db)

    analytics, youtube = authenticate()

    # Fetch window: backfill on first run (empty DB), trailing window otherwise
    has_history = conn.execute('SELECT COUNT(*) FROM channel_daily').fetchone()[0] > 0
    if args.backfill:
        window_days = args.backfill
    elif not has_history:
        window_days = DEFAULT_BACKFILL_DAYS
        logger.info(f'Empty DB — backfilling {window_days} days of history')
    else:
        window_days = REWRITE_WINDOW_DAYS
        # Self-heal gaps after downtime: re-fetch everything since the last
        # consolidated day, otherwise days that left the rewrite window while
        # the container was down would stay partial/missing forever
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
    # An explicit --date never falls back — silently reporting another day
    # would defeat the flag's debugging purpose.
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

    # Top videos of the reference day
    logger.info(f'Fetching top videos for {ref_day}')
    top_videos = fetch_top_videos(analytics, ref_day)
    titles = fetch_video_titles(youtube, conn, [v[0] for v in top_videos])
    for vid, v_views, v_watch in top_videos:
        conn.execute(
            'INSERT INTO video_daily (date, video_id, title, views, watch_minutes) '
            'VALUES (?, ?, ?, ?, ?) '
            'ON CONFLICT(date, video_id) DO UPDATE SET '
            'title=excluded.title, views=excluded.views, watch_minutes=excluded.watch_minutes',
            (ref_day, vid, titles.get(vid), v_views, v_watch),
        )
    conn.commit()

    # Public counters snapshot (subscriberCount is rounded — display as "~")
    snapshot = fetch_channel_snapshot(youtube)
    conn.execute(
        'INSERT OR REPLACE INTO channel_snapshot (date, subscriber_count, total_views, video_count) '
        'VALUES (?, ?, ?, ?)',
        (today.isoformat(), snapshot['subscriber_count'],
         snapshot['total_views'], snapshot['video_count']),
    )
    conn.commit()

    # Anomaly detection (consolidated days only)
    row = get_day(conn, ref_day)
    anomalies = []
    for value, column, label in (
        (row[0], 'views', 'views'),
        (row[4] - row[5], 'subs_gained - subs_lost', 'inscritos (net)'),
    ):
        baseline = get_baseline(conn, ref_day, BASELINE_DAYS, column)
        anomaly = detect_anomaly(value, baseline, label)
        if anomaly:
            anomalies.append(anomaly)

    digest = build_digest(conn, ref_day, top_videos, titles, snapshot, anomalies)

    if args.dry_run:
        logger.info('Dry run — digest below (not sent):')
        print('\n' + digest + '\n')
        sent = True
    else:
        sent = send_telegram(digest)
        if not sent:
            logger.error('Digest was computed and stored but could not be delivered')

    conn.close()
    return 0 if sent else 1


def iso_date(value: str) -> str:
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
