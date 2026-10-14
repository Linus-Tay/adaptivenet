"""Read-only, loopback-only HTTP API for UI, model, and controller teams."""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import html
import json
from .store import Store

HTML = """<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>AdaptiveNet | Ingestion Core</title><style>
:root{color-scheme:dark;font:15px system-ui;background:#080e19;color:#e5efff}body{max-width:1180px;margin:auto;padding:30px}h1{font-size:32px;letter-spacing:-.04em}p{color:#a6b7d2}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}article{background:#101f34;border:1px solid #2b4566;border-radius:16px;padding:20px}.metric{font-size:25px;font-weight:700}.muted{color:#94a9c6}.good{color:#6dd8b2}.bad{color:#ffadad}code{color:#a7cdfb}table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #2b4566;padding:7px;text-align:left}pre{overflow:auto;background:#111d2f;padding:18px;border-radius:12px}a{color:#a7cdfb}@media(max-width:650px){.grid{grid-template-columns:1fr}}</style>
<h1>AdaptiveNet <span class=muted>Ingestion Core</span></h1><p>Read-only feature and quality view · No AWS actions or route changes · Data timestamp required for safety</p><div id=status>Loading most recent persisted snapshot…</div><div id=regions class=grid></div><h2>CloudFront (global CDN)</h2><div id=cloudfront></div><h2>Data handoff</h2><p><a href=/v1/features/latest>Model-ready JSON contract</a> · <a href=/v1/features/history?limit=10>Historical feature windows</a> · <a href=/v1/ingestion/status>Source diagnostics</a></p><h2>Comparison</h2><pre id=comp></pre><script>
const esc=x=>String(x??'—').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
function number(v,s=''){return v===null||v===undefined?'Unavailable':esc(Math.round(v*100)/100)+s}
async function update(){try{let r=await fetch('/v1/features/latest',{cache:'no-store'}); if(!r.ok)throw Error('No snapshots');let d=await r.json();document.querySelector('#status').textContent='Snapshot '+d.computed_at+' · Model input: '+(d.ready_for_model?'READY':'NOT READY')+' · Internal-only baseline: '+(d.ready_for_internal_only_model?'READY':'NOT READY')+' · Automation: OFF';document.querySelector('#regions').innerHTML=Object.entries(d.regions).map(([n,v])=>'<article><h2>'+esc(n)+'</h2><p>Internal: '+esc(v.quality.internal)+' · External: '+esc(v.quality.external)+'</p><div class=metric>'+number(v.features.network_rtt_p50_ms,' ms')+'</div><p>Median RIPE ping RTT</p><div class=metric>'+number(v.features.packet_loss_pct,'%')+'</div><p>Probe packet loss · '+esc(v.features.ripe_probes_reporting)+' probes</p><div class=metric>'+number(v.features.cpu_busy_pct,'%')+'</div><p>CPU busy</p><div class=metric>'+number(v.features.memory_available_pct,'%')+'</div><p>Memory available</p><div class=metric>'+number(v.features.http_p95_ms,' ms')+'</div><p>HTTP catalog p95</p><h3>All regional features</h3><table><tbody>'+Object.entries(v.features).map(([k,val])=>'<tr><th>'+esc(k)+'</th><td>'+number(val, (k.endsWith('_pct')?'%':k.endsWith('_ms')?' ms':''))+'</td></tr>').join('')+'</tbody></table></article>').join('');document.querySelector('#comp').textContent=JSON.stringify(d.comparison,null,2);let cf=d.cloudfront||{};let f=cf.features||{};let labels={cloudfront_requests:'Requests per minute (latest minute)',cloudfront_4xx_pct:'Viewer 4xx rate',cloudfront_5xx_pct:'Viewer 5xx rate',cloudfront_total_error_pct:'Viewer total error rate',cloudfront_bytes_downloaded:'Bytes downloaded (latest minute)',cloudfront_bytes_uploaded:'Bytes uploaded (latest minute)'};let u={cloudfront_4xx_pct:'%',cloudfront_5xx_pct:'%',cloudfront_total_error_pct:'%'};document.querySelector('#cloudfront').innerHTML='<article><p>Status: '+esc(cf.status||'not_collected')+' · Distribution: '+esc(cf.distribution_id||'Not configured')+' · CloudWatch: us-east-1 / Global</p><table><tbody>'+Object.keys(labels).map(k=>'<tr><th>'+esc(labels[k])+'</th><td class=metric>'+number(f[k],u[k]||'')+'</td></tr>').join('')+'</tbody></table><p>Missing values stay unavailable; this data is global CDN telemetry, not a regional latency measurement.</p></article>'}catch(e){document.querySelector('#status').textContent=e.message}}
update();setInterval(update,30000)</script></html>"""


def serve(db_path: str, *, port: int = 8766):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            route = parsed.path
            store = Store(db_path)
            if route == "/":
                self.respond(200, HTML.encode(), "text/html; charset=utf-8"); return
            if route == "/healthz":
                data = {"status":"ok", "service":"adaptivenet-ingestion-api", "mode":"read_only"}
            elif route == "/v1/features/latest":
                data = store.latest_snapshot()
            elif route == "/v1/features/history":
                limit_raw = parse_qs(parsed.query).get("limit", ["50"])[0]
                try: limit = min(500,max(1,int(limit_raw)))
                except ValueError:
                    self.respond(400, b'{"error":"invalid limit"}'); return
                data = {"snapshots":store.recent_snapshots(limit)}
            elif route == "/v1/ingestion/status":
                data = {"latest_run":store.latest_run(),"latest_snapshot": (store.latest_snapshot() or {}).get("snapshot_id"),"automation_enabled":False}
            else:
                self.respond(404,b'{"error":"not found"}'); return
            if data is None:
                self.respond(404,b'{"error":"no snapshot yet"}'); return
            self.respond(200,json.dumps(data,allow_nan=False).encode())

        def respond(self,status,data,kind="application/json"):
            self.send_response(status)
            self.send_header("Content-Type",kind)
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Content-Security-Policy","default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            self.send_header("Content-Length",str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    # Intentionally never bind 0.0.0.0; dashboard/backend can reverse-proxy with auth later.
    server = ThreadingHTTPServer(("127.0.0.1",port),Handler)
    print(f"AdaptiveNet ingestion read-only API: http://127.0.0.1:{port}",flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
