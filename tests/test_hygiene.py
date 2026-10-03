"""The fixes from the release audit: bad requests, open errors, the hook log, the installer,
Codex errors, memory of ended sessions, the --open flag, and the demo exit. SPEC.md holds the
rules. These tests open no port, so a request goes through a socket pair."""

import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

from test_acceptance import ROOT

sys.path.insert(0, str(ROOT))

from agentforeman import cli, server  # noqa: E402
from agentforeman.collector import Collector  # noqa: E402

HOOK = ROOT / "agentforeman" / "agentforeman_hook.py"


class FakeHub:
    def __init__(self, agent=None):
        self.agent = agent

    def session(self, session_id):
        return self.agent

    def target(self, session_id, agent_id):
        return self.agent, None

    def poke(self):
        pass


def exchange(monkeypatch, raw, hub=None, dry_open=True):
    """Sends raw bytes to one Handler over a socket pair. Returns (status, body)."""
    monkeypatch.setattr(server.Handler, "hub", hub or FakeHub())
    monkeypatch.setattr(server.Handler, "port", 4320)
    monkeypatch.setattr(server.Handler, "token", "tok123")
    monkeypatch.setattr(server.Handler, "dry_open", dry_open)
    ours, theirs = socket.socketpair()

    def serve():
        try:
            server.Handler(ours, ("127.0.0.1", 1), None)
        finally:
            ours.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    # The client keeps its side open, as a browser does, so a server read past the body hangs.
    theirs.settimeout(5)
    theirs.sendall(raw)
    out = b""
    try:
        while chunk := theirs.recv(65536):
            out += chunk
    except TimeoutError:
        pass
    theirs.close()
    t.join(10)
    head, _, body = out.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1]) if head else None
    return status, body


def post(path, body, token="tok123", length=None):
    data = json.dumps(body).encode()
    head = (
        f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:4320\r\n"
        "Content-Type: application/json\r\n"
        f"X-Foreman-Token: {token}\r\n"
        f"Content-Length: {len(data) if length is None else length}\r\n\r\n"
    )
    return head.encode("latin-1") + data


def test_a_negative_content_length_gets_400(monkeypatch):
    # read(-1) reads until the client closes, so -1 would hang the request.
    for length in (-1, -5):
        status, _ = exchange(monkeypatch, post("/api/stop", {}, length=length))
        assert status == 400, length


def test_a_token_outside_ascii_gets_403(monkeypatch):
    status, _ = exchange(monkeypatch, post("/api/guard", {"mode": "warn"}, "t\xe9k"))
    assert status == 403, "the compare runs on bytes, so it refuses and never crashes"


def vscode_session():
    return {"cwd": "/home/demo/code/care-assistant", "entrypoint": "claude-vscode"}


def test_open_reports_a_missing_vscode_command(monkeypatch):
    monkeypatch.setattr(
        server,
        "vscode_folder_command",
        lambda cwd: ["/nonexistent/agentforeman-code", cwd],
    )
    status, body = exchange(
        monkeypatch,
        post("/api/open", {"session_id": "s1"}),
        FakeHub(vscode_session()),
        dry_open=False,
    )
    assert status == 500
    assert "Could not open VS Code" in json.loads(body)["error"]


def test_open_reports_a_command_that_runs_too_long(monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(server.subprocess, "run", slow)
    status, body = exchange(
        monkeypatch,
        post("/api/open", {"session_id": "s1"}),
        FakeHub(vscode_session()),
        dry_open=False,
    )
    assert status == 500
    assert "Could not open VS Code" in json.loads(body)["error"]


def run_hook(home, text):
    return subprocess.run(
        [sys.executable, str(HOOK), "pre"],
        input=text,
        capture_output=True,
        text=True,
        env={**os.environ, "AGENTFOREMAN_HOME": str(home)},
        timeout=10,
    )


def test_the_hook_logs_errors_only_while_debug_exists(tmp_path):
    quiet, loud = tmp_path / "quiet", tmp_path / "loud"
    quiet.mkdir()
    loud.mkdir()
    (loud / "debug").touch()
    for home in (quiet, loud):
        r = run_hook(home, "not json")
        assert (r.returncode, r.stdout) == (0, ""), "the hook still fails open"
    assert list(quiet.iterdir()) == []
    assert "error in mode pre" in (loud / "hook.log").read_text()


def test_install_skips_hook_entries_that_are_not_objects(tmp_path):
    settings = tmp_path / "settings.json"
    original = {
        "hooks": {
            "PermissionRequest": [
                {
                    "matcher": "",
                    "hooks": ["not an object", {"type": "command", "command": "true"}],
                }
            ],
            "Stop": ["also not an object"],
        }
    }
    settings.write_text(json.dumps(original))
    for command in ("install", "uninstall"):
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentforeman",
                command,
                "--settings",
                str(settings),
                "--control-dir",
                str(tmp_path / "ctl"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, r.stderr
    assert json.loads(settings.read_text()) == original


def test_a_broken_codex_database_prints_one_line(tmp_path, capsys):
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "state_5.sqlite").write_text("this is not a database")
    c = Collector(tmp_path / "claude", codex)
    for _ in range(2):
        c.codex_cache = (0.0, [])
        assert c.codex_threads() == []
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1 and "state_5.sqlite" in lines[0], lines


def test_the_collector_forgets_sessions_that_ended(tmp_path):
    claude = tmp_path / "claude"
    (claude / "sessions").mkdir(parents=True)
    cwd = "/home/demo/code/app"
    project = claude / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    project.mkdir(parents=True)
    # No <sessionId>.jsonl exists, so the collector takes the fallback path.
    (project / "other.jsonl").write_text("")
    entry = {"pid": os.getpid(), "sessionId": "s-gone", "cwd": cwd, "startedAt": 0}
    reg = claude / "sessions" / "s-gone.json"
    reg.write_text(json.dumps(entry))
    c = Collector(claude, tmp_path / "codex")
    assert [a["session_id"] for a in c.snapshot()["agents"]] == ["s-gone"]
    assert "s-gone" in c.fallback and c.subagents
    reg.unlink()
    assert c.snapshot()["agents"] == []
    assert c.fallback == {} and c.subagents == {}


def test_open_flag_opens_the_browser_and_the_folder_is_private(tmp_path, monkeypatch):
    opened, bound = [], []

    class OneShot:
        def __init__(self, address, handler):
            bound.append(address)

        def serve_forever(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(server, "ThreadingHTTPServer", OneShot)
    monkeypatch.setattr(server.Hub, "run", lambda self: None)
    monkeypatch.setattr(server.webbrowser, "open", opened.append)
    ctl = tmp_path / "ctl"
    args = ["--open", "--port", "4999", "--control-dir", str(ctl)]
    args += ["--claude-dir", str(tmp_path), "--codex-dir", str(tmp_path)]
    assert cli.main(args) == 0
    assert bound == [("127.0.0.1", 4999)]
    # --open signs the browser in, so the link carries the token from the control folder.
    token = (ctl / "token").read_text().strip()
    assert opened == [f"http://127.0.0.1:4999/?token={token}"]
    assert stat.S_IMODE(ctl.stat().st_mode) == 0o700


DEMO = """
import subprocess, sys
from agentforeman import demo
demo.start_server = lambda home, port: subprocess.Popen(["sleep", "300"])
sys.exit(demo.run(4999))
"""


def test_the_demo_removes_its_folder_on_sigterm():
    p = subprocess.Popen(
        [sys.executable, "-c", DEMO],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        line = p.stdout.readline()
        root = Path(re.search(r"sample data in (.+?)\)", line).group(1))
        assert root.is_dir()
        p.send_signal(signal.SIGTERM)
        assert p.wait(10) == 0
        assert not root.exists(), "the demo leaves no temp folder behind"
    finally:
        p.kill()
        p.wait()
