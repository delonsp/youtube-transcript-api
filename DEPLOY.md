# 🚀 Deploy no Dokploy + Integração com n8n

## 📋 Pré-requisitos

- Dokploy instalado e configurado na sua VPS DigitalOcean
- Repositório Git com este código (GitHub, GitLab, etc.)
- n8n rodando (pode ser no mesmo Dokploy ou em outro lugar)

---

## 🐳 Deploy no Dokploy

### 1. Criar Aplicação no Dokploy

1. Acesse o painel do Dokploy
2. Clique em **"New Application"**
3. Configure:
   - **Name**: `youtube-transcript-api`
   - **Type**: `Docker`
   - **Source**: Conecte seu repositório Git

### 2. Configurar Build

- **Build Type**: `Dockerfile`
- **Dockerfile Path**: `./Dockerfile`
- **Context Path**: `.`

### 3. Configurar Variáveis de Ambiente

No painel do Dokploy, adicione as seguintes variáveis:

```bash
API_KEY=sua-chave-secreta-aqui-gere-uma-forte
```

💡 **Dica**: Gere uma API Key segura:
```bash
openssl rand -hex 32
```

### 4. Configurar Domínio

- **Domain**: Configure um subdomínio (ex: `transcript.seudominio.com`)
- Dokploy vai automaticamente:
  - Configurar Traefik como reverse proxy
  - Gerar certificado SSL via Let's Encrypt
  - Expor sua API com HTTPS

### 5. Deploy

Clique em **"Deploy"** e aguarde o build completar.

---

## 🧪 Testar a API

### Health Check

```bash
curl https://transcript.seudominio.com/health
```

Resposta esperada:
```json
{
  "status": "healthy"
}
```

### Testar Endpoint de Transcrição

```bash
curl -X POST https://transcript.seudominio.com/transcript \
  -H "X-API-Key: sua-chave-secreta-aqui" \
  -H "Content-Type: application/json" \
  -d '{
    "video_id": "dQw4w9WgXcQ",
    "languages": ["pt", "en"]
  }'
```

---

## 🔌 Integrar com n8n

### 1. Criar Workflow no n8n

1. Adicione um nó **HTTP Request**
2. Configure:

**Método**: `POST`

**URL**: `https://transcript.seudominio.com/transcript`

**Authentication**: `Header Auth`
- **Name**: `X-API-Key`
- **Value**: `sua-chave-secreta-aqui`

**Body**:
```json
{
  "video_id": "{{ $json.video_id }}",
  "languages": ["pt", "en"],
  "preserve_formatting": false
}
```

### 2. Exemplo de Workflow Completo

```
┌─────────────────┐
│  Manual Trigger │
│  (input: video_id)
└────────┬────────┘
         │
         v
┌────────────────────┐
│  HTTP Request      │
│  (Get Transcript)  │
└────────┬───────────┘
         │
         v
┌────────────────────┐
│  OpenAI Chat       │
│  (Analyze content) │
└────────┬───────────┘
         │
         v
┌────────────────────┐
│  Code Node         │
│  (Format timestamps)
└────────┬───────────┘
         │
         v
┌────────────────────┐
│  YouTube API       │
│  (Post comment)    │
└────────────────────┘
```

### 3. Estrutura da Resposta da API

```json
{
  "video_id": "dQw4w9WgXcQ",
  "language": "pt",
  "transcript": [
    {
      "text": "Texto do primeiro segmento",
      "start": 0.0,
      "duration": 3.5
    },
    {
      "text": "Texto do segundo segmento",
      "start": 3.5,
      "duration": 2.8
    }
  ],
  "full_text": "Texto completo da transcrição..."
}
```

### 4. Processar com IA no n8n

Use o campo `full_text` ou `transcript` dependendo da sua necessidade:

- **`full_text`**: Texto corrido, ideal para análise de conteúdo
- **`transcript`**: Array com timestamps, ideal para gerar marcações de tempo

**Exemplo de prompt para OpenAI:**

```
Analise a transcrição abaixo e identifique os principais assuntos abordados.
Para cada assunto, me diga:
1. O timestamp de início (em segundos)
2. O título do assunto
3. Uma breve descrição

Transcrição: {{ $json.full_text }}

Transcrição com timestamps: {{ $json.transcript }}

Formato de saída:
0:00 - Introdução
2:30 - Primeiro tópico
5:45 - Segundo tópico
```

---

## 📊 Monitoramento

### Logs no Dokploy

1. Acesse sua aplicação no Dokploy
2. Vá em **"Logs"**
3. Monitore requests e possíveis erros

### Health Check Automático

O Dokploy verifica automaticamente a saúde da aplicação via endpoint `/health`

---

## 🔒 Segurança

✅ **Implementado:**
- Autenticação via API Key
- HTTPS via Let's Encrypt (Dokploy)
- Health checks

⚠️ **Recomendações:**
- Mantenha a API Key segura (use secrets manager do n8n)
- Monitore uso para evitar abuse
- Configure rate limiting se necessário (pode fazer via Traefik no Dokploy)

---

## 🐛 Troubleshooting

### API não responde

1. Verifique os logs no Dokploy
2. Confirme que a porta 8000 está exposta
3. Teste o health check: `curl https://seu-dominio/health`

### "Invalid API Key"

- Verifique se a variável `API_KEY` está configurada corretamente no Dokploy
- Confirme que está enviando o header `X-API-Key` no n8n

### "No transcript found"

- Alguns vídeos não têm transcrição disponível
- Tente outros idiomas no array `languages`
- Verifique se o vídeo existe e está público

---

## 📚 Documentação da API

Acesse `https://seu-dominio/docs` para ver a documentação interativa (Swagger UI) gerada automaticamente pelo FastAPI.

---

## 🎯 Próximos Passos

1. Deploy no Dokploy ✅
2. Configurar n8n workflow
3. Integrar com OpenAI para análise
4. Usar YouTube Data API para postar comentários
5. (Opcional) Fixar comentário manualmente

---

## 💡 Dicas

- Use `preserve_formatting: true` se quiser manter quebras de linha
- O campo `languages` aceita múltiplos idiomas em ordem de preferência
- A API tenta primeiro transcrições manuais, depois automáticas
- Timestamps estão em segundos (use `Math.floor(seconds / 60)` para minutos)
