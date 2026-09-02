# CLAUDE.md

LibNodes pushes parts of a large content-addressed book library to a fleet of reading
devices over `ssh` + `rsync`. FastAPI + Jinja2 + HTMX, no build step, no client framework.

`README.md` covers what it is and *why* it works this way — the CAS library, the rsync flag
choices, the layout table. `deploy/README.md` covers pi5 itself: systemd, nginx, the
password and every environment variable. This file is what neither of those says: how to
work in the tree.

## Commands

You are working **on pi5**, in the tree the service runs from. There is nothing to deploy:
edit, restart, look.

```bash
uv pip sync requirements-dev.txt                    # uv, not pip. ~/.local/bin/uv is the one PATH picks
uv run pytest                                       # 514 tests, ~20s on pi5, no network
uv run pytest tests/test_jobs.py::test_name -x
sudo systemctl restart libnodes                     # ~1s, no password: /etc/sudoers.d/libnodes
curl -s localhost:8090/healthz                      # and http://pi5:8090/ from the LAN
journalctl -u libnodes -f
uv run tools/shot.py /devices shots/devices.png     # see the UI: there is no display here
```

There is deliberately **no dev-server line**. `uvicorn --reload` defaults to 8000, which
urantia-library owns on this host; 8090 is the service's; and a second process in this tree
would write the live `var/` out from under it. The restart is a second, and `var/probe.json`
(saved at shutdown, restored at start) is what stops it blanking every dot. Run a second
instance only with **both** `--port` and `LIBNODES_STATE_DIR` pointed somewhere else.

`deploy/deploy.sh` still exists, but only for pushing to some *other* host — run here it
refuses, because its target is this tree.

Dependencies are declared in `requirements.in` / `requirements-dev.in` and compiled with
`uv pip compile requirements.in -o requirements.txt`. Never hand-edit the `.txt` files.
`uv pip sync requirements-dev.txt` is safe against the running service: the dev file starts
with `-r requirements.in`, so it is a strict superset and cannot uninstall the `uvloop` and
`httptools` the unit's ExecStart names.

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
- **rsync exit 23 is two outcomes, and only the diagnostics tell them apart.** It means
  "some files/attrs were not transferred" — a genuinely partial push, *or* a complete one
  that could not stamp a timestamp. `is_attrs_only` (`libnodes/jobs.py`) splits them: every
  line rsync prefixes with `rsync:` must be a `failed to set <attr>`, and there must be at
  least one, or it stays a failure. Treating them alike is not a wrong colour, it is three
  wrong things: the dock drew `TRANSFER FAILED` in red over a push that had delivered every
  byte; the manifest took `_record_partial` (only `files_sent` names) instead of
  `_update_manifest`; and the retry path ran the whole transfer twice more — job #1 reached
  `attempt=3` re-sending two books that were already there, byte-exact. This is the *normal*
  outcome on Android, not a corner case: `/sdcard` is the FUSE emulation layer and its
  daemon does not implement `utimensat`, returning EPERM to everyone, root included
  (measured on nexus10 — `touch -t` fails there as root). Pinned by
  `tests/test_hints.py::test_an_attrs_only_exit_23_is_not_a_partial_transfer` beside
  `::test_a_real_partial_transfer_still_says_so`, and end to end by
  `tests/test_job_lifecycle.py::test_a_push_that_only_failed_to_stamp_times_is_a_success`.
  The exit code is kept on the job and the banner says `TIMESTAMPS NOT SET`, because a
  plain green `SYNC COMPLETE` over an exit 23 is its own small lie.
- **`stores_times: false` needs both `--size-only` and `--no-times`, and neither alone.**
  A device that cannot store an mtime re-sends its whole library on every push, because
  rsync's quick check is size+mtime and a destination whose mtime is always the transfer
  time can never match. Measured against nexus10 with `-n -i` on files identical to the
  source — `<` is data on its way, `.` is nothing sent:

  | flags | | |
  |---|---|---|
  | `-a` | `<f..t......` re-sends | exit 23 |
  | `-a --no-times` | `<f..T......` re-sends | exit 0 |
  | `-a --size-only` | `.f..t......` quiet | exit 23 |
  | `-a --size-only --no-times` | quiet | exit 0 |

  `--size-only` stops rsync comparing an mtime that can never match; `--no-times` stops it
  then writing one it can never write, which is the exit 23 left in row three. Emitted
  together in `build_argv` or not at all. End to end on nexus10: an unchanged push went
  from 2 files, 33,531 bytes of wire, `attempt=3` and a red banner to 0 files, 284 bytes,
  exit 0. Pinned by `tests/test_scan_adopt.py`
  `::test_a_device_that_cannot_store_times_gets_both_flags`.
  This is the *only* exception to the `--size-only` entry above, it is confined to a node
  that declared it, and it is affordable because the library is content-addressed: a
  changed book gets a new blake2b blob and `scan`/Adopt compares hashes, not sizes.
- **`stores_times` describes the target path, not the device and never `fs:`.** Android
  splits in two and only one half fails. *Emulated* storage — `/sdcard`,
  `/storage/emulated/0` — is a FUSE shim with nothing underneath and no `utimensat`;
  nexus10 has only this, its `/storage` holding `emulated` and an alias of it. A *physical
  card* is mounted by vold as a real volume with `allow_utime` and works straight through:
  lg's `~/sd` is a symlink to `/storage/D94C-6302/…`, 466 GB of vfat on
  `/dev/block/vold/public:179,65`, where `touch -t` succeeds as root — while `/sdcard` on
  that same phone gives EPERM. So two `fs: vfat` Android nodes disagree, and lg must *not*
  carry the flag. Deriving this from `fs:` would drop lg and the Kobo to a size-only
  comparison for nothing, including the device `--modify-window=1` exists to keep exact.
  `--modify-window` is still emitted beside these flags and is inert there; it stays,
  because tangling a filesystem fact with a path fact costs more than a dead flag. Test
  the target, never the platform — `ssh -n <node> 'F=<target>/.ut; touch "$F" && (touch -t
  202001010101 "$F" && echo OK || echo EPERM); rm -f "$F"'`, and note the `-n`, or ssh
  eats the rest of a loop's stdin and the sweep stops after one node. Pinned by
  `tests/test_scan_adopt.py`
  `::test_the_opt_out_is_a_device_fact_not_a_filesystem_one`.
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
- **One directory, one file count, in every view.** The `DIR n` badge in the file table
  (`entries.files`, `file_rows.html`), the `PRESENT ON` fraction (`manifests.py`,
  `is_dir = 0`) and the dock all count files only.
  rsync does not — `Audio/` is 234 files to the index and 244 entries to rsync, being its
  9 subdirectories and itself — so nothing derived from `to-chk` may be labelled "files".
  Pinned by `tests/test_manifests.py::test_every_view_counts_files_the_same_way`.
- **The file table is the only navigator, and both halves of that are load-bearing.**
  A directory row's name is an `<a>` (`file_rows.html`) and `.pathline` is a real
  breadcrumb built from `index.ancestors()` (`lib_pane.html`) — down and up. There is no
  tree pane any more; it was `display: none` below 972px with nothing in its place, so a
  Nexus 10 in portrait (800 CSS px) could tick a directory and never enter one, and a book
  three levels down was unreachable. Delete either half and navigation simply stops at
  that width while the suite stays green and the page looks plausible. Pinned by
  `tests/test_routes.py::test_a_directory_row_is_a_link_and_a_file_row_is_not` and
  `::test_the_breadcrumb_is_one_link_per_ancestor_plus_a_root`.
  The link carries `p` and nothing else, which is not a preference: `children()` appends
  `is_dir = 0` for a query and tests `fmt IN (...)`, so a directory row only exists when
  both are empty.
- **`#sel-form` must keep `hx-disinherit="hx-include"`.** `hx-include` is inherited, and
  the form's is `#lib-params` — `p=<the directory we are in>`. Every link inside the table
  therefore appended it, so `hx-get="/lib/pane?p=Science/Aviation"` went out as
  `?p=Science/Aviation&p=Science` and FastAPI bound the last value: the server answered
  with the directory you were already in while `hx-push-url` had written the new one to
  the address bar. The URL moved, the content did not, and nothing failed. Measured on
  `htmx:configRequest` with `tools/shot.py`. Pinned by
  `tests/test_routes.py::test_the_table_does_not_smuggle_its_own_directory_into_a_link`.
- **Never walk the library in a request.** The file list and its breadcrumb come from the
  SQLite index (`libnodes/library.py`); a rebuild runs on one background thread and
  publishes by atomic rename. A full walk is 1.0 s on pi5 for 24,621 entries, and was
  ~29 s on the Pi 3 it replaced — the invariant survives the speedup, because a request
  must not depend on the walk being fast on *any* host.
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
- **Only the `status` file beside `capacity` may say whether a node is on a charger.**
  `charging_command` (`libnodes/probe.py`) derives it with `posixpath.dirname`, which is a
  narrower guess than the one `Device.battery` exists to avoid: sysfs fixes both names
  *within* one supply directory, and a missing `status` fails the `cat` and draws no bolt
  rather than a wrong one. The `online` files that look like the more direct question are
  measured liars — on lg, `charger_controller` reports `status: Charging` and `online: 1`
  permanently while the phone is unplugged (`usb/present: 0`, battery `Discharging`, every
  other supply `online: 0`), and its `usb` supply is typed `Unknown` rather than `USB`, so
  "the non-battery supply that is online" picks the liar and skips the truth on one device.
  A `battery_cmd` node derives nothing: its `termux-api` JSON already carries `plugged` and
  `status`, and asking twice would be a second invocation for something already answered.
  A device whose charger is not beside its charge says so with `charging:` — nexus10 reads
  its charge from `ds2784-fuelgauge`, which has no `status`, while the charger is
  `smb347-battery`, one of five supplies there with no rule relating them.
  Pinned by `tests/test_battery.py::test_the_status_file_is_the_sibling_of_the_capacity_file`
  and `::test_the_charger_rides_along_on_the_same_ssh`.
- **`POWER_SUPPLY_CURRENT_NOW`'s sign is not a charging signal.** It is the obvious
  fallback for a node with no `status`, and it means opposite things on this fleet:
  nexus10 reports it positive while charging and negative unplugged, while lg and bk both
  report it *positive* with `POWER_SUPPLY_STATUS=Discharging`. Three devices, two
  conventions. It also cannot tell "full on the charger" from "unplugged", both being
  ~0 — which is exactly the distinction the green bolt draws. Pinned by
  `tests/test_battery.py::test_the_current_sign_is_not_a_charging_signal`.
- **The charge state is never carried forward; the percentage is.** `adopt_battery`
  re-parses `power` on every read and stores whatever came back, `None` included, while
  `percent` survives a bad read. They are different kinds of fact: a level moves slowly, so
  a minute-old one is still roughly true, but a bolt is a claim about *now* and a stale one
  says a device is on a charger it may have been unplugged from since. `None` and
  `"unplugged"` stay distinct in the record even though both draw nothing, because only the
  tooltip can say which it was. Pinned by
  `tests/test_battery.py::test_a_charge_state_that_stops_reading_blanks_the_bolt`.
- **`var/probe.json` is written at shutdown and nowhere else.** Nothing reads it while
  the process runs — `load_cache` runs once in `start()`, `save_cache` once in `stop()` —
  so a periodic flush would buy durability against an *unclean* exit alone, and cost a
  write every time a node answered: every 10s across six nodes, for data nobody reads.
  It exists because a deploy restart blanked every node that happened to be asleep at that
  moment, and on this fleet the Kobo can be asleep for days. `checked_at` is restored
  untouched, which is the whole trick — every staleness test already in `probe.py` then
  treats a restored figure as due, so nothing downstream needs to know it came off disk.
  `reach` comes back in part and the omissions are load-bearing: `last_ok` is a historical
  fact and is what separates amber `sleeping` from red `offline`, while `state` is a
  measurement, and `checked_at`/`next_probe_at` would make the first sweep honour a backoff
  appointment made last session. `save_cache` sits *between* the task cancels and
  `reap()` — after the cancels so no task can still be writing a reading, before the reap
  so a shutdown that runs long cannot be what loses the cache. Pinned by
  `tests/test_probe.py::test_a_restart_measures_the_dot_rather_than_restoring_it` and
  `::test_the_cache_is_written_at_shutdown_and_not_before`.
- **`FreeSpace.checked_at` dates the reading; staleness is a separate flag.** The LAST SEEN
  column (`DeviceView.seen_at`) prints it, so it has to mean "when this figure was
  measured" and nothing else. Forcing a re-read — `invalidate_space` when a transfer
  lands, `refresh_all` on a `devices.yaml` edit — deliberately keeps the figures so the
  cell does not blink empty, and used to null `checked_at` to schedule the next probe.
  With the column in place that reads as "never measured" beside numbers plainly on
  screen, in the one moment the row is being watched. `_Slot.space_stale` carries the
  schedule instead, cleared in the one place `probe_space` commits to the ssh so no
  outcome can forget it. Pinned by
  `tests/test_probe.py::test_invalidating_the_cache_keeps_the_reading_it_dates`.
- **LAST SEEN dates the readings, not the dot.** One ssh carries `df` and `battery:` every
  `freespace_interval` (300s); the connect behind the dot repeats every 10–30s and the row
  re-renders every 10s. The two ages disagree by minutes as a matter of course, so the
  column prints `space.checked_at` and the *tooltip* carries `reach.last_ok` — printing the
  connect there would date STORAGE five minutes early, which is the fault the column exists
  to remove. It is also what makes an offline row's figures honest rather than hidden: a red
  row keeps the last reading in `--faint` with its age beside it, matching what the cards
  and every *sleeping* row already did. The storage bar dims via the `track-disk` modifier
  and not a row-level rule, because `.trow.is-offline .track > i` outranks `.track-err > i`
  and would take a red node's low-battery tint with it — the likeliest reason it is red.
  Pinned by `tests/test_battery.py::test_the_row_dates_the_readings_it_shows` and
  `::test_a_low_battery_keeps_its_tint_on_an_offline_row`.
- **The device table's CSS tracks, `<thead>` cells and row cells must agree in number.**
  A grid whose template grew a column the stylesheet does not know about still renders —
  it silently wraps the last cell onto a second line. `.subrow` is the one top-level div
  that is not a column and says so with `grid-column: 1 / -1`. Pinned by
  `tests/test_battery.py::test_the_grid_declares_a_track_for_every_cell`.
- **`--scale` has a second value, and the tablet regime is a third layout.** A 10" tablet
  is ~150 CSS px per inch against a monitor's ~96 — 184 under Chrome's "Desktop site",
  which widens the layout viewport to 980 CSS px and lands 8px above the 972px breakpoint
  that would have taken the rail out of the flow. So the fleet's own tablets got the
  desktop layout at half size with 190px of nav still in it. `app.css` raises `--scale` to
  1.35 for touch screens up to 1280px and repeats the 972px block's rail rules for the band
  above 972, where the rail is still in flow; the two are a wash on content width and a
  third larger on type. Three things are load-bearing and each has a test: the repeat can
  drift (`test_the_tablet_band_hides_the_rail_the_way_the_narrow_one_does`), the zoom must
  leave the Library a table because the file table is the only navigator there is
  (`::test_the_tablet_zoom_leaves_the_library_a_table`), and 1280 is the ceiling because
  above it the device row unstacks and its 1022px of track floors will not fit a zoomed
  panel (`::test_the_tablet_band_stops_where_the_device_row_stops_stacking`). The touch
  clause is `hover: none` **and** `pointer: coarse`: a browser with no pointing device at
  all also reports `hover: none` — headless chromium does, measured — so without the
  pointer half every narrow `tools/shot.py` capture renders the tablet layout instead of
  the desktop one it was asked for.
  Two things ride with the zoom, and both are invisible from this host. Chrome on Android
  inflates text per block rather than per page, so the Library's SIZE and MODIFIED came out
  half again the size of the NAME beside them and wrapped — `text-size-adjust: 100%` on
  `html` is the off switch, pinned by
  `::test_the_stylesheet_switches_off_chromes_text_autosizer`. And those two columns hold a
  formatted string that can only break, never elide, so their track floors are that string
  measured — 55px for `136.1 MB`, 69px for `2026-05-20` in 11.5px JetBrains Mono, plus
  2x12px of padding. They fitted at their maxima and wrapped only once squeezed, which is
  why a desktop showed nothing wrong. Pinned by
  `::test_the_size_and_date_columns_cannot_wrap`.
- **`#device-rows` is two different containers, so anything aimed at it must know which.**
  `devices.html` renders *either* the cards div or the rows div and gives both that id, and
  the Devices layout is now remembered in the `libnodes_view` cookie (`routes/devices.py`),
  so a browser stays in GRID instead of being reset to TABLE by every navigation. That
  turned three table-only fragments from unreachable into routine: the filter box's
  `hx-get`, `devices_rescan`'s template, and the card's Retry — which targeted
  `#device-rows` with `innerHTML` and so replaced all 9 cards with the single row
  `/device/{id}/probe` answered with. Each resolves through `resolved_view`, which trusts
  the cookie because the cookie is only ever written from an explicit `?view=` and
  therefore always agrees with the branch that rendered. The card body lives in
  `device_card.html` for the same reason `device_row.html` exists — so one card can be
  swapped as `outerHTML` — and the Test dialog's out-of-band refresh picks between them;
  aimed at a `#node-<id>` that grid mode does not render, htmx dropped that swap silently.
  Pinned by `tests/test_routes.py::test_the_devices_view_survives_a_trip_to_the_library`,
  `::test_a_grid_page_keeps_its_cards_when_filtered_or_rescanned` and
  `::test_a_retry_in_grid_replaces_one_card`. A bare `/devices` must keep writing no
  cookie — `::test_a_bare_devices_page_does_not_pin_its_own_default` — or the rail link
  freezes whichever default it just guessed.
  TABLE and GRID are two renderings of one fleet, not two feature sets, and the card was
  written without Test — it offered every action that *writes*, all behind Actions, and
  withheld the only one that changes nothing, so a red node in GRID could be retried but
  not diagnosed. `test_the_card_offers_every_action_the_row_does` compares the two
  templates by endpoint rather than by label, because that is what an action is.
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
  without the first, and the restart check — `curl -s localhost:8090/healthz`, and
  `deploy.sh:60` when that script is aimed at another host — gates on the second. The list
  is `auth.OPEN_PATHS`.
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
  `display: flex` rule outranks the UA's `[hidden]`. On this host that is `tools/shot.py`,
  which does both against the running service — a PNG, and `--eval` for anything
  `getComputedStyle` can answer. There is no display here, so it is not a convenience: it is
  the only way the UI is ever seen. `tests/test_theme.py` covers the other half by parsing
  `app.css` directly, which needs no browser and stays in the suite.

## Gotchas

- `var/` holds real local state — index, jobs, manifests, logs, `devices.yaml`. It is
  gitignored. Do not delete it to "clean up", and never commit it.
- `design_handoff_libnodes/` is gitignored and local-only. It has been consumed; the code is
  the artefact now. Do not add it back to git.
- `.venv/` is this host's aarch64 one (CPython 3.12.13) and the service **execs it**:
  `ExecStart=/home/tigran/libnodes/.venv/bin/uvicorn`. It is not a dev sandbox — a
  `uv pip sync` that dropped a runtime dep would take the fleet down at the next restart.
  It is still excluded from `deploy.sh`'s rsync, along with `var/`, `tests/`, `.git/` and
  the design bundle, for the case that script is now for: a *different* host.
- **`var/` is live state, not a working copy.** The running service holds `index.db`,
  `jobs.db`, `manifests.db` and `probe.json` open. A second LibNodes started in this tree
  inherits `LIBNODES_STATE_DIR=<project>/var` by default, and then two processes fight over
  those files and both ssh the whole fleet on their own schedules. `var/shot-profile/` is
  `tools/shot.py`'s browser profile and holds its login cookie; it is gitignored with the
  rest of `var/`.
- **pi5 is the dev box *and* the deployment, and they are one tree.** 192.168.1.32,
  aarch64, Debian 13, 4 cores, 15 GB, `/Books` and `/home/tigran/libnodes` on a 931 GB
  NVMe (WD Blue SN570, PCIe Gen2 x1 per `dtparam=pciex1_gen=2` — 430 MB/s measured, up from
  ~210 MB/s at Gen1). It serves **8090** on `0.0.0.0`, **LAN only** — no reverse proxy,
  8090 not forwarded. urantia-library holds 8000 (behind nginx on 443); 8080 is free. It
  was briefly public at `https://proxyai.ddns.net/` on 2026-08-17 and that was withdrawn
  the same evening — the allowlist was pinned to a rotating home IP, so it would eventually
  have admitted whoever the ISP handed the address to next. `deploy/README.md` has the full
  reasoning; do not re-add a public vhost without reading it.
- **The old Pi 3 has been stopped, not just superseded.** `ssh pi` (192.168.1.33,
  `raspberrypi`, armv7l, `/home/pi/libnodes`) reports its unit **disabled and inactive**,
  and 8090 there is connection-refused. That is what retires the "two instances can reach
  one fleet" hazard — it is a fact about that host, not a policy, so starting it again
  brings the hazard back: nothing in the code stops two hosts pushing to one device.
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

`tools/` is host-side tooling that is not part of the app: `tools/shot.py` only. Its
docstring carries the measured reason for every chromium flag it passes.

Open work is tracked in `TODO.md`.
