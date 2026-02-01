#!/usr/bin/env python3
"""
Local workflow for YouTube transcript processing
================================================

This script:
1. Downloads transcripts from YouTube videos (including members-only) using yt-dlp
2. Processes transcripts with local AI to identify topics and timestamps
3. Posts formatted comments to YouTube videos using YouTube Data API

Requirements:
- youtube_transcript_api (fast method for public videos)
- yt-dlp (fallback for members-only videos)
- google-api-python-client (for posting comments)
- Local AI model (OpenAI, Anthropic, Ollama, etc.)
"""

import os
import json
import argparse
import tempfile
from typing import List, Dict, Optional
from datetime import timedelta
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TranscriptDownloader:
    """Handles downloading transcripts from YouTube videos"""

    def __init__(self, cookies_file: Optional[str] = None):
        self.cookies_file = cookies_file

    def download(self, video_id: str, languages: Optional[List[str]] = None) -> Dict:
        """
        Download transcript using youtube-transcript-api first,
        fallback to yt-dlp if needed
        """
        # Try fast method first (youtube-transcript-api)
        try:
            logger.info(f"Attempting youtube-transcript-api for {video_id}")
            result = self._download_with_transcript_api(video_id, languages)
            logger.info("✅ youtube-transcript-api succeeded")
            return result
        except Exception as e:
            logger.warning(f"youtube-transcript-api failed: {e}")

        # Fallback to yt-dlp for members-only or blocked videos
        try:
            logger.info(f"Attempting yt-dlp fallback for {video_id}")
            result = self._download_with_ytdlp(video_id, languages)
            logger.info("✅ yt-dlp succeeded")
            return result
        except Exception as e:
            logger.error(f"❌ Both methods failed: {e}")
            raise

    def _download_with_transcript_api(self, video_id: str, languages: Optional[List[str]]) -> Dict:
        """Fast method using youtube-transcript-api"""
        from youtube_transcript_api import YouTubeTranscriptApi

        ytt_api = YouTubeTranscriptApi()

        if languages:
            transcript_list = ytt_api.list(video_id)
            transcript = transcript_list.find_transcript(languages)
            fetched = transcript.fetch()
        else:
            fetched = ytt_api.fetch(video_id)

        return {
            'video_id': video_id,
            'language': fetched.language_code,
            'snippets': [
                {
                    'text': snippet.text,
                    'start': snippet.start,
                    'duration': snippet.duration
                }
                for snippet in fetched.snippets
            ],
            'method': 'youtube-transcript-api'
        }

    def _download_with_ytdlp(self, video_id: str, languages: Optional[List[str]]) -> Dict:
        """Fallback method using yt-dlp with cookies for members-only videos"""
        import yt_dlp
        import tempfile
        import shutil
        import json as json_lib

        video_url = f"https://www.youtube.com/watch?v={video_id}"
        temp_dir = tempfile.mkdtemp()

        try:
            ydl_opts = {
                'skip_download': True,
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitlesformat': 'json3',
                'outtmpl': os.path.join(temp_dir, '%(id)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
            }

            # Tentar cookies do navegador primeiro (mais confiável para members-only)
            if self.cookies_file:
                # Se cookies_file é "chrome", "firefox", etc, usar diretamente do navegador
                if self.cookies_file in ['chrome', 'firefox', 'edge', 'safari', 'opera']:
                    ydl_opts['cookiesfrombrowser'] = (self.cookies_file,)
                    logger.info(f"Using cookies directly from browser: {self.cookies_file}")
                else:
                    # Arquivo de cookies tradicional
                    ydl_opts['cookiefile'] = self.cookies_file
                    logger.info(f"Using cookies from file: {self.cookies_file}")

            if languages:
                ydl_opts['subtitleslangs'] = languages

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)  # download=True to save subtitles

                if not info:
                    raise Exception("Failed to extract video info")

                # Find downloaded subtitle file - prefer requested languages
                subtitle_file = None
                subtitle_lang = None
                available_files = []

                # List all subtitle files
                for filename in os.listdir(temp_dir):
                    if filename.endswith('.json3') or filename.endswith('.vtt') or filename.endswith('.srt'):
                        parts = filename.split('.')
                        if len(parts) >= 3:
                            lang = parts[-2]
                            available_files.append((filename, lang))

                if not available_files:
                    raise Exception("No subtitle files downloaded")

                logger.info(f"Available subtitle files: {[f[0] for f in available_files]}")

                # Try to find preferred language
                if languages:
                    for preferred_lang in languages:
                        for filename, lang in available_files:
                            if lang == preferred_lang:
                                subtitle_file = os.path.join(temp_dir, filename)
                                subtitle_lang = lang
                                logger.info(f"Using preferred language '{lang}': {filename}")
                                break
                        if subtitle_file:
                            break

                # If no preferred language found, use first available
                if not subtitle_file:
                    filename, lang = available_files[0]
                    subtitle_file = os.path.join(temp_dir, filename)
                    subtitle_lang = lang
                    logger.info(f"Using fallback language '{lang}': {filename}")

                if not subtitle_file:
                    raise Exception("No subtitle file downloaded")

                # Read and parse subtitle file
                with open(subtitle_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                data = json_lib.loads(content)

                snippets = []
                if 'events' in data:
                    for event in data['events']:
                        if 'segs' in event:
                            text = ''.join([seg.get('utf8', '') for seg in event['segs']])
                            if text.strip():
                                snippets.append({
                                    'text': text.strip(),
                                    'start': event.get('tStartMs', 0) / 1000.0,
                                    'duration': event.get('dDurationMs', 0) / 1000.0
                                })

                if not snippets:
                    raise Exception("No subtitle text found")

                logger.info(f"✅ Extracted {len(snippets)} snippets from yt-dlp")

                return {
                    'video_id': video_id,
                    'language': subtitle_lang or 'unknown',
                    'snippets': snippets,
                    'method': 'yt-dlp'
                }

        finally:
            # Clean up temporary directory
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)


class AIProcessor:
    """Processes transcripts with AI to identify topics and timestamps"""

    def __init__(self, provider: str = 'openai'):
        """
        Initialize AI processor

        Args:
            provider: 'openai', 'deepseek', 'anthropic', or 'ollama'
        """
        self.provider = provider

    def identify_topics(self, transcript_data: Dict) -> List[Dict]:
        """
        Use AI to identify topics and their timestamps from transcript

        Returns list of topics:
        [
            {
                'timestamp': 123.5,  # seconds
                'title': 'Introduction',
                'description': 'Brief overview of the video'
            },
            ...
        ]
        """
        # Get video duration from last snippet
        snippets = transcript_data['snippets']
        video_duration = snippets[-1]['start'] + snippets[-1]['duration'] if snippets else 0
        logger.info(f"Video duration: {int(video_duration // 60)}m{int(video_duration % 60)}s")

        # Combine all snippets into full text with timestamps
        full_text_with_timestamps = self._format_transcript_for_ai(snippets)

        # Call AI based on provider
        if self.provider == 'openai':
            topics = self._process_with_openai(full_text_with_timestamps, video_duration)
        elif self.provider == 'deepseek':
            topics = self._process_with_deepseek(full_text_with_timestamps, video_duration)
        elif self.provider == 'anthropic':
            topics = self._process_with_anthropic(full_text_with_timestamps, video_duration)
        elif self.provider == 'ollama':
            topics = self._process_with_ollama(full_text_with_timestamps, video_duration)
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

        # Filter out timestamps beyond video duration
        valid_topics = []
        for topic in topics:
            if topic['timestamp'] <= video_duration:
                valid_topics.append(topic)
            else:
                logger.warning(f"⚠️  Removed invalid timestamp: {self._seconds_to_timestamp(topic['timestamp'])} - {topic['title']} (beyond video duration)")

        logger.info(f"✅ Valid topics: {len(valid_topics)} / {len(topics)}")
        return valid_topics

    def _format_transcript_for_ai(self, snippets: List[Dict]) -> str:
        """Format transcript with timestamps for AI processing"""
        lines = []
        for snippet in snippets:
            timestamp = self._seconds_to_timestamp(snippet['start'])
            lines.append(f"[{timestamp}] {snippet['text']}")
        return '\n'.join(lines)

    def _seconds_to_timestamp(self, seconds: float) -> str:
        """Convert seconds to MM:SS or HH:MM:SS format"""
        td = timedelta(seconds=int(seconds))
        total_seconds = int(td.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60

        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes}:{secs:02d}"

    def _process_with_openai(self, full_text: str, video_duration: float) -> List[Dict]:
        """Process with OpenAI API"""
        from openai import OpenAI
        import keyring

        # Get API key from keyring (fallback to environment variable)
        api_key = keyring.get_password('openai', 'api_key') or os.getenv('OPENAI_API_KEY')
        if not api_key:
            raise ValueError(
                "OpenAI API key not found. Set it with:\n"
                "python -c \"import keyring; keyring.set_password('openai', 'api_key', 'sk-...')\""
            )

        client = OpenAI(api_key=api_key)

        prompt = f"""Analyze the following YouTube video transcript with timestamps and identify the main topics discussed.

For each topic, provide:
1. The starting timestamp (in seconds)
2. A concise title
3. A brief description

Return the response as a JSON array with objects containing: timestamp, title, description

Transcript:
{full_text}

Response format:
[
  {{"timestamp": 0, "title": "Introduction", "description": "Overview of the video"}},
  {{"timestamp": 120, "title": "First Topic", "description": "Discussion about..."}}
]
"""

        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that analyzes video transcripts and identifies key topics with timestamps."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )

        result = json.loads(response.choices[0].message.content)
        return result.get('topics', [])

    def _process_with_deepseek(self, full_text: str, video_duration: float) -> List[Dict]:
        """Process with DeepSeek API (OpenAI-compatible)"""
        from openai import OpenAI
        import keyring

        # Get API key from keyring (try multiple locations, fallback to environment variable)
        api_key = (
            keyring.get_password('deepseek', 'alain_dutra') or
            keyring.get_password('DEEPSEEK_API_KEY', 'alain_dutra') or
            keyring.get_password('deepseek', 'api_key') or
            os.getenv('DEEPSEEK_API_KEY')
        )
        if not api_key:
            raise ValueError(
                "DeepSeek API key not found. Set it with:\n"
                "python -c \"import keyring; keyring.set_password('deepseek', 'api_key', 'sk-...')\""
            )

        # DeepSeek uses OpenAI-compatible API
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com"
        )

        duration_formatted = f"{int(video_duration // 60)}:{int(video_duration % 60):02d}"

        prompt = f"""Analise a transcrição de vídeo do YouTube abaixo (com timestamps) e identifique os principais tópicos discutidos.

DURAÇÃO DO VÍDEO: {duration_formatted} ({int(video_duration)} segundos)

Para cada tópico, forneça:
1. O timestamp de início (em segundos)
2. Um título conciso EM PORTUGUÊS
3. Uma breve descrição EM PORTUGUÊS

IMPORTANTE:
- Toda a resposta deve ser em PORTUGUÊS BRASILEIRO
- Todos os timestamps devem estar DENTRO da duração do vídeo (máximo {int(video_duration)} segundos)
- NÃO invente timestamps além da duração do vídeo

Retorne a resposta como um array JSON com objetos contendo: timestamp, title, description

Transcrição:
{full_text}

Formato da resposta (EM PORTUGUÊS):
[
  {{"timestamp": 0, "title": "Introdução", "description": "Visão geral do vídeo"}},
  {{"timestamp": 120, "title": "Primeiro Tópico", "description": "Discussão sobre..."}}
]
"""

        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Você é um assistente útil que analisa transcrições de vídeos e identifica os principais tópicos com timestamps. Responda SEMPRE em português brasileiro."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )

        result = json.loads(response.choices[0].message.content)

        # Handle both formats: {"topics": [...]} or [...]
        if isinstance(result, list):
            return result
        else:
            return result.get('topics', [])

    def _process_with_anthropic(self, full_text: str, video_duration: float) -> List[Dict]:
        """Process with Anthropic Claude API"""
        from anthropic import Anthropic
        import keyring

        # Get API key from keyring (fallback to environment variable)
        api_key = keyring.get_password('anthropic', 'api_key') or os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError(
                "Anthropic API key not found. Set it with:\n"
                "python -c \"import keyring; keyring.set_password('anthropic', 'api_key', 'sk-ant-...')\""
            )

        client = Anthropic(api_key=api_key)

        prompt = f"""Analyze the following YouTube video transcript with timestamps and identify the main topics discussed.

For each topic, provide:
1. The starting timestamp (in seconds)
2. A concise title
3. A brief description

Return the response as a JSON array with objects containing: timestamp, title, description

Transcript:
{full_text}"""

        message = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=4096,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        # Extract JSON from response
        response_text = message.content[0].text
        # Find JSON array in response
        start = response_text.find('[')
        end = response_text.rfind(']') + 1
        json_str = response_text[start:end]

        return json.loads(json_str)

    def _process_with_ollama(self, full_text: str, video_duration: float) -> List[Dict]:
        """Process with local Ollama model"""
        import requests

        prompt = f"""Analyze the following YouTube video transcript with timestamps and identify the main topics discussed.

For each topic, provide:
1. The starting timestamp (in seconds)
2. A concise title
3. A brief description

Return ONLY a JSON array with objects containing: timestamp, title, description

Transcript:
{full_text}"""

        response = requests.post(
            'http://localhost:11434/api/generate',
            json={
                'model': 'llama2',  # or your preferred model
                'prompt': prompt,
                'stream': False
            }
        )

        result = response.json()
        response_text = result['response']

        # Extract JSON from response
        start = response_text.find('[')
        end = response_text.rfind(']') + 1
        json_str = response_text[start:end]

        return json.loads(json_str)


class YouTubeManager:
    """Manages YouTube operations (comments and video updates) using YouTube Data API"""

    def __init__(self, credentials_file: str):
        """
        Initialize with OAuth2 credentials

        Args:
            credentials_file: Path to client_secrets.json from Google Cloud Console
        """
        self.credentials_file = credentials_file
        self.youtube = None

    def authenticate(self):
        """Authenticate with YouTube Data API"""
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        import pickle

        SCOPES = ['https://www.googleapis.com/auth/youtube.force-ssl']

        creds = None
        # Token file stores user's access and refresh tokens
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)

        # If no valid credentials, let user log in
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)

            # Save credentials for next run
            with open('token.pickle', 'wb') as token:
                pickle.dump(creds, token)

        self.youtube = build('youtube', 'v3', credentials=creds)
        logger.info("✅ Authenticated with YouTube Data API")

    def has_timestamp_comment(self, video_id: str) -> bool:
        """
        Check if video already has a timestamp comment from the channel owner.

        Looks for comments containing timestamp patterns like:
        - 00:00, 0:00, 1:23:45 etc
        - Keywords like "timestamps", "key points", "marcações"

        Args:
            video_id: YouTube video ID

        Returns:
            True if a timestamp comment exists, False otherwise
        """
        if not self.youtube:
            raise Exception("Not authenticated. Call authenticate() first.")

        import re

        try:
            # Get video's channel ID first
            video_response = self.youtube.videos().list(
                part="snippet",
                id=video_id
            ).execute()

            if not video_response['items']:
                return False

            channel_id = video_response['items'][0]['snippet']['channelId']

            # Get comments from the video
            request = self.youtube.commentThreads().list(
                part="snippet",
                videoId=video_id,
                maxResults=50,  # Check top 50 comments
                order="relevance"  # Owner comments usually appear first
            )
            response = request.execute()

            # Timestamp patterns: 0:00, 00:00, 1:23:45, etc.
            timestamp_pattern = re.compile(r'\b\d{1,2}:\d{2}(?::\d{2})?\b')

            # Keywords that indicate timestamp comments
            timestamp_keywords = [
                'timestamp', 'timestamps', 'marcações', 'marcacoes',
                'key points', 'pontos chave', 'navigation', 'navegação',
                'índice', 'indice', 'chapters', 'capítulos', 'capitulos'
            ]

            for item in response.get('items', []):
                comment = item['snippet']['topLevelComment']['snippet']
                author_channel_id = comment.get('authorChannelId', {}).get('value', '')
                text = comment['textDisplay'].lower()

                # Only check comments from the channel owner
                if author_channel_id == channel_id:
                    # Check for timestamp patterns (at least 3 timestamps)
                    timestamps_found = timestamp_pattern.findall(text)
                    if len(timestamps_found) >= 3:
                        return True

                    # Check for timestamp keywords + at least 1 timestamp
                    if len(timestamps_found) >= 1:
                        for keyword in timestamp_keywords:
                            if keyword in text:
                                return True

            return False

        except Exception as e:
            logger.warning(f"⚠️  Could not check for existing timestamp comments: {e}")
            return False  # Assume no existing comment on error

    def post_comment(self, video_id: str, text: str) -> Dict:
        """
        Post a comment to a YouTube video

        Args:
            video_id: YouTube video ID
            text: Comment text

        Returns:
            Response from YouTube API
        """
        if not self.youtube:
            raise Exception("Not authenticated. Call authenticate() first.")

        request = self.youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {
                        "snippet": {
                            "textOriginal": text
                        }
                    }
                }
            }
        )

        response = request.execute()
        logger.info(f"✅ Comment posted successfully: {response['id']}")
        return response

    def update_video_description(self, video_id: str, topics: List[Dict], append: bool = True) -> Dict:
        """
        Update video description with timestamps

        Args:
            video_id: YouTube video ID
            topics: List of topics with timestamps
            append: If True, append to existing description. If False, replace.

        Returns:
            Response from YouTube API
        """
        if not self.youtube:
            raise Exception("Not authenticated. Call authenticate() first.")

        # Get current video details
        request = self.youtube.videos().list(
            part="snippet",
            id=video_id
        )
        response = request.execute()

        if not response['items']:
            raise Exception(f"Video {video_id} not found or no access")

        video = response['items'][0]
        snippet = video['snippet']
        current_description = snippet['description']

        # Format timestamps for description (YouTube timeline format)
        timestamp_lines = ["\n\nTimestamps:"]

        # Ensure first timestamp is 0:00 (YouTube requirement)
        if not topics or topics[0]['timestamp'] > 0:
            timestamp_lines.append("0:00 Início")

        for topic in topics:
            timestamp = format_timestamp(topic['timestamp'])
            # YouTube requires "0:00 Title" format (no dash, no emoji)
            timestamp_lines.append(f"{timestamp} {topic['title']}")

        timestamps_text = '\n'.join(timestamp_lines)

        # Append or replace description
        if append:
            # Check if timestamps already exist
            if "Timestamps:" in current_description:
                # Replace existing timestamps section
                parts = current_description.split("Timestamps:")
                new_description = parts[0].rstrip() + timestamps_text
            else:
                new_description = current_description + timestamps_text
        else:
            new_description = timestamps_text

        # Update video
        request = self.youtube.videos().update(
            part="snippet",
            body={
                "id": video_id,
                "snippet": {
                    "title": snippet['title'],
                    "description": new_description,
                    "categoryId": snippet['categoryId']
                }
            }
        )

        response = request.execute()
        logger.info(f"✅ Video description updated successfully")
        return response


def format_topics_as_comment(topics: List[Dict]) -> str:
    """Format AI-identified topics as a YouTube comment with timestamps"""
    lines = ["📌 Timestamps:"]
    lines.append("")

    for topic in topics:
        timestamp = format_timestamp(topic['timestamp'])
        lines.append(f"{timestamp} - {topic['title']}")
        if topic.get('description'):
            lines.append(f"  {topic['description']}")
        lines.append("")

    return '\n'.join(lines)


def format_timestamp(seconds: float) -> str:
    """Format seconds as clickable YouTube timestamp (MM:SS or HH:MM:SS)"""
    td = timedelta(seconds=int(seconds))
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"


def main():
    parser = argparse.ArgumentParser(
        description='Download YouTube transcript, process with AI, and post comment',
        epilog='Examples:\n'
               '  # Members-only video (uses cookies, Portuguese, DeepSeek):\n'
               '  python local_workflow.py VIDEO_ID --members\n\n'
               '  # Public video:\n'
               '  python local_workflow.py VIDEO_ID\n\n'
               '  # Custom options:\n'
               '  python local_workflow.py VIDEO_ID --members --languages en pt --ai-provider ollama',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('video_id', help='YouTube video ID')

    # Simplified options
    parser.add_argument(
        '--members',
        action='store_true',
        help='Video is members-only (will use youtube_cookies.txt)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Process transcript and show comment without posting'
    )
    parser.add_argument(
        '--no-description',
        action='store_true',
        help='Do not update video description (default: always update)'
    )
    parser.add_argument(
        '--no-comment',
        action='store_true',
        help='Do not post comment (default: always post)'
    )

    # Advanced options (with smart defaults)
    parser.add_argument(
        '--cookies',
        help='Path to cookies.txt file OR browser name (chrome/firefox/edge/safari/opera) for direct browser cookies (default: chrome if --members)',
        default=None
    )
    parser.add_argument(
        '--languages',
        nargs='+',
        help='Preferred languages (default: pt en)',
        default=['pt', 'en']
    )
    parser.add_argument(
        '--ai-provider',
        choices=['openai', 'deepseek', 'anthropic', 'ollama'],
        default='deepseek',
        help='AI provider (default: deepseek)'
    )
    parser.add_argument(
        '--youtube-credentials',
        help='Path to YouTube OAuth2 client_secrets.json (default: client_secrets.json)',
        default='client_secrets.json'
    )
    parser.add_argument(
        '--save-transcript',
        help='Save transcript to file (JSON format)',
        default=None
    )
    parser.add_argument(
        '--sibling-videos',
        help='Comma-separated list of sibling video IDs (same live, different formats)',
        default=None
    )

    args = parser.parse_args()

    # Apply smart defaults
    if args.members and not args.cookies:
        args.cookies = 'chrome'  # Usar cookies do Chrome diretamente (mais confiável)
        logger.info(f"ℹ️  Members mode: using cookies from browser '{args.cookies}'")

    try:
        # Step 1: Download transcript
        logger.info("=" * 60)
        logger.info("STEP 1: Downloading transcript")
        logger.info("=" * 60)

        downloader = TranscriptDownloader(cookies_file=args.cookies)
        transcript_data = downloader.download(args.video_id, args.languages)

        logger.info(f"Downloaded {len(transcript_data['snippets'])} snippets")
        logger.info(f"Language: {transcript_data['language']}")
        logger.info(f"Method: {transcript_data['method']}")

        # Save transcript if requested
        if args.save_transcript:
            with open(args.save_transcript, 'w', encoding='utf-8') as f:
                json.dump(transcript_data, f, indent=2, ensure_ascii=False)
            logger.info(f"Transcript saved to: {args.save_transcript}")

        # Step 2: Process with AI
        logger.info("")
        logger.info("=" * 60)
        logger.info("STEP 2: Processing with AI")
        logger.info("=" * 60)

        processor = AIProcessor(provider=args.ai_provider)
        topics = processor.identify_topics(transcript_data)

        logger.info(f"Identified {len(topics)} topics:")
        for topic in topics:
            logger.info(f"  {format_timestamp(topic['timestamp'])} - {topic['title']}")

        # Step 3: Format comment
        logger.info("")
        logger.info("=" * 60)
        logger.info("STEP 3: Formatting comment")
        logger.info("=" * 60)

        comment_text = format_topics_as_comment(topics)
        print("\nGenerated Comment:")
        print("-" * 60)
        print(comment_text)
        print("-" * 60)

        # Step 4: Post to YouTube (unless dry-run)
        if args.dry_run:
            logger.info("\n🔍 DRY RUN MODE - Comment not posted")
            return

        logger.info("")
        logger.info("=" * 60)
        logger.info("STEP 4: Updating YouTube")
        logger.info("=" * 60)

        manager = YouTubeManager(credentials_file=args.youtube_credentials)
        manager.authenticate()

        # Determinar quais vídeos atualizar
        video_ids_to_update = [args.video_id]
        if args.sibling_videos:
            sibling_ids = [vid.strip() for vid in args.sibling_videos.split(',')]
            # Apenas adicionar os irmãos (o video_id principal já está na lista)
            for vid in sibling_ids:
                if vid != args.video_id and vid not in video_ids_to_update:
                    video_ids_to_update.append(vid)

            logger.info(f"\n🔗 Detectados {len(video_ids_to_update)} vídeos irmãos (mesma live, formatos diferentes)")
            logger.info(f"   Aplicando timestamps em todos os {len(video_ids_to_update)} vídeos:")
            for vid in video_ids_to_update:
                logger.info(f"   - {vid}")

        # Update description with timestamps (default: always, unless --no-description)
        if not args.no_description:
            logger.info("\n📝 Updating video descriptions with timestamps...")
            for video_id in video_ids_to_update:
                logger.info(f"   Updating {video_id}...")
                manager.update_video_description(video_id, topics, append=True)
            logger.info(f"✅ {len(video_ids_to_update)} video descriptions updated! Timestamps will appear in YouTube timeline.")
        else:
            logger.info("\n⏭️  Skipping video description update (--no-description)")

        # Post comment (default: always, unless --no-comment)
        if not args.no_comment:
            logger.info("\n💬 Posting comments...")
            comments_posted = 0
            comments_skipped = 0
            for video_id in video_ids_to_update:
                # Check if video already has timestamp comment
                if manager.has_timestamp_comment(video_id):
                    logger.info(f"   ⏭️  {video_id}: já tem comentário com timestamps - pulando")
                    comments_skipped += 1
                    continue

                logger.info(f"   Posting to {video_id}...")
                response = manager.post_comment(video_id, comment_text)
                logger.info(f"   ✅ Comment posted: {response['id']}")
                comments_posted += 1

            if comments_posted > 0:
                logger.info(f"✅ {comments_posted} comments posted!")
            if comments_skipped > 0:
                logger.info(f"⏭️  {comments_skipped} videos already had timestamp comments")
        else:
            logger.info("\n⏭️  Skipping comment (--no-comment)")

        logger.info("")
        logger.info(f"🎉 SUCCESS! {len(video_ids_to_update)} vídeos atualizados!")

    except KeyboardInterrupt:
        logger.info("\n⚠️  Interrupted by user")
    except Exception as e:
        logger.error(f"\n❌ Error: {e}", exc_info=True)
        return 1

    return 0


if __name__ == '__main__':
    exit(main())
