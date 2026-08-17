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
uv run pytest                                       # 457 tests, ~17s, no network
uv run pytest tests/test_jobs.py::test_name -x
./deploy/deploy.sh [--no-restart]                   # sync to pi5, uv pip sync, restart, poll /healthz
```

Dependencies are declared in `requirements.in` / `requirements-dev.in` and compiled with
`uv pip compile requirements.in -o requirements.txt`. Never hand-edit the `.txt` files.

## Invariants that break silently

Each of these, when broken, leaves the tests green and the UI plausible. That is why they
are listed.

- **`rsync -L` is mandatory for a *reader*, and never configurable.** `BASE_FLAGS`
  (`libnodes/jobs.py`), assembled in `build_argv`. The library is symlinks into a blake2b
  vault, so without `--copy-links` a transfer reports success and delivers dangling links
  that an e-reader shows as zero-byte files.
  The one exception is a different kind of target, not a setting: a device declaring
  `sync_mode: mirror` (`Device.sync_mode`) wants the tree replicated verbatim, so
  `build_argv` drops `-L` and sends the whole root so `.data/` travels with the links it
  kept. Both halves are load-bearing — dropping `-L` *without* the vault is precisely the
  dangling-link failure, reached from the other side. Pinned by
  `tests/test_sync_mode.py::test_a_mirror_push_keeps_the_symlinks`, with the reader
  invariant restated beside it.
- **A mirror's rsync source is `./`, and that is what makes `--delete` mean anything.**
  `--delete` prunes only directories in the transfer, so enumerating the top-level names
  tidies inside `Science/` while never scanning the destination root — a stray top-level
  file then outlives every replicate. Measured on a local pair: enumerated left
  `Leftover.pdf` and an orphaned `OldCat/`, `./` removed both. `mirror_sources` still
  returns the enumerated names, because `_estimate` prices them and `_update_manifest`
  records them; collapse `job.sources` to `./` as well and a replicate updates no manifest,
  leaving `PRESENT ON` blank for ever. Two lists, deliberately: what rsync is told, and
  what the app reasons about.
- **A mirror deletes; nothing else does.** `--delete` is the only genuinely destructive flag
  the program emits. `build_argv` therefore *refuses* to compose a mirror push with an empty
  source list or a target that normalises to `/` — both would be data loss rather than a
  wrong transfer — and Adopt never gets it. Kept under `-n`, deliberately: a mirror's dry
  run is the only preview of the prune. Full Sync must never route a mirror node, because
  its own note promises it never deletes. `retry` re-derives the whole root instead of
  replaying stored sources through `_resolve`, which would strip `.data/` while `--delete`
  stayed, and it now preserves `dry_run` so a preview cannot be retried into a prune.
- **A mirror's vault is not "extras".** `Manifests.extras` subtracts the index from a scan,
  and a mirror legitimately holds `.data/` and `urantia-library/`, neither of which is
  indexed — so without `expected_toplevel=SKIP_TOPLEVEL` the dialog invites you to delete
  ~24.6k blobs from a correct replica.
- **`SKIP_TOPLEVEL` is a browsing boundary that a mirror crosses on purpose.** See the
  entry further down; the short version is that "never pushable" became "pushable only to a
  node that names `sync_mode: mirror`", and `library.py`'s depth-0 filter — the thing that
  keeps it unbrowsable and unselectable — was not touched.
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
  atomic rename. A full walk is 1.0 s on pi5 for 24,621 entries, and was ~29 s on the Pi 3
  it replaced — the invariant survives the speedup, because a request must not depend on
  the walk being fast on *any* host.
- **Requests never probe a device.** A background task writes reachability into a dict
  (`libnodes/probe.py`); handlers read it. Otherwise six sleeping e-readers become a
  six-second page load. `devices_context` calls `probe.note_interest()`, which is a
  `time.time()` stamp and must stay one — it is the single place a request touches the
  probe, and the moment it does any I/O the invariant is gone.
- **The dot's freshness is the backoff, not the 10s poll.** Two independent cadences and
  only one is 10s: the browser's `hx-trigger="every 10s"` is hardcoded in `devices.html`
  and only re-renders the dict, while the probe backs a failing device off to
  `probe_backoff_max`. At the 300s default a device that came back stayed red for up to
  five minutes with the page dutifully refreshing the stale reading — the bug that produced
  `note_interest`, which cuts the ceiling to `probe_backoff_watched` (30s) while a Devices
  page is polling. `due()` re-judges against the *current* ceiling rather than trusting the
  stored `next_probe_at`, or a page opening now would wait out an appointment made while
  nobody was watching. Red is slower still: `offline` needs `sleeping_window` (1800s) to
  have passed, so red always means "down over 30 minutes" and amber `sleeping` is the first
  half hour. Losing a node is quick in either case, ~22s. Pinned by
  `tests/test_probe.py::test_an_opening_tab_pulls_a_long_backoff_forward`.
- **`_loop` never awaits a `df`.** A space probe is bounded at 15s and tried twice, so
  awaiting it put up to 30s per online node between reachability sweeps — 30s of every dot
  on the page being stale. Use `probe_space_soon`.
- **One ssh carries every device reading.** `df` and `battery:` come back from a single
  `_readings_script` invocation with `# df` / `# battery` markers, split by `_section`.
  On a sleeping Termux node the connection *is* the cost, and two probes on separate
  schedules would also drift apart in a row that shows both. `battery:` is a path because
  there is no portable way to ask — the sysfs node name varies by vendor — so a device
  that does not declare one reports nothing rather than a guess. Pinned by
  `tests/test_battery.py::test_the_battery_rides_along_with_df`.
- **The device table's CSS tracks, `<thead>` cells and row cells must agree in number.**
  A grid whose template grew a column the stylesheet does not know about still renders —
  it silently wraps the last cell onto a second line. `.subrow` is the one top-level div
  that is not a column and says so with `grid-column: 1 / -1`. Pinned by
  `tests/test_battery.py::test_the_grid_declares_a_track_for_every_cell`.
- **`Settings.concurrency` defaults to 1, and the default is not the deployment.** The 1 is
  a property of an unknown host: on the Pi 3 the NIC shared the USB 2.0 bus with the library
  disk, so two transfers went half as fast each. pi5 puts the library on PCIe NVMe and the
  NIC on its own bus, so `deploy/libnodes.service` sets `LIBNODES_CONCURRENCY=3`. Leave the
  code default at 1 — the remaining shared resource is Wi-Fi airtime across the six wireless
  nodes, which is why the unit says 3 rather than "unbounded", and a host that has not
  declared itself should not assume either.
- **Every template except `base.html` and the page templates must render standalone** — no
  `<html>`, no doctype. That is the HTMX contract, enforced by
  `test_fragments_render_standalone`. **A new fragment route must be added to `FRAGMENTS` in
  `tests/test_routes.py:7`**, or the contract simply is not enforced for it.
- **`SKIP_TOPLEVEL` (`libnodes/config.py`) is a security boundary, not housekeeping.**
  `urantia-library/` is a sibling app holding configuration and credentials;
  `Recommended/` is a pseudo-directory of duplicate symlinks that `-L` would expand into a
  second full copy of every recommended book; `.data/` must stay unbrowsable while
  remaining the target rsync dereferences into. The docstring there explains each one.
  It is enforced in exactly two places, and only the first is the boundary: the index walk
  at depth 0 (`library.py`), which is what makes these paths unbrowsable *and* unpushable,
  since `_resolve` (`routes/jobs.py`) admits only what the index vouches for; and
  `full_sync_sources`. `mirror_sources` deliberately consults neither — a
  `sync_mode: mirror` node is sent all of it, which is that mode's entire cost. Do not
  "fix" the asymmetry by editing the skiplist or the walk: browsing must stay closed, and
  the two together are what keep the exception confined to a node that named it. Pinned by
  `tests/test_sync_mode.py::test_the_vault_is_still_hidden_from_browsing` beside
  `::test_mirror_sources_carry_exactly_what_the_skiplist_hides`.
- **A scan drops symlinks — except on a mirror, where they are the library.** `parse_line`
  (`libnodes/scan.py`) keeps dirs and regular files; a link row would be a book it cannot
  identify. On a mirror node every book *is* a link, so dropping them reported a full
  library as an empty one. `keep_links` turns them into file rows carrying the blob hash
  read out of the link target, which makes the row an exact content claim rather than the
  size guess a scan is otherwise limited to. This needs `-l` in `scan_argv`: plain
  `-r --list-only` lists a symlink but prints no `-> target`, verified against rsync 3.4.1,
  and without the target there is no hash. Size is recorded as 0 on purpose — the link's
  own 63 bytes would be a lie about the book.
- **Cancelling a task that owns a subprocess does not stop the subprocess.** Every
  `stop()` must cancel its readers and then `await procs.reap(...)`
  (`libnodes/procs.py`); `terminate()` alone only asks. Get it wrong and an rsync keeps
  writing to a device after the service has gone, while the abandoned transport is
  collected after the loop has closed — surfacing as `RuntimeError: Event loop is closed`
  from a `__del__` that names nothing, minutes away from the cause. The order matters:
  reap *after* the cancels, never before.
- **Auth is off whenever `LIBNODES_PASSWORD` is unset, and that is deliberate.** It is
  what leaves the dev server and the suite untouched (`libnodes/auth.py`), and it is
  fail-open: the startup warning in `create_app` is the only thing between a host that lost
  its env var and a fleet the whole LAN can drive. There is no reverse proxy and no network
  ACL in front of it — pi5 is LAN-only, and the LAN is not a trust boundary. This password
  is the entire guard. Pinned by
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
  `ssh_argv` at `libnodes/probe.py:448`, `scan_argv` at `libnodes/scan.py:113`).
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
- `.venv/` is x86_64 and pi5 builds its own aarch64 one. `deploy.sh` excludes `.venv/`,
  `var/`, `tests/`, `.git/` and the design bundle, and creates the venv if the host has
  none — otherwise `uv pip sync` has nothing to sync into on a first deploy.
- **pi5 is the deployment target, not `pi`.** `ssh pi5` (192.168.1.32, aarch64, user
  `tigran`, `/home/tigran/libnodes`, uv at `/usr/local/bin/uv`). It serves **8090** on
  `0.0.0.0`, **LAN only** — no reverse proxy, 8090 not forwarded. urantia-library holds
  8000 behind nginx on 443; 8080 is free. The dev default is 8000. It was briefly public at
  `https://proxyai.ddns.net/` on 2026-08-17 and that was withdrawn the same evening — the
  allowlist was pinned to a rotating home IP, so it would eventually have admitted whoever
  the ISP handed the address to next. `deploy/README.md` has the full reasoning; do not
  re-add a public vhost without reading it. The old Pi 3 (`ssh pi`, `/home/pi/libnodes`,
  8090, uv at `~/.local/bin/uv`) is **still running its own copy** — nothing was stopped
  there — so two instances can reach the same fleet. Drive transfers from one at a time.
- **LibNodes only works at the URL root.** ~64 template URLs are absolute
  (`hx-get="/jobs/dock"`), `asset()` emits `/static/…`, and `AuthMiddleware` matches
  `scope["path"]` against exact strings in `OPEN_PATHS`. That is why it gets its own
  hostname rather than a `/libnodes` prefix under the existing one. Serving it under a
  sub-path is a feature (`LIBNODES_URL_PREFIX`), not an nginx setting.
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
