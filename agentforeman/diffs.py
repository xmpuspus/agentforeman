"""The diff on an approval card: what an Edit, MultiEdit, or Write would change.

A Write compares the file on disk only inside the session folder, so no file content from
outside it ever reaches /api/state. SPEC.md, section Foreman, holds the rules.
"""

import difflib
import os
import stat
from pathlib import Path

DIFF_CAP = 20_000
WRITE_MAX_BYTES = 256 * 1024
# The permission hook keeps this many characters of each input string, and 40 list items.
HOOK_TEXT_CAP = 4000
HOOK_LIST_CAP = 40
CUT_NOTE = "The hook stored only the first 4000 characters of each text, so the diff can end early."


def lines_of(n):
    return f"{n} line" if n == 1 else f"{n} lines"


def unified(path, old, new):
    return list(
        difflib.unified_diff(
            str(old or "").splitlines(),
            str(new or "").splitlines(),
            path,
            path,
            lineterm="",
        )
    )


def capped(out, note=None):
    text = "\n".join(out)
    if not text:
        return None, note or "The old and the new text are the same."
    if len(text) > DIFF_CAP:
        cut = "The diff is cut at 20,000 characters."
        return text[: DIFF_CAP - 4] + "\n...", f"{note} {cut}" if note else cut
    return text, note


def was_cut(*texts):
    return any(len(str(t or "")) >= HOOK_TEXT_CAP for t in texts)


def inside(path, folder):
    return Path(path).is_relative_to(folder)


def old_text(path, cwd):
    """Returns (text of the file, None), ("", None) for a missing file, or (None, reason)."""
    outside = "the file is outside the session folder"
    if not cwd or not os.path.isabs(path):
        return None, outside
    root = os.path.realpath(cwd)
    # realpath follows every link, so a link out of the folder resolves outside it.
    real = os.path.realpath(path)
    if not os.path.isdir(root) or not inside(real, root):
        return None, outside
    try:
        st = os.stat(real)
    except FileNotFoundError:
        return "", None
    except OSError:
        return None, "AgentForeman cannot read the file"
    # A second hard link can be a file outside the folder under a name inside it.
    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
        return None, "it is not a plain file"
    if st.st_size > WRITE_MAX_BYTES:
        return None, "the file is larger than 256 KB"
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            fst = os.fstat(f.fileno())
            if not stat.S_ISREG(fst.st_mode) or fst.st_size > WRITE_MAX_BYTES:
                return None, "the file changed while AgentForeman read it"
            data = f.read(WRITE_MAX_BYTES + 1)
    except OSError:
        return None, "AgentForeman cannot read the file"
    if b"\0" in data:
        return None, "it is a binary file"
    return data.decode(errors="replace"), None


def write_diff(path, content, cwd):
    n = len(content.splitlines())
    if was_cut(content):
        return None, (
            f"Writes {lines_of(n)} or more. The hook stored only the first 4000 "
            "characters, so AgentForeman shows no diff."
        )
    old, why = old_text(path, cwd)
    if why:
        return None, f"Writes {lines_of(n)}. AgentForeman shows no diff, because {why}."
    return capped(unified(path, old, content))


def approval_diff(tool, inp, cwd):
    """Returns (diff text or None, note or None) for one approval."""
    path = str(inp.get("file_path") or "")
    if tool == "Edit":
        old, new = inp.get("old_string"), inp.get("new_string")
        return capped(unified(path, old, new), CUT_NOTE if was_cut(old, new) else None)
    if tool == "MultiEdit":
        edits = [e for e in inp.get("edits") or [] if isinstance(e, dict)]
        out, cut = [], len(edits) >= HOOK_LIST_CAP
        for e in edits:
            old, new = e.get("old_string"), e.get("new_string")
            # Each edit gets its own hunk. Only the first keeps the file header.
            out += unified(path, old, new)[2 if out else 0 :]
            cut = cut or was_cut(old, new)
        return capped(out, CUT_NOTE if cut else None)
    if tool == "Write":
        return write_diff(path, str(inp.get("content") or ""), cwd)
    return None, None
