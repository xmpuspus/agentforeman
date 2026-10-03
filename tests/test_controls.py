"""Approve, reply, and stop: the hooks, the installer, and the server routes. SPEC.md, section Controls."""

import http.client
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

import pytest
from test_acceptance import (  # noqa: F401
    ROOT,
    by_name,
    get,
    home,
    page_token,
    post,
    server,
    state,
    token_for,
    wait_for,
)

HOOK = ROOT / "agentforeman" / "agentforeman_hook.py"
INSTALL = ROOT / "agentforeman" / "install.py"


def hook_env(control, attended="1"):
    return {
        **os.environ,
        "AGENTFOREMAN_HOME": str(control),
        "CLAUDE_CODE_SESSION_ATTENDED": attended,
        "AGENTFOREMAN_PERMISSION_GRACE_S": "0.1",
    }


def start_hook(mode, payload, control, attended="1"):
    p = subprocess.Popen(
        [sys.executable, str(HOOK), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=hook_env(control, attended),
    )
    p.stdin.write(json.dumps(payload))
    p.stdin.close()
    return p


def finish(p, timeout=5):
    """Waits for a hook started with start_hook and returns its stdout and stderr."""
    p.wait(timeout)
    return p.stdout.read(), p.stderr.read()


def one_file(folder):
    return wait_for(lambda: next(iter(sorted(folder.glob("*.json"))), None), timeout=5)


SUDO = {
    "session_id": "sid-alpha",
    "tool_name": "Bash",
    "tool_input": {"command": "sudo -n true", "description": "Check sudo"},
}


# The permission hook


@pytest.mark.parametrize("behavior", ["allow", "deny"])
def test_permission_hook_waits_for_the_portal_decision(tmp_path, behavior):
    p = start_hook("permission", SUDO, tmp_path)
    req = one_file(tmp_path / "requests")
    data = json.loads(req.read_text())
    assert data["pid"] == p.pid and data["tool_input"]["command"] == "sudo -n true"
    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions" / req.name).write_text(
        json.dumps({"behavior": behavior, "message": "No sudo today."})
    )
    out, _ = finish(p)
    decision = json.loads(out)["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == behavior
    if behavior == "deny":
        assert decision["message"] == "No sudo today."
    assert not list((tmp_path / "requests").glob("*.json")), (
        "the hook cleans up its request"
    )


def test_permission_hook_steps_aside_when_nobody_can_answer(tmp_path):
    t = time.time()
    p = start_hook("permission", SUDO, tmp_path, attended="0")
    out, _ = finish(p)
    assert out == "" and p.returncode == 0 and time.time() - t < 3
    assert not (tmp_path / "requests").exists(), "a claude -p run must never wait"


def test_permission_hook_cleans_up_when_answered_locally(tmp_path):
    p = start_hook("permission", SUDO, tmp_path)
    req = one_file(tmp_path / "requests")
    p.send_signal(
        signal.SIGTERM
    )  # Claude Code ends the hook when the dialog is answered
    p.wait(5)
    assert not req.exists()


def call_row(tool_use_id, tool_input=SUDO["tool_input"]):
    block = {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": tool_input}
    return {"type": "assistant", "message": {"content": [block]}}


def result_row(tool_use_id):
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": "done"}
    return {"type": "user", "message": {"content": [block]}}


def add_rows(path, *rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)


def test_permission_hook_ends_when_answered_in_the_claude_app(tmp_path):
    # An answer in the Claude app runs the tool but leaves the hook running, so the hook
    # watches for the call's result and removes its request.
    t = tmp_path / "s.jsonl"
    add_rows(t, call_row("toolu_old", {"command": "ls"}), call_row("toolu_sudo"))
    p = start_hook("permission", {**SUDO, "transcript_path": str(t)}, tmp_path)
    req = one_file(tmp_path / "requests")
    add_rows(t, result_row("toolu_other"))
    time.sleep(2)
    assert p.poll() is None and req.exists(), "another call's result is not an answer"
    add_rows(t, result_row("toolu_sudo"))
    out, _ = finish(p)
    assert p.returncode == 0 and out == "" and not req.exists()


def test_permission_hook_watches_the_subagent_transcript(tmp_path):
    t = tmp_path / "sid-alpha.jsonl"
    sub = tmp_path / "sid-alpha" / "subagents" / "agent-aaa.jsonl"
    add_rows(t, call_row("toolu_parent", {"command": "make"}))
    add_rows(sub, call_row("toolu_sub"))
    payload = {**SUDO, "transcript_path": str(t), "agent_id": "aaa"}
    p = start_hook("permission", payload, tmp_path)
    req = one_file(tmp_path / "requests")
    assert json.loads(req.read_text())["agent_id"] == "aaa"
    add_rows(sub, result_row("toolu_sub"))
    finish(p)
    assert not req.exists()


def test_permission_hook_skips_a_call_answered_during_the_grace_time(tmp_path):
    t = tmp_path / "s.jsonl"
    add_rows(t, call_row("toolu_sudo"), result_row("toolu_sudo"))
    p = start_hook("permission", {**SUDO, "transcript_path": str(t)}, tmp_path)
    out, _ = finish(p)
    assert out == "" and not (tmp_path / "requests").exists()


# The reply hook


def reply_payload(tmp_path, sid="sid-beta"):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"content": []}}) + "\n"
    )
    return {
        "session_id": sid,
        "transcript_path": str(transcript),
        "hook_event_name": "Stop",
    }, transcript


def test_reply_hook_delivers_the_reply_and_exits_2(tmp_path):
    payload, _ = reply_payload(tmp_path)
    p = start_hook("reply", payload, tmp_path)
    waiting = one_file(tmp_path / "waiting")
    assert json.loads(waiting.read_text())["pid"] == p.pid
    (tmp_path / "replies").mkdir()
    (tmp_path / "replies" / "sid-beta.json").write_text(
        json.dumps({"id": "r1", "text": "Yes, open the PR."})
    )
    _, err = finish(p)
    assert p.returncode == 2, "exit 2 wakes the model"
    assert err == "Reply from the user, sent from AgentForeman:\n\nYes, open the PR."
    assert (
        not waiting.exists() and not (tmp_path / "replies" / "sid-beta.json").exists()
    )


def stamped(seconds_from_now):
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + seconds_from_now)
    )


def test_reply_hook_ends_when_the_user_types_in_the_session(tmp_path):
    payload, transcript = reply_payload(tmp_path)
    p = start_hook("reply", payload, tmp_path)
    waiting = one_file(tmp_path / "waiting")
    row = {"type": "user", "timestamp": stamped(3), "message": {"content": "next"}}
    with open(transcript, "a") as f:
        f.write(json.dumps(row) + "\n")
    p.wait(5)
    assert p.returncode == 0 and not waiting.exists()


def test_reply_hook_ignores_the_turns_own_late_reply_line(tmp_path):
    payload, transcript = reply_payload(tmp_path)
    p = start_hook("reply", payload, tmp_path)
    waiting = one_file(tmp_path / "waiting")
    # Claude Code writes the turn's last reply just after the hook starts, stamped before it.
    row = {"type": "assistant", "timestamp": stamped(-1), "message": {"content": []}}
    with open(transcript, "a") as f:
        f.write(json.dumps(row) + "\n")
    time.sleep(2.5)
    assert p.poll() is None and waiting.exists(), "the hook still waits for a reply"
    p.send_signal(signal.SIGTERM)
    p.wait(5)


def test_a_newer_reply_hook_takes_over(tmp_path):
    payload, _ = reply_payload(tmp_path)
    first = start_hook("reply", payload, tmp_path)
    one_file(tmp_path / "waiting")
    second = start_hook("reply", payload, tmp_path)
    wait_for(
        lambda: (
            json.loads((tmp_path / "waiting" / "sid-beta.json").read_text())["pid"]
            == second.pid
        )
    )
    first.wait(5)
    assert first.returncode == 0 and second.poll() is None
    second.send_signal(signal.SIGTERM)
    second.wait(5)
    assert not (tmp_path / "waiting" / "sid-beta.json").exists()


def test_reply_hook_steps_aside_when_nobody_can_answer(tmp_path):
    payload, _ = reply_payload(tmp_path)
    p = start_hook("reply", payload, tmp_path, attended="0")
    p.wait(5)
    assert p.returncode == 0 and not (tmp_path / "waiting").exists()


# The stop hook


@pytest.mark.parametrize(
    "agent_id,denied", [("aaa", True), ("bbb", False), (None, False)]
)
def test_stop_hook_denies_only_the_stopped_subagent(tmp_path, agent_id, denied):
    (tmp_path / "stop").mkdir()
    (tmp_path / "stop" / "aaa.json").write_text("{}")
    payload = {
        "session_id": "s",
        "tool_name": "Bash",
        "tool_input": {},
        "agent_id": agent_id,
    }
    out, _ = finish(start_hook("stop", payload, tmp_path))
    if denied:
        hso = json.loads(out)["hookSpecificOutput"]
        assert (
            hso["permissionDecision"] == "deny"
            and "Do not call any more tools" in hso["permissionDecisionReason"]
        )
    else:
        assert out == ""


@pytest.mark.parametrize("mode", ["permission", "reply", "stop"])
def test_every_hook_fails_open_on_bad_input(tmp_path, mode):
    p = subprocess.run(
        [sys.executable, str(HOOK), mode],
        input="not json",
        capture_output=True,
        text=True,
        env=hook_env(tmp_path),
        timeout=5,
    )
    assert (p.returncode, p.stdout) == (0, "")


# The installer


def run_install(settings, control, *extra):
    return subprocess.run(
        [
            sys.executable,
            str(INSTALL),
            "--settings",
            str(settings),
            "--control-dir",
            str(control),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_install_adds_four_hooks_and_uninstall_removes_only_them(tmp_path):
    settings = tmp_path / "settings.json"
    mine = {
        "type": "command",
        "command": "python3 ~/.claude/hooks/permission-autoapprove.py",
        "timeout": 10,
    }
    original = {
        "permissions": {"defaultMode": "bypassPermissions"},
        "hooks": {"PermissionRequest": [{"matcher": "", "hooks": [mine]}]},
    }
    settings.write_text(json.dumps(original, indent=2) + "\n")
    control = tmp_path / "ctl"
    for _ in range(2):  # a second install replaces, never doubles
        r = run_install(settings, control)
        assert r.returncode == 0, r.stderr
    s = json.loads(settings.read_text())
    ours = [
        h
        for groups in s["hooks"].values()
        for g in groups
        for h in g["hooks"]
        if "agentforeman-hook" in h["command"]
    ]
    assert len(ours) == 4, "permission, reply, pre, and the edit guard"
    assert mine in s["hooks"]["PermissionRequest"][0]["hooks"], "existing hooks stay"
    stop = s["hooks"]["Stop"][0]["hooks"][0]
    assert stop["asyncRewake"] is True and stop["timeout"] == 21600
    assert (
        control / "hooks" / "agentforeman_hook.py"
    ).read_bytes() == HOOK.read_bytes()
    assert json.loads((control / "installed.json").read_text())["hook_sha256"]
    assert len(list(tmp_path.glob("settings.json.bak-*"))) == 2
    r = run_install(settings, control, "--uninstall")
    assert r.returncode == 0 and json.loads(settings.read_text()) == original
    assert not (control / "installed.json").exists()


def test_installed_commands_are_safe_when_idle_or_missing(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    control = tmp_path / "ctl"
    run_install(settings, control)
    cmds = {
        e: g[0]["hooks"][0]["command"]
        for e, g in json.loads(settings.read_text())["hooks"].items()
    }
    t = time.time()
    r = subprocess.run(
        ["/bin/bash", "-c", cmds["PreToolUse"]],
        input='{"agent_id": "x"}',
        capture_output=True,
        text=True,
    )
    assert (r.returncode, r.stdout) == (0, "") and time.time() - t < 1, (
        "no stop pending: exit at once"
    )
    (control / "hooks" / "agentforeman_hook.py").unlink()
    (control / "stop").mkdir()
    (control / "stop" / ".any").touch()
    for event, cmd in cmds.items():
        r = subprocess.run(
            ["/bin/bash", "-c", cmd],
            input="{}",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert (r.returncode, r.stdout) == (0, ""), (
            f"{event} must not block when the hook file is gone"
        )


def test_install_refuses_settings_that_do_not_parse(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{broken")
    r = run_install(settings, tmp_path / "ctl")
    assert r.returncode != 0 and settings.read_text() == "{broken"


# The server


def live_pid(home):  # noqa: F811
    p = subprocess.Popen(["sleep", "300"])
    home.setdefault("extra", []).append(p)
    return p.pid


@pytest.fixture
def ctl(home):  # noqa: F811
    yield home["control"]
    for p in home.get("extra", []):
        p.kill()
        p.wait()


def drop(folder, name, data):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data))


def raw(port, method, path, headers, body=b"{}"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body if method == "POST" else None,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_post_needs_the_token_the_host_and_the_origin(server):  # noqa: F811
    good = {"Content-Type": "application/json", "X-Foreman-Token": page_token(server)}
    body = json.dumps({"session_id": "sid-alpha"}).encode()
    assert raw(server, "POST", "/api/open", good, body) == 200
    assert (
        raw(server, "POST", "/api/open", {**good, "X-Foreman-Token": "nope"}, body)
        == 403
    )
    assert (
        raw(server, "POST", "/api/open", {"Content-Type": "application/json"}, body)
        == 403
    )
    assert (
        raw(
            server,
            "POST",
            "/api/open",
            {**good, "Origin": "https://evil.example"},
            body,
        )
        == 403
    )
    assert (
        raw(server, "POST", "/api/open", {**good, "Host": "evil.example:80"}, body)
        == 403
    )
    assert raw(server, "GET", "/api/state", {"Host": "rebound.example"}) == 403
    assert page_token(server) not in get(server, "/api/state")[2].decode()


def test_a_refused_post_does_not_break_the_next_request(server):  # noqa: F811
    # The refusal leaves the body unread, so the server must close the connection.
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    body = json.dumps({"session_id": "sid-beta", "text": "x"})
    conn.request(
        "POST",
        "/api/reply",
        body,
        {"Content-Type": "application/json", "X-Foreman-Token": "old"},
    )
    r = conn.getresponse()
    r.read()
    assert r.status == 403 and r.getheader("Connection") == "close"
    conn.request("GET", "/api/state", headers={"X-Foreman-Token": token_for(server)})
    r = conn.getresponse()
    assert r.status == 200 and json.loads(r.read())["agents"]


def test_approve_flow(server, ctl, home):  # noqa: F811
    pid = live_pid(home)
    req = {
        "id": "req1",
        "pid": pid,
        "session_id": "sid-beta",
        "tool_name": "Bash",
        "tool_input": {"command": "sudo -n true", "description": "Check sudo"},
        "created_ms": int(time.time() * 1000),
    }
    drop(ctl / "requests", "req1.json", req)
    beta = wait_for(lambda: (b := by_name(state(server), "beta-1"))["approval"] and b)
    assert beta["status"] == "waiting" and beta["status_guess"] is False
    assert (
        beta["approval"]["detail"] == "sudo -n true"
        and beta["approval"]["desc"] == "Check sudo"
    )
    assert (
        post(server, "/api/approve", {"request_id": "req1", "behavior": "allow"})[0]
        == 200
    )
    assert (
        json.loads((ctl / "decisions" / "req1.json").read_text())["behavior"] == "allow"
    )
    assert (
        post(server, "/api/approve", {"request_id": "zzz", "behavior": "allow"})[0]
        == 404
    )
    assert (
        post(server, "/api/approve", {"request_id": "req1", "behavior": "maybe"})[0]
        == 400
    )


def test_a_request_without_its_hook_is_dropped(server, ctl):  # noqa: F811
    dead = subprocess.Popen(["true"])
    dead.wait()
    drop(
        ctl / "requests",
        "old.json",
        {
            "id": "old",
            "pid": dead.pid,
            "session_id": "sid-beta",
            "tool_name": "Bash",
            "tool_input": {},
        },
    )
    wait_for(lambda: not (ctl / "requests" / "old.json").exists())
    assert by_name(state(server), "beta-1")["approval"] is None


def test_reply_flow(server, ctl, home):  # noqa: F811
    assert (
        post(server, "/api/reply", {"session_id": "sid-beta", "text": "hi"})[0] == 409
    ), "no hook waits yet"
    drop(
        ctl / "waiting",
        "sid-beta.json",
        {"pid": live_pid(home), "session_id": "sid-beta"},
    )
    wait_for(lambda: by_name(state(server), "beta-1")["reply_ready"])
    assert (
        post(server, "/api/reply", {"session_id": "sid-beta", "text": "  "})[0] == 400
    )
    assert (
        post(
            server,
            "/api/reply",
            {"session_id": "sid-beta", "text": "Yes, open the PR."},
        )[0]
        == 200
    )
    assert (
        json.loads((ctl / "replies" / "sid-beta.json").read_text())["text"]
        == "Yes, open the PR."
    )
    assert wait_for(lambda: by_name(state(server), "beta-1")["reply_pending"])


def test_stop_flow(server, ctl):  # noqa: F811
    assert (
        post(server, "/api/stop", {"session_id": "sid-alpha", "agent_id": "bbb"})[0]
        == 409
    ), "callers already finished"
    assert (
        post(server, "/api/stop", {"session_id": "sid-alpha", "agent_id": "nope"})[0]
        == 404
    )
    assert (
        post(server, "/api/stop", {"session_id": "sid-alpha", "agent_id": "aaa"})[0]
        == 200
    )
    assert (ctl / "stop" / "aaa.json").exists() and (ctl / "stop" / ".any").exists()
    subs = wait_for(
        lambda: (
            {s["name"]: s for s in by_name(state(server), "alpha-1")["subagents"]}
            if any(
                s["stop_requested"]
                for s in by_name(state(server), "alpha-1")["subagents"]
            )
            else None
        )
    )
    assert subs["rules"]["stop_requested"] and not subs["logs"]["stop_requested"]


def test_controls_show_as_installed_only_with_the_current_hook(server, ctl):  # noqa: F811
    assert state(server)["controls_installed"] is None
    import hashlib

    sha = hashlib.sha256(HOOK.read_bytes()).hexdigest()
    drop(ctl, "installed.json", {"installed_ms": 1, "hook_sha256": sha})
    got = wait_for(lambda: state(server)["controls_installed"])
    assert got["current"] is True
    assert by_name(state(server), "alpha-1")["controls"] is True, (
        "the session started after the install"
    )


def test_an_update_and_reinstall_while_the_server_runs_reads_as_current(
    tmp_path, monkeypatch
):
    import hashlib

    from agentforeman import control

    hook = tmp_path / "agentforeman_hook.py"
    monkeypatch.setattr(control, "HOOK_SOURCE", hook)
    c = control.Control(tmp_path / "ctl")
    for version in ("v1", "v2 with a fix"):
        hook.write_text(version)
        sha = hashlib.sha256(version.encode()).hexdigest()
        drop(
            tmp_path / "ctl", "installed.json", {"installed_ms": 1, "hook_sha256": sha}
        )
        assert c.installed()["current"] is True, version
    hook.write_text("v3, not installed yet")
    assert c.installed()["current"] is False


def test_the_first_answer_wins_and_a_second_click_changes_nothing(tmp_path):
    import os

    from agentforeman import control

    c = control.Control(tmp_path / "ctl")
    request = {
        "id": "r1",
        "pid": os.getpid(),
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "make deploy"},
        "created_ms": 1,
    }
    drop(tmp_path / "ctl" / "requests", "r1.json", request)
    assert c.decide("r1", "deny") is None
    assert c.decide("r1", "allow") == "already answered"
    answer = json.loads((tmp_path / "ctl" / "decisions" / "r1.json").read_text())
    assert answer["behavior"] == "deny", "a later allow must not replace the deny"
    assert not list((tmp_path / "ctl" / "decisions").glob(".*.tmp"))
