#!/usr/bin/env python3
"""
Script para testar cookies do YouTube com yt-dlp
"""

import yt_dlp
import sys

def test_cookies(video_id: str, cookies_file: str = None, use_browser: str = None):
    """
    Testa se consegue acessar vídeo de membros

    Args:
        video_id: ID do vídeo
        cookies_file: Caminho para arquivo de cookies (opcional)
        use_browser: Nome do navegador (chrome, firefox, edge) para usar cookies direto (opcional)
    """
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        'skip_download': True,
        'quiet': False,
        'no_warnings': False,
    }

    if cookies_file:
        ydl_opts['cookiefile'] = cookies_file
        print(f"🍪 Usando cookies do arquivo: {cookies_file}")
    elif use_browser:
        ydl_opts['cookiesfrombrowser'] = (use_browser,)
        print(f"🌐 Usando cookies do navegador: {use_browser}")
    else:
        print("⚠️  Nenhum cookie fornecido - tentando sem autenticação")

    print(f"\n🎥 Testando vídeo: {video_url}\n")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

            if info:
                print("\n✅ SUCESSO! Conseguiu acessar o vídeo")
                print(f"Título: {info.get('title', 'N/A')}")
                print(f"Canal: {info.get('uploader', 'N/A')}")
                print(f"Duração: {info.get('duration', 0) // 60}m{info.get('duration', 0) % 60}s")

                # Verificar legendas
                subtitles = info.get('subtitles', {})
                auto_captions = info.get('automatic_captions', {})

                if subtitles:
                    print(f"\n📝 Legendas manuais disponíveis: {list(subtitles.keys())}")
                if auto_captions:
                    print(f"🤖 Legendas automáticas disponíveis: {list(auto_captions.keys())}")

                if not subtitles and not auto_captions:
                    print("\n⚠️  Nenhuma legenda disponível!")

                return True
            else:
                print("\n❌ FALHOU: yt-dlp retornou None")
                return False

    except Exception as e:
        print(f"\n❌ ERRO: {e}")
        return False


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Uso:")
        print("  # Com arquivo de cookies")
        print("  python test_cookies.py VIDEO_ID youtube_cookies.txt")
        print()
        print("  # Com cookies do navegador (Chrome)")
        print("  python test_cookies.py VIDEO_ID --browser chrome")
        print()
        print("  # Com cookies do navegador (Edge)")
        print("  python test_cookies.py VIDEO_ID --browser edge")
        print()
        print("  # Sem autenticação (apenas vídeos públicos)")
        print("  python test_cookies.py VIDEO_ID")
        sys.exit(1)

    video_id = sys.argv[1]
    cookies_file = None
    use_browser = None

    if len(sys.argv) > 2:
        if sys.argv[2] == '--browser':
            if len(sys.argv) > 3:
                use_browser = sys.argv[3]
            else:
                print("❌ Erro: Especifique o navegador (chrome, edge, firefox)")
                sys.exit(1)
        else:
            cookies_file = sys.argv[2]

    success = test_cookies(video_id, cookies_file, use_browser)
    sys.exit(0 if success else 1)
