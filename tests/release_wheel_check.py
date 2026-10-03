"""Builds the wheel, installs it in a fresh venv, and runs the installed command.

Run: python3 tests/release_wheel_check.py
An editable install hides missing data files, so only the installed wheel proves the package.
"""

import json
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kw):
    r = subprocess.run(list(args), capture_output=True, text=True, timeout=600, **kw)
    if r.returncode:
        sys.exit(f"failed: {' '.join(map(str, args))}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def main():
    work = Path(tempfile.mkdtemp(prefix="af-wheel-"))
    run(
        sys.executable,
        "-m",
        "pip",
        "wheel",
        str(ROOT),
        "--no-deps",
        "-w",
        str(work / "dist"),
    )
    wheel = next((work / "dist").glob("agentforeman-*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    for need in (
        "agentforeman/static/index.html",
        "agentforeman/static/app.js",
        "agentforeman/static/app.css",
    ):
        assert need in names, f"{need} is missing from the wheel"
    assert not any(n.startswith("tests/") for n in names), "tests do not ship"
    run(sys.executable, "-m", "venv", str(work / "venv"))
    pip = work / "venv" / "bin" / "pip"
    run(str(pip), "install", "--quiet", str(wheel))
    exe = work / "venv" / "bin" / "agentforeman"
    version = run(str(exe), "--version").strip()
    assert version.startswith("agentforeman "), version
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    p = subprocess.Popen(
        [str(exe), "demo", "--port", str(port)],
        cwd=work,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        # The demo prints its sign-in link, and the API needs the token in it.
        token = re.search(r"\?token=([A-Za-z0-9_-]+)", p.stdout.readline()).group(1)
        state = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/state", headers={"X-Foreman-Token": token}
        )
        end = time.time() + 30
        while True:
            try:
                with urllib.request.urlopen(state, timeout=3) as r:
                    agents = json.loads(r.read())["agents"]
                break
            except OSError:
                assert time.time() < end, "the installed demo did not answer"
                time.sleep(0.3)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/app.js", timeout=3) as r:
            assert r.status == 200
    finally:
        p.terminate()
        p.wait(10)
    assert len(agents) >= 5
    print(
        f"[PASS] {wheel.name}: {len(names)} files, {version}, the installed demo serves {len(agents)} sessions"
    )


if __name__ == "__main__":
    main()
