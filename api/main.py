from fastapi import FastAPI, HTTPException, Security, Header
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from typing import Optional, List
import os
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    InvalidVideoId,
)

app = FastAPI(
    title="YouTube Transcript API",
    description="API para extrair transcrições de vídeos do YouTube",
    version="1.0.0",
)

# Configuração de autenticação via API Key
API_KEY = os.getenv("API_KEY", "your-secret-api-key-change-this")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


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

    except InvalidVideoId:
        raise HTTPException(status_code=400, detail="Invalid video ID format")
    except VideoUnavailable:
        raise HTTPException(status_code=404, detail="Video not found or unavailable")
    except TranscriptsDisabled:
        raise HTTPException(
            status_code=403, detail="Transcripts are disabled for this video"
        )
    except NoTranscriptFound:
        raise HTTPException(
            status_code=404,
            detail=f"No transcript found for the specified languages: {request.languages}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
