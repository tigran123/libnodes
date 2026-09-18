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
uv run pytest                                       # 655 tests, ~24s on pi5, no network
uv run pytest tests/test_jobs.py::test_name -x
sudo systemctl restart libnodes                     # ~1s, no password: /etc/sudoers.d/libnodes
                                                    # (stop/start are in that rule too, since a
                                                    #  VACUUM needs the db unheld -- but a stop
                                                    #  leaves the fleet UI down: ask first)
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
- **An `upstream` node is a pull source, and the refusal is in `build_argv`.**
  `sync_mode: upstream` (`Device.sync_mode`) means the library's *source* — the production
  host other admins upload to, so it is ahead of us and a push is a regression. sigmaai.au
  was declared `mirror`, which put Replicate in its Actions menu, and Replicate composes
  `rsync -a --delete ./ tigran@sigmaai.au:/Books/`: pointed at the server everyone uploads
  to, it would have deleted every book production held and pi5 did not. Every writing route
  refuses it *and* `build_argv` raises for it, because `JobRunner.submit` composes the argv
  for every writing path there is — including `retry`, which replays a stored job's sources
  long after a route was fixed. `/device/{id}/adopt` had **no mode guard at all** and was
  the last way in; it has one now. Pinned by
  `tests/test_upstream.py::test_build_argv_refuses_to_compose_any_push_to_an_upstream_node`
  and `::test_no_writing_route_is_a_way_into_an_upstream_node`.
- **`full_sync` is mutually exclusive with `mirror`, and with nothing else.** That is by an
  explicit `device.is_mirror` term in `device_full_sync` and an `elif` in
  `device_menu.html`, because Full Sync's note promises it never deletes and a mirror's
  transfer is defined by `--delete`. `upstream` is a value neither knew about, so the
  instant a node stops being a mirror the `elif` fires and **Full Sync — a push to
  production — reappears in its menu**, route guard satisfied. Three guards answer it: the
  model coerces `full_sync` off for an upstream, the route adds `or device.is_upstream`,
  and the template keeps `and not device.is_upstream`. Pinned by
  `::test_a_full_sync_true_upstream_is_still_not_offered_full_sync`, whose fixture node
  declares `full_sync: true` on purpose.
- **A pull is `build_pull_argv`, never `build_argv(direction=…)`, and `-R` is why.**
  With a *remote* source, `-R` makes the remote's own path a component of the destination.
  Measured against sigmaai.au: `rsync -aR -n tigran@sigmaai.au:/Books/ /Books/` wants
  `cd+++++++++ Books/` and all 20,793 blobs under `Books/.data/` — a second library at
  `/Books/Books/`, every symlink in it dangling because `../../.data/<blob>` no longer
  resolves, and the `--exclude=/.data/` anchor broken on the way past. No error, no
  warning, and nobody reads a 45,000-line dry run. `-L` goes for the mirror's reason
  reached from the far side, and the device-as-destination flags (`--no-perms/--no-owner/
  --no-group`, `--modify-window`, `--size-only`, `--no-times`) are absent *by
  construction* rather than by branch — the fixture upstream declares `fs: vfat` and
  `stores_times: false` so that is testable. A separate function, because a `direction=`
  parameter's default would be the dangerous direction.
- **A pull uses `--partial-dir`, not `--partial`, because the vault trusts the name.**
  On interruption plain `--partial` renames the partial file to its *final* name. On a
  device that is an accepted cost; in `/Books/.data/` it is a blob whose contents do not
  hash to the blake2b name it is sitting under, and every symlink pointing at it serves a
  truncated book until something notices. This is the one deliberate deviation from
  `BASE_FLAGS`. `--delete` is not conditional in a pull — it is absent, with no branch that
  could add it.
- **A pull writes the manifest from what rsync received, never from the index, and is the
  only job that reindexes.** `_update_manifest` records "what this device has" by walking
  the *local* index, which after a pull is inverted in direction — and it would run before
  the reindex, so it would record the pre-pull index as a claim about the far end. For a
  year it therefore wrote nothing at all and said `run a scan to refresh PRESENT ON`, and
  that was a worse answer than it sounds: a book pulled from sigmaai.au on 2026-09-17 read
  as absent *from the node it had just come from*, because that node's last scan was three
  days older than the book. `_credit_pull` uses the evidence the transfer itself produced —
  rsync names every file it received, and a file we received from a node is a file that
  node has. Three things make it honest. The @-lines are filtered by `SKIP_TOPLEVEL`, so
  the 20.8k vault blobs and the catalog snapshot never become library rows (`_note_sent`).
  The **filesystem** decides, not the @-line: rsync prints a name when it *starts* sending
  it and `--partial-dir` keeps an interrupted file out of its final name, so a path that is
  not there is not credited — which is what lets this run on an abort as well as a clean
  exit, and why `_record_partial`'s `files_sent` truncation is not reused (the filtered
  `.data/` lines still counted toward `xfr#`, so the list is no longer a prefix). And the
  rows are `source='pull'`, which `replace_scan` deletes alongside its own: a scan has just
  looked at the far end and must be able to retract what a transfer once implied, or a book
  deleted upstream reads present for ever. A dry run credits nothing. It calls
  `reindex_soon` on every terminal outcome including abort (an interrupted pull has still
  written files, and books the index does not know about are invisible in the Library view
  *and* unpushable), and never on a dry run, and always *after* the catalog phase —
  `LibraryIndex` reads `catalog_db` for title/author, so reindexing first bakes the old
  catalog in. The credit runs *before* that reindex, and can, because it reads the
  filesystem rather than the index. Pinned by `tests/test_upstream.py`
  `::test_a_pull_credits_the_upstream_with_what_it_received`,
  `::test_a_pull_credits_only_what_actually_landed` and
  `::test_a_scan_retracts_what_a_pull_claimed`.
- **A pull that stopped `urantia-library` and did not start it must never be green — and
  the `finally` is not enough.** Abort is safe as it stands: it terminates the subprocess
  without cancelling `_run`, so `_stream` returns 143 as an ordinary value and the
  `finally` runs. What a `finally` cannot cover is this process going away *inside* the
  window, which `sudo systemctl restart libnodes` does in about a second and CLAUDE.md
  itself tells you to do casually. `var/service-hold.json` is the durable half: written
  before the stop, unlinked after the start, and read by `JobRunner.start()` before it
  accepts work. `asyncio.shield` does not help — the loop closes underneath it. The stop
  also never runs on a phase that failed earlier, so a pull cannot start a service somebody
  had deliberately stopped. Pinned by
  `::test_a_restart_during_the_quiet_window_starts_the_service_again`,
  `::test_a_pull_restarts_the_local_service_even_when_the_catalog_fails` and
  `::test_a_pull_that_never_stopped_the_service_never_starts_it`.
- **`sudo` cannot work from inside LibNodes; the service commands go through polkit.**
  `deploy/libnodes.service` sets `NoNewPrivileges=yes`, which makes sudo's setuid bit inert
  — it refuses outright, with a different message from "a password is required", and no
  sudoers rule fixes it. `deploy/50-libnodes-urantia.rules` authorises exactly one action,
  one unit and one user, and the unit keeps its hardening. Neither `pkcheck` form is usable
  as a preflight (with `--detail` it is refused to untrusted callers; without it a
  unit-scoped rule never matches), so the probe is `systemctl is-active` followed, only if
  active, by `systemctl start` — a no-op on a running unit that travels the exact path
  `stop` will.
- **`cas_tree`, not `is_mirror`, decides whether a scan keeps symlinks.** `scan_argv`'s
  `-l`, `Scanner`'s `keep_links` and the extras dialog's `expected_toplevel` are all facts
  about the *shape* of the node's tree, which a mirror and an upstream share. Getting it
  wrong on an upstream fails green in the worst direction: every book there is a symlink,
  so a scan that drops links keeps only vault rows, `expected_toplevel` filters those away,
  and the dialog reports a full production library as an empty backlog. `is_mirror` stays
  strictly "replicated to, with `--delete`" — widening it is how `/replicate` comes back to
  life pointed at production. Pinned by
  `::test_an_upstream_scan_asks_rsync_for_link_targets_like_a_mirrors_does`.
- **A scanned symlink's size comes out of the vault, and an unresolvable one is a dash.**
  `parse_line` records size 0 for a kept link and carries the blake2b instead. The same
  scan lists the vault, so `Manifests.extras` resolves the real size through
  `.data/<hash>` — 88.3 MB rather than `0 B` for the one book sigmaai.au had and pi5 did
  not. Where nothing resolves the row carries `None` and the dialog draws `—`, and the
  footer stops totalling dashes into "0 B listed": a zero is a claim about the book, a dash
  is an admission. Keyed off the vault row's own basename rather than `".data/" + blob`, so
  a sharded vault later cannot silently stop matching.
- **`PULL_EXCLUDES` is not `SKIP_TOPLEVEL`, and only two of the nine names overlap.** A
  pull *wants* `.data/` — it is the vault every incoming symlink resolves into — and wants
  `Recommended/`, whose companion links cost a few hundred bytes with no `-L` to expand
  them. It holds back `/urantia-library/` (protecting **pi5's own** `secrets.env`, whose
  `APP_URL`/`APP_ENV`/`APP_ROOT_PATH`/`VITE_API_URL` are per-host, from the upstream's),
  `/.data/staging/` (half-written uploads: a torn blob would not hash to its own name, and
  the vault trusts what lands in it) and `/Unsorted/` (55 GB of Ubuntu images today, and
  the only one of the three that is a preference rather than a boundary). Because the
  backlog dialog is a picture of the far end and the pull declines part of it, rows the
  excludes hold back are **marked** rather than hidden — the dialog says "a Pull brings 1
  of these across; 13 are held back" instead of "exactly this".
- **A pull holds the queue.** `LIBNODES_CONCURRENCY=3`, so without the gate in `_run` a
  push to a reader could be dereferencing symlinks into a vault a pull is still filling:
  exit 24 "file has vanished", or a blob still in `.rsync-partial`. The CAS makes a
  *finished* blob safe to read at any instant and says nothing about one mid-flight.
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
- **`FsProfile.perms: False` means no owner either, and the three flags travel together.**
  `build_argv` answers it with `--no-perms --no-owner --no-group`. `--no-perms` alone
  cancels only `-a`'s `-p` and leaves `-o` and `-g`, and every node but the ThinkPad
  connects as root — so rsync calls `chown` on every directory and every temp file, and a
  FAT driver returns EPERM to root like everyone else. The Kobo's `/mnt/onboard` is
  `vfat rw,noatime,fmask=0022,dmask=0022` with no `uid=` at all: ownership is a mount
  constant with nothing on disk behind it, so there is never anything for `-o`/`-g` to
  write. The wrong red banner is the cheap half — **a file whose `chown` fails never gets
  its mtime stamped either**, keeps the *transfer* time, fails the next push's size+mtime
  quick check, and is re-sent for ever. That is the re-send loop of the entry above,
  reached from the owner side instead of the timestamp side. Measured against the live
  Kobo on 5 books already byte-identical: `--no-perms` alone `xfr#5`, 105,310 B, exit 23;
  all three `xfr#5`, 103,915 B, exit 0 (that run is the *repair* — it writes the mtimes);
  again `xfr#0`, **415 B**, exit 0. Do not "fix" this in `is_attrs_only` instead: that
  whitelist knows only `failed to set <attr>`, and forgiving the exit would paint the
  banner green while leaving the re-send loop running. Android's emulated storage is why
  it survived so long — sdcardfs fakes `chown` as a silent no-op, so every nexus10 log is
  clean and only a real FAT driver ever said so. Pinned by
  `tests/test_progress_real.py::test_a_filesystem_without_perms_gets_no_owner_either`,
  with the ext4 half asserted beside it in `::test_perms_are_kept_for_a_real_filesystem`.
- **`-W` is not the answer to a FAT device re-sending, and neither is `--size-only`.** Both
  look like the fix and both cost more than they save. `--size-only` does not even reach
  this bug — `chown` runs whatever rsync decides to send, so the exit 23 and the three
  attempts survive it — and the re-send it targets is already at zero once the owner flags
  and `--modify-window` are right (415 B, above). `-W` throws away delta resume, which
  this fleet leans on: `--partial` is in `BASE_FLAGS` because the Nexus 10 drops off Wi-Fi
  whenever its screen goes off, so interrupted pushes are routine, not exceptional.
  Measured on `lg`, one 22,070,669-byte book interrupted at 90%: delta sends 2.23 MB
  (19,860,392 matched), `-r -W --size-only` sends 22.08 MB (0 matched) — **9.9x the
  wire**. `-v` is inert here for a third reason: `INFO_FLAGS` is appended after the
  transfer flags and wins over it.
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
  tree pane any more; it was `display: none` below the rail breakpoint with nothing in its
  place, so a Nexus 10 in portrait (800 CSS px) could tick a directory and never enter one,
  and a book three levels down was unreachable. Delete either half and navigation simply stops at
  that width while the suite stays green and the page looks plausible. Pinned by
  `tests/test_routes.py::test_a_directory_row_is_a_link_and_a_file_row_is_not` and
  `::test_the_breadcrumb_is_one_link_per_ancestor_plus_a_root`.
  The link carries `p` and nothing else, and that has to be deliberate now that a
  directory row can coexist with a filter: `children()` narrows the current level instead
  of searching the subtree, so `Audio` at the root leaves the `Audio/` row and you can
  click it. Dropping `q` *is* the behaviour — the link swaps the whole `#lib` pane, so the
  pane comes back with an empty box and the full listing, which is what a tree click did.
  (`fmt` still removes every directory on its own: a directory's `fmt` is NULL and NULL
  satisfies no `IN`.) Pinned by
  `tests/test_routes.py::test_a_directory_link_carries_only_where_it_is_going`.
- **The filename never elides; the catalog title beside it always may.** `.file-name` wraps
  (`overflow-wrap: anywhere` — these names have no spaces, and only `anywhere` also lets the
  1fr NAME track shrink to its floor), and below 1089px the NAME cell is a **grid** rather
  than the flex every other cell uses, so the name owns column 2 and the title drops to a
  second row of it. As a flex item the title sat *beside* the name and the two shared one
  line: on a Tab S4 in portrait — 700 CSS px at `--scale` 1.35, the narrowest screen in the
  fleet — both ellipsised at about 23 characters, and
  `Poxititeli-avtomobilej-Zapiski-sledovatelja-1965.fb2.zip` is not a name you can recognise
  from its opening. The NAME track floor stays **140px** and must: at 1090px the table has
  not stacked yet *and* the rail is still 190px of flow, so the seven floors have only
  `1090/1.21 - 226 = 674px` of panel, and raising NAME to 240 buys a band where the row
  overflows `.panel`'s `overflow: hidden` in silence. Wrapping is what makes the narrow
  floor affordable. Pinned by `tests/test_battery.py::test_the_filename_is_never_elided`
  and `::test_a_stacked_row_gives_the_filename_its_own_line`.
- **The Library filter narrows one level; it is not a search.** `children()`
  (`libnodes/library.py`) is always `parent IS ?`, and `q` adds `name LIKE '%q%'` to it.
  It used to replace the scope instead — `(path = ? OR path LIKE 'p/%') AND is_dir = 0` —
  which reads as the more powerful feature and is the wrong question in front of a
  directory listing: at the root it scanned all 24.6k entries to answer with up to 2,000
  bare basenames, no column saying where any of them lived, seconds of it on a Nexus 10;
  and being `is_dir = 0` it could never return the directory you were obviously narrowing
  towards. `ix_entries_parent` is what keeps the level version cheap however large the
  library grows. Pinned by
  `tests/test_library.py::test_filter_narrows_the_current_level` and
  `tests/test_routes.py::test_the_filter_narrows_the_listing_rather_than_leaving_it`.
- **The Devices poll must carry the filter, or it erases it.** `#device-rows`
  (`devices.html`, both branches) re-fetches itself `every 10s` — the same element the
  filter box targets — so without `hx-include="[name=q]"` it is a bare GET: `q` binds to
  `None`, `_filtered` short-circuits, and `innerHTML` puts all ten devices back under a
  box still reading `lg`. For ever, because `innerHTML` leaves the polling div and its
  trigger intact, and with nothing failing anywhere. Rescan had the same hole, against a
  `q` parameter `devices_rescan` has always declared. `hx-disinherit="hx-include"` ships
  beside it: `hx-include` is inherited and that container holds every row's
  Test/Retry/Abort button — the entry below, from the other page. Pinned by
  `tests/test_routes.py::test_the_ten_second_poll_carries_the_filter` and
  `::test_rescan_keeps_the_filter_too`.
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
- **A subtree count is an index range on `(device_id, path)`, never a `LIKE` prefix.**
  `Manifests.presence` asks "how many of this directory's files does the device hold" once
  per directory per device, and SQLite will not answer `path LIKE 'dir/%'` from an index:
  the `ESCAPE` clause disables the LIKE optimisation, and the default
  `case_sensitive_like=OFF` disables it again against a BINARY column. Each query therefore
  planned as `SEARCH manifest USING INDEX ix_manifest_device (device_id=?)` — a full scan of
  that device's slice, priced by what the *device* holds and not by the subtree being asked
  about, so three files cost the same as three thousand. One page of `/Books/Fiction` is 304
  directories × 15 devices = 4,560 of them against a 268,692-row manifest (dragon alone
  91,032): **18.10 s measured**, for a listing that renders in milliseconds either way —
  and the root was 1.12 s, so *every* Library visit paid it. The half-open range
  `path >= 'dir/' AND path < 'dir0'` is **30 ms** on the same data, on the primary key, with
  no schema change and no new index ("0" is 0x30 and "/" is 0x2F, so that bound is the exact
  successor of the prefix under BINARY collation and is safe for every path — unlike the
  `￿` sentinel the idiom is usually written with, which drops any name starting with a
  non-BMP character). Equivalence checked over the whole live index, 23,064 directory ×
  device comparisons, zero mismatches, with one deliberate difference: `LIKE` was
  case-insensitive, so `Fiction/Abramov` had been counting the files under
  `Fiction/abramov/` as its own. The docstring claimed "two queries total regardless of row
  count" the entire time, which is why nobody counted them. Pinned by
  `tests/test_manifests.py::test_a_directory_count_costs_the_subtree_not_the_whole_device`,
  which counts SQLite VM steps through `set_progress_handler` (3.1k → 30.1k under the LIKE)
  rather than asserting on an `EXPLAIN QUERY PLAN` string whose wording changes between
  releases, and by `::test_a_directory_does_not_borrow_files_from_a_case_variant_sibling`.
  `LibraryIndex.max_file_size` had the same shape over `entries` and now uses the same range.
  `manifest` carries **no secondary index at all** as a result, and that is the finding rather
  than an oversight: nine of the ten statements in `manifests.py` lead with `device_id`, which
  the primary key answers better than `ix_manifest_device` did (it carries `path`, so no table
  lookup per row), and the tenth — `presence`'s batched `path IN (…) AND device_id IN (…)` —
  plans on the primary key too, measured at 3.65 ms with `ix_manifest_path` and 3.46 ms
  without on a copy of the live database. Both were pure write cost: 20,000 recorded scan rows
  went 66 ms to 47 ms, and 39 MiB of a 117 MiB file came back. Adding one back needs a query
  that reads it, not a hunch. Pinned by
  `::test_the_unread_secondary_indexes_are_dropped_and_stay_dropped`, which also pins that the
  `DROP`s live in `_ensure` — every statement in `SCHEMA` is `IF NOT EXISTS`, so removing them
  there alone would have left the live database carrying both for ever.
  Two things made this hurt more than a slow page: `/lib/list` is the filter box's keystroke
  handler, so every keypress inside `Fiction` re-ran all 18 s of it; and the handlers are
  `async def` over blocking `sqlite3`, so those seconds blocked the event loop and the dock's
  SSE stream with it. A threadpool would have hidden this rather than fixed it.
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
  That rule only fires when a read comes back *empty*, and an unreachable device produces
  no read at all to blank the bolt with — s4l sat five days at `100%` beside a bolt
  claiming a charger nothing had been able to ask about — so `DeviceView.bolt_class` draws
  none on a red row. `offline` is a statement about the reading's age and not merely about
  the dot: it needs `sleeping_window` (1800s) since `reach.last_ok`, and the readings come
  back on the same ssh as the connect, so red means the charge state is at least half an
  hour old. Amber `sleeping` keeps its bolt — under half an hour a charger it was on is
  very probably still under it, and the percentage beside it is no fresher. `battery_note`
  moves to the past tense there rather than dropping the clause, because the tooltip is
  still the only thing that can tell `None` from `"unplugged"`. Pinned by
  `::test_an_offline_row_draws_no_bolt` and
  `::test_an_offline_tooltip_stops_claiming_the_present`, with the sleeping half asserted
  beside each. `test_pressing_test_reports_the_charger` now fakes the TCP connect as well
  as the ssh, or the row it checks comes back red and the bolt it asserts is the fixture's.
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
- **`--scale` is a real page zoom, and three breakpoints are derived from it by hand.**
  `zoom: var(--scale)` on `html` (`app.css`), 1.21 on the desktop. Every px length in the
  file mirrors the design bundle's Tailwind scale 1:1, so the alternative was rewriting 412
  values, and scaling only the fonts would leave rows, gutters and icons behind. It went
  1.08 -> 1.21 (about 112%) because a 28in 4K monitor turned portrait needed Chrome's
  per-site zoom at 125% to be readable, which is the app failing to size itself; measured on
  the live page at 2156 CSS px, the card title renders 22.0px against 19.7px before.
  **A media query is matched against the real viewport and cannot read a custom property**,
  so the three breakpoints derived from `--scale` do not follow it and must be changed with
  it: **1089** (= 900 x scale, where the rail leaves the flow and the Library stacks),
  **1090** (the tablet band's floor, its matched pair) and **1520** (>= 1248 x scale, where
  the device and jobs rows stack). Forget one and you get a band where the layout has
  stopped stacking and cannot lay out either -- the device row carried exactly that for
  months at 1281-1348, written down in `TODO.md` until this change forced the fix. Each is
  now recomputed from the stylesheet by a test:
  `tests/test_battery.py::test_the_rail_breakpoint_follows_the_zoom`,
  `::test_the_stack_breakpoint_clears_the_row_floor` and
  `::test_the_file_grid_stacks_before_it_runs_out_of_panel`. The touch value is untouched at
  1.35, and the tablet zoom's 1280 ceiling with it.
- **`--scale` has a second value, and the tablet regime is a third layout.** A 10" tablet
  is ~150 CSS px per inch against a monitor's ~96 — 184 under Chrome's "Desktop site",
  which widens the layout viewport to 980 CSS px and landed 8px above the *old* 972px
  breakpoint that would have taken the rail out of the flow. So the fleet's own tablets got
  the desktop layout at half size with 190px of nav still in it. (At 1089 that particular
  case is now inside the block, and the band below owns the 1090–1280 landscape one.)
  `app.css` raises `--scale` to
  1.35 for touch screens up to 1280px and repeats the 1089px block's rail rules for the band
  above 1089, where the rail is still in flow; the two are a wash on content width and a
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
- **In portrait that same tablet is 800 CSS px, so the zoom lands on the *phone* block.**
  The 1089px rules were drawn for a 412px screen, and two of them are wrong once `--scale`
  1.35 is on top: one card column (the two-column rule lives in the 1090–1280 band and so
  fires only in landscape), and a 44px touch minimum multiplied into 59.4 CSS px — 0.40" on
  a Nexus 10 (800 CSS px across a 5.33" edge is 150 to the inch) and 0.47" on an S4 (700
  across 5.56" is 126), against the 0.27" a thumb needs. The 44 is a *physical* rule, so it
  is divided by the zoom that follows it: 33 x 1.35 = 44.6, in a `max-width: 1089px` + touch
  block that repeats the zoom band's two clauses so it is on exactly where the zoom is. Type
  is deliberately not in it — the same arithmetic puts it at 0.112" and 0.134" against a
  desktop's 0.141", so `font-size` never appears there and
  `::test_the_touch_minimum_survives_the_zoom` asserts it does not. The cards take a floor
  rather than a third breakpoint — `minmax(230px, 1fr)`, so `auto-fill` gives both tablets
  two columns and a phone one (`::test_a_tablet_in_portrait_fits_two_cards`).
- **A tablet's CSS width is a user setting, so a fallback's floor must be measured on the
  thing it holds.** Two 10" tablets, both 2560x1600: a Nexus 10 is exactly 800 CSS px, and a
  Galaxy Tab S4 is **700**, because Samsung's Screen zoom moves the density and dpr with it
  (2.286 against 2.0 — measured off two screenshots against the 16.2px of `.view` padding
  both had to draw; Chrome for Android's own page-zoom menu moves it again, 80% taking the
  S4 to 875). Both first attempts here were tidy numbers borrowed from somewhere roomier —
  a 256px card floor from the desktop's narrowest three-up, a 768px two-up breakpoint from
  "tablet" — and both landed in the 100px between the two tablets, so the Nexus 10 got the
  new layout and the S4 kept the old one. The floors that replaced them are measurements of
  the content: a card's own box overflows below 220px (forced narrower and narrower against
  the running service, `tools/shot.py --eval` on `scrollWidth`), and the stacked row's widest
  untooltipped value is the 124px address. Size a fallback from what it must hold, never from
  the screen you think it is on.
- **The stacked device row is the fallback for every narrow screen, not a phone layout.**
  It is what a 27" 2.5K monitor turned portrait gets — 1152 CSS px at 125%, against the
  1510px a single-line row needs (1022px of track floors + 190 rail + 36 gutter, x `--scale`)
  — and nine label/value lines per device put nine devices past a 2560px screen one at a
  time. Above 660px it goes two-up, five lines, and the pairs are the row's own grouping:
  Device with Type, Address with Target, Storage with Battery, and the two ages side by side
  where they already belong. Actions spans, its floor being 320px of failure text plus three
  buttons. 660 is the address, which is the widest value here with no tooltip to fall back on
  — 124px at 11.5px mono — plus the label, the 8px gap and 2x12px of padding, twice, in the
  binding regime of a touch screen under 1089px: `2 x (76 + 8 + 24 + 124) + 24 = 488` layout
  px is 660 real px at `--scale` 1.35. The label is 76px here and 92px when it has a row to
  itself, because 92 was never its text — "LAST SEEN" is 60.7px — and two-up cannot afford
  31px of dead space per column. Pinned by
  `tests/test_battery.py::test_the_stacked_row_pairs_its_cells`.
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
  The Settings tick that hides the card's button row is the one exception, and it is a
  *whole-fleet* exception on purpose. It first exempted a red or syncing card, on exactly
  the reasoning above — and that was wrong here, where six of ten nodes are red at any
  moment: the tick left the buttons on most of the cards and was reported as doing nothing
  at all, on two browsers. A preference that holds only for the cards you were not looking
  at is not a preference. Neither action is lost, only moved, which is what makes the tick
  affordable: Rescan in the topbar re-probes every node, and a running job keeps its Abort
  in the dock. Pinned by
  `tests/test_card_prefs.py::test_the_tick_takes_the_buttons_off_every_card`,
  `::test_a_syncing_card_drops_them_too_and_keeps_its_badge` and
  `::test_abort_is_still_reachable_with_the_buttons_off`, which checks the claim in that
  last sentence rather than assuming it.

- **The Library's position fills the rail *link*, and never reinterprets a bare
  `/library`.** `libnodes/libpos.py`, written by `/library` and `/lib/pane`, read in
  `deps.base_context` as `library_href`. Same bug as `libnodes_view` from the other page:
  the rail is a plain `href` with no query string, the Library's position lives only in
  `?p=`, so walking to Devices and back landed at `/Books` however deep you were. What the
  cookie must *not* do is change what a URL means — it fills in the link
  (`/library?p=Fiction%2FLeonid-Perov`), so a typed URL, a bookmark and Back all still say
  what they say, and the address bar can never disagree with the listing. `library_context`
  overwrites `library_href` with the path on screen, because `base_context` read a cookie
  that is one navigation behind. Three things differ from `libnodes_view` and each is
  load-bearing. A bare `/lib/pane` records the **root** rather than writing nothing: that
  rule exists so arriving by the rail cannot pin a default the handler *guessed*, and there
  is no guess here — a bare call is the breadcrumb's root link, and the root is then where
  you are. The value is **validated against the index on every read**, because `"grid"`
  cannot go stale and a path can: renamed, deleted, or hand-edited to `.data`, all of which
  `index.require` answers with a 400 — which would take out `/devices` and `/jobs` too, for
  a rail link nobody clicked, so `resolved_pos` swallows `PathError` and forgets instead.
  And it is **percent-encoded**, for the `cardprefs.SEP` reason reached from a worse angle:
  a path can hold a comma, a space or Cyrillic, none of them cookie-octets. `/lib/list` and
  `/lib/selection` write nothing — filtering, sorting and ticking do not move you, and the
  filter fires on every keystroke. Pinned by `tests/test_routes.py`
  `::test_the_library_position_survives_a_trip_to_the_devices_page`,
  `::test_going_back_to_the_root_is_remembered_too`,
  `::test_a_remembered_directory_that_no_longer_exists_is_forgotten` and
  `::test_a_path_that_is_not_a_cookie_value_still_survives`.
- **A GRID card's fields are per browser, and the cookie names what is *hidden*.**
  `libnodes/cardprefs.py`, ticked at `/settings`, read once in `deps.base_context` so all
  six paths that render a card get it — `/devices/grid`, `/device/{id}/card`,
  `/device/{id}/probe` and the Test dialog's out-of-band include among them; the last of
  those is the swap that fails silently. The polarity is the load-bearing part: no cookie
  then means the card as it always was, and a field added later is visible by default
  rather than silently missing from every card. Nothing is hidden with CSS — `.card` is
  `display: flex`, which outranks an `.is-hidden` class and the UA's `[hidden]` alike, so
  the template omits the block and a rendered-HTML test can see it. The separator is a
  **dot**: a comma is not a cookie-octet, so `set_cookie` quoted the value and escaped it
  to `"addr\054seen"`, which comes back unsplittable and reads as "nothing hidden".
  TABLE is untouched and must stay so — its nine tracks, nine `<thead>` cells and nine
  `data-label` cells have to agree in number, which is the entry above.
- **`Settings.concurrency` defaults to 1, and the default is not the deployment.** The 1 is
  a property of an unknown host: on the Pi 3 the NIC shared the USB 2.0 bus with the library
  disk, so two transfers went half as fast each. pi5 puts the library on PCIe NVMe and the
  NIC on its own bus, so `deploy/libnodes.service` sets `LIBNODES_CONCURRENCY=3`. Leave the
  code default at 1 — the remaining shared resource is Wi-Fi airtime across the six wireless
  nodes, which is why the unit says 3 rather than "unbounded", and a host that has not
  declared itself should not assume either.
- **A device id reaches the DOM through `Device.dom_id`, never raw.** htmx spans two worlds
  with the same string: `hx-target="#node-<id>"` is a querySelector, and — the part that
  cannot be escaped around — an out-of-band swap builds its own selector as
  `"#" + element.getAttribute("id")` and runs *that* through `querySelectorAll` (verified in
  the vendored 2.0.4, `oobSwap`). So the id **attribute** has to be selector-safe, not just
  the targets. `sigmaai.au` found it: `#scan-status-sigmaai.au` parses as the id
  `scan-status-sigmaai` plus the class `au`, matches nothing, and htmx answers an
  unresolvable target by firing `htmx:targetError` and **not sending the request** — so Scan
  device on that node did nothing at all, and the access log had no POST in it to say why.
  Row Retry, card Retry and the Test dialog's out-of-band row refresh were broken the same
  way. Not fixed by renaming the node: the id is the key in `manifests.db`, `jobs.db` and
  `probe.json`, and it is the hostname. `dom_id` folds anything outside `[A-Za-z0-9_-]` to a
  dash and is used for every `id=` and every `#`-selector; the **URLs keep the real id**.
  `DevicesFile` refuses two ids that fold to the same `dom_id`. Pinned by
  `tests/test_dom_ids.py`, which keeps a dotted id in its own fixture because the rest of
  the fleet is dot-free — which is exactly why nothing caught this.
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
- **An ssh *remote* command is the one thing that cannot be an argv list.** ssh joins
  everything after `user@host` with single spaces and hands the result to a shell on the
  far side, so a tidy argv list arrives **unquoted** and is re-split on whitespace. Pass
  exactly one element, quoted here with `shlex.join`/`shlex.quote` — which is what
  `probe._readings_script` has always done, and what `jobs._ssh_command` now does for the
  pull's snapshot and cleanup. Getting it wrong cost job #18: the snapshot script went out
  as a five-element list and came back `SyntaxError: Expected one or more names after
  'import'` from python plus `bash: -c: line 2: syntax error near unexpected token '('`,
  because the newline-separated one-liner had been re-split into four commands. The job
  log is no help and actively misleads — `_stream` writes the argv back out shlex-quoted,
  so it printed the command as it *should* have been sent. `_SNAPSHOT_PY` is therefore also
  newline-free, belt and braces. Pinned by
  `tests/test_upstream.py::test_a_remote_command_is_one_already_quoted_word` (asserting the
  *shape* — one element after the destination — because the contents were correct
  throughout) beside `::test_a_remote_command_survives_the_shell_that_will_re_split_it`,
  which parses what we send the way bash would and compiles the script that comes out.
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
