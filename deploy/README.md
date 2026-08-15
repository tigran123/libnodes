# Deploying LibNodes to the Pi

## Before the first deploy: make `~/libnodes` writable

`/home/pi/libnodes` is currently a **read-only** bind mount. From the Pi's `/etc/fstab`:

```
/ext/disk4/Kiwix          /Kiwix            none  defaults,ro,bind,nofail  0  0
/ext/disk4/work/libnodes  /home/pi/libnodes none  defaults,ro,bind,nofail  0  0
                                                           ^^
```

The `ro` looks like it was carried over from the `Kiwix` line above it — the `Books` and
`Audio` binds on the same disk both use `rw`. LibNodes cannot run against a read-only
tree: it writes `var/index.db`, `var/jobs.db`, `var/manifests.db`, `var/devices.yaml`
and `var/logs/`, and `uv pip sync` writes into `.venv/`.

Fix:

```bash
sudo sed -i 's#\(/ext/disk4/work/libnodes.*\)ro,bind#\1rw,bind#' /etc/fstab
sudo mount -o remount,rw,bind /home/pi/libnodes
touch ~/libnodes/.probe && rm ~/libnodes/.probe && echo writable
```

(The underlying `/ext/disk4` mount is already `rw`, so nothing else needs changing.)

## Deploy

```bash
./deploy/deploy.sh
```

Syncs the source (excluding `var/`, `.venv/`, `tests/`, `design_handoff_libnodes/`),
runs `uv pip sync requirements.txt` on the Pi, and restarts the service if installed.

## Install the service (once)

```bash
ssh pi 'sudo cp /home/pi/libnodes/deploy/libnodes.service /etc/systemd/system/ \
        && sudo systemctl daemon-reload && sudo systemctl enable --now libnodes'
```

Then LibNodes is on `http://pi:8090/`.

## nginx: optional, and probably unnecessary

uvicorn binds `0.0.0.0:8090`, so `http://pi:8090/` already works. A reverse proxy in
front of it is **not** needed to make LibNodes reachable, and adding one just to forward
one port to another buys nothing.

`deploy/libnodes.nginx` exists for the one case that does pay: serving LibNodes on
**port 80** (`http://pi/`, alongside the plain file browser on `:8080`) and serving
`/static` straight off the disk so uvicorn never spends a request on 600 KB of fonts.

If you install it, note two things:

- **`proxy_buffering off` on `/jobs/stream` is mandatory.** That endpoint is Server-Sent
  Events; with buffering on, nginx holds the fragments and the sync dock appears to
  freeze mid-transfer.
- Afterwards, change `--host 0.0.0.0` to `--host 127.0.0.1` in `libnodes.service` so
  8090 stops being a second public door.

### What is already on the Pi's nginx

`urantia-library-plain.nginx` serves `:8080` as an autoindex file browser over `/Books`,
with `disable_symlinks off` so it follows the CAS symlinks to real bytes. It denies
`/urantia-library/`, `CLAUDE.md`, `GEMINI.md` and dotfiles — the same exclusion set as
LibNodes' `SKIP_TOPLEVEL`, arrived at independently. LibNodes on :8090 does not touch it.

## Pi resource notes

- **One rsync at a time** (`LIBNODES_CONCURRENCY=1`). The NIC shares the USB 2.0 bus
  with the library disk; two transfers halve each and spike load.
- A full reindex walks ~20.8k entries in **~29 s** on the Pi and runs on a single
  background thread. It is safe to run during a transfer, but it does compete for the
  same disk.
- Expect ~60 MB RSS. `MemoryMax=320M` in the unit is a leak-catcher, not a budget.
- Logs are capped at `LIBNODES_LOG_RETENTION` (200) files under `var/logs/`.

## Environment

Every setting is a `LIBNODES_`-prefixed environment variable or a line in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `LIBNODES_LIBRARY_ROOT` | `/Books` | the library root |
| `LIBNODES_STATE_DIR` | `<project>/var` | index, jobs, manifests, logs, devices.yaml |
| `LIBNODES_CATALOG_DB` | `/Books/.data/db/lib.db` | read-only; optional |
| `LIBNODES_CONCURRENCY` | `1` | simultaneous rsyncs |
| `LIBNODES_PROBE_INTERVAL` | `10` | seconds between reachability sweeps |
| `LIBNODES_REINDEX_INTERVAL` | `1800` | seconds; `0` disables the periodic rebuild |
| `LIBNODES_LOG_RETENTION` | `200` | job logs kept on disk |
