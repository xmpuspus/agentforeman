"""Builds the AgentForeman state from the Claude session registry, transcripts, and Codex SQLite files.

Everything here is read-only. Security software on some Macs scans each file that AgentForeman
opens, so every reader keeps its file offset and only opens a file again when its size or mtime moved.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from agentforeman.diffs import approval_diff
from agentforeman.rulelib import hook

TAIL_BYTES = 2_000_000
RECENT_ACTIONS = 8
TIMELINE_ACTIONS = 40
MESSAGE_CHARS = 4000
KEEP_ACTIONS = 200
SUBAGENT_WINDOW_S = 6 * 3600
SUBAGENT_QUIET_S = 120
SUBAGENT_GONE_S = 15 * 60
TEAMMATE_ACTIVE_S = 60
SUBAGENTS_PER_SESSION = 12
CODEX_WINDOW_MS = 48 * 3600 * 1000
CODEX_REFRESH_S = 5
FALLBACK_REFRESH_S = 30
# The permission hook keeps this many characters of each input string.
HOOK_TEXT_CAP = 4000
REPEAT_WINDOW = 8
REPEAT_MIN = 4
TEAM_HEAD_BYTES = 64 * 1024
PS_REFRESH_S = 3
EDIT_WINDOW_MS = 30 * 60_000
NO_RULES = {"enabled": False, "items": [], "suggestions": [], "recent": []}

NOTICE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
NOTICE_FIELD = re.compile(r"<(task-id|tool-use-id|status)>(.*?)</\1>", re.S)
FINISHED = {"completed", "failed", "killed"}
CD_PREFIX = re.compile(r"^\s*cd\s+(\"[^\"]*\"|'[^']*'|\S+)\s*(&&|;|\n)\s*")
# Claude Code appends these rows again and again, so the transcript tail holds the newest title.
TITLE_ROWS = {
    "custom-title": "customTitle",
    "agent-name": "agentName",
    "ai-title": "aiTitle",
}
SUMMARY_KEY = {
    "Grep": "pattern",
    "Glob": "pattern",
    "Agent": "description",
    "Task": "description",
    "WebFetch": "url",
    "WebSearch": "query",
    "SendMessage": "to",
    "Skill": "skill",
}


def now_ms():
    return int(time.time() * 1000)


def project_dir_name(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def parse_ts(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(
            datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
        )
    except ValueError:
        return None


def summarize(name, inp):
    if not isinstance(inp, dict):
        return ""
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return os.path.basename(
            str(inp.get("file_path") or inp.get("notebook_path") or "")
        )
    if name == "Bash":
        return " ".join(CD_PREFIX.sub("", str(inp.get("command") or "")).split())[:80]
    key = SUMMARY_KEY.get(name)
    return str(inp.get(key) or "") if key else ""


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def message_text(text):
    """Collapses spaces but keeps line breaks. A long text keeps its end, where the ask usually is."""
    lines = []
    for line in str(text).splitlines():
        line = " ".join(line.split())
        if line or (lines and lines[-1]):
            lines.append(line)
    out = "\n".join(lines).strip()
    return out if len(out) <= MESSAGE_CHARS else "..." + out[-MESSAGE_CHARS:]


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def add_bins(total, bins):
    return [a + b for a, b in zip(total, bins)]


def repeating(actions):
    """The tool and target of at least 4 of the newest 8 calls, or None. A stuck agent loops so."""
    counts = Counter(
        (a["tool"], a["summary"]) for a in actions[:REPEAT_WINDOW] if a["summary"]
    )
    if not counts:
        return None
    (tool, target), n = counts.most_common(1)[0]
    return {"tool": tool, "target": target, "count": n} if n >= REPEAT_MIN else None


def epoch_ms(value):
    """A team config stores joinedAt as epoch ms, as a number or a string."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return parse_ts(value)


class Tail:
    """Reads only the complete lines a file gained since the last call."""

    def __init__(self, path):
        self.path = path
        self.offset = None
        self.size = -1
        self.mtime = -1

    def read(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return []
        if st.st_size == self.size and st.st_mtime_ns == self.mtime:
            return []
        first = self.offset is None or st.st_size < self.offset
        start = max(0, st.st_size - TAIL_BYTES) if first else self.offset
        try:
            with open(self.path, "rb") as f:
                f.seek(start)
                data = f.read(st.st_size - start)
        except OSError:
            return []
        if first and start > 0:
            cut = data.find(b"\n")
            data = data[cut + 1 :] if cut >= 0 else b""
            start = st.st_size - len(data)
        end = data.rfind(b"\n") + 1
        self.offset = start + end
        self.size, self.mtime = st.st_size, st.st_mtime_ns
        rows = []
        for line in data[:end].splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


class Transcript:
    """What one transcript says: actions, results, the last reply, and subagent completions."""

    def __init__(self, path):
        self.path = path
        self.tail = Tail(path)
        self.actions = {}
        self.order = []
        self.results = {}
        self.by_tool_use = {}
        self.by_task = {}
        self.last_assistant = None
        self.first_ms = None
        self.last_entry_ms = None
        self.last_message = None
        self.last_prompt = None
        self.interrupted_ms = None
        self.model = None
        self.context_tokens = None
        self.titles = {}
        self.edits = {}  # file path -> ts_ms of the newest edit call

    @property
    def title(self):
        return next((self.titles[k] for k in TITLE_ROWS if self.titles.get(k)), None)

    def update(self):
        for row in self.tail.read():
            self._take(row)

    def _take(self, row):
        if row.get("isSidechain") and "agentId" not in row:
            return
        kind = row.get("type")
        if kind in TITLE_ROWS:
            text = " ".join(str(row.get(TITLE_ROWS[kind]) or "").split())
            if text:
                self.titles[kind] = text[:120]
            return
        ts = parse_ts(row.get("timestamp"))
        if kind == "queue-operation":
            self._notices(str(row.get("content") or ""), ts)
            return
        if kind == "attachment":
            att = row.get("attachment") or {}
            if att.get("type") == "queued_command":
                self._notices(str(att.get("prompt") or ""), ts)
            return
        if ts and kind in ("user", "assistant"):
            self.first_ms = min(self.first_ms or ts, ts)
            self.last_entry_ms = max(self.last_entry_ms or 0, ts)
        msg = row.get("message") or {}
        content = msg.get("content")
        if kind == "user":
            self._take_user(content, ts, bool(row.get("isMeta")))
        elif kind == "assistant":
            self._take_assistant(msg, content, ts)

    def _notices(self, text, ts):
        if "<task-notification>" not in text:
            return
        for body in NOTICE.findall(text):
            fields = dict(NOTICE_FIELD.findall(body))
            status = fields.get("status", "").strip()
            for key, store in (
                ("task-id", self.by_task),
                ("tool-use-id", self.by_tool_use),
            ):
                ident = fields.get(key, "").strip()
                if ident and (
                    ident not in store or (ts or 0) >= (store[ident][1] or 0)
                ):
                    store[ident] = (status, ts)

    def _take_user(self, content, ts, meta=False):
        has_result = False
        if isinstance(content, list):
            for c in content:
                if (
                    isinstance(c, dict)
                    and c.get("type") == "tool_result"
                    and c.get("tool_use_id")
                ):
                    has_result = True
                    tid = c["tool_use_id"]
                    self.results[tid] = (ts, bool(c.get("is_error")))
                    act = self.actions.get(tid)
                    if act:
                        act.update(done=True, end_ms=ts, error=bool(c.get("is_error")))
        text = text_of(content)
        self._notices(text, ts)
        clean = " ".join(text.split())
        if clean.startswith("[Request interrupted"):
            self.interrupted_ms = ts or 0
        # isMeta rows hold skill text that Claude Code adds, not something the user typed.
        if (
            not has_result
            and not meta
            and clean
            and not clean.startswith(("<", "[Request interrupted", "Caveat"))
        ):
            self.last_prompt = clean[:200]

    def _take_assistant(self, msg, content, ts):
        # A reply after an interruption means the agent runs again.
        self.interrupted_ms = None
        tool_ids = []
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("id"):
                tid = c["id"]
                tool_ids.append(tid)
                if tid not in self.actions:
                    self.order.append(tid)
                inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                target = hook.file_path(inp)
                if c.get("name") in hook.EDIT_TOOLS and target and ts:
                    target = os.path.normpath(target)
                    self.edits[target] = max(self.edits.get(target, 0), ts)
                result = self.results.get(tid)
                self.actions[tid] = {
                    "tool": c.get("name") or "",
                    "summary": summarize(c.get("name"), inp),
                    "desc": str(inp.get("description") or "")[:120]
                    if c.get("name") == "Bash"
                    else "",
                    "ts_ms": ts,
                    "done": result is not None,
                    "end_ms": result[0] if result else None,
                    "error": bool(result and result[1]),
                }
            elif c.get("type") == "text" and str(c.get("text", "")).strip():
                self.last_message = message_text(c["text"])
        if len(self.order) > KEEP_ACTIONS:
            for tid in self.order[:-KEEP_ACTIONS]:
                self.actions.pop(tid, None)
            self.order = self.order[-KEEP_ACTIONS:]
        self.last_assistant = {
            "stop": msg.get("stop_reason"),
            "ts_ms": ts,
            "tools": tool_ids,
        }
        model = msg.get("model")
        if model and model != "<synthetic>":
            self.model = model
        usage = msg.get("usage")
        if isinstance(usage, dict):
            keys = (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
            self.context_tokens = sum(int(usage.get(k) or 0) for k in keys)

    def recent(self, n=RECENT_ACTIONS):
        return [
            dict(self.actions[t])
            for t in reversed(self.order[-n:])
            if t in self.actions
        ]

    def activity(self, at_ms):
        bins = [0] * 30
        for t in self.order:
            ts = self.actions[t]["ts_ms"]
            if ts is not None and 0 <= at_ms - ts < 30 * 60_000:
                bins[29 - int((at_ms - ts) // 60_000)] += 1
        return bins

    def unanswered(self):
        la = self.last_assistant
        return bool(la and any(t not in self.results for t in la["tools"]))

    def recent_edits(self, at_ms):
        """Files edited in the last 30 minutes. Older ones leave memory too."""
        self.edits = {
            p: t for p, t in self.edits.items() if at_ms - t <= EDIT_WINDOW_MS
        }
        return self.edits


class Subagents:
    """The subagents folder of one session, listed again only when the folder changes."""

    def __init__(self, folder):
        self.folder = folder
        self.mtime = None
        self.files = []
        self.meta = {}
        self.transcripts = {}
        self.shown = []  # the transcripts of the last view

    def update(self):
        try:
            mtime = os.stat(self.folder).st_mtime_ns
        except OSError:
            self.files = []
            return
        if mtime != self.mtime:
            self.mtime = mtime
            try:
                self.files = sorted(
                    n
                    for n in os.listdir(self.folder)
                    if n.startswith("agent-") and n.endswith(".jsonl")
                )
            except OSError:
                self.files = []

    def _meta(self, agent_id):
        if agent_id not in self.meta:
            try:
                with open(
                    os.path.join(self.folder, f"agent-{agent_id}.meta.json")
                ) as f:
                    self.meta[agent_id] = json.load(f)
            except (OSError, ValueError):
                self.meta[agent_id] = {}
        return self.meta[agent_id]

    def view(self, parent, at_s):
        """Returns the shown subagents, newest first, and how many changed in the window."""
        found = []
        for name in self.files:
            path = os.path.join(self.folder, name)
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if at_s - mtime <= SUBAGENT_WINDOW_S:
                found.append((mtime, name[len("agent-") : -len(".jsonl")], path))
        found.sort(reverse=True)
        total = len(found)
        loaded = []
        for mtime, agent_id, path in found[:SUBAGENTS_PER_SESSION]:
            tr = self.transcripts.setdefault(agent_id, Transcript(path))
            tr.update()
            loaded.append((mtime, agent_id, self._meta(agent_id), tr))
        self.shown = [tr for *_, tr in loaded]
        # A subagent can start its own subagents. They share this folder, and the
        # Agent call that started each one sits in its parent subagent's transcript.
        owner = {tid: aid for _, aid, _, tr in loaded for tid in tr.actions}
        views = []
        for mtime, agent_id, meta, tr in loaded:
            parent_id = owner.get(meta.get("toolUseId"))
            if parent_id == agent_id:
                parent_id = None
            caller = self.transcripts[parent_id] if parent_id else parent
            view = self._one(agent_id, meta, tr, caller, mtime, at_s)
            view["parent_agent_id"] = parent_id
            view["depth"] = int(meta.get("spawnDepth") or (2 if parent_id else 1))
            views.append(view)
        return views, total

    @staticmethod
    def _finish(agent_id, meta, tr, caller, mtime, at_s):
        """Returns (done, outcome, ended_ms) for one subagent."""
        tool_use_id = meta.get("toolUseId")
        if meta.get("requestShape") == "background":
            notice = (
                caller.by_task.get(agent_id) or caller.by_tool_use.get(tool_use_id)
                if caller
                else None
            )
            # A notice older than the subagent's newest entry belongs to an earlier
            # run that was resumed since.
            last = tr.last_entry_ms or mtime * 1000
            if notice and notice[0] in FINISHED and (notice[1] or 0) >= last - 5000:
                return True, notice[0], notice[1]
        elif caller is not None and tool_use_id in caller.results:
            end_ms, error = caller.results[tool_use_id]
            return True, "failed" if error else "completed", end_ms
        if tr.interrupted_ms is not None:
            return True, "killed", tr.interrupted_ms or int(mtime * 1000)
        quiet = at_s - mtime
        la = tr.last_assistant
        if la and la["stop"] == "end_turn" and quiet >= SUBAGENT_QUIET_S:
            return True, "completed", int(mtime * 1000)
        if quiet >= SUBAGENT_GONE_S and not tr.unanswered():
            return True, "ended", int(mtime * 1000)
        return False, None, None

    @classmethod
    def _one(cls, agent_id, meta, tr, caller, mtime, at_s):
        at_ms = int(at_s * 1000)
        teammate = meta.get("taskKind") == "in_process_teammate"
        if teammate:
            busy = at_s - mtime < TEAMMATE_ACTIVE_S or tr.unanswered()
            state, outcome, ended = ("running" if busy else "idle"), None, None
        else:
            done, outcome, ended = cls._finish(agent_id, meta, tr, caller, mtime, at_s)
            state = "done" if done else "running"
        start_call = caller.actions.get(meta.get("toolUseId")) if caller else None
        recent = tr.recent(RECENT_ACTIONS)
        return {
            "agent_id": agent_id,
            "kind": "teammate" if teammate else "subagent",
            "name": meta.get("name") or meta.get("description") or agent_id,
            "description": meta.get("description") or "",
            "model": meta.get("model") or "",
            "agent_type": meta.get("agentType") or "",
            "state": state,
            "outcome": outcome,
            "started_ms": (start_call or {}).get("ts_ms") or tr.first_ms,
            "ended_ms": ended,
            "tool_calls": len(tr.order),
            "last_action": recent[0] if recent else None,
            "recent_actions": recent,
            "last_message": tr.last_message,
            "activity": tr.activity(at_ms),
            "updated_ms": int(mtime * 1000),
        }


class Teams:
    """Split-pane (tmux) Agent Teams teammates. Each one runs as its own claude process and
    writes its own top-level transcript in the project folder of its cwd."""

    def __init__(self, claude):
        self.root = claude / "teams"
        self.projects = claude / "projects"
        self.configs = {}  # config path -> (mtime, data)
        self.listings = {}  # project folder -> (mtime, .jsonl names)
        self.heads = {}  # transcript path -> (mtime, (teamName, agentName) or None, final)
        self.found = {}  # (team, member name, joinedAt) -> transcript path
        self.transcripts = {}
        self.members = (
            set()
        )  # (team, name) of every split-pane member, live lead or not
        self.ps = (
            0.0,
            [],
            frozenset(),
        )  # (time, command lines, transcripts it checked)
        # Teammate session id -> (name, recent edits). A split-pane teammate is a session of
        # its own, so the same-file guard counts its edits under its own id.
        self.edits = {}

    def _configs(self):
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        seen = {}
        for name in names:
            path = os.path.join(self.root, name, "config.json")
            try:
                mtime = os.stat(path).st_mtime_ns
            except OSError:
                continue
            hit = self.configs.get(path)
            if not hit or hit[0] != mtime:
                try:
                    with open(path) as f:
                        hit = (mtime, json.load(f))
                except (OSError, ValueError):
                    continue
            seen[path] = hit
        self.configs = seen
        return [data for _, data in seen.values() if isinstance(data, dict)]

    def _listing(self, folder):
        try:
            mtime = os.stat(folder).st_mtime_ns
        except OSError:
            return []
        hit = self.listings.get(folder)
        if not hit or hit[0] != mtime:
            try:
                names = [n for n in os.listdir(folder) if n.endswith(".jsonl")]
            except OSError:
                names = []
            hit = self.listings[folder] = (mtime, names)
        return hit[1]

    def _head(self, path, mtime):
        """(teamName, agentName) from the first 64 KB. A hit stays, and so does a miss whose
        first 64 KB were already full. Another miss is read again only when its mtime moves."""
        hit = self.heads.get(path)
        if hit and (hit[1] or hit[2] or hit[0] == mtime):
            return hit[1]
        found = None
        try:
            with open(path, "rb") as f:
                data = f.read(TEAM_HEAD_BYTES)
        except OSError:
            data = b""
        for line in data.split(b"\n"):
            if b"teamName" not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("teamName") and row.get("agentName"):
                found = (row["teamName"], row["agentName"])
                break
        self.heads[path] = (mtime, found, len(data) == TEAM_HEAD_BYTES)
        return found

    def _find(self, team, member):
        """The newest top-level transcript changed after the member joined, whose rows carry
        its team and name."""
        key = (team, member.get("name"), member.get("joinedAt"))
        path = self.found.get(key)
        if path and os.path.exists(path):
            return path
        if not member.get("cwd"):
            return None  # never list the projects folder itself
        folder = self.projects / project_dir_name(str(member["cwd"]))
        joined_s = (epoch_ms(member.get("joinedAt")) or 0) / 1000
        best = None
        for name in self._listing(folder):
            path = str(folder / name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < joined_s or self._head(path, st.st_mtime_ns) != key[:2]:
                continue
            if best is None or st.st_mtime > best[0]:
                best = (st.st_mtime, path)
        if best:
            self.found[key] = best[1]
        return best[1] if best else None

    def _commands(self, shown):
        """Command lines of every process, from at most one `ps` call per tick. The list is
        kept a few seconds, but a new teammate gets a fresh one, as its process just started."""
        stamp, cmds, checked = self.ps
        if time.time() - stamp < PS_REFRESH_S and shown <= checked:
            return cmds
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
            ).stdout
            cmds = [f" {line.strip()} " for line in out.splitlines()]
        except (OSError, subprocess.SubprocessError):
            pass  # keep the last list
        self.ps = (time.time(), cmds, shown)
        return cmds

    def is_mate(self, path):
        """True when a transcript belongs to a split-pane member of any team. A teammate
        whose lead died still runs its own claude process, and it must not show as a session."""
        if not self.members or not path:
            return False
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return False
        return self._head(path, mtime) in self.members

    @staticmethod
    def _alive(cmds, team, lead, member):
        agent = f" --agent-id {member.get('name')}@{team} "
        parent = f" --parent-session-id {lead} "
        return any(agent in c and parent in c for c in cmds)

    def view(self, live_ids, at_s):
        """Returns {lead session id: [teammate views]} and the set of teammate transcripts."""
        found, members = [], set()
        for cfg in self._configs():
            lead = cfg.get("leadSessionId")
            team = str(cfg.get("name") or "")
            for m in cfg.get("members") or []:
                if not isinstance(m, dict) or m.get("backendType") == "in-process":
                    continue
                members.add((team, m.get("name")))
                if lead not in live_ids:
                    continue
                path = self._find(team, m)
                if path:
                    found.append((team, lead, m, path))
        self.members = members
        paths = {f[3] for f in found}
        cmds = self._commands(frozenset(paths)) if found else []
        out = {}
        self.edits = {}
        for team, lead, m, path in found:
            tr = self.transcripts.get(path) or self.transcripts.setdefault(
                path, Transcript(path)
            )
            tr.update()
            alive = self._alive(cmds, team, lead, m)
            view = self._one(m, tr, path, alive, at_s)
            out.setdefault(lead, []).append(view)
            if alive:
                self.edits[view["agent_id"]] = (
                    view["name"],
                    tr.recent_edits(int(at_s * 1000)),
                )
        for stale in set(self.transcripts) - paths:
            del self.transcripts[stale]
        self.found = {k: v for k, v in self.found.items() if v in paths}
        return out, paths

    @staticmethod
    def _one(m, tr, path, alive, at_s):
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = 0
        if alive:
            busy = at_s - mtime < TEAMMATE_ACTIVE_S or tr.unanswered()
            state, outcome, ended = ("running" if busy else "idle"), None, None
        else:
            state, outcome, ended = "done", "ended", int(mtime * 1000)
        recent = tr.recent(RECENT_ACTIONS)
        prompt = str(m.get("prompt") or "").strip()
        sid = Path(path).stem
        return {
            # Hooks in the teammate's process carry this as session_id and no agent_id.
            "agent_id": sid,
            "kind": "teammate",
            "backend": "tmux",
            "name": str(m.get("name") or sid),
            "description": (prompt.splitlines() or [""])[0][:200],
            "model": m.get("model") or tr.model or "",
            "agent_type": m.get("agentType") or "",
            "state": state,
            "outcome": outcome,
            "started_ms": epoch_ms(m.get("joinedAt")) or tr.first_ms,
            "ended_ms": ended,
            "tool_calls": len(tr.order),
            "last_action": recent[0] if recent else None,
            "recent_actions": recent,
            "last_message": tr.last_message,
            "activity": tr.activity(int(at_s * 1000)),
            "updated_ms": int(mtime * 1000),
            "parent_agent_id": None,
            "depth": 1,
        }


class Collector:
    def __init__(self, claude_dir, codex_dir, control=None):
        self.claude = Path(claude_dir).expanduser()
        self.codex = Path(codex_dir).expanduser()
        self.control = control
        self.registry = {}
        self.transcripts = {}
        self.subagents = {}
        self.teams = Teams(self.claude)
        self.mate_status = {}
        self.fallback = {}
        self.codex_cache = (0.0, [])
        self.codex_error = None
        # Request id -> (diff, note). A Write diff opens the file, so each request reads it once.
        self.diffs = {}
        self.diffs_seen = set()

    def direct_path(self, entry):
        if not entry.get("cwd") or not entry.get("sessionId"):
            return None
        return str(
            self.claude
            / "projects"
            / project_dir_name(entry["cwd"])
            / f"{entry['sessionId']}.jsonl"
        )

    def live_sessions(self):
        folder = self.claude / "sessions"
        try:
            names = {n for n in os.listdir(folder) if n.endswith(".json")}
        except OSError:
            return []
        for gone in set(self.registry) - names:
            del self.registry[gone]
        live = []
        for name in names:
            path = folder / name
            try:
                mtime = os.stat(path).st_mtime_ns
            except OSError:
                continue
            cached = self.registry.get(name)
            if not cached or cached[0] != mtime:
                try:
                    with open(path) as f:
                        cached = (mtime, json.load(f))
                except (OSError, ValueError):
                    continue
                self.registry[name] = cached
            entry = cached[1]
            if isinstance(entry, dict) and entry.get("pid") and pid_alive(entry["pid"]):
                live.append(entry)
        return live

    def _transcript_path(self, entry, claimed):
        direct = self.direct_path(entry)
        if direct is None:
            return None
        if os.path.exists(direct):
            return direct
        sid = entry["sessionId"]
        hit = self.fallback.get(sid)
        if (
            hit
            and time.time() - hit[0] < FALLBACK_REFRESH_S
            and (hit[1] is None or os.path.exists(hit[1]))
        ):
            return hit[1] if hit[1] not in claimed else None
        folder = os.path.dirname(direct)
        started = (entry.get("startedAt") or 0) / 1000
        best = None
        try:
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if not name.endswith(".jsonl") or path in claimed:
                    continue
                mtime = os.stat(path).st_mtime
                if mtime >= started and (best is None or mtime > best[0]):
                    best = (mtime, path)
        except OSError:
            pass
        found = best[1] if best else None
        self.fallback[sid] = (time.time(), found)
        return found

    @staticmethod
    def _columns(con, table):
        return {r[1] for r in con.execute(f"pragma table_info({table})")}

    def codex_threads(self):
        stamp, cached = self.codex_cache
        if time.time() - stamp < CODEX_REFRESH_S:
            return cached
        threads = []
        state_db = self.codex / "state_5.sqlite"
        if state_db.exists():
            try:
                con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=1)
                try:
                    cols = self._columns(con, "threads")
                    extra = [
                        c if c in cols else "null"
                        for c in ("thread_source", "agent_nickname")
                    ]
                    rows = con.execute(
                        f"select id, title, cwd, model, updated_at_ms, {extra[0]}, {extra[1]} from threads "
                        "where archived = 0 and updated_at_ms >= ? order by updated_at_ms desc limit 20",
                        (now_ms() - CODEX_WINDOW_MS,),
                    ).fetchall()
                    parents = {}
                    if self._columns(con, "thread_spawn_edges"):
                        parents = dict(
                            con.execute(
                                "select child_thread_id, parent_thread_id from thread_spawn_edges"
                            )
                        )
                finally:
                    con.close()
                threads = [
                    {
                        "thread_id": r[0],
                        "title": r[1] or "",
                        "cwd": r[2] or "",
                        "model": r[3] or "",
                        "updated_ms": r[4],
                        "status": None,
                        "source": r[5] or "",
                        "nickname": r[6] or "",
                        "parent_thread_id": parents.get(r[0]),
                    }
                    for r in rows
                ]
            except sqlite3.Error as e:
                self._codex_failed(state_db, e)
                threads = []
        hist_db = self.codex / "thread_history_1.sqlite"
        if threads and hist_db.exists():
            try:
                con = sqlite3.connect(f"file:{hist_db}?mode=ro", uri=True, timeout=1)
                try:
                    for t in threads:
                        row = con.execute(
                            "select status from thread_turns where thread_id = ? order by started_at desc, rowid desc limit 1",
                            (t["thread_id"],),
                        ).fetchone()
                        t["status"] = row[0] if row else None
                finally:
                    con.close()
            except sqlite3.Error as e:
                self._codex_failed(hist_db, e)
        self.codex_cache = (time.time(), threads)
        return threads

    def _codex_failed(self, db, err):
        """Prints one line per new error, not one every refresh."""
        line = f"Codex threads: cannot read {db.name}: {err}"
        if line != self.codex_error:
            print(line, file=sys.stderr, flush=True)
        self.codex_error = line

    def _control_state(self):
        ctl = self.control
        if ctl is None:
            return {
                "requests": {},
                "keys": [],
                "waiting": {},
                "replies": {},
                "stops": {},
                "notes": {},
                "delivered": {},
                "installed": None,
                "rules": NO_RULES,
                "guard": "warn",
            }
        waiting = ctl.waiting()
        replies = ctl.pending_replies()
        # A reply that no live hook waits for can never arrive, so drop it.
        for sid in set(replies) - set(waiting):
            (ctl.dir / "replies" / f"{sid}.json").unlink(missing_ok=True)
            del replies[sid]
        notes, delivered = ctl.notes()
        requests = ctl.requests()
        return {
            "requests": requests,
            # Every waiting call in any session, with its rule key, for "similar".
            "keys": [
                (r, hook.rule_key(r.get("tool_name"), r.get("tool_input")))
                for rs in requests.values()
                for r in rs
            ],
            "waiting": waiting,
            "replies": replies,
            "stops": ctl.stops(),
            "notes": notes,
            "delivered": delivered,
            "installed": ctl.installed(),
            "rules": ctl.rules_state(),
            "guard": ctl.guard_mode(),
        }

    def snapshot(self):
        at_ms = now_ms()
        self.ctl = self._control_state()
        live = self.live_sessions()
        mates, mate_paths = self.teams.view(
            {e.get("sessionId") for e in live}, at_ms / 1000
        )
        if self.control:
            alive = {e.get("sessionId") for e in live} | {
                s["agent_id"]
                for subs in mates.values()
                for s in subs
                if s["state"] != "done"
            }
            self._drop_orphans(alive)
        # A split-pane teammate runs its own claude process. It shows under its lead only.
        mate_ids = {Path(p).stem for p in mate_paths}
        mate_ids |= {
            e.get("sessionId") for e in live if self.teams.is_mate(self.direct_path(e))
        }
        # A teammate's turn ends as a session's does: its own registry status leaves busy.
        self.mate_status = {
            e.get("sessionId"): e.get("status")
            for e in live
            if e.get("sessionId") in mate_ids
        }
        live = sorted(
            (e for e in live if e.get("sessionId") not in mate_ids),
            key=lambda x: x.get("startedAt") or 0,
        )
        direct = {p for p in map(self.direct_path, live) if p}
        agents, edits, used = [], [], set()
        self.diffs_seen = set()
        for e in live:
            claimed = direct | used | mate_paths
            path = self._transcript_path(e, claimed - {self.direct_path(e)})
            tr = None
            if path:
                used.add(path)
                tr = self.transcripts.get(path) or self.transcripts.setdefault(
                    path, Transcript(path)
                )
                tr.update()
            agents.append(
                self._agent(e, tr, path, at_ms, mates.get(e.get("sessionId"), []))
            )
            edits.append(self._edits_of(tr, path, at_ms))
        for stale in set(self.transcripts) - used:
            del self.transcripts[stale]
        self._forget_ended(live, used)
        self.diffs = {k: v for k, v in self.diffs.items() if k in self.diffs_seen}
        files = self._shared(agents, edits)
        if self.control:
            self.control.write_edits(files)
        counts = {"working": 0, "your_turn": 0, "waiting": 0, "idle": 0}
        for a in agents:
            counts[a["status"]] += 1
        return {
            "generated_at": at_ms,
            "agents": agents,
            "codex": self.codex_threads(),
            "counts": counts,
            "controls_installed": self.ctl["installed"],
            "rules": self.ctl["rules"],
            "guard": {"mode": self.ctl["guard"]},
        }

    def _forget_ended(self, live, used):
        """Drops the subagent trackers and fallback paths of sessions that ended, so a server
        that runs for weeks does not grow."""
        folders = {os.path.join(os.path.splitext(p)[0], "subagents") for p in used}
        for gone in set(self.subagents) - folders:
            del self.subagents[gone]
        sids = {e.get("sessionId") for e in live}
        for gone in set(self.fallback) - sids:
            del self.fallback[gone]

    def _edits_of(self, tr, path, at_ms):
        """Files that the session and its subagents edited in the last 30 minutes."""
        sa = (
            self.subagents.get(os.path.join(os.path.splitext(path)[0], "subagents"))
            if path
            else None
        )
        found = {}
        for t in ([tr] if tr else []) + (sa.shown if sa else []):
            for p, ms in t.recent_edits(at_ms).items():
                found[p] = max(found.get(p, 0), ms)
        return found

    def _shared(self, agents, edits):
        """Sets each session's shared_files and returns the files map for edits.json."""
        owners = [(a["session_id"], a["name"], e) for a, e in zip(agents, edits)]
        owners += [(sid, name, e) for sid, (name, e) in self.teams.edits.items()]
        files = {}
        for sid, name, e in owners:
            for p, ms in e.items():
                files.setdefault(p, []).append(
                    {"session_id": sid, "name": name, "last_ms": ms}
                )
        for a, e in zip(agents, edits):
            a["shared_files"] = [
                {"path": p, "with": others}
                for p in sorted(e)
                if (
                    others := [
                        {"session_id": x["session_id"], "name": x["name"]}
                        for x in files[p]
                        if x["session_id"] != a["session_id"]
                    ]
                )
            ]
        return {
            p: sorted(v, key=lambda x: x["session_id"])
            for p, v in sorted(files.items())
        }

    @staticmethod
    def _status(e, tr):
        reg = e.get("status")
        la = tr.last_assistant if tr else None
        if reg == "busy":
            return (
                "working",
                False,
                e.get("statusUpdatedAt") or (tr.last_entry_ms if tr else None),
            )
        if reg == "waiting":
            return "waiting", False, la["ts_ms"] if la else None
        if la and la["stop"] == "end_turn":
            return "your_turn", False, la["ts_ms"]
        if tr and tr.unanswered():
            return "waiting", True, la["ts_ms"]
        return "idle", False, tr.last_entry_ms if tr else None

    def _approval(self, req, cwd):
        inp = req.get("tool_input") if isinstance(req.get("tool_input"), dict) else {}
        tool = req.get("tool_name") or ""
        detail = str(inp.get("command") or inp.get("file_path") or inp.get("url") or "")
        key = hook.rule_key(tool, inp)
        rid = req.get("id")
        if rid not in self.diffs:
            self.diffs[rid] = approval_diff(tool, inp, cwd)
        self.diffs_seen.add(rid)
        diff, note = self.diffs[rid]
        similar = sum(1 for r, k in self.ctl["keys"] if k == key and r.get("id") != rid)
        return {
            "request_id": rid,
            "tool": tool,
            "summary": summarize(tool, inp),
            # The whole stored command, because Approve runs exactly this.
            "detail": detail,
            "detail_cut": len(detail) >= HOOK_TEXT_CAP,
            "desc": str(inp.get("description") or "")[:200],
            "agent_id": req.get("agent_id"),
            "created_ms": req.get("created_ms"),
            "diff": diff,
            "diff_note": note,
            "rule_key": key,
            "rule_ok": hook.rule_valid(key),
            "similar": similar,
        }

    def _note(self, key):
        """What the page shows about the newest note for this thread."""
        pending = self.ctl["notes"].get(key)
        if pending:
            return {
                "pending": True,
                "text": pending.get("text"),
                "at_ms": pending.get("at_ms"),
                "delivered_ms": None,
            }
        got = self.ctl["delivered"].get(key) or {}
        sent = self.control.sent.get(got.get("id")) if self.control and got else None
        return {
            "pending": False,
            "text": sent[0] if sent else None,
            "at_ms": sent[1] if sent else None,
            "delivered_ms": got.get("delivered_ms"),
        }

    def _drop_orphans(self, alive):
        """A note or stop for a session that ended can never arrive, so it goes at once."""
        for kind in ("notes", "stops"):
            for key, data in list(self.ctl[kind].items()):
                if (data or {}).get("session_id") not in alive:
                    self._drop(kind, key)

    def _drop(self, kind, key):
        """Deletes a note or stop that can no longer arrive, and forgets it for this tick."""
        if key in self.ctl[kind]:
            del self.ctl[kind][key]
            if kind == "notes":
                self.control.clear_note(key)
            else:
                self.control.clear_stop(key)

    def _sub_controls(self, subs):
        for s in subs:
            tmux = s.get("backend") == "tmux"
            stop = f"session-{s['agent_id']}" if tmux else s["agent_id"]
            # A thread is over when it is done. A teammate's turn also ends when its own
            # registry status leaves busy. Without an entry, it waits for its process to end.
            reg = self.mate_status.get(s["agent_id"]) if tmux else None
            over = s["state"] == "done" or reg not in (None, "busy")
            if over and self.control:
                self._drop("stops", stop)
                self._drop("notes", s["agent_id"])
            s["stop_requested"] = stop in self.ctl["stops"]
            s["note"] = self._note(s["agent_id"])
            s["repeating"] = repeating(s["recent_actions"])

    def _agent(self, e, tr, path, at_ms, mates=()):
        status, guess, since = self._status(e, tr)
        sid = e.get("sessionId") or ""
        reqs = list(self.ctl["requests"].get(sid) or [])
        # A split-pane teammate asks in its own session, which shows only under its lead.
        for m in mates:
            got = self.ctl["requests"].get(m["agent_id"]) or []
            reqs += [{**r, "agent_id": m["agent_id"]} for r in got]
        reqs.sort(key=lambda r: r.get("created_ms") or 0)
        approval = self._approval(reqs[0], e.get("cwd") or "") if reqs else None
        if approval:
            status, guess, since = "waiting", False, approval["created_ms"]
        current = None
        if tr and status in ("working", "waiting"):
            pending = next((a for a in tr.recent(KEEP_ACTIONS) if not a["done"]), None)
            if pending:
                current = {**pending, "started_ms": pending["ts_ms"]}
        subs, total = [], 0
        if path:
            folder = os.path.join(os.path.splitext(path)[0], "subagents")
            sa = self.subagents.setdefault(folder, Subagents(folder))
            sa.update()
            subs, total = sa.view(tr, at_ms / 1000)
        subs, total = subs + list(mates), total + len(mates)
        self._sub_controls(subs)
        busy = e.get("status") == "busy"
        if not busy and self.control:
            # The turn ended, so the next tool call starts a new turn.
            self._drop("notes", sid)
            self._drop("stops", f"session-{sid}")
        activity = tr.activity(at_ms) if tr else [0] * 30
        team = activity
        for s in subs:
            team = add_bins(team, s["activity"])
        busy_subs = [s["updated_ms"] for s in subs if s["state"] == "running"]
        last_entry = tr.last_entry_ms if tr else None
        stamps = [t for t in [last_entry, *busy_subs] if t]
        cwd = e.get("cwd") or ""
        installed = self.ctl["installed"]
        bridge = e.get("bridgeSessionId")
        return {
            "kind": "claude",
            "pid": e.get("pid"),
            "session_id": sid,
            "name": e.get("name") or sid[:8],
            "title": tr.title if tr else None,
            "cwd": cwd,
            "project": os.path.basename(cwd.rstrip("/")) or cwd,
            "entrypoint": e.get("entrypoint") or "",
            "status": status,
            "status_guess": guess,
            "since_ms": since,
            "current_action": current,
            "recent_actions": tr.recent() if tr else [],
            "last_action": (tr.recent(1) or [None])[0] if tr else None,
            "timeline": tr.recent(TIMELINE_ACTIONS) if tr else [],
            "last_message": tr.last_message if tr else None,
            "last_prompt": tr.last_prompt if tr else None,
            "activity": activity,
            "team_activity": team,
            "model": tr.model if tr else None,
            "context_tokens": tr.context_tokens if tr else None,
            "transcript": path,
            "subagents": subs,
            "subagents_total": total,
            "started_ms": e.get("startedAt"),
            "last_entry_ms": last_entry,
            "last_activity_ms": max(stamps) if stamps else None,
            "approval": approval,
            "approvals_waiting": len(reqs),
            "reply_ready": sid in self.ctl["waiting"],
            "reply_pending": self.ctl["replies"].get(sid),
            "busy": busy,
            "note": self._note(sid),
            "stop_requested": f"session-{sid}" in self.ctl["stops"],
            "repeating": repeating(tr.recent()) if tr else None,
            # Running sessions reload ~/.claude/settings.json, so they pick up the hooks too.
            "controls": bool(installed),
            "remote_url": f"https://claude.ai/code/{bridge}"
            if isinstance(bridge, str) and bridge.startswith("session_")
            else None,
        }
