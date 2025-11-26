#!/usr/bin/env python3
"""
Script de teste local para validar a API sem deploy
"""

from youtube_transcript_api import YouTubeTranscriptApi

def test_public_video():
    """Testa com vídeo público"""
    print("=" * 60)
    print("Testando com vídeo PÚBLICO")
    print("=" * 60)

    video_id = "dQw4w9WgXcQ"  # Rick Astley - Never Gonna Give You Up

    try:
        ytt_api = YouTubeTranscriptApi()
        fetched = ytt_api.fetch(video_id, languages=['en'])

        print(f"✅ Sucesso!")
        print(f"Video ID: {fetched.video_id}")
        print(f"Idioma: {fetched.language} ({fetched.language_code})")
        print(f"Gerado automaticamente: {fetched.is_generated}")
        print(f"Total de snippets: {len(fetched.snippets)}")
        print("\nPrimeiros 3 snippets:")
        for i, snippet in enumerate(fetched.snippets[:3], 1):
            print(f"{i}. [{snippet.start:.2f}s] {snippet.text}")

        # Texto completo
        full_text = " ".join([s.text for s in fetched.snippets])
        print(f"\nTexto completo (primeiros 200 chars):")
        print(full_text[:200] + "...")

    except Exception as e:
        print(f"❌ Erro: {e}")


def test_member_video():
    """Testa com vídeo de membros (vai falhar sem auth)"""
    print("\n" + "=" * 60)
    print("Testando com vídeo de MEMBROS (sem autenticação)")
    print("=" * 60)

    video_id = "sft7TnDvGR0"  # Seu vídeo de membros

    try:
        ytt_api = YouTubeTranscriptApi()
        fetched = ytt_api.fetch(video_id, languages=['pt'])

        print(f"✅ Sucesso! (inesperado)")
        print(f"Video ID: {fetched.video_id}")

    except Exception as e:
        print(f"❌ Erro esperado: {type(e).__name__}")
        print(f"Mensagem: {str(e)[:200]}...")


def test_with_language_preference():
    """Testa especificando múltiplos idiomas"""
    print("\n" + "=" * 60)
    print("Testando com preferência de idiomas [pt, en]")
    print("=" * 60)

    video_id = "9bZkp7q19f0"  # PSY - GANGNAM STYLE (tem múltiplas legendas)

    try:
        ytt_api = YouTubeTranscriptApi()

        # Listar transcrições disponíveis
        transcript_list = ytt_api.list(video_id)
        print("Transcrições disponíveis:")
        for transcript in transcript_list:
            print(f"  - {transcript.language} ({transcript.language_code}) - Gerada: {transcript.is_generated}")

        # Buscar português ou inglês
        transcript = transcript_list.find_transcript(['pt', 'en'])
        fetched = transcript.fetch()

        print(f"\n✅ Transcrição encontrada!")
        print(f"Idioma selecionado: {fetched.language} ({fetched.language_code})")
        print(f"Total de snippets: {len(fetched.snippets)}")

    except Exception as e:
        print(f"❌ Erro: {e}")


if __name__ == "__main__":
    print("🧪 Teste Local - YouTube Transcript API\n")

    test_public_video()
    test_member_video()
    test_with_language_preference()

    print("\n" + "=" * 60)
    print("✅ Testes concluídos!")
    print("=" * 60)
