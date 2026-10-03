"""Browser checks of the first-run fixes: the empty page says what to do, the theme follows
the system, and the Overview says what the page is for."""

import pytest
from test_release import page_url, running

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def test_an_empty_page_says_what_to_do(browser, tmp_path):
    with running(
        "serve",
        "--claude-dir",
        str(tmp_path / "claude"),
        "--codex-dir",
        str(tmp_path / "codex"),
        "--control-dir",
        str(tmp_path / "ctl"),
        "--dry-open",
    ) as port:
        pg = browser.new_page()
        pg.goto(page_url(port))
        pg.wait_for_selector("#content")
        pg.wait_for_function(
            "document.querySelector('#content').textContent.includes('No Claude Code sessions')",
            timeout=10000,
        )
        text = pg.text_content("#content")
        assert "agentforeman demo" in text
        assert "No sessions match" not in text, (
            "no filter is on, so nothing failed to match"
        )
        pg.close()


@pytest.mark.parametrize(
    "scheme,label", [("dark", "Switch to light"), ("light", "Switch to dark")]
)
def test_the_theme_follows_the_system_on_first_load(browser, scheme, label):
    with running("demo") as port:
        ctx = browser.new_context(color_scheme=scheme)
        pg = ctx.new_page()
        pg.goto(page_url(port))
        pg.wait_for_selector("#content .kpis")
        lum = pg.evaluate(
            """() => {
              const c = getComputedStyle(document.body).backgroundColor.match(/\\d+/g).map(Number);
              return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
            }"""
        )
        assert (lum < 0.35) == (scheme == "dark"), lum
        assert pg.get_by_text(label, exact=True).count() >= 1
        ctx.close()


def test_the_overview_says_what_the_page_is_for(browser):
    with running("demo") as port:
        pg = browser.new_page()
        pg.goto(page_url(port))
        pg.wait_for_selector("#content .lede")
        assert len(pg.text_content("#content .lede").strip()) > 20
        assert "AgentForeman" in pg.title()
        pg.close()
