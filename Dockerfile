# Usa imagem Python slim para reduzir tamanho
FROM python:3.12-slim

# Define diretório de trabalho
WORKDIR /app

# Copia arquivos de dependências
COPY requirements.txt .

# Instala dependências
RUN pip install --no-cache-dir -r requirements.txt

# Copia a biblioteca youtube-transcript-api
COPY youtube_transcript_api/ ./youtube_transcript_api/

# Copia a API
COPY api/ ./api/

# Expõe porta 8000
EXPOSE 8000

# Define variável de ambiente para Python não criar arquivos .pyc
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/health')" || exit 1

# Comando para iniciar a aplicação
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
