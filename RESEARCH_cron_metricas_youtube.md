# Deep Research — Cronjob de métricas do canal YouTube

> Gerado em 2026-06-07 por workflow multi-agente (4 pesquisadores + 3 verificadores
> adversariais contra docs oficiais do Google + 1 crítico de completude; ~576k tokens).
> Status do pré-requisito: **YouTube Analytics API já habilitada** no projeto
> `meu-n8n-475803` (feito pelo Dr. Alain em 2026-06-07).

## TL;DR

A infraestrutura existente cobre **~80% do necessário**. Falta essencialmente:
**1 token OAuth novo** (`token_analytics.pickle`, scopes de Analytics que nenhum dos 6
pickles atuais tem), **1 script novo** (`channel_metrics_report.py`, ~250 linhas, zero
dependências novas), **1 linha no crontab**, **1 env var no Dokploy** e pequenos ajustes
em `entrypoint-cron.sh` / `docker-compose.yml` / `Dockerfile`. Esforço estimado: 1-2 dias.

**Decisão bloqueante antes de gerar o token**: incluir receita ou não — scopes não podem
ser adicionados a um refresh token já emitido.

---

## 1. O que já existe (reutilizável)

| Item | Onde | Observação |
|---|---|---|
| Libs Google | `requirements_local.txt:7-8` | `google-api-python-client` + `google-auth-oauthlib` — a Analytics API v2 usa o mesmo client (`build('youtubeAnalytics', 'v2', credentials=...)`). **Zero deps novas.** |
| Padrão OAuth pickle | `download_via_api.py:23-44` | Template canônico: load pickle → refresh → `InstalledAppFlow` + `run_local_server` → save. Copiar para o novo token. |
| Helper Telegram | `check_auth_health.py:22-35` | `send_telegram()` em stdlib pura, `parse_mode='HTML'`. Importável, mas as env vars são lidas a nível de módulo com `os.environ[]` (crasha se ausentes) — extrair para `telegram_utils.py` ou copiar as 14 linhas. |
| Secrets via base64 | `entrypoint-cron.sh:7-36` | Padrão `TOKEN_*_B64` → decode no boot. Adicionar bloco para `TOKEN_ANALYTICS_B64`. |
| Volume persistente | `docker-compose.yml:15-19` | Padrão do volume `transcripts` — replicar como `metrics:` para o SQLite. |
| Token Data API | `token_captions.pickle` | Scopes `youtube.force-ssl` + `youtube.readonly` cobrem **todos os reads da Data API** incl. `mine=true`, dislikes do dono e stats de vídeos members-only. |
| `.gitignore` | linha 26 | `*.pickle` já coberto — novo token não vaza para o fork público. |

**CHANNEL_ID**: `UCRLKi_9gQNthwlKi70qi54g` (Dr. Alain Dutra). Não está hardcoded — os
scripts derivam via `channels().list(mine=True)`. Na Analytics API usar `ids=channel==MINE`.

**Tokens existentes e scopes** (todos do mesmo client OAuth de `meu-n8n-475803`, tipo `installed`):

| Pickle | Scopes | Deployado |
|---|---|---|
| `token.pickle` | youtube.force-ssl | sim |
| `token_captions.pickle` | youtube.force-ssl + youtube.readonly | sim |
| `token_docs.pickle` | documents + youtube.force-ssl | sim |
| `token_estudos_avancados.pickle` | documents + youtube.force-ssl | sim |
| `token_bulk.pickle` | youtube.readonly | **órfão** (housekeeping pendente) |
| `token_weekly.pickle` | youtube.force-ssl + documents | **órfão** |

➡️ **Nenhum tem `yt-analytics.readonly` nem `yt-analytics-monetary.readonly`** → novo token obrigatório.

## 2. Papel de cada API

| | YouTube Data API v3 | YouTube Analytics API v2 | YouTube Reporting API v1 |
|---|---|---|---|
| Natureza | Contadores públicos near-realtime | Métricas privadas do dono, query síncrona JSON | Bulk reports (jobs + CSV diário) |
| Delay | ~nenhum | ~48-72h (dia fecha em horário do Pacífico) | Report do dia X pronto em ~48h |
| Fornece | subscriberCount (**arredondado**), viewCount, likes/comments por vídeo | views, engagedViews, watch time, avg duration, subsGained/Lost (**exatos**), likes/comments/shares, receita | Tudo da Analytics + **impressões de thumbnail e CTR** (reach reports, desde 15/jan/2026) |
| Auth | `token_captions.pickle` atual serve | **novo** `token_analytics.pickle` | mesmo scope da Analytics |
| Custo p/ este cron | ~3-5 units/dia (quota 10k) | 3-4 queries/dia (quota separada, não publicada — risco nulo) | jobs.create 1x + download diário |

## 3. Fatos críticos verificados (com fonte oficial)

1. **Impressões de thumbnail e CTR NÃO existem na Analytics API** (targeted queries).
   Desde 15/jan/2026 existem **apenas na Reporting API** — reports `channel_reach_basic_a1`
   / `channel_reach_combined_a1`, colunas `video_thumbnail_impressions` e
   `video_thumbnail_impressions_ctr`. Colocá-las numa query da Analytics API → **HTTP 400**
   na request inteira. _(developers.google.com/youtube/analytics/revision_history)_
2. **`subscriberCount` da Data API é arredondado PARA BAIXO a 3 algarismos significativos
   mesmo para o dono autenticado** (desde set/2019; exato só ≤1000 inscritos). Deltas
   precisos vêm de `subscribersGained`/`subscribersLost` da Analytics API.
   _(developers.google.com/youtube/v3/docs/channels)_
3. **Scopes exatos**: `https://www.googleapis.com/auth/yt-analytics.readonly` e
   `.../yt-analytics-monetary.readonly` (receita). **Não dá para adicionar scope a um
   refresh token existente** — decidir sobre receita ANTES de gerar o token.
4. **Delay de dados**: oficial só diz "a few days" (~2 dias para earnings; traffic sources
   48-72h). Reportar **D-3** e regravar os últimos 7 dias a cada run (a API trunca
   `endDate` **silenciosamente** quando os dados não estão prontos — detectar rows < dias pedidos).
5. **Receita de memberships/Super Chat NÃO está na API** — `estimatedRevenue` = ads +
   YouTube Premium apenas. Para um canal que vive de members-only, o número seria
   parcial/enganoso. Breakdown de membros: só no Studio. Receita tem ajuste de fim de mês.
6. **OAuth consent em "Testing" mata refresh tokens em 7 dias**; "In production" remove o
   limite. Indício forte de que já está em production (os 4 tokens atuais renovam há meses),
   mas confirmar no Console antes de gerar o 5º. _(support.google.com/cloud/answer/15549945)_
7. **Shorts**: mudança de metodologia de views em 31/03/2025 criou `engagedViews`
   (metodologia antiga). Coletar **ambas** desde o dia 1 para não quebrar séries.
8. **Quota jun/2026 (Data API)**: `search.list` virou bucket separado de 100 calls/dia
   (nunca usar; uploads via playlist `UU...` = 1 unit). Novo `videos.batchGetStats`
   (1 unit, bucket próprio de 10k/dia) — ideal para polling de stats de vídeos.
9. **Dimensões temporais da Analytics API**: só `day`/`month` — sem granularidade horária
   nem "últimas 48h" do Studio. `dislikes` ainda listada para o dono (confirmar empiricamente).
10. **Members-only na dimensão `video`**: ✅ CONFIRMADO empiricamente em 2026-06-07 —
    query com `filters=video==53Ft9fLaiCE` (live members-only) retornou dados
    (13 views, 439 min). O top vídeos do digest inclui conteúdo members-only.

## 4. Arquitetura v1 recomendada

**Script único `channel_metrics_report.py`** (~250 linhas, google-api-python-client cru):

1. Analytics API `dimensions=day`, últimos 35 dias: `views, engagedViews,
   estimatedMinutesWatched, averageViewDuration, subscribersGained, subscribersLost,
   likes, comments, shares` (+ `estimatedRevenue` com `currency=BRL` **se** o scope
   monetário for incluído). **SEM métricas de impressão** (ver fato 1).
2. Analytics API `dimensions=video&sort=-views&maxResults=10` para top vídeos (mapear
   título via `videos.list` ou `channel_videos.json`).
3. Data API `channels.list(part=statistics, mine=true)` → total de inscritos (arredondado,
   exibir como "~").
4. **Storage**: SQLite em `/app/metrics/metrics.db` (volume novo `metrics:`). Tabelas
   `channel_daily` (PK date) e `video_daily` (PK date+video_id) + coluna `consolidated`
   (true quando o dia tem >72h). UPSERT dos últimos 7 dias a cada run.
5. **Digest Telegram** (reuso de `send_telegram`): views D-3 vs média 7d, watch time,
   inscritos ±/net, engajamento, top 3 vídeos, linha de anomalia condicional. **Rotular o
   dia de referência explicitamente** ("dados de qui 04/06") — todo cálculo de data em
   **um único fuso definido** (recomendado: data da dimensão `day` da API como string,
   sem conversão) para evitar off-by-one Pacific/UTC/BRT.
6. **Anomalia v1**: z-score do dia vs 28 dias anteriores, threshold |z| ≥ 2.5 + fallback
   percentual (queda >40% vs média 7d). Só sobre dias `consolidated`. Com backfill inicial,
   baseline existe desde o dia 1.
7. **Backfill**: 1º run com `startDate` de 90-365 dias atrás (1 query extra, mesma chamada).
8. **Tratamento de falha**: try/except global → alerta Telegram de **ERRO** (mensagem
   distinta do digest); tolerar response sem chave `rows`; truncar digest a <4096 chars
   (limite do Telegram).
9. **Schedule**: `0 11 * * *` (11h UTC = 8h BRT, após os jobs de 6h-10h; processamento
   diário do YouTube em PT já fechado). Opcional: resumo semanal `30 11 * * 0`.
10. **Health check**: estender `check_auth_health.py` com query barata da Analytics API
    (1 dia, `metrics=views`) no check semanal.

**v2 (não fazer agora)**: componente Reporting API para impressões/CTR (jobs + CSV),
espelho Google Sheets para gráficos, gráfico de tendência via sendPhoto, retenção por
vídeo (`audienceWatchRatio`), monitor de live (`concurrentViewers` exige polling durante
a transmissão — não cabe em cron diário).

## 5. Checklist de implementação

- [ ] Confirmar OAuth consent screen "In production" no Console (`meu-n8n-475803`)
- [x] Habilitar YouTube Analytics API no projeto ✅ (2026-06-07)
- [ ] **Decidir: receita no digest?** (define scopes do token — irreversível sem re-auth)
- [ ] Gerar `token_analytics.pickle` localmente (fluxo browser, padrão `download_via_api.py:23-44`)
- [ ] Criar `channel_metrics_report.py` (+ `telegram_utils.py` extraído)
- [ ] `Dockerfile`: `COPY channel_metrics_report.py .` (copia individual, não `COPY . .`)
- [ ] `crontab.txt`: linha `0 11 * * *`
- [ ] `entrypoint-cron.sh`: bloco decode `TOKEN_ANALYTICS_B64`
- [ ] **GOTCHA `entrypoint-cron.sh:39`**: env vars novas em texto plano usadas em runtime
      precisam entrar no regex whitelist (vars `*_B64` não — viram arquivo no boot)
- [ ] `docker-compose.yml`: env var + volume `metrics:`
- [ ] Dokploy: `TOKEN_ANALYTICS_B64` (`cat token_analytics.pickle | base64`)
- [ ] `.env` local: adicionar a var para testes com `docker compose --env-file`
- [ ] 1º run local com `--dry-run`: validar members-only na dimensão video vs Studio
- [ ] Estender `check_auth_health.py`
- [ ] Atualizar `CLAUDE.md` (diagrama de jobs, tabela de env vars, file structure)
- [ ] Housekeeping: deletar ou documentar `token_bulk.pickle` / `token_weekly.pickle`
- [ ] Confirmar persistência do volume `metrics` em redeploy do Dokploy + backup mensal do `.db`

## 6. Riscos principais (do crítico)

- **Falha silenciosa**: sem alerta de erro, o job pode quebrar por semanas despercebido → item 8 da arquitetura.
- **Off-by-one de fuso** corrompe a série histórica via UPSERT → item 5 (um fuso único, rotulagem explícita).
- **Anomalias falsas em dados parciais** → flag `consolidated` + supressão.
- **Volume zerado em redeploy** mascara perda de histórico (digest continua chegando) → confirmar persistência + backup.
- **Single point of failure na conta Google**: evento de segurança revoga os 5+ tokens de uma vez — documentar runbook de re-auth em massa.
- `/var/log/cron.log` sem rotação (6º job piora) — adicionar logrotate ou truncamento.

## 7. Decisões do Dr. Alain — TOMADAS em 2026-06-07

1. **Receita: NÃO** → token apenas com `yt-analytics.readonly` (sem scope monetário).
   Questão do chat privado do Telegram fica irrelevante.
2. **Impressões/CTR de thumbnail: v2** → v1 sem componente Reporting API.
3. Digest **diário às 11h UTC (8h BRT)**, dia de referência D-3.
4. **Anomalia: SIM na v1** (z-score ≥2.5 vs 28d + queda >40% vs média 7d, só dias consolidados).
5. **Backfill inicial: 365 dias.**
