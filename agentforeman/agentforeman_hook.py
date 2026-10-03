"""Claude Code hooks that let AgentForeman approve, reply, stop, steer, and guard.

Usage, from ~/.claude/settings.json: python3 agentforeman_hook.py permission | reply | pre | edit
The mode `stop` is the old name of `pre`. Settings from an older install still call it.

The hooks and the server talk through files in the control folder (AGENTFOREMAN_HOME,
default ~/.agentforeman). Every mode fails open: on any error it exits 0 with no output,
so a broken hook never blocks a tool call. Rules fail closed: a call that they cannot read
safely goes to the page as before. SPEC.md, sections Controls and Foreman, hold the
contract. The server imports the rule functions from this file. The installer copies this
file alone, so it imports only the standard library.
"""

import json
import os
import re
import shlex
import signal
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

HOME = Path(os.environ.get("AGENTFOREMAN_HOME") or Path.home() / ".agentforeman")
# Another hook that allows at once, such as an auto-approver, answers within this time.
PERMISSION_GRACE_S = float(os.environ.get("AGENTFOREMAN_PERMISSION_GRACE_S", "1.5"))
PERMISSION_WAIT_S = 3500
REPLY_WAIT_S = 6 * 3600
REPLY_PREFIX = "Reply from the user, sent from AgentForeman:\n\n"
STOP_REASON = (
    "STOPPED from AgentForeman: the user stopped this subagent. "
    "Do not call any more tools. Reply now with one short paragraph that says what you "
    "finished and what is left."
)
SESSION_STOP_REASON = "The user stopped this session from AgentForeman."
NOTE_PREFIX = "Note from the user, sent from AgentForeman while you work:\n\n"
TEXT_CAP = 4000
# The tool call's own row sits near the end of the transcript when the dialog opens.
TAIL_BYTES = 512 * 1024
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
FILE_RULE_TOOLS = ("Read", *EDIT_TOOLS)
BARE_RULE_TOOLS = ("Grep", "Glob", "LS", "WebSearch")
# A rule never starts with a word that runs the rest of the line as a command.
WRAPPERS = {
    "eval", "exec", "sudo", "su", "env", "xargs", "nohup", "command", "builtin",
    "source", ".", "time", "nice", "timeout", "watch", "doas",
}  # fmt: skip
INTERPRETERS = {
    "bash", "sh", "zsh", "fish", "dash", "python", "python3", "node", "deno", "bun",
    "perl", "ruby", "php", "osascript", "pwsh", "npx",
}  # fmt: skip
# The matcher refuses these anywhere, so no expansion, redirect, group, or escape can hide a
# second command.
UNSAFE_BASH = ("`", "$", "<", ">", "\n", "\r", "(", ")", "{", "}", "\\")
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
# These variables change which program runs or what it loads, so a call that sets one
# never matches a rule.
RISKY_VAR = re.compile(
    r"^(PATH|IFS|ENV|BASH_ENV|SHELLOPTS|BASHOPTS|PS4|PROMPT_COMMAND|NODE_OPTIONS|NODE_PATH"
    r"|PERL5OPT|PERL5LIB|RUBYOPT|RUBYLIB|LD_\w*|DYLD_\w*|PYTHON\w*|GIT_\w*|npm_config_\w*"
    r"|NPM_CONFIG_\w*)$"
)
RULE = re.compile(r"^([A-Za-z]+)(?:\((.+)\))?$", re.S)
PLAIN_WORD = re.compile(r"^[A-Za-z0-9_./:@%+=,~-]+$")
HOST = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*$")
EDITS_STALE_MS = 120_000
EDIT_WINDOW_MS = 30 * 60_000


def now_ms():
    return int(time.time() * 1000)


def log(*parts):
    """Writes one line to hook.log while the file `debug` exists in the control folder."""
    try:
        if (HOME / "debug").exists():
            with open(HOME / "hook.log", "a") as f:
                f.write(
                    f"{time.strftime('%H:%M:%S')} pid={os.getpid()} ppid={os.getppid()} "
                    + " ".join(map(str, parts))
                    + "\n"
                )
    except OSError:
        pass


def write_json(path, obj):
    """Writes a whole file or nothing, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def remove(*paths):
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass


def trimmed(value):
    if isinstance(value, str):
        return value[:TEXT_CAP]
    if isinstance(value, dict):
        return {str(k)[:200]: trimmed(v) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [trimmed(v) for v in value[:40]]
    return value


def on_exit_remove(path):
    """Claude Code ends the hook when the dialog is answered locally, so clean up on a signal."""

    def handler(signum, frame):
        remove(path)
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, handler)


def attended():
    """True only when a person can answer. A `claude -p` run sets 0, and a waiting hook would hang it."""
    return os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") == "1"


def agent_transcript(data):
    """The transcript that holds the tool call: the subagent's own file for a subagent."""
    path = data.get("transcript_path") or ""
    aid = str(data.get("agent_id") or "")
    if path and aid and "/" not in aid:
        sub = Path(path).with_suffix("") / "subagents" / f"agent-{aid}.jsonl"
        if sub.exists():
            return str(sub)
    return path


def result_ids(row):
    content = (row.get("message") or {}).get("content")
    if row.get("type") != "user" or not isinstance(content, list):
        return set()
    return {
        b.get("tool_use_id")
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_result"
    }


def find_call(path, data):
    """Returns (tool_use_id, answered, end offset) for this call in the transcript tail.

    Claude Code writes the tool call's row before it opens the dialog. The newest row with
    the same tool name and input is this call, unless the hook input names the ID.
    """
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read()
    except OSError:
        return None, False, 0
    end = size - len(chunk) + chunk.rfind(b"\n") + 1
    want = data.get("tool_use_id")
    found, done = want, set()
    for line in chunk[: end - (size - len(chunk))].splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        done |= result_ids(row)
        if want or row.get("type") != "assistant":
            continue
        for b in (row.get("message") or {}).get("content") or []:
            if (
                isinstance(b, dict)
                and b.get("type") == "tool_use"
                and b.get("name") == data.get("tool_name")
                and b.get("input") == data.get("tool_input")
            ):
                found = b.get("id")
    return found, bool(found) and found in done, end


def answered(path, offset, tool_use_id):
    """Reads whole new lines after offset. Returns (answered, new offset)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return False, offset
    cut = chunk.rfind(b"\n") + 1
    for line in chunk[:cut].splitlines():
        if tool_use_id.encode() not in line:
            continue
        try:
            if tool_use_id in result_ids(json.loads(line)):
                return True, offset + cut
        except ValueError:
            continue
    return False, offset + cut


def file_path(inp):
    return str(inp.get("file_path") or inp.get("notebook_path") or "")


def url_host(url):
    try:
        u = urlparse(str(url or ""))
    except ValueError:
        return ""
    return (u.hostname or "") if u.scheme in ("http", "https") else ""


def split_parts(command):
    """Splits on &&, ||, ; and | outside quotes. None for a quoted ;&| or a lone &."""
    parts, cur, quote, i = [], "", None, 0
    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote = None
            elif c in ";&|":
                return None
        elif c in "'\"":
            quote = c
        elif c in ";&|":
            op = command[i : i + 2] if command[i : i + 2] in ("&&", "||") else c
            if op == "&":
                return None
            parts.append(cur)
            cur, i = "", i + len(op)
            continue
        cur += c
        i += 1
    return None if quote else [*parts, cur]


def bash_parts(command):
    """Each part of a command as its words, after leading VAR=value words. None when the
    command holds anything that the split cannot read safely. SPEC.md lists the cases."""
    if any(c in command for c in UNSAFE_BASH):
        return None
    pieces = split_parts(command)
    if pieces is None:
        return None
    out = []
    for piece in pieces:
        try:
            words = shlex.split(piece)
        except ValueError:
            return None
        while words and (m := ASSIGNMENT.match(words[0])):
            if RISKY_VAR.match(m[1]):
                return None
            words.pop(0)
        if not words:
            return None
        out.append(words)
    return out


def loose_parts(command):
    """The parts of a command that the safe split refuses, good enough to name its key."""
    out = []
    for piece in re.split(r"&&|\|\||[;|&\n]", command):
        try:
            words = shlex.split(piece)
        except ValueError:
            words = piece.split()
        while words and ASSIGNMENT.match(words[0]):
            words.pop(0)
        if words:
            out.append(words)
    return out


def is_cd(words):
    return len(words) == 2 and words[0] == "cd"


def rule_key(tool, inp):
    """The rule that would allow calls like this one, for example Bash(npm test)."""
    inp = inp if isinstance(inp, dict) else {}
    if tool == "Bash":
        command = str(inp.get("command") or "")
        parts = bash_parts(command) or loose_parts(command)
        words = next((w for w in parts if not is_cd(w)), parts[0] if parts else [])
        return f"Bash({' '.join(words[:2])})" if words else "Bash"
    if tool in EDIT_TOOLS:
        return f"{tool}({file_path(inp)})" if file_path(inp) else tool
    if tool == "WebFetch":
        host = url_host(inp.get("url"))
        return f"WebFetch({host})" if host else "WebFetch"
    return str(tool or "")


def parse_rule(rule):
    m = RULE.match(rule) if isinstance(rule, str) else None
    return (m[1], m[2]) if m else (None, None)


def program(word):
    """The program a word runs, without its folder or version: /usr/bin/python3.12 is python."""
    return re.sub(r"[\d.]+$", "", os.path.basename(word)) or word


def bash_rule_words(arg):
    """The words of a valid Bash rule, or None."""
    words = arg.split(" ")
    if not 1 <= len(words) <= 3 or not all(PLAIN_WORD.match(w) for w in words):
        return None
    first = words[0]
    # /bin/bash or .venv/bin/python3.12 runs the same program as bash or python.
    names = {first, os.path.basename(first), program(first)}
    if names & WRAPPERS or first.startswith("-") or "=" in first:
        return None
    if names & (INTERPRETERS | {program(i) for i in INTERPRETERS}):
        # Any flag can run inline code or change what runs, except -m with a module name.
        rest = words[2:] if words[1:2] == ["-m"] else words[1:]
        if not rest or rest[0].startswith("-"):
            return None
    return words


def path_prefix_ok(arg):
    base = arg.rstrip("/")
    return (
        arg.startswith("/")
        and "//" not in arg
        and os.path.normpath(base or "/") == base
        and len([p for p in base.split("/") if p]) >= 2
    )


def rule_valid(rule):
    tool, arg = parse_rule(rule)
    if tool == "Bash":
        return arg is not None and bash_rule_words(arg) is not None
    if tool in FILE_RULE_TOOLS:
        return arg is not None and path_prefix_ok(arg)
    if tool == "WebFetch":
        return arg is not None and bool(HOST.match(arg))
    return tool in BARE_RULE_TOOLS and arg is None


def under(path, prefix):
    base = prefix.rstrip("/")
    return path == base or path.startswith(base + "/")


def file_match(prefix, inp):
    path = file_path(inp)
    if not path.startswith("/") or ".." in path.split("/"):
        return False
    path = os.path.normpath(path)
    # A link inside the folder can point out of it, so the resolved path must stay inside too.
    return under(path, prefix) and under(
        os.path.realpath(path), os.path.realpath(prefix.rstrip("/"))
    )


def matching_rule(rules, tool, inp):
    """The first valid rule that allows this call, or None. Every part of a Bash command
    must match a rule, except a part that is exactly `cd <path>`."""
    inp = inp if isinstance(inp, dict) else {}
    parsed = [(r, *parse_rule(r)) for r in rules if rule_valid(r)]
    if tool == "Bash":
        bash = [(r, arg.split(" ")) for r, t, arg in parsed if t == "Bash"]
        found = []
        for words in bash_parts(str(inp.get("command") or "")) or []:
            if is_cd(words):
                continue
            hit = next((r for r, rw in bash if words[: len(rw)] == rw), None)
            if hit is None:
                return None
            found.append(hit)
        return found[0] if found else None
    for r, t, arg in parsed:
        if t != tool:
            continue
        if (
            arg is None
            or (t in FILE_RULE_TOOLS and file_match(arg, inp))
            or (t == "WebFetch" and url_host(inp.get("url")) == arg.lower())
        ):
            return r
    return None


def rule_matches(rule, tool, inp):
    return matching_rule([rule], tool, inp) is not None


def rule_allow(data):
    """The rule in rules.json that allows this call, or None. Any doubt means None."""
    try:
        cfg = read_json(HOME / "rules.json")
        if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
            return None
        items = cfg.get("items") or []
        rules = [i.get("rule") for i in items if isinstance(i, dict)]
        return matching_rule(rules, data.get("tool_name"), data.get("tool_input"))
    except Exception:  # a broken rules file allows nothing
        return None


def log_auto(data, rule):
    """Appends the allowed call to auto.jsonl. Returns False when it cannot, so the call
    goes to the page instead of an allow that nobody can see."""
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    entry = {
        "at_ms": now_ms(),
        "session_id": data.get("session_id") or "",
        "agent_id": data.get("agent_id") or None,
        "tool_name": data.get("tool_name") or "",
        "summary": str(inp.get("command") or file_path(inp) or inp.get("url") or "")[
            :200
        ],
        "rule": rule,
    }
    try:
        HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(HOME / "auto.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        return False
    return True


def print_decision(decision):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": decision,
                }
            }
        )
    )


def permission(data):
    if not attended():
        return
    rule = rule_allow(data)
    if rule and log_auto(data, rule):
        return print_decision({"behavior": "allow"})
    time.sleep(PERMISSION_GRACE_S)
    # An answer in the terminal ends this hook, but an answer in the Claude app does not.
    # The tool call's result in the transcript shows that someone answered.
    transcript = agent_transcript(data)
    call, done, offset = find_call(transcript, data) if transcript else (None, False, 0)
    if done:
        return
    parent = os.getppid()
    rid = uuid.uuid4().hex[:12]
    req = HOME / "requests" / f"{rid}.json"
    decision = HOME / "decisions" / f"{rid}.json"
    on_exit_remove(req)
    write_json(
        req,
        {
            "id": rid,
            "pid": os.getpid(),
            "session_id": data.get("session_id") or "",
            "agent_id": data.get("agent_id") or None,
            "cwd": data.get("cwd") or "",
            "tool_name": data.get("tool_name") or "",
            "tool_input": trimmed(data.get("tool_input") or {}),
            "created_ms": now_ms(),
        },
    )
    end = time.time() + PERMISSION_WAIT_S
    tick = 0
    try:
        while time.time() < end:
            tick += 1
            if tick % 3 == 0:
                if os.getppid() != parent:
                    return
                if call:
                    done, offset = answered(transcript, offset, call)
                    if done:
                        log("permission end: answered outside AgentForeman")
                        return
            d = read_json(decision) if decision.exists() else None
            if d and d.get("behavior") in ("allow", "deny"):
                out = {"behavior": d["behavior"]}
                if d["behavior"] == "deny":
                    out["message"] = str(
                        d.get("message") or "Denied from AgentForeman."
                    )[:500]
                print_decision(out)
                return
            time.sleep(0.3)
    finally:
        remove(req, decision)


def new_turn_started(path, offset, since_ms):
    """True when the transcript gained a user or assistant entry stamped after the turn ended.

    Claude Code can write the turn's last reply just after the Stop hook starts, so a late
    line with an older timestamp is not a new turn.
    """
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return False
    for line in data.splitlines():
        try:
            row = json.loads(line)
            if row.get("type") not in ("user", "assistant"):
                continue
            ts = (
                datetime.fromisoformat(
                    str(row.get("timestamp")).replace("Z", "+00:00")
                ).timestamp()
                * 1000
            )
        except (ValueError, TypeError, AttributeError):
            continue
        if ts > since_ms + 1000:
            return True
    return False


def reply(data):
    if data.get("agent_id") or not attended():
        return
    sid = str(data.get("session_id") or "")
    if not sid or "/" in sid:
        return
    waiting = HOME / "waiting" / f"{sid}.json"
    incoming = HOME / "replies" / f"{sid}.json"
    transcript = data.get("transcript_path") or ""
    try:
        offset = os.stat(transcript).st_size
    except OSError:
        offset = None
    me = os.getpid()
    parent = os.getppid()
    since = now_ms()
    write_json(waiting, {"pid": me, "session_id": sid, "since_ms": since})
    log("reply wait", sid, "parent", parent)

    def mine():
        w = read_json(waiting)
        return bool(w) and w.get("pid") == me

    def handler(signum, frame):
        log("reply end: signal", signum)
        if mine():
            remove(waiting)
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, handler)
    end = time.time() + REPLY_WAIT_S
    try:
        while time.time() < end:
            if not mine():
                log("reply end: a newer hook took over")
                return
            if incoming.exists():
                msg = read_json(incoming) or {}
                text = str(msg.get("text") or "").strip()[:TEXT_CAP]
                remove(incoming)
                if text:
                    remove(waiting)
                    sys.stderr.write(REPLY_PREFIX + text)
                    sys.stderr.flush()
                    os._exit(2)
            if os.getppid() != parent:
                log("reply end: parent changed to", os.getppid())
                return
            if offset is not None and new_turn_started(transcript, offset, since):
                log("reply end: the transcript gained a user or assistant entry")
                return
            time.sleep(1)
    finally:
        if mine():
            remove(waiting)


def claim(path):
    """Moves the file aside, so only one of two parallel tool calls uses it. Returns its data."""
    taken = path.with_name(f".{path.name}.{os.getpid()}.claim")
    try:
        os.rename(path, taken)
    except OSError:
        return None
    data = read_json(taken)
    remove(taken)
    return data


def pre_tool(output):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", **output}}))


def deny_stopped():
    pre_tool({"permissionDecision": "deny", "permissionDecisionReason": STOP_REASON})


def pre(data):
    """PreToolUse: ends a stopped session, denies a stopped subagent, or hands over a note."""
    sid = str(data.get("session_id") or "")
    aid = str(data.get("agent_id") or "")
    if "/" in sid or "/" in aid:
        return
    session_stop = HOME / "stop" / f"session-{sid}.json"
    if sid and session_stop.exists():
        if aid:
            return deny_stopped()
        if claim(session_stop) is not None:
            print(json.dumps({"continue": False, "stopReason": SESSION_STOP_REASON}))
        return
    if aid and (HOME / "stop" / f"{aid}.json").exists():
        return deny_stopped()
    note(sid, aid)


def note(sid, aid):
    """A subagent note waits for that agent_id. A main-thread note waits for a call of its
    session with no agent_id. The note adds context and never answers the call."""
    key = aid or sid
    path = HOME / "notes" / f"{key}.json"
    if not key or not path.exists():
        return
    found = read_json(path) or {}
    if (found.get("agent_id") or "") != aid or (
        not aid and found.get("session_id") != sid
    ):
        return
    found = claim(path)
    if found is None:
        return
    text = str(found.get("text") or "").strip()[:TEXT_CAP]
    if text:
        pre_tool({"additionalContext": NOTE_PREFIX + text})
    write_json(
        HOME / "notes" / "delivered" / f"{key}.json",
        {"id": found.get("id"), "delivered_ms": now_ms()},
    )


def edit(data):
    """PreToolUse on a file edit: names the other live sessions that edited the same file."""
    mode = (read_json(HOME / "guard.json") or {}).get("mode", "warn")
    if mode not in ("warn", "block"):
        return
    edits = read_json(HOME / "edits.json")
    now = now_ms()
    # The server rewrites edits.json at least every 60 s, so an old file means it is down.
    if not isinstance(edits, dict) or now - edits["at_ms"] > EDITS_STALE_MS:
        return
    path = os.path.normpath(file_path(data.get("tool_input") or {}))
    sid = data.get("session_id")
    names = [
        str(e.get("name") or e.get("session_id"))
        for e in edits["files"].get(path) or []
        if e.get("session_id") != sid and now - e["last_ms"] <= EDIT_WINDOW_MS
    ]
    if not names:
        return
    text = (
        f"Another live session ({', '.join(dict.fromkeys(names))}) also edited {path} in "
        "the last 30 minutes. Read the file again before you change it, so that you do "
        "not undo that work."
    )
    if mode == "block":
        pre_tool({"permissionDecision": "ask", "permissionDecisionReason": text})
    else:
        pre_tool({"additionalContext": text})


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return
        modes = {
            "permission": permission,
            "reply": reply,
            "pre": pre,
            "stop": pre,
            "edit": edit,
        }
        modes[mode](data)
    except Exception:  # fail open: never block Claude Code because of this hook
        log("error in mode", mode, traceback.format_exc().strip().replace("\n", " | "))


if __name__ == "__main__":
    main()
    sys.exit(0)
