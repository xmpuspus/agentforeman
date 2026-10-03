# Contributing to AgentForeman

Thank you for your help. This file tells you how to set up the project, run the checks, and send a change.

## Every change starts in SPEC.md

`SPEC.md` is the contract of AgentForeman. Write the new or changed rule there first. Then change the code and the tests to match it. A pull request that changes behavior without a SPEC change goes back for one.

## Set up

You need macOS and Python 3.11 or later.

```
git clone https://github.com/xmpuspus/agentforeman
cd agentforeman
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
```

The package itself needs no other package. The `dev` extra adds pytest, Playwright, and ruff for the checks.

## Run the checks

```
ruff check .
ruff format --check .
pytest -q
```

- The tests start real servers on free ports of `127.0.0.1`, against fake Claude Code and Codex folders.
- The browser tests use Playwright with Chromium.
- No test reads or writes your real `~/.claude` or `~/.agentforeman`.

Never run `agentforeman install` against your real settings while you test. Pass `--settings` and `--control-dir` with paths in a temporary folder.

## Code style

- Use only the Python standard library at run time.
- Keep functions small. Write few comments, and make each one say why.
- The hook file `agentforeman/agentforeman_hook.py` imports only the standard library, because the installer copies it alone.
- A test changes only when the contract changes. Never loosen an assertion to make a test pass.

## Send a change

1. Make a branch.
2. Change `SPEC.md`, then the code and the tests.
3. Run the checks above.
4. Add a line to `CHANGELOG.md` under "Unreleased".
5. Open a pull request that says what changed and why.

To report a security problem, read [SECURITY.md](SECURITY.md) first.
