"""The server's side of the control folder: approval requests, replies, stops, steering notes,
rules, and the same-file guard.

The hooks in agentforeman_hook.py write requests, waiting files, note receipts, and the
auto-allow log. The server writes decisions, replies, stop files, notes, rules, the guard
mode, and edits.json. A file whose hook process is gone is stale, so the server ignores and
deletes it. SPEC.md, sections Controls and Foreman, hold the contract.
"""

import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

from agentforeman.collector import pid_alive
from agentforeman.rulelib import HOOK_SOURCE, hook

STOP_MAX_S = 6 * 3600
NOTE_MAX_S = 6 * 3600
RECEIPT_MAX_S = 3600
SENT_KEEP = 200
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
TEXT_CAP = 4000
GUARD_MODES = ("off", "warn", "block")
SUGGEST_MIN = 3
RECENT_AUTO = 20
EDITS_REWRITE_S = 60
AUTO_FIELDS = ("at_ms", "session_id", "agent_id", "tool_name", "summary", "rule")


def safe_id(value):
    value = str(value or "")
    return value if SAFE_ID.match(value) else None


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def now_ms():
    return int(time.time() * 1000)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def write_json_once(path, obj):
    """Writes the file only when it does not exist. Returns False when it exists already.
    The link is atomic, so of two answers that race, only the first one lands."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj))
    try:
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


class Control:
    def __init__(self, folder):
        self.dir = Path(folder).expanduser()
        self._hook_sha = (None, "")  # (file stamp, hash) of agentforeman_hook.py
        # Note id -> (text, at_ms). A receipt holds only the id, so the page keeps the text here.
        self.sent = {}
        # The server answers each POST on its own thread, and rules.json and approvals.json
        # are read, changed, and written whole.
        self.lock = threading.Lock()
        self._cache = {}  # file name -> ((mtime, size), data)
        self._approvals = None  # rule key -> allows through /api/approve
        self._auto = (
            0,
            {},
            deque(maxlen=RECENT_AUTO),
        )  # (offset, hits per rule, newest)
        self._edits = (None, 0.0)  # (files last written, time.time() of that write)

    def _files(self, sub):
        try:
            return [
                self.dir / sub / n
                for n in os.listdir(self.dir / sub)
                if n.endswith(".json")
            ]
        except OSError:
            return []

    def installed(self):
        info = read_json(self.dir / "installed.json")
        if not info:
            return None
        # An update can change the hook while the server runs, so the hash follows the file.
        try:
            st = HOOK_SOURCE.stat()
            stamp = (st.st_mtime_ns, st.st_size)
            if self._hook_sha[0] != stamp:
                self._hook_sha = (
                    stamp,
                    hashlib.sha256(HOOK_SOURCE.read_bytes()).hexdigest(),
                )
        except OSError:
            self._hook_sha = (None, "")
        return {
            "installed_ms": info.get("installed_ms"),
            "current": info.get("hook_sha256") == self._hook_sha[1],
        }

    def _live(self, sub):
        """Files of this kind whose hook process still runs. Deletes the stale ones."""
        out = []
        for path in self._files(sub):
            data = read_json(path)
            if data is None:
                continue
            if not pid_alive(data.get("pid")):
                path.unlink(missing_ok=True)
                continue
            out.append(data)
        return out

    def requests(self):
        by_session = {}
        for r in sorted(self._live("requests"), key=lambda r: r.get("created_ms") or 0):
            if (
                safe_id(r.get("id"))
                and (self.dir / "decisions" / f"{r['id']}.json").exists()
            ):
                continue  # answered, the hook is about to exit
            by_session.setdefault(r.get("session_id"), []).append(r)
        return by_session

    def waiting(self):
        return {w.get("session_id"): w for w in self._live("waiting")}

    def pending_replies(self):
        out = {}
        for path in self._files("replies"):
            data = read_json(path)
            if data:
                out[path.stem] = {"id": data.get("id"), "sent_ms": data.get("sent_ms")}
        return out

    def stops(self):
        out = self._fresh("stop", "at_ms", STOP_MAX_S)
        self._sync_any("stop", bool(out))
        return out

    def _fresh(self, sub, field, max_s):
        """Files of this kind by name stem. Deletes the ones older than max_s."""
        out = {}
        now = time.time()
        for path in self._files(sub):
            data = read_json(path) or {}
            if now - (data.get(field) or 0) / 1000 > max_s:
                path.unlink(missing_ok=True)
                continue
            out[path.stem] = data
        return out

    def notes(self):
        """Returns the notes that wait for a tool call, and the receipts of delivered notes."""
        pending = self._fresh("notes", "at_ms", NOTE_MAX_S)
        self._sync_any("notes", bool(pending))
        return pending, self._fresh("notes/delivered", "delivered_ms", RECEIPT_MAX_S)

    def _sync_any(self, sub, on):
        # The PreToolUse command starts Python only while this marker exists.
        self._sync_marker(self.dir / sub / ".any", on)

    @staticmethod
    def _sync_marker(marker, on):
        if on and not marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            marker.touch()
        elif not on and marker.exists():
            marker.unlink(missing_ok=True)

    def _cached(self, name):
        """Reads a small file of the control folder again only when it changed."""
        try:
            st = os.stat(self.dir / name)
        except OSError:
            self._cache.pop(name, None)
            return None
        stamp = (st.st_mtime_ns, st.st_size)
        hit = self._cache.get(name)
        if not hit or hit[0] != stamp:
            hit = self._cache[name] = (stamp, read_json(self.dir / name))
        return hit[1]

    def rules(self):
        """rules.json as {"enabled", "items"}. A missing or broken file means off and empty."""
        data = self._cached("rules.json")
        data = data if isinstance(data, dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        return {
            "enabled": data.get("enabled") is True,
            "items": [
                {"rule": i["rule"], "added_ms": i.get("added_ms")}
                for i in items
                if isinstance(i, dict) and isinstance(i.get("rule"), str)
            ],
        }

    def guard_mode(self):
        data = self._cached("guard.json")
        mode = data.get("mode") if isinstance(data, dict) else None
        return mode if mode in GUARD_MODES else "warn"

    def approval_counts(self):
        if self._approvals is None:
            data = read_json(self.dir / "approvals.json")
            self._approvals = {
                k: v
                for k, v in (data if isinstance(data, dict) else {}).items()
                if isinstance(k, str) and isinstance(v, int)
            }
        return self._approvals

    def auto_log(self):
        """Hits per rule and the newest entries of auto.jsonl, newest first. Reads only the
        lines that the hook added since the last call."""
        path = self.dir / "auto.jsonl"
        offset, hits, recent = self._auto
        try:
            size = os.stat(path).st_size
        except OSError:
            size = 0
        if size < offset:  # the file was replaced or cut, so count again
            offset, hits, recent = 0, {}, deque(maxlen=RECENT_AUTO)
        if size > offset:
            try:
                with open(path, "rb") as f:
                    f.seek(offset)
                    data = f.read(size - offset)
            except OSError:
                data = b""
            end = data.rfind(b"\n") + 1
            for line in data[:end].splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("rule"), str):
                    hits[row["rule"]] = hits.get(row["rule"], 0) + 1
                    recent.append({k: row.get(k) for k in AUTO_FIELDS})
            offset += end
        self._auto = (offset, hits, recent)
        return hits, list(reversed(recent))

    def rules_state(self):
        r = self.rules()
        hits, recent = self.auto_log()
        taken = {i["rule"] for i in r["items"]}
        suggestions = [
            {"rule": k, "approvals": n}
            for k, n in self.approval_counts().items()
            if n >= SUGGEST_MIN and k not in taken and hook.rule_valid(k)
        ]
        return {
            "enabled": r["enabled"],
            "items": [{**i, "hits": hits.get(i["rule"], 0)} for i in r["items"]],
            "suggestions": sorted(
                suggestions, key=lambda s: (-s["approvals"], s["rule"])
            ),
            "recent": recent,
        }

    def write_edits(self, files):
        """Writes edits.json when the files change, and at least every 60 s, so the edit hook
        can tell a live server from a stopped one. Keeps edits.any for the installed command."""
        last, written = self._edits
        if files != last or time.time() - written >= EDITS_REWRITE_S:
            try:
                write_json(self.dir / "edits.json", {"at_ms": now_ms(), "files": files})
            except (
                OSError
            ):  # a full disk must not stop the page; the hook sees a stale file
                return
            self._edits = (files, time.time())
        self._sync_marker(
            self.dir / "edits.any", bool(files) and self.guard_mode() != "off"
        )

    # Writers. Each returns None on success or an error string for the caller.

    def decide(self, request_id, behavior, message=""):
        err, _ = self.decide_all(request_id, behavior, message)
        return err

    def decide_all(self, request_id, behavior, message="", similar=False):
        """Answers one request, and with `similar` every other live request with the same
        rule key. Returns (error or None, number answered)."""
        rid = safe_id(request_id)
        if not rid or behavior not in ("allow", "deny"):
            return "bad request", 0
        req = read_json(self.dir / "requests" / f"{rid}.json")
        if not req or not pid_alive(req.get("pid")):
            return "gone", 0
        key = hook.rule_key(req.get("tool_name"), req.get("tool_input"))
        targets = [req]
        if similar:
            targets += [
                r
                for r in self.open_requests()
                if r.get("id") != rid
                and hook.rule_key(r.get("tool_name"), r.get("tool_input")) == key
            ]
        at = now_ms()
        answered = 0
        for r in targets:
            # The first answer wins. A second click never turns a deny into an allow.
            if write_json_once(
                self.dir / "decisions" / f"{r['id']}.json",
                {
                    "behavior": behavior,
                    "message": str(message or "")[:500],
                    "at_ms": at,
                },
            ):
                answered += 1
            elif r is req:
                return "already answered", 0
        if behavior == "allow" and answered:
            self._count_allows(answered, key)
        return None, answered

    def open_requests(self):
        """Live requests that wait for an answer, in any session."""
        return [
            r for rs in self.requests().values() for r in rs if safe_id(r.get("id"))
        ]

    def _count_allows(self, n, key):
        with self.lock:
            counts = self.approval_counts()
            counts[key] = counts.get(key, 0) + n
            write_json(self.dir / "approvals.json", counts)

    def change_rules(self, action, rule=None, enabled=None):
        """Adds, removes, or switches rules. Adding never turns rules on."""
        with self.lock:
            cur = self.rules()
            items = cur["items"]
            if action == "add" and hook.rule_valid(rule):
                if rule not in {i["rule"] for i in items}:
                    items.append({"rule": rule, "added_ms": now_ms()})
            elif action == "remove" and isinstance(rule, str):
                items = [i for i in items if i["rule"] != rule]
            elif action == "enable" and isinstance(enabled, bool):
                cur["enabled"] = enabled
            else:
                return "bad request"
            write_json(
                self.dir / "rules.json", {"enabled": cur["enabled"], "items": items}
            )
            return None

    def set_guard(self, mode):
        if mode not in GUARD_MODES:
            return "bad request"
        write_json(self.dir / "guard.json", {"mode": mode})
        if mode == "off":
            self._sync_marker(self.dir / "edits.any", False)
        return None

    def reply(self, session_id, text):
        sid = safe_id(session_id)
        text = str(text or "").strip()
        if not sid or not text:
            return "bad request"
        w = read_json(self.dir / "waiting" / f"{sid}.json")
        if not w or not pid_alive(w.get("pid")):
            return "not waiting"
        rid = os.urandom(6).hex()
        write_json(
            self.dir / "replies" / f"{sid}.json",
            {"id": rid, "text": text[:TEXT_CAP], "sent_ms": int(time.time() * 1000)},
        )
        return None

    def stop(self, session_id, agent_id):
        aid = safe_id(agent_id)
        if not aid:
            return "bad request"
        write_json(
            self.dir / "stop" / f"{aid}.json",
            {
                "session_id": str(session_id),
                "agent_id": aid,
                "at_ms": int(time.time() * 1000),
            },
        )
        self._sync_any("stop", True)
        return None

    def stop_session(self, session_id):
        sid = safe_id(session_id)
        if not sid:
            return "bad request"
        write_json(
            self.dir / "stop" / f"session-{sid}.json",
            {"session_id": sid, "at_ms": int(time.time() * 1000)},
        )
        self._sync_any("stop", True)
        return None

    def clear_stop(self, name):
        """Deletes stop/<name>.json: an agent ID, or session-<session id>."""
        name = safe_id(name)
        if name:
            (self.dir / "stop" / f"{name}.json").unlink(missing_ok=True)

    def note(self, session_id, agent_id, text):
        """Writes a note for the main thread of a session, or for one subagent of it."""
        sid, aid = safe_id(session_id), safe_id(agent_id)
        text = str(text or "").strip()[:TEXT_CAP]
        if not sid or (agent_id and not aid) or not text:
            return "bad request"
        nid = os.urandom(6).hex()
        at = int(time.time() * 1000)
        write_json(
            self.dir / "notes" / f"{aid or sid}.json",
            {"id": nid, "session_id": sid, "agent_id": aid, "text": text, "at_ms": at},
        )
        self.sent[nid] = (text, at)
        if len(self.sent) > SENT_KEEP:
            del self.sent[next(iter(self.sent))]
        self._sync_any("notes", True)
        return None

    def clear_note(self, key):
        key = safe_id(key)
        if key:
            (self.dir / "notes" / f"{key}.json").unlink(missing_ok=True)
