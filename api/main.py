from fastapi import FastAPI, HTTPException, Security, Header
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from typing import Optional, List
import os
import base64
import tempfile
import yt_dlp
import logging
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    InvalidVideoId,
)

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="YouTube Transcript API",
    description="API para extrair transcrições de vídeos do YouTube",
    version="1.0.0",
)

# Configuração de autenticação via API Key
API_KEY = os.getenv("API_KEY", "your-secret-api-key-change-this")
YOUTUBE_COOKIES_B64 = os.getenv("YOUTUBE_COOKIES", None)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


def get_cookies_file():
    """Decodifica cookies base64 e salva em arquivo temporário"""
    if not YOUTUBE_COOKIES_B64:
        return None

    try:
        cookies_content = base64.b64decode(YOUTUBE_COOKIES_B64).decode('utf-8')
        temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt')
        temp_file.write(cookies_content)
        temp_file.flush()
        return temp_file.name
    except Exception as e:
        logger.error(f"Error decoding cookies: {e}")
        return None


def fetch_with_ytdlp(video_id: str, languages: Optional[List[str]] = None):
    """Fallback usando yt-dlp para vídeos bloqueados ou de membros"""
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    # Adicionar cookies se disponíveis
    cookies_file = get_cookies_file()
    if cookies_file:
        logger.info(f"Using cookies file: {cookies_file}")
    else:
        logger.warning("No cookies file available - members-only videos may fail")

    ydl_opts = {
        'skip_download': True,  # NÃO baixar vídeo
        'writesubtitles': True,  # Baixar legendas manuais
        'writeautomaticsub': True,  # Baixar legendas automáticas
        'quiet': True,  # Silenciar output
        'no_warnings': True,  # Sem warnings
        # Node.js será detectado automaticamente pelo yt-dlp
    }

    # Adicionar cookies para vídeos de membros
    if cookies_file:
        ydl_opts['cookiefile'] = cookies_file
        logger.info(f"Cookie file size: {os.path.getsize(cookies_file)} bytes")

        # Verificar se arquivo existe e tem conteúdo
        with open(cookies_file, 'r') as f:
            first_line = f.readline().strip()
            logger.info(f"Cookie file first line: {first_line[:50]}...")
    else:
        logger.warning("No cookies - this will likely fail for members-only videos")

    # Configurar idiomas preferidos
    if languages:
        ydl_opts['subtitleslangs'] = languages

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Attempting yt-dlp extraction for {video_id}")
            logger.info(f"yt-dlp options: {ydl_opts}")

            info = ydl.extract_info(video_url, download=False)

            if not info:
                raise Exception("yt-dlp returned None - likely authentication failure")

            logger.info(f"✅ Video info extracted successfully!")
            logger.info(f"Available subtitle languages: {list(info.get('subtitles', {}).keys())}")
            logger.info(f"Available automatic captions: {list(info.get('automatic_captions', {}).keys())}")

            # Tentar pegar legendas nos idiomas solicitados
            subtitles = info.get('subtitles', {})
            automatic_captions = info.get('automatic_captions', {})

            # Preferir legendas manuais
            available_subs = subtitles or automatic_captions

            if not available_subs:
                raise Exception("No subtitles found")

            # Selecionar idioma
            selected_lang = None
            if languages:
                for lang in languages:
                    if lang in available_subs:
                        selected_lang = lang
                        break

            if not selected_lang:
                selected_lang = list(available_subs.keys())[0]

            # Baixar legendas
            subtitle_url = None
            for fmt in available_subs[selected_lang]:
                if fmt.get('ext') in ['json3', 'srv3']:
                    subtitle_url = fmt.get('url')
                    break

            if not subtitle_url:
                raise Exception("Could not find suitable subtitle format")

            # Buscar conteúdo das legendas
            import requests
            response = requests.get(subtitle_url)
            response.raise_for_status()

            # Parse JSON (formato YouTube)
            import json
            data = response.json()

            snippets = []
            if 'events' in data:
                for event in data['events']:
                    if 'segs' in event:
                        text = ''.join([seg.get('utf8', '') for seg in event['segs']])
                        snippets.append({
                            'text': text.strip(),
                            'start': event.get('tStartMs', 0) / 1000.0,
                            'duration': event.get('dDurationMs', 0) / 1000.0
                        })

            return {
                'video_id': video_id,
                'language': selected_lang,
                'snippets': snippets,
                'method': 'yt-dlp'
            }

    finally:
        # Limpar arquivo temporário de cookies
        if cookies_file and os.path.exists(cookies_file):
            os.unlink(cookies_file)


def verify_api_key(api_key: str = Security(api_key_header)):
    """Valida a API Key"""
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return api_key


class TranscriptRequest(BaseModel):
    video_id: str = Field(..., description="ID do vídeo do YouTube")
    languages: Optional[List[str]] = Field(
        None,
        description="Lista de idiomas preferidos (ex: ['pt', 'en']). Se não informado, usa o idioma padrão do vídeo.",
    )
    preserve_formatting: Optional[bool] = Field(
        False, description="Preservar formatação do texto"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "video_id": "dQw4w9WgXcQ",
                "languages": ["pt", "en"],
                "preserve_formatting": False,
            }
        }


class TranscriptSnippet(BaseModel):
    text: str
    start: float
    duration: float


class TranscriptResponse(BaseModel):
    video_id: str
    language: str
    transcript: List[TranscriptSnippet]
    full_text: str


@app.get("/")
def read_root():
    """Health check endpoint"""
    return {
        "status": "ok",
        "message": "YouTube Transcript API is running",
        "docs": "/docs",
    }


@app.get("/health")
def health_check():
    """Health check endpoint"""
    return {"status": "healthy"}


@app.post("/transcript", response_model=TranscriptResponse)
def get_transcript(
    request: TranscriptRequest, api_key: str = Security(verify_api_key)
):
    """
    Extrai a transcrição de um vídeo do YouTube

    - **video_id**: ID do vídeo (ex: dQw4w9WgXcQ da URL youtube.com/watch?v=dQw4w9WgXcQ)
    - **languages**: Lista opcional de idiomas preferidos (ex: ['pt', 'en'])
    - **preserve_formatting**: Preservar formatação (padrão: False)
    """
    try:
        # Instanciar a API
        ytt_api = YouTubeTranscriptApi()

        # Buscar transcrição usando a nova API
        if request.languages:
            # Buscar lista de transcrições disponíveis
            transcript_list = ytt_api.list(request.video_id)
            # Encontrar transcrição no idioma desejado
            transcript = transcript_list.find_transcript(request.languages)
            # Fetch com preserve_formatting
            fetched = transcript.fetch(preserve_formatting=request.preserve_formatting)
        else:
            # Buscar transcrição padrão diretamente
            fetched = ytt_api.fetch(
                request.video_id,
                preserve_formatting=request.preserve_formatting
            )

        # Converter para o formato de resposta
        snippets = [
            TranscriptSnippet(
                text=snippet.text,
                start=snippet.start,
                duration=snippet.duration
            )
            for snippet in fetched.snippets
        ]

        # Gerar texto completo
        if request.preserve_formatting:
            full_text = "\n".join([snippet.text for snippet in fetched.snippets])
        else:
            full_text = " ".join([snippet.text for snippet in fetched.snippets])

        return TranscriptResponse(
            video_id=fetched.video_id,
            language=fetched.language_code,
            transcript=snippets,
            full_text=full_text,
        )

    except Exception as e:
        # Tentar fallback com yt-dlp para qualquer erro
        logger.warning(f"youtube-transcript-api failed: {type(e).__name__}: {e}. Trying yt-dlp fallback...")
        try:
            result = fetch_with_ytdlp(request.video_id, request.languages)

            # Converter snippets
            snippets = [
                TranscriptSnippet(
                    text=s['text'],
                    start=s['start'],
                    duration=s['duration']
                )
                for s in result['snippets']
            ]

            # Gerar texto completo
            if request.preserve_formatting:
                full_text = "\n".join([s['text'] for s in result['snippets']])
            else:
                full_text = " ".join([s['text'] for s in result['snippets']])

            logger.info(f"✅ yt-dlp fallback succeeded! Retrieved {len(snippets)} snippets")

            return TranscriptResponse(
                video_id=result['video_id'],
                language=result['language'],
                transcript=snippets,
                full_text=full_text,
            )

        except Exception as ytdlp_error:
            # Se yt-dlp também falhar, retornar ambos os erros
            logger.error(f"❌ yt-dlp fallback also failed: {ytdlp_error}")

            # Retornar erro específico baseado no erro original
            if isinstance(e, InvalidVideoId):
                raise HTTPException(status_code=400, detail="Invalid video ID format")
            elif isinstance(e, VideoUnavailable):
                raise HTTPException(
                    status_code=404,
                    detail=f"Video not found or unavailable. yt-dlp also failed: {str(ytdlp_error)}"
                )
            elif isinstance(e, TranscriptsDisabled):
                raise HTTPException(
                    status_code=403,
                    detail=f"Transcripts are disabled for this video. yt-dlp also failed: {str(ytdlp_error)}"
                )
            elif isinstance(e, NoTranscriptFound):
                raise HTTPException(
                    status_code=404,
                    detail=f"No transcript found. yt-dlp also failed: {str(ytdlp_error)}"
                )
            else:
                # Erro genérico
                raise HTTPException(
                    status_code=500,
                    detail=f"Both methods failed. youtube-transcript-api: {str(e)}. yt-dlp: {str(ytdlp_error)}"
                )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
