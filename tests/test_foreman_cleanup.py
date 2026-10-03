# ruff: noqa: F811
"""A note or a stop for a session that ended can never arrive, so the server drops it at once.
Found in the live test on 2026-10-04: a note for a killed session stayed for its full 6 hours.
"""

import json
import time

from test_acceptance import home, post, server, state, wait_for  # noqa: F401
from test_foreman_core import MATE_A, drop, make_team, now_ms, procs  # noqa: F401


def test_a_note_and_a_stop_for_an_ended_session_go_at_once(server, home):
    ctl = home["control"]
    drop(
        ctl / "notes",
        "sid-ended.json",
        {
            "id": "n1",
            "session_id": "sid-ended",
            "agent_id": None,
            "text": "x",
            "at_ms": now_ms(),
        },
    )
    drop(
        ctl / "notes",
        "agent-of-ended.json",
        {
            "id": "n2",
            "session_id": "sid-ended",
            "agent_id": "agent-of-ended",
            "text": "x",
            "at_ms": now_ms(),
        },
    )
    drop(
        ctl / "stop",
        "session-sid-ended.json",
        {"session_id": "sid-ended", "at_ms": now_ms()},
    )
    (ctl / "notes" / ".any").touch()
    wait_for(lambda: not (ctl / "notes" / "sid-ended.json").exists(), timeout=10)
    wait_for(lambda: not (ctl / "notes" / "agent-of-ended.json").exists(), timeout=10)
    wait_for(lambda: not (ctl / "stop" / "session-sid-ended.json").exists(), timeout=10)
    wait_for(lambda: not (ctl / "notes" / ".any").exists(), timeout=10)
    assert not list((ctl / "notes").glob("*.json")) and not list(
        (ctl / "stop").glob("*.json")
    )


def test_a_note_for_a_live_session_stays(server, home):
    ctl = home["control"]
    assert (
        post(server, "/api/steer", {"session_id": "sid-alpha", "text": "Keep going."})[
            0
        ]
        == 200
    )
    time.sleep(3)
    assert (ctl / "notes" / "sid-alpha.json").exists()


def test_a_note_for_a_running_split_pane_teammate_stays(server, home, procs):
    make_team(home, procs)
    wait_for(
        lambda: any(
            s["agent_id"] == MATE_A
            for a in state(server)["agents"]
            for s in a["subagents"]
        ),
        timeout=10,
    )
    code, _ = post(
        server,
        "/api/steer",
        {"session_id": "sid-alpha", "agent_id": MATE_A, "text": "Cite sources."},
    )
    assert code == 200
    time.sleep(3)
    note = json.loads((home["control"] / "notes" / f"{MATE_A}.json").read_text())
    assert note["text"] == "Cite sources."
