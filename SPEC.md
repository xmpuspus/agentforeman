# AgentForeman shows every live Claude and Codex agent on one local page

AgentForeman is a local web app. It finds every live Claude Code session on this Mac, including VS Code panel sessions and their subagents. It shows what each one does now, what it did last, and which ones wait for the user. With its hooks installed, it also approves or denies a waiting tool call, sends a reply to a session, stops a subagent or a whole session, and sends a steering note to a working agent.

## Constraints

- Python 3.11+ standard library only. No `pip install`, no npm, no build step, no CDN.
- Bind to `127.0.0.1` only. Default port `4320`. Never use port `4317`.
- The server never writes to the Claude or Codex folders, and never changes settings.
- The server writes only to its control folder, `~/.agentforeman/` (`--control-dir`). The hooks read and write the same folder.
- The server creates its control folder with mode `0700`, so only the user can read it.
- Only `agentforeman install` changes `~/.claude/settings.json`, and only when the user runs it. It makes a backup first. `agentforeman uninstall` removes exactly what it added.
- Never list or read all of `~/.claude/projects`. Open only the transcripts of live sessions, their subagents, and the split-pane teammates of live sessions.
- Security software on some Macs scans each file that the server opens. Keep opens few.
- Read each transcript tail once, then only the new bytes.

## Run

The code is one Python package, `agentforeman`. `python3 -m agentforeman` and the console script `agentforeman` run `agentforeman.cli:main`.

```
agentforeman [serve] [--port 4320] [--claude-dir ~/.claude] [--codex-dir ~/.codex] [--control-dir ~/.agentforeman] [--dry-open] [--open]
agentforeman demo [--port 4321]
agentforeman install [--settings ~/.claude/settings.json] [--control-dir ~/.agentforeman]
agentforeman uninstall [--settings ~/.claude/settings.json] [--control-dir ~/.agentforeman]
agentforeman --version
```

- `serve` is the default when no command is given.
- `--open` opens the page in the browser after the server starts.
- `--version` prints `agentforeman <version>`. The version lives only in `agentforeman/__init__.py`.
- Port `4317` is refused for `serve` and `demo`.

The server prints one line with its URL. `GET /api/state` must answer within 10 seconds of start.

## Live sessions

1. Read every `<claude-dir>/sessions/*.json`. Keep an entry only when its `pid` is alive (`os.kill(pid, 0)` succeeds).
2. Use the fields `pid`, `sessionId`, `cwd`, `name`, `status`, `entrypoint`, `startedAt`, and `statusUpdatedAt`. Any field can be missing.

## Transcripts

1. The project folder is `<claude-dir>/projects/<cwd>`, with every character outside A-Z, a-z, and 0-9 replaced by `-`.
   Example: `/home/x/.claude` becomes `-home-x--claude`.
2. The transcript is `<project folder>/<sessionId>.jsonl`.
3. If that file is missing, use the newest `.jsonl` file in that project folder that changed after `startedAt`.
   Skip a file that another live session claims. If no file fits, the transcript is `null`.
4. On first read, read only the last 2 MB and drop the first partial line.
5. After that, read only the bytes added since the last read. Keep a half-written last line for the next pass.
6. Each line is one JSON object. Skip lines that fail to parse.
7. When a session ends, the server drops its transcript reader, its subagent tracker, and its fallback path, so a long-running server does not grow.

## Status

| Registry `status` | Transcript | Agent `status` | `status_guess` |
| - | - | - | - |
| `busy` | any | `working` | false |
| `waiting` | any | `waiting` | false |
| other | last assistant entry has `stop_reason` `end_turn` | `your_turn` | false |
| other | last assistant entry has a `tool_use` with no matching `tool_result` | `waiting` | true |
| other | anything else, or no transcript | `idle` | false |

- `since_ms` is the epoch-ms time when the state began.
- For `your_turn` and `waiting`, that is the last assistant entry. For `working`, it is the registry `statusUpdatedAt`, or the newest transcript entry when that field is missing.
- `last_activity_ms` is the newest of the last transcript entry and the last write of each running subagent.
- Use `null` when no real timestamp exists. Never compute an age from 0.
- Count every user prompt, whatever its `promptSource` (`typed`, `sdk`, or absent).

## Actions

- Every `tool_use` block in an assistant entry is one action: `{"tool", "summary", "desc", "ts_ms", "done", "end_ms", "error"}`.
- `summary` for Read, Edit, and Write: the basename of `file_path`.
- `summary` for Bash: the first 80 characters of `command`, after a leading `cd <dir>` followed by `&&`, `;`, or a line break is removed.
- `desc` for Bash: the `description` input, at most 120 characters. Otherwise an empty string. The page shows `desc` first when it exists.
- `summary` for other tools: `pattern` for Grep and Glob, `description` for Agent, `url` for WebFetch, `query` for WebSearch, `to` for SendMessage, `skill` for Skill. Otherwise an empty string.
- `done` is true when a later user entry has a `tool_result` with the same `tool_use_id`. `end_ms` is that entry's time. `error` is the result's `is_error`.
- `recent_actions`: newest first, at most 8. `timeline`: newest first, at most 40.
- `current_action`: the newest action that is not done, when the status is `working` or `waiting`. Otherwise `null`.
- `current_action` adds `started_ms`.
- `last_message`: the text of the newest assistant text block. Spaces inside a line collapse, line breaks stay, and blank-line runs become one blank line. At most 4000 characters. A longer text keeps its end, because the question usually comes last.
- `last_prompt`: the newest typed user prompt, at most 200 characters. Skip `isMeta` rows, which hold skill text, and rows that start with `<`.
- `activity`: 30 integers, the count of actions in each of the last 30 minutes, oldest first.

## Subagents

1. Files: `<project folder>/<sessionId>/subagents/agent-<id>.jsonl` and `agent-<id>.meta.json`.
2. The meta file holds `name`, `description`, `model`, `agentType`, `toolUseId`, and `requestShape`.
3. Read the subagents folder again only when its mtime changes.
4. The caller is the parent session, or the subagent whose transcript holds the Agent call with this `toolUseId`. That nested subagent gets `parent_agent_id` and `depth` 2.
5. Completion notices are `<task-notification>` blocks. Read them from user entries, `queue-operation` rows (`content`), and `attachment` rows of type `queued_command` (`prompt`).
6. `state` is `done` when one of these holds:
   - `requestShape` is `background`. The caller has a notice whose `<task-id>` is the agent ID, or whose `<tool-use-id>` is `toolUseId`. Its `<status>` is `completed`, `failed`, or `killed`. The notice is no more than 5 seconds older than the subagent's newest entry. An older notice belongs to an earlier run that was resumed since.
   - `requestShape` is not `background`. The caller has a `tool_result` for `toolUseId`. An `is_error` result gives the outcome `failed`.
   - Its own last assistant entry has `stop_reason` `end_turn`, and its file did not change for 120 seconds.
   - Its newest entry after its last assistant entry is a user entry that starts with `[Request interrupted`. The outcome is `killed`.
   - Its file did not change for 15 minutes, and it has no open tool call. The outcome is `ended`.
7. Otherwise `state` is `running`. `outcome` is `completed`, `failed`, `killed`, `ended`, or `null`.
8. A meta file with `taskKind` `in_process_teammate` is an Agent Teams teammate: `kind` is `teammate`. Its `state` is `running` when its file changed in the last 60 seconds or it has an open tool call. Otherwise it is `idle`. A teammate is never `done`.
9. Show subagents whose file changed in the last 6 hours, newest first, at most 12 per session. `subagents_total` is the count before that cap.

## Codex

- Read `<codex-dir>/state_5.sqlite` table `threads` in read-only mode (`file:...?mode=ro`).
- Keep rows with `archived = 0` and `updated_at_ms` in the last 48 hours, newest first, at most 20.
- Status is the `status` of the newest `thread_turns` row for that thread, in `<codex-dir>/thread_history_1.sqlite`.
- Status values: `completed`, `failed`, `inProgress`, `interrupted`, or `null`.
- `source` and `nickname` come from `thread_source` and `agent_nickname` when those columns exist.
- `parent_thread_id` comes from `thread_spawn_edges` when that table exists. The page nests a Codex subagent thread under its parent.
- Missing files or tables give an empty list, not an error.
- A database that cannot be read gives an empty list, and the server prints one line to stderr. It prints the same error again only after a different one.

## API

`GET /api/state` returns:

```json
{
  "generated_at": 0,
  "agents": [{
    "kind": "claude", "pid": 0, "session_id": "", "name": "", "cwd": "", "project": "",
    "entrypoint": "", "status": "working", "status_guess": false, "since_ms": null,
    "current_action": null, "recent_actions": [], "last_message": null, "activity": [],
    "model": null, "context_tokens": null, "transcript": null, "subagents": [{
      "agent_id": "", "kind": "subagent", "name": "", "description": "", "model": "", "agent_type": "",
      "state": "running", "outcome": null, "started_ms": null, "ended_ms": null, "tool_calls": 0,
      "last_action": null, "recent_actions": [], "last_message": null, "activity": [],
      "updated_ms": 0, "parent_agent_id": null, "depth": 1
    }],
    "subagents_total": 0, "timeline": [], "last_prompt": null, "team_activity": [],
    "started_ms": null, "last_entry_ms": null, "last_activity_ms": null
  }],
  "codex": [{"thread_id": "", "title": "", "cwd": "", "model": "", "updated_ms": 0, "status": null,
             "source": "", "nickname": "", "parent_thread_id": null}],
  "counts": {"working": 0, "your_turn": 0, "waiting": 0, "idle": 0}
}
```

- `project` is the basename of `cwd`.
- `title` is the session title from the newest transcript row of type `custom-title` (`customTitle`), else `agent-name` (`agentName`), else `ai-title` (`aiTitle`). It is `null` when none exists. The page shows `title` as the session name, with the registry `name` below it.
- `model` and `context_tokens` come from the newest assistant `usage`. Context is input plus cache read plus cache creation tokens.
- `team_activity` adds the `activity` of every shown subagent to the session's own `activity`.
- `GET /api/events` is a Server-Sent Events stream. It sends `event: state` with the same JSON at once and on each change.
- The stream also sends a comment line at least every 15 seconds.
- Every request must carry a `Host` header of `127.0.0.1:<port>` or `localhost:<port>`. Others get 403. This blocks DNS rebinding.
- Every POST must send `Content-Type: application/json`, the header `X-Foreman-Token` with the token from the page's `<meta name="foreman-token">`, and no `Origin` header other than the page's own. Others get 403 or 415. `/api/state` never has the token.
- The server compares the token as bytes, so a token header with characters outside ASCII gets 403.
- A `Content-Length` that is negative or not a number gets 400.
- After a 403, the page reads the token again from `GET /` and sends the request once more. So a tab stays usable after someone deletes the token file.

### Sign-in

Every account on a Mac can reach `127.0.0.1`. So only a request with the token can read the page or the data.

1. At start, the server reads the token from `<control-dir>/token`. A valid token has 32 or more of `A-Z`, `a-z`, `0-9`, `_`, and `-`.
2. If the file is missing or holds no valid token, the server writes a new random token with mode `0600`.
3. `GET /?token=<token>` with the right token answers 303 to `/`. It sets the cookie `agentforeman-<port>=<token>`.
4. The cookie has `Max-Age` of one year, `Path=/`, `HttpOnly`, and `SameSite=Lax`.
5. Every GET path except `/app.js` and `/app.css` needs the cookie or the header `X-Foreman-Token`. Those two files hold no data.
6. Without the token, `GET /` returns 401 with a page that says to run `agentforeman open`. Other paths return 401 JSON.
7. A wrong token in the link gets 401 and no cookie.
8. The server prints its address and the command `agentforeman open`, never the token. It logs no request.
9. `agentforeman open [--port P] [--control-dir D] [--print]` reads the token file and opens the sign-in link in the browser.
10. With `--print`, it prints the link. It exits 1 when the token file is missing or nothing answers on the port.
11. Before it opens or prints the link, `open` sends `GET /api/hello?nonce=<32 hex digits>`. The server answers `{"proof": HMAC-SHA256(token, nonce)}` and needs no token for this path.
12. If the proof is wrong, `open` exits 1 and sends no link. So another program on the port never gets the token.
13. `--open` opens the sign-in link when the server starts.
14. `agentforeman demo` makes the token before it starts the server, and prints the sign-in link.
15. When the event stream gets 401, the page shows "Signed out: run agentforeman open".
- Every POST response carries `Connection: close`. A refused request leaves its body unread, and on a kept-alive connection that body would start the next request.
- `POST /api/open` takes `{"session_id": "..."}` and looks up that live session's `cwd` on the server.
- It runs VS Code's `code <cwd>`, then `open "vscode://anthropic.claude-code/open?session=<id>"`. It ignores any path in the request body.
- It finds `code` on PATH or inside `Visual Studio Code.app`. Without either, it runs `open -b com.microsoft.VSCode <cwd>`.
- A command that fails, is missing, or runs longer than 15 seconds returns 500 with its error text. A terminal session returns 409.
- An unknown ID returns 404. Other methods return 405.
- With `--dry-open`, it runs nothing and returns `{"ok": true, "commands": [[...], [...]]}`.
- `GET /` returns the page.

## Page

The page is an enterprise admin portal, in the pattern of LangSmith and Langfuse.

- A dark navy sidebar holds Overview, Sessions, Subagents, Activity, Codex, and Rules, plus the connection state.
- A top bar holds the breadcrumb, search (`/`), the project filter, alerts, and the theme switch.
- Overview: five KPI tiles, a "Needs attention" table (oldest first), an "Other sessions" table without the attention rows, and a live activity feed.
- KPI tiles always count every session. Search and the project filter change only the tables, and a chip row shows each active filter with a clear control.
- Tool-call counts include subagent calls. The Running tile names stalled sessions and stalled subagents.
- Sessions: a sortable table with status, current activity, a 30-minute sparkline, subagents, last event, since, context, model, and host.
- Narrow screens keep the important columns. Context shows from 1400 px wide. Model and host show from 1600 px wide. Below 1600 px, the Context cell shows the model under the token count.
- A Claude model ID shows in short form, for example "Opus 5.5". Any other model name shows as it is.
- Demo data: `agentforeman/demo.py` builds sample sessions in every state in a temporary folder.
- `agentforeman demo` serves them. On exit, also on SIGTERM, it removes that folder and stops its processes and teammates.
- The demo has one Edit card with a short diff, and two sessions that wait on `make eval`.
- It has a lead with two split-pane teammates, and two sessions that edited one file.
- It has exactly one repeating session, and a rule suggestion while rules are off.
- The demo answers approve, reply, steer, and stop as the hooks and the model do.
- A note arrives at the agent's next tool call, and that call reflects it.
- A session stop ends the turn at the next tool call. A note to the stalled session keeps waiting.
- `tools/record_gifs.py` records the README GIFs in `docs/media/` from the real page on the demo.
- Each GIF is 960 px wide and shows the pointer. `docs/media/` holds only the files that the README uses.
- `hero.gif` runs at most 15 s and 2 MB. Each feature clip runs at most 25 s and 3 MB.
- All media together stay under 10 MB.
- Rows show the end of the last paragraph of `last_message`. The drawer shows the full text.
- Subagents: one group per session, with the session's badge in the group header and "N of M shown" when the cap cuts rows. The header's "N working" counts working subagents and working teammates. Columns: status, subagent, started, duration, tool calls, last action, and model. The subagent title is its task (`description`). Its name, type, and parent subagent go below.
- Activity: tool calls per minute with a y-axis, and an event log table with time, source, tool, target, duration, and status.
- Codex: status badges, and subagent threads nested under their parent thread.
- A click on a session row opens a drawer with tabs: Timeline (latest request, full latest reply, tool calls), Trace (one bar per tool call and per subagent on a shared clock), Subagents, and Details.
- A click on a subagent row opens that subagent's drawer: task, result, model, duration, tool calls, and its own timeline.
- Status badges use short labels: Needs input, Awaiting approval, Stalled, Running, Idle, Parked.
- "Parked" is a finished turn older than 6 hours. "Stalled" is a busy session with no event from the session or its running subagents for 10 minutes.
- Subagent badges: Running, Stalled (no file change for 30 minutes), Done, Failed, Killed, Ended, and Teammate working or idle.
- Labels are short. Each badge, number, and button has a one-line tooltip.
- Light and dark themes. Rust marks only what needs the user. No emoji.
- The theme follows the system's `prefers-color-scheme` until the user picks one. Only a pick is saved.
- The theme button names the theme it switches to: "Switch to dark" or "Switch to light".
- The Overview starts with one sentence, in an element with class `lede`, that says what the page is for.
- With no session and no filter, the session tables say "No Claude Code sessions found. Start one, or run agentforeman demo to see sample sessions." They never say "No sessions match" then.
- The tab title is "AgentForeman".
- The page updates live from `/api/events`. The tab title starts with the "Needs attention" count.
- Keys: `j` and `k` move, `Enter` opens the drawer, `o` opens VS Code, `/` searches, `Esc` closes. `Enter` on a focused button or link only presses that control. `Esc` in the search box clears the search.
- Rows keep their place through live updates, so a click lands on the row you aimed at. By default, sessions sort by state, then by the time they entered that state, oldest first. Subagents sort by start, newest first, and Subagents groups by their newest subagent start.
- A live update keeps keyboard focus, scroll positions, and the open drawer tab. It waits while the pointer is down or text is selected on the page.

## Controls

Four hooks connect sessions to the server. They live in `agentforeman/agentforeman_hook.py`, and `agentforeman install` copies the file to `<control-dir>/hooks/` and adds four entries to `~/.claude/settings.json`. Every entry command contains `agentforeman-hook`, so the uninstaller finds it. Running sessions reload `~/.claude/settings.json`, so they get the controls without a restart.

The hook file imports only the Python standard library, because it runs alone from the control folder. Its folder comes from `AGENTFOREMAN_HOME`, and the permission grace time from `AGENTFOREMAN_PERMISSION_GRACE_S`.

The installer:

- Keeps the mode of the settings file.
- Writes through a settings file that is a symlink, and keeps the link.
- Skips hook entries that are not objects, and leaves them as they are.
- Refuses a settings file that does not parse, and changes nothing.
- On uninstall, also removes `installed.json` and the hook copy.

Each hook fails open. On any error it exits 0 with no output, so a broken hook never blocks a tool call. While the file `debug` exists in the control folder, the hook writes its errors and its steps to `hook.log` there.

The control folder holds:

| Path | Written by | Meaning |
| - | - | - |
| `requests/<id>.json` | permission hook | A permission prompt waits. Fields: `id`, `pid`, `session_id`, `agent_id`, `tool_name`, `tool_input`, `created_ms`. |
| `decisions/<id>.json` | server | `{"behavior": "allow" or "deny", "message"}` for that request. |
| `waiting/<session_id>.json` | reply hook | The session finished a turn and takes a reply. Fields: `pid`, `session_id`, `since_ms`. |
| `replies/<session_id>.json` | server | `{"id", "text", "sent_ms"}`. The reply hook deletes it when it delivers the reply. |
| `stop/<agent_id>.json` | server | Stop this subagent. `stop/.any` exists while any stop file exists. |
| `stop/session-<session_id>.json` | server | Stop this whole session. Fields: `session_id`, `at_ms`. |
| `notes/<agent_id or session_id>.json` | server | A steering note that waits for a tool call. `notes/.any` exists while any note waits. |
| `notes/delivered/<agent_id or session_id>.json` | pre hook | `{"id", "delivered_ms"}` for the last note that the hook handed over. |
| `installed.json` | installer | `{"installed_ms", "hook_sha256"}`. |
| `rules.json` | server | `{"enabled", "items": [{"rule", "added_ms"}]}`. A missing file means off and empty. |
| `auto.jsonl` | permission hook | One line for each call that a rule allowed. |
| `approvals.json` | server | `{rule key: allows}` through `POST /api/approve`. |
| `guard.json` | server | `{"mode": "off", "warn", or "block"}`. A missing file means `warn`. |
| `edits.json` | server | `{"at_ms", "files": {path: [{"session_id", "name", "last_ms"}]}}`. |
| `edits.any` | server | Exists while the guard is not off and a live session edited a file in the last 30 minutes. |

A file whose `pid` process is gone is stale. The server ignores it and deletes it.

### Approve or deny a waiting tool call

1. Claude Code runs the PermissionRequest hook when a permission dialog opens. The local dialog stays open while the hook runs, and the first answer wins.
2. The hook waits 1.5 seconds, then writes its request file. Another hook that allows at once, such as an auto-approver, wins before the file exists.
3. The hook checks for its decision file every 0.3 seconds. On a decision it prints `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": ..., "message": ...}}}`, deletes both files, and exits.
4. When you answer in VS Code or the terminal, Claude Code ends the hook, and the hook deletes its request file.
5. An answer in the Claude app (Remote Control) does not end the hook. So the hook finds its tool call in the transcript: the newest `tool_use` row with the same tool name and input, in the subagent's own transcript for a subagent. When a `tool_result` for that call appears, the hook deletes its request file and exits 0 with no output. It also exits when its parent process changes.
6. `POST /api/approve` takes `{"request_id", "behavior": "allow" or "deny", "message"}`. It writes the decision only for a live request. Otherwise it returns 404.
   The first answer wins. The server creates the decision file only when none exists, so a second answer returns 404 and changes nothing.

### Reply to a session

1. The reply hook is a Stop hook with `asyncRewake: true`, so it runs in the background and the session stays idle.
2. It writes its waiting file, then checks every second for a reply file for its session.
3. On a reply it deletes the reply file, writes the text to stderr, and exits 2. Claude Code wakes the model and shows the text as Stop hook feedback.
4. It exits 0 without a reply when the transcript gains a user or assistant entry, when a newer reply hook of the same session takes over the waiting file, when its parent process ends, or after 6 hours.
5. `POST /api/reply` takes `{"session_id", "text"}`. It writes the reply only when the session has a live waiting file. Otherwise it returns 409.
6. The model sees: `Reply from the user, sent from AgentForeman:` followed by the text.

### Stop a subagent

1. The stop hook is the PreToolUse hook, mode `pre`. Its command first checks `stop/.any` and `notes/.any` in the shell, and exits 0 when both are missing, in about 6 ms. Python starts only while a stop or a note is pending.
2. Each hook call carries `agent_id` when a subagent makes the call. A call from an agent with a stop file gets `permissionDecision: deny` and a reason that tells it to stop and report.
3. The subagent ends its turn at its next tool call. A command that already runs keeps running until it ends.
4. `POST /api/stop` takes `{"session_id", "agent_id"}` for a running subagent of a live session. The server deletes the stop file when the subagent is done, or after 6 hours.

### State

- Each agent gets `approval` (the live request or `null`), `reply_ready` (a live waiting file), `reply_pending` (a reply not yet delivered, or `null`), `controls` (the hooks are installed), and `remote_url` (`https://claude.ai/code/<bridgeSessionId>` when the registry has one).
- Each subagent gets `stop_requested`.
- `controls_installed` is set when `installed.json` exists. Its `current` is true when the installed hash matches `agentforeman/agentforeman_hook.py`. The server hashes the file again when it changes, so an update and a new install show as current without a restart.
- A live approval request sets the session status to `waiting` with `status_guess` false.

### Page

- An approval shows the tool and command with Approve and Deny, in the Needs attention table and at the top of the session panel.
- A session with `reply_ready` shows a reply box in its panel and a Reply button in the attention table. After you send, the box clears at once, and the panel shows "Sent" until the hook delivers the text.
- A running subagent shows a Stop button in its panel and on the Subagents page. The first click asks to confirm. A stopped subagent shows the badge Stopping until it ends.
- Each session panel links to its Remote Control page as "Open in Claude app", which works for sessions without controls.
- The sidebar shows whether the controls are on.

## Foreman

The foreman controls change a run while it works: a steering note, a stop for a whole session, and a flag for a loop. This section also covers frame blocking, the whole command on an approval, safe escaping, and split-pane teammates. The second release adds the approval diff and rule key, "Approve all", "Always allow" rules, and the same-file guard.

### Frame blocking

- Every HTTP response sends `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, and `X-Content-Type-Options: nosniff`.
- This covers errors, refused requests, static files, and the `/api/events` stream.
- So another page cannot show AgentForeman in a frame and trick a click on its buttons.

### The whole command on an approval

- `approval.detail` holds the whole stored command, file path, or URL. The server does not cut it.
- The permission hook keeps at most 4000 characters of each input string. `approval.detail_cut` is true when the detail has 4000 characters or more, because then the hook probably cut it.
- The Needs attention row shows the command itself, in monospace on one line with an ellipsis. Its tooltip holds the whole text. The description shows below it.
- The panel's `.ap-cmd` shows the whole command with its line breaks. When `detail_cut` is true, the panel says that the hook stored only the first 4000 characters.

### Escaping

- The page puts every server value into HTML through `esc()`. This includes the output of `modelName()` and `host()`, and every number from the server.

### Split-pane teammates

1. A team lives in `<claude-dir>/teams/<team>/config.json`. Read it again only when its mtime changes.
2. A team counts only when its `leadSessionId` is a live session. The teams of a dead lead show nowhere.
3. Each member whose `backendType` is not `in-process` is a split-pane (tmux) teammate. It runs as its own claude process.
4. Its transcript is a top-level `<project folder>/<uuid>.jsonl` in the project folder of the member's `cwd`. Its rows carry `teamName` and `agentName`.
5. To find it, read only the first 64 KB of each file that changed after `joinedAt`. A file that matched stays matched. A file that did not match is read again only when its mtime changes, and never when its first 64 KB were already full. A member without a transcript does not show.
6. Process liveness comes from at most one `ps -axo pid=,command=` call per tick. The list is kept 3 seconds, but a new teammate gets a fresh list. A teammate lives when a command line holds `--agent-id <name>@<team>` and `--parent-session-id <lead>`.
7. `state` is `running` when the process lives and the transcript changed in the last 60 seconds or has an open tool call. It is `idle` when the process lives otherwise. It is `done` with `outcome` `ended` when no process runs.
8. The teammate goes into its lead's `subagents` list, in the same shape as other subagents, plus `"backend": "tmux"`:
   - `kind` is `teammate`, and `agent_id` is the stem of its transcript, which is also its session id.
   - `name` is the member name. `description` is the first line of the member `prompt`, at most 200 characters.
   - `model` and `agent_type` come from the member. `started_ms` comes from `joinedAt`.
   - `tool_calls`, `last_action`, `recent_actions`, `last_message`, `activity`, `updated_ms`, and `note` come from its transcript and notes, as for a subagent.
9. A teammate transcript never shows as a session of its own, and the transcript fallback never claims it. This holds also when its lead died: a live session whose transcript head carries the team and name of a split-pane member of any team does not show.
10. Working teammates count in the Running tile's subagent count. Search covers them.
11. Hooks run inside the teammate's own process with its own `session_id` and no `agent_id`. So a steer or stop for a split-pane teammate is a main-thread note or a session stop keyed by its `agent_id`.
12. A permission request from a split-pane teammate shows on its lead, with the teammate's `agent_id`. So the panel says which teammate asks.

### Steering notes

1. `POST /api/steer` takes `{"session_id", "agent_id", "text"}`. `agent_id` is optional. The route needs the token, host, and origin checks of every POST.
2. It returns 404 for an unknown session or subagent, and 400 for empty text.
3. It returns 409 when the target runs no tool call: a session whose registry `status` is not `busy`, or a subagent whose `state` is not `running`.
4. It writes `<control-dir>/notes/<agent_id or session_id>.json` = `{"id", "session_id", "agent_id", "text", "at_ms"}`. `agent_id` is `null` for the main thread. The text has at most 4000 characters. A newer note for the same target replaces the old one.
5. `notes/.any` exists while any note waits, as `stop/.any` does for stops.
6. The `pre` hook hands over a note at the next PreToolUse call of the matching thread:
   - A subagent note goes to the call whose `agent_id` is equal.
   - A main-thread note goes to a call with no `agent_id` and the same `session_id`.
7. The hook prints `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "..."}}`. The context starts with "Note from the user, sent from AgentForeman while you work:", then the text.
8. A note never sets `permissionDecision`. It never blocks or answers the call.
9. The hook moves the note aside before it reads it, so two parallel calls cannot both get it. Then it deletes the note and writes `notes/delivered/<key>.json` = `{"id", "delivered_ms"}`.
10. `/api/state` gives each session and subagent `"note": {"pending", "text", "at_ms", "delivered_ms"}`. After a delivery, `text` and `at_ms` come from the server's memory, so they are `null` after a server restart.
11. The server removes a main-thread note when the session's turn ends (registry `status` is not `busy`). It removes a subagent note when that subagent is done.
12. It removes the note of a split-pane teammate when its process ends, or when the teammate's own registry entry has a `status` other than `busy`.
13. The server also removes any note older than 6 hours, and delivery receipts older than 1 hour.

### Stop a whole session

1. `POST /api/stop` with `session_id` and no `agent_id` stops the whole session.
2. It returns 404 for an unknown session and 409 when the registry `status` is not `busy`.
3. Otherwise it writes `stop/session-<session_id>.json` = `{"session_id", "at_ms"}` and sets `stop/.any`.
4. `/api/state` gives each session `stop_requested`.
5. On a main-thread call of that session, the `pre` hook prints `{"continue": false, "stopReason": "The user stopped this session from AgentForeman."}` and deletes the file. So the stop works once.
6. On a subagent call of that session, the hook denies the call with the subagent stop reason, until the main-thread call uses the stop. Split-pane teammates are sessions of their own, so the lead's stop does not reach them.
7. The server deletes the session stop when the turn ends, and when `POST /api/reply` succeeds for that session, because a reply wakes the session.
8. A stop for a split-pane teammate writes `stop/session-<its agent_id>.json`. It needs `state` `running`, else 409. The server deletes it as it deletes the teammate's note.
9. A subagent stop works as before.

### Hook modes

- `agentforeman/agentforeman_hook.py` has the modes `permission`, `reply`, `pre`, and `edit`. `pre` is for PreToolUse. It handles session stops, subagent stops, and notes, in that order.
- `edit` is the same-file guard. It runs on PreToolUse for Edit, Write, MultiEdit, and NotebookEdit.
- `stop` stays an alias of `pre`, because settings from an older install call it.
- Every mode fails open: on any error it exits 0 with no output.
- `agentforeman install` adds exactly four hook entries. The `pre` command starts Python only when `stop/.any` or `notes/.any` exists, and exits at once otherwise.
- The `edit` entry has the matcher `Edit|Write|MultiEdit|NotebookEdit`. Its command starts Python only when `edits.any` exists.
- A second install replaces the entries and never adds them twice. `agentforeman uninstall` removes exactly the four entries.

### Repeat flag

- `/api/state` gives each session and subagent `"repeating": {"tool", "target", "count"}` or `null`. Each session also gets `last_action`, its newest action or `null`, as a subagent does.
- It is set when at least 4 of the last 8 tool calls share the same tool and target. The target is the action `summary`. A call with an empty summary never counts.
- The page shows a `.flag-repeat` chip in the session row and in the panel, for example "Repeating: Read config.toml x5". Its tooltip suggests a steer note.

### Page

- `/api/state` gives each session `busy`: true when the registry `status` is `busy`.
- The panel of a busy session holds a steer box, `textarea[data-steer=<session id>]`, with a send button `[data-steer-send=<session id>]`. `Cmd+Enter` also sends.
- The panel of a running subagent or teammate holds the same box, keyed by its `agent_id`.
- After a send, the panel shows `.steer.sent` with "Waits for its next tool call", then "Delivered at <time>".
- An idle session shows no steer box. Without current controls, the box says how to install them.
- The panel of a busy session holds `[data-stop-session=<session id>]` when the controls are installed. With an older hook, its tooltip says to run the installer again, because an older hook never reads a session stop.
- The first click changes its text to "Confirm stop" for 4 seconds. The second click stops the session, the same as the subagent Stop button.
- A running split-pane teammate has a Stop button, like a subagent. Its badges are "Teammate · working", "Teammate · idle", "Teammate · ended", and "Stopping".
- Teammates show on the panel's Subagents tab and on the Subagents page, marked "split pane".

### The diff on an approval

1. Each approval gets `"diff"` (unified diff text, or `null`) and `"diff_note"` (one plain sentence, or `null`).
2. Edit: the diff of `old_string` to `new_string`. MultiEdit: the diffs of every edit, with one file header.
3. Write: the diff of the file on disk against the new content, only when all of these hold:
   - The resolved path is inside the resolved session `cwd`. The session `cwd` comes from the registry.
   - The path holds no link that resolves out of the `cwd`.
   - The file is missing, or it is a regular file of at most 256 KB.
   - A file on disk has one hard link and no NUL byte.
4. A missing file inside the `cwd` shows every new line as added.
5. Otherwise the diff is `null`, and `diff_note` gives the line count of the new content and the reason.
6. The server never opens a file outside the session folder. So no content of such a file reaches `/api/state`.
7. Other tools get a `null` diff.
8. The diff text has at most 20,000 characters. A cut diff ends with `...`, and `diff_note` says so.
9. The hook keeps 4000 characters of each input string. A cut Edit says so in `diff_note`. A cut Write shows no diff.
10. The server makes each diff once for each request, because a file open can be slow.

### The rule key

1. Each approval gets `"rule_key"`, the rule that would allow calls like it.
2. Bash: `Bash(<first two words>)` of the first part that is not a plain `cd <path>`, after leading `VAR=value` words.
3. A part of one word gives `Bash(<word>)`. For example `cd /work/beta && CI=1 npm test -- --watch` gives `Bash(npm test)`.
4. Edit, Write, MultiEdit, and NotebookEdit give `<Tool>(<file_path>)`. NotebookEdit uses `notebook_path` when `file_path` is missing.
5. WebFetch gives `WebFetch(<host>)`. Any other tool gives `<Tool>`.
6. Each approval also gets `"rule_ok"`: true when the key is a valid rule. Only then the card shows "Always allow".
7. `rule_key`, the Bash split, `rule_valid`, and the matcher live in `agentforeman/agentforeman_hook.py`. The server imports that file through `agentforeman/rulelib.py`.

### Approve all

1. Each approval gets `"similar"`: the number of other live, unanswered requests in any session with the same rule key.
2. The key need not be a valid rule. Two waiting `Task` calls count, and so do two `mcp__x__y` calls.
3. A Bash key names only the start of a command. So `npm test && git push` counts under `Bash(npm test)`, and the button tooltip says so.
4. `POST /api/approve` takes `"similar": true`. It answers the named request and every counted request with the same behavior.
5. It returns `{"ok": true, "count": N}`, where N counts the named request too. Without `similar`, N is 1.

### Rules on the page side

1. `<control>/rules.json` = `{"enabled": bool, "items": [{"rule", "added_ms"}]}`. A missing or broken file means off and empty.
2. `POST /api/rules` takes `{"action": "add" | "remove" | "enable", "rule"?, "enabled"?}`. It needs the token, host, and origin checks.
3. Adding never turns rules on. Adding a rule that exists changes nothing and returns 200.
4. An unknown action, a missing field, or an invalid rule returns 400 and leaves the file as it was.
5. Valid Bash rules: `Bash(<1 to 3 plain words>)`, with single spaces. A plain word holds only letters, digits, and `_./:@%+=,~-`.
6. The first word never starts with `-`, never holds `=`, and is not a wrapper.
7. Wrappers: `eval`, `exec`, `sudo`, `su`, `env`, `xargs`, `nohup`, `command`, `builtin`, `source`, `.`, `time`, `nice`, `timeout`, `watch`, `doas`.
8. Interpreters: `bash`, `sh`, `zsh`, `fish`, `dash`, `python`, `python3`, `node`, `deno`, `bun`, `perl`, `ruby`, `php`, `osascript`, `pwsh`, `npx`.
   - The check uses the program name too: the first word without its folder and without trailing version digits.
   - So `/bin/bash`, `/usr/bin/sudo`, `.venv/bin/python`, and `python3.12` count as `bash`, `sudo`, `python`, and `python`.
9. An interpreter rule needs a second word that is not a flag. The one exception is `-m`, which needs a third word that is not a flag.
10. So `Bash(python3 -m pytest)` is valid. `Bash(python3)`, `Bash(python3 -c)`, `Bash(python3 -m)`, and `Bash(node -e)` are not.
11. This flag check is stricter than a list of inline-code flags. Any flag can run code or change what runs.
12. Read, Edit, Write, MultiEdit, and NotebookEdit need an absolute folder or file prefix, at least two folders deep.
13. The prefix has no `..` or `.` part and no `//`. For example `Edit(/work/beta/src/)`.
14. WebFetch needs a host name, for example `WebFetch(docs.python.org)`. Grep, Glob, LS, and WebSearch stand alone, without an argument.
15. Every other rule returns 400, for example `Bash`, `Edit`, `Edit(/)`, `Bash(sudo apt install)`, and `Nonsense(x)`.
16. The server counts each allow through `POST /api/approve` under the request's rule key in `<control>/approvals.json`. Denials never count.
17. `/api/state` gives `"rules": {"enabled", "items", "suggestions", "recent"}`.
18. `items`: `[{"rule", "added_ms", "hits"}]`. `hits` counts the `auto.jsonl` lines for that rule.
19. `suggestions`: `[{"rule", "approvals"}]` for each key with 3 or more allows that is a valid rule and not yet a rule.
20. `recent`: the last 20 lines of `auto.jsonl`, newest first.
21. The server reads only the lines that `auto.jsonl` gained since the last tick.

### Rules on the hook side

1. In mode `permission`, the hook reads `rules.json` before the 1.5 second wait. A `claude -p` run gets no rules, as it gets no page.
2. When `enabled` is exactly `true` and a valid rule matches the call, the hook allows the call at once.
3. It appends `{"at_ms", "session_id", "agent_id", "tool_name", "summary", "rule"}` to `auto.jsonl`. Then it prints the allow decision.
4. It writes no request file. When it cannot append to `auto.jsonl`, it sends the call to the page.
5. The hook checks each rule from the file again with `rule_valid`, because a person can edit the file by hand.
6. Rules off, a missing file, or a broken file: the hook behaves exactly as before.
7. The hook matches the untrimmed tool input, so a long command cannot hide a second command after 4000 characters.

Bash matching fails closed. There is no match when the command holds any of these:

- A backtick, any `$`, `<`, `>`, `(`, `)`, `{`, `}`, a backslash, a line break, or a carriage return.
- A quoted `;`, `&`, or `|`. An unclosed quote, or a word that `shlex` cannot split.
- A single `&`, `|&`, or an empty part, for example `npm test;`.
- A leading `VAR=value` word whose name changes what runs or what loads.
- These names: `PATH`, `IFS`, `ENV`, `BASH_ENV`, `SHELLOPTS`, `BASHOPTS`, `PS4`, `PROMPT_COMMAND`, `NODE_OPTIONS`, and `NODE_PATH`.
- These names too: `PERL5OPT`, `PERL5LIB`, `RUBYOPT`, `RUBYLIB`, and names that start with `LD_`, `DYLD_`, `PYTHON`, `GIT_`, or `npm_config_`.

A bare `$`, a backslash, and these variable names close ways to run other code under a plain rule.

1. The matcher splits on `&&`, `||`, `;`, and `|`. It drops leading `VAR=value` words from each part.
2. Each part must start with the words of some Bash rule, word by word. So `npm testing` does not match `Bash(npm test)`.
3. A part that is exactly `cd <one path>` is neutral. A call with only neutral parts matches nothing.
4. The log names the rule of the first part that is not neutral.
5. File rules match an absolute `file_path` with no `..` part. Its normalized path and its resolved path must both be under the prefix.
6. A prefix covers the path itself and every path below it. It never covers a path that only starts with the same letters.
7. A rule covers its own tool only. So `Edit(/work/app/src/)` does not allow a Write.
8. WebFetch rules match the exact host of an `http` or `https` URL.

### Same-file guard on the page side

1. On every tick, the server finds the files that each live session and its subagents edited in the last 30 minutes.
2. An edit is a call to Edit, Write, MultiEdit, or NotebookEdit with a `file_path`. The server normalizes the path.
3. A split-pane teammate is a session of its own. Its edits count under its own session id and name.
4. The server writes `<control>/edits.json` = `{"at_ms", "files": {path: [{"session_id", "name", "last_ms"}]}}`.
5. It writes the file when the files change, and at least every 60 seconds, also when it lists no file.
6. `name` is the registry name of the session, for example `care-assistant-3f`.
7. `/api/state` gives each session `"shared_files": [{"path", "with": [{"session_id", "name"}]}]`.
8. A file is in `shared_files` when another live session also edited it in the last 30 minutes. The list is empty otherwise.
9. `/api/state` gives `"guard": {"mode"}` at the top level.
10. `<control>/guard.json` = `{"mode": "off" | "warn" | "block"}`. The default is `warn`.
11. `POST /api/guard` takes `{"mode"}` and returns 400 for any other value. It needs the token, host, and origin checks.
12. The server keeps `<control>/edits.any` while the mode is not off and some live session edited a file.

### Same-file guard on the hook side

1. The hook mode `edit` runs on PreToolUse for Edit, Write, MultiEdit, and NotebookEdit.
2. It exits quietly when the mode is off, or when `edits.json` is missing, broken, or older than 120 seconds.
3. It also exits quietly when no other session edited the file in the last 30 minutes.
4. An old `edits.json` means that the server is down, so the guard steps aside.
5. Otherwise it writes one sentence that names the other session and the file.
6. Warn mode prints `hookSpecificOutput.additionalContext` with that sentence. The call runs, and the model reads the sentence.
7. Block mode prints `permissionDecision` `ask` with the sentence as `permissionDecisionReason`. Claude Code asks you first.
8. The mode fails open on any error.

### Page for the second release

- The approval card shows the diff in `.ap-diff`. Added lines are green, and removed lines are rust.
- When there is no diff, the card shows `diff_note`.
- `[data-approve-similar]` shows when `similar` is more than 0. Its text is "Approve all N", and N counts this call too.
- The first click changes the text to "Confirm: approve all N" for 4 seconds. The second click sends the request.
- `[data-always-allow]` reads "Always allow <rule key>". It shows only when `rule_ok` is true.
- Always allow adds the rule. The call on the card still waits for Approve or Deny.
- While rules are off, the card says that rules are off and links to the Rules page.
- The sidebar has a Rules entry, route `#rules`. Its pill shows "On" while rules are on.
- The Rules page holds the switch `[data-rules-enabled]`, off by default.
- Next to the switch, one sentence says that rules approve calls even while the server does not run.
- The page lists the rules with hits and a Remove button, and a box to type a new rule.
- It lists the suggestions with an Add button, and the recent calls that rules allowed.
- The page also holds the guard mode: Off, Warn, or Block, with one sentence for the mode in use.
- The Overview shows a `.shared-files` panel while any session has a shared file. It lists each file and its sessions.
- The session panel shows a "Shared files" chip when that session has a shared file.
