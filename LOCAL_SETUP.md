# 🏠 Local Workflow Setup

Guia para rodar os scripts de download/processamento localmente no Mac.

> **Nota**: produção roda em container Dokploy na VPS via cron. Este documento é apenas para desenvolvimento e operações ad-hoc no laptop. A fonte da verdade dos jobs é `crontab.txt`.

## 📋 Conteúdo

1. [Pré-requisitos](#pré-requisitos)
2. [Instalação](#instalação)
3. [Autenticação](#autenticação)
4. [DeepSeek API Key](#deepseek-api-key)
5. [Scripts disponíveis](#scripts-disponíveis)
6. [Cookies (fallback opcional)](#cookies-fallback-opcional)
7. [Troubleshooting](#troubleshooting)

---

## Pré-requisitos

- macOS, Python 3.12+
- Poetry (gerenciador oficial do projeto — `pyproject.toml` é a fonte da verdade)
- Conta Google Cloud com YouTube Data API v3 habilitada

---

## Instalação

```bash
cd /Users/alain_dutra/youtube-transcript-api

# Para a biblioteca upstream (testes/dev)
poetry install --with test,dev

# Para os scripts de automação (mesmas deps do Docker)
pip install -r requirements_local.txt
```

---

## Autenticação

O sistema usa **3 tiers** para baixar transcripts (ver `transcript_processor.py`):

1. **youtube-transcript-api** — sem auth, vídeos públicos
2. **YouTube Captions API (OAuth)** — `token_captions.pickle`, **método primário** para todos os vídeos do canal (incluindo members-only). OAuth refresh tokens renovam indefinidamente.
3. **yt-dlp + cookies** — fallback opcional (cookies expiram em 3-14 dias, ver seção [Cookies](#cookies-fallback-opcional))

### Setup do OAuth (uma vez por máquina)

1. **Google Cloud Console**: criar projeto, habilitar **YouTube Data API v3**, gerar credenciais OAuth (Desktop app), baixar como `client_secrets.json` no root do projeto.

2. **Primeiro run** abre browser pra autorizar e gera os tokens pickle:
   ```bash
   python download_via_api.py --max 1
   ```

3. **Tokens gerados** (todos gitignored):
   - `token.pickle` — YouTube Data API
   - `token_captions.pickle` — Captions API (primário)
   - `token_docs.pickle` — Google Docs (level 1)
   - `token_estudos_avancados.pickle` — Google Docs (level 2)

### Renovar token (raro — só se revogado)

1. Apagar o pickle correspondente
2. Re-rodar o script — fluxo OAuth abre no browser
3. Se for atualizar a VPS: `cat token_captions.pickle | base64` → setar `TOKEN_CAPTIONS_B64` no Dokploy

---

## DeepSeek API Key

Todos os scripts de IA (timestamps, summaries) usam **DeepSeek** (`deepseek-chat`).

```bash
# Salvar no Keychain (recomendado — persistente)
python -c "import keyring; keyring.set_password('deepseek', 'api_key', 'sk-...')"

# Verificar
python -c "import keyring; print('OK' if keyring.get_password('deepseek','api_key') else 'NOT FOUND')"
```

Fallback: variável de ambiente `DEEPSEEK_API_KEY`.

> Os scripts checam keyring primeiro, depois env var.

---

## Scripts disponíveis

| Script | Função | Equivalente cron |
|---|---|---|
| `download_via_api.py` | Baixa transcripts via Captions API | 6h UTC daily |
| `batch_process_videos.py` | Timestamps para lives members-only | 7h UTC daily |
| `fill_doc_summaries.py` | Google Docs summaries (level 1) | 8h UTC daily |
| `run_estudos_avancados.py` | Google Docs summaries (level 2) | 9h UTC daily |
| `check_auth_health.py` | Testa Captions API + cookies, alerta Telegram | 10h UTC seg |

### Exemplos

```bash
# Atualizar transcripts (incremental — usa .progress_api.json)
python download_via_api.py --max 500 --delay 2 --output ./transcripts

# Dry run de timestamps
python batch_process_videos.py --max-videos 5

# Listar pendentes do nível 2
python estudos_avancados_processor.py --list-pending

# Processar um vídeo específico (level 2)
python estudos_avancados_processor.py VIDEO_ID --dry-run
```

---

## Cookies (fallback opcional)

Após o commit `15a042a`, cookies **não são mais necessários** — Captions API cobre todos os vídeos do canal (incluindo members-only). Cookies ficaram como tier 3 do fallback, útil para troubleshoot.

### Quando você precisa de cookies

- Captions API revogada/quebrada (raro)
- Vídeos de outros canais que você é membro (não é seu canal)

### Exportar cookies

1. Instalar extensão **"Get cookies.txt LOCALLY"** (Chrome/Edge/Firefox)
2. Abrir youtube.com logado, exportar para `youtube_cookies.txt` no root do projeto
3. Atualizar VPS (se quiser fallback ativo lá): `cat youtube_cookies.txt | base64` → `YOUTUBE_COOKIES` no Dokploy

### Detecção automática (`get_default_cookies()` em `transcript_processor.py`)

- **Local**: usa `chrome` direto (extração via yt-dlp)
- **Docker**: usa `youtube_cookies.txt` (decodado do env var no entrypoint)

> **Importante**: feche o Chrome antes de rodar yt-dlp localmente — o browser segura o cookie DB.

---

## Troubleshooting

### Captions API revogada / token expirado
```bash
rm token_captions.pickle
python download_via_api.py --max 1   # refaz OAuth
```

### "No module named 'youtube_transcript_api'"
```bash
pip install -r requirements_local.txt
```

### keyring crash
Acontece em ambientes sem keychain (Docker, headless). Os scripts caem em fallback para env var. Em Docker, sempre usar `DEEPSEEK_API_KEY` direto.

### Vídeo com ID começando com hífen
```bash
python download_via_api.py -- -YMooVl3oms   # usar `--` separator
```

### Testar cookies (tier 3) manualmente
```bash
python test_cookies.py VIDEO_ID youtube_cookies.txt
```

### Health check completo
```bash
python check_auth_health.py    # testa Captions API + cookies, manda Telegram
```

---

## 📁 Arquivos sensíveis (todos gitignored)

```
client_secrets.json          # OAuth credentials do Google
token*.pickle                # Tokens OAuth (renovam sozinhos)
youtube_cookies*.txt         # Cookies (fallback opcional)
.env                         # Env vars locais
```

Para detalhes completos de arquitetura/deploy, ver `CLAUDE.md`.
