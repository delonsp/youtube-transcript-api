#!/usr/bin/env python3
"""
Mirror the local SQLite metrics into Supabase (Postgres) for dashboards and
ad-hoc querying. SQLite stays the primary store; Supabase is a durable mirror.

Writes go through PostgREST with the project's service_role key (server-side
secret — bypasses RLS). The tables (public.yt_metrics_*) have RLS enabled with
no policies, so only the service_role can touch them.

Fully fail-soft: if SUPABASE_URL / SUPABASE_SERVICE_KEY are unset or the request
fails, it logs and returns — the digest job never breaks because of the mirror.

Env:
    SUPABASE_URL          e.g. https://<ref>.supabase.co
    SUPABASE_SERVICE_KEY  service_role secret (Dashboard → Settings → API)
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

TABLE_PREFIX = 'yt_metrics_'


def _enabled():
    return bool(os.environ.get('SUPABASE_URL') and os.environ.get('SUPABASE_SERVICE_KEY'))


def _upsert(table, rows, on_conflict):
    """Batch upsert rows into a Supabase table via PostgREST."""
    if not rows:
        return
    import requests
    base = os.environ['SUPABASE_URL'].rstrip('/')
    key = os.environ['SUPABASE_SERVICE_KEY']
    url = f'{base}/rest/v1/{table}?on_conflict={on_conflict}'
    headers = {
        'apikey': key,
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
        'Prefer': 'resolution=merge-duplicates,return=minimal',
    }
    resp = requests.post(url, headers=headers, data=json.dumps(rows), timeout=30)
    resp.raise_for_status()


def _fetch(conn, query, cols, params=()):
    rows = conn.execute(query, params).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def sync_to_supabase(conn, ref_day):
    """Upsert the metrics history into Supabase. Idempotent and fail-soft.
    channel_daily / reach / snapshot are mirrored in full (small, self-healing);
    video_window only for the current period_end."""
    if not _enabled():
        logger.info('SUPABASE_URL/SUPABASE_SERVICE_KEY not set — skipping Supabase mirror')
        return

    try:
        daily_cols = ['date', 'views', 'engaged_views', 'watch_minutes',
                      'avg_view_duration_sec', 'avg_view_percentage', 'subs_gained',
                      'subs_lost', 'likes', 'comments', 'shares', 'consolidated',
                      'updated_at']
        daily = _fetch(conn, f'SELECT {",".join(daily_cols)} FROM channel_daily', daily_cols)
        for r in daily:                       # SQLite stores 0/1; pg wants bool
            r['consolidated'] = bool(r['consolidated']) if r['consolidated'] is not None else None
        _upsert(f'{TABLE_PREFIX}channel_daily', daily, 'date')

        reach_cols = ['date', 'thumbnail_impressions', 'thumbnail_ctr', 'updated_at']
        reach = _fetch(conn, f'SELECT {",".join(reach_cols)} FROM channel_reach', reach_cols)
        _upsert(f'{TABLE_PREFIX}channel_reach', reach, 'date')

        snap_cols = ['date', 'subscriber_count', 'total_views', 'video_count']
        snap = _fetch(conn, f'SELECT {",".join(snap_cols)} FROM channel_snapshot', snap_cols)
        _upsert(f'{TABLE_PREFIX}channel_snapshot', snap, 'date')

        win_cols = ['period_end', 'video_id', 'title', 'views', 'watch_minutes',
                    'subs_gained', 'subs_lost', 'avg_view_percentage']
        win = _fetch(conn, f'SELECT {",".join(win_cols)} FROM video_window WHERE period_end = ?',
                     win_cols, (ref_day,))
        _upsert(f'{TABLE_PREFIX}video_window', win, 'period_end,video_id')

        logger.info(
            f'Supabase mirror OK ({len(daily)} daily, {len(reach)} reach, '
            f'{len(snap)} snapshot, {len(win)} video_window rows)'
        )
    except Exception as e:
        logger.warning(f'Supabase mirror failed ({e}); SQLite remains the source of truth')
