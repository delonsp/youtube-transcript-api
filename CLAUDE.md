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

Members-only videos require authentication cookies:

1. Export cookies from browser using "Get cookies.txt LOCALLY" extension
2. Convert to base64: `cat youtube_cookies.txt | base64`
3. Set as environment variable: `YOUTUBE_COOKIES=<base64-string>`
4. The API decodes and creates temporary cookie file for yt-dlp

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
├── test_local.py               # Quick local testing script
├── DEPLOY.md                   # Dokploy deployment guide
└── requirements.txt            # Production dependencies (FastAPI, yt-dlp)
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
