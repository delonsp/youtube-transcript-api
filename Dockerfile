FROM python:3.12-slim

WORKDIR /app

# Instala cron, Node.js (necessario para yt-dlp) e utilitarios
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    cron \
    nodejs \
    npm \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dependencias Python
COPY requirements_local.txt .
RUN pip install --no-cache-dir -r requirements_local.txt

# Copiar biblioteca e scripts
COPY youtube_transcript_api/ ./youtube_transcript_api/
COPY transcript_processor.py .
COPY batch_process_videos.py .
COPY fill_doc_summaries.py .
COPY estudos_avancados_processor.py .
COPY download_via_api.py .
COPY run_estudos_avancados.py .
COPY google_docs_manager.py .

# Crontab e entrypoint
COPY crontab.txt .
COPY entrypoint-cron.sh .
RUN chmod +x entrypoint-cron.sh

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

CMD ["/app/entrypoint-cron.sh"]
