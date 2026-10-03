"""Regression tests for the accuracy audit: one busy lead session with many kinds of subagents.

Fixture facts: lead-1 is busy since 60 min ago. Its own last entry is 15 min old, but its "bld"
subagent wrote 5 s ago. "qop" finished by a notice that sits only in a queue-operation row.
"res" got an old "completed" notice and then resumed. "ux-qa" is an Agent Teams teammate.
"err" got an is_error result. Twelve older fillers push the folder past the 12-row cap.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest
from test_acceptance import (
    ROOT,
    enc,
    free_port,
    iso,
    ms,
    prompt,
    register,
    reply,
    signed,
    state,
    tool_call,
    tool_result,
    wait_for,
    write_jsonl,
)

SID = "sid-lead"
LONG_REPLY = (
    "Five builders still work.\n\n"
    + "The branch holds the parser change and the new tests. " * 8
    + "Want me to merge the branch?"
)


def notice(task_id, tool_use_id, status="completed"):
    return (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n<status>{status}</status>\n</task-notification>"
    )


@pytest.fixture(scope="module")
def lead_home(tmp_path_factory):
    root = tmp_path_factory.mktemp("lead")
    claude, codex = root / "claude", root / "codex"
    (claude / "sessions").mkdir(parents=True)
    proc = subprocess.Popen(["sleep", "600"])
    (claude / "sessions" / f"{proc.pid}.json").write_text(
        json.dumps(
            {
                "pid": proc.pid,
                "sessionId": SID,
                "cwd": "/work/lead",
                "name": "lead-1",
                "status": "busy",
                "entrypoint": "claude-vscode",
                "startedAt": ms(-7200),
                "statusUpdatedAt": ms(-3600),
            }
        )
    )
    proj = claude / "projects" / enc("/work/lead")
    write_jsonl(
        proj / f"{SID}.jsonl",
        [
            prompt(SID, "Build the parser change", -4000),
            {
                "type": "user",
                "isMeta": True,
                "sessionId": SID,
                "timestamp": iso(-3990),
                "message": {
                    "role": "user",
                    "content": "Base directory for this skill: /x",
                },
            },
            tool_call(SID, "toolu_b1", "Agent", {"description": "Build part B"}, -3500),
            tool_result(SID, "toolu_b1", -3499, "Async agent launched"),
            tool_call(SID, "toolu_q1", "Agent", {"description": "Queue check"}, -3400),
            tool_result(SID, "toolu_q1", -3399, "Async agent launched"),
            tool_call(SID, "toolu_r1", "Agent", {"description": "Resumed task"}, -3300),
            tool_result(SID, "toolu_r1", -3299, "Async agent launched"),
            tool_call(SID, "toolu_e1", "Agent", {"description": "Failing task"}, -3200),
            {
                "type": "user",
                "sessionId": SID,
                "timestamp": iso(-3000),
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_e1",
                            "content": "boom",
                            "is_error": True,
                        }
                    ],
                },
            },
            reply(SID, LONG_REPLY, -1800),
            {"type": "ai-title", "aiTitle": "Parser change build", "sessionId": SID},
            {"type": "custom-title", "customTitle": "Parser lead", "sessionId": SID},
            {"type": "ai-title", "aiTitle": "Parser change build v2", "sessionId": SID},
            {
                "type": "user",
                "sessionId": SID,
                "timestamp": iso(-900),
                "message": {"role": "user", "content": notice("res", "toolu_r1")},
            },
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "timestamp": iso(-100),
                "content": notice("qop", "toolu_other"),
            },
        ],
    )
    subs = proj / SID / "subagents"
    subs.mkdir(parents=True)

    def subagent(agent_id, meta, rows, age_s):
        write_jsonl(subs / f"agent-{agent_id}.jsonl", rows)
        (subs / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
        t = time.time() - age_s
        os.utime(subs / f"agent-{agent_id}.jsonl", (t, t))

    bg = {
        "model": "sonnet",
        "agentType": "general-purpose",
        "requestShape": "background",
    }
    bash = {"command": "cd /work/lead && make build", "description": "Run the build"}
    subagent(
        "bld",
        {**bg, "name": "bld", "description": "Build part B", "toolUseId": "toolu_b1"},
        [tool_call(SID, "b-1", "Bash", bash, -5)],
        5,
    )
    subagent(
        "res",
        {**bg, "name": "res", "description": "Resumed task", "toolUseId": "toolu_r1"},
        [tool_call(SID, "r-1", "Grep", {"pattern": "TODO"}, -20)],
        5,
    )
    subagent(
        "qop",
        {**bg, "name": "qop", "description": "Queue check", "toolUseId": "toolu_q1"},
        [tool_call(SID, "q-1", "Read", {"file_path": "/work/lead/a.py"}, -200)],
        60,
    )
    subagent(
        "tm",
        {
            "name": "ux-qa",
            "agentType": "ux-qa",
            "model": "opus",
            "taskKind": "in_process_teammate",
            "teamName": "session-lead",
        },
        [reply(SID, "Waiting for the next task.", -300)],
        300,
    )
    subagent(
        "err",
        {
            "model": "sonnet",
            "agentType": "general-purpose",
            "name": "err",
            "description": "Failing task",
            "toolUseId": "toolu_e1",
        },
        [reply(SID, "It failed.", -3000)],
        3000,
    )
    subagent(
        "stp",
        {**bg, "name": "stp", "description": "Stopped task", "toolUseId": "toolu_s1"},
        [
            tool_call(SID, "s-1", "Bash", {"command": "sleep 900"}, -40),
            tool_result(
                SID, "s-1", -35, "The user doesn't want to proceed with this tool use."
            ),
            {
                "type": "user",
                "sessionId": SID,
                "timestamp": iso(-35),
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "[Request interrupted by user for tool use]",
                        }
                    ],
                },
            },
        ],
        30,
    )
    for i in range(12):
        subagent(
            f"f{i:02d}",
            {
                **bg,
                "name": f"f{i:02d}",
                "description": f"Filler {i}",
                "toolUseId": f"toolu_f{i}",
            },
            [reply(SID, "Done.", -4000 - i)],
            4000 + i,
        )

    codex.mkdir()
    db = sqlite3.connect(codex / "state_5.sqlite")
    db.execute(
        "create table threads (id text, title text, cwd text, model text, updated_at_ms integer,"
        " archived integer, thread_source text, agent_nickname text)"
    )
    db.executemany(
        "insert into threads values (?,?,?,?,?,?,?,?)",
        [
            ("th-p", "Research task", "/work/lead", "gpt-6", ms(-600), 0, "user", None),
            (
                "th-c",
                "Need you to fan out",
                "/work/lead",
                "gpt-6",
                ms(-500),
                0,
                "subagent",
                "Ohm",
            ),
        ],
    )
    db.execute(
        "create table thread_spawn_edges (child_thread_id text, parent_thread_id text)"
    )
    db.execute("insert into thread_spawn_edges values ('th-c', 'th-p')")
    db.commit()
    db.close()

    port = free_port()
    register(port, root / "control")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentforeman",
            "serve",
            "--port",
            str(port),
            "--claude-dir",
            str(claude),
            "--codex-dir",
            str(codex),
            "--control-dir",
            str(root / "control"),
            "--dry-open",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: state(port), timeout=20)
        yield port
    finally:
        server.terminate()
        server.wait(5)
        proc.kill()
        proc.wait()


@pytest.fixture(scope="module")
def lead(lead_home):
    st = state(lead_home)
    return st, st["agents"][0], {s["name"]: s for s in st["agents"][0]["subagents"]}


def test_busy_session_with_a_working_subagent_is_not_stale(lead):
    _, a, _ = lead
    assert a["status"] == "working"
    assert abs(a["since_ms"] - ms(-3600)) < 1500, "since is when the busy state began"
    assert abs(a["last_entry_ms"] - ms(-900)) < 1500
    assert a["last_activity_ms"] > time.time() * 1000 - 60_000, (
        "a subagent write 5 s ago counts as session activity"
    )


def test_notice_in_a_queue_row_finishes_the_subagent(lead):
    q = lead[2]["qop"]
    assert (q["state"], q["outcome"]) == ("done", "completed")


def test_resumed_subagent_with_an_old_notice_stays_running(lead):
    assert lead[2]["res"]["state"] == "running"


def test_teammate_is_never_done(lead):
    t = lead[2]["ux-qa"]
    assert (t["kind"], t["state"], t["outcome"]) == ("teammate", "idle", None)


def test_error_result_marks_the_subagent_failed(lead):
    e = lead[2]["err"]
    assert (e["state"], e["outcome"]) == ("done", "failed")


def test_subagent_cap_reports_the_full_count(lead):
    _, a, subs = lead
    assert len(a["subagents"]) == 12 and a["subagents_total"] == 18
    assert {"bld", "res", "qop", "ux-qa", "err", "stp"} <= set(subs)


def test_interrupted_subagent_is_killed_at_once(lead):
    s = lead[2]["stp"]
    assert (s["state"], s["outcome"]) == ("done", "killed")
    assert abs(s["ended_ms"] - ms(-35)) < 1500


def test_bash_summary_drops_cd_and_keeps_the_description(lead):
    act = lead[2]["bld"]["last_action"]
    assert act["summary"] == "make build" and act["desc"] == "Run the build"


def test_title_prefers_the_custom_name(lead):
    assert lead[1]["title"] == "Parser lead" and lead[1]["name"] == "lead-1"


def test_skill_text_is_not_the_latest_request(lead):
    assert lead[1]["last_prompt"] == "Build the parser change"


def test_cd_prefix_forms_are_dropped():
    sys.path.insert(0, str(ROOT))
    from agentforeman.collector import summarize

    for cmd in ("cd /a && make", "cd /a; make", "cd /a\nmake", 'cd "/a b" && make'):
        assert summarize("Bash", {"command": cmd}) == "make", cmd


def test_last_message_keeps_paragraphs_and_the_closing_question(lead):
    msg = lead[1]["last_message"]
    assert len(msg) > 280 and "\n\n" in msg
    assert msg.endswith("Want me to merge the branch?")


def test_codex_subagent_thread_nests_under_its_parent(lead):
    child = next(t for t in lead[0]["codex"] if t["thread_id"] == "th-c")
    assert child["parent_thread_id"] == "th-p"
    assert (child["source"], child["nickname"]) == ("subagent", "Ohm")


def test_page_shows_the_audited_states(lead_home):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page(viewport={"width": 1440, "height": 900})
            base = f"http://127.0.0.1:{lead_home}"
            pg.goto(signed(base + "/#sessions"))
            pg.wait_for_selector("table.sessions tr[data-sid]")
            assert pg.text_content("table.sessions tr[data-sid] .badge") == "Running"
            assert (
                pg.text_content("table.sessions tr[data-sid] .cell-main")
                == "Parser lead"
            )
            assert "lead-1" in pg.text_content("table.sessions tr[data-sid] .cell-sub")
            pg.goto(base + "/#overview")
            pg.wait_for_selector(".kpi[data-filter=running]")
            tile = pg.text_content(".kpi[data-filter=running] .s")
            assert tile == "2 subagents working", "bld and res run, the rest finished"
            pg.goto(base + "/#subagents")
            pg.wait_for_selector("tr.group")
            assert "12 of 18 shown" in pg.text_content("tr.group")
            badges = dict(
                pg.eval_on_selector_all(
                    "tr[data-aid]",
                    "els => els.map(e => [e.dataset.aid, e.querySelector('.badge').textContent])",
                )
            )
            assert badges["qop"] == "Done" and badges["err"] == "Failed"
            assert badges["stp"] == "Killed"
            assert badges["tm"] == "Teammate · idle" and badges["res"] == "Running"
            pg.goto(base + "/#codex")
            pg.wait_for_selector("#content tr.child")
            assert "Ohm" in pg.text_content("#content tr.child")
        finally:
            browser.close()
