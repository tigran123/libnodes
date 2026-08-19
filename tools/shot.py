#!/usr/bin/env python3
"""Photograph — and interrogate — the running UI on a host with no display.

pi5 is the dev box now, over ssh, with no X and no Wayland. This is therefore the only way
anyone sees the UI, so it has to answer both halves of the §Conventions rule "when a change
is visual, assert on computed style or a screenshot": it writes a PNG *and* it can run JS in
the page and print what `getComputedStyle` says.

Run it through the venv, which is where `websockets` lives (it arrives with
`uvicorn[standard]`, so this adds no dependency):

    uv run tools/shot.py /devices                      # writes shots/devices.png
    uv run tools/shot.py /devices --show               # ...and opens it on your X display
    uv run tools/shot.py /devices out.png --theme light
    uv run tools/shot.py /library shots/lib.png --full
    uv run tools/shot.py /devices - --eval "getComputedStyle(document.querySelector('.trow')).gridTemplateColumns"

The first argument is the *route*, the second the file. That order is a trap when the file
is the only thing on your mind, so it is defended twice: an output-looking first argument is
rejected by name, and omitting the file writes shots/<route>.png rather than nothing. The
first version defaulted the file to "-" (write nothing), and `shot.py devices.png` therefore
photographed the route /devices.png in silence and exited 0.

Why CDP rather than `chromium --headless --screenshot=out.png`, which is one line:

  * The service has a password, and the one-liner cannot carry a cookie. Here we log in
    once by filling the real form (it is a plain POST; see templates/login.html) and the
    profile keeps the cookie, because `remember` is checked by default and
    LIBNODES_SESSION_DAYS is 30. So the password is needed once a month, not once a shot.
  * A PNG cannot answer "is the track template still 9 columns". Runtime.evaluate can.

The password comes from $LIBNODES_SHOT_PASSWORD, or from ~/.config/libnodes/shot-password
if that exists. Two sources because neither alone works: an exported variable does not
survive between an agent's shell invocations, and an rc-file export is the wrong place for a
password. Both are outside the tree deliberately — this file is in git, and
/etc/default/libnodes (where the service reads the same secret) is 0600 root and is not read
here. With neither set this still works and simply photographs the login card.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PASSWORD_FILE = Path.home() / ".config" / "libnodes" / "shot-password"

# Measured on pi5, 2026-08-19: the same screenshot takes 25.7 s with Chromium's default
# profile and 0.7 s with these. The load-bearing one is --user-data-dir; with the shared
# profile Chromium spends ~25 s failing GCM registration against Google *before* it will
# render a page (the log fills with "Registration response error ... PHONE_REGISTRATION_
# ERROR"). The rest are the usual headless hygiene, and none of them is decoration.
FLAGS = [
    "--headless",
    "--no-sandbox",             # root-less already; the sandbox needs namespaces we do not have here
    "--disable-gpu",            # no GPU on a headless Pi; without this it retries and logs
    "--hide-scrollbars",        # a scrollbar in a screenshot is a diff that is not a change
    "--no-proxy-server",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-client-side-phishing-detection",
    "--disable-domain-reliability",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-component-update",
    "--metrics-recording-only",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-features=Translate,MediaRouter,OptimizationHints,GCMDriver,BackgroundSync",
]


class Browser:
    """One chromium, one CDP socket, closed on the way out."""

    def __init__(self, profile: Path, size: tuple[int, int]) -> None:
        self.profile = profile
        self.size = size
        self._id = 0

    def __enter__(self) -> "Browser":
        self.profile.mkdir(parents=True, exist_ok=True)
        # Stale port file: chromium writes it at startup, and a crashed run leaves the old
        # number behind. Deleting it first is what makes the wait below mean "this one is up".
        (self.profile / "DevToolsActivePort").unlink(missing_ok=True)
        self.proc = subprocess.Popen(
            [os.environ.get("CHROMIUM", "chromium"), *FLAGS,
             f"--user-data-dir={self.profile}",
             # Port 0, not a fixed number: two shots running at once must not collide, and
             # the real port comes back in DevToolsActivePort.
             "--remote-debugging-port=0",
             f"--window-size={self.size[0]},{self.size[1]}",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        port = None
        for _ in range(150):                      # 15 s ceiling; it takes 0.2 s in practice
            line = (self.profile / "DevToolsActivePort")
            if line.exists() and (text := line.read_text().splitlines()):
                port = int(text[0])
                break
            time.sleep(0.1)
        if port is None:
            self.proc.kill()
            sys.exit("chromium never wrote DevToolsActivePort — is it installed?")
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
        page = next(t for t in targets if t["type"] == "page")
        # 64 MB: a full-page PNG of the library tree arrives base64-encoded in one frame and
        # the library default of 1 MB truncates it.
        self.ws = connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024).__enter__()
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Network.enable")
        # The window size above sizes the OS window; this sizes the layout viewport, which
        # is what the screenshot and every media query actually see.
        self.call("Emulation.setDeviceMetricsOverride",
                  width=self.size[0], height=self.size[1], deviceScaleFactor=1, mobile=False)
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.call("Browser.close")
        except Exception:                                             # noqa: BLE001
            self.proc.kill()
        self.proc.wait(timeout=10)

    def call(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            got = json.loads(self.ws.recv())
            if got.get("id") == self._id:               # events interleave; ignore them
                if "error" in got:
                    raise RuntimeError(f"{method}: {got['error']}")
                return got.get("result", {})

    def js(self, expression: str):
        result = self.call("Runtime.evaluate", expression=expression,
                           returnByValue=True, awaitPromise=True)
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"].get("text", "js failed"))
        return result["result"].get("value")

    def goto(self, url: str, settle_ms: int) -> None:
        self.call("Page.navigate", url=url)
        for _ in range(300):                              # 30 s ceiling
            if self.js("document.readyState") == "complete":
                break
            time.sleep(0.1)
        # Every page here finishes assembling itself over HTMX — the device rows, the dock —
        # so "load complete" is a page mid-build. Wait for htmx to go quiet, then settle.
        for _ in range(int(settle_ms / 50) + 20):
            if self.js("document.querySelectorAll('.htmx-request').length") == 0:
                break
            time.sleep(0.05)
        time.sleep(settle_ms / 1000)


def main() -> int:
    ap = argparse.ArgumentParser(description="Screenshot / inspect the running LibNodes UI.")
    ap.add_argument("path", help="a route such as /devices, or a full URL")
    ap.add_argument("out", nargs="?", default=None,
                    help="PNG to write; default shots/<route>.png, '-' writes none")
    ap.add_argument("--size", default="1440x900", help="viewport, WxH (default 1440x900)")
    ap.add_argument("--full", action="store_true",
                    help="capture the whole page, not just the viewport")
    ap.add_argument("--theme", choices=("dark", "light"),
                    help="stamp the libnodes_theme cookie before loading")
    ap.add_argument("--eval", metavar="JS", help="run JS in the page and print the result")
    ap.add_argument("--settle-ms", type=int, default=400,
                    help="quiet time after htmx stops before capturing (default 400)")
    ap.add_argument("--show", action="store_true",
                    help="open the PNG on your X display (needs ssh -Y; see the docstring)")
    args = ap.parse_args()

    # A route that ends in an image extension is someone typing the output file first. Say
    # so, rather than dutifully photographing http://.../devices.png.
    if Path(args.path).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return err(f"{args.path!r} looks like an output file, but the first argument is the "
                   f"route.\n       try: {Path(sys.argv[0]).name} /devices {args.path}")

    # Never nothing: an omitted output is the common case, and writing no file while exiting
    # 0 is the one outcome that teaches you nothing at all.
    if args.out is None:
        slug = args.path.strip("/").replace("/", "-") or "root"
        args.out = str(PROJECT_ROOT / "shots" / f"{slug}.png")

    base = os.environ.get("LIBNODES_SHOT_BASE", "http://127.0.0.1:8090").rstrip("/")
    url = args.path if args.path.startswith("http") else f"{base}/{args.path.lstrip('/')}"
    width, _, height = args.size.partition("x")
    # Under var/, which is gitignored and per-machine — and kept rather than made temporary,
    # because it is what holds the login cookie between runs.
    profile = Path(os.environ.get("LIBNODES_SHOT_PROFILE",
                                  PROJECT_ROOT / "var" / "shot-profile"))

    t0 = time.time()
    with Browser(profile, (int(width), int(height))) as br:
        if args.theme:
            host = base.split("//", 1)[-1].split(":")[0]
            br.call("Network.setCookie", name="libnodes_theme", value=args.theme,
                    domain=host, path="/")
        br.goto(url, args.settle_ms)

        if br.js("location.pathname") == "/login":
            password = _password()
            if password:
                br.js("document.getElementById('password').value = "
                      f"{json.dumps(password)}; "
                      "document.querySelector('.login-card').submit(); true")
                for _ in range(200):
                    if br.js("location.pathname") != "/login":
                        break
                    time.sleep(0.1)
                br.goto(url, args.settle_ms)
                if br.js("location.pathname") == "/login":
                    return err("login refused — LIBNODES_SHOT_PASSWORD is wrong")
            else:
                print("note: no password (LIBNODES_SHOT_PASSWORD or "
                      f"{PASSWORD_FILE}); this is the login card, not the app",
                      file=sys.stderr)

        # What did we actually land on? A 404 renders perfectly well and looks like a page,
        # and a redirect means the route was not the one asked for.
        status = br.js("performance.getEntriesByType('navigation')[0]?.responseStatus")
        landed = br.js("location.pathname")
        if isinstance(status, int) and status >= 400:
            print(f"warning: {landed} answered HTTP {status}", file=sys.stderr)
        elif landed.rstrip("/") != ("/" + args.path.strip("/")).rstrip("/") \
                and not args.path.startswith("http"):
            print(f"note: redirected to {landed}", file=sys.stderr)

        if args.eval:
            print(json.dumps(br.js(args.eval), indent=2, default=str))

        if args.out != "-":
            shot = br.call("Page.captureScreenshot", format="png",
                           captureBeyondViewport=args.full)
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(shot["data"]))
            print(f"{out} — {out.stat().st_size:,} bytes, {time.time() - t0:.2f}s")
            if args.show:
                _show(out)
        elif args.show:
            print("note: --show has nothing to show; out is '-'", file=sys.stderr)
    return 0


def _show(path: Path) -> None:
    """Hand the PNG to an X viewer, if there is an X display to hand it to.

    pi5 has no display of its own, so this only means anything inside an X-forwarded
    session — `ssh -Y pi5`, and specifically one that bypasses the control socket
    (`ssh -S none -4 -Y`), since a session multiplexed onto a master that was opened
    without forwarding lands with no DISPLAY at all.
    """
    if not os.environ.get("DISPLAY"):
        print("note: no DISPLAY, so --show did nothing. Reconnect with X forwarding "
              "(ssh -S none -4 -Y pi5) — the PNG is written either way.", file=sys.stderr)
        return
    for viewer in ("display", "xdg-open"):
        if shutil.which(viewer):
            # Popen and no wait: the window is yours to keep, and the shell prompt comes
            # straight back. Output is dropped because ImageMagick narrates warnings about
            # colour profiles that mean nothing here.
            subprocess.Popen([viewer, str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    print("note: --show found neither `display` nor `xdg-open`", file=sys.stderr)


def _password() -> str | None:
    from_env = os.environ.get("LIBNODES_SHOT_PASSWORD")
    if from_env:
        return from_env
    try:
        # .strip(), because a password file almost always ends in the newline the editor
        # added, and a trailing newline is a wrong password with no visible difference.
        return PASSWORD_FILE.read_text().strip() or None
    except OSError:
        return None


def err(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
