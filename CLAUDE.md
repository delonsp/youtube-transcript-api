# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains **two components**:

1. **youtube-transcript-api library** - Original open-source Python library for extracting YouTube transcripts (upstream by jdepoix)
2. **Automation scripts + Docker cron container** - Custom scripts for batch processing YouTube channel videos (timestamps, summaries, Google Docs) deployed as a cron container on Dokploy

**Note**: The FastAPI wrapper (`api/main.py`) is deprecated and no longer deployed. All automation runs via cron jobs.

## Architecture

```
Dokploy VPS
└── Cron Container (single container)
    ├── 6h UTC - download_via_api.py      (download transcripts via OAuth)
    ├── 7h UTC - batch_process_videos.py  (timestamps for members-only lives)
    ├── 8h UTC - fill_doc_summaries.py    (Google Docs summaries, level 1)
    ├── 9h UTC - run_estudos_avancados.py (Google Docs summaries, level 2)
    ├── 10h Mon - check_auth_health.py    (test OAuth + cookies + analytics, Telegram alert)
    └── 11h UTC - channel_metrics_report.py (channel metrics digest via Telegram)
```

### Authentication for Members-Only Videos

**3-tier fallback** in `TranscriptDownloader` (`transcript_processor.py`):
1. **youtube-transcript-api** — no auth, works for public videos
2. **YouTube Captions API (OAuth)** — `token_captions.pickle`, works for ALL channel owner videos (including members-only). OAuth refresh tokens auto-renew indefinitely. **This is the primary method.**
3. **yt-dlp with cookies** — `youtube_cookies.txt` or browser. **Optional fallback only.** Cookies expire every 3-14 days on servers.

**Cookie auto-detection** via `get_default_cookies()`:
- **Local**: Uses `chrome` (direct browser extraction)
- **Docker**: Uses `youtube_cookies.txt` (decoded from `YOUTUBE_COOKIES` base64 env var)

**Health check** (`check_auth_health.py`, weekly Monday 10h UTC):
- Tests Captions API first (critical), then cookies (informational), then Analytics API (metrics job)
- Alerts via Telegram with severity based on which method failed

### Channel Metrics (channel_metrics_report.py)

Daily digest of channel metrics via Telegram (11h UTC, reference day D-3 in UTC).
Digest layout: origin tag `[vindo do módulo YT da VPS]` → anomalies → 7-day rollup
(WoW) → daily D-3 → top 5 videos as a monospace `<pre>` table (Vídeo/Views/Inscr/
Conv/Ret, where Conv = net subs ÷ views) → retention curve of #1 → totals, plus a
30-day trend chart image.

- **YouTube Analytics API v2** (`token_analytics.pickle`, scopes `yt-analytics.readonly` + `youtube.readonly`): daily + 7-day views, watch time, subscribers gained/lost, engagement; per-video net subscribers and retention (`averageViewPercentage`); retention curve of the #1 video (`elapsedVideoTimeRatio`, fail-soft — empty for members-only/low-view)
- **YouTube Reporting API v1** (`youtube_reporting.py`, same token): thumbnail impressions + CTR (`channel_reach_basic_a1` bulk CSV). **Fail-soft**: omitted from the digest until the *YouTube Reporting API* is enabled in GCP AND its first report is ready (~48h cold start). Dedupes backfill reports by newest `createTime`.
- **YouTube Data API v3** (same token): public counters snapshot (`subscriberCount` rounded DOWN to 3 sig figs even for the owner — shown as "~")
- **Trend chart**: 30-day views line via matplotlib (Agg), sent with `sendPhoto`
- **Storage**: SQLite at `metrics/metrics.db` (named volume `metrics:`). Tables: `channel_daily`, `video_daily`, `video_window`, `channel_reach`, `reporting_jobs`, `channel_snapshot`. Last 7 days re-upserted each run; `consolidated` flag marks days >= 72h old; self-heals gaps after downtime
- **Supabase mirror** (`supabase_sync.py`, fail-soft): each run upserts the SQLite history into Postgres tables `public.yt_metrics_*` in the **N8N** project (`ajcsyvqlruambfavyqfd`) for dashboards/queries. SQLite stays primary. Uses PostgREST + service_role key (`SUPABASE_URL` + `SUPABASE_SERVICE_KEY` env); tables have RLS enabled with no policies (service_role only). Skipped on `--dry-run`
- **Anomaly alerts**: z-score >= 2.5 vs 28 consolidated days + fallback drop > 40% vs 7d average
- First run with empty DB backfills 365 days automatically
- Members-only videos ARE included in Analytics API data (verified empirically)
- **Caveat**: per-video subscriber delta is watch-page-only and does NOT sum to the channel net (documented in code — don't "fix" the discrepancy). `averageViewPercentage` can exceed 100% for Shorts (replays)
- **NOT available anywhere via API**: memberships/Super Chat revenue (only in YouTube Studio)
- Flags: `--dry-run`, `--date YYYY-MM-DD`, `--backfill N`, `--no-chart`
- Full research: `RESEARCH_cron_metricas_youtube.md`

**Refreshing OAuth token** (rare — only if revoked):
1. Run: `python download_via_api.py --max 1`
2. Follow OAuth flow in browser
3. Update Dokploy: `cat token_captions.pickle | base64` → `TOKEN_CAPTIONS_B64`

**Refreshing cookies** (optional fallback, only if needed):
1. Open a members-only video in Chrome
2. Extension "Get cookies.txt LOCALLY" → Export (blue button)
3. `cat ~/Downloads/youtube.com_cookies.txt | base64` → Dokploy `YOUTUBE_COOKIES`

### AI Provider

All scripts use **DeepSeek** (`deepseek-chat` model) via OpenAI-compatible API:
- `batch_process_videos.py` - timestamps
- `fill_doc_summaries.py` - summaries + Q&A (level 1)
- `estudos_avancados_processor.py` - summaries + timestamps + Q&A (level 2)

API key: keyring (`deepseek`, `api_key`) with fallback to `DEEPSEEK_API_KEY` env var.

## Development Commands

### Library Development (upstream)

```bash
poetry install --with test,dev
poe test          # Run tests
poe coverage      # Tests with coverage (must be 100%)
poe format        # Format code
poe precommit     # Format, lint, coverage
```

### Docker/Deployment

```bash
# Build and test locally
docker compose build cron
docker compose --env-file .env run --rm cron bash -c "crontab -l"

# Test a single job
docker compose --env-file .env run --rm cron bash -c ". /app/.env.cron && python batch_process_videos.py --max-videos 5"
```

**Deployment**: Automatic via Dokploy on push to `origin-new`.

## File Structure

```
.
├── youtube_transcript_api/          # Original library (upstream)
├── api/main.py                      # FastAPI wrapper (DEPRECATED, not deployed)
├── transcript_processor.py          # Core: TranscriptDownloader, YouTubeManager
├── batch_process_videos.py          # Batch timestamps for members-only lives
├── fill_doc_summaries.py            # Google Docs summaries (level 1 - Tira Duvidas)
├── estudos_avancados_processor.py   # Google Docs summaries (level 2 - Estudos Avancados)
├── run_estudos_avancados.py         # Wrapper: lists pending and processes each
├── download_via_api.py              # Download transcripts via YouTube Captions API
├── google_docs_manager.py           # Google Docs stub entry manager
├── channel_metrics_report.py        # Daily channel metrics digest (Analytics API -> SQLite -> Telegram)
├── youtube_reporting.py             # Reporting API component: thumbnail impressions/CTR (fail-soft)
├── supabase_sync.py                 # Mirror metrics to Supabase Postgres (fail-soft, PostgREST)
├── telegram_utils.py                # Shared Telegram helper (send_telegram + send_telegram_photo)
├── Dockerfile                       # Cron container (python:3.12-slim + cron)
├── docker-compose.yml               # Single service: cron
├── entrypoint-cron.sh               # Decodes base64 secrets, installs crontab
├── crontab.txt                      # 5 daily jobs (6h-9h, 11h UTC) + weekly health check
├── requirements_local.txt           # Python dependencies
├── .env                             # Local env vars with base64 secrets (gitignored)
├── youtube_cookies.txt              # Cookies for members-only (gitignored)
├── client_secrets.json              # OAuth credentials (gitignored)
├── token.pickle                     # YouTube API OAuth token
├── token_captions.pickle            # YouTube Captions API OAuth token
├── token_docs.pickle                # Google Docs API OAuth token (level 1)
├── token_estudos_avancados.pickle   # Google Docs API OAuth token (level 2)
└── token_analytics.pickle           # YouTube Analytics API OAuth token (metrics)
```

## Environment Variables (Dokploy)

All tokens/secrets as base64, decoded by `entrypoint-cron.sh`:

| Env Var | Source File | Usage |
|---|---|---|
| `CLIENT_SECRETS_B64` | client_secrets.json | Google OAuth |
| `TOKEN_PICKLE_B64` | token.pickle | YouTube API |
| `TOKEN_CAPTIONS_B64` | token_captions.pickle | YouTube Captions API |
| `TOKEN_DOCS_B64` | token_docs.pickle | Google Docs (level 1) |
| `TOKEN_ESTUDOS_B64` | token_estudos_avancados.pickle | Google Docs (level 2) |
| `TOKEN_ANALYTICS_B64` | token_analytics.pickle | YouTube Analytics API (metrics) |
| `YOUTUBE_COOKIES` | youtube_cookies.txt | yt-dlp members-only |
| `DEEPSEEK_API_KEY` | (direct) | DeepSeek AI API |
| `SUPABASE_URL` | (direct) | Supabase metrics mirror (N8N project) |
| `SUPABASE_SERVICE_KEY` | (direct) | Supabase service_role key (server-only) |

Generate with: `cat <file> | base64`

## Git Workflow

Fork with two remotes:
- `origin` -> `jdepoix/youtube-transcript-api` (upstream, read-only)
- `origin-new` -> `delonsp/youtube-transcript-api` (push here, triggers Dokploy)

```bash
git push origin-new master    # Deploy
# NEVER push to origin
```

## Local Automation Scripts

### batch_process_videos.py - Batch Timestamps (Members-Only)

```bash
python batch_process_videos.py --max-videos 300              # Dry run
echo "sim" | python batch_process_videos.py --max-videos 300 # Process
```

Filters members-only lives, groups siblings (landscape + portrait), detects existing timestamps.

### fill_doc_summaries.py - Google Docs Summaries (Level 1)

```bash
python fill_doc_summaries.py --since 2024-11-20                     # Dry run
python fill_doc_summaries.py --since 2024-11-20 --process --max 10  # Process
```

### estudos_avancados_processor.py - Estudos Avancados (Level 2)

```bash
python estudos_avancados_processor.py --list-pending     # List pending
python estudos_avancados_processor.py VIDEO_ID           # Process one
python estudos_avancados_processor.py VIDEO_ID --dry-run # Preview
```

### download_via_api.py - Transcript Downloads

```bash
python download_via_api.py --max 500 --delay 2 --output ./transcripts
```

Uses YouTube Captions API directly (all videos, public + members).

### channel_metrics_report.py - Channel Metrics Digest

```bash
python channel_metrics_report.py --dry-run            # Print digest, no Telegram
python channel_metrics_report.py --date 2026-06-04    # Override reference day
python channel_metrics_report.py --backfill 90        # Force re-fetch window
python channel_metrics_report.py                      # Full run (DB + Telegram)
```

First run on an empty DB backfills 365 days. Reference day is D-3 (UTC).

## Important Patterns

### youtube-transcript-api (Instance Methods)

```python
ytt_api = YouTubeTranscriptApi()
fetched = ytt_api.fetch(video_id, languages=['pt', 'en'])
# WRONG: YouTubeTranscriptApi.get_transcript()  # Static methods don't exist
```

### yt-dlp Subtitle Extraction

```python
ydl_opts = {
    'skip_download': True,
    'writesubtitles': True,
    'writeautomaticsub': True,
    # Do NOT specify 'format' when only extracting subtitles
}
```

### Timestamp Detection

Checks description (3+ timestamps) and channel owner comments (3+ timestamps OR 1+ timestamp + keywords like "timestamps", "key points").

## Common Pitfalls

1. **Don't use static methods** - `YouTubeTranscriptApi` requires instantiation
2. **Don't specify video format** when extracting only subtitles with yt-dlp
3. **Don't use `print()` in production** - use `logging` module
4. **Don't push to `origin`** - always push to `origin-new`
5. **Video IDs starting with hyphen** - Use `--` separator (e.g., `python script.py -- -YMooVl3oms`)
6. **Chrome cookies** - Must close Chrome before extracting cookies with yt-dlp
