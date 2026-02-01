#!/bin/bash
set -e

echo "=== Entrypoint cron container ==="

# Decodificar secrets base64 para arquivos
if [ -n "$CLIENT_SECRETS_B64" ]; then
    echo "$CLIENT_SECRETS_B64" | base64 -d > /app/client_secrets.json
    echo "OK: client_secrets.json"
fi

if [ -n "$TOKEN_PICKLE_B64" ]; then
    echo "$TOKEN_PICKLE_B64" | base64 -d > /app/token.pickle
    echo "OK: token.pickle"
fi

if [ -n "$TOKEN_CAPTIONS_B64" ]; then
    echo "$TOKEN_CAPTIONS_B64" | base64 -d > /app/token_captions.pickle
    echo "OK: token_captions.pickle"
fi

if [ -n "$TOKEN_DOCS_B64" ]; then
    echo "$TOKEN_DOCS_B64" | base64 -d > /app/token_docs.pickle
    echo "OK: token_docs.pickle"
fi

if [ -n "$TOKEN_ESTUDOS_B64" ]; then
    echo "$TOKEN_ESTUDOS_B64" | base64 -d > /app/token_estudos_avancados.pickle
    echo "OK: token_estudos_avancados.pickle"
fi

if [ -n "$YOUTUBE_COOKIES" ]; then
    echo "$YOUTUBE_COOKIES" | base64 -d > /app/youtube_cookies.txt
    echo "OK: youtube_cookies.txt"
fi

# Cron nao herda env vars - exportar para arquivo que o cron le
printenv | grep -E '^(DEEPSEEK_API_KEY|ANTHROPIC_API_KEY|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|PYTHONPATH|PATH|HOME)=' > /app/.env.cron
echo "OK: env vars exportadas para /app/.env.cron"

# Prefixar cada job do crontab com source do .env
# Criar crontab final com env vars injetadas
{
    echo "SHELL=/bin/bash"
    echo "BASH_ENV=/app/.env.cron"
    echo ""
    # Cada linha do crontab: injetar source do .env antes do comando
    while IFS= read -r line; do
        # Pular comentarios e linhas vazias
        if [[ "$line" =~ ^# ]] || [[ -z "$line" ]]; then
            echo "$line"
        else
            # Extrair schedule (5 campos) e comando
            schedule=$(echo "$line" | awk '{print $1, $2, $3, $4, $5}')
            command=$(echo "$line" | awk '{for(i=6;i<=NF;i++) printf "%s ", $i; print ""}')
            echo "$schedule . /app/.env.cron && $command"
        fi
    done < /app/crontab.txt
} > /tmp/crontab.final

crontab /tmp/crontab.final
echo "OK: crontab instalado"
crontab -l

# Criar log file
touch /var/log/cron.log

echo "=== Iniciando cron ==="
# Rodar cron em foreground e tail do log
cron && tail -f /var/log/cron.log
