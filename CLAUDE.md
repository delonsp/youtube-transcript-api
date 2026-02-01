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
    ├── 6h UTC - download_via_api.py      (download transcripts)
    ├── 7h UTC - batch_process_videos.py  (timestamps for members-only lives)
    ├── 8h UTC - fill_doc_summaries.py    (Google Docs summaries, level 1)
    └── 9h UTC - run_estudos_avancados.py (Google Docs summaries, level 2)
```

### Cookie Authentication for Members-Only Videos

Auto-detection via `get_default_cookies()` in `transcript_processor.py`:
- **Local**: Uses `chrome` (direct browser extraction, always fresh)
- **Docker**: Uses `youtube_cookies.txt` (decoded from `YOUTUBE_COOKIES` base64 env var)

**Exporting cookies** (when Docker cookies expire):
1. Close Chrome completely (Cmd+Q)
2. Run: `yt-dlp --cookies-from-browser chrome --cookies youtube_cookies.txt --skip-download "https://youtube.com"`
3. Update Dokploy env var: `cat youtube_cookies.txt | base64`

**Chrome profile**: Default profile (`alain.uro@gmail.com`) is the channel owner account.

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
├── Dockerfile                       # Cron container (python:3.12-slim + cron)
├── docker-compose.yml               # Single service: cron
├── entrypoint-cron.sh               # Decodes base64 secrets, installs crontab
├── crontab.txt                      # 4 daily jobs (6h, 7h, 8h, 9h UTC)
├── requirements_local.txt           # Python dependencies
├── .env                             # Local env vars with base64 secrets (gitignored)
├── youtube_cookies.txt              # Cookies for members-only (gitignored)
├── client_secrets.json              # OAuth credentials (gitignored)
├── token.pickle                     # YouTube API OAuth token
├── token_captions.pickle            # YouTube Captions API OAuth token
├── token_docs.pickle                # Google Docs API OAuth token (level 1)
└── token_estudos_avancados.pickle   # Google Docs API OAuth token (level 2)
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
| `YOUTUBE_COOKIES` | youtube_cookies.txt | yt-dlp members-only |
| `DEEPSEEK_API_KEY` | (direct) | DeepSeek AI API |

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
