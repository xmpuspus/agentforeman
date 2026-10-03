"""Acceptance tests for the first foreman release: frame blocking, the whole command on an
approval, split-pane (tmux) teammates, steering notes, stopping a whole session, and the
repeat flag. SPEC.md, section Foreman, holds the contract.

The fixture sessions come from test_acceptance: alpha-1 and eps-1 are busy, beta-1 ended
its turn, gamma-1 is idle with an open tool call. Rows written during a test carry wall-clock
timestamps, because NOW_MS is fixed when the module loads.
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from test_acceptance import (  # noqa: F401
    ROOT,
    by_name,
    enc,
    home,
    post,
    server,
    state,
    token_for,
    wait_for,
    write_jsonl,
)
from test_controls import finish, run_install, start_hook  # noqa: F401


def iso_now(offset_s=0.0):
    t = time.time() + offset_s
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def now_ms():
    return int(time.time() * 1000)


def user_prompt(sid, text, offset_s=0.0):
    return {
        "type": "user",
        "sessionId": sid,
        "timestamp": iso_now(offset_s),
        "message": {"role": "user", "content": text},
    }


def call(sid, tool_id, name, inp, offset_s=0.0):
    return {
        "type": "assistant",
        "sessionId": sid,
        "timestamp": iso_now(offset_s),
        "message": {
            "role": "assistant",
            "model": "claude-opus-5-5",
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": inp}
            ],
        },
    }


def result(sid, tool_id, offset_s=0.0):
    return {
        "type": "user",
        "sessionId": sid,
        "timestamp": iso_now(offset_s),
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
            ],
        },
    }


def answer(sid, text, offset_s=0.0):
    return {
        "type": "assistant",
        "sessionId": sid,
        "timestamp": iso_now(offset_s),
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
        },
    }


def append(path, *rows):
    with open(path, "a") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)


def drop(folder, name, data):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data))


def end_turn(home, name="alpha"):  # noqa: F811
    """Marks a fixture session idle in the Claude Code registry, the way a finished turn does."""
    path = home["claude"] / "sessions" / f"{home['pids'][name]}.json"
    reg = json.loads(path.read_text())
    reg.update(status="idle", statusUpdatedAt=now_ms())
    path.write_text(json.dumps(reg))


@pytest.fixture
def procs():
    started = []

    def start(*argv):
        p = subprocess.Popen(list(argv) or ["sleep", "300"])
        started.append(p)
        return p.pid

    yield start
    for p in started:
        p.kill()
        p.wait()


def headers_of(port, path, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Foreman-Token": token_for(port), **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers


# Frame blocking


@pytest.mark.parametrize("path", ["/", "/app.js", "/app.css", "/api/state"])
def test_every_response_refuses_to_load_in_a_frame(server, path):  # noqa: F811
    code, h = headers_of(server, path)
    assert code == 200
    assert (h.get("X-Frame-Options") or "").upper() == "DENY"
    assert "frame-ancestors 'none'" in (h.get("Content-Security-Policy") or "")


def test_a_refused_request_also_refuses_frames(server):  # noqa: F811
    code, h = headers_of(server, "/api/state", {"Host": "rebound.example"})
    assert code == 403
    assert (h.get("X-Frame-Options") or "").upper() == "DENY"


# The whole command on an approval


def test_an_approval_carries_the_whole_command(server, home, procs):  # noqa: F811
    cmd = "python3 - <<'EOF'\n" + "print('step')\n" * 120 + "EOF"
    assert len(cmd) > 1500
    drop(
        home["control"] / "requests",
        "long1.json",
        {
            "id": "long1",
            "pid": procs(),
            "session_id": "sid-beta",
            "tool_name": "Bash",
            "tool_input": {"command": cmd, "description": "Print the steps"},
            "created_ms": now_ms(),
        },
    )
    ap = wait_for(lambda: by_name(state(server), "beta-1")["approval"])
    assert ap["detail"] == cmd, "the page must show the command that runs, uncut"
    assert ap["desc"] == "Print the steps"


# Split-pane (tmux) teammates

MATE_A = "aaaa1111-0000-4000-8000-000000000001"
MATE_B = "bbbb2222-0000-4000-8000-000000000002"


def make_team(home, procs, lead="sid-alpha", cwd="/work/alpha"):  # noqa: F811
    claude = home["claude"]
    team = claude / "teams" / "team-x"
    team.mkdir(parents=True)
    joined = str(now_ms() - 600_000)
    member = {"agentType": "researcher", "model": "sonnet", "backendType": "tmux"}
    (team / "config.json").write_text(
        json.dumps(
            {
                "name": "team-x",
                "leadAgentId": "team-lead@team-x",
                "leadSessionId": lead,
                "members": [
                    {
                        "agentId": "team-lead@team-x",
                        "name": "team-lead",
                        "backendType": "in-process",
                        "cwd": cwd,
                    },
                    {
                        **member,
                        "agentId": "lane-a@team-x",
                        "name": "lane-a",
                        "prompt": "Research the A market",
                        "cwd": cwd,
                        "joinedAt": joined,
                    },
                    {
                        **member,
                        "agentId": "lane-b@team-x",
                        "name": "lane-b",
                        "prompt": "Research the B market",
                        "cwd": cwd,
                        "joinedAt": joined,
                    },
                ],
            }
        )
    )
    proj = claude / "projects" / enc(cwd)

    def mate(sid, name, rows):
        write_jsonl(
            proj / f"{sid}.jsonl",
            [{**r, "teamName": "team-x", "agentName": name} for r in rows],
        )

    mate(
        MATE_A,
        "lane-a",
        [
            user_prompt(MATE_A, "Research the A market", -500),
            call(MATE_A, "m1", "WebFetch", {"url": "https://example.com/a"}, -2),
        ],
    )
    mate(
        MATE_B,
        "lane-b",
        [
            user_prompt(MATE_B, "Research the B market", -500),
            answer(MATE_B, "Done. The B report is written.", -300),
        ],
    )
    # Claude Code starts each split-pane teammate as its own process with these flags.
    procs(
        sys.executable,
        "-c",
        "import time; time.sleep(300)",
        "--agent-id",
        "lane-a@team-x",
        "--agent-name",
        "lane-a",
        "--team-name",
        "team-x",
        "--parent-session-id",
        lead,
    )


def teammates(port, lead_name="alpha-1"):
    subs = by_name(state(port), lead_name)["subagents"]
    return {s["name"]: s for s in subs if s["name"] in ("lane-a", "lane-b")}


def test_split_pane_teammates_show_under_their_lead(server, home, procs):  # noqa: F811
    make_team(home, procs)
    mates = wait_for(lambda: len(teammates(server)) == 2 and teammates(server))
    a, b = mates["lane-a"], mates["lane-b"]
    assert a["kind"] == "teammate" and b["kind"] == "teammate"
    assert a["state"] == "running", "its process runs and its tool call is open"
    assert b["state"] != "running", "no process runs for lane-b"
    assert a["description"] == "Research the A market"
    assert a["agent_type"] == "researcher" and a["model"] == "sonnet"
    assert a["tool_calls"] >= 1 and a["last_action"]["tool"] == "WebFetch"
    assert a["agent_id"] == MATE_A
    ids = {x.get("session_id") for x in state(server)["agents"]}
    assert MATE_A not in ids and MATE_B not in ids, "a teammate is not a session"


def test_a_team_of_a_dead_lead_shows_nowhere(server, home, procs):  # noqa: F811
    make_team(home, procs, lead="sid-gone")
    time.sleep(3)
    for a in state(server)["agents"]:
        names = {s["name"] for s in a["subagents"]}
        assert not names & {"lane-a", "lane-b"}, a["name"]


# Steering notes


def test_steer_route(server, home):  # noqa: F811
    ctl = home["control"]
    code, _ = post(
        server, "/api/steer", {"session_id": "sid-alpha", "text": "Use the staging DB."}
    )
    assert code == 200
    note = json.loads((ctl / "notes" / "sid-alpha.json").read_text())
    assert note["text"] == "Use the staging DB." and note["agent_id"] is None
    assert note["session_id"] == "sid-alpha" and note["id"]
    assert (ctl / "notes" / ".any").exists()
    assert wait_for(lambda: by_name(state(server), "alpha-1")["note"]["pending"])

    code, _ = post(
        server,
        "/api/steer",
        {"session_id": "sid-alpha", "agent_id": "aaa", "text": "Skip the old files."},
    )
    assert code == 200
    assert json.loads((ctl / "notes" / "aaa.json").read_text())["agent_id"] == "aaa"
    sub = wait_for(
        lambda: next(
            s
            for s in by_name(state(server), "alpha-1")["subagents"]
            if s["agent_id"] == "aaa" and s["note"]["pending"]
        )
    )
    assert sub

    assert (
        post(server, "/api/steer", {"session_id": "sid-alpha", "text": "  "})[0] == 400
    )
    assert post(server, "/api/steer", {"session_id": "nope", "text": "x"})[0] == 404
    assert (
        post(
            server,
            "/api/steer",
            {"session_id": "sid-alpha", "agent_id": "zzz", "text": "x"},
        )[0]
        == 404
    )


def test_steer_refuses_a_target_that_runs_no_tool_call(server):  # noqa: F811
    # beta-1 ended its turn and subagent "callers" (bbb) finished, so a note would never arrive.
    assert post(server, "/api/steer", {"session_id": "sid-beta", "text": "x"})[0] == 409
    assert (
        post(
            server,
            "/api/steer",
            {"session_id": "sid-alpha", "agent_id": "bbb", "text": "x"},
        )[0]
        == 409
    )


def test_a_note_that_never_arrived_goes_when_the_turn_ends(server, home):  # noqa: F811
    ctl = home["control"]
    assert (
        post(server, "/api/steer", {"session_id": "sid-alpha", "text": "Late note."})[0]
        == 200
    )
    end_turn(home)
    wait_for(lambda: not (ctl / "notes" / "sid-alpha.json").exists(), timeout=10)
    assert wait_for(
        lambda: not by_name(state(server), "alpha-1")["note"]["pending"] or "ok"
    )
    wait_for(lambda: not (ctl / "notes" / ".any").exists(), timeout=10)


def note(ctl, key, sid, text, agent_id=None):
    drop(
        ctl / "notes",
        f"{key}.json",
        {
            "id": f"n-{key}",
            "session_id": sid,
            "agent_id": agent_id,
            "text": text,
            "at_ms": now_ms(),
        },
    )
    (ctl / "notes" / ".any").touch()


def pre(tmp_path, sid, agent_id=None):
    payload = {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": "ls"}}
    if agent_id:
        payload["agent_id"] = agent_id
    out, _ = finish(start_hook("pre", payload, tmp_path))
    return json.loads(out) if out.strip() else None


def test_the_pre_hook_delivers_a_note_to_the_main_thread_once(tmp_path):
    note(tmp_path, "s1", "s1", "Use the staging DB.")
    assert pre(tmp_path, "s2") is None, "another session gets nothing"
    assert pre(tmp_path, "s1", agent_id="aaa") is None, "a subagent of s1 gets nothing"
    out = pre(tmp_path, "s1")
    h = out["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse"
    assert "Use the staging DB." in h["additionalContext"]
    assert "permissionDecision" not in h, "a note never blocks the call"
    assert not (tmp_path / "notes" / "s1.json").exists()
    receipt = json.loads((tmp_path / "notes" / "delivered" / "s1.json").read_text())
    assert receipt["id"] == "n-s1" and receipt["delivered_ms"] > 0
    assert pre(tmp_path, "s1") is None, "a note arrives once"


def test_the_pre_hook_delivers_a_subagent_note_only_to_that_subagent(tmp_path):
    note(tmp_path, "aaa", "s1", "Skip the old files.", agent_id="aaa")
    assert pre(tmp_path, "s1") is None
    assert pre(tmp_path, "s1", agent_id="bbb") is None
    out = pre(tmp_path, "s1", agent_id="aaa")
    assert "Skip the old files." in out["hookSpecificOutput"]["additionalContext"]


def test_a_delivered_note_shows_on_the_page(server, home):  # noqa: F811
    ctl = home["control"]
    assert (
        post(server, "/api/steer", {"session_id": "sid-alpha", "text": "Use staging."})[
            0
        ]
        == 200
    )
    nid = json.loads((ctl / "notes" / "sid-alpha.json").read_text())["id"]
    (ctl / "notes" / "sid-alpha.json").unlink()
    drop(
        ctl / "notes" / "delivered",
        "sid-alpha.json",
        {"id": nid, "delivered_ms": now_ms()},
    )
    got = wait_for(
        lambda: (n := by_name(state(server), "alpha-1")["note"])["delivered_ms"] and n
    )
    assert got["pending"] is False


# Stopping a whole session


def test_stop_a_whole_session_route(server, home):  # noqa: F811
    ctl = home["control"]
    assert post(server, "/api/stop", {"session_id": "sid-alpha"})[0] == 200
    assert (ctl / "stop" / "session-sid-alpha.json").exists()
    assert (ctl / "stop" / ".any").exists()
    assert wait_for(lambda: by_name(state(server), "alpha-1")["stop_requested"])
    assert post(server, "/api/stop", {"session_id": "sid-beta"})[0] == 409, (
        "its turn ended"
    )
    assert post(server, "/api/stop", {"session_id": "nope"})[0] == 404


def test_a_session_stop_clears_when_the_turn_ends(server, home):  # noqa: F811
    ctl = home["control"]
    assert post(server, "/api/stop", {"session_id": "sid-alpha"})[0] == 200
    end_turn(home)
    wait_for(lambda: not (ctl / "stop" / "session-sid-alpha.json").exists(), timeout=10)
    assert wait_for(
        lambda: not by_name(state(server), "alpha-1")["stop_requested"] or "ok"
    )


def test_a_reply_clears_a_pending_session_stop(server, home, procs):  # noqa: F811
    ctl = home["control"]
    assert post(server, "/api/stop", {"session_id": "sid-alpha"})[0] == 200
    drop(ctl / "waiting", "sid-alpha.json", {"pid": procs(), "session_id": "sid-alpha"})
    wait_for(lambda: by_name(state(server), "alpha-1")["reply_ready"])
    assert (
        post(server, "/api/reply", {"session_id": "sid-alpha", "text": "Go on."})[0]
        == 200
    )
    assert not (ctl / "stop" / "session-sid-alpha.json").exists(), (
        "a reply wakes the session, so the old stop must not end it again"
    )


def test_the_pre_hook_ends_a_stopped_sessions_turn_once(tmp_path):
    drop(tmp_path / "stop", "session-s1.json", {"session_id": "s1", "at_ms": now_ms()})
    (tmp_path / "stop" / ".any").touch()
    assert pre(tmp_path, "s2") is None
    sub = pre(tmp_path, "s1", agent_id="aaa")
    assert sub["hookSpecificOutput"]["permissionDecision"] == "deny"
    out = pre(tmp_path, "s1")
    assert out["continue"] is False and out["stopReason"]
    assert not (tmp_path / "stop" / "session-s1.json").exists(), "the stop is used once"
    assert pre(tmp_path, "s1") is None


def test_the_old_stop_mode_name_still_works(tmp_path):
    drop(
        tmp_path / "stop",
        "aaa.json",
        {"session_id": "s1", "agent_id": "aaa", "at_ms": now_ms()},
    )
    payload = {
        "session_id": "s1",
        "agent_id": "aaa",
        "tool_name": "Bash",
        "tool_input": {},
    }
    out, _ = finish(start_hook("stop", payload, tmp_path))
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_pre_hook_fails_open_on_bad_input(tmp_path):
    p = subprocess.run(
        [sys.executable, str(ROOT / "agentforeman" / "agentforeman_hook.py"), "pre"],
        input="not json",
        capture_output=True,
        text=True,
        env={**os.environ, "AGENTFOREMAN_HOME": str(tmp_path)},
        timeout=5,
    )
    assert (p.returncode, p.stdout) == (0, "")


def test_the_installed_pre_command_runs_for_a_note_and_skips_when_idle(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    control = tmp_path / "ctl"
    assert run_install(settings, control).returncode == 0
    groups = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    cmd = next(
        h["command"]
        for g in groups
        for h in g["hooks"]
        if "agentforeman-hook" in h["command"] and g.get("matcher", "") == ""
    )
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "tool_input": {}})
    t = time.time()
    r = subprocess.run(
        ["/bin/bash", "-c", cmd], input=payload, capture_output=True, text=True
    )
    assert (r.returncode, r.stdout) == (0, "") and time.time() - t < 1, (
        "nothing pending"
    )
    note(control, "s1", "s1", "Check the logs first.")
    r = subprocess.run(
        ["/bin/bash", "-c", cmd],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (
        "Check the logs first."
        in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    )


# The repeat flag


def test_a_session_that_repeats_one_read_is_flagged(server, home):  # noqa: F811
    eps = home["claude"] / "projects" / enc("/work/epsilon") / "sid-eps-new.jsonl"
    rows = []
    for i in range(5):
        rows += [
            call(
                "sid-eps", f"rp{i}", "Read", {"file_path": "/work/epsilon/config.toml"}
            ),
            result("sid-eps", f"rp{i}"),
        ]
    append(eps, *rows)
    flag = wait_for(lambda: by_name(state(server), "eps-1")["repeating"])
    assert flag["tool"] == "Read" and flag["count"] >= 4
    assert "config.toml" in flag["target"]
    assert by_name(state(server), "alpha-1")["repeating"] is None


def test_varied_reads_are_not_flagged(server, home):  # noqa: F811
    eps = home["claude"] / "projects" / enc("/work/epsilon") / "sid-eps-new.jsonl"
    rows = []
    for i in range(6):
        rows += [
            call("sid-eps", f"vr{i}", "Read", {"file_path": f"/work/epsilon/f{i}.py"}),
            result("sid-eps", f"vr{i}"),
        ]
    append(eps, *rows)

    def last_summary():
        la = by_name(state(server), "eps-1").get("last_action") or {}
        return str(la.get("summary") or "").endswith("f5.py")

    wait_for(last_summary)
    assert by_name(state(server), "eps-1")["repeating"] is None


def test_the_permission_hook_is_unchanged_by_a_pending_note(tmp_path):
    # A note waits for PreToolUse. It must not answer a permission prompt.
    note(tmp_path, "s1", "s1", "Use staging.")
    payload = {
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "sudo -n true"},
    }
    p = start_hook("permission", payload, tmp_path)
    wait_for(lambda: list((tmp_path / "requests").glob("*.json")), timeout=5)
    assert (tmp_path / "notes" / "s1.json").exists()
    p.send_signal(signal.SIGTERM)
    p.wait(5)
