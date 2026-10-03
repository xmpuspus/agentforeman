"""The demo must show every foreman feature, and the README media must stay small and current.

The README GIFs record the demo, so the demo carries one clear example of each feature:
a diff card, two calls that approve together, split-pane teammates, a shared file, one
repeating session, and a rule suggestion. The demo also answers a steer note and a stop,
the way the real hooks do.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_release import ROOT, get, running, token  # noqa: F401

MEDIA = ROOT / "docs" / "media"


def post(port, path, body):
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Foreman-Token": token(port)},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


def wait(check, timeout=15):
    import time

    end = time.time() + timeout
    while time.time() < end:
        got = check()
        if got:
            return got
        time.sleep(0.3)
    raise AssertionError(f"condition not met in {timeout}s")


def test_the_demo_shows_every_foreman_feature():
    with running("demo") as port:
        st = json.loads(get(port, "/api/state"))
        agents = st["agents"]
        approvals = [a["approval"] for a in agents if a.get("approval")]
        assert any(ap.get("diff") for ap in approvals), "a card with a diff"
        keys = [ap["rule_key"] for ap in approvals]
        assert any(keys.count(k) >= 2 for k in keys), "two calls that approve together"
        assert any(ap["similar"] >= 1 for ap in approvals)
        mates = [
            s for a in agents for s in a["subagents"] if s.get("backend") == "tmux"
        ]
        assert len(mates) >= 2, "split-pane teammates"
        shared = [f["path"] for a in agents for f in a.get("shared_files") or []]
        assert any(shared.count(p) >= 2 for p in shared), (
            "one file that two sessions edit"
        )
        assert sum(1 for a in agents if a.get("repeating")) == 1, (
            "exactly one repeating session"
        )
        assert st["rules"]["suggestions"], "a rule suggestion"
        assert st["controls_installed"] and st["controls_installed"]["current"]


def test_the_demo_answers_a_steer_note_and_a_stop():
    with running("demo") as port:
        st = json.loads(get(port, "/api/state"))
        busy = [a for a in st["agents"] if a["status"] == "working"]
        assert len(busy) >= 2
        steered, stopped = busy[0]["session_id"], busy[1]["session_id"]
        assert (
            post(
                port,
                "/api/steer",
                {"session_id": steered, "text": "Use the staging data."},
            )
            == 200
        )

        def delivered():
            a = next(
                x
                for x in json.loads(get(port, "/api/state"))["agents"]
                if x["session_id"] == steered
            )
            return a["note"]["delivered_ms"]

        wait(delivered)
        assert post(port, "/api/stop", {"session_id": stopped}) == 200

        def ended():
            a = next(
                x
                for x in json.loads(get(port, "/api/state"))["agents"]
                if x["session_id"] == stopped
            )
            return a["status"] != "working" and not a["stop_requested"]

        wait(ended)


def gif_width(path):
    data = path.read_bytes()[:10]
    assert data[:6] in (b"GIF87a", b"GIF89a"), path.name
    return int.from_bytes(data[6:8], "little")


# PyPI shows the README too, and it cannot follow a relative image path.
RAW = "https://raw.githubusercontent.com/xmpuspus/agentforeman/main/"
SHOWCASE = MEDIA / "showcase.gif"


def readme_images():
    return re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", (ROOT / "README.md").read_text())


def test_the_readme_leads_with_the_showcase_and_every_image_exists():
    readme = (ROOT / "README.md").read_text().splitlines()
    assert any(RAW + "docs/media/showcase.gif" in line for line in readme[:15]), (
        "the showcase sits at the top"
    )
    images = readme_images()
    for src in images:
        assert src.startswith(RAW), f"{src}: PyPI cannot show a relative path"
        assert (ROOT / src.removeprefix(RAW)).is_file(), f"{src} is missing"
    gifs = sorted(MEDIA.glob("*.gif"))
    assert len(gifs) >= 7, "the showcase, the hero, and one clip per main feature"
    for gif in gifs:
        assert f"{RAW}docs/media/{gif.name}" in images, (
            f"{gif.name} is not in the README"
        )


def test_every_gif_fits_the_readme_width():
    for gif in MEDIA.glob("*.gif"):
        if gif != SHOWCASE:
            assert 800 <= gif_width(gif) <= 1000, f"{gif.name}: {gif_width(gif)} px"


def test_the_showcase_fits_a_linkedin_post():
    head = SHOWCASE.read_bytes()[:10]
    assert gif_width(SHOWCASE) == 1080
    assert int.from_bytes(head[8:10], "little") == 1080, "the showcase is square"
    assert SHOWCASE.stat().st_size <= 5_000_000, "LinkedIn takes a GIF up to 5 MB"
    if shutil.which("ffprobe"):
        frames = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0"]
            + [
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "csv=p=0",
                str(SHOWCASE),
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert int(frames) <= 500, f"LinkedIn takes up to 500 frames, not {frames}"


def test_the_showcase_shows_every_feature_clip():
    showcase = (ROOT / "tools" / "record_showcase.py").read_text()
    recorder = (ROOT / "tools" / "record_gifs.py").read_text()
    scenes = re.search(r"SCENES = \{(.*?)\n\}", recorder, re.S).group(1)
    for scene in re.findall(r'^    "([a-z-]+)":', scenes, re.M):
        assert f'("{scene}",' in showcase, f"the showcase skips {scene}"


@pytest.mark.skipif(
    not shutil.which("ffprobe"), reason="needs ffprobe (brew install ffmpeg)"
)
def test_every_gif_is_short():
    for gif in MEDIA.glob("*.gif"):
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(gif),
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        limit = {"hero.gif": 15, "showcase.gif": 75}.get(gif.name, 25)
        assert float(out) <= limit, f"{gif.name}: {out} s, over {limit} s"


def test_the_gif_recorder_names_every_readme_clip():
    recorder = (ROOT / "tools" / "record_gifs.py").read_text()
    for gif in MEDIA.glob("*.gif"):
        if gif == SHOWCASE:
            assert (ROOT / "tools" / "record_showcase.py").is_file()
        else:
            assert gif.stem in recorder, (
                f"tools/record_gifs.py cannot record {gif.name} again"
            )


def test_the_media_folder_holds_only_what_the_readme_uses():
    used = {Path(src).name for src in readme_images()}
    extra = [p.name for p in MEDIA.glob("*") if p.name not in used]
    assert not extra, f"unused media: {extra}"
