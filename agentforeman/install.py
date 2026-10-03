"""Adds the AgentForeman control hooks to Claude Code, or removes them.

Run:
  agentforeman install    # install, or update after a change to agentforeman_hook.py
  agentforeman uninstall  # remove exactly what the install added

It backs up the settings file first, then adds four hook entries whose commands contain
the marker `agentforeman-hook`. Other settings and hooks stay as they are. Running sessions
reload the settings file, so they get the controls without a restart. The file imports only
the standard library, so `python3 install.py [--uninstall]` also works.
"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_NAME = "agentforeman_hook.py"
HOOK_SOURCE = HERE / HOOK_NAME
MARKER = "agentforeman-hook"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def commands(control_dir):
    d = Path(control_dir)
    hook = shlex.quote(str(d / "hooks" / HOOK_NAME))
    # A missing script must never block a tool call, so each command checks for it first.
    guard = f"[ -f {hook} ] || exit 0"
    env = f"AGENTFOREMAN_HOME={shlex.quote(str(d))}"
    any_stop = shlex.quote(str(d / "stop" / ".any"))
    any_note = shlex.quote(str(d / "notes" / ".any"))
    any_edit = shlex.quote(str(d / "edits.any"))
    return {
        "PermissionRequest": f"{guard}; {env} exec python3 {hook} permission # {MARKER}",
        "Stop": f"{guard}; {env} exec python3 {hook} reply # {MARKER}",
        # Python starts only while a stop or a note is pending. The empty path costs about 6 ms.
        "PreToolUse": f"[ -e {any_stop} ] || [ -e {any_note} ] || exit 0; {guard}; {env} exec python3 {hook} pre # {MARKER}",
        # The server keeps edits.any only while the guard is on and some session edited a file.
        "Edit": f"[ -e {any_edit} ] || exit 0; {guard}; {env} exec python3 {hook} edit # {MARKER}",
    }


def entries(control_dir):
    """(event, entry) pairs. The `pre` entry comes before the edit guard."""
    c = commands(control_dir)
    return [
        (
            "PermissionRequest",
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": c["PermissionRequest"],
                        "timeout": 3600,
                    }
                ],
            },
        ),
        (
            "Stop",
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": c["Stop"],
                        "asyncRewake": True,
                        "timeout": 21600,
                    }
                ]
            },
        ),
        (
            "PreToolUse",
            {
                "matcher": "",
                "hooks": [
                    {"type": "command", "command": c["PreToolUse"], "timeout": 30}
                ],
            },
        ),
        (
            "PreToolUse",
            {
                "matcher": "Edit|Write|MultiEdit|NotebookEdit",
                "hooks": [{"type": "command", "command": c["Edit"], "timeout": 10}],
            },
        ),
    ]


def ours(hook):
    return isinstance(hook, dict) and MARKER in str(hook.get("command", ""))


def strip_ours(settings):
    """Removes every hook whose command has the marker. Returns how many it removed.
    An entry that is not an object is not ours, so it stays as it is."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        groups = hooks[event] if isinstance(hooks[event], list) else []
        kept_groups = []
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(inner, list):
                kept_groups.append(group)
                continue
            kept = [h for h in inner if not ours(h)]
            removed += len(inner) - len(kept)
            if kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups:
            hooks[event] = kept_groups
        elif groups:
            del hooks[event]
    return removed


def write_settings(path, settings):
    # A settings file can link into a dotfiles repo, so write the file that the link names
    # and keep the link.
    target = Path(os.path.realpath(path))
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        mode = None
    # mkstemp makes the file with mode 0600 and a random name, so no other user can read the
    # settings before the chmod below.
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        json.loads(
            tmp.read_text()
        )  # never replace the file with something that does not parse
        os.chmod(tmp, 0o644 if mode is None else mode)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def backup(settings_path):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    copy = settings_path.with_name(f"{settings_path.name}.bak-{stamp}-agentforeman")
    n = 2
    while copy.exists():  # two runs in one second must not share a backup
        copy = settings_path.with_name(
            f"{settings_path.name}.bak-{stamp}-{n}-agentforeman"
        )
        n += 1
    shutil.copy2(settings_path, copy)
    print(f"Backup: {copy}")


def run(settings, control_dir, uninstall=False):
    settings_path = Path(settings).expanduser()
    control = Path(control_dir).expanduser()
    try:
        data = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except ValueError as e:
        sys.exit(f"{settings_path} does not parse as JSON ({e}). Nothing changed.")
    if not isinstance(data, dict):
        sys.exit(f"{settings_path} is not a JSON object. Nothing changed.")
    if settings_path.exists():
        backup(settings_path)

    removed = strip_ours(data)
    if uninstall:
        write_settings(settings_path, data)
        (control / "installed.json").unlink(missing_ok=True)
        (control / "hooks" / HOOK_NAME).unlink(missing_ok=True)
        print(f"Removed {removed} AgentForeman hooks from {settings_path}.")
        return 0

    (control / "hooks").mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(control, 0o700)
    shutil.copy2(HOOK_SOURCE, control / "hooks" / HOOK_NAME)
    hooks = data.setdefault("hooks", {})
    for event, entry in entries(control):
        hooks.setdefault(event, []).append(entry)
    write_settings(settings_path, data)
    (control / "installed.json").write_text(
        json.dumps(
            {
                "installed_ms": int(time.time() * 1000),
                "hook_sha256": sha256(HOOK_SOURCE),
            }
        )
    )
    print(
        f"Installed the AgentForeman hooks in {settings_path}: PermissionRequest, Stop, "
        "PreToolUse, and the PreToolUse edit guard."
    )
    print(
        "Running sessions reload the settings, so they get the controls without a restart."
    )
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Install or remove the AgentForeman control hooks."
    )
    ap.add_argument("--settings", default="~/.claude/settings.json")
    ap.add_argument("--control-dir", default="~/.agentforeman")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()
    return run(args.settings, args.control_dir, args.uninstall)


if __name__ == "__main__":
    sys.exit(main())
