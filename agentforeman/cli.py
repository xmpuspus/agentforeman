"""The agentforeman command: serve the page, open it, run the demo, or add and remove the
control hooks.

Run: agentforeman [serve] [--port 4320] [--open]
     agentforeman open [--port 4320] [--print]
     agentforeman demo [--port 4321]
     agentforeman install | uninstall [--settings ~/.claude/settings.json]
"""

import argparse
import hmac
import json
import secrets
import sys
import urllib.request
import webbrowser
from pathlib import Path

from agentforeman import __version__

CONTROL_DIR = "~/.agentforeman"
# OpenTelemetry collectors listen on this port, and Claude Code may export to it.
RESERVED_PORT = 4317


def parser():
    ap = argparse.ArgumentParser(
        prog="agentforeman",
        description="Approve, steer, and stop every Claude Code agent from one page.",
    )
    ap.add_argument(
        "--version", action="version", version=f"agentforeman {__version__}"
    )
    sub = ap.add_subparsers(
        dest="command", metavar="{serve,open,demo,install,uninstall}"
    )
    s = sub.add_parser("serve", help="serve the page on 127.0.0.1 (the default)")
    s.add_argument("--port", type=int, default=4320)
    s.add_argument("--claude-dir", default="~/.claude")
    s.add_argument("--codex-dir", default="~/.codex")
    s.add_argument("--control-dir", default=CONTROL_DIR)
    s.add_argument(
        "--dry-open",
        action="store_true",
        help="return the VS Code open commands without running them",
    )
    s.add_argument("--open", action="store_true", help="open the page in the browser")
    o = sub.add_parser("open", help="open the running page in the browser")
    o.add_argument("--port", type=int, default=4320)
    o.add_argument("--control-dir", default=CONTROL_DIR)
    o.add_argument(
        "--print",
        action="store_true",
        help="print the link instead of opening it, for another browser",
    )
    d = sub.add_parser("demo", help="serve the page on sample sessions")
    d.add_argument("--port", type=int, default=4321)
    d.add_argument(
        "--finish-after",
        type=float,
        default=None,
        help="seconds until the care-kb session asks a question",
    )
    for name, text in (
        ("install", "add the control hooks to Claude Code"),
        ("uninstall", "remove the control hooks from Claude Code"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--settings", default="~/.claude/settings.json")
        p.add_argument("--control-dir", default=CONTROL_DIR)
    return ap


def open_page(port, control_dir, print_only=False):
    """Opens the page with its sign-in link. The link holds the token from the control folder,
    so only this macOS user can make it."""
    from agentforeman.server import TOKEN_RE, proof, sign_in_url

    try:
        token = (Path(control_dir).expanduser() / "token").read_text().strip()
    except OSError:
        token = ""
    if not TOKEN_RE.fullmatch(token):
        print("AgentForeman has not started yet. Start it with: agentforeman --open")
        return 1
    # Another program can listen on the port while AgentForeman is stopped. It must never get
    # the link, so the server first shows that it knows the token.
    nonce = secrets.token_hex(16)
    try:
        hello = f"http://127.0.0.1:{port}/api/hello?nonce={nonce}"
        with urllib.request.urlopen(hello, timeout=3) as r:
            sent = str(json.loads(r.read()).get("proof") or "")
    except (OSError, ValueError, AttributeError):
        sent = None
    if sent is None:
        print(
            f"Nothing answers on port {port}. Start AgentForeman with: agentforeman --open"
        )
        return 1
    if not hmac.compare_digest(sent, proof(token, nonce)):
        print(
            f"The program on port {port} is not your AgentForeman, so nothing opened."
        )
        return 1
    url = sign_in_url(port, token)
    if print_only:
        print(url)
    else:
        webbrowser.open(url)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    # With no command, agentforeman serves, so `agentforeman --port 5000` works too.
    if not argv or (
        argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")
    ):
        argv = ["serve", *argv]
    ap = parser()
    args = ap.parse_args(argv)
    if getattr(args, "port", None) == RESERVED_PORT:
        ap.error("port 4317 is reserved for OpenTelemetry; pick another port")
    if args.command == "serve":
        from agentforeman.server import serve

        return serve(
            port=args.port,
            claude_dir=args.claude_dir,
            codex_dir=args.codex_dir,
            control_dir=args.control_dir,
            dry_open=args.dry_open,
            open_browser=args.open,
        )
    if args.command == "open":
        return open_page(args.port, args.control_dir, args.print)
    if args.command == "demo":
        from agentforeman.demo import run

        return run(args.port, args.finish_after)
    from agentforeman.install import run

    return run(args.settings, args.control_dir, uninstall=args.command == "uninstall")


if __name__ == "__main__":
    sys.exit(main())
