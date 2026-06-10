#!/usr/bin/env python3
"""Generate a self-contained HTML dashboard (data baked in) from metrics.db.
Outputs to a staging dir for publishing to here.now. Snapshot, not live —
re-run to refresh."""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

DB = 'metrics/metrics.db'
OUT = sys.argv[1] if len(sys.argv) > 1 else '/tmp/yt-dashboard/index.html'

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# Reference day = latest consolidated day
ref_day = conn.execute(
    'SELECT MAX(date) FROM channel_daily WHERE consolidated = 1'
).fetchone()[0]
ref_date = datetime.strptime(ref_day, '%Y-%m-%d').date()

# Daily series (last 90 days up to ref_day)
start90 = (ref_date - timedelta(days=89)).isoformat()
daily = [dict(r) for r in conn.execute(
    'SELECT date, views, watch_minutes, subs_gained, subs_lost, avg_view_percentage '
    'FROM channel_daily WHERE date BETWEEN ? AND ? ORDER BY date', (start90, ref_day))]
series = [{
    'date': r['date'],
    'views': r['views'] or 0,
    'net_subs': (r['subs_gained'] or 0) - (r['subs_lost'] or 0),
    'watch_hours': round((r['watch_minutes'] or 0) / 60),
    'retention': round(r['avg_view_percentage'] or 0, 1),
} for r in daily]

# 7-day rollup + WoW
def window_sum(start, end):
    rows = conn.execute(
        'SELECT views, watch_minutes, subs_gained, subs_lost FROM channel_daily '
        'WHERE date BETWEEN ? AND ?', (start, end)).fetchall()
    return {
        'views': sum(r[0] or 0 for r in rows),
        'watch_hours': round(sum(r[1] or 0 for r in rows) / 60),
        'net_subs': sum((r[2] or 0) - (r[3] or 0) for r in rows),
    }

wk = window_sum((ref_date - timedelta(days=6)).isoformat(), ref_day)
prev = window_sum((ref_date - timedelta(days=13)).isoformat(),
                  (ref_date - timedelta(days=7)).isoformat())

def wow(cur, old):
    return round((cur / old - 1) * 100) if old else None

rollup = {
    'views': wk['views'], 'views_wow': wow(wk['views'], prev['views']),
    'watch_hours': wk['watch_hours'], 'watch_wow': wow(wk['watch_hours'], prev['watch_hours']),
    'net_subs': wk['net_subs'], 'net_wow': wow(wk['net_subs'], prev['net_subs']),
}

# Snapshot
snap = conn.execute(
    'SELECT subscriber_count, total_views FROM channel_snapshot ORDER BY date DESC LIMIT 1'
).fetchone()
snapshot = {'subs': snap[0], 'total_views': snap[1]} if snap else {}

# Top videos of the latest window
vw_period = conn.execute('SELECT MAX(period_end) FROM video_window').fetchone()[0]
videos = []
for r in conn.execute(
    'SELECT title, video_id, views, watch_minutes, subs_gained, subs_lost, avg_view_percentage '
    'FROM video_window WHERE period_end = ? ORDER BY views DESC LIMIT 8', (vw_period,)):
    views = r['views'] or 0
    net = (r['subs_gained'] or 0) - (r['subs_lost'] or 0)
    videos.append({
        'title': r['title'] or r['video_id'],
        'views': views, 'net_subs': net,
        'conv': round(net / views * 100, 2) if views else 0,
        'retention': round(r['avg_view_percentage'] or 0),
    })

data = {
    'ref_day': ref_day,
    'generated': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
    'series': series, 'rollup': rollup, 'snapshot': snapshot,
    'videos': videos, 'vw_period': vw_period,
}

HTML = """<!doctype html>
<html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Métricas — Canal Dr. Alain</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#0f1115;--card:#181b22;--line:#cc0000;--txt:#e6e8eb;--muted:#9aa0a8;--up:#34d399;--down:#f87171}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;padding:16px;max-width:1000px;margin:0 auto}
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
</style></head><body>
<h1>📊 Canal Dr. Alain — métricas</h1>
<div class="sub" id="sub"></div>
<div class="kpis" id="kpis"></div>
<div class="card"><h2>Views diárias (90 dias)</h2><canvas id="viewsChart"></canvas></div>
<div class="card"><h2>Inscritos líquidos por dia (90 dias)</h2><canvas id="subsChart"></canvas></div>
<div class="card"><h2 id="vtitle">Top vídeos</h2><div style="overflow-x:auto"><table id="vtable"></table></div></div>
<div class="sub">Snapshot gerado de <span id="gen"></span> · dado de referência D-3. Atualizado a cada regeração.</div>
<script>
const D = __DATA__;
const fmt = n => new Intl.NumberFormat('pt-BR').format(Math.round(n));
const sign = n => (n>=0?'+':'')+fmt(n);
const dl = s => s.slice(8,10)+'/'+s.slice(5,7);

document.getElementById('sub').textContent = 'Referência: '+dl(D.ref_day)+' · '+fmt(D.snapshot.subs||0)+' inscritos';
document.getElementById('gen').textContent = D.generated;
document.getElementById('vtitle').textContent = 'Top vídeos (semana até '+dl(D.vw_period)+')';

function delta(v){ if(v==null) return ''; const c=v>=0?'up':'down'; return `<div class="delta ${c}">${v>=0?'▲':'▼'} ${Math.abs(v)}% vs sem. ant.</div>`; }
const R=D.rollup;
document.getElementById('kpis').innerHTML = [
  ['Inscritos (total)', fmt(D.snapshot.subs||0), ''],
  ['Views (7d)', fmt(R.views), delta(R.views_wow)],
  ['Inscritos líq. (7d)', sign(R.net_subs), delta(R.net_wow)],
  ['Horas assistidas (7d)', fmt(R.watch_hours)+'h', delta(R.watch_wow)],
].map(([l,v,d])=>`<div class="kpi"><div class="label">${l}</div><div class="val">${v}</div>${d}</div>`).join('');

const labels = D.series.map(p=>dl(p.date));
const mk=(id,label,data,color,type='line')=>new Chart(document.getElementById(id),{
  type, data:{labels,datasets:[{label,data,borderColor:color,backgroundColor:color+'22',
    fill:type==='line',tension:.3,pointRadius:0,borderWidth:2}]},
  options:{plugins:{legend:{display:false}},scales:{
    x:{ticks:{color:'#9aa0a8',maxTicksLimit:8},grid:{display:false}},
    y:{ticks:{color:'#9aa0a8'},grid:{color:'#242832'}}}}});
mk('viewsChart','Views',D.series.map(p=>p.views),'#cc0000');
mk('subsChart','Net subs',D.series.map(p=>p.net_subs),'#34d399','bar');

const rows = D.videos.map((v,i)=>{
  const ret = v.retention>=50?`<span class="badge hot">${v.retention}% 🔥</span>`
    : v.retention<25?`<span class="badge warn">${v.retention}% ⚠️</span>`:v.retention+'%';
  return `<tr><td>${i+1}</td><td class="vtitle" title="${v.title.replace(/"/g,'&quot;')}">${v.title}</td>
    <td class="num">${fmt(v.views)}</td><td class="num">${sign(v.net_subs)}</td>
    <td class="num">${v.conv}%</td><td class="num">${ret}</td></tr>`;}).join('');
document.getElementById('vtable').innerHTML =
  '<tr><th>#</th><th>Vídeo</th><th class="num">Views</th><th class="num">Inscr</th><th class="num">Conv</th><th class="num">Ret</th></tr>'+rows;
</script></body></html>"""

import os
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, 'w') as f:
    f.write(HTML.replace('__DATA__', json.dumps(data, ensure_ascii=False)))
print(f'Dashboard escrito em {OUT} ({len(series)} dias, {len(videos)} vídeos, ref {ref_day})')
