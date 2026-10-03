"""Black-box acceptance tests for AgentForeman, run against a fake Claude and Codex home."""

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW_MS = int(time.time() * 1000)


def iso(offset_s):
    t = datetime.fromtimestamp(NOW_MS / 1000 + offset_s, tz=timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def ms(offset_s):
    return int(NOW_MS + offset_s * 1000)


def enc(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def prompt(sid, text, at):
    return {
        "type": "user",
        "sessionId": sid,
        "timestamp": iso(at),
        "promptSource": "sdk",
        "message": {"role": "user", "content": text},
    }


def tool_call(sid, tool_id, name, inp, at, stop="tool_use"):
    return {
        "type": "assistant",
        "sessionId": sid,
        "timestamp": iso(at),
        "message": {
            "role": "assistant",
            "model": "claude-opus-5-5",
            "stop_reason": stop,
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 200,
                "output_tokens": 50,
            },
            "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": inp}
            ],
        },
    }


def tool_result(sid, tool_id, at, text="ok"):
    return {
        "type": "user",
        "sessionId": sid,
        "timestamp": iso(at),
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": text}
            ],
        },
    }


def reply(sid, text, at):
    return {
        "type": "assistant",
        "sessionId": sid,
        "timestamp": iso(at),
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 5,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 0,
                "output_tokens": 20,
            },
            "content": [{"type": "text", "text": text}],
        },
    }


def write_jsonl(path, rows, prefix=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(prefix)
        for r in rows:
            f.write(json.dumps(r) + "\n")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The page and the API need the page token. Servers on the fixture home take TEST_TOKEN, and a
# test that starts a server on another control folder records its token here by port.
TEST_TOKEN = "test-token-" + "0" * 32
TOKENS = {}


def token_for(port):
    return TOKENS.get(port, TEST_TOKEN)


def register(port, control_dir):
    """Makes the token of a server before it starts, and records it for get() and signed()."""
    sys.path.insert(0, str(ROOT))
    from agentforeman.server import load_token

    Path(control_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    TOKENS[port] = load_token(control_dir)
    return TOKENS[port]


def signed(url):
    """The sign-in link for a page URL, so the browser gets the cookie on its first load."""
    port = int(re.search(r":(\d+)", url).group(1))
    base, _, frag = url.partition("#")
    link = f"{base.rstrip('/')}/?token={token_for(port)}"
    return link + (f"#{frag}" if frag else "")


def get(port, path, timeout=5):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"X-Foreman-Token": token_for(port)}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def state(port):
    return json.loads(get(port, "/api/state")[2])


def page_token(port):
    html = get(port, "/")[2].decode()
    return re.search(r'<meta name="foreman-token" content="([^"]+)">', html).group(1)


def post(port, path, body, token=None, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Foreman-Token": page_token(port) if token is None else token,
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def wait_for(check, timeout=8.0):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = check()
            if last:
                return last
        except Exception as e:  # server still warming up or mid-update
            last = e
        time.sleep(0.25)
    raise AssertionError(f"condition not met in {timeout}s, last={last!r}")


def by_name(st, name):
    found = [a for a in st["agents"] if a["name"] == name]
    assert len(found) == 1, f"{name}: {len(found)} agents"
    return found[0]


@pytest.fixture
def home(tmp_path):
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    (claude / "sessions").mkdir(parents=True)
    procs = []

    def live_pid():
        p = subprocess.Popen(["sleep", "300"])
        procs.append(p)
        return p.pid

    def register(pid, sid, cwd, name, status, entrypoint="claude-vscode"):
        (claude / "sessions" / f"{pid}.json").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "sessionId": sid,
                    "cwd": cwd,
                    "name": name,
                    "status": status,
                    "entrypoint": entrypoint,
                    "startedAt": ms(-3600),
                    "statusUpdatedAt": ms(-60),
                }
            )
        )

    info = {
        "claude": claude,
        "codex": codex,
        "control": tmp_path / "control",
        "pids": {},
    }
    # The server keeps a token file that it finds, so every server on this home takes TEST_TOKEN.
    info["control"].mkdir(mode=0o700)
    (info["control"] / "token").write_text(TEST_TOKEN + "\n")

    # alpha: working, 3 MB of old history before the recent part, and four subagents
    sid_a = "sid-alpha"
    pid = live_pid()
    info["pids"]["alpha"] = pid
    register(pid, sid_a, "/work/alpha", "alpha-1", "busy")
    old = [tool_call(sid_a, "old1", "Glob", {"pattern": "**/*.old"}, -3 * 86400)]
    filler = "".join(
        json.dumps(prompt(sid_a, "x" * 400, -2 * 86400)) + "\n" for _ in range(7000)
    )
    notification = (
        "<task-notification>\n<task-id>abc</task-id>\n<tool-use-id>toolu_sub3</tool-use-id>\n"
        "<status>completed</status>\n</task-notification>"
    )
    recent = [
        prompt(sid_a, "Fix the login bug", -360),
        tool_call(sid_a, "toolu_sub2", "Agent", {"description": "Map callers"}, -330),
        tool_result(sid_a, "toolu_sub2", -300),
        tool_call(sid_a, "toolu_sub3", "Agent", {"description": "Search logs"}, -300),
        tool_result(sid_a, "toolu_sub3", -299, "Async agent launched"),
        {
            "type": "user",
            "sessionId": sid_a,
            "timestamp": iso(-270),
            "message": {"role": "user", "content": notification},
        },
        tool_call(
            sid_a, "t1", "Bash", {"command": "pytest -q tests/test_login.py"}, -240
        ),
        tool_result(sid_a, "t1", -180),
        tool_call(
            sid_a, "toolu_sub1", "Agent", {"description": "Check expiry rules"}, -120
        ),
        tool_result(sid_a, "toolu_sub1", -119, "Async agent launched"),
        tool_call(sid_a, "t2", "Edit", {"file_path": "/work/alpha/src/app.py"}, -60),
    ]
    proj_a = claude / "projects" / enc("/work/alpha")
    trans_a = proj_a / f"{sid_a}.jsonl"
    old_text = "".join(json.dumps(r) + "\n" for r in old)
    write_jsonl(trans_a, recent, prefix=old_text + filler)
    info["trans_a"] = trans_a

    subs = proj_a / sid_a / "subagents"
    subs.mkdir(parents=True)

    def subagent(agent_id, meta, rows, age_s):
        write_jsonl(subs / f"agent-{agent_id}.jsonl", rows)
        (subs / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
        t = time.time() - age_s
        os.utime(subs / f"agent-{agent_id}.jsonl", (t, t))

    base = {"model": "sonnet", "agentType": "general-purpose"}
    subagent(
        "aaa",
        {
            **base,
            "name": "rules",
            "description": "Check expiry rules",
            "toolUseId": "toolu_sub1",
            "requestShape": "background",
        },
        [
            tool_call(
                sid_a, "toolu_nested", "Agent", {"description": "Scan dates"}, -50
            ),
            tool_result(sid_a, "toolu_nested", -40),
            tool_call(sid_a, "s1", "Read", {"file_path": "/work/alpha/rules.py"}, -30),
        ],
        5,
    )
    # a subagent that the "rules" subagent started, stored in the same folder
    subagent(
        "eee",
        {
            **base,
            "name": "dates",
            "description": "Scan dates",
            "toolUseId": "toolu_nested",
            "spawnDepth": 2,
        },
        [tool_call(sid_a, "n1", "Grep", {"pattern": "date"}, -45)],
        5,
    )
    subagent(
        "bbb",
        {
            **base,
            "name": "callers",
            "description": "Map callers",
            "toolUseId": "toolu_sub2",
        },
        [reply(sid_a, "Found 4 callers.", -310)],
        5,
    )
    subagent(
        "ccc",
        {
            **base,
            "name": "logs",
            "description": "Search logs",
            "toolUseId": "toolu_sub3",
            "requestShape": "background",
        },
        [tool_call(sid_a, "s3", "Grep", {"pattern": "ERROR"}, -280)],
        5,
    )
    subagent(
        "ddd",
        {
            **base,
            "name": "quiet",
            "description": "Quiet finisher",
            "toolUseId": "toolu_sub4",
            "requestShape": "background",
        },
        [reply(sid_a, "Done.", -400)],
        300,
    )
    subagent(
        "old",
        {
            **base,
            "name": "ancient",
            "description": "Old run",
            "toolUseId": "toolu_sub5",
            "requestShape": "background",
        },
        [reply(sid_a, "Done.", -30000)],
        8 * 3600,
    )

    # beta: idle after a finished turn, so it is the user's turn
    sid_b = "sid-beta"
    pid = live_pid()
    info["pids"]["beta"] = pid
    register(pid, sid_b, "/work/beta", "beta-1", "idle")
    write_jsonl(
        claude / "projects" / enc("/work/beta") / f"{sid_b}.jsonl",
        [
            prompt(sid_b, "Run the tests", -1200),
            reply(sid_b, "All tests pass. Want me to open a PR?", -900),
        ],
    )

    # gamma: idle with an unanswered tool call, so it probably waits for approval
    sid_c = "sid-gamma"
    pid = live_pid()
    register(pid, sid_c, "/work/gamma", "gamma-1", "idle")
    write_jsonl(
        claude / "projects" / enc("/work/gamma") / f"{sid_c}.jsonl",
        [
            prompt(sid_c, "Clean the build", -600),
            tool_call(sid_c, "t9", "Bash", {"command": "rm -rf build"}, -480),
        ],
    )

    # delta: live in a terminal, with no transcript yet
    pid = live_pid()
    register(pid, "sid-delta", "/work/delta", "delta-1", "idle", "cli")

    # epsilon: the transcript file name differs from the registry session id
    sid_f = "sid-eps"
    pid = live_pid()
    register(pid, sid_f, "/work/epsilon", "eps-1", "busy")
    write_jsonl(
        claude / "projects" / enc("/work/epsilon") / "sid-eps-new.jsonl",
        [
            prompt(sid_f, "Read the config", -100),
            tool_call(
                sid_f, "r1", "Read", {"file_path": "/work/epsilon/config.toml"}, -50
            ),
        ],
    )

    # dead: a registry entry whose process has exited
    dead = subprocess.Popen(["true"])
    dead.wait()
    register(dead.pid, "sid-dead", "/work/dead", "dead-1", "busy")

    # codex: one recent thread, one old, one archived
    codex.mkdir()
    db = sqlite3.connect(codex / "state_5.sqlite")
    db.execute(
        "create table threads (id text, title text, cwd text, model text, updated_at_ms integer, archived integer)"
    )
    db.executemany(
        "insert into threads values (?,?,?,?,?,?)",
        [
            ("th-new", "Fix login bug", "/work/alpha", "gpt-6", ms(-3600), 0),
            ("th-old", "Old work", "/work/alpha", "gpt-6", ms(-72 * 3600), 0),
            ("th-arch", "Archived work", "/work/alpha", "gpt-6", ms(-1800), 1),
        ],
    )
    db.commit()
    db.close()
    db = sqlite3.connect(codex / "thread_history_1.sqlite")
    db.execute(
        "create table thread_turns (thread_id text, turn_id text, status text, started_at integer, completed_at integer)"
    )
    db.executemany(
        "insert into thread_turns values (?,?,?,?,?)",
        [
            ("th-new", "u1", "completed", NOW_MS // 1000 - 7200, NOW_MS // 1000 - 7000),
            ("th-new", "u2", "inProgress", NOW_MS // 1000 - 3600, None),
        ],
    )
    db.commit()
    db.close()

    yield info
    for p in procs:
        p.kill()
        p.wait()


def snapshot(*dirs):
    out = {}
    for d in dirs:
        for p in Path(d).rglob("*"):
            if p.is_file():
                st = p.stat()
                out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture
def server(home):
    port = free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentforeman",
            "serve",
            "--port",
            str(port),
            "--claude-dir",
            str(home["claude"]),
            "--codex-dir",
            str(home["codex"]),
            "--control-dir",
            str(home["control"]),
            "--dry-open",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    start = time.time()
    try:
        wait_for(lambda: state(port), timeout=10)
        home["first_state_s"] = time.time() - start
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_lists_only_live_sessions(server, home):
    st = state(server)
    names = sorted(a["name"] for a in st["agents"])
    assert names == ["alpha-1", "beta-1", "delta-1", "eps-1", "gamma-1"]
    assert home["first_state_s"] < 10
    assert st["counts"] == {"working": 2, "your_turn": 1, "waiting": 1, "idle": 1}


def test_working_session_shows_current_and_recent_actions(server):
    a = by_name(state(server), "alpha-1")
    assert a["status"] == "working" and a["status_guess"] is False
    assert a["project"] == "alpha" and a["cwd"] == "/work/alpha"
    assert a["current_action"]["tool"] == "Edit"
    assert a["current_action"]["summary"] == "app.py"
    assert abs(a["current_action"]["started_ms"] - ms(-60)) < 1500
    tools = [x["tool"] for x in a["recent_actions"]]
    assert tools == ["Edit", "Agent", "Bash", "Agent", "Agent"], (
        "the 3 MB-old Glob must stay unread"
    )
    bash = a["recent_actions"][2]
    assert bash["done"] is True and bash["summary"].startswith(
        "pytest -q tests/test_login.py"
    )
    assert a["recent_actions"][0]["done"] is False
    assert a["model"] == "claude-opus-5-5" and a["context_tokens"] == 1210
    assert len(a["activity"]) == 30 and sum(a["activity"]) == 5


def test_your_turn_and_waiting_and_idle(server):
    st = state(server)
    b = by_name(st, "beta-1")
    assert b["status"] == "your_turn" and b["current_action"] is None
    assert "open a PR" in b["last_message"]
    assert abs(b["since_ms"] - ms(-900)) < 1500
    c = by_name(st, "gamma-1")
    assert c["status"] == "waiting" and c["status_guess"] is True
    assert (
        c["current_action"]["tool"] == "Bash"
        and c["current_action"]["summary"] == "rm -rf build"
    )
    d = by_name(st, "delta-1")
    assert d["status"] == "idle" and d["transcript"] is None and d["since_ms"] is None
    e = by_name(st, "eps-1")
    assert e["transcript"].endswith("sid-eps-new.jsonl")
    assert (
        e["recent_actions"][0]["tool"] == "Read"
        and e["recent_actions"][0]["summary"] == "config.toml"
    )


def test_subagents_nested_with_state(server):
    subs = {s["name"]: s for s in by_name(state(server), "alpha-1")["subagents"]}
    assert set(subs) == {"rules", "callers", "logs", "quiet", "dates"}, (
        "the 8-hour-old subagent must be hidden"
    )
    assert subs["dates"]["parent_agent_id"] == "aaa" and subs["dates"]["depth"] == 2
    assert subs["dates"]["state"] == "done", "its parent subagent has its result"
    assert all(
        subs[n]["parent_agent_id"] is None
        for n in ("rules", "callers", "logs", "quiet")
    )
    assert subs["rules"]["state"] == "running"
    assert (
        subs["rules"]["last_action"]["tool"] == "Read"
        and subs["rules"]["last_action"]["summary"] == "rules.py"
    )
    assert (
        subs["rules"]["description"] == "Check expiry rules"
        and subs["rules"]["model"] == "sonnet"
    )
    assert subs["callers"]["state"] == "done"
    assert subs["logs"]["state"] == "done"
    assert subs["quiet"]["state"] == "done"


def test_appended_lines_and_registry_changes_show_up(server, home):
    line = (
        json.dumps(tool_call("sid-alpha", "t3", "Grep", {"pattern": "TODO"}, 0)) + "\n"
    )
    with open(home["trans_a"], "a") as f:
        f.write(line[:25])
    time.sleep(2.5)
    assert by_name(state(server), "alpha-1")["recent_actions"][0]["tool"] == "Edit"
    with open(home["trans_a"], "a") as f:
        f.write(line[25:])
    wait_for(
        lambda: by_name(state(server), "alpha-1")["recent_actions"][0]["tool"] == "Grep"
    )
    assert by_name(state(server), "alpha-1")["current_action"]["summary"] == "TODO"

    reg = home["claude"] / "sessions" / f"{home['pids']['beta']}.json"
    data = json.loads(reg.read_text())
    data["status"] = "busy"
    reg.write_text(json.dumps(data))
    wait_for(lambda: by_name(state(server), "beta-1")["status"] == "working")


def test_codex_threads(server):
    codex = state(server)["codex"]
    assert [t["thread_id"] for t in codex] == ["th-new"]
    assert codex[0]["title"] == "Fix login bug" and codex[0]["status"] == "inProgress"


def test_open_uses_server_side_cwd_only(server):
    code, body = post(server, "/api/open", {"session_id": "sid-alpha", "cwd": "/etc"})
    assert code == 200 and body["ok"] is True
    first = body["commands"][0]
    assert first[-1] == "/work/alpha" and first[0].split("/")[-1] in ("code", "open"), (
        "VS Code opens the session folder from the server-side cwd"
    )
    assert (
        body["commands"][1][0] == "open"
        and "session=sid-alpha" in body["commands"][1][1]
    )
    assert post(server, "/api/open", {"session_id": "nope"})[0] == 404
    with pytest.raises(urllib.error.HTTPError) as err:
        get(server, "/api/open")
    assert err.value.code == 405


def test_event_stream_sends_state(server):
    req = urllib.request.Request(
        f"http://127.0.0.1:{server}/api/events",
        headers={"X-Foreman-Token": token_for(server)},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert "text/event-stream" in r.headers.get("Content-Type", "")
        seen = ""
        end = time.time() + 5
        while time.time() < end and "data:" not in seen:
            seen += r.readline().decode()
    assert "event: state" in seen and '"agents"' in seen


def test_page_is_self_contained(server):
    code, ctype, body = get(server, "/")
    html = body.decode()
    assert code == 200 and "text/html" in ctype and "<html" in html.lower()
    assert not re.search(r'(src|href)=["\']https?://', html), (
        "no CDN or external assets"
    )


def test_binds_loopback_only(server):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            lan_ip = s.getsockname()[0]
        except OSError:
            pytest.skip("no non-loopback address")
    if lan_ip.startswith("127."):
        pytest.skip("no non-loopback address")
    with socket.socket() as s:
        s.settimeout(2)
        assert s.connect_ex((lan_ip, server)) != 0


def test_never_writes_to_claude_or_codex_dirs(home):
    before = snapshot(home["claude"], home["codex"])
    port = free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentforeman",
            "serve",
            "--port",
            str(port),
            "--claude-dir",
            str(home["claude"]),
            "--codex-dir",
            str(home["codex"]),
            "--control-dir",
            str(home["control"]),
            "--dry-open",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: state(port), timeout=10)
        time.sleep(3)
    finally:
        proc.terminate()
        proc.wait(5)
    assert snapshot(home["claude"], home["codex"]) == before
