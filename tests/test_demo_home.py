"""The demo home must keep showing every state, because the README GIFs record it."""

import sys
import time

from test_acceptance import ROOT

sys.path.insert(0, str(ROOT))

from agentforeman.collector import Collector, Teams  # noqa: E402
from agentforeman.control import Control  # noqa: E402
from agentforeman.demo import TEAM, TEAM_LEAD, Home, Writer  # noqa: E402


def wait_for(check, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        got = check()
        if got:
            return got
        time.sleep(0.3)
    raise AssertionError(f"condition not met in {timeout}s")


def test_demo_home_shows_one_example_of_each_feature():
    home = Home().build()
    try:
        st = Collector(home.claude, home.codex, Control(home.control)).snapshot()
        agents = st["agents"]
        approvals = [a["approval"] for a in agents if a.get("approval")]
        diff = next(ap["diff"] for ap in approvals if ap.get("diff"))
        assert len(diff.splitlines()) <= 10, "a short diff that reads at a glance"
        keys = [ap["rule_key"] for ap in approvals]
        assert keys.count("Bash(make eval)") == 2
        assert all(
            ap["similar"] == 1
            for ap in approvals
            if ap["rule_key"] == "Bash(make eval)"
        )
        lead = next(a for a in agents if a["session_id"] == TEAM_LEAD)
        mates = [s for s in lead["subagents"] if s.get("backend") == "tmux"]
        assert sorted(s["name"] for s in mates) == ["gateway", "redaction"]
        assert not {s["agent_id"] for s in mates} & {a["session_id"] for a in agents}, (
            "a split-pane teammate shows under its lead only"
        )
        shared = {
            a["name"]: [f["path"] for f in a["shared_files"]]
            for a in agents
            if a["shared_files"]
        }
        assert sorted(shared) == ["churn-model-7c", "churn-model-e2"]
        assert [a["name"] for a in agents if a["repeating"]] == ["noc-copilot-c4"]
        rules = st["rules"]
        assert rules["enabled"] is False
        assert [s["rule"] for s in rules["suggestions"]] == ["Bash(make eval)"]
        assert len(rules["recent"]) == 2
        assert st["controls_installed"]["current"]
    finally:
        home.close()


def test_demo_teammates_run_as_split_pane_processes_and_stop_with_the_demo():
    home = Home().build()
    mates = [p for p in home.procs if "--agent-id" in p.args]
    try:
        assert len(mates) == 2
        for p in mates:
            line = f" {' '.join(p.args)} "
            name = p.args[p.args.index("--agent-id") + 1].split("@")[0]
            assert Teams._alive([line], TEAM, TEAM_LEAD, {"name": name})
            assert p.poll() is None
    finally:
        home.close()
    assert all(p.poll() is not None for p in mates), "the demo stops its teammates"


def test_demo_delivers_a_note_and_ends_a_stopped_turn():
    home = Home().build()
    writer = Writer(home)
    writer.start()
    try:
        ctl = Control(home.control)
        collector = Collector(home.claude, home.codex, ctl)

        def agent(sid):
            return next(
                a for a in collector.snapshot()["agents"] if a["session_id"] == sid
            )

        busy = [a for a in collector.snapshot()["agents"] if a["status"] == "working"]
        steered, stopped = busy[0]["session_id"], busy[1]["session_id"]
        assert ctl.note(steered, None, "Use the staging data.") is None
        # The stalled session makes no tool call, so its note must keep waiting.
        assert ctl.note("demo-manuals", None, "Re-index shard 7 first.") is None
        wait_for(lambda: agent(steered)["note"]["delivered_ms"], timeout=8)
        assert any("staging" in x["desc"] for x in agent(steered)["timeline"][:3])
        assert agent("demo-manuals")["note"]["pending"] is True
        assert ctl.stop_session(stopped) is None
        wait_for(
            lambda: (
                agent(stopped)["status"] != "working"
                and not agent(stopped)["stop_requested"]
            )
        )
        calls = len(agent(stopped)["timeline"])
        time.sleep(5)
        assert agent(stopped)["status"] == "idle"
        assert len(agent(stopped)["timeline"]) == calls, (
            "a stopped session makes no call"
        )
    finally:
        writer.stop.set()
        home.close()


def test_demo_approves_similar_calls_and_an_edit():
    home = Home().build()
    writer = Writer(home)
    writer.start()
    try:
        ctl = Control(home.control)
        collector = Collector(home.claude, home.codex, ctl)
        assert ctl.decide_all("demo-eval-a", "allow", similar=True) == (None, 2)
        assert ctl.decide("demo-edit", "allow") is None

        def answered():
            by = {a["name"]: a for a in collector.snapshot()["agents"]}
            done = [
                by[n]
                for n in ("churn-model-e2", "scam-shield-41", "bill-explainer-5d")
                if by[n]["status"] == "your_turn"
            ]
            return len(done) == 3 and done

        done = wait_for(answered)
        assert all(a["approval"] is None for a in done)
        assert "tests pass" in done[2]["last_message"]
    finally:
        writer.stop.set()
        home.close()


def test_demo_home_covers_every_state():
    home = Home().build()
    try:
        st = Collector(home.claude, home.codex).snapshot()
        by = {a["name"]: a for a in st["agents"]}
        assert by["care-assistant-3f"]["status"] == "your_turn"
        assert (
            by["smb-receptionist-a1"]["status"] == "waiting"
            and by["smb-receptionist-a1"]["status_guess"]
        )
        assert (
            by["offer-engine-9e"]["since_ms"] < time.time() * 1000 - 6 * 3600 * 1000
        ), "parked"
        idle_for = time.time() * 1000 - by["field-copilot-0d"]["last_activity_ms"]
        assert (
            by["field-copilot-0d"]["status"] == "working" and idle_for > 10 * 60_000
        ), "stalled"
        assert by["noc-copilot-c4"]["entrypoint"] == "cli"
        subs = {s["name"]: s for s in by["churn-model-7c"]["subagents"]}
        assert (
            subs["dupes"]["parent_agent_id"] == "days-b"
            and subs["dupes"]["state"] == "running"
        )
        assert (subs["days-c"]["outcome"], subs["valid"]["outcome"]) == (
            "completed",
            "failed",
        )
        kb = {s["name"]: s for s in by["care-kb-52"]["subagents"]}
        assert kb["reviewer"]["kind"] == "teammate" and kb["faq"]["outcome"] == "killed"
        assert all(a["title"] for a in st["agents"])
        nested = [t for t in st["codex"] if t["parent_thread_id"] == "cx-1"]
        assert len(nested) == 2
    finally:
        home.close()


def test_writer_moves_docs_into_needs_attention():
    home = Home().build()
    writer = Writer(home, finish_after=0.5)
    writer.start()
    try:
        collector = Collector(home.claude, home.codex)
        end = time.time() + 6
        kb = None
        while time.time() < end:
            kb = next(
                a for a in collector.snapshot()["agents"] if a["name"] == "care-kb-52"
            )
            if kb["status"] == "your_turn":
                break
            time.sleep(0.3)
        assert kb["status"] == "your_turn", kb["status"]
        assert kb["last_message"].endswith(
            "Should I run the care assistant evals against it?"
        )
    finally:
        writer.stop.set()
        home.close()


def test_demo_answers_approve_reply_and_stop():
    from agentforeman.control import Control

    home = Home().build()
    writer = Writer(home)
    writer.start()
    try:
        ctl = Control(home.control)
        collector = Collector(home.claude, home.codex, ctl)

        def agent(name):
            return next(a for a in collector.snapshot()["agents"] if a["name"] == name)

        assert (
            agent("smb-receptionist-a1")["approval"]["detail"]
            == "git push origin missed-call-booking"
        )
        assert ctl.decide("demo-push", "allow") is None
        assert ctl.reply("demo-care", "Yes, open the PR.") is None
        assert ctl.stop("demo-churn", "days-a") is None
        end = time.time() + 12
        while time.time() < end:
            booked, care = agent("smb-receptionist-a1"), agent("care-assistant-3f")
            days_a = next(
                s for s in agent("churn-model-7c")["subagents"] if s["name"] == "days-a"
            )
            if (
                booked["status"] == "your_turn"
                and "Opened pull request" in (care["last_message"] or "")
                and days_a["state"] == "done"
            ):
                break
            time.sleep(0.5)
        assert booked["approval"] is None and booked["last_message"].startswith(
            "Pushed"
        )
        assert "Opened pull request #128" in care["last_message"]
        assert (days_a["state"], days_a["outcome"]) == ("done", "killed")
    finally:
        writer.stop.set()
        home.close()
