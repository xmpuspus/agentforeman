"""The hook file, imported as a module, so the server and the hook share one rule key, one Bash
split, and one rule check. The installer copies agentforeman_hook.py on its own, so that file
imports nothing from this package."""

from pathlib import Path

from agentforeman import agentforeman_hook as hook

HOOK_SOURCE = Path(hook.__file__).resolve()
