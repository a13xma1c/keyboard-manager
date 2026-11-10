"""Remote control: a small HTTP/JSON API and web panel for the running application.

Off by default. When on, it listens on ``127.0.0.1`` (this computer only) or, if
allowed, on every network interface so a phone / tablet / Stream Deck on the same
network can use it. Every request needs the access token (``Authorization: Bearer
<token>``, or ``?token=`` for the web panel); without it the answer is 401.

API (all JSON)::

    GET  /api/status                   running macro, pause state, queue, profile, layer
    GET  /api/macros                   the macros that may be run remotely
    GET  /api/history?limit=20         recent runs
    POST /api/run      {"macro": "name or id", "queue": false}
    POST /api/stop | /api/pause | /api/resume | /api/toggle-pause
    POST /api/profile  {"name": "Work"}          ("" = All profiles)
    POST /api/layer    {"name": "nav", "one_shot": false}
    POST /api/sync                     sync now (folder and team libraries)
    GET  /                             the web panel (needs ?token=...)

Runs started remotely count as triggers ("remote"): cooldowns and daily limits
apply, disabled macros and macros outside the active profile are refused, and the
"remote macros" setting can restrict remote runs to favorites.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:  # pragma: no cover
    from keyboardmanager.core.service import MacroService

ENABLED_SETTING = "remote_enabled"
PORT_SETTING = "remote_port"
LAN_SETTING = "remote_lan"
TOKEN_SETTING = "remote_token"
SCOPE_SETTING = "remote_scope"   # "all" or "favorites"
DEFAULT_PORT = 8765
MAX_BODY = 64 * 1024
SCOPES = ("all", "favorites")


def new_token() -> str:
    return secrets.token_urlsafe(18)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class RemoteApi:
    """What the API can do, without HTTP (the server maps requests onto it)."""

    def __init__(self, service: "MacroService") -> None:
        self.service = service

    # ------------------------------------------------------------ reading
    def status(self) -> dict:
        from keyboardmanager import __version__

        svc = self.service
        running = svc.running_macro
        return {
            "app": "Keyboard Manager", "version": __version__,
            "running": running.name if running else None,
            "paused": svc.paused,
            "queue": [name for name, _ in svc.queue_snapshot()],
            "profile": svc.active_profile, "layer": svc.active_layer,
            "profiles": svc.all_profiles(),
            "hotkeys_enabled": svc.hotkeys_enabled, "autorun_enabled": svc.autorun_enabled,
        }

    def macros(self) -> list[dict]:
        out = []
        for m in self.service.db.list_macros():
            if not self._allowed(m):
                continue
            out.append({"id": m.id, "name": m.name, "hotkey": m.hotkey, "folder": m.folder,
                        "favorite": m.favorite, "enabled": m.enabled,
                        "active": self.service.macro_active(m), "description": m.description})
        out.sort(key=lambda d: (not d["favorite"], d["name"].lower()))
        return out

    def history(self, limit: int = 20) -> list[dict]:
        limit = max(1, min(200, limit))
        return [{"macro": h.macro_name, "trigger": h.trigger, "status": h.status,
                 "started_at": h.started_at, "duration_ms": h.duration_ms, "message": h.message}
                for h in self.service.db.list_history()[:limit]]

    # ------------------------------------------------------------ acting
    def run(self, ident: Any, queue: bool = False) -> dict:
        macro = self._find(ident)
        if not macro.enabled:
            raise ApiError(409, f'"{macro.name}" is disabled')
        if not self.service.macro_active(macro):
            raise ApiError(409, f'"{macro.name}" is not in the active profile')
        busy = self.service.running_macro is not None
        if not self.service.run(macro, trigger="remote", queue=bool(queue)):
            raise ApiError(409, "another macro is running (send \"queue\": true to queue it)")
        return {"ok": True, "macro": macro.name, "queued": busy}

    def control(self, what: str) -> dict:
        svc = self.service
        if what == "stop":
            svc.stop()
            done = True
        elif what == "pause":
            done = svc.pause()
        elif what == "resume":
            done = svc.resume()
        else:
            svc.toggle_pause()
            done = True
        return {"ok": True, "changed": bool(done), "paused": svc.paused}

    def profile(self, name: Any) -> dict:
        if not isinstance(name, str):
            raise ApiError(400, '"name" must be text')
        try:
            self.service.set_active_profile(name)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        return {"ok": True, "profile": self.service.active_profile}

    def layer(self, name: Any, one_shot: Any = False) -> dict:
        if not isinstance(name, str):
            raise ApiError(400, '"name" must be text')
        self.service.set_layer(name, bool(one_shot))
        return {"ok": True, "layer": self.service.active_layer}

    def sync(self) -> dict:
        outcome = self.service.sync_now()
        return {"ok": not outcome.failed, "summary": outcome.summary()}

    # ------------------------------------------------------------ helpers
    def _allowed(self, macro) -> bool:
        scope = self.service.db.get_setting(SCOPE_SETTING, "all")
        return scope != "favorites" or macro.favorite

    def _find(self, ident: Any):
        macros = self.service.db.list_macros()
        match = None
        if isinstance(ident, int) or (isinstance(ident, str) and ident.isdigit()):
            match = next((m for m in macros if m.id == int(ident)), None)
        if match is None and isinstance(ident, str):
            wanted = ident.strip().lower()
            match = next((m for m in macros if m.name.lower() == wanted), None)
        if match is None or not self._allowed(match):
            raise ApiError(404, f"no macro {ident!r} that may be run remotely")
        return match


class _Handler(BaseHTTPRequestHandler):
    server_version = "KeyboardManager"
    api: RemoteApi
    token: str

    # quiet: no request log on stderr
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if not self._authorized(query):
                raise ApiError(401, "missing or wrong access token")
            if method == "GET" and url.path in ("/", "/index.html"):
                self._send(200, PANEL_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            body = self._body() if method == "POST" else {}
            result = self._route(method, url.path, query, body)
            self._json(200, result)
        except ApiError as exc:
            self._json(exc.status, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - never kill the server thread
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _route(self, method: str, path: str, query: dict, body: dict) -> Any:
        api = self.api
        if method == "GET":
            if path == "/api/status":
                return api.status()
            if path == "/api/macros":
                return api.macros()
            if path == "/api/history":
                try:
                    limit = int(query.get("limit", ["20"])[0])
                except ValueError:
                    raise ApiError(400, "limit must be a number") from None
                return api.history(limit)
        if method == "POST":
            if path == "/api/run":
                if "macro" not in body:
                    raise ApiError(400, 'send {"macro": "name or id"}')
                return api.run(body["macro"], body.get("queue", False))
            if path in ("/api/stop", "/api/pause", "/api/resume", "/api/toggle-pause"):
                return api.control(path.rsplit("/", 1)[1])
            if path == "/api/profile":
                return api.profile(body.get("name"))
            if path == "/api/layer":
                return api.layer(body.get("name"), body.get("one_shot", False))
            if path == "/api/sync":
                return api.sync()
        raise ApiError(404 if path.startswith("/api/") or method == "GET" else 405, f"no such endpoint {path}")

    def _authorized(self, query: dict) -> bool:
        header = self.headers.get("Authorization", "")
        given = header[7:].strip() if header.lower().startswith("bearer ") else ""
        given = given or query.get("token", [""])[0]
        return bool(self.token) and hmac.compare_digest(given.encode(), self.token.encode())

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "bad Content-Length") from None
        if length > MAX_BODY:
            raise ApiError(413, "request too large")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise ApiError(400, "the body must be JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "the body must be a JSON object")
        return data

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


class RemoteServer:
    """The HTTP server on its own thread."""

    def __init__(self, service: "MacroService", token: str, port: int = DEFAULT_PORT, lan: bool = False) -> None:
        handler = type("Handler", (_Handler,), {"api": RemoteApi(service), "token": token})
        self.host = "0.0.0.0" if lan else "127.0.0.1"
        self._server = ThreadingHTTPServer((self.host, port), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.lan = lan
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="remote")
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(2)


class RemoteError(Exception):
    pass


class RemoteClient:
    """Talks to a running instance (used by ``keyboardmanager ctl``)."""

    def __init__(self, port: int, token: str, host: str = "127.0.0.1", timeout: float = 5.0) -> None:
        self.base = f"http://{host}:{port}"
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body or {}).encode() if method == "POST" else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - local URL
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error", str(exc))
            except (ValueError, AttributeError):
                message = str(exc)
            raise RemoteError(message) from None
        except (urllib.error.URLError, OSError) as exc:
            raise RemoteError(f"Keyboard Manager is not reachable at {self.base} ({exc})") from None


PANEL_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Keyboard Manager</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f5f7;color:#222}
 header{background:#1565c0;color:#fff;padding:12px 16px}
 header h1{font-size:18px;margin:0}
 #status{font-size:14px;opacity:.9;margin-top:4px}
 main{padding:12px 16px;max-width:720px;margin:auto}
 .row{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
 button{font-size:16px;padding:12px 14px;border:0;border-radius:8px;background:#fff;
        box-shadow:0 1px 2px rgba(0,0,0,.15);cursor:pointer;text-align:left}
 .stop{background:#c62828;color:#fff}.pause{background:#ef6c00;color:#fff}
 .macros{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}
 .macros button small{display:block;color:#777;font-size:12px}
 .inactive{opacity:.5}
 select{font-size:16px;padding:10px;border-radius:8px}
 #msg{min-height:20px;font-size:14px;color:#555;margin-bottom:8px}
</style></head><body>
<header><h1>Keyboard Manager</h1><div id="status">connecting...</div></header>
<main>
 <div class="row"><button class="stop" onclick="post('/api/stop')">Stop</button>
  <button class="pause" onclick="post('/api/toggle-pause')">Pause / resume</button>
  <select id="profile" onchange="post('/api/profile',{name:this.value})"></select></div>
 <div id="msg"></div>
 <div class="macros" id="macros"></div>
</main>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
const H={'Authorization':'Bearer '+token,'Content-Type':'application/json'};
async function get(p){const r=await fetch(p,{headers:H});return r.json();}
async function post(p,b){const r=await fetch(p,{method:'POST',headers:H,body:JSON.stringify(b||{})});
 const d=await r.json();document.getElementById('msg').textContent=d.ok?'OK':(d.error||'error');refresh();}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function refresh(){
 try{const s=await get('/api/status');
  let t=s.running?('Running: '+s.running+(s.paused?' (paused)':'')):'Idle';
  if(s.queue.length)t+=' - queued: '+s.queue.length;
  if(s.layer)t+=' - layer '+s.layer;
  document.getElementById('status').textContent=t;
  const sel=document.getElementById('profile');
  sel.innerHTML='<option value="">All profiles</option>'+s.profiles.map(p=>'<option>'+esc(p)+'</option>').join('');
  sel.value=s.profile;
 }catch(e){document.getElementById('status').textContent='not reachable';}
}
async function load(){
 const ms=await get('/api/macros');
 document.getElementById('macros').innerHTML=ms.map(m=>'<button class="'+(m.active&&m.enabled?'':'inactive')+
  '" data-id="'+m.id+'">'+(m.favorite?'&#9733; ':'')+esc(m.name)+'<small>'+esc(m.hotkey||m.folder||'')+'</small></button>').join('');
 document.querySelectorAll('.macros button').forEach(b=>b.onclick=()=>post('/api/run',{macro:Number(b.dataset.id),queue:true}));
}
load();refresh();setInterval(refresh,2000);
</script></body></html>
"""
