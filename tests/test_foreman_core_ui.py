"""Browser tests for the first foreman release, driven through the page the way a person uses it.

Selectors that the page must keep:
- The Needs attention row of a waiting tool call shows the command itself in its message cell.
- The session panel holds a steer box `textarea[data-steer=<session id>]` and its send button
  `[data-steer-send=<session id>]` while the session runs. After a send, the panel shows an
  element `.steer.sent`.
- The session panel of a running session holds `[data-stop-session=<session id>]`. The first
  click changes its text to "Confirm stop". The second click stops the session.
- A row flagged as repeating holds an element `.flag-repeat` whose text contains "Repeating".
- A split-pane teammate shows on the Subagents tab of its lead's panel, like any subagent.
"""

import json

import pytest
from test_acceptance import ROOT, enc, home, state, wait_for  # noqa: F401
from test_foreman_core import append, call, make_team, procs, result  # noqa: F401
from test_acceptance import signed
from test_ui_e2e import browser, drop, page, portal, sleeper  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")


def open_session(page, sid):  # noqa: F811
    page.goto(page.url.split("#")[0] + "#sessions")
    page.wait_for_selector("table.sessions")
    page.click(f"table.sessions tr[data-sid={sid}]")
    page.wait_for_selector("#drawer:not([hidden]) h3")


def test_the_attention_row_shows_the_command_itself(page, home, sleeper):  # noqa: F811
    drop(
        home["control"] / "requests",
        "rq9.json",
        {
            "id": "rq9",
            "pid": sleeper(),
            "session_id": "sid-beta",
            "tool_name": "Bash",
            "tool_input": {
                "command": "git push origin webhook-retries",
                "description": "Push the branch",
            },
            "created_ms": 0,
        },
    )
    row = ".panel.attn tr[data-sid=sid-beta]"
    page.wait_for_selector(f"{row} [data-approve]")
    assert "git push origin webhook-retries" in page.text_content(row)


def test_the_panel_shows_a_long_command_uncut(page, home, sleeper):  # noqa: F811
    cmd = "python3 - <<'EOF'\n" + "print('step')\n" * 120 + "EOF"
    drop(
        home["control"] / "requests",
        "rq8.json",
        {
            "id": "rq8",
            "pid": sleeper(),
            "session_id": "sid-beta",
            "tool_name": "Bash",
            "tool_input": {"command": cmd, "description": "Print the steps"},
            "created_ms": 0,
        },
    )
    page.wait_for_selector(".panel.attn tr[data-sid=sid-beta] [data-approve]")
    page.click(".panel.attn tr[data-sid=sid-beta] .cell-main")
    page.wait_for_selector("#drawer .ap-cmd")
    shown = page.text_content("#drawer .ap-cmd")
    assert shown.count("print('step')") == 120, "every line of the command shows"


def test_steer_a_running_session_from_its_panel(page, home):  # noqa: F811
    open_session(page, "sid-alpha")
    box = "#drawer textarea[data-steer=sid-alpha]"
    page.wait_for_selector(box)
    page.click(box)
    page.keyboard.type("Use the staging database.")
    page.click("#drawer [data-steer-send=sid-alpha]")
    path = home["control"] / "notes" / "sid-alpha.json"
    wait_for(lambda: path.exists())
    assert json.loads(path.read_text())["text"] == "Use the staging database."
    page.wait_for_selector("#drawer .steer.sent", timeout=10000)


def test_an_idle_session_offers_no_steer_box(page):  # noqa: F811
    open_session(page, "sid-beta")
    assert page.query_selector("#drawer textarea[data-steer=sid-beta]") is None


def test_stop_a_whole_session_asks_to_confirm(page, home):  # noqa: F811
    from test_ui_e2e import install_marker

    install_marker(home)
    open_session(page, "sid-alpha")
    stop = "#drawer [data-stop-session=sid-alpha]"
    page.wait_for_selector(stop)
    page.click(stop)
    page.wait_for_function(
        f"document.querySelector('{stop}').textContent.includes('Confirm stop')"
    )
    path = home["control"] / "stop" / "session-sid-alpha.json"
    assert not path.exists(), "one click only asks"
    page.click(stop)
    wait_for(lambda: path.exists())


def test_a_repeating_session_is_flagged_in_its_row(page, home):  # noqa: F811
    eps = home["claude"] / "projects" / enc("/work/epsilon") / "sid-eps-new.jsonl"
    rows = []
    for i in range(5):
        rows += [
            call(
                "sid-eps", f"ru{i}", "Read", {"file_path": "/work/epsilon/config.toml"}
            ),
            result("sid-eps", f"ru{i}"),
        ]
    append(eps, *rows)
    page.goto(page.url.split("#")[0] + "#sessions")
    page.wait_for_selector(
        "table.sessions tr[data-sid=sid-eps] .flag-repeat", timeout=10000
    )
    assert "Repeating" in page.text_content(
        "table.sessions tr[data-sid=sid-eps] .flag-repeat"
    )


def test_split_pane_teammates_show_in_the_leads_panel(page, portal, home, procs):  # noqa: F811
    make_team(home, procs)
    port = int(portal.rsplit(":", 1)[1])
    wait_for(
        lambda: any(
            s["name"] == "lane-a" for a in state(port)["agents"] for s in a["subagents"]
        ),
        timeout=10,
    )
    open_session(page, "sid-alpha")
    page.click("#drawer [data-tab=subagents]")
    page.wait_for_function(
        "document.querySelector('#drawer').textContent.includes('Research the A market')",
        timeout=10000,
    )
    text = page.text_content("#drawer")
    assert "lane-a" in text and "Research the A market" in text


def test_the_portal_does_not_render_inside_another_page(browser, portal):  # noqa: F811
    ctx = browser.new_context()
    pg = ctx.new_page()
    # The signed link would show the page, so only the frame headers can stop it.
    src = signed(portal + "/")
    pg.set_content(f'<iframe id="f" src="{src}" width="800" height="600"></iframe>')
    pg.wait_for_timeout(2500)
    frame = next((f for f in pg.frames if f != pg.main_frame), None)
    rendered = False
    if frame is not None:
        try:
            rendered = frame.query_selector("#content") is not None
        except Exception:
            rendered = False
    ctx.close()
    assert not rendered, "a page on another origin must not show the portal in a frame"


def test_a_model_name_with_markup_shows_as_text(page, home):  # noqa: F811
    row = call("sid-alpha", "mx1", "Bash", {"command": "ls"})
    row["message"]["model"] = '<i id="xss-model">m</i>'
    row["message"]["usage"] = {"input_tokens": 1, "output_tokens": 1}
    append(home["trans_a"], row)
    page.goto(page.url.split("#")[0] + "#sessions")
    page.wait_for_function(
        "document.querySelector('table.sessions').textContent.includes('xss-model')",
        timeout=10000,
    )
    assert page.query_selector("#xss-model") is None
