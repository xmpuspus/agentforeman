"""Records docs/media/showcase.gif: one square GIF of every feature, sized for a LinkedIn post.

Run: python3 tools/record_showcase.py [--mp4 PATH]
Needs Playwright with Chromium, Pillow, and ffmpeg. Each scene of tools/record_gifs.py runs on
fresh demo data under a caption that names the feature. A title card opens the GIF, and an
install card ends it. The GIF is 1080 x 1080, at most 500 frames and 5 MB, so LinkedIn takes
it as an image. The MP4 copy goes to tmp/showcase.mp4 unless --mp4 names another path.
"""

import argparse
import base64
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import record_gifs as rec  # noqa: E402

OUT = ROOT / "docs" / "media" / "showcase.gif"
MP4 = ROOT / "tmp" / "showcase.mp4"
SIZE = 1080
BAND = 200
# The page draws at the README width, and the frame scales it into the area under the band.
VIEW = {"width": 1120, "height": 912}
APP = (SIZE, round(VIEW["height"] * SIZE / VIEW["width"]))

# Scene of tools/record_gifs.py -> the caption above it. Captions name what the page does.
STORY = [
    ("hero", "See which sessions wait for approval.", "Approve with the full diff."),
    ("approve-similar", "Approve every matching call", "with one click."),
    ("rules", "Turn approvals into allow rules.", "Off until you switch them on."),
    ("steer", "Send a note to a running session.", "It arrives at the next tool call."),
    ("guard", "Warn when two sessions", "edit the same file."),
    ("teammates", "Show subagents and teammates", "in their own tmux panes."),
    ("stop", "Stop one session or subagent.", "The others keep running."),
]
INTRO_SECONDS = 2.5
OUTRO_SECONDS = 4.0
# The scenes play this many times faster than they ran, so the whole GIF takes about a minute.
SPEED = 1.6
# Each scene keeps its first frame this long, so the caption can be read.
FIRST_SECONDS = 0.9
# Waits longer than this get cut down, so the GIF keeps moving.
HOLD_SECONDS = 1.0
FPS = 10
MAX_FRAMES = 500
MAX_BYTES = 5_000_000

FONT = "-apple-system, 'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO = "'SF Mono', Menlo, monospace"


def logo(px):
    svg = (ROOT / "docs" / "media" / "logo.svg").read_text()
    return svg.replace('width="72" height="72"', f'width="{px}" height="{px}"', 1)


def page_html(body, height):
    return f"""<!doctype html><html><head><style>
html, body {{ margin: 0; width: {SIZE}px; height: {height}px; background: #18181b;
  font-family: {FONT}; color: #fafafa; -webkit-font-smoothing: antialiased; }}
</style></head><body>{body}</body></html>"""


def band_html(line1, line2):
    return page_html(
        f"""<div style="height:{BAND}px; box-sizing:border-box; padding:30px 56px 0;
  border-bottom:1px solid #3f3f46">
  <div style="display:flex; align-items:center; gap:12px; font-size:22px; color:#a1a1aa;
    font-weight:600; letter-spacing:.2px">{logo(30)}AgentForeman</div>
  <div style="margin-top:18px; font-size:44px; line-height:52px; font-weight:700;
    letter-spacing:-.6px">{line1} <span style="color:#34d399">{line2}</span></div>
</div>""",
        BAND,
    )


def intro_html():
    return page_html(
        f"""<div style="height:{SIZE}px; display:flex; flex-direction:column; justify-content:center;
  padding:0 96px; box-sizing:border-box">
  {logo(168)}
  <div style="margin-top:44px; font-size:92px; font-weight:800; letter-spacing:-2.5px">
    AgentForeman</div>
  <div style="margin-top:22px; font-size:46px; line-height:58px; font-weight:600; color:#d4d4d8;
    letter-spacing:-.6px">Approve, steer, and stop every Claude Code agent from
    <span style="color:#34d399">one page</span>.</div>
  <div style="margin-top:48px; font-size:28px; color:#a1a1aa">
    For the sessions you already run in VS Code and the terminal.</div>
</div>""",
        SIZE,
    )


def outro_html():
    return page_html(
        f"""<div style="height:{SIZE}px; display:flex; flex-direction:column; justify-content:center;
  padding:0 96px; box-sizing:border-box">
  <div style="display:flex; align-items:center; gap:24px; font-size:56px; font-weight:800;
    letter-spacing:-1.4px">{logo(96)}AgentForeman</div>
  <div style="margin-top:64px; font-family:{MONO}; font-size:44px; padding:30px 36px;
    border-radius:16px; background:#09090b; border:1px solid #3f3f46">
    <span style="color:#71717a">$</span> pipx install <span style="color:#34d399">agentforeman</span></div>
  <div style="margin-top:56px; font-size:34px; line-height:52px; color:#d4d4d8">
    Runs on your Mac. Only the Python standard library.<br>Free and open source, MIT license.</div>
  <div style="margin-top:44px; font-size:34px; font-weight:600; color:#fafafa">
    github.com/xmpuspus/agentforeman</div>
</div>""",
        SIZE,
    )


def render_cards():
    """Draws the title card, the install card, and one caption band per scene."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chromium")
        page = browser.new_page(viewport={"width": SIZE, "height": SIZE})

        def shot(html, height):
            page.set_viewport_size({"width": SIZE, "height": height})
            page.set_content(html)
            return Image.open(io.BytesIO(page.screenshot())).convert("RGB")

        cards = {
            "intro": shot(intro_html(), SIZE),
            "outro": shot(outro_html(), SIZE),
        }
        for name, line1, line2 in STORY:
            cards[name] = shot(band_html(line1, line2), BAND)
        browser.close()
    return cards


def timeline(cast):
    """Samples the screencast at FPS of play time, keeps a frame only when the page changed,
    and cuts long waits. Returns (png data, seconds of play) pairs."""
    frames, end = cast.frames, cast.end
    start = frames[0][0]
    picks, i, t = [], 0, start
    while t < end:
        while i + 1 < len(frames) and frames[i + 1][0] <= t:
            i += 1
        if picks and picks[-1][0] == i:
            picks[-1][1] += 1 / FPS
        else:
            picks.append([i, 1 / FPS])
        t += SPEED / FPS
    out = [(frames[i][1], min(sec, HOLD_SECONDS)) for i, sec in picks]
    out[0] = (out[0][0], max(out[0][1], FIRST_SECONDS))
    return out


def compose(band, png):
    shot = Image.open(io.BytesIO(base64.b64decode(png))).convert("RGB")
    frame = Image.new("RGB", (SIZE, SIZE))
    frame.paste(band, (0, 0))
    frame.paste(shot.resize(APP, Image.LANCZOS), (0, BAND))
    return frame


def encode(listing, gif, mp4):
    ffmpeg = rec.ffmpeg() or "ffmpeg"
    src = ["-f", "concat", "-safe", "0", "-i", str(listing)]
    vf = (
        f"fps={FPS},mpdecimate,split[a][b];"
        "[a]palettegen=max_colors=96:stats_mode=diff[p];"
        "[b][p]paletteuse=dither=none:diff_mode=rectangle"
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            *src,
            "-vf",
            vf,
            "-fps_mode",
            "vfr",
            str(gif),
        ],
        check=True,
    )
    mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", *src, "-vf", "fps=30,format=yuv420p"]
        + ["-c:v", "libx264", "-crf", "20", "-movflags", "+faststart", str(mp4)],
        check=True,
    )


def count_frames(gif):
    probe = shutil.which("ffprobe") or str(
        Path(rec.ffmpeg() or "ffmpeg").with_name("ffprobe")
    )
    out = subprocess.run(
        [probe, "-v", "error", "-count_frames", "-select_streams", "v:0"]
        + ["-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(gif)],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mp4", type=Path, default=MP4)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    cards = render_cards()
    tmp = Path(tempfile.mkdtemp(prefix="agentforeman-showcase-"))
    try:
        lines, n = [], 0

        def add(image, seconds):
            nonlocal n
            path = tmp / f"{n:05d}.png"
            image.save(path)
            lines.extend([f"file '{path}'", f"duration {seconds:.3f}"])
            n += 1
            return path

        add(cards["intro"], INTRO_SECONDS)
        for name, *_ in STORY:
            cast = rec.capture(name, VIEW)
            for png, seconds in timeline(cast):
                add(compose(cards[name], png), seconds)
            print(f"{name}: {cast.seconds():.1f} s recorded", flush=True)
        last = add(cards["outro"], OUTRO_SECONDS)
        # The concat format needs the last file twice, or it drops the last duration.
        lines.append(f"file '{last}'")
        listing = tmp / "frames.txt"
        listing.write_text("\n".join(lines) + "\n")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        encode(listing, args.out, args.mp4)
        seconds = sum(float(x.split()[1]) for x in lines if x.startswith("duration"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    size, frames = args.out.stat().st_size, count_frames(args.out)
    print(
        f"showcase: {seconds:.1f} s, {frames} frames, {size / 1e6:.2f} MB", flush=True
    )
    if frames > MAX_FRAMES or size > MAX_BYTES:
        sys.exit(
            f"over the limit: {frames} of {MAX_FRAMES} frames, {size} of {MAX_BYTES} bytes"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
