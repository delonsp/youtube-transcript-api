#!/usr/bin/env python3
"""
YouTube Reporting API component — thumbnail impressions & CTR.

These metrics do NOT exist in the Analytics API (targeted queries); they live
only in the Reporting API's bulk "reach" reports (channel_reach_basic_a1),
delivered as daily CSVs. The flow is:

  1. ensure_reach_job(): create (once) a reporting job and persist its job_id.
     The first CSV is only ready ~48h after the job is created.
  2. fetch_reach_by_day(): list the job's reports, download the CSVs and
     aggregate thumbnail impressions + a views-weighted CTR per day.

EVERYTHING here is fail-soft: any error (API disabled, no reports yet, schema
mismatch) logs a warning and returns an empty result so the digest simply omits
the impressions line — the main job never crashes because of this component.

NOTE: this module could not be validated end-to-end before deploy because the
YouTube Reporting API was disabled in the GCP project and the first report has a
~48h cold start. Validate the CSV column parsing against a real report once data
is available. Column lookup is intentionally name-based and tolerant.
"""

import csv
import io
import logging
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

REACH_REPORT_TYPE = 'channel_reach_basic_a1'
JOB_NAME = 'channel-metrics-reach-basic'

# Column name candidates in the reach CSV (lower-cased, exact then substring)
_DATE_COLS = ('date',)
_IMPRESSION_COLS = ('video_thumbnail_impressions',)
_CTR_COLS = ('video_thumbnail_impressions_ctr',)


def build_reporting(creds):
    return build('youtubereporting', 'v1', credentials=creds)


def ensure_reach_job(service, conn):
    """Return the job_id for the reach report, creating the job once if needed.
    Returns None (and logs) if the API is disabled or the job can't be created."""
    row = conn.execute(
        'SELECT job_id FROM reporting_jobs WHERE report_type = ?', (REACH_REPORT_TYPE,)
    ).fetchone()
    if row:
        return row[0]

    try:
        jobs = service.jobs().list().execute().get('jobs', [])
    except HttpError as e:
        status = getattr(e.resp, 'status', '?')
        logger.warning(
            f'Reporting API unavailable (HTTP {status}) — impressions disabled. '
            f'Enable "YouTube Reporting API" in the GCP project if you want them.'
        )
        return None
    except Exception as e:
        logger.warning(f'Reporting API jobs.list failed ({e}) — impressions disabled')
        return None

    def _find_existing():
        for j in service.jobs().list().execute().get('jobs', []):
            if j.get('reportTypeId') == REACH_REPORT_TYPE:
                return j['id']
        return None

    job_id = _find_existing()
    if job_id:
        logger.info(f'Reusing existing reach reporting job {job_id}')
    else:
        try:
            created = service.jobs().create(
                body={'reportTypeId': REACH_REPORT_TYPE, 'name': JOB_NAME}
            ).execute()
            job_id = created['id']
            logger.info(
                f'Created reach reporting job {job_id} '
                f'(first CSV ready in ~48h, with 30d backfill)'
            )
        except HttpError as e:
            # 409: a job for this report type already exists — adopt it instead
            if getattr(e.resp, 'status', None) == 409:
                job_id = _find_existing()
                if job_id:
                    logger.info(f'Adopted pre-existing reach job {job_id} (409 on create)')
                else:
                    logger.warning('Reach job create returned 409 but none found — disabled')
                    return None
            else:
                logger.warning(f'Could not create reach job ({e}) — impressions disabled')
                return None
        except Exception as e:
            logger.warning(f'Could not create reach job ({e}) — impressions disabled')
            return None

    conn.execute(
        'INSERT OR REPLACE INTO reporting_jobs (report_type, job_id, created_at) '
        'VALUES (?, ?, ?)',
        (REACH_REPORT_TYPE, job_id, datetime.now(timezone.utc).isoformat(timespec='seconds')),
    )
    conn.commit()
    return job_id


def _find_col(header: list, candidates) -> int:
    """Index of the first matching column (exact lower-case, then substring)."""
    lower = [h.lower() for h in header]
    for cand in candidates:
        if cand in lower:
            return lower.index(cand)
    for cand in candidates:
        for i, h in enumerate(lower):
            if cand in h:
                return i
    return -1


def _parse_reach_csv(text: str) -> dict:
    """Aggregate one CSV into {date: {'impressions': int, 'clicks': float}}.
    CTR per row is impressions*ctr = clicks; channel CTR is recomputed later as
    total clicks / total impressions so multi-row aggregation stays correct."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return {}

    di = _find_col(header, _DATE_COLS)
    ii = _find_col(header, _IMPRESSION_COLS)
    ci = _find_col(header, _CTR_COLS)
    if di < 0 or ii < 0:
        logger.warning(
            f'Reach CSV missing expected columns (date/impressions); header={header}'
        )
        return {}

    agg = {}
    for parts in reader:
        if len(parts) <= max(di, ii, ci if ci >= 0 else 0):
            continue
        date = parts[di]
        try:
            impressions = int(float(parts[ii] or 0))
        except ValueError:
            continue
        ctr = 0.0
        if ci >= 0:
            try:
                ctr = float(parts[ci] or 0)
            except ValueError:
                ctr = 0.0
        slot = agg.setdefault(date, {'impressions': 0, 'clicks': 0.0})
        slot['impressions'] += impressions
        slot['clicks'] += impressions * ctr
    return agg


def fetch_reach_by_day(service, creds, job_id: str, created_after: str = None) -> dict:
    """Download the job's reports and return {date: {'impressions', 'ctr'}}.
    created_after: RFC3339 timestamp to limit which reports are listed.

    YouTube emits multiple reports for the same day when it reprocesses/backfills
    data — same (startTime, endTime), newer createTime. The documented rule is to
    import ONLY the newest per day, never sum them, so we dedupe by (startTime,
    endTime) keeping the max createTime before parsing."""
    try:
        reports = []
        page_token = None
        while True:
            kwargs = {'jobId': job_id}
            if created_after:
                kwargs['createdAfter'] = created_after
            if page_token:
                kwargs['pageToken'] = page_token
            resp = service.jobs().reports().list(**kwargs).execute()
            reports.extend(resp.get('reports', []))
            page_token = resp.get('nextPageToken')
            if not page_token:
                break
    except Exception as e:
        logger.warning(f'Could not list reach reports ({e}) — impressions omitted')
        return {}

    if not reports:
        logger.info('No reach reports available yet (cold start ~48h after job creation)')
        return {}

    # Keep only the newest report per (startTime, endTime) data day.
    winners = {}
    for rep in reports:
        key = (rep.get('startTime'), rep.get('endTime'))
        cur = winners.get(key)
        if cur is None or (rep.get('createTime', '') > cur.get('createTime', '')):
            winners[key] = rep

    from google.auth.transport.requests import AuthorizedSession
    session = AuthorizedSession(creds)

    merged = {}  # date -> {'impressions', 'clicks'} (winners cover distinct days)
    for rep in winners.values():
        url = rep.get('downloadUrl')
        if not url:
            continue
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f'Failed to download a reach report ({e}); skipping')
            continue
        for date, slot in _parse_reach_csv(resp.text).items():
            m = merged.setdefault(date, {'impressions': 0, 'clicks': 0.0})
            m['impressions'] += slot['impressions']
            m['clicks'] += slot['clicks']

    out = {}
    for date, m in merged.items():
        impr = m['impressions']
        ctr = (m['clicks'] / impr) if impr else 0.0
        # Defensive: the CSV column might ship CTR as a percentage (0-100)
        # rather than a fraction (0-1). A real thumbnail CTR is never > 1.0
        # as a fraction, so treat anything above 1 as a percentage.
        if ctr > 1:
            ctr /= 100
        out[date] = {'impressions': impr, 'ctr': ctr}
    return out
