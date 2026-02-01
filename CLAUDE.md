# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains **two distinct components**:

1. **youtube-transcript-api library** - The original open-source Python library for extracting YouTube transcripts (maintained by jdepoix)
2. **FastAPI wrapper** - A custom REST API service (`api/main.py`) that wraps the library and adds fallback support via yt-dlp for members-only videos

## Development Commands

### Library Development (upstream youtube-transcript-api)

The library uses Poetry and Poe for task management:

```bash
# Install dependencies (requires poetry)
poetry install --with test,dev

# Run tests
poe test

# Run tests with coverage (must be 100%)
poe coverage

# Format code
poe format

# Lint code
poe lint

# Run all pre-commit checks (format, lint, coverage)
poe precommit
```

### FastAPI API Development

The FastAPI wrapper is deployed via Docker on Dokploy:

```bash
# Local testing (without Docker)
python test_local.py

# Run API locally
uvicorn api.main:app --reload --port 8000

# Test API endpoint
curl -X POST http://localhost:8000/transcript \
  -H "X-API-Key: your-secret-api-key-change-this" \
  -H "Content-Type: application/json" \
  -d '{"video_id": "dQw4w9WgXcQ", "languages": ["en"]}'
```

### Docker/Deployment

```bash
# Build Docker image
docker build -t youtube-transcript-api .

# Run container locally
docker run -p 8000:8000 \
  -e API_KEY=your-secret-key \
  -e YOUTUBE_COOKIES=<base64-encoded-cookies> \
  youtube-transcript-api
```

**Deployment**: Automatic via Dokploy when pushing to `origin-new` (user's fork at `delonsp/youtube-transcript-api`)

## Architecture

### Two-Layer Fallback System

The FastAPI wrapper (`api/main.py`) implements a critical fallback mechanism for accessing YouTube transcripts:

**Primary Method**: `youtube-transcript-api` library
- Fast, uses official YouTube Transcript API
- **Limitation**: Currently broken for members-only videos (cookie auth issue #437)
- **Limitation**: Blocked on cloud provider IPs (AWS, GCP, DigitalOcean)

**Fallback Method**: `yt-dlp`
- Triggered on ANY exception from primary method
- Extracts subtitles via YouTube's subtitle API
- **Supports**: Members-only videos via cookie authentication
- **Trade-off**: Slower, more resource-intensive

### Exception Handling Flow

```python
try:
    # Try youtube-transcript-api first
    ytt_api = YouTubeTranscriptApi()
    fetched = ytt_api.fetch(video_id, languages=languages)
    return success_response
except Exception as e:
    # Log and try yt-dlp fallback
    logger.warning(f"youtube-transcript-api failed: {e}. Trying yt-dlp fallback...")
    try:
        result = fetch_with_ytdlp(video_id, languages)
        return success_response
    except Exception as ytdlp_error:
        # Both methods failed - return detailed error
        raise HTTPException(status_code=500, detail=both_errors)
```

### Cookie Authentication for Members-Only Videos

Members-only videos require authentication cookies. There are two methods:

**Method 1 - Direct Browser Extraction (Recommended for local scripts)**

yt-dlp can extract cookies directly from browsers. Pass the browser name instead of a file path:

```python
# In local_workflow.py / fill_doc_summaries.py
downloader = TranscriptDownloader(cookies_file='chrome')  # or 'firefox', 'edge', 'safari', 'opera'
```

```bash
# Command line
python local_workflow.py VIDEO_ID --members  # Defaults to chrome
python local_workflow.py VIDEO_ID --cookies firefox
```

**Method 2 - Cookie File (Required for Docker/cloud deployment)**

1. Export cookies from browser using "Get cookies.txt LOCALLY" extension
2. For Docker: Convert to base64: `cat youtube_cookies.txt | base64`
3. Set as environment variable: `YOUTUBE_COOKIES=<base64-string>`
4. The API decodes and creates temporary cookie file for yt-dlp

**Which to use:**
- **Local development**: Use browser extraction (`--cookies chrome`) - always fresh, no maintenance
- **Docker/Dokploy**: Use cookie file method - browser extraction doesn't work in containers

### yt-dlp Configuration

**Critical**: When extracting only subtitles, do NOT specify `format` option:

```python
# CORRECT - for subtitle extraction only
ydl_opts = {
    'skip_download': True,
    'writesubtitles': True,
    'writeautomaticsub': True,
}

# INCORRECT - causes "Requested format is not available" error
ydl_opts = {
    'skip_download': True,
    'format': 'bestaudio/best',  # ❌ Don't use when only extracting subtitles
}
```

## File Structure

```
.
├── youtube_transcript_api/     # Original library (upstream)
│   ├── _api.py                 # Core API implementation
│   ├── _transcripts.py         # Transcript objects
│   ├── _errors.py              # Custom exceptions
│   ├── formatters.py           # Output formatters (JSON, SRT, WebVTT, etc.)
│   └── proxies.py              # Proxy configuration (Webshare, generic)
├── api/                        # FastAPI wrapper (custom)
│   └── main.py                 # REST API with fallback logic
├── local_workflow.py           # Local processing: transcript → AI timestamps → YouTube comment
├── batch_process_videos.py     # Batch process multiple videos for timestamps
├── google_docs_manager.py      # Manage Google Docs for live summaries (stub entries)
├── fill_doc_summaries.py       # Fill Google Docs with AI-generated summaries + Q&A
├── test_local.py               # Quick local testing script
├── DEPLOY.md                   # Dokploy deployment guide
├── requirements.txt            # Production dependencies (FastAPI, yt-dlp)
├── requirements_local.txt      # Local development dependencies
├── youtube_cookies.txt         # Cookies for members-only videos (local only)
├── client_secrets.json         # OAuth credentials for YouTube Data API
├── token.pickle                # YouTube API OAuth token
└── token_docs.pickle           # Google Docs API OAuth token
```

## Important API Usage Patterns

### Using youtube-transcript-api Library (Instance Methods)

```python
# CORRECT - Use instance methods
ytt_api = YouTubeTranscriptApi()
transcript_list = ytt_api.list(video_id)
transcript = transcript_list.find_transcript(['pt', 'en'])
fetched = transcript.fetch(preserve_formatting=False)

# INCORRECT - Don't use static methods (they don't exist)
YouTubeTranscriptApi.list_transcripts(video_id)  # ❌
YouTubeTranscriptApi.get_transcript(video_id)    # ❌
```

### Accessing Transcript Data

```python
# FetchedTranscript provides both structured and text formats
fetched = ytt_api.fetch(video_id)

# Access individual snippets
for snippet in fetched.snippets:
    print(f"[{snippet.start}s] {snippet.text}")

# Get full text
full_text = " ".join([s.text for s in fetched.snippets])

# Metadata
print(fetched.video_id)         # str
print(fetched.language_code)    # str (e.g., "en", "pt")
print(fetched.is_generated)     # bool
```

## Known Issues & Workarounds

### IP Blocking by YouTube

**Problem**: DigitalOcean/AWS/GCP IPs are blocked by YouTube
**Error**: `RequestBlocked` or `IpBlocked`
**Solutions**:
1. Use yt-dlp fallback (already implemented)
2. Use rotating residential proxies (Webshare - documented in README.md)
3. Not an issue when deployed - yt-dlp fallback handles it

### Members-Only Videos

**Problem**: youtube-transcript-api cookie auth broken (Issue #437)
**Error**: `VideoUnplayable: "Join this channel to get access to members-only content"`
**Solution**: yt-dlp fallback with `YOUTUBE_COOKIES` environment variable

### Logging in Production

Use Python's `logging` module, not `print()`:

```python
import logging
logger = logging.getLogger(__name__)

# Logs will appear in Dokploy/Docker logs
logger.info("Message")
logger.warning("Warning")
logger.error("Error")
```

## Git Workflow

**Important**: This is a fork. Two remotes are configured:

- `origin` → `jdepoix/youtube-transcript-api` (upstream, read-only)
- `origin-new` → `delonsp/youtube-transcript-api` (user's fork, push here)

```bash
# Push to user's fork (triggers Dokploy auto-deploy)
git push origin-new master

# DO NOT push to origin (permission denied)
```

## Environment Variables

Required for production deployment:

- `API_KEY` - Secret key for API authentication (generate with `openssl rand -hex 32`)
- `YOUTUBE_COOKIES` (optional) - Base64-encoded cookies for members-only videos

## Testing Strategy

**Library tests**: Use `poe test` or `poe coverage` (100% coverage required)

**API tests**: Use `test_local.py` for quick validation before deployment:
- Tests public videos
- Tests members-only videos (expects failure without cookies)
- Tests language preferences

**Production testing**: Monitor Dokploy logs for:
- `"youtube-transcript-api failed: ... Trying yt-dlp fallback..."`
- `"✅ yt-dlp fallback succeeded!"`
- `"❌ yt-dlp fallback also failed:"`

## Common Pitfalls

1. **Don't use static methods** - `YouTubeTranscriptApi` requires instantiation
2. **Don't specify video format** when extracting only subtitles with yt-dlp
3. **Don't use `print()` in production** - use `logging` module
4. **Don't push to `origin`** - always push to `origin-new`
5. **Don't assume cookies work with youtube-transcript-api** - they're currently broken, use yt-dlp fallback
6. **Video IDs starting with hyphen** - Use `--` separator in argparse (e.g., `python script.py -- -YMooVl3oms`)

## Local Automation Scripts

### local_workflow.py - Single Video Processing

Processes a single video: downloads transcript → generates timestamps with AI → posts comment to YouTube.

```bash
# Process a public video
python local_workflow.py VIDEO_ID

# Process a members-only video (requires cookies)
python local_workflow.py VIDEO_ID --members

# Video ID starting with hyphen
python local_workflow.py --members -- -YMooVl3oms
```

**Features:**
- Uses DeepSeek API for timestamp generation
- Detects sibling videos (landscape/portrait versions of same live)
- Updates description AND posts pinned comment
- Checks for existing timestamps before processing

### batch_process_videos.py - Batch Processing

Scans channel videos and processes those missing timestamps.

```bash
# Scan last 300 videos (dry run - shows what would be processed)
python batch_process_videos.py --max-videos 300

# Process with confirmation
echo "sim" | python batch_process_videos.py --max-videos 300
```

**Features:**
- Filters members-only lives only
- Groups sibling videos (📱 portrait + landscape)
- Detects existing timestamps in description AND comments
- Prevents duplicate timestamp comments

### google_docs_manager.py - Live Summaries Document (Stubs)

Manages Google Doc with live summaries - lists undocumented lives and creates entry templates (stubs).

```bash
# List lives not documented since Nov 2024
python google_docs_manager.py --since 2024-11-20

# Add stub entries to document
python google_docs_manager.py --since 2024-11-20 --add
```

### fill_doc_summaries.py - Generate Full Summaries

Downloads transcripts and generates AI summaries + Q&A for undocumented lives.

```bash
# Dry run - list what would be processed
python fill_doc_summaries.py --since 2024-11-20

# Process and generate summaries (max 10)
python fill_doc_summaries.py --since 2024-11-20 --process --max 10
```

**Features:**
- Uses cookies directly from Chrome browser (no cookies.txt needed)
- Groups sibling videos (📱 portrait + landscape)
- Generates summary + Q&A with DeepSeek API
- Inserts formatted entries with proper spacing
- Requires: `DEEPSEEK_API_KEY` environment variable

**Requires (both scripts):**
- Google Docs API enabled in Cloud Console
- `client_secrets.json` with OAuth credentials
- First run opens browser for authentication

## Timestamp Detection Logic

The scripts check for existing timestamps in multiple places:

1. **Description**: Looks for 3+ timestamps (pattern `\d{1,2}:\d{2}`)
2. **Channel owner comments**: Checks comments by video owner for:
   - 3+ timestamps, OR
   - 1+ timestamp + keywords: "timestamps", "key points", "🎯", "📌", "marcações"

## YouTube Data API Usage

### Posting Comments

```python
from googleapiclient.discovery import build
import pickle

# Load OAuth credentials
with open('token.pickle', 'rb') as f:
    creds = pickle.load(f)

youtube = build('youtube', 'v3', credentials=creds)

# Post comment
youtube.commentThreads().insert(
    part='snippet',
    body={
        'snippet': {
            'videoId': video_id,
            'topLevelComment': {
                'snippet': {'textOriginal': comment_text}
            }
        }
    }
).execute()
```

### Updating Video Description

```python
youtube.videos().update(
    part='snippet',
    body={
        'id': video_id,
        'snippet': {
            'title': current_title,
            'description': new_description,
            'categoryId': category_id
        }
    }
).execute()
```

## Library Versions & Documentation

Key libraries used (verified with context7):

| Library | Version | Documentation |
|---------|---------|---------------|
| youtube-transcript-api | latest | [jdepoix/youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api) |
| yt-dlp | latest | [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) |
| FastAPI | 0.115+ | [fastapi.tiangolo.com](https://fastapi.tiangolo.com) |
| google-api-python-client | latest | [googleapis/google-api-python-client](https://github.com/googleapis/google-api-python-client) |
| DeepSeek API | V3.2-Exp | [api-docs.deepseek.com](https://api-docs.deepseek.com) |

### DeepSeek API Models (Updated Sep 2025)

| Model Name | Points To | Use Case |
|------------|-----------|----------|
| `deepseek-chat` | DeepSeek-V3.2-Exp (non-thinking) | General chat, summaries, timestamps |
| `deepseek-reasoner` | DeepSeek-V3.2-Exp (thinking) | Complex reasoning, math, code |

**Note:** Model names auto-upgrade to latest version. No need to change `deepseek-chat` in scripts.

**API Key Storage:** Use keyring (recommended) or environment variable:
```python
# Set key (one time)
import keyring
keyring.set_password('deepseek', 'api_key', 'sk-...')

# Scripts auto-retrieve from keyring, fallback to env var
api_key = keyring.get_password('deepseek', 'api_key') or os.getenv('DEEPSEEK_API_KEY')
```

### youtube-transcript-api Key Methods

```python
ytt_api = YouTubeTranscriptApi()

# Fetch with language priority
fetched = ytt_api.fetch(video_id, languages=['pt', 'en'])

# List available transcripts
transcript_list = ytt_api.list(video_id)
transcript = transcript_list.find_transcript(['pt', 'en'])
transcript = transcript_list.find_manually_created_transcript(['pt'])
transcript = transcript_list.find_generated_transcript(['en'])

# Translate transcript
translated = transcript.translate('en').fetch()

# Use proxy
from youtube_transcript_api.proxies import GenericProxyConfig
ytt_api = YouTubeTranscriptApi(
    proxy_config=GenericProxyConfig(
        http_url="http://user:pass@proxy:port",
        https_url="https://user:pass@proxy:port"
    )
)
```

### yt-dlp Python Embedding

```python
import yt_dlp

ydl_opts = {
    'skip_download': True,
    'writesubtitles': True,
    'writeautomaticsub': True,
    'subtitleslangs': ['pt', 'en'],
    'quiet': True,
}

# Authentication: choose ONE method
# Method 1: Direct browser extraction (local development)
ydl_opts['cookiesfrombrowser'] = ('chrome',)  # or 'firefox', 'edge', 'safari', 'opera'

# Method 2: Cookie file (Docker/cloud)
# ydl_opts['cookiefile'] = 'youtube_cookies.txt'

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info(url, download=False)
    subtitles = info.get('subtitles', {})
    automatic_captions = info.get('automatic_captions', {})
```
