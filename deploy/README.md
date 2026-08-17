# Deploying LibNodes to pi5

The host is **pi5** — `192.168.1.32`, aarch64, Debian 13, 4 cores, 15 GB RAM, 447 GB NVMe,
1 Gbps ethernet. `ssh pi5` logs in as `tigran`; the tree lives at `/home/tigran/libnodes`
and the library at `/Books` on the NVMe. Nothing is a bind mount and nothing needs one.

It was a Raspberry Pi 3 (`ssh pi`, `/home/pi/libnodes`, armv7l, 923 MB) until 2026-08-17.
That machine is still running its own copy and has not been touched — which means two
instances can reach the same fleet. Nothing in the code prevents both pushing to one device
at once, so drive transfers from one of them at a time.

## Deploy

```bash
./deploy/deploy.sh
```

Syncs the source (excluding `var/`, `.venv/`, `tests/`, `design_handoff_libnodes/`), runs
`uv pip sync requirements.txt` on pi5, restarts the service if installed, and then polls
`/healthz` for 30 s — so a deploy that starts a broken build fails at the deploy rather
than looking fine. The `.venv` is never shipped: the workstation's is x86_64 and pi5 builds
its own aarch64 one.

`LIBNODES_HOST`, `LIBNODES_DEST`, `LIBNODES_UV` and `LIBNODES_PORT` override the four
targets if you ever need to deploy somewhere else. `uv` is at `/usr/local/bin/uv` on pi5
and at `/home/pi/.local/bin/uv` on the old Pi.

## Install the service (once)

```bash
ssh pi5 'sudo cp /home/tigran/libnodes/deploy/libnodes.service /etc/systemd/system/ \
         && sudo systemctl daemon-reload && sudo systemctl enable --now libnodes'
```

Then LibNodes is on `http://pi5:8090/` from the LAN.

## `var/` does not travel

`deploy.sh` excludes it deliberately: `var/` holds the index, jobs, manifests, logs and
**`devices.yaml`**, all of which are genuinely per-machine. The workstation's `devices.yaml`
is an uncorrected seed and must never be copied over a real one. The fleet definition was
brought across from the old Pi once, by hand:

```bash
ssh pi5 'mkdir -p ~/libnodes/var'
ssh pi 'cat ~/libnodes/var/devices.yaml' | ssh pi5 'cat > ~/libnodes/var/devices.yaml'
```

The index, jobs and manifests were not: they rebuild. A first start therefore shows
`PRESENT ON` blank everywhere until each device has been scanned once.

Two things in that file remember the old host, and both are correct as they stand:
`nexus10`'s target is `/sdcard/koreader/cache/webbrowser/192.168.1.33` — a path **on the
tablet**, where the books already are; renaming it to `…/.32` would orphan them and force a
full re-push.

## Outbound ssh from pi5

Every push and probe is `ssh` from pi5 to a node, so pi5's `~/.ssh/config` is part of the
deployment. It already carries `ControlMaster auto` / `ControlPersist 3600` — which
`libnodes/probe.py` and the rsync `-e ssh` rely on for connection reuse — and the six
port-2222 root entries for the termux and Kobo nodes. `thinkpad` (the `sync_mode: mirror`
node, `User tigran`, port 22) had to be added; it was the one fleet member missing.

pi5's public key is installed on every registered node. Host keys are not pre-seeded, but
`StrictHostKeyChecking=accept-new` in `ssh_argv` handles the first connection.

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
- A full reindex walks 24,621 entries in **1.0 s** on a single background thread, measured
  2026-08-17. The same walk took ~29 s on the Pi 3. It is safe to run during a transfer.
- Expect ~64 MB RSS (63.5 MB measured, idle, index loaded). `MemoryMax=1G` in the unit is a
  leak-catcher, not a budget.
- Logs are capped at `LIBNODES_LOG_RETENTION` (200) files under `var/logs/`.

## Environment

Every setting is a `LIBNODES_`-prefixed environment variable or a line in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `LIBNODES_LIBRARY_ROOT` | `/Books` | the library root |
| `LIBNODES_STATE_DIR` | `<project>/var` | index, jobs, manifests, logs, devices.yaml |
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
it. With no reverse proxy in front, this password is the *only* guard. Set one:

```bash
sudo install -m 600 /dev/null /etc/default/libnodes
echo "LIBNODES_PASSWORD=your-password-here" | sudo tee /etc/default/libnodes >/dev/null
sudo systemctl restart libnodes
```

`libnodes.service` reads that file through `EnvironmentFile=-/etc/default/libnodes`. The
leading `-` makes it optional, so the unit still starts on a machine that has no such file.

**Do not put the password in `.env`.** `deploy.sh` syncs with `--delete` and does not
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
