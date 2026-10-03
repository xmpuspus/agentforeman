"""Browser tests for the second foreman release, driven through the page the way a person uses it.

Selectors that the page must keep:
- The approval card in the session panel shows an edit's diff in `.ap-diff`.
- `[data-approve-similar]` shows when other waiting calls share the rule key. Its text holds
  "all N", where N counts this call too. The first click asks to confirm. The second click
  answers all N.
- `[data-always-allow]` on the approval card adds the card's rule key as a rule.
- The Rules page (`#rules`) holds a switch `[data-rules-enabled]`, off by default.
- The Overview shows a `.shared-files` panel while two live sessions edit one file.
"""

import json

import pytest
from test_acceptance import enc, home, wait_for  # noqa: F401
from test_foreman_approvals import request  # noqa: F401
from test_foreman_core import append, call, now_ms  # noqa: F401
from test_ui_e2e import browser, page, portal, sleeper  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")


def open_attention_row(page, sid):  # noqa: F811
    page.wait_for_selector(f".panel.attn tr[data-sid={sid}] [data-approve]")
    page.click(f".panel.attn tr[data-sid={sid}] .cell-main")
    page.wait_for_selector("#drawer .approval")


def test_the_card_shows_the_diff_of_an_edit(page, home, sleeper):  # noqa: F811
    inp = {
        "file_path": "/work/beta/app.py",
        "old_string": "retries = 1\n",
        "new_string": "retries = 5\n",
    }
    request(home["control"], "ue1", sleeper(), "sid-beta", "Edit", inp)
    open_attention_row(page, "sid-beta")
    page.wait_for_selector("#drawer .ap-diff")
    diff = page.text_content("#drawer .ap-diff")
    assert "-retries = 1" in diff and "+retries = 5" in diff


def test_approve_similar_asks_then_answers_all(page, home, sleeper):  # noqa: F811
    ctl = home["control"]
    request(
        ctl,
        "us1",
        sleeper(),
        "sid-beta",
        "Bash",
        {"command": "npm test -- --shard=1"},
        now_ms() - 2000,
    )
    request(
        ctl,
        "us2",
        sleeper(),
        "sid-gamma",
        "Bash",
        {"command": "npm test -- --shard=2"},
        now_ms() - 1000,
    )
    open_attention_row(page, "sid-beta")
    btn = "#drawer [data-approve-similar]"
    page.wait_for_selector(btn)
    assert "all 2" in page.text_content(btn)
    page.click(btn)
    page.wait_for_function(
        f"document.querySelector('{btn}').textContent.includes('Confirm')"
    )
    assert not (ctl / "decisions" / "us1.json").exists(), "one click only asks"
    page.click(btn)
    wait_for(
        lambda: (
            (ctl / "decisions" / "us1.json").exists()
            and (ctl / "decisions" / "us2.json").exists()
        )
    )


def test_always_allow_adds_the_rule(page, home, sleeper):  # noqa: F811
    request(
        home["control"], "ua1", sleeper(), "sid-beta", "Bash", {"command": "npm test"}
    )
    open_attention_row(page, "sid-beta")
    page.wait_for_selector("#drawer [data-always-allow]")
    assert "npm test" in page.text_content("#drawer [data-always-allow]")
    page.click("#drawer [data-always-allow]")
    path = home["control"] / "rules.json"
    wait_for(lambda: path.exists() and json.loads(path.read_text())["items"])
    assert [i["rule"] for i in json.loads(path.read_text())["items"]] == [
        "Bash(npm test)"
    ]


def test_the_rules_page_turns_rules_on(page, home):  # noqa: F811
    page.goto(page.url.split("#")[0] + "#rules")
    switch = "#content [data-rules-enabled]"
    page.wait_for_selector(switch)
    assert (
        not (home["control"] / "rules.json").exists()
        or not json.loads((home["control"] / "rules.json").read_text())["enabled"]
    )
    page.click(switch)
    path = home["control"] / "rules.json"
    wait_for(lambda: path.exists() and json.loads(path.read_text())["enabled"])


def test_the_overview_shows_files_that_two_sessions_edit(page, home):  # noqa: F811
    eps = home["claude"] / "projects" / enc("/work/epsilon") / "sid-eps-new.jsonl"
    edit = {"file_path": "/work/shared/app.py", "old_string": "a", "new_string": "b"}
    append(home["trans_a"], call("sid-alpha", "us9", "Edit", edit, -20))
    append(eps, call("sid-eps", "us8", "Edit", edit, -10))
    page.wait_for_selector("#content .shared-files", timeout=10000)
    text = page.text_content("#content .shared-files")
    assert "/work/shared/app.py" in text and "alpha-1" in text and "eps-1" in text
