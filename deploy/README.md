# Running LibNodes on pi5

The host is **pi5** — `192.168.1.32`, aarch64, Debian 13 (trixie), Raspberry Pi 5 Model B
Rev 1.1, 4 cores at `arm_freq=2800`, 15 GB RAM, 1 Gbps ethernet. `ssh pi5` logs in as
`tigran`; the tree lives at `/home/tigran/libnodes` and the library at `/Books`. Both are on
one **931 GB NVMe** — a WD Blue SN570 1TB, 917 G ext4 `rw,noatime`, 282 G used. Nothing is a
bind mount and nothing needs one.

The drive runs at **PCIe Gen2 x1** (`dtparam=pciex1_gen=2` in `/boot/firmware/config.txt`;
the link reports 5.0 GT/s x1, the drive itself is 8.0 GT/s x4 capable and the Pi 5 only
offers the one lane). Measured **430 MB/s** sequential — `dd iflag=direct` on an 824 MB
blob, 2026-08-19 — against ~210 MB/s before, which is Gen1. That ceiling is the reason
`LIBNODES_CONCURRENCY=3` is safe here at all; see §pi5 resource notes.

**This is also where development happens.** The tree the service execs *is* the working
tree, so there is nothing to push: edit it, `sudo systemctl restart libnodes`, look at
`http://pi5:8090/`. `CLAUDE.md` §Commands is the loop.

It was a Raspberry Pi 3 (`ssh pi`, `/home/pi/libnodes`, armv7l, 923 MB) until 2026-08-17.
That machine is still up and still holds its copy, but its `libnodes` unit is **disabled and
inactive** and 8090 there is connection-refused — checked 2026-08-19. That is what retires
the "two instances can reach one fleet" problem, and it retires it as a fact rather than a
rule: nothing in the code stops two hosts pushing to one device, so if that unit is ever
started again, drive transfers from one host at a time.

## Restarting

```bash
sudo systemctl restart libnodes && curl -s localhost:8090/healthz
```

About a second, and no password: `/etc/sudoers.d/libnodes` carries
`tigran ALL=(root) NOPASSWD: /usr/bin/systemctl restart libnodes` — that one command only.
Restarting is cheap partly because `var/probe.json` is written at shutdown and read at
start, so the fleet's dots survive it; a restart used to blank every node that happened to
be asleep, and on this fleet the Kobo can be asleep for days.

Dependencies, when `requirements.txt` changes:

```bash
uv pip sync requirements-dev.txt        # a superset of requirements.txt; see CLAUDE.md
```

That single venv is both the service's and the test suite's — `ExecStart` names
`.venv/bin/uvicorn` — so sync the **dev** file, never the runtime one alone, and never a
list that could drop `uvloop` or `httptools`.

## Deploying somewhere else

`./deploy/deploy.sh` is still here, but it is now only for pushing this tree to a
*different* host. Run on pi5 it refuses: its own target is this tree, and it syncs with
`--delete`. It syncs the source (excluding `var/`, `.venv/`, `tests/`,
`design_handoff_libnodes/`), runs `uv pip sync requirements.txt` there, restarts the service
if the unit is installed, then polls `/healthz` for 30 s so a push that starts a broken
build fails at the push rather than looking fine.

`LIBNODES_HOST`, `LIBNODES_DEST`, `LIBNODES_UV` and `LIBNODES_PORT` override the four
targets. Note the `uv` paths differ per host: `~/.local/bin/uv` is what `PATH` picks on pi5
(0.12.5; the root-owned `/usr/local/bin/uv` is older, and is what `LIBNODES_UV` still
defaults to), and `/home/pi/.local/bin/uv` on the old Pi.

## The service, already installed

`/etc/systemd/system/libnodes.service` is a copy of `deploy/libnodes.service`, enabled and
active. Editing the file in the tree changes nothing until it is copied over — that copy and
the `daemon-reload` after it are the one part of this that needs full root, so it belongs to
the machine's sysadmin, not to a deploy:

```bash
sudo cp deploy/libnodes.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now libnodes
```

`sudo systemctl restart libnodes` on its own needs no password (see §Restarting); everything
above does.

## `var/` is this host's, and it is live

`deploy.sh` excludes it deliberately, and on pi5 there is a second reason: the running
service holds `index.db`, `jobs.db`, `manifests.db` and `probe.json` **open**. A second
LibNodes started in this tree picks up `LIBNODES_STATE_DIR=<project>/var` by default and
then both processes write those files and both ssh the whole fleet. `var/shot-profile/` is
`tools/shot.py`'s chromium profile, holding its login cookie.

`var/devices.yaml` is the real fleet: 9 devices, hand-edited, hot-reloaded. It was brought
across from the old Pi once, by hand — and since pi5 now has a key there, from the pi5 side:

```bash
ssh pi 'cat ~/libnodes/var/devices.yaml' > ~/libnodes/var/devices.yaml
```

The index, jobs and manifests were not: they rebuild. A first start therefore shows
`PRESENT ON` blank everywhere until each device has been scanned once.

One thing in that file remembers the old host and is correct as it stands: `nexus10`'s
target is `/sdcard/koreader/cache/webbrowser/192.168.1.33` — a path **on the tablet**, where
the books already are; renaming it to `…/.32` would orphan them and force a full re-push.

## Outbound ssh from pi5

Every push and probe is `ssh` from pi5 to a node, so pi5's `~/.ssh/config` is part of the
deployment. It already carries `ControlMaster auto` / `ControlPersist 3600` — which
`libnodes/probe.py` and the rsync `-e ssh` rely on for connection reuse — and the six
port-2222 root entries for the termux and Kobo nodes. `thinkpad` (the `sync_mode: mirror`
node) has **no `~/.ssh/config` entry** and needs none: it is `192.168.1.3` in `/etc/hosts`,
and `devices.yaml` supplies its `user: tigran` and `port: 22` on the command line, which is
where every push and probe gets them anyway. It was off — no route — when this was checked
on 2026-08-19, so mirror behaviour could not be re-verified against it that day.

pi5's public key is installed on every registered node. Host keys are not pre-seeded, but
`StrictHostKeyChecking=accept-new` in `ssh_argv` handles the first connection.

## Looking at the UI on a host with no display

pi5 has no X and no Wayland session, and development happens over ssh — so nobody, human or
agent, sees the UI by opening a window here. Two ways it gets seen:

- **From your own machine**, `http://pi5:8090/` over the LAN. This is the real thing, and the
  password applies.
- **`tools/shot.py`**, which drives the installed chromium (151.0.7922.137) headless over the
  DevTools protocol and writes a PNG — or answers a question about the rendered page:

```bash
uv run tools/shot.py /devices                              # writes shots/devices.png
uv run tools/shot.py /devices --show                       # ...and opens it on your screen
uv run tools/shot.py /library shots/lib.png --full --theme light
uv run tools/shot.py /devices - --eval \
    "getComputedStyle(document.querySelector('.trow')).gridTemplateColumns"
```

`--show` hands the PNG to ImageMagick's `display`, which only means anything inside an
X-forwarded session. Connect as `ssh -S none -4 -Y pi5`: `-Y` asks for forwarding (sshd here
allows it and `xauth` is installed, which is the part that silently breaks it when absent),
and **`-S none` is the load-bearing flag** — `~/.ssh/config` sets `ControlMaster auto` for
`Host *`, so without it the session rides a master opened without forwarding and you get no
`DISPLAY` and no explanation. Nothing outbound is affected: the connection reuse
`libnodes/probe.py` and rsync depend on is pi5 → node, not this.

The first argument is the **route**; the file is the second and is optional. `shot.py
devices.png` therefore photographs the route `/devices.png` — which the tool now refuses by
name, having once done it silently and exited 0.

Three things about it are measured rather than chosen. It passes a private
`--user-data-dir` (`var/shot-profile/`) because with Chromium's default profile the *same*
screenshot takes **25.7 s** instead of **0.7 s** — the time goes on failing GCM registration
against Google before the page will render. It waits for htmx to go quiet rather than for
the load event, because every page here finishes assembling itself over HTMX. And it drives
CDP rather than `chromium --headless --screenshot=…`, because the one-liner can neither carry
a login cookie nor report a computed style.

The password: `$LIBNODES_SHOT_PASSWORD`, or `~/.config/libnodes/shot-password` (mode 600),
neither of which is in the tree. It logs in by filling the real form once; `remember` is
checked by default and `LIBNODES_SESSION_DAYS` is 30, so the profile's cookie then serves for
a month. With no password available it still runs and photographs the login card.

## There is no reverse proxy, and that is the decision

**LibNodes is reachable on the LAN only**, at `http://pi5:8090/`. uvicorn binds
`0.0.0.0:8090`, the router does not forward that port, and no nginx vhost proxies it.

`deploy/libnodes.nginx` is in the tree but **is not installed** — it is the withdrawn
public vhost, kept so that reinstating one is an edit rather than a rediscovery. Its header
carries four things to check first, including the one that will bite: its no-SNI
`default_server` block is already live on pi5 under a different filename, and two default
servers on `:443` is a config error.

It was briefly public, at `https://proxyai.ddns.net/` behind nginx with a valid certificate
and an `allow <home-ip>; deny all;` rule, on 2026-08-17. That lasted about an hour and was
withdrawn the same evening. The reason is worth keeping, because the setup looked safe:

- **The allowlist was pinned to a rotating address.** `allow 78.147.193.57` was the house's
  public IP — and the whole reason this box runs DDNS is that the IP changes. When it does,
  the lockout is the harmless half; the other half is that whoever the ISP hands the address
  to next inherits the allow rule. A guard that silently transfers to a stranger is worse
  than no public door, in front of a panel that starts transfers, deletes job history and
  drives `rsync --delete` at the mirror node.
- **The name was found in 17 minutes.** A scanner fetched `/.git/config` from
  `proxyai.ddns.net` by name at 23:02, seventeen minutes after the vhost went live. 63
  distinct hostile source IPs were blocked across this host's vhosts that day.
- The upside was small: pi5 does the rsync and pi5 is on the LAN, so a remote push would
  genuinely have worked — but the device is normally in your hand when you want it synced.

`proxyai.ddns.net` now resolves to a host that answers nothing (see below). If LibNodes is
ever exposed again, note that **it only works at the URL root** — roughly 64 template URLs
are absolute (`hx-get="/jobs/dock"`), `asset()` emits `/static/…`, and `AuthMiddleware`
matches `scope["path"]` against exact strings in `OPEN_PATHS`. A sub-path such as
`/libnodes` is a feature (`LIBNODES_URL_PREFIX`), not an nginx setting. And `/jobs/stream`
would need `proxy_buffering off`: it is Server-Sent Events, and a buffering proxy makes the
sync dock appear to freeze mid-transfer.

`--host 0.0.0.0` in the unit is therefore load-bearing rather than lazy — it *is* the LAN
door. That also means the LAN is the whole attack surface, and `LIBNODES_PASSWORD` is the
only thing guarding it. See §Access.

### What else is on pi5's nginx

`ulib.ddns.net` on 443 proxies the urantia-library webapp on `127.0.0.1:8000`, with its own
Let's Encrypt cert, a per-IP rate limit on `/api/(login|register)`, and a public-IP
allowlist. A shared `listen 80` block 301s every name to https — though port 80 is not
forwarded either, so that redirect only ever runs for requests originating on the LAN.

`/etc/nginx/sites-available/no-sni-default` is a leftover of the brief public experiment
that was kept on purpose, and is not managed by this repo. A client connecting by IP sends
no SNI, so nginx falls back to the default server for the socket; without an explicit one
that is whichever `listen 443 ssl` block was parsed first, by filename ordering. It now
returns `444` — connection closed, nothing served — which is also where `proxyai.ddns.net`
lands. It still needs a certificate, because the handshake must complete before nginx can
discover there was no SNI.

Port 8080 is free on pi5 — the plain `/Books` file browser that occupied it on the old Pi
was not brought across.

## pi5 resource notes

- **`LIBNODES_CONCURRENCY=3`**, set in the unit. The code default is `1` and stays `1`: that
  is the right default for a host whose disk and NIC might share a bus, as the Pi 3's USB
  2.0 did. On pi5 the library is on PCIe NVMe and the NIC is separate, so the host is no
  longer the constraint; what is still shared is Wi-Fi airtime across the six wireless
  nodes, which is why it is 3 and not unbounded.
- A full reindex walks 24,621 entries (20,782 files, 248 GiB) in **0.61 s** on a single
  background thread — measured 2026-08-19 on the Gen2 NVMe, three consecutive runs at
  0.61/0.64/0.61 s. It was 1.0 s on the previous drive and ~29 s on the Pi 3. It is safe to
  run during a transfer.
- Expect ~65 MB RSS (67.4 MB measured, idle, index loaded, after four hours up).
  `MemoryMax=1G` in the unit is a leak-catcher, not a budget.
- The suite is **514 tests in ~20 s** here (`uv run pytest`), and needs no network. It was
  ~17 s on the x86_64 workstation, which is the closest thing to a slowdown this move cost.
- Logs are capped at `LIBNODES_LOG_RETENTION` (200) files under `var/logs/`.

## Environment

Every setting is a `LIBNODES_`-prefixed environment variable or a line in `.env`. The unit
sets four of them (`LIBRARY_ROOT`, `STATE_DIR`, `CATALOG_DB`, `CONCURRENCY=3`) and reads
`LIBNODES_PASSWORD` from `/etc/default/libnodes`; everything else runs at its default:

| Variable | Default | Notes |
|---|---|---|
| `LIBNODES_LIBRARY_ROOT` | `/Books` | the library root |
| `LIBNODES_STATE_DIR` | `<project>/var` | index, jobs, manifests, logs, devices.yaml, probe.json |
| `LIBNODES_CATALOG_DB` | `/Books/.data/db/lib.db` | read-only; optional |
| `LIBNODES_CONCURRENCY` | `1` | simultaneous rsyncs; the pi5 unit sets `3` |
| `LIBNODES_PROBE_INTERVAL` | `10` | seconds between reachability sweeps |
| `LIBNODES_PROBE_BACKOFF_MAX` | `300` | ceiling on the per-device backoff — in effect, how long a recovery can go unnoticed when nobody has a Devices page open |
| `LIBNODES_PROBE_BACKOFF_WATCHED` | `30` | the same ceiling while a Devices page is polling |
| `LIBNODES_WATCH_WINDOW` | `150` | how long after the last Devices request the fleet still counts as watched — above 60s on purpose, since a backgrounded tab polls only once a minute |
| `LIBNODES_REINDEX_INTERVAL` | `1800` | seconds; `0` disables the periodic rebuild |
| `LIBNODES_LOG_RETENTION` | `200` | job logs kept on disk |
| `LIBNODES_PASSWORD` | *(empty)* | the shared login password; **empty means no login at all** |
| `LIBNODES_SESSION_DAYS` | `30` | how long "stay signed in" lasts |

## Access

LibNodes binds `0.0.0.0`, so without a password every host on the LAN can start transfers,
abort them and delete history. Being LAN-only is not a substitute: the LAN is not a trust
boundary — it holds six of the fleet's own nodes, and anything that joins the Wi-Fi is on
it. With no reverse proxy in front, this password is the *only* guard.

**One is set.** `/etc/default/libnodes` exists, `root:root` `0600`, and the last start logged
no warning — which is how you check, since the warning below is the only signal. Setting one
from scratch:

```bash
sudo install -m 600 /dev/null /etc/default/libnodes
echo "LIBNODES_PASSWORD=your-password-here" | sudo tee /etc/default/libnodes >/dev/null
sudo systemctl restart libnodes
```

`libnodes.service` reads that file through `EnvironmentFile=-/etc/default/libnodes`. The
leading `-` makes it optional, so the unit still starts on a machine that has no such file.

**Do not put the password in `.env`.** That hazard is now hypothetical rather than routine —
nothing rsyncs over this tree any more — but it is one edit away from being live again, and
the failure is silent: `deploy.sh` syncs with `--delete` and does not
exclude it, so a `.env` written on pi5 is deleted by the next deploy and the lock silently
disappears. `/etc/default/libnodes` is outside `$DEST`, so rsync cannot reach it, and it
never enters git. Mode `600` keeps it away from any other user on the host.

With no password set the service still starts and serves normally, and logs

```
WARNING:  no LIBNODES_PASSWORD set - the UI is open to every host that can reach this port.
```

That warning is the only guard: `journalctl -u libnodes | grep LIBNODES_PASSWORD` after a
deploy is how you check the lock actually came back up.

Changing the password and restarting logs everybody out — the cookie is signed with a key
derived from the password, so old sessions stop verifying. There is nothing to clear.
