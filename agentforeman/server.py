"""AgentForeman's server: a local page that shows every live Claude Code and Codex agent.

With the control hooks installed (agentforeman install), it also approves or denies a
waiting tool call, sends a reply to a session, stops a subagent or a whole session, sends a
steering note to a working session or subagent, keeps "Always allow" rules, and warns when
two sessions edit one file.

Run: agentforeman serve [--port 4320] [--claude-dir ~/.claude] [--codex-dir ~/.codex]
                        [--control-dir ~/.agentforeman] [--dry-open] [--open]
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote

from agentforeman.collector import Collector
from agentforeman.control import Control

# A server started outside a login shell often lacks `code` on PATH, so look in the app bundle too.
VSCODE_CLI = [
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
    str(
        Path.home()
        / "Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
    ),
]


def vscode_folder_command(cwd):
    found = shutil.which("code") or next(
        (p for p in VSCODE_CLI if Path(p).exists()), None
    )
    return [found, cwd] if found else ["open", "-b", "com.microsoft.VSCode", cwd]


def run_open(cmd):
    """Runs one open command. Returns an error text, or None when it worked."""
    try:
        done = subprocess.run(cmd, check=False, timeout=15, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)[:200] or type(e).__name__
    if done.returncode != 0:
        err = (done.stderr or b"").decode(errors="replace").strip()[:200]
        return err or str(done.returncode)
    return None


STATIC = Path(__file__).resolve().parent / "static"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
TICK_S = 1.5
HEARTBEAT_S = 15
POST_ROUTES = {
    "/api/open": "_open",
    "/api/approve": "_approve",
    "/api/reply": "_reply",
    "/api/stop": "_stop",
    "/api/steer": "_steer",
    "/api/rules": "_rules",
    "/api/guard": "_guard",
}
# Another page must not show AgentForeman in a frame, where a click could press its buttons.
SAFE_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,}")
NONCE_RE = re.compile(r"[0-9a-f]{32}")
COOKIE_AGE_S = 365 * 24 * 3600
SIGN_IN_PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>AgentForeman</title>
<style>body{font:15px/1.5 -apple-system,Helvetica,Arial,sans-serif;max-width:560px;margin:15vh auto;padding:0 24px;color:#18181b}
code{background:#f4f4f5;padding:2px 6px;border-radius:4px}</style></head><body>
<h1>Open AgentForeman from your terminal</h1>
<p>This page shows and controls your Claude Code sessions, so it opens only with its link.</p>
<p>Run <code>agentforeman open</code>. It opens this page with the link, and the browser remembers it.</p>
</body></html>"""


def load_token(control_dir):
    """Returns the page token from <control_dir>/token, and makes one when the file is missing.

    Only this macOS user can read the file. The token stays the same across restarts, so a
    browser that opened the link once stays signed in."""
    path = Path(control_dir) / "token"
    try:
        token = path.read_text().strip()
    except OSError:
        token = ""
    if TOKEN_RE.fullmatch(token):
        return token
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(token + "\n")
    return token


def sign_in_url(port, token):
    return f"http://127.0.0.1:{port}/?token={token}"


def proof(token, nonce):
    """Shows that a server knows the token, and does not show the token."""
    return hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()


class Hub:
    """Rebuilds the snapshot on a background thread and wakes event-stream readers on change."""

    def __init__(self, collector):
        self.collector = collector
        self.cond = threading.Condition()
        self.state = None
        self.body = None
        self.version = 0
        self.ready = threading.Event()
        self.wake = threading.Event()

    def poke(self):
        """Rebuilds the state now, so a button press shows at once."""
        self.wake.set()

    def run(self):
        while True:
            started = time.time()
            try:
                state = self.collector.snapshot()
            except Exception as e:  # keep serving the last good state
                print(f"snapshot failed: {e!r}", file=sys.stderr, flush=True)
                state = None
            if state is not None:
                key = json.dumps(
                    {k: v for k, v in state.items() if k != "generated_at"},
                    sort_keys=True,
                )
                with self.cond:
                    changed = key != getattr(self, "_key", None)
                    self._key = key
                    self.state = state
                    self.body = json.dumps(state).encode()
                    if changed:
                        self.version += 1
                        self.cond.notify_all()
                self.ready.set()
            self.wake.wait(max(0.2, TICK_S - (time.time() - started)))
            self.wake.clear()

    def session(self, session_id):
        with self.cond:
            agents = (self.state or {}).get("agents", [])
        return next((a for a in agents if a["session_id"] == session_id), None)

    def target(self, session_id, agent_id):
        """Returns (session, subagent) from the last snapshot. Either can be None."""
        agent = self.session(session_id) if session_id else None
        subs = (agent or {}).get("subagents", [])
        return agent, next((s for s in subs if s["agent_id"] == agent_id), None)


class Handler(BaseHTTPRequestHandler):
    hub = None
    control = None
    dry_open = False
    port = 4320
    token = ""
    protocol_version = "HTTP/1.1"

    def _host_ok(self):
        # A DNS-rebinding page reaches 127.0.0.1 under its own host name, so accept only ours.
        return (self.headers.get("Host") or "") in (
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
        )

    def _post_ok(self):
        """Returns an error response tuple, or None when the request may act."""
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            # A JSON content type also forces a CORS preflight, which this server never answers.
            return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "send application/json"}
        origin = self.headers.get("Origin")
        if origin and origin not in (
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        ):
            return HTTPStatus.FORBIDDEN, {"error": "wrong origin"}
        if not self._token_ok(self.headers.get("X-Foreman-Token")):
            return HTTPStatus.FORBIDDEN, {
                "error": "missing or wrong token; reload the page"
            }
        return None

    def _token_ok(self, sent):
        # Bytes, because compare_digest raises on a str that is not ASCII.
        sent = (sent or "").encode("utf-8", "replace")
        return bool(self.token) and secrets.compare_digest(sent, self.token.encode())

    def _cookie(self):
        return f"agentforeman-{self.port}"

    def _signed_in(self):
        """True when the request carries the token in the page's cookie or in the header.
        Any user of this Mac can reach 127.0.0.1, so the page and the API need the token."""
        if self._token_ok(self.headers.get("X-Foreman-Token")):
            return True
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except CookieError:
            return False
        morsel = jar.get(self._cookie())
        return bool(morsel) and self._token_ok(morsel.value)

    def _sign_in(self):
        """Stores the token in a cookie, then sends the browser to the page without the token
        in its address bar."""
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{self._cookie()}={self.token}; Max-Age={COOKIE_AGE_S}; Path=/; HttpOnly; SameSite=Lax",
        )
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0:
            raise ValueError("negative length")
        body = json.loads(self.rfile.read(min(length, 20_000)) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("not an object")
        return body

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        # Every response passes here, including send_error and the event stream.
        for name, value in SAFE_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode())

    def do_GET(self):
        if not self._host_ok():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "wrong host"})
        path, _, query = self.path.partition("?")
        if path in FILES and path != "/":
            name, ctype = FILES[path]
            return self._send(HTTPStatus.OK, (STATIC / name).read_bytes(), ctype)
        if path == "/" and self._token_ok(parse_qs(query).get("token", [""])[0]):
            return self._sign_in()
        if path == "/api/hello":
            # `agentforeman open` asks this before it sends the token. Only a server that
            # knows the token can answer, so another program on the port gets no link.
            nonce = parse_qs(query).get("nonce", [""])[0]
            if not NONCE_RE.fullmatch(nonce):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "send a nonce"})
            return self._json(HTTPStatus.OK, {"proof": proof(self.token, nonce)})
        if not self._signed_in():
            if path == "/":
                return self._send(
                    HTTPStatus.UNAUTHORIZED, SIGN_IN_PAGE, "text/html; charset=utf-8"
                )
            return self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "not signed in; run agentforeman open"},
            )
        if path == "/api/state":
            if not self.hub.ready.wait(10):
                return self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE, {"error": "warming up"}
                )
            with self.hub.cond:
                body = self.hub.body
            return self._send(HTTPStatus.OK, body)
        if path == "/api/events":
            return self._events()
        if path in POST_ROUTES:
            return self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "use POST"})
        if path in FILES:
            name, ctype = FILES[path]
            body = (STATIC / name).read_bytes()
            if name == "index.html":
                meta = f'<meta name="foreman-token" content="{self.token}">'.encode()
                body = body.replace(
                    b'<meta charset="utf-8">', b'<meta charset="utf-8">\n' + meta, 1
                )
            return self._send(HTTPStatus.OK, body, ctype)
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        # A refused request leaves its body unread. On a kept-alive connection those bytes
        # would start the next request, so every POST closes its connection.
        self.close_connection = True
        if not self._host_ok():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "wrong host"})
        path = self.path.split("?", 1)[0]
        if path not in POST_ROUTES:
            return self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "not allowed"})
        refused = self._post_ok()
        if refused:
            return self._json(*refused)
        try:
            body = self._body()
        except ValueError:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "send a JSON object"})
        return getattr(self, POST_ROUTES[path])(body)

    def _open(self, body):
        session_id = str(body.get("session_id") or "")
        agent = self.hub.session(session_id) if session_id else None
        if not agent or not agent["cwd"]:
            return self._json(
                HTTPStatus.NOT_FOUND, {"error": "no live session with that id"}
            )
        # The deep link would resume a terminal session inside VS Code too, and two
        # live copies of one session mix up its transcript.
        if agent["entrypoint"] == "cli":
            return self._json(
                HTTPStatus.CONFLICT,
                {"error": "This session runs in a terminal. Switch to that terminal."},
            )
        cwd = agent["cwd"]
        commands = [
            vscode_folder_command(cwd),
            [
                "open",
                f"vscode://anthropic.claude-code/open?session={quote(session_id)}",
            ],
        ]
        if not self.dry_open:
            for cmd in commands:
                err = run_open(cmd)
                if err:
                    return self._json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"Could not open VS Code: {err}"},
                    )
                time.sleep(0.8)
        self._json(HTTPStatus.OK, {"ok": True, "commands": commands})

    def _approve(self, body):
        err, count = self.control.decide_all(
            body.get("request_id"),
            body.get("behavior"),
            body.get("message"),
            similar=body.get("similar") is True,
        )
        if err == "bad request":
            return self._json(
                HTTPStatus.BAD_REQUEST,
                {"error": "send request_id and behavior allow or deny"},
            )
        if err:
            return self._json(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "That request is gone. It was answered in the session, or the session moved on."
                },
            )
        self.hub.poke()
        self._json(HTTPStatus.OK, {"ok": True, "count": count})

    def _rules(self, body):
        err = self.control.change_rules(
            body.get("action"), body.get("rule"), body.get("enabled")
        )
        if err:
            return self._json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "That rule grants too much or does not parse. SPEC.md lists the rules that AgentForeman takes."
                },
            )
        self.hub.poke()
        self._json(HTTPStatus.OK, {"ok": True})

    def _guard(self, body):
        if self.control.set_guard(body.get("mode")):
            return self._json(
                HTTPStatus.BAD_REQUEST, {"error": "mode must be off, warn, or block"}
            )
        self.hub.poke()
        self._json(HTTPStatus.OK, {"ok": True})

    def _reply(self, body):
        session_id = str(body.get("session_id") or "")
        if not self.hub.session(session_id):
            return self._json(
                HTTPStatus.NOT_FOUND, {"error": "no live session with that id"}
            )
        err = self.control.reply(session_id, body.get("text"))
        if err == "bad request":
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "write a reply first"})
        if err:
            return self._json(
                HTTPStatus.CONFLICT,
                {
                    "error": "This session does not take a reply now. It is busy, or it started before the controls were installed."
                },
            )
        # The reply wakes the session, so a stop sent before it must not end the new turn.
        self.control.clear_stop(f"session-{session_id}")
        self.hub.poke()
        self._json(HTTPStatus.OK, {"ok": True})

    def _stop(self, body):
        session_id = str(body.get("session_id") or "")
        if not body.get("agent_id"):
            return self._stop_session(session_id)
        _, sub = self.hub.target(session_id, body.get("agent_id"))
        if not sub:
            return self._json(
                HTTPStatus.NOT_FOUND, {"error": "no such subagent in a live session"}
            )
        if sub.get("backend") == "tmux":
            # A split-pane teammate is a session of its own, and its id is its session id.
            if sub["state"] != "running":
                return self._json(
                    HTTPStatus.CONFLICT,
                    {"error": "That teammate runs no tool call now."},
                )
            err = self.control.stop_session(sub["agent_id"])
        elif sub["state"] == "done":
            return self._json(
                HTTPStatus.CONFLICT, {"error": "That subagent already finished."}
            )
        else:
            err = self.control.stop(session_id, sub["agent_id"])
        if err:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad agent id"})
        self.hub.poke()
        self._json(HTTPStatus.OK, {"ok": True})

    def _stop_session(self, session_id):
        agent = self.hub.session(session_id) if session_id else None
        if not agent:
            return self._json(
                HTTPStatus.NOT_FOUND, {"error": "no live session with that id"}
            )
        if not agent["busy"]:
            return self._json(
                HTTPStatus.CONFLICT, {"error": "This session's turn already ended."}
            )
        if self.control.stop_session(session_id):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad session id"})
        self.hub.poke()
        self._json(HTTPStatus.OK, {"ok": True})

    def _steer(self, body):
        session_id = str(body.get("session_id") or "")
        agent_id = body.get("agent_id")
        agent, sub = self.hub.target(session_id, agent_id)
        if not agent or (agent_id and not sub):
            return self._json(
                HTTPStatus.NOT_FOUND, {"error": "no such session or subagent"}
            )
        text = str(body.get("text") or "").strip()
        if not text:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "write a note first"})
        # The hook hands a note over at the next tool call, so a target that runs none
        # would never get it.
        if not (sub["state"] == "running" if sub else agent["busy"]):
            return self._json(
                HTTPStatus.CONFLICT,
                {"error": "It runs no tool call now, so the note would never arrive."},
            )
        if sub and sub.get("backend") == "tmux":
            err = self.control.note(sub["agent_id"], None, text)
        else:
            err = self.control.note(session_id, sub and sub["agent_id"], text)
        if err:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad id"})
        self.hub.poke()
        self._json(HTTPStatus.OK, {"ok": True})

    def do_PUT(self):
        self.close_connection = True
        if not self._host_ok():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "wrong host"})
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "not allowed"})

    do_DELETE = do_PATCH = do_PUT

    def _events(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.hub.ready.wait(10)
        seen = -1
        try:
            while True:
                with self.hub.cond:
                    if self.hub.version == seen:
                        self.hub.cond.wait(HEARTBEAT_S)
                    version, body = self.hub.version, self.hub.body
                if version != seen and body:
                    self.wfile.write(b"event: state\ndata: " + body + b"\n\n")
                    seen = version
                else:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer also looks up the host name of 127.0.0.1 here, after the bind and before
        # the listen. Nothing uses that name, and a slow lookup keeps the port shut until it ends.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def serve(
    port=4320,
    claude_dir="~/.claude",
    codex_dir="~/.codex",
    control_dir="~/.agentforeman",
    dry_open=False,
    open_browser=False,
):
    control = Control(control_dir)
    # The folder holds the hooks' requests and the rules, so only this user may read it.
    control.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(control.dir, 0o700)
    hub = Hub(Collector(claude_dir, codex_dir, control))
    threading.Thread(target=hub.run, daemon=True).start()
    Handler.hub = hub
    Handler.control = control
    Handler.port = port
    Handler.token = load_token(control.dir)
    Handler.dry_open = dry_open
    server = LocalServer(("127.0.0.1", port), Handler)
    # The link holds the token, and output often goes to a log file that others can read.
    # So the link goes only to the browser.
    print(
        f"AgentForeman running at http://127.0.0.1:{port}. "
        "To open it, run: agentforeman open"
        + (f" --port {port}" if port != 4320 else ""),
        flush=True,
    )
    if open_browser:
        webbrowser.open(sign_in_url(port, Handler.token))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Without the server, edits.json goes stale, so the edit hook need not start Python.
        (control.dir / "edits.any").unlink(missing_ok=True)
    return 0
