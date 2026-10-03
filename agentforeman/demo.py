"""Builds a demo Claude Code and Codex home with sample sessions, then runs AgentForeman on it.

Run: agentforeman demo [--port 4321]
Open the link that it prints. Press Ctrl+C to stop. Nothing outside a temporary folder changes,
and the demo removes that folder when it exits.

The sample sessions cover every state that AgentForeman shows: a reply that waits for you, a tool
call that waits for approval, busy sessions with subagents, a nested subagent, a teammate,
failed and stopped subagents, a stalled session, a parked session, a terminal session, and
Codex threads with subagent threads. They also hold one clear example of each control: an Edit
card with its diff, two sessions that wait on the same command, a lead with two split-pane
teammates, two sessions that edited one file, one repeating session, and a rule suggestion.
A writer thread keeps the busy ones making tool calls, and answers approve, reply, steer, and
stop the way the installed hooks and the model would.
"""

import json
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
MODEL = "claude-opus-5-5"


def iso(t):
    d = datetime.fromtimestamp(t, tz=timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def enc(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def usage(ctx):
    return {
        "input_tokens": 12,
        "cache_read_input_tokens": ctx,
        "cache_creation_input_tokens": 900,
        "output_tokens": 80,
    }


def prompt(sid, text, t):
    return {
        "type": "user",
        "sessionId": sid,
        "timestamp": iso(t),
        "promptSource": "typed",
        "message": {"role": "user", "content": text},
    }


def call(sid, tid, name, inp, t, ctx=180_000, model=MODEL, text=None):
    content = [{"type": "tool_use", "id": tid, "name": name, "input": inp}]
    if text:
        content.insert(0, {"type": "text", "text": text})
    msg = {
        "role": "assistant",
        "model": model,
        "stop_reason": "tool_use",
        "usage": usage(ctx),
        "content": content,
    }
    return {"type": "assistant", "sessionId": sid, "timestamp": iso(t), "message": msg}


def result(sid, tid, t, text="ok", error=False):
    block = {
        "type": "tool_result",
        "tool_use_id": tid,
        "content": text,
        "is_error": error,
    }
    return {
        "type": "user",
        "sessionId": sid,
        "timestamp": iso(t),
        "message": {"role": "user", "content": [block]},
    }


def reply(sid, text, t, ctx=180_000, model=MODEL):
    msg = {
        "role": "assistant",
        "model": model,
        "stop_reason": "end_turn",
        "usage": usage(ctx),
        "content": [{"type": "text", "text": text}],
    }
    return {"type": "assistant", "sessionId": sid, "timestamp": iso(t), "message": msg}


def note(sid, text, t):
    return {
        "type": "user",
        "sessionId": sid,
        "timestamp": iso(t),
        "message": {"role": "user", "content": text},
    }


def notice(task_id, tool_use_id, status):
    return (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>{tool_use_id}</tool-use-id>\n"
        f"<status>{status}</status>\n</task-notification>"
    )


# Tool calls that busy sessions repeat. Bash calls carry a description, as Claude Code writes them.
WORK = {
    "churn": [
        (
            "Bash",
            {
                "command": "python3 backfill_features.py --day 2026-03-04",
                "description": "Backfill the usage features for March 4",
            },
        ),
        ("Read", {"file_path": "/home/demo/code/churn-model/features/usage.py"}),
        ("Grep", {"pattern": "account_id IS NULL"}),
        (
            "Bash",
            {
                "command": "python3 check_counts.py --month 2026-03",
                "description": "Compare row counts with the source",
            },
        ),
        ("Edit", {"file_path": "/home/demo/code/churn-model/features/schema.sql"}),
    ],
    "days_a": [
        (
            "Bash",
            {
                "command": "python3 backfill_features.py --day 2026-03-07",
                "description": "Backfill March 7",
            },
        ),
        (
            "Bash",
            {
                "command": "python3 backfill_features.py --day 2026-03-08",
                "description": "Backfill March 8",
            },
        ),
        ("Read", {"file_path": "/home/demo/code/churn-model/logs/2026-03-08.log"}),
    ],
    "days_b": [
        (
            "Bash",
            {
                "command": "python3 backfill_features.py --day 2026-03-15",
                "description": "Backfill March 15",
            },
        ),
        ("Grep", {"pattern": "duplicate key"}),
        (
            "Bash",
            {
                "command": "python3 backfill_features.py --day 2026-03-16",
                "description": "Backfill March 16",
            },
        ),
    ],
    "dupes": [
        ("Grep", {"pattern": "GROUP BY account_id, day HAVING count"}),
        ("Read", {"file_path": "/home/demo/code/churn-model/features/dedupe.sql"}),
        (
            "Bash",
            {
                "command": "python3 find_dupes.py --from 2026-03-11 --to 2026-03-20",
                "description": "Count duplicate feature rows",
            },
        ),
    ],
    "kb": [
        ("Read", {"file_path": "/home/demo/code/care-kb/faq/plans.md"}),
        ("Edit", {"file_path": "/home/demo/code/care-kb/faq/plans.md"}),
        (
            "Bash",
            {
                "command": "python3 build_index.py --source faq",
                "description": "Rebuild the retrieval index for the FAQ",
            },
        ),
    ],
    "links": [
        ("WebFetch", {"url": "https://help.example.com/plans/compare"}),
        ("Grep", {"pattern": "](http"}),
        ("WebFetch", {"url": "https://help.example.com/plans/change"}),
    ],
    # The one repeating session: it polls an alarm feed that never reconnects.
    "noc": [
        (
            "Bash",
            {
                "command": "python3 feed_status.py --source alarms",
                "description": "Check whether the alarm feed reconnected",
            },
        ),
        (
            "Bash",
            {
                "command": "python3 feed_status.py --source alarms",
                "description": "Check whether the alarm feed reconnected",
            },
        ),
        ("Read", {"file_path": "/home/demo/code/noc-copilot/config/feeds.yaml"}),
        (
            "Bash",
            {
                "command": "python3 feed_status.py --source alarms",
                "description": "Check whether the alarm feed reconnected",
            },
        ),
    ],
    # A lead of two split-pane teammates.
    "landing": [
        ("Grep", {"pattern": "model_gateway"}),
        (
            "SendMessage",
            {
                "to": "gateway",
                "message": "Every team calls models through the gateway now. Add the budgets.",
            },
        ),
        ("Read", {"file_path": "/home/demo/code/ai-landing-zone/ARCHITECTURE.md"}),
        (
            "SendMessage",
            {"to": "redaction", "message": "Redact before the gateway logs a prompt."},
        ),
        ("Bash", {"command": "make lint", "description": "Lint the landing zone"}),
    ],
    "gateway": [
        ("Read", {"file_path": "/home/demo/code/ai-landing-zone/gateway/budgets.py"}),
        ("Edit", {"file_path": "/home/demo/code/ai-landing-zone/gateway/budgets.py"}),
        (
            "Bash",
            {
                "command": "pytest tests/gateway -q",
                "description": "Run the gateway tests",
            },
        ),
    ],
    "redaction": [
        (
            "Read",
            {"file_path": "/home/demo/code/ai-landing-zone/redaction/patterns.py"},
        ),
        (
            "Edit",
            {"file_path": "/home/demo/code/ai-landing-zone/redaction/patterns.py"},
        ),
        (
            "Bash",
            {
                "command": "pytest tests/redaction -q",
                "description": "Run the redaction tests",
            },
        ),
    ],
}

# What a session does at its next tool call when a steering note mentions the keyword.
# Any other note gets a short acknowledgement before the next planned call.
REACT = {
    "churn": (
        "staging",
        "Bash",
        {
            "command": "python3 backfill_features.py --day 2026-03-09 --source staging",
            "description": "Backfill March 9 from the staging data",
        },
        "Got your note. I load March 9 from the staging data now, then the other days.",
    ),
}
NOTED = "Got your note. I follow it from here."

TEAM = "landing-zone"
TEAM_LEAD = "demo-landing"
LANDING = "/home/demo/code/ai-landing-zone"
SCHEMA = "/home/demo/code/churn-model/features/schema.sql"
PRORATION = "/home/demo/code/bill-explainer/src/proration.py"
MAKE_EVAL = {"command": "make eval", "description": "Run the eval suite"}


class Home:
    """A temporary Claude and Codex home. Each live session gets a real process, so its PID is alive."""

    def __init__(self, root=None):
        self.root = Path(root or tempfile.mkdtemp(prefix="agentforeman-demo-"))
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"
        self.control = self.root / "control"
        self.procs = []
        self.now = time.time()
        self.paths = {}
        self.rng = random.Random(7)
        self.ids = 0
        # Request id -> the waiting call and what the session does after your answer.
        self.asks = {}

    def tid(self, prefix="toolu_demo"):
        self.ids += 1
        return f"{prefix}{self.ids:05d}"

    def close(self):
        """Stops every process of the demo: sessions, teammates, and the stand-in hooks."""
        for p in self.procs:
            p.kill()
            p.wait()

    def session(
        self,
        key,
        name,
        cwd,
        status,
        title,
        rows,
        started_ago,
        status_ago,
        entry="claude-vscode",
        argv=("sleep", "86400"),
    ):
        proc = subprocess.Popen(list(argv))
        self.procs.append(proc)
        sid = rows[0]["sessionId"]
        reg = {
            "pid": proc.pid,
            "sessionId": sid,
            "cwd": cwd,
            "name": name,
            "status": status,
            "entrypoint": entry,
            "startedAt": int((self.now - started_ago) * 1000),
            "statusUpdatedAt": int((self.now - status_ago) * 1000),
        }
        (self.claude / "sessions").mkdir(parents=True, exist_ok=True)
        reg_path = self.claude / "sessions" / f"{proc.pid}.json"
        reg_path.write_text(json.dumps(reg))
        folder = self.claude / "projects" / enc(cwd)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{sid}.jsonl"
        # ISO timestamps in one format sort in time order.
        rows = sorted(rows, key=lambda r: r["timestamp"])
        rows.append({"type": "ai-title", "aiTitle": title, "sessionId": sid})
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.paths[key] = {
            "sid": sid,
            "transcript": path,
            "registry": reg_path,
            "reg": reg,
        }
        return sid

    def subagent(self, key, agent_id, meta, rows, age):
        folder = self.paths[key]["transcript"].with_suffix("") / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"agent-{agent_id}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        (folder / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta))
        t = self.now - age
        os.utime(path, (t, t))
        self.paths[f"{key}/{agent_id}"] = {
            "sid": self.paths[key]["sid"],
            "transcript": path,
        }
        return path

    def ask(self, key, rid, tool, inp, tid, ago, done, then, denied):
        """Registers a call that waits for approval. After an allow, the session gets `done`
        as the tool result, then runs `then`: ("call", tool, input, result) and ("reply", text)."""
        self.asks[rid] = {
            "key": key,
            "tool": tool,
            "input": inp,
            "tid": tid,
            "ago": ago,
            "done": done,
            "then": then,
            "denied": denied,
        }

    def teammate(self, name, sid, prompt_text, work, joined_ago, model="sonnet"):
        """A split-pane teammate: its own process and transcript, listed in the team config.

        The process's command line carries --agent-id and --parent-session-id, as Claude Code
        starts a teammate in a tmux pane, so AgentForeman can tell that it still runs."""
        rows = [prompt(sid, prompt_text, self.now - joined_ago + 5)]
        rows += self.history(sid, WORK[work], joined_ago - 20, 8, every=40, ctx=70_000)
        for r in rows:
            r.update(teamName=TEAM, agentName=name)
        argv = [
            sys.executable,
            "-c",
            "import time; time.sleep(86400)",
            "--agent-id",
            f"{name}@{TEAM}",
            "--parent-session-id",
            TEAM_LEAD,
        ]
        self.session(
            name,
            name,
            LANDING,
            "busy",
            prompt_text.splitlines()[0].rstrip("."),
            rows,
            joined_ago,
            joined_ago - 5,
            entry="cli",
            argv=argv,
        )
        return {
            "name": name,
            "agentType": "general-purpose",
            "model": model,
            "backendType": "tmux",
            "cwd": LANDING,
            "joinedAt": int((self.now - joined_ago) * 1000),
            "prompt": prompt_text,
        }

    def history(self, sid, work, start_ago, end_ago, every=45, ctx=150_000):
        """Finished tool calls spread over a past window, so sparklines and the chart have shape."""
        rows, t, i = [], self.now - start_ago, 0
        while t < self.now - end_ago:
            name, inp = work[i % len(work)]
            tid = self.tid()
            rows += [
                call(sid, tid, name, inp, t, ctx + i * 900),
                result(sid, tid, t + self.rng.uniform(1, 9)),
            ]
            t += self.rng.uniform(every * 0.4, every * 1.6)
            i += 1
        return rows

    def build(self):
        n = self.now
        # A turn that ended with a question: Needs input.
        sid = "demo-care"
        rows = [
            prompt(
                sid,
                "Hand the chat to a live agent after two failed answers. A failed answer is "
                "a thumbs-down, or the customer asking the same question again.",
                n - 1500,
            )
        ]
        rows += self.history(
            sid,
            [
                ("Grep", {"pattern": "def route_turn"}),
                (
                    "Read",
                    {"file_path": "/home/demo/code/care-assistant/src/care/handoff.py"},
                ),
                (
                    "Bash",
                    {
                        "command": "pytest tests/test_handoff.py -q",
                        "description": "Run the handoff tests",
                    },
                ),
            ],
            1480,
            420,
        )
        rows.append(
            reply(
                sid,
                "I found it: the router counted failed answers per turn, so the count went back to zero on each new message.\n\nThe count lives on the conversation now, and the second failed answer hands the chat to a live agent with the transcript. All 42 handoff tests pass. Want me to open a pull request?",
                n - 380,
                96_000,
            )
        )
        self.session(
            "care",
            "care-assistant-3f",
            "/home/demo/code/care-assistant",
            "idle",
            "Hand the chat to a live agent after two failed answers",
            rows,
            1600,
            380,
        )

        # A tool call with no result: Awaiting approval.
        sid = "demo-receptionist"
        rows = [
            prompt(
                sid,
                "Book appointments from missed calls: text the caller the open slots, "
                "book the one they pick, then push the branch.",
                n - 900,
            )
        ]
        rows += self.history(
            sid,
            [
                (
                    "Read",
                    {
                        "file_path": "/home/demo/code/smb-receptionist/src/receptionist/missed_call.py"
                    },
                ),
                (
                    "Edit",
                    {
                        "file_path": "/home/demo/code/smb-receptionist/src/receptionist/missed_call.py"
                    },
                ),
                (
                    "Bash",
                    {
                        "command": "pytest tests/test_missed_call.py",
                        "description": "Run the missed-call tests",
                    },
                ),
            ],
            880,
            260,
        )
        self.push_tid = self.tid()
        push = {
            "command": "git push origin missed-call-booking",
            "description": "Push the missed-call-booking branch",
        }
        rows.append(call(sid, self.push_tid, "Bash", push, n - 200, 120_000))
        self.ask(
            "receptionist",
            "demo-push",
            "Bash",
            push,
            self.push_tid,
            200,
            "To github.com:demo/smb-receptionist.git\n * [new branch] missed-call-booking",
            [
                (
                    "reply",
                    "Pushed the missed-call-booking branch.\n\nWant me to open the pull request?",
                )
            ],
            "I did not push. Tell me when to try again.",
        )
        self.session(
            "receptionist",
            "smb-receptionist-a1",
            "/home/demo/code/smb-receptionist",
            "idle",
            "Book appointments from missed calls",
            rows,
            1000,
            200,
        )

        # A busy lead with four subagents, one nested, one failed.
        sid = "demo-churn"
        rows = [
            prompt(
                sid,
                "Backfill the March usage features. Split the month across subagents and check the row counts.",
                n - 1700,
            )
        ]
        launches = [
            ("toolu_days_a", "Backfill March 1 to 10"),
            ("toolu_days_b", "Backfill March 11 to 20"),
            ("toolu_days_c", "Backfill March 21 to 31"),
        ]
        for i, (tid, desc) in enumerate(launches):
            rows += [
                call(sid, tid, "Agent", {"description": desc}, n - 1650 + i * 4),
                result(sid, tid, n - 1649 + i * 4, "Async agent launched"),
            ]
        rows.append(
            call(
                sid,
                "toolu_valid",
                "Agent",
                {"description": "Validate row counts"},
                n - 1600,
            )
        )
        rows.append(
            result(
                sid,
                "toolu_valid",
                n - 700,
                "Row counts differ for March 9: 41,220 accounts in the source, 41,198 loaded. "
                "The staging data has the full day.",
                error=True,
            )
        )
        rows.append(note(sid, notice("days-c", "toolu_days_c", "completed"), n - 300))
        rows += self.history(sid, WORK["churn"], 1550, 20, every=50, ctx=240_000)
        # The two oldest busy sessions make live tool calls, so a steer note and a stop
        # sent to them arrive within seconds.
        self.session(
            "churn",
            "churn-model-7c",
            "/home/demo/code/churn-model",
            "busy",
            "Backfill March usage features",
            rows,
            3600,
            1700,
        )
        bg = {
            "agentType": "general-purpose",
            "model": "sonnet",
            "requestShape": "background",
        }
        self.subagent(
            "churn",
            "days-a",
            {
                **bg,
                "name": "days-a",
                "description": "Backfill March 1 to 10",
                "toolUseId": "toolu_days_a",
            },
            self.history("demo-churn", WORK["days_a"], 1640, 10, every=40),
            8,
        )
        nest = self.history("demo-churn", WORK["days_b"], 1640, 900, every=40)
        nest.append(
            call(
                "demo-churn",
                "toolu_nest",
                "Agent",
                {"description": "Check duplicate feature rows"},
                n - 880,
            )
        )
        nest += self.history("demo-churn", WORK["days_b"], 860, 12, every=40)
        self.subagent(
            "churn",
            "days-b",
            {
                **bg,
                "name": "days-b",
                "description": "Backfill March 11 to 20",
                "toolUseId": "toolu_days_b",
            },
            nest,
            10,
        )
        self.subagent(
            "churn",
            "dupes",
            {
                "agentType": "general-purpose",
                "model": "haiku",
                "name": "dupes",
                "description": "Check duplicate feature rows",
                "toolUseId": "toolu_nest",
                "spawnDepth": 2,
            },
            self.history("demo-churn", WORK["dupes"], 870, 15, every=60),
            14,
        )
        done = self.history("demo-churn", WORK["days_a"], 1640, 330, every=40)
        done.append(
            reply(
                "demo-churn",
                "March 21 to 31 is loaded: 453,420 account-day rows, no gaps.",
                n - 320,
            )
        )
        self.subagent(
            "churn",
            "days-c",
            {
                **bg,
                "name": "days-c",
                "description": "Backfill March 21 to 31",
                "toolUseId": "toolu_days_c",
            },
            done,
            320,
        )
        valid = self.history(
            "demo-churn",
            [
                (
                    "Bash",
                    {
                        "command": f"python3 check_counts.py --day 2026-03-{day:02d}",
                        "description": f"Count rows for March {day}",
                    },
                )
                for day in range(1, 11)
            ],
            1590,
            710,
            every=90,
        )
        valid.append(
            reply(
                "demo-churn",
                "Row counts differ for March 9. The staging data has the full day.",
                n - 705,
            )
        )
        self.subagent(
            "churn",
            "valid",
            {
                "agentType": "general-purpose",
                "model": "sonnet",
                "name": "valid",
                "description": "Validate row counts",
                "toolUseId": "toolu_valid",
            },
            valid,
            705,
        )

        # A busy session with an Agent Teams teammate, a working subagent, and a stopped one.
        sid = "demo-kb"
        rows = [
            prompt(
                sid,
                "Rewrite the plan FAQ that the care assistant answers from, and check every link.",
                n - 1300,
            )
        ]
        rows += [
            call(
                sid,
                "toolu_links",
                "Agent",
                {"description": "Check every link in the plan FAQ"},
                n - 1250,
            ),
            result(sid, "toolu_links", n - 1249, "Async agent launched"),
        ]
        rows += [
            call(
                sid,
                "toolu_faq",
                "Agent",
                {"description": "Draft answers to the top chat questions"},
                n - 1240,
            ),
            result(sid, "toolu_faq", n - 1239, "Async agent launched"),
        ]
        rows += self.history(sid, WORK["kb"], 1200, 15, every=55, ctx=130_000)
        self.session(
            "kb",
            "care-kb-52",
            "/home/demo/code/care-kb",
            "busy",
            "Rewrite the plan FAQ for the care assistant",
            rows,
            1400,
            1300,
        )
        self.subagent(
            "kb",
            "links",
            {
                **bg,
                "name": "links",
                "description": "Check every link in the plan FAQ",
                "toolUseId": "toolu_links",
            },
            self.history("demo-kb", WORK["links"], 1240, 10, every=35),
            6,
        )
        faq = self.history(
            "demo-kb",
            [
                ("Grep", {"pattern": "change my plan"}),
                (
                    "Read",
                    {"file_path": "/home/demo/code/care-kb/data/top_questions.csv"},
                ),
                ("Write", {"file_path": "/home/demo/code/care-kb/faq/plan-answers.md"}),
            ],
            1230,
            640,
            every=80,
        )
        faq += [note("demo-kb", "[Request interrupted by user for tool use]", n - 630)]
        self.subagent(
            "kb",
            "faq",
            {
                **bg,
                "name": "faq",
                "description": "Draft answers to the top chat questions",
                "toolUseId": "toolu_faq",
            },
            faq,
            630,
        )
        tm = {
            "name": "reviewer",
            "agentType": "reviewer",
            "model": "opus",
            "taskKind": "in_process_teammate",
            "teamName": "kb",
        }
        self.subagent(
            "kb",
            "reviewer",
            tm,
            [reply("demo-kb", "Waiting for the next draft.", n - 240)],
            240,
        )

        # Busy, but quiet for 25 minutes with nothing under it: Stalled.
        sid = "demo-manuals"
        rows = [prompt(sid, "Re-index the equipment manuals from scratch.", n - 3000)]
        rows += self.history(
            sid,
            [
                (
                    "Bash",
                    {
                        "command": f"python3 index_manuals.py --shard {i}",
                        "description": f"Embed manual shard {i}",
                    },
                )
                for i in range(1, 13)
            ],
            2950,
            1560,
            every=120,
        )
        rows.append(
            call(
                sid,
                self.tid(),
                "Bash",
                {
                    "command": "python3 index_manuals.py --all",
                    "description": "Re-index every manual shard",
                },
                n - 1500,
                88_000,
            )
        )
        self.session(
            "manuals",
            "field-copilot-0d",
            "/home/demo/code/field-copilot",
            "busy",
            "Re-index the equipment manuals",
            rows,
            3100,
            3000,
        )

        # A finished turn from yesterday: Parked.
        sid = "demo-offers"
        rows = [
            prompt(sid, "Move the offer ranker to the new embeddings.", n - 9 * 3600)
        ]
        rows += self.history(
            sid,
            [
                (
                    "Bash",
                    {
                        "command": "python3 embed_offers.py --model embed-v3",
                        "description": "Embed the offer catalog with the new model",
                    },
                ),
                (
                    "Bash",
                    {
                        "command": "python3 train_ranker.py --embeddings embed-v3",
                        "description": "Retrain the offer ranker",
                    },
                ),
                (
                    "Bash",
                    {
                        "command": "python3 eval_ranker.py --holdout 2026-08",
                        "description": "Score the ranker on the August holdout",
                    },
                ),
            ],
            9 * 3600 - 60,
            8 * 3600,
            every=600,
        )
        rows.append(
            reply(
                sid,
                "The ranker runs on the new embeddings. Offer acceptance on the August holdout rises to 6.8% from 6.1%.",
                n - 8 * 3600,
                70_000,
            )
        )
        self.session(
            "offers",
            "offer-engine-9e",
            "/home/demo/code/offer-engine",
            "idle",
            "Move the offer ranker to the new embeddings",
            rows,
            9 * 3600 + 60,
            8 * 3600,
        )

        # A session that runs in a terminal, not in VS Code.
        sid = "demo-noc"
        rows = [prompt(sid, "Group alarm storms into one incident.", n - 800)]
        rows += self.history(sid, WORK["noc"], 780, 25, every=70, ctx=60_000)
        self.session(
            "noc",
            "noc-copilot-c4",
            "/home/demo/code/noc-copilot",
            "busy",
            "Group alarm storms into one incident",
            rows,
            3400,
            800,
            entry="cli",
        )

        self.explainer()
        self.features()
        self.scam()
        self.landing()
        self.codex_threads()
        self.controls()
        return self

    def done(self, sid, ago, name, inp, out="ok", error=False, text=None, ctx=90_000):
        """One finished tool call: the call, and its result a few seconds later."""
        tid = self.tid()
        t = self.now - ago
        return [
            call(sid, tid, name, inp, t, ctx, text=text),
            result(sid, tid, t + 3, out, error),
        ]

    def waiting_call(self, sid, ago, name, inp, text=None, ctx=95_000):
        """A call with no result yet: its permission prompt waits."""
        tid = self.tid()
        return tid, call(sid, tid, name, inp, self.now - ago, ctx, text=text)

    def explainer(self):
        """An Edit that waits for approval, so its card shows a short diff."""
        sid = "demo-explainer"
        rows = [
            prompt(
                sid,
                "Customers who change plans mid-cycle see a wrong prorated line on the bill. "
                "Find the bug in the proration and fix it.",
                self.now - 720,
            )
        ]
        rows += self.done(sid, 700, "Grep", {"pattern": "def prorate"})
        rows += self.done(sid, 660, "Read", {"file_path": PRORATION})
        rows += self.done(
            sid,
            610,
            "Bash",
            {
                "command": "pytest tests/test_proration.py -q",
                "description": "Run the proration tests",
            },
            "2 failed, 16 passed in 0.4s",
            error=True,
        )
        rows += self.done(
            sid,
            560,
            "Read",
            {"file_path": "/home/demo/code/bill-explainer/tests/test_proration.py"},
        )
        edit = {
            "file_path": PRORATION,
            "old_string": (
                "def prorate(charge, days_used, cycle):\n"
                "    return round(charge * days_used / 30, 2)"
            ),
            "new_string": (
                "def prorate(charge, days_used, cycle):\n"
                "    days = monthrange(cycle.start.year, cycle.start.month)[1]\n"
                "    return round(charge * days_used / days, 2)"
            ),
        }
        tid, row = self.waiting_call(
            sid,
            140,
            "Edit",
            edit,
            "prorate() divides by a fixed 30 days, so a change in February charges too "
            "little and a change in a 31-day month too much. I divide by the real days "
            "in the billing month.",
        )
        rows.append(row)
        self.ask(
            "explainer",
            "demo-edit",
            "Edit",
            edit,
            tid,
            140,
            f"The file {PRORATION} has been updated successfully.",
            [
                (
                    "call",
                    "Bash",
                    {
                        "command": "pytest tests/test_proration.py -q",
                        "description": "Run the proration tests",
                    },
                    "18 passed in 0.4s",
                ),
                (
                    "reply",
                    "Mid-cycle changes now prorate by the real days in the billing month: "
                    "10 days of a $30 plan in February is $10.71, not $10.00. "
                    "All 18 proration tests pass.",
                ),
            ],
            "I left proration.py as it was. Tell me which day count you want.",
        )
        self.session(
            "explainer",
            "bill-explainer-5d",
            "/home/demo/code/bill-explainer",
            "idle",
            "Fix the proration of mid-cycle plan changes",
            rows,
            800,
            140,
        )

    def features(self):
        """A second session in the churn-model folder. It also edited features/schema.sql,
        so the guard warned it, and it now waits on `make eval`."""
        sid = "demo-features"
        rows = [
            prompt(
                sid,
                "Add tenure and plan-change features to the churn model, then run the evals.",
                self.now - 1500,
            )
        ]
        rows += self.done(sid, 1470, "Grep", {"pattern": "tenure"})
        rows += self.done(sid, 1420, "Read", {"file_path": SCHEMA})
        rows += self.done(
            sid,
            1300,
            "Bash",
            {
                "command": "python3 build_features.py --sample 10000 --only tenure_months,plan_changes_90d",
                "description": "Build the new features on a sample",
            },
            "10,000 rows: tenure_months 0 nulls, plan_changes_90d 0 nulls",
        )
        rows += self.done(
            sid,
            1110,
            "Read",
            {"file_path": SCHEMA},
            text="The guard says that churn-model-7c also edited features/schema.sql in "
            "the last 30 minutes, so I read the file again before my edit.",
        )
        rows += self.done(sid, 1060, "Edit", {"file_path": SCHEMA})
        rows += self.done(
            sid,
            900,
            "Bash",
            {
                "command": "python3 train.py --quick",
                "description": "Train a quick model with the new features",
            },
            "AUC 0.811 on the February holdout (was 0.786)",
        )
        tid, row = self.waiting_call(
            sid,
            150,
            "Bash",
            MAKE_EVAL,
            "The guard said that churn-model-7c also edited features/schema.sql, so I read "
            "it again and added the two columns below its usage columns. Both changes "
            "stay. Now I run the evals.",
        )
        rows.append(row)
        self.ask(
            "features",
            "demo-eval-a",
            "Bash",
            MAKE_EVAL,
            tid,
            150,
            "churn eval: AUC 0.814 (was 0.786), 0 of 6 customer slices worse",
            [
                (
                    "reply",
                    "Tenure and plan changes are in: AUC rises to 0.814 from 0.786, "
                    "and no customer slice gets worse.",
                )
            ],
            "I did not run the evals. Tell me when to run them.",
        )
        self.session(
            "features",
            "churn-model-e2",
            "/home/demo/code/churn-model",
            "idle",
            "Add tenure and plan-change features",
            rows,
            1600,
            150,
        )

    def scam(self):
        """The other session that waits on `make eval`, so the card shows Approve all 2."""
        sid = "demo-scam"
        links = "/home/demo/code/scam-shield/src/shield/links.py"
        rows = [
            prompt(
                sid,
                'Catch look-alike links in scam messages, like "rnybank.example" posing '
                'as "mybank.example".',
                self.now - 1000,
            )
        ]
        rows += self.done(sid, 980, "Grep", {"pattern": "def score_links"})
        rows += self.done(sid, 950, "Read", {"file_path": links})
        rows += self.done(sid, 800, "Edit", {"file_path": links})
        rows += self.done(
            sid,
            700,
            "Bash",
            {
                "command": 'python3 -m shield.cli "Your account is locked: https://rnybank.example/verify"',
                "description": "Score a message with a look-alike link",
            },
            "scam 0.97: rnybank.example looks like mybank.example (rn for m)",
        )
        rows += self.done(
            sid,
            500,
            "Read",
            {"file_path": "/home/demo/code/scam-shield/tests/test_links.py"},
        )
        tid, row = self.waiting_call(sid, 120, "Bash", MAKE_EVAL)
        rows.append(row)
        self.ask(
            "scam",
            "demo-eval-b",
            "Bash",
            MAKE_EVAL,
            tid,
            120,
            "scam eval: 2,400 messages, recall 0.93 (was 0.81), false positives 0.4%",
            [
                (
                    "reply",
                    'Look-alike links are caught now: "rnybank.example" scores as a scam. '
                    "Recall on the scam eval rises to 0.93 from 0.81, with 0.4% false positives.",
                )
            ],
            "I did not run the evals. Tell me when to run them.",
        )
        self.session(
            "scam",
            "scam-shield-41",
            "/home/demo/code/scam-shield",
            "idle",
            "Catch look-alike links in scam messages",
            rows,
            1100,
            120,
        )

    def landing(self):
        """A lead with two split-pane teammates of Agent Teams."""
        sid = TEAM_LEAD
        rows = [
            prompt(
                sid,
                "Stand up the AI landing zone. Start a team: one teammate for the model "
                "gateway, one for redaction.",
                self.now - 1260,
            )
        ]
        # The lead plans alone first, then starts the team about 5 minutes ago.
        solo = [w for w in WORK["landing"] if w[0] != "SendMessage"]
        rows += self.history(sid, solo, 1240, 330, every=60, ctx=90_000)
        rows += self.history(sid, WORK["landing"], 320, 15, every=40, ctx=110_000)
        self.session(
            "landing",
            "ai-landing-zone-6b",
            LANDING,
            "busy",
            "Stand up the AI landing zone",
            rows,
            1300,
            1260,
        )
        members = [
            self.teammate(
                "gateway",
                "demo-gateway",
                "Cap token spend per team at the model gateway.\n"
                "Reject a call over the monthly budget with a clear error.",
                "gateway",
                300,
            ),
            self.teammate(
                "redaction",
                "demo-redaction",
                "Redact account numbers before prompts leave the network.\n"
                "Keep the last four digits, so agents can still find the account.",
                "redaction",
                280,
            ),
        ]
        folder = self.claude / "teams" / TEAM
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "config.json").write_text(
            json.dumps({"name": TEAM, "leadSessionId": sid, "members": members})
        )

    def fake_hook(self):
        """A live process that stands in for a waiting hook, so the server keeps its files."""
        p = subprocess.Popen(["sleep", "86400"])
        self.procs.append(p)
        return p.pid

    def put(self, sub, name, data):
        folder = self.control / sub if sub else self.control
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(json.dumps(data))

    def controls(self):
        """Control files as the installed hooks write them: the waiting approvals, one reply
        waiting, approvals that suggest a rule, and two calls that rules allowed earlier."""
        import hashlib

        sha = hashlib.sha256(
            (PACKAGE / "agentforeman_hook.py").read_bytes()
        ).hexdigest()
        self.put(
            "",
            "installed.json",
            {"installed_ms": int((self.now - 86400) * 1000), "hook_sha256": sha},
        )
        for rid, ask in self.asks.items():
            self.put(
                "requests",
                f"{rid}.json",
                {
                    "id": rid,
                    "pid": self.fake_hook(),
                    "session_id": self.paths[ask["key"]]["sid"],
                    "tool_name": ask["tool"],
                    "tool_input": ask["input"],
                    "created_ms": int((self.now - ask["ago"]) * 1000),
                },
            )
        # You approved `make eval` three times, so the Rules page suggests a rule for it.
        self.put("", "approvals.json", {"Bash(make eval)": 3, "Bash(git push)": 1})
        # Rules ran earlier today, and you turned them off since.
        added = int((self.now - 26 * 3600) * 1000)
        self.put(
            "",
            "rules.json",
            {
                "enabled": False,
                "items": [
                    {"rule": "Bash(git status)", "added_ms": added},
                    {"rule": "Bash(git diff)", "added_ms": added},
                ],
            },
        )
        auto = [
            ("demo-care", "git status --short", "Bash(git status)", 2400),
            ("demo-receptionist", "git diff --stat", "Bash(git diff)", 2150),
        ]
        (self.control / "auto.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "at_ms": int((self.now - ago) * 1000),
                        "session_id": sid,
                        "agent_id": None,
                        "tool_name": "Bash",
                        "summary": command,
                        "rule": rule,
                    }
                )
                + "\n"
                for sid, command, rule, ago in auto
            )
        )
        self.put(
            "waiting",
            "demo-care.json",
            {
                "pid": self.fake_hook(),
                "session_id": "demo-care",
                "since_ms": int((self.now - 380) * 1000),
            },
        )

    def codex_threads(self):
        self.codex.mkdir(parents=True, exist_ok=True)
        ms = lambda ago: int((self.now - ago) * 1000)  # noqa: E731
        db = sqlite3.connect(self.codex / "state_5.sqlite")
        db.execute(
            "create table threads (id text, title text, cwd text, model text, updated_at_ms integer,"
            " archived integer, thread_source text, agent_nickname text)"
        )
        db.executemany(
            "insert into threads values (?,?,?,?,?,?,?,?)",
            [
                (
                    "cx-1",
                    "Add hallucination checks to the care assistant evals",
                    "/home/demo/code/care-assistant",
                    "gpt-6",
                    ms(90),
                    0,
                    "user",
                    None,
                ),
                (
                    "cx-2",
                    "Check answers against the plan FAQ",
                    "/home/demo/code/care-assistant",
                    "gpt-6",
                    ms(100),
                    0,
                    "subagent",
                    "Wren",
                ),
                (
                    "cx-3",
                    "Collect chats that quote a made-up price",
                    "/home/demo/code/care-assistant",
                    "gpt-6",
                    ms(110),
                    0,
                    "subagent",
                    "Finch",
                ),
                (
                    "cx-4",
                    "Review the gateway budget diff",
                    "/home/demo/code/ai-landing-zone",
                    "gpt-6",
                    ms(1200),
                    0,
                    "user",
                    None,
                ),
                (
                    "cx-5",
                    "Profile the slow transcript redaction",
                    "/home/demo/code/ai-landing-zone",
                    "gpt-6",
                    ms(3 * 3600),
                    0,
                    "user",
                    None,
                ),
            ],
        )
        db.execute(
            "create table thread_spawn_edges (child_thread_id text, parent_thread_id text)"
        )
        db.executemany(
            "insert into thread_spawn_edges values (?, ?)",
            [("cx-2", "cx-1"), ("cx-3", "cx-1")],
        )
        db.commit()
        db.close()
        db = sqlite3.connect(self.codex / "thread_history_1.sqlite")
        db.execute(
            "create table thread_turns (thread_id text, turn_id text, status text, started_at integer, completed_at integer)"
        )
        s = int(self.now)
        db.executemany(
            "insert into thread_turns values (?,?,?,?,?)",
            [
                ("cx-1", "t1", "inProgress", s - 300, None),
                ("cx-4", "t1", "completed", s - 1500, s - 1200),
                ("cx-5", "t1", "failed", s - 3 * 3600 - 60, s - 3 * 3600),
            ],
        )
        db.commit()
        db.close()


class Writer(threading.Thread):
    """Keeps the busy sessions, subagents, and teammates making tool calls, as live agents do.

    It also plays the hooks and the model: it answers approvals, replies, and stops, and it
    hands a steering note to its agent at that agent's next tool call. A job is a session
    key, a subagent key (session/agent), or a split-pane teammate, which is a session too.

    With finish_after set, care-kb ends its turn with a question after that many seconds,
    so it moves into Needs attention while you watch.
    """

    JOBS = [
        ("churn", "churn", 2.6),
        ("churn/days-a", "days_a", 3.1),
        ("churn/days-b", "days_b", 3.7),
        ("churn/dupes", "dupes", 4.3),
        ("kb", "kb", 2.9),
        ("kb/links", "links", 3.4),
        ("noc", "noc", 4.0),
        ("landing", "landing", 3.3),
        ("gateway", "gateway", 2.8),
        ("redaction", "redaction", 3.6),
    ]

    def __init__(self, home, finish_after=None):
        super().__init__(daemon=True)
        self.home = home
        self.finish_at = time.time() + finish_after if finish_after else None
        self.stop = threading.Event()
        self.next = {
            key: time.time() + i * 0.7 for i, (key, _, _) in enumerate(self.JOBS)
        }
        self.step = {key: 0 for key, _, _ in self.JOBS}
        self.pending = []
        self.status_due = []
        self.stopped = set()  # subagents that a stop ended
        self.ended = set()  # sessions and teammates whose turn a stop ended
        self.finished = False

    def later(self, delay, key, row):
        self.pending.append((time.time() + delay, key, row))

    def set_status(self, key, status):
        info = self.home.paths[key]
        info["reg"] = {
            **info["reg"],
            "status": status,
            "statusUpdatedAt": int(time.time() * 1000),
        }
        info["registry"].write_text(json.dumps(info["reg"]))

    def answer_controls(self, now):
        """Plays the session side of approve, reply, and stop, as the real hooks and model would."""
        c = self.home.control
        for d in (
            sorted((c / "decisions").glob("*.json"))
            if (c / "decisions").exists()
            else []
        ):
            decision = json.loads(d.read_text())
            d.unlink()
            (c / "requests" / d.name).unlink(missing_ok=True)
            ask = self.home.asks.pop(d.stem, None)
            if ask:
                self.answer(ask, decision.get("behavior") == "allow", now)
        for r in (
            sorted((c / "replies").glob("*.json")) if (c / "replies").exists() else []
        ):
            key = self.key_of(r.stem)
            r.unlink()
            (c / "waiting" / r.name).unlink(missing_ok=True)
            if not key:
                continue
            sid = self.home.paths[key]["sid"]
            tid = self.home.tid("toolu_live")
            self.set_status(key, "busy")
            self.append(
                key,
                call(
                    sid,
                    tid,
                    "Bash",
                    {
                        "command": "gh pr create --fill",
                        "description": "Open the pull request",
                    },
                    now,
                ),
            )
            self.later(
                3,
                key,
                result(sid, tid, now + 3, "https://github.com/demo/repo/pull/128"),
            )
            self.later(6, key, reply(sid, "Opened pull request #128.", now + 6))
            self.later_status(6.2, key, "idle")
        for st in sorted((c / "stop").glob("*.json")) if (c / "stop").exists() else []:
            aid = st.stem
            parent = next(
                (k.split("/")[0] for k in self.home.paths if k.endswith("/" + aid)),
                None,
            )
            if not parent or aid in self.stopped:
                continue
            self.stopped.add(aid)
            sid = self.home.paths[parent]["sid"]
            self.later(
                1.5,
                f"{parent}/{aid}",
                reply(
                    sid,
                    "Stopped by the user before the last days were loaded.",
                    now + 1.5,
                ),
            )
            meta = json.loads(
                self.home.paths[f"{parent}/{aid}"]["transcript"]
                .with_suffix(".meta.json")
                .read_text()
            )
            self.later(
                2,
                parent,
                note(sid, notice(aid, meta.get("toolUseId", ""), "killed"), now + 2),
            )

    def key_of(self, sid):
        """The session key of a session id, or None."""
        return next(
            (k for k, v in self.home.paths.items() if "/" not in k and v["sid"] == sid),
            None,
        )

    def answer(self, ask, allow, now):
        """The session side of an approval: the tool result, then its next steps."""
        key = ask["key"]
        sid = self.home.paths[key]["sid"]
        self.set_status(key, "busy")
        if not allow:
            self.append(
                key, result(sid, ask["tid"], now, "Denied from AgentForeman.", True)
            )
            self.later(2.5, key, reply(sid, ask["denied"], now + 2.5, 124_000))
            self.later_status(3, key, "idle")
            return
        self.append(key, result(sid, ask["tid"], now, ask["done"]))
        t = 0.0
        for step in ask["then"]:
            t += 2.5
            if step[0] == "reply":
                self.later(t, key, reply(sid, step[1], now + t, 124_000))
                continue
            _, name, inp, out = step
            tid = self.home.tid("toolu_live")
            self.later(t, key, call(sid, tid, name, inp, now + t, 124_000))
            t += 2
            self.later(t, key, result(sid, tid, now + t, out))
        self.later_status(t + 0.5, key, "idle")

    def deliver_note(self, key, now):
        """Hands a waiting note to this agent at its tool call, as the PreToolUse hook does.
        Returns the note text, or None."""
        aid = key.split("/")[1] if "/" in key else None
        sid = self.home.paths[key]["sid"]
        folder = self.home.control / "notes"
        path = folder / f"{aid or sid}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if (data.get("agent_id") or None) != aid or (
            not aid and data.get("session_id") != sid
        ):
            return None
        path.unlink(missing_ok=True)
        (folder / "delivered").mkdir(parents=True, exist_ok=True)
        (folder / "delivered" / path.name).write_text(
            json.dumps({"id": data.get("id"), "delivered_ms": int(now * 1000)})
        )
        return str(data.get("text") or "")

    def end_turn(self, key):
        """Ends a session's turn at its next tool call when a stop waits for it. A teammate
        is a session of its own, so its Stop works the same way."""
        stop = (
            self.home.control / "stop" / f"session-{self.home.paths[key]['sid']}.json"
        )
        if not stop.exists():
            return False
        stop.unlink(missing_ok=True)
        # Results that are still due arrive now, so no call is left without one.
        for item in [p for p in self.pending if p[1] == key]:
            self.pending.remove(item)
            self.append(key, item[2])
        self.ended.add(key)
        self.set_status(key, "idle")
        return True

    def runs(self, key, now):
        """True when this job still makes tool calls."""
        session = key.split("/")[0]
        if key.split("/")[-1] in self.stopped or session in self.ended:
            return False
        # A stopped session's subagents cannot call tools, so they wait for it to end.
        sid = self.home.paths[session]["sid"]
        if "/" in key and (self.home.control / "stop" / f"session-{sid}.json").exists():
            return False
        # After care-kb asks its question, it waits for you and makes no more calls.
        return not (
            session == "kb"
            and (self.finished or (self.finish_at and now >= self.finish_at))
        )

    def tool_call(self, key, work, now):
        """The job's next tool call. A note that waits for it changes the call."""
        sid = self.home.paths[key]["sid"]
        text = None
        steered = self.deliver_note(key, now)
        react = REACT.get(work)
        if steered is not None and react and react[0] in steered.lower():
            name, inp, text = react[1], react[2], react[3]
        else:
            name, inp = WORK[work][self.step[key] % len(WORK[work])]
            self.step[key] += 1
            text = NOTED if steered is not None else None
        tid = self.home.tid("toolu_live")
        self.append(key, call(sid, tid, name, inp, now, text=text))
        done = now + self.home.rng.uniform(0.8, 2.2)
        self.pending.append((done, key, result(sid, tid, done)))

    def later_status(self, delay, key, status):
        self.status_due.append((time.time() + delay, key, status))

    def append(self, key, row):
        with open(self.home.paths[key]["transcript"], "a") as f:
            f.write(json.dumps(row) + "\n")

    def run(self):
        while not self.stop.wait(0.25):
            now = time.time()
            for due, key, row in [p for p in self.pending if p[0] <= now]:
                self.pending.remove((due, key, row))
                self.append(key, row)
            for due, key, status in [p for p in self.status_due if p[0] <= now]:
                self.status_due.remove((due, key, status))
                self.set_status(key, status)
            self.answer_controls(now)
            for key, work, every in self.JOBS:
                if not self.runs(key, now) or now < self.next[key]:
                    continue
                self.next[key] = now + every * self.home.rng.uniform(0.8, 1.2)
                if "/" in key or not self.end_turn(key):
                    self.tool_call(key, work, now)
            if self.finish_at and now >= self.finish_at:
                self.finish_kb(now)
                self.finish_at = None
                self.finished = True

    def finish_kb(self, now):
        kb = self.home.paths["kb"]
        text = "The plan FAQ is rewritten, and every link works.\n\nShould I run the care assistant evals against it?"
        self.append("kb", reply(kb["sid"], text, now, 140_000))
        self.set_status("kb", "idle")
        self.home.put(
            "waiting",
            "demo-kb.json",
            {
                "pid": self.home.fake_hook(),
                "session_id": "demo-kb",
                "since_ms": int(now * 1000),
            },
        )


def start_server(home, port):
    # From the package's parent folder, `-m agentforeman` finds this same copy, in a clone
    # and in an installed wheel.
    cmd = [
        sys.executable,
        "-m",
        "agentforeman",
        "serve",
        "--port",
        str(port),
        "--claude-dir",
        str(home.claude),
        "--codex-dir",
        str(home.codex),
        "--control-dir",
        str(home.control),
        "--dry-open",
    ]
    return subprocess.Popen(
        cmd, cwd=PACKAGE.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def run(port=4321, finish_after=None):
    # `kill` and a test runner send SIGTERM, which skips `finally` unless it raises.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    home = Home()
    writer = server = None
    try:
        home.build()
        writer = Writer(home, finish_after)
        writer.start()
        from agentforeman.server import load_token, sign_in_url

        # The demo shows sample data only, so its sign-in link can go to the terminal.
        link = sign_in_url(port, load_token(home.control))
        server = start_server(home, port)
        print(
            f"Demo at {link} (sample data in {home.root}). Press Ctrl+C to stop.",
            flush=True,
        )
        return server.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if writer:
            writer.stop.set()
            writer.join(5)
        if server:
            server.terminate()
            server.wait(10)
        home.close()
        shutil.rmtree(home.root, ignore_errors=True)
