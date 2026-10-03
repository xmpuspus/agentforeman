# Security policy

## Report a problem privately

Do not open a public issue for a security problem. Use a private GitHub security advisory:

1. Open https://github.com/xmpuspus/agentforeman/security/advisories/new.
2. Describe the problem, the version, and the steps that show it.
3. Send the advisory.

The maintainer answers in the advisory. A fix and a new release come before any public notice.

## Threat model

AgentForeman reads your Claude Code and Codex sessions. With its hooks on, it can approve tool calls, send text to sessions, and stop them. So it guards its page and controls against other accounts, other web pages, and other computers.

### What AgentForeman defends against

- **Other computers.** The server listens on `127.0.0.1` only. No other computer can connect.
- **Other accounts on your Mac.** Every account can reach `127.0.0.1`. So the page and its data need the token, or they return 401.
- **The token.** It is random, and it lives in `~/.agentforeman/token` with mode `0600`. It stays the same across restarts.
- **The sign-in link.** `agentforeman open` and `--open` give the token to the browser in a link.
- **A program that takes the port.** Before it opens the link, `agentforeman open` checks that the server knows the token. Another program on the port gets no link.
- **The redirect.** The server answers the link with a cookie, then sends the browser to `/`. The token leaves the address bar.
- **The cookie.** It is `HttpOnly` and `SameSite=Lax`, and its name holds the port. Page scripts cannot read it.
- **Logs.** The server prints no token and logs no request. The demo prints its link, because it shows sample data only.
- **Other web pages.** Each POST must carry the token in a header. The page gets it from `GET /`, which needs the cookie.
- **Cross-site requests.** A POST must send `Content-Type: application/json`. The browser then sends a CORS preflight, which the server never answers.
- **Origin checks.** The server refuses a POST with an `Origin` header other than its own page.
- **DNS rebinding.** The server refuses a request whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>`.
- **Clickjacking.** Every response blocks frames: `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'`.
- **Request smuggling.** Every POST response closes its connection.
- **Path tricks.** `POST /api/open` takes a session ID and looks up the folder on the server. It ignores any path in the request.
- **File leaks in diffs.** An approval diff reads only files inside the session folder. A link out of that folder never counts.

### Rules are off by default and fail closed

- "Always allow" rules start off. Adding a rule never turns rules on.
- A Bash rule never matches a command that the matcher cannot read safely.
- Such commands hold `$`, backticks, redirects, groups, backslashes, a quoted `;`, or a risky variable such as `PATH`.
- A rule never starts with a wrapper such as `sudo` or `env`.
- The server refuses an interpreter rule such as `Bash(python3 -c)`.
- A missing or broken rules file allows nothing.

### What AgentForeman trusts

- **Programs of the same macOS user.** They can read and write the control folder, `~/.agentforeman`, as you can. They can also read your Claude Code files directly. AgentForeman does not defend against them.
- **The control folder.** The server and the installer create it with mode `0700`, so other users cannot read it.
- **Your browser.** Browsers share `127.0.0.1` cookies across ports. If your browser opens a page that another account serves on `127.0.0.1`, that page gets the cookie. Open only local pages that you trust.
- **Claude Code.** The hooks act on the data that Claude Code gives them.

### Hooks fail open

A hook that fails exits with no output, so Claude Code goes on as if the hook were not there. A broken hook never blocks a tool call, and never approves one.
