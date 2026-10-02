# CLAUDE.md

LibNodes pushes parts of a large content-addressed book library to a fleet of reading
devices over `ssh` + `rsync`. FastAPI + Jinja2 + HTMX, no build step, no client framework.

`README.md` says what it is and *why* it works this way (the CAS library, the rsync flags,
the layout). `deploy/README.md` covers pi5: systemd, nginx, the password, every environment
variable. This file is how to work in the tree, and the index of invariants that fail
silently. Each invariant names where it is enforced and the test that pins it; the *why*,
with its measurement, lives once, in the docstring or comment at that code. Read it there.

## Commands

You are working **on pi5**, in the tree the service runs from. Edit, restart, look.

```bash
uv pip sync requirements-dev.txt        # uv, not pip. ~/.local/bin/uv is the one PATH picks
uv run pytest                           # ~730 tests, ~30 s on pi5, no network
uv run pytest tests/test_jobs.py::test_name -x
uv run ruff check .                     # lint (ruff.toml); tests/test_lint.py runs it too
sudo systemctl restart libnodes         # ~1 s, no password (/etc/sudoers.d/libnodes). stop/start
                                        # are allowed too, but a stop leaves the fleet UI down: ask
curl -s localhost:8090/healthz
journalctl -u libnodes -f
uv run tools/shot.py /devices shots/devices.png     # the only way to see the UI: no display
```

No dev-server line, deliberately. `uvicorn --reload` defaults to 8000, which urantia-library
owns; 8090 is the service's; and a second process in this tree would share the live `var/`.
Run a second instance only with **both** `--port` and `LIBNODES_STATE_DIR` pointed elsewhere
(8080/8081 and a copy of `var/` in a scratch dir work well for before/after comparisons).
`tools/shot.py` needs `LIBNODES_SHOT_PASSWORD` or `~/.config/libnodes/shot-password` to get
past the login card; `LIBNODES_SHOT_BASE` points it at another port.

`deploy/deploy.sh` is for some *other* host; run here it refuses.

Dependencies live in `requirements.in` / `requirements-dev.in`, compiled with
`uv pip compile requirements.in -o requirements.txt` (and the same for `-dev`). Never
hand-edit the `.txt` files. `uv pip sync requirements-dev.txt` is safe against the running
service: the dev file includes `requirements.in`, so it cannot drop `uvloop`/`httptools`.

## Invariants that break silently

Each of these, when broken, leaves the tests green or the UI plausible. That is why they are
listed.

### Transfers

- **`rsync -L` is mandatory for a reader and never configurable.** The library is symlinks
  into a blake2b vault; without it a push "succeeds" with dangling links. The only
  exception is `sync_mode: mirror`, which drops `-L` *and* sends the whole root so `.data/`
  travels with the links. `jobs.BASE_FLAGS`, `build_argv`.
  `test_sync_mode.py::test_a_mirror_push_keeps_the_symlinks`.
- **A mirror's rsync source is `./`**, or `--delete` never scans the destination root and a
  stray top-level file survives for ever. `mirror_sources` still returns the enumerated
  names: `_estimate` prices them and `_update_manifest` records them. Two lists on purpose.
- **`--delete` is emitted in exactly three places.** A mirror (outward, refused with no
  sources or a root target; never on Adopt); a pull (inward, bounded by its excludes and
  `--max-delete=settings.pull_max_delete`, exit 25 never retried); and a `books` node with
  `prune: true`, only when `device.prune and device.full_sync and whole_library and not
  adopt`. `whole_library` defaults to False, so a forgetful caller deletes nothing. A
  reader's prune names the top-level categories (never `./`), so names the library never
  had survive, and `excludes` are what survives *inside* them (`*.sdr/` for KOReader).
  `--delete-excluded` must never appear. Dry runs keep `--delete` under `-n`: it is the
  only preview. `retry` re-derives whole-root and full-library jobs rather than replaying
  their sources. `prune` is coerced off for non-`books` nodes. `tests/test_prune.py`.
- **Every non-dry-run job retracts the manifest rows it pruned** (`JobRunner._debit`, from
  rsync's `deleting` lines). `test_prune.py::test_a_push_retracts_the_rows_it_pruned`.
- **An `upstream` node is a pull source and never a destination.** `build_argv` raises for
  it, because `JobRunner.submit` composes every writing path including `retry`, and every
  writing route refuses it too. `test_upstream.py::test_build_argv_refuses_to_compose_any_push_to_an_upstream_node`,
  `::test_no_writing_route_is_a_way_into_an_upstream_node`,
  `::test_a_selection_dry_run_refuses_an_upstream_or_a_mirror`.
- **`full_sync` is exclusive with `mirror`, and upstream needs three guards to stay out of
  it:** the model coerces it off, the route checks `is_upstream`, the menu template too.
  `test_upstream.py::test_a_full_sync_true_upstream_is_still_not_offered_full_sync`.
- **A pull is `build_pull_argv`, never `build_argv(direction=…)`.** No `-R` (with a remote
  source it builds a second library at `/Books/Books/`), no `-L`, none of the
  device-as-destination flags, and `--partial-dir` rather than `--partial` because the vault
  trusts a blob's name. See `PULL_FLAGS`.
- **A pull credits the upstream from what rsync received, never from the index**
  (`_credit_pull`, filesystem-checked, `source='pull'`, retracted by the next scan). It is
  the only job that reindexes: on every terminal outcome except a dry run, after the
  catalog phase, and it waits for the rebuild and reports it
  (`::test_a_pull_ends_by_saying_the_index_caught_up`). `test_upstream.py::test_a_pull_credits_the_upstream_with_what_it_received`,
  `::test_a_scan_retracts_what_a_pull_claimed`.
- **A pull that stopped urantia-library must always start it again.** The `finally` in
  `_run_pull` covers failures and Abort; `var/service-hold.json` covers the process dying in
  the window (`JobRunner.start()` reads it). The `systemctl` calls are not abortable — a
  killed `stop` client does not cancel the stop. Before phase 1 a preflight asks
  `is-active` and runs a no-op `start`, so a missing polkit rule fails the job before a byte
  moves; an inactive unit is neither stopped nor started. `tests/test_upstream.py`
  `::test_a_restart_during_the_quiet_window_starts_the_service_again`,
  `::test_abort_cannot_kill_the_service_stop_half_way`,
  `::test_a_pull_that_may_not_manage_the_service_transfers_nothing`,
  `::test_a_stopped_service_is_neither_stopped_nor_started`.
- **`sudo` cannot work inside LibNodes.** The unit has `NoNewPrivileges=yes`; the service
  commands go through polkit (`deploy/50-libnodes-urantia.rules`: one action, one unit, one
  user). `/etc/sudoers.d/libnodes` is for your shell, not the service.
- **Pushes run together, a pull runs alone, and a device runs one job at a time.**
  `JobRunner._admit` (readers–writer, a waiting pull holds new pushes back) and a lock per
  device; the job's state is re-checked after both, so an Abort or ✕ while it waited wins.
  `test_upstream.py::test_two_pulls_never_run_at_once`, `tests/test_jobs.py`
  `::test_a_push_stopped_while_waiting_behind_a_pull_never_runs`,
  `::test_one_device_runs_one_job_at_a_time`, `::test_a_job_queued_twice_runs_once`.
- **A restart fails running jobs and takes back queued/deferred ones**, argv rebuilt against
  the current devices.yaml (a mode change is refused like a retry).
  `test_jobs.py::test_a_job_waiting_at_a_restart_runs_after_it`,
  `::test_a_job_whose_device_changed_mode_is_not_resumed`.
- **`cas_tree`, not `is_mirror`, decides whether a scan keeps symlinks** (`scan_argv`'s
  `-l`, `keep_links`, `expected_toplevel`). `is_mirror` stays "replicated to, with
  --delete"; widening it revives `/replicate` against production.
  `test_upstream.py::test_an_upstream_scan_asks_rsync_for_link_targets_like_a_mirrors_does`.
- **`SKIP_TOPLEVEL` is a security boundary** (urantia-library's credentials, `Recommended/`'s
  duplicate links, the vault). Enforced at the index walk's depth 0 — which makes these
  unbrowsable *and* unpushable, since `_resolve` admits only indexed paths — and in
  `full_sync_sources`. `mirror_sources` ignores it on purpose. `PULL_EXCLUDES` is a
  different list for a different question. `test_sync_mode.py::test_the_vault_is_still_hidden_from_browsing`.
- **Do not add `-h` or `%i`**, or promote `--modify-window` to `BASE_FLAGS`, or answer a FAT
  re-send with `-W` or `--size-only`. Each was measured and rejected; the reasons sit beside
  `BASE_FLAGS`, `OUT_FORMAT` and in `build_argv`.
- **FAT gets `--modify-window=1`; `perms: False` gets `--no-perms --no-owner --no-group`
  together** (a failed chown leaves the mtime unstamped: a re-send loop).
  `FS_PROFILES` in `models.py`. `test_progress_real.py::test_a_filesystem_without_perms_gets_no_owner_either`.
- **A push is size+mtime.** `--size-only` belongs to Adopt, and to `stores_times: false`,
  which needs `--size-only --no-times` together and describes the *target path*, not the
  device or `fs:`. `test_scan_adopt.py::test_a_normal_push_is_not_size_only`,
  `::test_a_device_that_cannot_store_times_gets_both_flags`,
  `::test_the_opt_out_is_a_device_fact_not_a_filesystem_one`.
- **rsync exit 23 is two outcomes**; `is_attrs_only` tells "failed to set times" from a real
  partial transfer. `test_hints.py::test_an_attrs_only_exit_23_is_not_a_partial_transfer`.
- **Counting:** `bytes_done` is file size handled, `bytes_wire` is the link (only from the
  closing summary); `xfr#` is files done, `to-chk` is entries walked; directories are never
  files in any view (`DIR n`, PRESENT ON, the dock, `files_deleted`). Only the transfer
  phase writes a job's numbers (`_stream(track=False)` for the rest). `DELETE_RE` matches
  `deleting <path>` without `*`. `test_manifests.py::test_every_view_counts_files_the_same_way`,
  `test_upstream.py::test_the_catalog_phase_does_not_redefine_the_transfers_numbers`.
- **rsync output is decoded incrementally** (`_iter_lines`): a 4 KiB read can split a
  Cyrillic letter. `test_jobs.py::test_a_character_split_across_a_read_survives`.
- **Commands are argv lists; one ssh builder** (`probe.ssh_base`, used for the probe, Test,
  scans, every `-e` and a pull's remote commands). An ssh *remote* command is the one thing
  that cannot be a list — ssh re-splits it — so it is one `shlex.join`ed word.
  `test_upstream.py::test_a_remote_command_is_one_already_quoted_word`, `tests/test_ssh_keepalive.py`.
- **Cancelling a task does not stop its subprocess.** Every `stop()` cancels readers, then
  `procs.reap`s; a registry deregisters a child only once it has exited, or the one process
  needing a reap is missing. `test_scan_adopt.py::test_stopping_the_scanner_reaps_a_listing_still_running`.

### Library, manifests, probe

- **Never walk the library in a request.** Queries come from the SQLite index; rebuilds run
  on one thread and publish by rename. A rebuild asked for mid-walk runs again
  (`AppState.reindex_soon`); a failed one says so in the index chip.
- **A subtree is a half-open range on `path`, never a `LIKE` prefix** (`presence`,
  `max_file_size`, `subtree`): LIKE here scans the whole device slice (18 s measured). The
  manifest has no secondary index, and adding one needs a query that reads it.
  `test_manifests.py::test_a_directory_count_costs_the_subtree_not_the_whole_device`.
- **`Manifests.last_sync` is cached and every write keeps it exact** — the Devices poll asks
  for the whole fleet every 10 s. `test_manifests.py::test_last_sync_stays_exact_through_every_write`.
- **The coverage map counts only library paths**: `Manifests.coverage` joins to the index on
  `path` (a range count took dragon's vault for books), and takes sizes from the index. The
  root alone is cached per device, keyed on the index's `indexed_at` and a counter every
  write bumps *after* committing (`_touched`; a new writer must call it), so a count taken
  across a write is never served. `test_manifests.py::test_coverage_counts_only_the_librarys_own_files`,
  `::test_the_root_coverage_follows_every_write`, `::test_a_write_during_the_root_count_is_not_cached_away`.
- **A push credits only what its excludes let through, and the map measures a device against
  that share.** `_update_manifest` and `_estimate` drop `LibraryIndex.excluded_roots`
  (rsync's own matching, `models.ExcludeRules`: `*` stops at `/`, and an unanchored `Video/`
  matches at any depth). A false credit stands until the next scan, which may be never. A
  leftover inside an excluded tree counts as held but never stands in for a missing book
  (`_Drawn.held_out`). `test_excludes.py::test_a_push_does_not_credit_what_its_excludes_held_back`,
  `::test_a_leftover_does_not_stand_in_for_a_missing_book`.
- **A scan overrules every row written before it began, push rows included**
  (`replace_scan`, with `Scanner._run`'s start time). A row written after that survives: a
  push that finished mid-scan may have landed behind the listing.
  `test_manifests.py::test_a_scan_retracts_a_push_it_did_not_find`,
  `::test_a_push_that_lands_during_a_scan_survives_it`.
- **"Not there" needs a `scans` row**; no rows and no scan is "nothing recorded"
  (`CoverageRow.state`). `test_routes.py::test_a_scanned_device_holding_nothing_is_not_called_unscanned`.
- **A scanned symlink's size comes from the vault; unresolvable is `None` (a dash), not 0.**
  A mirror's vault is not "extras" (`expected_toplevel=SKIP_TOPLEVEL`).
- **Requests never probe a device.** `note_interest()` is a `time.time()` stamp and must stay
  one. The dot's freshness is the backoff, which `probe_backoff_watched` shortens while a
  Devices page polls; `due()` re-judges against the current ceiling.
  `test_probe.py::test_an_opening_tab_pulls_a_long_backoff_forward`.
- **One ssh carries every reading** (`_readings_script`: df with its toybox fallback,
  battery, charger), and `_loop` never awaits it. The Test button runs the same script.
  `test_battery.py::test_the_battery_rides_along_with_df`.
- **Only the `status` file beside `capacity` (or a declared `charging:`) says "on a
  charger"** — never an `online` file, never the sign of `CURRENT_NOW`. The charge state is
  never carried forward, and an offline row draws no bolt. `tests/test_battery.py`
  `::test_the_status_file_is_the_sibling_of_the_capacity_file`,
  `::test_the_current_sign_is_not_a_charging_signal`, `::test_an_offline_row_draws_no_bolt`.
- **`var/probe.json` is written at shutdown only**, between the task cancels and the reap,
  and restores `last_ok` but not `state`. `FreeSpace.checked_at` dates the reading (LAST
  SEEN prints it); staleness is `_Slot.space_stale`.
  `test_probe.py::test_the_cache_is_written_at_shutdown_and_not_before`.
- **`Settings.concurrency` defaults to 1; pi5's unit sets 3.** The default is for a host
  that has not declared itself.

### UI

- **Every template except `base.html` and the pages renders standalone.** A new fragment
  route goes into `FRAGMENTS` in `tests/test_routes.py`, or the contract is not enforced.
- **A device id reaches the DOM only as `Device.dom_id`** — htmx's out-of-band swap builds a
  CSS selector from the id attribute, and `sigmaai.au` matched nothing. URLs keep the real
  id. `tests/test_dom_ids.py`.
- **The presence map is one slot per fleet device, in fleet order** (`presence_slots`),
  never wraps (`.pmap` has no `flex-wrap`), and every state rule names its context
  (`.pmap > .p-ok`) or `.pmap > i` outranks it. Header labels share the slots' flex, gap
  and padding. `test_manifests.py::test_the_presence_map_has_one_slot_per_device_in_fleet_order`,
  `test_theme.py::test_a_slot_is_painted_by_its_state_and_not_by_the_default`,
  `test_battery.py::test_the_map_header_lines_up_with_the_slots`.
- **The file table is the only navigator**: a directory name is a link carrying `p` alone,
  and `.pathline` is a real breadcrumb. `#sel-form` needs `hx-disinherit="hx-include"`, or
  every link inside it smuggles the current `p`. The filter narrows one level (`parent IS
  ?`), it does not search. `test_routes.py::test_a_directory_row_is_a_link_and_a_file_row_is_not`,
  `::test_the_table_does_not_smuggle_its_own_directory_into_a_link`,
  `test_library.py::test_filter_narrows_the_current_level`.
- **The crumb and the selection bar are pinned as one group** (`.lib-sticky`, opaque
  background, padding not margin). `test_theme.py::test_the_selection_bar_stays_on_screen`.
- **The filename never elides; the catalog title may.** NAME's 140px floor is what the
  1090px band can afford. `test_battery.py::test_the_filename_is_never_elided`.
- **The Devices poll carries the filter** (`hx-include="[name=q]"` plus `hx-disinherit`), or
  it erases it every 10 s. `test_routes.py::test_the_ten_second_poll_carries_the_filter`.
- **`#device-rows` is two containers** (table or cards); anything aimed at it resolves the
  view through `resolved_view`. A bare `/devices` writes no cookie. TABLE and GRID offer the
  same actions; there is no Retry beside Test. `test_routes.py::test_a_test_in_grid_refreshes_one_card`,
  `::test_a_bare_devices_page_does_not_pin_its_own_default`, `::test_there_is_no_retry_beside_test`.
- **Cookies:** `libnodes_card_hide` names what is *hidden*, dot-separated (a comma is not a
  cookie-octet); `libnodes_lib_path` fills the rail *link* only, is percent-encoded and
  validated against the index on every read. `tests/test_card_prefs.py`,
  `test_routes.py::test_the_library_position_survives_a_trip_to_the_devices_page`.
- **Nothing is hidden with CSS where a flex rule can outrank `[hidden]`** — omit the markup.
  Assert on computed style or a screenshot for any visual change.
- **`--scale` is a page zoom, and three breakpoints are derived from it by hand**: 1089
  (900 × scale), 1090, and 1520 (≥ 1248 × scale). Touch screens up to 1280px get 1.35, and
  sizes that must be physical are divided by it. Floors were measured on the content, never
  borrowed from a screen size (the Tab S4 is 700 CSS px, the Nexus 10 800). `tests/test_battery.py`
  `::test_the_rail_breakpoint_follows_the_zoom`, `::test_the_tablet_zoom_leaves_the_library_a_table`,
  `::test_the_touch_minimum_survives_the_zoom`.
- **The device table's tracks, header cells and row cells agree in number.**
  `test_battery.py::test_the_grid_declares_a_track_for_every_cell`. So do the coverage map's:
  four device tracks plus one per folder, and `repeat()` cannot take 0, so no folders is its
  own rule. `test_routes.py::test_the_coverage_grid_declares_a_track_for_every_cell`.
- **A dialog scrolls in its body and never outgrows the screen**; Escape or a tap on the
  backdrop closes the top one. `test_theme.py::test_a_dialog_taller_than_the_screen_scrolls_and_keeps_its_close`.
  Its `dvh` cap is in `@supports`: a declaration holding `var()` does not fall back to the
  line above when a unit is unknown, it computes to `none` (Chrome < 108, the Nexus 10 and
  the LG G4). `test_theme.py::test_a_newer_viewport_unit_is_behind_supports`.
- **The SSE dock:** `dock`/`done` events are never dropped from a full subscriber queue;
  terminal lines are batched (`LINE_BATCH`). The dock opens a stream only while a job is
  live (six connections per host). `AuthMiddleware` is pure ASGI, never
  `BaseHTTPMiddleware`, and nothing may read `request.is_disconnected()` beside
  `EventSourceResponse`. `test_jobs.py::test_a_full_queue_still_hears_that_a_job_finished`.

### Access

- **Auth is off whenever `LIBNODES_PASSWORD` is unset** — fail-open, warned at startup. The
  password is `SecretStr` because `settings` is in every template context. The LAN is not a
  trust boundary; the password is the whole guard. `test_auth.py::test_no_password_means_no_lock`.
- **An unauthenticated fragment gets 401 + `HX-Redirect` and an empty body**, never a page.
  `/static` and `/healthz` are open (`auth.OPEN_PATHS`).

## Conventions

- Shared state hangs off `request.app.state.lib` (`AppState`); reach it with
  `deps.state(request)` and build context with `deps.base_context(request, active)`.
- One route module per view under `libnodes/routes/`. A page extends `base.html`; everything
  else returns a bare fragment.
- New settings go on `Settings` (`config.py`), which makes them `LIBNODES_` variables; add
  them to the table in `deploy/README.md` at the same time.
- Formatting belongs in the Jinja filters in `templating.py`, not in handlers or templates.
- **Comments explain why, once.** A decision driven by a measurement cites it where the code
  enforces it. Elsewhere, point there in a line. Keep a history note only where the obvious
  fix would bring the bug back.
- `static/app.css` is hand-written; its custom properties mirror the design bundle's
  Tailwind tokens. Fonts are self-hosted (`static/fonts/regenerate.py`).
- Tests: pytest-asyncio auto mode, no network. The `library` fixture is a real CAS tree
  (with a dangling link). `fake_rsync` emits real `--info=progress2` output.
- Visual changes are checked with `tools/shot.py` (PNG, or `--eval` for computed style);
  `tests/test_theme.py` parses `app.css` for the rest.

## Gotchas

- `var/` is **live state** the service holds open (index, jobs, manifests, probe.json,
  logs, `devices.yaml`). Never delete it, never commit it. `var/shot-profile/` holds
  `shot.py`'s login cookie.
- `.venv/` is what the unit executes. A sync that dropped a runtime dependency takes the fleet
  down at the next restart.
- `design_handoff_libnodes/` is gitignored and consumed; do not add it back.
- **pi5 is the dev box and the deployment.** 192.168.1.32, aarch64, Debian 13, `/Books` on a
  931 GB NVMe (Gen2, 430 MB/s). 8090 on `0.0.0.0`, LAN only; urantia-library holds 8000
  behind nginx. It was public for an evening in 2026-08 and withdrawn; read
  `deploy/README.md` before adding any public vhost.
- The old Pi 3 (`ssh pi`) is stopped and disabled. Starting it again means two hosts can
  push to one device; nothing in the code prevents that.
- LibNodes only works at the URL root (absolute template URLs, `asset()`, `OPEN_PATHS`).
- The `/fleet/*` and `/node/*` 308 redirects (`main.py`) keep stale tabs polling; keep them.
- `devices.yaml` is hand-edited and hot-reloaded (inotify on the **parent directory**,
  because editors save by rename). A field it does not know fails validation and shows in
  the Devices chip while the last good config keeps serving.

## Where things are

The module table is in `README.md` §Layout; every module opens with a docstring saying what
it guarantees. `tools/shot.py` is host tooling, not app code. Open work is in `TODO.md`.
