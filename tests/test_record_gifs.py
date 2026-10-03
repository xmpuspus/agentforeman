"""tools/record_gifs.py records every README scene from the real page on the demo sessions.

Each scene drives the page with the clicks and keys of the recording, so a renamed selector or
a changed demo session breaks this test before it breaks the README media.
"""

import importlib.util

import pytest
from test_acceptance import ROOT
from test_release import page_url, running

playwright = pytest.importorskip("playwright.sync_api")

SCENES = [
    "hero",
    "approve-similar",
    "steer",
    "teammates",
    "guard",
    "stop",
    "rules",
]


def load_recorder():
    spec = importlib.util.spec_from_file_location(
        "record_gifs", ROOT / "tools" / "record_gifs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_recorder_knows_every_scene():
    assert list(load_recorder().SCENES) == SCENES


def test_the_readme_media_fit_their_budgets():
    rec = load_recorder()
    gifs = list(rec.OUT.glob("*.gif"))
    for gif in gifs:
        # tools/record_showcase.py owns the showcase, and its test checks its own limits.
        if gif.stem == "showcase":
            continue
        budget = rec.SCENES[gif.stem][3]
        assert gif.stat().st_size <= budget, f"{gif.name}: over {budget} bytes"
    total = sum(p.stat().st_size for p in rec.OUT.glob("*"))
    assert total <= rec.TOTAL_BYTES, f"docs/media: {total} bytes"


@pytest.mark.parametrize("scene", SCENES)
def test_the_recorder_records_each_scene(scene, tmp_path):
    rec = load_recorder()
    if not rec.ffmpeg():
        pytest.skip("ffmpeg is not installed")
    gif = rec.record(scene, out=tmp_path)
    assert gif == tmp_path / f"{scene}.gif"
    data = gif.read_bytes()
    assert data[:4] == b"GIF8" and len(data) > 10_000, len(data)
    assert int.from_bytes(data[6:8], "little") == rec.GIF_WIDTH


# Subagent rows whose text the cell cuts off, such as a "Teammate · working" badge or the tool
# name of the last action.
CLIPPED = """(sel) => [...document.querySelectorAll(sel)]
  .filter((td) => td.scrollWidth > td.clientWidth + 1)
  .map((td) => td.textContent.trim())"""


def test_the_teammates_scene_shows_whole_cells_and_counts_working_teammates():
    rec = load_recorder()
    from agentforeman.demo import TEAM_LEAD

    with running("demo") as port, playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport=rec.VIEW)
        page.goto(page_url(port, "subagents"))
        lead = f"#content tr.group[data-sid='{TEAM_LEAD}']"
        # Both teammates of the lead work, as in the recording.
        page.wait_for_function(
            """(sid) => [...document.querySelectorAll(`#content tr[data-sid='${sid}'][data-aid] .badge`)]
              .filter((e) => e.textContent === "Teammate · working").length === 2""",
            arg=TEAM_LEAD,
            timeout=15000,
        )
        assert "· 2 working" in page.inner_text(lead), page.inner_text(lead)
        clipped = page.evaluate(CLIPPED, "#content tr[data-aid] > td")
        assert not clipped, f"cut off on the Subagents page: {clipped}"
        # The first click on Stop asks to confirm, and the wider button still fits.
        page.click("#content [data-stop='demo-gateway']")
        page.wait_for_selector("#content [data-stop='demo-gateway'].on")
        clipped = page.evaluate(CLIPPED, "#content tr[data-aid] > td")
        assert not clipped, f"cut off while Stop asks to confirm: {clipped}"
        page.click("#content tr[data-aid='demo-gateway'] td:nth-child(2)")
        page.click("#drawer [data-back]")
        page.click("#drawer [data-tab=subagents]")
        page.wait_for_selector("#drawer tr[data-aid]")
        clipped = page.evaluate(CLIPPED, "#drawer tr[data-aid] > td")
        assert not clipped, f"cut off in the session panel: {clipped}"
        browser.close()
