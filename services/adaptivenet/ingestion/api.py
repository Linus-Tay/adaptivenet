"""Read-only, loopback-only HTTP API for UI, model, and controller teams."""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
import json
from .store import Store

# Bundled, self-contained dashboard: no external JavaScript or CSS dependencies.
from importlib.resources import files

HTML = files("adaptivenet.ingestion").joinpath("dashboard.html").read_text(encoding="utf-8")


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
