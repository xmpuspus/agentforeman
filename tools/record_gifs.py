"""Records the README GIFs from the real page running on the demo sessions.

Run: python3 tools/record_gifs.py [scene ...]
Needs Playwright with Chromium and ffmpeg. Writes docs/media/<scene>.gif, 960 px wide.
With no scene, it records them all, then checks that docs/media stays under 10 MB. tests/test_record_gifs.py records every scene into a
temporary folder. Each scene gets fresh demo data, so every run records the same story.
"""

import base64
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentforeman.demo import Home, Writer, start_server  # noqa: E402
from agentforeman.server import load_token, sign_in_url  # noqa: E402

OUT = ROOT / "docs" / "media"
# The GIF scales this view down to 960 px, so 13 px text stays readable in the README.
VIEW = {"width": 1120, "height": 720}
GIF_WIDTH = 960
# A process started outside a login shell often lacks Homebrew on PATH, so look there too.
FFMPEG_PATHS = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]

# The headless browser draws no pointer, so a dot shows where each click lands.
CURSOR = """
addEventListener('DOMContentLoaded', () => {
  const c = document.createElement('div');
  c.style.cssText = 'position:fixed;left:-40px;top:-40px;width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:50%;' +
    'background:rgba(24,24,27,.28);border:2px solid #18181b;box-shadow:0 0 0 2px #fff;z-index:2147483647;pointer-events:none;transition:transform .1s';
  document.body.appendChild(c);
  addEventListener('mousemove', (e) => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', () => (c.style.transform = 'scale(.55)'), true);
  addEventListener('mouseup', () => (c.style.transform = 'scale(1)'), true);
});
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Pointer:
    """Moves the mouse in small steps, so the recording shows the pointer travel."""

    def __init__(self, page):
        self.page = page
        view = page.viewport_size
        self.x, self.y = view["width"] / 2, view["height"] / 2

    def box(self, selector):
        # Each state update rebuilds the page, so an element can be gone for a moment.
        for _ in range(20):
            el = self.page.locator(selector).first
            try:
                el.scroll_into_view_if_needed(timeout=10000)
                box = el.bounding_box(timeout=10000)
            except PlaywrightError:
                box = None
            if box:
                return box
            self.page.wait_for_timeout(50)
        raise RuntimeError(f"{selector} has no box")

    def to(self, selector, dx=0.5, dy=0.5, ms=450):
        box = self.box(selector)
        x, y = box["x"] + box["width"] * dx, box["y"] + box["height"] * dy
        # A second click on the same button must land inside its 4 s confirm window.
        if abs(x - self.x) < 3 and abs(y - self.y) < 3:
            return
        steps = max(8, ms // 16)
        for i in range(1, steps + 1):
            self.page.mouse.move(
                self.x + (x - self.x) * i / steps, self.y + (y - self.y) * i / steps
            )
            self.page.wait_for_timeout(16)
        self.x, self.y = x, y

    def click(self, selector, dx=0.5, dy=0.5, pause=900):
        self.to(selector, dx, dy)
        self.page.mouse.down()
        self.page.wait_for_timeout(90)
        self.page.mouse.up()
        self.page.wait_for_timeout(pause)


def go(page, route):
    """Shows a page of the app. The app is open already, so only the hash changes: a reload
    would redraw every pixel, and the GIF would grow."""
    page.evaluate("(r) => { location.hash = r; }", route)
    page.wait_for_selector("#content .panel, #content .kpis")


def open_session(p, sid, panel="#content", pause=900):
    """Opens a session's panel from its row."""
    p.click(f"{panel} tr[data-sid='{sid}'] td:nth-child(2)", dx=0.35, pause=200)
    p.page.wait_for_selector(f"#drawer:not([hidden]) [data-copy='{sid}']")
    p.page.wait_for_timeout(pause)


def confirm(page, p, selector, gone):
    """Clicks a button that asks to confirm, then clicks it again inside its 4 s window.
    Tries once more when `gone` stays, so a late second click never ends the scene."""
    for _ in range(2):
        p.click(selector, pause=200)
        page.wait_for_selector(f"{selector}.on", timeout=5000)
        page.wait_for_timeout(700)
        p.click(selector, pause=200)
        try:
            page.wait_for_selector(gone, state="detached", timeout=5000)
            return
        except PlaywrightError:
            continue
    raise RuntimeError(f"{selector} did not take the confirm click")


def scene_hero(page, base, p):
    """The Overview, then one approval card with its diff, then Approve."""
    go(page, "overview")
    page.wait_for_timeout(1000)
    p.to(".kpi[data-filter=attention]")
    page.wait_for_timeout(700)
    p.to(".panel.attn tr[data-sid='demo-explainer'] td:nth-child(4)", dx=0.3)
    page.wait_for_timeout(600)
    open_session(p, "demo-explainer", ".panel.attn", pause=700)
    p.to("#drawer .ap-diff", dx=0.45, dy=0.6)
    page.wait_for_timeout(2000)
    p.click("#drawer .approval [data-behavior=allow]", pause=300)
    page.wait_for_selector("#drawer .approval", state="detached", timeout=10000)
    page.wait_for_timeout(1800)


def scene_approve_similar(page, base, p):
    """Two sessions wait on `make eval`. Approve all 2 asks to confirm, then answers both."""
    go(page, "overview")
    page.wait_for_timeout(800)
    for sid in ("demo-features", "demo-scam"):
        p.to(f".panel.attn tr[data-sid='{sid}'] td:nth-child(4)", dx=0.25)
        page.wait_for_timeout(600)
    open_session(p, "demo-scam", ".panel.attn")
    confirm(page, p, "#drawer [data-approve-similar]", "#drawer .approval")
    page.wait_for_timeout(900)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    p.to(".panel.attn tr[data-sid='demo-scam'] td:nth-child(4)", dx=0.25)
    # Both sessions run their evals, then come back with their result.
    for sid in ("demo-features", "demo-scam"):
        page.wait_for_selector(
            f".panel.attn tr[data-sid='{sid}'] .badge.b-input", timeout=10000
        )
    page.wait_for_timeout(2200)


def scene_steer(page, base, p):
    """A note to a running session, then the note arrives at its next tool call."""
    go(page, "overview")
    page.wait_for_timeout(700)
    open_session(p, "demo-churn")
    p.click("#drawer [data-steer]", pause=200)
    page.keyboard.type("Use the staging data for March 9.", delay=45)
    page.wait_for_timeout(400)
    p.click("#drawer [data-steer-send]", pause=300)
    page.wait_for_selector(
        "#drawer .steer.sent .badge:text('Delivered')", timeout=15000
    )
    p.to("#drawer .steer.sent", dx=0.3)
    page.wait_for_timeout(1200)
    # The call that the note changed. Newer calls push it down the timeline.
    p.to("#drawer .timeline li:has-text('staging data')", dx=0.4)
    page.wait_for_timeout(1300)
    # The reply has no tooltip, so the pointer can rest there and hide nothing.
    p.to("#drawer .msg:nth-of-type(2)", dx=0.85, dy=0.5)
    page.wait_for_timeout(1500)


def scene_teammates(page, base, p):
    """Split-pane teammates show under their lead."""
    go(page, "subagents")
    page.wait_for_timeout(1000)
    p.to("#content tr.group[data-sid='demo-landing']", dx=0.2)
    page.wait_for_timeout(900)
    for aid in ("demo-gateway", "demo-redaction"):
        p.to(f"#content tr[data-aid='{aid}'] td:nth-child(2)", dx=0.3)
        page.wait_for_timeout(800)
    p.click("#content tr[data-aid='demo-gateway'] td:nth-child(2)", dx=0.3, pause=200)
    page.wait_for_selector("#drawer:not([hidden]) [data-back]")
    page.wait_for_timeout(2000)
    p.click("#drawer [data-back]", pause=200)
    page.wait_for_selector("#drawer:not([hidden]) [data-copy='demo-landing']")
    page.wait_for_timeout(600)
    p.click("#drawer [data-tab=subagents]", pause=2200)


def scene_guard(page, base, p):
    """Two sessions edited features/schema.sql. The second one got the warning before its edit."""
    go(page, "overview")
    page.wait_for_timeout(800)
    p.to("#content .shared-files tbody tr td:first-child", dx=0.4)
    page.wait_for_timeout(1400)
    p.to("#content .shared-files tbody tr td:nth-child(2)", dx=0.4)
    page.wait_for_timeout(1000)
    open_session(p, "demo-features", ".panel.attn", pause=600)
    p.to("#drawer .d-flags", dx=0.2)
    page.wait_for_timeout(1200)
    p.to("#drawer .msg:nth-of-type(2)", dx=0.5, dy=0.6)
    page.wait_for_timeout(2000)
    # Newest first: make eval, the quick training run, the edit, then the Read after the warning.
    p.to("#drawer .timeline li:nth-child(4)", dx=0.4)
    page.wait_for_timeout(1200)


def scene_stop(page, base, p):
    """Stop a whole session, with the confirm step."""
    go(page, "overview")
    page.wait_for_timeout(700)
    open_session(p, "demo-noc", pause=600)
    p.to("#drawer .d-flags", dx=0.2)
    page.wait_for_timeout(1300)
    stop = "#drawer [data-stop-session]"
    confirm(page, p, stop, stop)
    page.wait_for_selector("#drawer .d-title .badge.b-idle", timeout=15000)
    p.to("#drawer .d-title", dx=0.15)
    page.wait_for_timeout(2000)


def scene_rules(page, base, p):
    """The Rules page: a suggested rule, add it, then turn rules on."""
    go(page, "overview")
    page.wait_for_timeout(600)
    p.click("#nav a[data-route=rules]", pause=900)
    add = "#content [data-rule-add='Bash(make eval)']"
    p.to(add, dx=0.5)
    page.wait_for_timeout(700)
    p.click(add, pause=200)
    page.wait_for_selector("#content [data-rule-remove='Bash(make eval)']")
    page.wait_for_timeout(1000)
    p.click("#content [data-rules-enabled]", pause=200)
    page.wait_for_selector("#content [data-rules-enabled].on")
    page.wait_for_timeout(1200)
    p.to("#content .panel:nth-of-type(3) tbody tr td:nth-child(4)", dx=0.3)
    page.wait_for_timeout(1600)


# Scene name -> (scene, seconds until care-kb asks its question, longest GIF in seconds,
# largest GIF in bytes). The README shows the hero first, so it is the shortest and smallest.
SCENES = {
    "hero": (scene_hero, None, 15, 2_000_000),
    "approve-similar": (scene_approve_similar, None, 25, 3_000_000),
    "steer": (scene_steer, None, 25, 3_000_000),
    "teammates": (scene_teammates, None, 25, 3_000_000),
    "guard": (scene_guard, None, 25, 3_000_000),
    "stop": (scene_stop, None, 25, 3_000_000),
    "rules": (scene_rules, None, 25, 3_000_000),
}
# All README media together.
TOTAL_BYTES = 10_000_000


def ffmpeg():
    return shutil.which("ffmpeg") or next(
        (p for p in FFMPEG_PATHS if Path(p).exists()), None
    )


class Screencast:
    """Lossless frames of the page from Chromium, each with the time it was drawn.

    A recorded video sharpens each change over several frames, and every one of those frames
    adds to the GIF. These frames change only where the page changed, so the GIF stays small.
    Chromium sends a frame only when the page draws, so each frame lasts until the next one.
    """

    def __init__(self, page):
        self.page = page
        self.cdp = page.context.new_cdp_session(page)
        self.frames = []
        self.end = None
        self.cdp.on("Page.screencastFrame", self._frame)

    def _frame(self, event):
        self.frames.append((event["metadata"]["timestamp"], event["data"]))
        # Chromium sends the next frame only after this one is acknowledged.
        self.cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})

    def start(self):
        view = self.page.viewport_size
        self.cdp.send(
            "Page.startScreencast",
            {
                "format": "png",
                "everyNthFrame": 1,
                "maxWidth": view["width"],
                "maxHeight": view["height"],
            },
        )
        # A small pointer move makes the page draw, so the first frame arrives at once.
        self.page.mouse.move(view["width"] / 2 + 1, view["height"] / 2)

    def stop(self):
        self.cdp.send("Page.stopScreencast")
        self.end = time.time()

    def seconds(self):
        return self.end - self.frames[0][0]

    def write(self, folder):
        """Writes the frames and an ffmpeg concat list that keeps their timing."""
        lines = []
        for i, (at, data) in enumerate(self.frames):
            frame = folder / f"{i:05d}.png"
            frame.write_bytes(base64.b64decode(data))
            until = self.frames[i + 1][0] if i + 1 < len(self.frames) else self.end
            lines += [f"file '{frame}'", f"duration {max(until - at, 0.001):.3f}"]
        # The concat format needs the last file twice, or it drops the last duration.
        lines.append(f"file '{folder / f'{len(self.frames) - 1:05d}.png'}'")
        listing = folder / "frames.txt"
        listing.write_text("\n".join(lines) + "\n")
        return listing


def to_gif(listing, out):
    vf = (
        f"fps=12,scale={GIF_WIDTH}:-1:flags=lanczos,split[a][b];"
        "[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle"
    )
    subprocess.run(
        [
            ffmpeg() or "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-vf",
            vf,
            str(out),
        ],
        check=True,
    )


def capture(name, view=VIEW):
    """Runs one scene on fresh demo data in a browser of the given size, and returns its
    Screencast. tools/record_showcase.py uses it too."""
    fn, finish_after = SCENES[name][:2]
    home = Home()
    writer = server = None
    # Everything that starts a process sits inside the try, so a failed start still stops
    # the demo's teammate processes.
    try:
        home.build()
        writer = Writer(home, finish_after)
        port = free_port()
        token = load_token(home.control)
        server = start_server(home, port)
        base = f"http://127.0.0.1:{port}"
        ready = urllib.request.Request(
            base + "/api/state", headers={"X-Foreman-Token": token}
        )
        for _ in range(120):
            try:
                urllib.request.urlopen(ready, timeout=2)
                break
            except OSError:
                time.sleep(0.25)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chromium")
            # Reduced motion stops the pulse of the status dots, so the page draws only
            # when something changes.
            ctx = browser.new_context(viewport=view, reduced_motion="reduce")
            ctx.add_init_script(CURSOR)
            page = ctx.new_page()
            # The sign-in link sets the cookie, then the page loads without the token in its URL.
            page.goto(sign_in_url(port, token) + "#overview")
            page.wait_for_selector("#content .kpis")
            writer.start()
            page.wait_for_timeout(1000)
            cast = Screencast(page)
            cast.start()
            fn(page, base, Pointer(page))
            cast.stop()
            ctx.close()
            browser.close()
        return cast
    finally:
        if writer:
            writer.stop.set()
            if writer.is_alive():
                writer.join(5)
        if server:
            server.terminate()
            server.wait(10)
        home.close()
        shutil.rmtree(home.root, ignore_errors=True)


def record(name, out=OUT):
    """Records one scene into out/<name>.gif and returns that path. Raises when the scene
    runs longer than its limit or the GIF is larger than its budget, so no clip gets cut."""
    limit, budget = SCENES[name][2:]
    cast = capture(name)
    if cast.seconds() > limit:
        raise RuntimeError(f"{name} runs {cast.seconds():.1f} s, over {limit} s")
    tmp = Path(tempfile.mkdtemp(prefix="agentforeman-rec-"))
    try:
        gif = Path(out) / f"{name}.gif"
        gif.parent.mkdir(parents=True, exist_ok=True)
        to_gif(cast.write(tmp), gif)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    size = gif.stat().st_size
    print(f"{name}: {cast.seconds():.1f} s, {size / 1e6:.2f} MB", flush=True)
    if size > budget:
        raise RuntimeError(f"{name}.gif is {size} bytes, over {budget}")
    return gif


if __name__ == "__main__":
    for scene in sys.argv[1:] or SCENES:
        record(scene)
    total = sum(g.stat().st_size for g in OUT.glob("*"))
    print(f"docs/media: {total / 1e6:.2f} MB", flush=True)
    if total > TOTAL_BYTES:
        sys.exit(f"docs/media holds {total} bytes, over {TOTAL_BYTES}")
