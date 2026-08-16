# CLAUDE.md

LibNodes pushes parts of a large content-addressed book library to a fleet of reading
devices over `ssh` + `rsync`. FastAPI + Jinja2 + HTMX, no build step, no client framework.

`README.md` covers what it is and *why* it works this way — the CAS library, the rsync flag
choices, the layout table. `deploy/README.md` covers the Pi, systemd and every environment
variable. This file is what neither of those says: how to work in the tree.

## Commands

```bash
uv pip sync requirements-dev.txt                    # uv, not pip. /usr/local/bin/uv here
uv run uvicorn libnodes.main:app --reload           # http://127.0.0.1:8000/devices
uv run pytest                                       # 360 tests, ~15s, no network
uv run pytest tests/test_jobs.py::test_name -x
./deploy/deploy.sh [--no-restart]                   # sync to pi, uv pip sync, restart, poll /healthz
```

Dependencies are declared in `requirements.in` / `requirements-dev.in` and compiled with
`uv pip compile requirements.in -o requirements.txt`. Never hand-edit the `.txt` files.

## Invariants that break silently

Each of these, when broken, leaves the tests green and the UI plausible. That is why they
are listed.

- **`rsync -L` is mandatory and not configurable.** `BASE_FLAGS` (`libnodes/jobs.py:120`),
  assembled in `build_argv` (`libnodes/jobs.py:369`). The library is symlinks into a
  blake2b vault, so without `--copy-links` a transfer reports success and delivers
  dangling links that an e-reader shows as zero-byte files.
- **Do not add `-h` or `%i`.** Both were tried against real hardware and rejected for a
  measured reason. See README §"The rsync flags belong to the program" before touching
  `BASE_FLAGS`, `INFO_FLAGS` or `OUT_FORMAT`.
- **`--modify-window=1` on FAT is mandatory, and per-filesystem.** It hangs off
  `FsProfile.modify_window` (`libnodes/models.py`), beside `perms`, and `build_argv`
  emits it. FAT's seconds field counts in twos, so a timestamp rsync wrote reads back up
  to a second early and the exact comparison re-sends the file for ever: 8,786 of 24,620
  files on a real FAT32 card, 0 with the window. Do not promote it to `BASE_FLAGS` — on
  ext4 the timestamps are exact and the exact comparison is the point.
- **A push is size+mtime; `--size-only` is Adopt's alone.** It looks like the fix whenever
  a push re-sends files the device already has, and it is not: on FAT the real cause is
  the modify window above, and everywhere else the sizes match precisely *because* the
  content diverged in place. Pinned by
  `tests/test_scan_adopt.py::test_a_normal_push_is_not_size_only`. If a push seems to be
  re-sending too much, the dry run — now on every Library row — says what it would send.
- **`bytes_done` is the size of the files rsync handled, not network traffic.** Delta
  matching against the copy already on the device makes the two diverge by orders of
  magnitude — 4.38 GB of files across 6.7 MB of link, measured. `bytes_wire`
  (`SUMMARY_RE`) is the honest figure and only exists once rsync prints its closing
  `sent … received …` line.
- **rsync's three progress numbers count three different things**
  (`_apply_progress`, `libnodes/jobs.py`). `xfr#N` is transfers *completed*; `to-chk` is
  file-list *entries* walked past, directories and skipped files included; the byte
  counter is the running sum of the `@%l` sizes. Reading `to-chk` as "files done" once
  reported 35 files sent for a run that had sent 15. The `@` line is printed when a file
  *starts*, so the last one in an interrupted log names a file that never landed —
  `_record_partial` truncates to `files_sent` for exactly that reason.
- **One directory, one file count, in every view.** The tree (`entries.files`), the
  `PRESENT ON` fraction (`manifests.py`, `is_dir = 0`) and the dock all count files only.
  rsync does not — `Audio/` is 234 files to the index and 244 entries to rsync, being its
  9 subdirectories and itself — so nothing derived from `to-chk` may be labelled "files".
  Pinned by `tests/test_manifests.py::test_every_view_counts_files_the_same_way`.
- **Never walk the library in a request.** The tree and file list come from the SQLite
  index (`libnodes/library.py`); a rebuild runs on one background thread and publishes by
  atomic rename. A full walk is ~29 s on the Pi.
- **Requests never probe a device.** A background task writes reachability into a dict
  (`libnodes/probe.py`); handlers read it. Otherwise six sleeping e-readers become a
  six-second page load.
- **One rsync at a time.** `Settings.concurrency`, default 1. The Pi's NIC shares the USB 2.0
  bus with the library disk, so two transfers go half as fast each.
- **Every template except `base.html` and the page templates must render standalone** — no
  `<html>`, no doctype. That is the HTMX contract, enforced by
  `test_fragments_render_standalone`. **A new fragment route must be added to `FRAGMENTS` in
  `tests/test_routes.py:7`**, or the contract simply is not enforced for it.
- **`SKIP_TOPLEVEL` (`libnodes/config.py:45`) is a security boundary, not housekeeping.**
  `urantia-library/` is a sibling app holding configuration and credentials;
  `Recommended/` is a pseudo-directory of duplicate symlinks that `-L` would expand into a
  second full copy of every recommended book; `.data/` must stay unbrowsable while
  remaining the target rsync dereferences into. The docstring there explains each one.
- **Cancelling a task that owns a subprocess does not stop the subprocess.** Every
  `stop()` must cancel its readers and then `await procs.reap(...)`
  (`libnodes/procs.py`); `terminate()` alone only asks. Get it wrong and an rsync keeps
  writing to a device after the service has gone, while the abandoned transport is
  collected after the loop has closed — surfacing as `RuntimeError: Event loop is closed`
  from a `__del__` that names nothing, minutes away from the cause. The order matters:
  reap *after* the cancels, never before.
- **Auth is off whenever `LIBNODES_PASSWORD` is unset, and that is deliberate.** It is
  what leaves the dev server and the suite untouched (`libnodes/auth.py`), and it is
  fail-open: the startup warning in `create_app` is the only thing between a Pi that lost
  its env var and a fleet the whole LAN can drive. Pinned by
  `tests/test_auth.py::test_no_password_means_no_lock`. The password is `SecretStr`
  because `base_context` puts all of `settings` into every template context.
- **An unauthenticated fragment gets `HX-Redirect` and an empty body, never a page.** A
  login page returned to an `hx-get` is swapped into a table row — the thing
  `test_fragments_render_standalone` exists to forbid. The 401 is honoured because htmx
  acts on `HX-Redirect` *before* it consults the status code (verified in the vendored
  2.0.4). `/static` and `/healthz` are open on purpose — the login page would be unstyled
  without the first, and `deploy.sh:60` gates every deploy on the second. The list is
  `auth.OPEN_PATHS`.
- **`AuthMiddleware` is pure ASGI, not `BaseHTTPMiddleware`.** The latter buffers the
  response body, which breaks `EventSourceResponse` — the dock would arrive in lumps,
  exactly as it does when nginx buffers `/jobs/stream`. It reads `scope` only and never
  wraps `send`.
- **rsync and ssh are argv lists, never shell strings** (`build_argv`,
  `ssh_argv` at `libnodes/probe.py:341`, `scan_argv` at `libnodes/scan.py:112`).
  `BatchMode=yes` throughout, so a missing key fails fast instead of hanging on a prompt.

## Conventions

- All shared state hangs off `request.app.state.lib` — an `AppState` (`libnodes/state.py`).
  Reach it with `deps.state(request)` and build context with
  `deps.base_context(request, active)`; do not assemble the rail/dock context by hand.
  `base_context` namespaces the dock under `dock` deliberately — see the comment there.
- One route module per view under `libnodes/routes/`. A full page extends `base.html`;
  everything else returns a bare fragment.
- New settings go on `Settings` (`libnodes/config.py:60`), which makes them
  `LIBNODES_`-prefixed environment variables automatically. Add them to the env table in
  `deploy/README.md` at the same time.
- Number, size and time formatting belongs in the Jinja filters in
  `libnodes/templating.py` (`hsize`, `hsize_short`, `commafy`, `reltime`, `freshness`,
  `hhmmss`, `clock`, `isodate`) — not in handlers, not inline in templates.
- **Comments explain why, and cite the measurement when one drove the decision** —
  `24,616 lines around 4 real transfers`, `~29 s`, `76 s / 258 MB`, `894G for a 248G
  library`. That density is the house style; match it rather than trimming it.
- `static/app.css` is hand-written. Its custom-property names mirror the Tailwind tokens of
  the design bundle the UI was built from, so the two stay cross-readable. Fonts are
  self-hosted (`static/fonts/regenerate.py`) so the app works on an isolated LAN.
- Tests use pytest-asyncio in auto mode and need no network. The `library` fixture builds a
  real CAS tree — symlinks into a blob vault, including a dangling one — because that shape
  is what most behaviour depends on. `fake_rsync` (`tests/conftest.py:143`) emits genuine
  `--info=progress2` output so the runner, parser and SSE fan-out test end to end.
- **When a change is visual, assert on computed style or a screenshot.** Asserting that
  `element.hidden` was set once passed happily while the UI was visibly broken, because a
  `display: flex` rule outranks the UA's `[hidden]`.

## Gotchas

- `var/` holds real local state — index, jobs, manifests, logs, `devices.yaml`. It is
  gitignored. Do not delete it to "clean up", and never commit it.
- `design_handoff_libnodes/` is gitignored and local-only. It has been consumed; the code is
  the artefact now. Do not add it back to git.
- `.venv/` is x86_64 and the Pi builds its own. `deploy.sh` excludes `.venv/`, `var/`,
  `tests/`, `.git/` and the design bundle.
- The Pi serves on **8090** (8080 is nginx for urantia-library); the dev default is 8000.
- The `/fleet/*` and `/node/*` 308 redirects (`libnodes/main.py:54`) exist for pages left
  open across the rename. Remove them and a stale tab polls a 404 forever, with the table
  silently frozen and nothing saying why.
- `devices.yaml` has no editor UI. It is hand-edited and hot-reloaded: the watcher
  (`libnodes/watch.py`) is inotify on the **parent directory**, not the file, because
  editors save by rename and a file watch would survive pointing at an unlinked inode.
- `Device.formats` and `rsync_flags` parse but are ignored — kept only so an older
  `devices.yaml` still loads. See `libnodes/models.py:141` and the note in `TODO.md`.

## Where things are

The module-by-module table is in `README.md` §Layout. Every module also opens with a
docstring stating what it exists to guarantee — read that before changing one.

Open work is tracked in `TODO.md`.
