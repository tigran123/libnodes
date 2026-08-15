# LibNodes

Pushes selected parts of a large book library out to a fleet of reading devices —
Kobo/KOReader over dropbear, Android/Termux, plain Linux hosts — over `ssh` + `rsync`,
and keeps a cached view of what each device already holds.

Built to run on a Raspberry Pi 3 against a 250 GB library, so the constraints are real:
one transfer at a time, never walk the library in a request, and 60 MB of RSS.

FastAPI + Jinja2 + HTMX. No client framework, no build step, no Node, no ORM.

```bash
uv pip sync requirements-dev.txt
uv run uvicorn libnodes.main:app --reload      # http://127.0.0.1:8000/devices
uv run pytest
```

Deploying to a Pi: [`deploy/README.md`](deploy/README.md).

## The one thing to know about the library

It is **content-addressed**. Every book in the browsable tree is a symlink into a vault
of blake2b-named blobs:

```
/Books/Science/Philology/Scheffer-…-2022.pdf -> ../../.data/b50cc577e28f4d3e…
```

`find -type f` therefore returns zero books, and a plain `rsync -a` copies dangling
symlinks that an e-reader will happily display as zero-byte files. Two consequences run
through the whole codebase:

- **`rsync -L` is mandatory**, and not configurable. Getting it wrong produces a
  transfer that reports success and delivers nothing.
- **The blob hash is a free content identity.** The index records it and manifests
  compare it, so "is the device's copy stale?" has an exact answer rather than an
  mtime/size guess.

If your library is an ordinary tree of real files, none of this hurts: `-L` on a
non-symlink is a no-op.

## The rsync flags belong to the program

`devices.yaml` describes *devices*, not rsync invocations. LibNodes builds the command
itself because it depends on the exact behaviour of the flags:

| Flag | Why |
|---|---|
| `-L` | the library is symlinks into a CAS vault |
| `-R` | a source keeps its path shape on the device |
| `--out-format=@%l\|%n` | file events are declared, so parsing is exact rather than a guess about which lines look like filenames |
| `--info=progress2,flist0,misc0,stats1` | one aggregate progress line; no chatter we would only have to filter |
| `-O` | otherwise rsync stamps every directory's mtime and reports each as touched |
| `--no-perms` | **only** where the target filesystem cannot store them |

That last one is why devices declare `fs:` (vfat, exfat, ext4, …) rather than a flag
list: the filesystem is the actual constraint, and device type is only a proxy for it.
A Linux host with an exFAT disk gets FAT treatment; an ext4 target keeps full archive
semantics. See `FS_PROFILES` in `models.py`.

Deliberately absent: `-h` (we format numbers ourselves), `%i` in the out-format (it
makes rsync log every unchanged file on a FAT target — 24,616 lines around 4 real
transfers, measured), and `--modify-window` (FAT's 2-second mtime granularity did not
materialise on the tested hardware).

## Adopting a device that already has the library

A device populated by other means is invisible to LibNodes, and worse, looks entirely
out of date: its files carry the mtimes of whenever they were copied, so rsync's
size+mtime check wants to re-send all of them. Two actions fix that, both under
**Actions** on the device row:

- **Scan** — `rsync --list-only` over the target, recording what is there. 20,782 files
  in 35 s on a real Android device over WiFi.
- **Adopt** — a `--size-only` run. rsync skips files whose size already matches but
  still repairs their timestamps, so nothing is transferred and every later sync is
  incremental. Measured: 266 GB library reconciled in 76 s, 258 MB sent (the files
  genuinely missing), after which a normal sync itemises nothing.

Every action shows the command it will run, because an action whose effect you have to
infer from its label is a bad action.

## Layout

| Module | Responsibility |
|---|---|
| `config.py` | settings; `devices.yaml` load, validate, watch |
| `models.py` | the `devices.yaml` schema, filesystem profiles, YAML→line mapping |
| `library.py` | SQLite index: the walk, the queries, the path guard |
| `probe.py` | cached reachability and free-space probes, with backoff |
| `jobs.py` | job store, rsync runner, progress parser, failure hints |
| `manifests.py` | what each device holds; `PRESENT ON` and staleness |
| `scan.py` | remote listing, and recovery of mangled filenames |
| `watch.py` | inotify on `devices.yaml`, so edits appear without polling |
| `host.py` | `/proc` telemetry for the rail footer |
| `yamlview.py` | token colouring for the read-only config view |
| `routes/` | one module per view; full pages return the shell, everything else a fragment |

`static/app.css` is hand-written, not generated: its custom-property names mirror the
Tailwind theme of the design bundle this UI was built from, so the two stay
cross-readable. That bundle is not in the repository — it has been implemented, and the
code is the artefact. Fonts are self-hosted (`static/fonts/regenerate.py` refetches
them) so the machine works on an isolated LAN.

## Design rules worth not breaking

- **Nothing blocks.** A push returns immediately and streams into the dock over SSE. The
  one blocking dialog in the app is the offline-push confirmation.
- **Requests never probe a device.** A background task writes reachability into a dict;
  handlers read it. Otherwise six sleeping e-readers become a six-second page load.
- **Never walk the library in a request.** The tree and file list come from the index; a
  rebuild runs on one background thread and publishes by atomic rename.
- **One rsync at a time.** On a Pi the NIC shares the USB bus with the library disk, so
  two transfers do not go twice as fast — they go half as fast each.
- **Every template except `base.html` and the page templates must render standalone.**
  That is the HTMX contract; `test_fragments_render_standalone` enforces it.

## Testing notes

263 tests, no network required. Two fixtures encode lessons that cost real debugging:

- `tests/data_rsync_human.log` — verbatim output from a real transfer. `-avhP` includes
  `-h`, so rsync reported `734.38K` rather than `1,234,567`, and a parser tested only
  against hand-written fixtures reported 0 bytes for every genuine transfer.
- The library fixture builds a real CAS tree — symlinks into a blob directory, including
  a dangling one — because that shape is what most of the interesting behaviour depends
  on.

When a change is visual, assert on computed style or a screenshot. Asserting that
`element.hidden` was set once passed happily while the UI was visibly broken, because a
`display: flex` rule outranks the UA's `[hidden]`.

## Status

Working: devices with live reachability, library explorer with instant filter, pushes to
one or many devices, live SSE progress dock, job history with logs, scan/adopt, dry runs,
read-only `devices.yaml` view with inotify refresh, light and dark themes, responsive
layout for tablets.

Not built yet: the device configuration drawer (edit `devices.yaml` by hand for now —
changes take effect immediately), presets, wake-on-LAN.
