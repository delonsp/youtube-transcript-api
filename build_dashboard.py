#!/usr/bin/env python3
"""Generate a self-contained HTML dashboard (data baked in) from metrics.db
plus live Analytics API queries for multi-period top videos, and DeepSeek
suggestions of what to publish next based on the top 20 videos of 90 days.

Snapshot, not live — re-run and republish to refresh. Both the API fetch and
the AI section are fail-soft: without token/key the dashboard still builds
with whatever is available.

Usage:
    python build_dashboard.py [output.html] [--no-ai] [--publish]

--publish pushes the generated HTML to the password-protected here.now site
(env HERENOW_API_KEY required; slug via HERENOW_DASHBOARD_SLUG). Republishing
keeps the site password. Used by the daily cron at 11h30 UTC.
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DB = 'metrics/metrics.db'
PERIODS = [('7d', 7), ('28d', 28), ('90d', 90)]
AI_TOP_N = 20
DEEPSEEK_MODEL = 'deepseek-chat'

DASHBOARD_SLUG = os.environ.get('HERENOW_DASHBOARD_SLUG', 'polite-riddle-javf')

OUT = '/tmp/yt-dashboard/index.html'
NO_AI = '--no-ai' in sys.argv
PUBLISH = '--publish' in sys.argv
for a in sys.argv[1:]:
    if not a.startswith('--'):
        OUT = a


def publish_herenow(html_path):
    """Publish the dashboard HTML to here.now (create/update flow:
    PUT manifest -> upload presigned -> finalize). Password set on the site
    persists across republishes. Raises on failure."""
    import hashlib
    import requests

    api_key = os.environ.get('HERENOW_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('HERENOW_API_KEY not set')

    content = open(html_path, 'rb').read()
    headers = {'authorization': f'Bearer {api_key}',
               'x-herenow-client': 'channel-metrics/build-dashboard'}
    body = {
        'files': [{'path': 'index.html', 'size': len(content),
                   'contentType': 'text/html',
                   'hash': hashlib.sha256(content).hexdigest()}],
        'viewer': {'title': 'Métricas do Canal — Dr. Alain',
                   'description': 'Dashboard privado de métricas do YouTube'},
    }
    r = requests.put(f'https://here.now/api/v1/publish/{DASHBOARD_SLUG}',
                     headers=headers, json=body, timeout=30)
    r.raise_for_status()
    resp = r.json()
    if resp.get('error'):
        raise RuntimeError(f"here.now: {resp['error']}")

    for up in resp['upload'].get('uploads', []):
        put_headers = {}
        ct = (up.get('headers') or {}).get('Content-Type')
        if ct:
            put_headers['Content-Type'] = ct
        u = requests.put(up['url'], data=content, headers=put_headers, timeout=60)
        u.raise_for_status()

    f = requests.post(resp['upload']['finalizeUrl'], headers=headers,
                      json={'versionId': resp['upload']['versionId']}, timeout=30)
    f.raise_for_status()
    logger.info(f"Publicado: {resp.get('siteUrl', DASHBOARD_SLUG)}")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

ref_day = conn.execute(
    'SELECT MAX(date) FROM channel_daily WHERE consolidated = 1'
).fetchone()[0]
ref_date = datetime.strptime(ref_day, '%Y-%m-%d').date()

# ---------------- channel series (90d) + rollup -----------------------------

start90 = (ref_date - timedelta(days=89)).isoformat()
series = [{
    'date': r['date'],
    'views': r['views'] or 0,
    'net_subs': (r['subs_gained'] or 0) - (r['subs_lost'] or 0),
    'watch_hours': round((r['watch_minutes'] or 0) / 60),
} for r in conn.execute(
    'SELECT date, views, watch_minutes, subs_gained, subs_lost '
    'FROM channel_daily WHERE date BETWEEN ? AND ? ORDER BY date', (start90, ref_day))]


def window_sum(start, end):
    rows = conn.execute(
        'SELECT views, watch_minutes, subs_gained, subs_lost FROM channel_daily '
        'WHERE date BETWEEN ? AND ?', (start, end)).fetchall()
    return {
        'views': sum(r[0] or 0 for r in rows),
        'watch_hours': round(sum(r[1] or 0 for r in rows) / 60),
        'net_subs': sum((r[2] or 0) - (r[3] or 0) for r in rows),
    }


def wow(cur, old):
    return round((cur / old - 1) * 100) if old else None


wk = window_sum((ref_date - timedelta(days=6)).isoformat(), ref_day)
prev = window_sum((ref_date - timedelta(days=13)).isoformat(),
                  (ref_date - timedelta(days=7)).isoformat())
rollup = {
    'views': wk['views'], 'views_wow': wow(wk['views'], prev['views']),
    'watch_hours': wk['watch_hours'], 'watch_wow': wow(wk['watch_hours'], prev['watch_hours']),
    'net_subs': wk['net_subs'], 'net_wow': wow(wk['net_subs'], prev['net_subs']),
}

snap = conn.execute(
    'SELECT subscriber_count, total_views FROM channel_snapshot ORDER BY date DESC LIMIT 1'
).fetchone()
snapshot = {'subs': snap[0], 'total_views': snap[1]} if snap else {}

# ---------------- multi-period top videos (live Analytics API) --------------

def video_row(v, titles):
    views = v['views'] or 0
    net = v['net_subs']
    return {
        'video_id': v['video_id'],
        'title': titles.get(v['video_id'], v['video_id']),
        'views': views, 'net_subs': net,
        'watch_hours': round((v['watch_minutes'] or 0) / 60),
        'conv': round(net / views * 100, 2) if views else 0,
        'retention': round(v['avg_view_pct'] or 0),
    }


def get_video_reach(conn, ref_day, days=30):
    """Per-video impressions + weighted CTR over the last N days of reach data.
    Returns {video_id: {'impressions', 'ctr'}} (empty if reach not collected)."""
    start = (datetime.strptime(ref_day, '%Y-%m-%d').date()
             - timedelta(days=days - 1)).isoformat()
    try:
        rows = conn.execute(
            'SELECT video_id, SUM(thumbnail_impressions), '
            'SUM(thumbnail_impressions * thumbnail_ctr) '
            'FROM video_reach WHERE date BETWEEN ? AND ? GROUP BY video_id',
            (start, ref_day)).fetchall()
    except sqlite3.OperationalError:
        return {}  # table absent (pre-migration DB)
    out = {}
    for vid, impr, clicks in rows:
        if impr:
            out[vid] = {'impressions': impr, 'ctr': clicks / impr}
    return out


CONV_MIN_VIEWS = 5000   # floor to keep conversion ranking statistically sane
CONV_TOP_N = 10

periods_data = {}
ai_input = []
ai_conv_input = []
try:
    from channel_metrics_report import (authenticate, fetch_top_videos_window,
                                        fetch_video_titles)
    _, analytics, youtube = authenticate()
    for label, days in PERIODS:
        start = (ref_date - timedelta(days=days - 1)).isoformat()
        vids = fetch_top_videos_window(analytics, start, ref_day,
                                       max_results=AI_TOP_N)
        titles = fetch_video_titles(youtube, conn, [v['video_id'] for v in vids])
        periods_data[label] = [video_row(v, titles) for v in vids]
        logger.info(f'Top vídeos {label}: {len(vids)}')
    ai_input = periods_data.get('90d', [])[:AI_TOP_N]

    # Top by CONVERSION over 90d (wide fetch removes the top-by-views bias)
    start90q = (ref_date - timedelta(days=89)).isoformat()
    wide = fetch_top_videos_window(analytics, start90q, ref_day, max_results=200)
    eligible = [v for v in wide if (v['views'] or 0) >= CONV_MIN_VIEWS]
    eligible.sort(key=lambda v: v['net_subs'] / v['views'], reverse=True)
    conv_ids = [v['video_id'] for v in eligible[:CONV_TOP_N]]
    conv_titles = fetch_video_titles(youtube, conn, conv_ids)
    ai_conv_input = [video_row(v, conv_titles) for v in eligible[:CONV_TOP_N]]
    logger.info(f'Top conversão (90d, ≥{CONV_MIN_VIEWS} views): {len(ai_conv_input)}')
except Exception as e:
    logger.warning(f'Analytics API indisponível ({e}); usando só o SQLite')
    vw_period = conn.execute('SELECT MAX(period_end) FROM video_window').fetchone()[0]
    rows = []
    for r in conn.execute(
        'SELECT title, video_id, views, watch_minutes, subs_gained, subs_lost, '
        'avg_view_percentage FROM video_window WHERE period_end = ? '
        'ORDER BY views DESC', (vw_period,)):
        views = r['views'] or 0
        net = (r['subs_gained'] or 0) - (r['subs_lost'] or 0)
        rows.append({'title': r['title'] or r['video_id'], 'views': views,
                     'net_subs': net,
                     'watch_hours': round((r['watch_minutes'] or 0) / 60),
                     'conv': round(net / views * 100, 2) if views else 0,
                     'retention': round(r['avg_view_percentage'] or 0)})
    periods_data['7d'] = rows
    ai_input = rows

# ---------------- AI suggestions (DeepSeek, fail-soft) ----------------------

def _ai_video_line(v, reach):
    line = (f"- {v['title']} | {v['views']} views | {v['net_subs']:+d} inscritos | "
            f"conversão {v['conv']}% | retenção {v['retention']}%")
    r = reach.get(v.get('video_id'))
    if r:
        line += (f" | CTR thumb {r['ctr'] * 100:.1f}% "
                 f"({r['impressions']:,} impressões/30d)".replace(',', '.'))
    return line


def _ai_prompt(videos, conv_videos=None, reach=None):
    reach = reach or {}
    table = '\n'.join(_ai_video_line(v, reach) for v in videos)

    conv_block = ''
    if conv_videos:
        conv_table = '\n'.join(_ai_video_line(v, reach) for v in conv_videos)
        conv_block = f"""

Top {len(conv_videos)} vídeos por CONVERSÃO de inscritos nos últimos 90 dias (mínimo 5 mil views — inclui vídeos de menor alcance que convertem muito bem; esta lista elimina o viés de olhar só os mais vistos):

{conv_table}"""

    ctr_note = ''
    if reach:
        ctr_note = ('\nCTR thumb = taxa de cliques na thumbnail quando exibida '
                    '(últimos 30 dias). CTR baixo + retenção alta = vídeo bom com '
                    'thumbnail/título fracos (retrabalhar packaging); CTR alto + '
                    'retenção baixa = promessa que o conteúdo não entrega.')

    return f"""Você é estrategista de conteúdo de um canal de saúde no YouTube (Dr. Alain Dutra, ~920 mil inscritos, público brasileiro leigo interessado em saúde preventiva).

Top {len(videos)} vídeos por VIEWS dos últimos 90 dias (views, inscritos ganhos via página do vídeo, conversão = inscritos/views, retenção = % médio assistido):

{table}{conv_block}
{ctr_note}
Analise os padrões cruzando as 4 dimensões (alcance/views, CTR de thumbnail, retenção, conversão de inscritos): temas e formatos de título que funcionam, o que converte vs o que só dá view, o que retém, e onde o packaging (thumb/título) está desperdiçando vídeo bom. Sugira 5 PRÓXIMOS VÍDEOS a publicar. Responda APENAS JSON válido, sem markdown:
{{"padroes": ["4-5 insights curtos sobre o que funciona, incluindo ao menos 1 sobre CTR/packaging se houver dados"], "sugestoes": [{{"titulo": "título pronto no estilo do canal", "tema": "tema/ângulo", "justificativa": "por que deve performar, citando os dados"}}]}}"""


def _parse_ai_json(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.split('```')[1].lstrip('json').strip()
    return json.loads(text)


def _suggestions_fable(prompt):
    """Primary: Claude Fable 5 via the Anthropic API (ANTHROPIC_API_KEY).
    Fable 5 rejects temperature/top_p and an explicit thinking=disabled —
    send neither."""
    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    response = client.messages.create(
        model='claude-fable-5',
        max_tokens=3000,
        messages=[{'role': 'user', 'content': prompt}],
    )
    text = next(b.text for b in response.content if b.type == 'text')
    return _parse_ai_json(text)


def _suggestions_deepseek(prompt):
    """Fallback: DeepSeek (project default for the other jobs)."""
    api_key = None
    try:
        import keyring
        api_key = keyring.get_password('deepseek', 'api_key')
    except Exception:
        pass
    api_key = api_key or os.getenv('DEEPSEEK_API_KEY')
    if not api_key:
        raise RuntimeError('DEEPSEEK_API_KEY ausente')

    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url='https://api.deepseek.com')
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[{'role': 'user', 'content': prompt}],
        temperature=0.7, max_tokens=2000,
    )
    return _parse_ai_json(resp.choices[0].message.content)


def ai_suggestions(videos, conv_videos=None, reach=None):
    """Fable 5 first (best strategic analysis, ~1 call/day), DeepSeek as
    fallback so the AI section survives an Anthropic outage/key issue.
    The result carries a `modelo` label shown on the dashboard."""
    prompt = _ai_prompt(videos, conv_videos, reach)
    try:
        out = _suggestions_fable(prompt)
        out['modelo'] = 'Claude Fable 5'
        logger.info('Sugestões geradas com Fable 5')
        return out
    except Exception as e:
        logger.warning(f'Fable 5 indisponível ({e}); usando DeepSeek')
        out = _suggestions_deepseek(prompt)
        out['modelo'] = 'DeepSeek (fallback)'
        logger.info('Sugestões geradas com DeepSeek (fallback)')
        return out


ai = {}
if not NO_AI and ai_input:
    try:
        video_reach = get_video_reach(conn, ref_day)
        if video_reach:
            logger.info(f'CTR por vídeo disponível para {len(video_reach)} vídeos')
        ai = ai_suggestions(ai_input, ai_conv_input, video_reach)
        logger.info(f"IA: {len(ai.get('sugestoes', []))} sugestões geradas")
    except Exception as e:
        logger.warning(f'Sugestões de IA falharam ({e}); seção omitida')

data = {
    'ref_day': ref_day,
    'generated': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
    'series': series, 'rollup': rollup, 'snapshot': snapshot,
    'periods': periods_data, 'ai': ai,
}

# ---------------- HTML ------------------------------------------------------

HTML = """<!doctype html>
<html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Métricas — Canal Dr. Alain</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#0f1115;--card:#181b22;--txt:#e6e8eb;--muted:#9aa0a8;--up:#34d399;--down:#f87171;--accent:#8b5cf6}
  *{box-sizing:border-box}
  body{margin:0 auto;background:var(--bg);color:var(--txt);font:15px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;padding:16px;max-width:1000px}
  h1{font-size:20px;margin:0 0 2px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:18px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
  .kpi{background:var(--card);border-radius:12px;padding:14px}
  .kpi .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  .kpi .val{font-size:24px;font-weight:700;margin-top:4px}
  .kpi .delta{font-size:13px;margin-top:2px}
  .up{color:var(--up)} .down{color:var(--down)}
  .card{background:var(--card);border-radius:12px;padding:16px;margin-bottom:18px}
  .card h2{font-size:15px;margin:0 0 12px;color:var(--muted);font-weight:600}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #242832}
  th{color:var(--muted);font-weight:600}
  td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
  .vtitle{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .badge{padding:1px 6px;border-radius:6px;font-size:12px}
  .hot{background:#2a1d00;color:#fbbf24} .warn{background:#2a1414;color:#f87171}
  canvas{max-height:260px}
  .tabs{display:flex;gap:8px;margin-bottom:12px}
  .tab{background:#242832;border:0;color:var(--muted);padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px}
  .tab.on{background:var(--accent);color:#fff}
  .sug{border-left:3px solid var(--accent);padding:10px 12px;margin:10px 0;background:#1d2029;border-radius:0 10px 10px 0}
  .sug .t{font-weight:700}
  .sug .j{color:var(--muted);font-size:13px;margin-top:4px}
  .pat{color:var(--muted);font-size:13px;margin:4px 0}
  .pat::before{content:"• "}
</style></head><body>
<h1>📊 Canal Dr. Alain — métricas</h1>
<div class="sub" id="sub"></div>
<div class="kpis" id="kpis"></div>
<div class="card" id="aiCard" style="display:none">
  <h2 id="aiTitle">🤖 O que publicar — sugestões da IA (base 90d: top 20 por views + top 10 por conversão + CTR de thumbnail)</h2>
  <div id="aiPats"></div><div id="aiSugs"></div>
  <div class="sub" id="aiModel" style="margin:10px 0 0"></div>
</div>
<div class="card"><h2 id="vtitle">Top vídeos</h2>
  <div class="tabs" id="tabs"></div>
  <div style="overflow-x:auto"><table id="vtable"></table></div>
</div>
<div class="card"><h2>Views diárias (90 dias)</h2><canvas id="viewsChart"></canvas></div>
<div class="card"><h2>Inscritos líquidos por dia (90 dias)</h2><canvas id="subsChart"></canvas></div>
<div class="sub">Snapshot gerado em <span id="gen"></span> · dado de referência D-3.</div>
<script>
const D = __DATA__;
const fmt = n => new Intl.NumberFormat('pt-BR').format(Math.round(n));
const sign = n => (n>=0?'+':'')+fmt(n);
const dl = s => s.slice(8,10)+'/'+s.slice(5,7);

document.getElementById('sub').textContent = 'Referência: '+dl(D.ref_day)+' · '+fmt(D.snapshot.subs||0)+' inscritos';
document.getElementById('gen').textContent = D.generated;

function delta(v){ if(v==null) return ''; const c=v>=0?'up':'down'; return `<div class="delta ${c}">${v>=0?'▲':'▼'} ${Math.abs(v)}% vs sem. ant.</div>`; }
const R=D.rollup;
document.getElementById('kpis').innerHTML = [
  ['Inscritos (total)', fmt(D.snapshot.subs||0), ''],
  ['Views (7d)', fmt(R.views), delta(R.views_wow)],
  ['Inscritos líq. (7d)', sign(R.net_subs), delta(R.net_wow)],
  ['Horas assistidas (7d)', fmt(R.watch_hours)+'h', delta(R.watch_wow)],
].map(([l,v,d])=>`<div class="kpi"><div class="label">${l}</div><div class="val">${v}</div>${d}</div>`).join('');

// IA
if (D.ai && D.ai.sugestoes && D.ai.sugestoes.length){
  document.getElementById('aiCard').style.display='block';
  document.getElementById('aiPats').innerHTML = (D.ai.padroes||[]).map(p=>`<div class="pat">${p}</div>`).join('');
  document.getElementById('aiSugs').innerHTML = D.ai.sugestoes.map(s=>
    `<div class="sug"><div class="t">${s.titulo}</div><div class="j"><b>${s.tema}</b> — ${s.justificativa}</div></div>`).join('');
  document.getElementById('aiModel').textContent = '🧠 Análise gerada por: ' + (D.ai.modelo || 'IA');
}

// top vídeos com períodos
const periods = Object.keys(D.periods);
let cur = periods[0];
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
function renderTable(){
  document.getElementById('vtitle').textContent = 'Top vídeos ('+cur+', até '+dl(D.ref_day)+')';
  const rows = D.periods[cur].map((v,i)=>{
    const ret = v.retention>=50?`<span class="badge hot">${v.retention}% 🔥</span>`
      : v.retention<25?`<span class="badge warn">${v.retention}% ⚠️</span>`:v.retention+'%';
    return `<tr><td>${i+1}</td><td class="vtitle" title="${esc(v.title)}">${esc(v.title)}</td>
      <td class="num">${fmt(v.views)}</td><td class="num">${fmt(v.watch_hours)}h</td>
      <td class="num">${sign(v.net_subs)}</td><td class="num">${v.conv}%</td><td class="num">${ret}</td></tr>`;}).join('');
  document.getElementById('vtable').innerHTML =
    '<tr><th>#</th><th>Vídeo</th><th class="num">Views</th><th class="num">Horas</th><th class="num">Inscr</th><th class="num">Conv</th><th class="num">Ret</th></tr>'+rows;
  document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('on', b.dataset.p===cur));
}
document.getElementById('tabs').innerHTML = periods.map(p=>
  `<button class="tab" data-p="${p}">${p==='7d'?'Semana':p==='28d'?'Mês':'90 dias'}</button>`).join('');
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{cur=b.dataset.p;renderTable();});
renderTable();

const labels = D.series.map(p=>dl(p.date));
const mk=(id,label,data,color,type='line')=>new Chart(document.getElementById(id),{
  type, data:{labels,datasets:[{label,data,borderColor:color,backgroundColor:color+'22',
    fill:type==='line',tension:.3,pointRadius:0,borderWidth:2}]},
  options:{plugins:{legend:{display:false}},scales:{
    x:{ticks:{color:'#9aa0a8',maxTicksLimit:8},grid:{display:false}},
    y:{ticks:{color:'#9aa0a8'},grid:{color:'#242832'}}}}});
mk('viewsChart','Views',D.series.map(p=>p.views),'#cc0000');
mk('subsChart','Net subs',D.series.map(p=>p.net_subs),'#34d399','bar');
</script></body></html>"""

os.makedirs(os.path.dirname(OUT) or '.', exist_ok=True)
with open(OUT, 'w') as f:
    f.write(HTML.replace('__DATA__', json.dumps(data, ensure_ascii=False)))
print(f'Dashboard: {OUT} | períodos: {list(periods_data)} | '
      f'IA: {len(ai.get("sugestoes", []))} sugestões | ref {ref_day}')

if PUBLISH:
    try:
        publish_herenow(OUT)
    except Exception as e:
        logger.error(f'Publicação no here.now falhou: {e}')
        try:
            from telegram_utils import send_telegram
            send_telegram('⚠️ <b>youtube-adm: refresh do dashboard FALHOU</b>\n\n'
                          f'<code>{str(e)[:300]}</code>\n'
                          'O digest não é afetado; dashboard ficou na versão anterior.')
        except Exception:
            pass
        sys.exit(1)
