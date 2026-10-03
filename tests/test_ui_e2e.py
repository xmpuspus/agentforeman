"""Browser tests that drive the portal page against the fake sessions in test_acceptance.

Fixture facts: alpha-1 and eps-1 run, beta-1 waits for a reply (15 min), gamma-1 has an
unanswered tool call (8 min), delta-1 is idle in a terminal. alpha-1 has five subagents, and
"dates" (task "Scan dates") was started by "rules" (task "Check expiry rules").
"""

import json
import subprocess
import sys

import pytest
from test_acceptance import (  # noqa: F401
    ROOT,
    free_port,
    home,
    signed,
    state,
    tool_call,
    wait_for,
)

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def portal(home):  # noqa: F811
    port = free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentforeman",
            "serve",
            "--port",
            str(port),
            "--claude-dir",
            str(home["claude"]),
            "--codex-dir",
            str(home["codex"]),
            "--control-dir",
            str(home["control"]),
            "--dry-open",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: state(port), timeout=20)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(5)


@pytest.fixture
def page(browser, portal):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    pg.errors = errors  # a test that causes an expected error removes it from here
    pg.goto(signed(portal + "/#overview"))
    pg.wait_for_selector("#content .kpis")
    yield pg
    ctx.close()
    assert not errors, f"browser errors: {errors}"


def rows(pg, table="table.sessions"):
    return pg.eval_on_selector_all(
        f"#content {table} tbody tr[data-sid] .cell-main",
        "els => els.map(e => e.textContent)",
    )


def test_overview_counts_and_attention_order(page):
    tiles = page.eval_on_selector_all(
        ".kpi",
        "els => els.map(e => [e.querySelector('.k').textContent, e.querySelector('.v').textContent])",
    )
    values = dict(tiles)
    assert values["Needs attention"] == "2" and values["Running"] == "2"
    assert values["Idle"] == "1" and values["Live sessions"] == "5"
    attention = page.eval_on_selector_all(
        ".panel.attn tbody tr .cell-main", "els => els.map(e => e.textContent)"
    )
    assert attention == ["beta-1", "gamma-1"], "oldest wait first"
    badges = page.eval_on_selector_all(
        ".panel.attn tbody .badge", "els => els.map(e => e.textContent)"
    )
    assert badges == ["Needs input", "Awaiting approval"]
    assert page.title().startswith("(2) ")


def test_kpi_tile_opens_filtered_sessions(page):
    page.click(".kpi.attn")
    page.wait_for_function("location.hash === '#sessions'")
    assert page.text_content(".seg button.on") == "Needs attention"
    assert sorted(rows(page)) == ["beta-1", "gamma-1"]
    page.click("#nav a[data-route=overview]")
    page.wait_for_selector(".kpis")
    page.click("a[data-go=sessions]")
    page.wait_for_function(
        "document.querySelector('.seg button.on')?.textContent === 'All'"
    )
    assert len(rows(page)) == 5, "View all drops the old status filter"


def test_sort_search_and_project_filter(page):
    page.goto(page.url.split("#")[0] + "#sessions")
    page.wait_for_selector("table.sessions")
    page.click("th[data-sort=name]")
    assert rows(page) == ["alpha-1", "beta-1", "delta-1", "eps-1", "gamma-1"]
    page.click("th[data-sort=name]")
    assert rows(page) == ["gamma-1", "eps-1", "delta-1", "beta-1", "alpha-1"]
    page.fill("#search", "callers")
    page.wait_for_function(
        "document.querySelectorAll('table.sessions tbody tr').length === 1"
    )
    assert rows(page) == ["alpha-1"], "search reaches subagent names"
    page.fill("#search", "zzzz-no-match")
    page.wait_for_selector("#content .empty")
    page.fill("#search", "")
    page.select_option("#project-filter", "/work/beta")
    page.wait_for_function(
        "document.querySelectorAll('table.sessions tbody tr').length === 1"
    )
    assert rows(page) == ["beta-1"]


def test_drawer_timeline_nested_subagents_and_keys(page):
    page.goto(page.url.split("#")[0] + "#sessions")
    page.wait_for_selector("table.sessions")
    page.click("table.sessions tr[data-sid=sid-alpha]")
    page.wait_for_selector("#drawer:not([hidden]) h3")
    assert page.text_content("#drawer h3") == "alpha-1"
    first = page.text_content("#drawer .timeline li")
    assert "Edit" in first and "app.py" in first and "Running" in first
    page.click("#drawer [data-tab=subagents]")
    names = page.eval_on_selector_all(
        "#drawer tbody tr .cell-main", "els => els.map(e => e.textContent)"
    )
    assert len(names) == 5
    i = names.index("└Scan dates")
    assert names[i - 1] == "Check expiry rules", (
        "a nested subagent follows the subagent that started it"
    )
    page.click("#drawer [data-tab=details]")
    assert "/work/alpha" in page.text_content("#drawer .kv")
    page.keyboard.press("Escape")
    page.wait_for_selector("#drawer", state="hidden")
    page.keyboard.press("j")
    page.keyboard.press("Enter")
    page.wait_for_selector("#drawer:not([hidden]) h3")
    page.keyboard.press("Escape")
    page.wait_for_selector("#drawer", state="hidden")


def test_subagents_page_groups_and_nests(page):
    page.goto(page.url.split("#")[0] + "#subagents")
    page.wait_for_selector("tr.group")
    assert "alpha-1" in page.text_content("tr.group")
    child = page.text_content("tr.child")
    assert "Scan dates" in child and "started by Check expiry rules" in child


def test_subagent_rows_stay_in_place_when_one_works(page, home):  # noqa: F811
    # The server lists the newest file change first. A row that moves on every tool call
    # takes the click that was meant for the row above it.
    page.goto(page.url.split("#")[0] + "#subagents")
    page.wait_for_selector("#content tr[data-aid]")
    order = page.eval_on_selector_all(
        "#content tr[data-aid]", "els => els.map(e => e.dataset.aid)"
    )
    assert order[:2] == ["aaa", "eee"] and order.index("ccc") > 1, order
    subs = home["trans_a"].parent / "sid-alpha" / "subagents"
    with open(subs / "agent-ccc.jsonl", "a") as f:
        f.write(
            json.dumps(tool_call("sid-alpha", "s3b", "Grep", {"pattern": "MOVED"}, 0))
            + "\n"
        )
    page.wait_for_function(
        "document.querySelector('#content tr[data-aid=ccc]').textContent.includes('MOVED')",
        timeout=10000,
    )
    after = page.eval_on_selector_all(
        "#content tr[data-aid]", "els => els.map(e => e.dataset.aid)"
    )
    assert after == order


def test_activity_and_codex_pages(page):
    page.goto(page.url.split("#")[0] + "#activity")
    page.wait_for_selector("#content table tbody tr[data-sid]")
    log = "#content table tbody tr[data-sid]"
    assert page.eval_on_selector_all(log, "els => els.length") == 11, (
        "5 session calls, 4 subagent calls, and one each for gamma-1 and eps-1"
    )
    page.goto(page.url.split("#")[0] + "#codex")
    page.wait_for_selector("#content table tbody tr")
    assert "Fix login bug" in page.text_content("#content table tbody")
    cursor = page.eval_on_selector(
        "#content table tbody tr", "e => getComputedStyle(e).cursor"
    )
    assert cursor == "default", (
        "a Codex row opens nothing, so it must not look clickable"
    )


def test_theme_persists(page):
    page.click("#theme")
    assert page.get_attribute("html", "data-theme") == "dark"
    page.reload()
    page.wait_for_selector("#content .kpis")
    assert page.get_attribute("html", "data-theme") == "dark"


def test_live_update_without_reload(page, home):  # noqa: F811
    page.goto(page.url.split("#")[0] + "#sessions")
    page.wait_for_selector("table.sessions")
    with open(home["trans_a"], "a") as f:
        f.write(
            json.dumps(
                tool_call("sid-alpha", "t9", "Grep", {"pattern": "LIVECHECK"}, 0)
            )
            + "\n"
        )
    page.wait_for_function(
        "document.querySelector('table.sessions tr[data-sid=sid-alpha]').textContent.includes('LIVECHECK')",
        timeout=10000,
    )
    row = page.text_content("table.sessions tr[data-sid=sid-alpha]")
    assert "Grep" in row and "LIVECHECK" in row, (
        "the row shows the new tool call without a page reload"
    )


def test_esc_in_search_clears_the_filter(page):
    page.goto(page.url.split("#")[0] + "#sessions")
    page.wait_for_selector("table.sessions")
    page.fill("#search", "callers")
    page.wait_for_selector(".chips")
    assert rows(page) == ["alpha-1"]
    page.focus("#search")
    page.keyboard.press("Escape")
    page.wait_for_function(
        "document.querySelectorAll('table.sessions tbody tr').length === 5"
    )
    assert page.input_value("#search") == ""
    assert page.query_selector(".chips") is None


def test_enter_on_a_button_does_not_open_the_drawer(page):
    page.goto(page.url.split("#")[0] + "#sessions")
    page.wait_for_selector("table.sessions")
    page.keyboard.press("j")
    page.focus("#theme")
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    assert page.get_attribute("html", "data-theme") == "dark", (
        "Enter pressed the button"
    )
    assert page.is_hidden("#drawer"), "Enter on a button must not open the row"


def test_terminal_button_shows_tooltip_and_toast(page):
    page.goto(page.url.split("#")[0] + "#sessions")
    page.click("table.sessions tr[data-sid=sid-delta]")
    page.wait_for_selector("#drawer [data-terminal]")
    page.hover("#drawer [data-terminal]")
    page.wait_for_selector("#tip:not([hidden])")
    assert "Terminal session" in page.text_content("#tip")
    # Playwright treats aria-disabled as disabled, but a person can still click it.
    page.click("#drawer [data-terminal]", force=True)
    page.wait_for_selector("#toast:not([hidden])")
    assert not page.is_hidden("#drawer")


def test_live_update_keeps_focus_and_selection(page, home):  # noqa: F811
    def append(pattern):
        with open(home["trans_a"], "a") as f:
            f.write(
                json.dumps(
                    tool_call("sid-alpha", pattern, "Grep", {"pattern": pattern}, 0)
                )
                + "\n"
            )

    page.focus(".kpi[data-filter=attention]")
    append("FOCUSCHECK")
    page.wait_for_function(
        "document.querySelector('.feed').textContent.includes('FOCUSCHECK')"
    )
    assert page.evaluate(
        "document.activeElement.matches('.kpi[data-filter=attention]')"
    )

    page.goto(page.url.split("#")[0] + "#sessions")
    page.click("table.sessions tr[data-sid=sid-alpha]")
    page.click("#drawer [data-tab=details]")
    page.evaluate(
        """() => { const r = document.createRange(); r.selectNodeContents(document.querySelector('#drawer .kv dd'));
                   getSelection().removeAllRanges(); getSelection().addRange(r); }"""
    )
    append("SELCHECK")
    page.wait_for_timeout(4000)
    assert page.evaluate("getSelection().toString()") == "/work/alpha", (
        "the selection survives"
    )
    row = "table.sessions tr[data-sid=sid-alpha]"
    assert "SELCHECK" not in page.text_content(row), (
        "the page waits while text is selected"
    )
    page.evaluate("getSelection().removeAllRanges()")
    page.wait_for_function(
        f"document.querySelector('{row}').textContent.includes('SELCHECK')",
        timeout=5000,
    )


def test_visible_tooltip_follows_a_live_update(page, home):  # noqa: F811
    page.goto(page.url.split("#")[0] + "#sessions")
    cell = "table.sessions tr[data-sid=sid-alpha] td:nth-child(3) .act"
    page.wait_for_selector(cell)
    page.hover(cell)
    page.wait_for_selector("#tip:not([hidden])")
    page.evaluate(
        """() => { window.tipHid = 0; new MutationObserver(() => { if (document.querySelector('#tip').hidden) window.tipHid++; })
                   .observe(document.querySelector('#tip'), { attributes: true, attributeFilter: ['hidden'] }); }"""
    )
    with open(home["trans_a"], "a") as f:
        f.write(
            json.dumps(tool_call("sid-alpha", "tt", "Grep", {"pattern": "TIPCHECK"}, 0))
            + "\n"
        )
    page.wait_for_function(
        "document.querySelector('#tip').textContent.includes('TIPCHECK')", timeout=10000
    )
    assert page.evaluate("window.tipHid") == 0, (
        "the tooltip must not blink on a live update"
    )


def test_subagent_drawer_parent_and_back(page):
    page.goto(page.url.split("#")[0] + "#sessions")
    page.click("table.sessions tr[data-sid=sid-alpha]")
    page.click("#drawer [data-tab=subagents]")
    page.click("#drawer tr[data-aid=eee]")
    page.wait_for_selector("#drawer [data-sub]")
    assert page.text_content("#drawer h3") == "Scan dates"
    page.click("#drawer [data-sub]")
    page.wait_for_function(
        "document.querySelector('#drawer h3').textContent === 'Check expiry rules'"
    )
    page.click("#drawer [data-back]")
    page.wait_for_selector("#drawer [data-tab=subagents].on")
    assert page.text_content("#drawer h3") == "alpha-1", (
        "Back returns to the tab you came from"
    )


def test_attention_open_button_sends_the_session(page):
    with page.expect_response("**/api/open") as resp:
        page.click(".panel.attn tr[data-sid=sid-beta] [data-open]")
    # The page reads the body only on an error, so Playwright cannot read this one.
    r = resp.value
    assert r.status == 200 and json.loads(r.request.post_data) == {
        "session_id": "sid-beta"
    }
    page.wait_for_selector("#toast:not([hidden])")
    assert "beta-1" in page.text_content("#toast")
    assert page.is_hidden("#drawer"), "the Open button must not also open the row"


def test_message_cell_keeps_the_closing_question_visible(page):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.evaluate(
        """() => { const a = S.agents.find(x => x.session_id === 'sid-beta');
                   a.last_message = 'Start. ' + 'The suite and the gates now run on the branch. '.repeat(3) + 'Want me to merge it?';
                   render(); }"""
    )
    cell = ".panel.attn tr[data-sid=sid-beta] .tail"
    box, end = page.evaluate(
        f"""() => {{ const t = document.querySelector('{cell}'); const r = document.createRange();
                    const text = t.querySelector('bdi').firstChild; r.setStart(text, text.length - 1); r.setEnd(text, text.length);
                    return [t.getBoundingClientRect().toJSON(), r.getBoundingClientRect().toJSON()]; }}"""
    )
    assert box["left"] <= end["left"] and end["right"] <= box["right"] + 1, (
        "the last character must sit inside the visible cell"
    )


@pytest.fixture
def sleeper():
    procs = []

    def make():
        p = subprocess.Popen(["sleep", "300"])
        procs.append(p)
        return p.pid

    yield make
    for p in procs:
        p.kill()
        p.wait()


def drop(folder, name, data):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data))


def install_marker(home):  # noqa: F811
    import hashlib

    sha = hashlib.sha256(
        (ROOT / "agentforeman" / "agentforeman_hook.py").read_bytes()
    ).hexdigest()
    drop(home["control"], "installed.json", {"installed_ms": 1, "hook_sha256": sha})


def test_approve_from_the_attention_table(page, home, sleeper):  # noqa: F811
    req = {
        "id": "rq1",
        "pid": sleeper(),
        "session_id": "sid-beta",
        "tool_name": "Bash",
        "tool_input": {"command": "sudo -n true", "description": "Check sudo"},
        "created_ms": 0,
    }
    drop(home["control"] / "requests", "rq1.json", req)
    page.wait_for_selector(".panel.attn tr[data-sid=sid-beta] [data-approve]")
    assert (
        page.text_content(".panel.attn tr[data-sid=sid-beta] .badge")
        == "Awaiting approval"
    )
    page.click(".panel.attn tr[data-sid=sid-beta] .cell-main")
    page.wait_for_selector("#drawer .approval [data-behavior=deny]")
    assert "sudo -n true" in page.text_content("#drawer .ap-cmd")
    page.keyboard.press("Escape")
    page.wait_for_selector("#drawer", state="hidden")
    page.click(".panel.attn tr[data-sid=sid-beta] [data-behavior=allow]")
    wait_for(lambda: (home["control"] / "decisions" / "rq1.json").exists())
    assert (
        json.loads((home["control"] / "decisions" / "rq1.json").read_text())["behavior"]
        == "allow"
    )
    assert page.is_hidden("#drawer"), "a button press must not also open the row"
    page.wait_for_function(
        "!document.querySelector('.panel.attn tr[data-sid=sid-beta] [data-approve]')",
        timeout=10000,
    )


def test_reply_box_keeps_the_draft_through_live_updates(page, home, sleeper):  # noqa: F811
    drop(
        home["control"] / "waiting",
        "sid-beta.json",
        {"pid": sleeper(), "session_id": "sid-beta"},
    )
    page.wait_for_selector(".panel.attn tr[data-sid=sid-beta] [data-reply-open]")
    page.click(".panel.attn tr[data-sid=sid-beta] [data-reply-open]")
    box = "#drawer textarea[data-reply=sid-beta]"
    page.wait_for_selector(box)
    page.click(box)
    page.keyboard.type("Yes, open the PR")
    with open(home["trans_a"], "a") as f:
        f.write(
            json.dumps(
                tool_call("sid-alpha", "dr1", "Grep", {"pattern": "DRAFTCHECK"}, 0)
            )
            + "\n"
        )
    page.wait_for_function(
        "document.querySelector('.feed').textContent.includes('DRAFTCHECK')",
        timeout=10000,
    )
    assert page.input_value(box) == "Yes, open the PR"
    assert page.evaluate(f"document.activeElement.matches('{box}')"), (
        "focus stays in the box"
    )
    page.keyboard.type(" now.")
    page.keyboard.press("Meta+Enter")
    wait_for(lambda: (home["control"] / "replies" / "sid-beta.json").exists())
    assert (
        json.loads((home["control"] / "replies" / "sid-beta.json").read_text())["text"]
        == "Yes, open the PR now."
    )
    page.wait_for_selector("#drawer .reply.sent")


def test_a_tab_open_across_a_restart_gets_the_new_token(page, home, sleeper):  # noqa: F811
    drop(
        home["control"] / "waiting",
        "sid-beta.json",
        {"pid": sleeper(), "session_id": "sid-beta"},
    )
    page.wait_for_selector(".panel.attn tr[data-sid=sid-beta] [data-reply-open]")
    page.evaluate("TOKEN = 'token-from-before-the-restart'")
    page.click(".panel.attn tr[data-sid=sid-beta] [data-reply-open]")
    page.keyboard.type("Sent after a restart.")
    page.keyboard.press("Meta+Enter")
    wait_for(lambda: (home["control"] / "replies" / "sid-beta.json").exists())
    assert page.text_content("#toast") == "Reply sent."
    assert page.evaluate("TOKEN") != "token-from-before-the-restart"
    stale = [e for e in page.errors if "status of 403" in e]
    assert len(stale) == 1, page.errors  # the one refused try with the old token
    page.errors.remove(stale[0])


def test_stop_asks_to_confirm_then_stops(page, home):  # noqa: F811
    install_marker(home)
    page.goto(page.url.split("#")[0] + "#subagents")
    stop = "#content tr[data-aid=aaa] [data-stop]"
    page.wait_for_selector(stop)
    assert page.text_content("#ctl-state") == "Controls on"
    page.click(stop)
    page.wait_for_function(
        f"document.querySelector('{stop}').textContent === 'Confirm stop'"
    )
    assert not (home["control"] / "stop" / "aaa.json").exists(), "one click only asks"
    page.click(stop)
    wait_for(lambda: (home["control"] / "stop" / "aaa.json").exists())
    page.wait_for_function(
        "document.querySelector('#content tr[data-aid=aaa] .badge').textContent === 'Stopping'",
        timeout=10000,
    )


def test_controls_off_points_to_the_claude_app(page):
    assert page.text_content("#ctl-state") == "Controls off"
    page.goto(page.url.split("#")[0] + "#subagents")
    page.wait_for_selector("#content tr[data-aid=aaa] .btn.danger[aria-disabled=true]")


def test_rows_show_the_end_of_the_last_paragraph(page):
    tail = page.evaluate(
        "lastPara('First part.\\n\\nSecond ' + 'word '.repeat(60) + 'Want me to merge?')"
    )
    assert tail.startswith("...") and tail.endswith("Want me to merge?")
    assert len(tail) <= 163


@pytest.mark.parametrize("width,height", [(1280, 800), (1440, 900), (1920, 1080)])
def test_layout_has_no_overflow(browser, portal, width, height):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    pg = ctx.new_page()
    try:
        pg.goto(signed(portal + "/"))
        for route in ("overview", "sessions", "subagents", "activity", "codex"):
            pg.goto(f"{portal}/#{route}")
            pg.reload()
            pg.wait_for_selector("#content .panel")
            assert pg.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            ), f"{route} scrolls sideways at {width}"
            clipped = pg.evaluate(
                """[...document.querySelectorAll('.badge, .btn, .kpi .v, .kpi .k, .nav a, .panel-h h2, th, .crumbs')]
                   .filter(e => e.offsetParent && e.scrollWidth > e.clientWidth + 1).map(e => e.textContent.trim())"""
            )
            assert not clipped, f"{route} at {width}: clipped {clipped}"
            # A cell that overflows by even a pixel draws an ellipsis after its badge.
            badge_cells = pg.evaluate(
                """[...document.querySelectorAll('td')].filter(td => td.offsetParent && td.querySelector('.badge')
                       && td.scrollWidth > td.clientWidth).map(td => td.textContent.trim())"""
            )
            assert not badge_cells, (
                f"{route} at {width}: badge cells overflow {badge_cells}"
            )
    finally:
        ctx.close()
