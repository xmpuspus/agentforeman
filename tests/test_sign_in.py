"""Only a browser that opened the sign-in link can read or use the page.

Every account on a Mac can reach 127.0.0.1, so the page, the state, and the event stream need
the token. The token lives in the control folder, which only this user can read. SPEC.md,
"Sign-in", holds the rules.
"""

import hashlib
import hmac
import http.client
import http.server
import json
import os
import re
import stat
import subprocess
import sys
import threading

from test_acceptance import ROOT, TEST_TOKEN, home, server  # noqa: F401
from test_release import cli, free_port, running

from agentforeman.server import LocalServer


def ask(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r, body


def test_without_the_token_the_page_and_the_api_refuse(server):  # noqa: F811
    r, body = ask(server, "/")
    assert r.status == 401 and b"agentforeman open" in body
    assert TEST_TOKEN.encode() not in body
    for path in ("/api/state", "/api/events", "/nothing"):
        assert ask(server, path)[0].status == 401, path
    # The script and the styles hold no data, so they need no token.
    for path in ("/app.js", "/app.css"):
        assert ask(server, path)[0].status == 200, path


def test_a_wrong_token_gets_no_cookie(server):  # noqa: F811
    r, _ = ask(server, "/?token=" + "x" * 43)
    assert r.status == 401 and r.getheader("Set-Cookie") is None
    cookie = {"Cookie": f"agentforeman-{server}={'x' * 43}"}
    assert ask(server, "/api/state", cookie)[0].status == 401


def test_the_sign_in_link_sets_a_cookie_and_drops_the_token_from_the_address(server):  # noqa: F811
    r, _ = ask(server, f"/?token={TEST_TOKEN}")
    assert r.status == 303 and r.getheader("Location") == "/"
    set_cookie = r.getheader("Set-Cookie")
    assert set_cookie.startswith(f"agentforeman-{server}={TEST_TOKEN};"), set_cookie
    for part in ("HttpOnly", "SameSite=Lax", "Path=/"):
        assert part in set_cookie, part
    cookie = {"Cookie": f"agentforeman-{server}={TEST_TOKEN}"}
    r, body = ask(server, "/", cookie)
    assert r.status == 200
    assert f'<meta name="foreman-token" content="{TEST_TOKEN}">'.encode() in body
    r, body = ask(server, "/api/state", cookie)
    assert r.status == 200 and json.loads(body)["agents"]


def test_a_cookie_for_another_port_does_not_sign_in(server):  # noqa: F811
    cookie = {"Cookie": f"agentforeman-{server + 1}={TEST_TOKEN}"}
    assert ask(server, "/api/state", cookie)[0].status == 401


def test_the_token_file_is_private_and_stays_across_restarts(tmp_path):
    ctl = tmp_path / "ctl"
    args = [
        "serve",
        "--claude-dir",
        str(tmp_path / "c"),
        "--codex-dir",
        str(tmp_path / "x"),
    ]
    args += ["--control-dir", str(ctl), "--dry-open"]
    with running(*args):
        first = (ctl / "token").read_text().strip()
    assert stat.S_IMODE((ctl / "token").stat().st_mode) == 0o600
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first)
    with running(*args):
        assert (ctl / "token").read_text().strip() == first


def test_the_server_prints_no_token(tmp_path):
    ctl = tmp_path / "ctl"
    port = free_port()
    p = subprocess.Popen(
        [sys.executable, "-m", "agentforeman", "serve", "--port", str(port)]
        + ["--claude-dir", str(tmp_path / "c"), "--codex-dir", str(tmp_path / "x")]
        + ["--control-dir", str(ctl), "--dry-open"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        line = p.stdout.readline()
    finally:
        p.terminate()
        p.wait(10)
    assert "agentforeman open" in line, line
    assert (ctl / "token").read_text().strip() not in line
    assert "token=" not in line


def test_open_prints_the_link_only_while_the_server_runs(tmp_path):
    ctl = tmp_path / "ctl"
    r = cli("open", "--print", "--control-dir", str(ctl))
    assert r.returncode == 1 and "has not started" in r.stdout
    args = [
        "serve",
        "--claude-dir",
        str(tmp_path / "c"),
        "--codex-dir",
        str(tmp_path / "x"),
    ]
    with running(*args, "--control-dir", str(ctl), "--dry-open") as port:
        token = (ctl / "token").read_text().strip()
        r = cli("open", "--print", "--port", str(port), "--control-dir", str(ctl))
        assert r.returncode == 0
        assert r.stdout.strip() == f"http://127.0.0.1:{port}/?token={token}"
    r = cli("open", "--print", "--port", str(port), "--control-dir", str(ctl))
    assert r.returncode == 1 and "Nothing answers" in r.stdout


class Squatter(http.server.BaseHTTPRequestHandler):
    """Another program on the port, which answers every request and records the paths."""

    seen = []

    def do_GET(self):
        Squatter.seen.append(self.path)
        body = json.dumps({"proof": "0" * 64}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_open_never_sends_the_link_to_another_program_on_the_port(tmp_path):
    ctl = tmp_path / "ctl"
    args = [
        "serve",
        "--claude-dir",
        str(tmp_path / "c"),
        "--codex-dir",
        str(tmp_path / "x"),
    ]
    with running(*args, "--control-dir", str(ctl), "--dry-open"):
        pass
    token = (ctl / "token").read_text().strip()
    port = free_port()
    squatter = LocalServer(("127.0.0.1", port), Squatter)
    threading.Thread(target=squatter.serve_forever, daemon=True).start()
    try:
        r = cli("open", "--print", "--port", str(port), "--control-dir", str(ctl))
    finally:
        squatter.shutdown()
    assert r.returncode == 1 and "not your AgentForeman" in r.stdout, r.stdout
    assert token not in r.stdout
    assert all(token not in p for p in Squatter.seen), Squatter.seen


def test_hello_proves_the_token_without_showing_it(server):  # noqa: F811
    nonce = "ab" * 16
    r, body = ask(server, f"/api/hello?nonce={nonce}")
    assert r.status == 200
    sent = json.loads(body)["proof"]
    assert (
        sent
        == hmac.new(TEST_TOKEN.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    )
    assert TEST_TOKEN not in body.decode()
    assert ask(server, "/api/hello?nonce=xyz")[0].status == 400


def test_install_keeps_a_private_settings_file_private(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"SECRET": "x"}}))
    os.chmod(settings, 0o600)
    r = cli(
        "install", "--settings", str(settings), "--control-dir", str(tmp_path / "ctl")
    )
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    left = [p.name for p in tmp_path.iterdir() if p.name.startswith(".settings.json")]
    assert not left, f"temporary files left: {left}"
