# Changelog

All notable changes to AgentForeman are in this file. The format follows Keep a Changelog, and the versions follow Semantic Versioning.

## Unreleased

## 0.1.0 - 2026-10-04

The first public release.

### Added

- One local page that shows every live Claude Code session, its subagents, and recent Codex threads.
- A Needs attention table for sessions that wait for a reply or an approval, oldest first.
- Approve or deny a waiting tool call, with the whole command and the diff of an edit.
- Approve all similar calls in every session with one click.
- "Always allow" rules that the page suggests after 3 approvals. Rules start off and fail closed.
- Reply to a session that ended its turn.
- Steering notes that a running session, subagent, or teammate reads at its next tool call.
- Stop one subagent or a whole session at its next tool call.
- A same-file guard that warns or asks before a second session edits a file.
- Split-pane (tmux) Agent Teams teammates under their lead, with their own controls.
- A flag for an agent that repeats the same call.
- A session panel with a timeline, a trace, subagents, and details.
- Search, a project filter, keyboard keys, an activity chart, and desktop alerts.
- A theme that follows the system until you pick one.
- The `agentforeman` command with `serve`, `open`, `demo`, `install`, `uninstall`, and `--version`.
- `agentforeman demo`, which serves sample sessions from a temporary folder and removes it on exit.

### Security

- The server listens on `127.0.0.1` only.
- The page and its data open only with a sign-in link, so other accounts on the Mac cannot use them.
- The token lives in a file that only you can read, and the browser keeps it in an `HttpOnly` cookie.
- Host and origin checks and frame blocking guard every action.
- The control folder has mode `0700`. The installer writes the settings file through a private temporary file.
