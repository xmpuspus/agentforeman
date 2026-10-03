"""Release contract of AgentForeman: the command line, the package, its names, and the install.

The tests run `python3 -m agentforeman` from the repo root, the way a person runs it from a
clone. tests/release_wheel_check.py covers the installed package.
"""

import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cli(*args, env=None, timeout=60, cwd=ROOT):
    return subprocess.run(
        [sys.executable, "-m", "agentforeman", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Port -> the page token of a server that running() started.
TOKENS = {}


def get(port, path):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Foreman-Token": TOKENS.get(port, "")},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode()


def page_url(port, route="overview"):
    """The sign-in link of a running() server, so a browser gets the cookie on its first load."""
    return f"http://127.0.0.1:{port}/?token={TOKENS[port]}#{route}"


@contextmanager
def running(*args):
    port = free_port()
    demo = args[0] == "demo"
    if not demo:
        sys.path.insert(0, str(ROOT))
        from agentforeman.server import load_token

        # A test server must never use the real control folder.
        ctl = Path(args[args.index("--control-dir") + 1])
        ctl.mkdir(parents=True, exist_ok=True, mode=0o700)
        TOKENS[port] = load_token(ctl)
    p = subprocess.Popen(
        [sys.executable, "-m", "agentforeman", *args, "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.PIPE if demo else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        if demo:
            # The demo prints its sign-in link, because it shows sample data only.
            line = p.stdout.readline()
            TOKENS[port] = re.search(r"\?token=([A-Za-z0-9_-]+)", line).group(1)
        end = time.time() + 30
        while True:
            try:
                json.loads(get(port, "/api/state"))
                break
            except (OSError, ValueError):
                if time.time() > end:
                    raise AssertionError("the server did not answer in 30 s")
                time.sleep(0.3)
        yield port
    finally:
        p.terminate()
        p.wait(10)


def token(port):
    return re.search(
        r'<meta name="foreman-token" content="([^"]+)">', get(port, "/")
    ).group(1)


def post(port, path, body, tok):
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["X-Foreman-Token"] = tok
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def ours(settings):
    s = json.loads(settings.read_text())
    return [
        h["command"]
        for groups in (s.get("hooks") or {}).values()
        for g in groups
        for h in g["hooks"]
        if "agentforeman-hook" in h["command"]
    ]


def test_version():
    r = cli("--version")
    assert r.returncode == 0
    assert re.fullmatch(r"agentforeman \d+\.\d+\.\d+\s*", r.stdout), r.stdout


def test_help_lists_the_commands():
    out = cli("--help").stdout
    for word in ("serve", "demo", "install", "uninstall"):
        assert word in out, word


def test_demo_serves_sample_sessions():
    with running("demo") as port:
        st = json.loads(get(port, "/api/state"))
        assert len(st["agents"]) >= 5
        html = get(port, "/")
        assert re.search(r"<title>[^<]*AgentForeman[^<]*</title>", html)
        assert token(port)


def test_serve_reads_the_given_folders_and_keeps_its_own_folder_private(tmp_path):
    ctl = tmp_path / "ctl"
    with running(
        "serve",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--control-dir",
        str(ctl),
        "--dry-open",
    ) as port:
        assert json.loads(get(port, "/api/state"))["agents"] == []
        assert post(port, "/api/guard", {"mode": "block"}, token(port)) == 200
        assert post(port, "/api/guard", {"mode": "warn"}, None) == 403
        assert post(port, "/api/guard", {"mode": "warn"}, "wrong") == 403
    assert stat.S_IMODE(ctl.stat().st_mode) == 0o700


def test_install_and_uninstall_with_the_defaults(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}))
    os.chmod(settings, 0o600)
    r = cli("install", env={"HOME": str(home)})
    assert r.returncode == 0, r.stderr
    cmds = ours(settings)
    assert len(cmds) == 4
    assert all(str(home / ".agentforeman") in c for c in cmds)
    assert any((home / ".agentforeman" / "hooks").glob("*.py"))
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600, "install keeps the file mode"
    assert stat.S_IMODE((home / ".agentforeman").stat().st_mode) == 0o700
    assert json.loads(settings.read_text())["model"] == "opus"
    r = cli("uninstall", env={"HOME": str(home)})
    assert r.returncode == 0, r.stderr
    assert ours(settings) == []
    assert json.loads(settings.read_text())["model"] == "opus"
    assert not any((home / ".agentforeman" / "hooks").glob("*.py")), (
        "uninstall removes the hook copy"
    )


def test_install_writes_through_a_linked_settings_file(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    link = home / ".claude" / "settings.json"
    link.symlink_to(real)
    assert cli("install", env={"HOME": str(home)}).returncode == 0
    assert link.is_symlink(), "the link to a dotfiles repo stays a link"
    assert len(ours(real)) == 4


def test_install_refuses_a_settings_file_that_does_not_parse(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{broken")
    r = cli(
        "install", "--settings", str(settings), "--control-dir", str(tmp_path / "ctl")
    )
    assert r.returncode != 0 and settings.read_text() == "{broken"


def test_the_installed_hook_runs_alone_and_uses_its_own_home(tmp_path):
    ctl = tmp_path / "ctl"
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    assert (
        cli(
            "install", "--settings", str(settings), "--control-dir", str(ctl)
        ).returncode
        == 0
    )
    hook = next((ctl / "hooks").glob("*.py"))
    env = {
        **os.environ,
        "AGENTFOREMAN_HOME": str(ctl),
        "CLAUDE_CODE_SESSION_ATTENDED": "1",
        "AGENTFOREMAN_PERMISSION_GRACE_S": "0.1",
    }
    payload = {
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "sudo -n true"},
    }
    p = subprocess.Popen(
        [sys.executable, str(hook), "permission"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    p.stdin.write(json.dumps(payload))
    p.stdin.close()
    try:
        end = time.time() + 10
        while not list((ctl / "requests").glob("*.json")):
            assert time.time() < end, "the copied hook wrote no request in its own home"
            time.sleep(0.2)
    finally:
        p.terminate()
        p.wait(5)


def test_the_hook_fails_open_with_no_home_folder(tmp_path):
    hook = next(ROOT.glob("agentforeman/*hook*.py"))
    r = subprocess.run(
        [sys.executable, str(hook), "pre"],
        input=json.dumps({"session_id": "s1", "tool_name": "Bash", "tool_input": {}}),
        capture_output=True,
        text=True,
        env={**os.environ, "AGENTFOREMAN_HOME": str(tmp_path / "missing")},
        cwd=tmp_path,
        timeout=10,
    )
    assert (r.returncode, r.stdout) == (0, "")
