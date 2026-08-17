# TODO

The working list. `README.md` §Status is the user-facing summary of what works; this file is
what is left, with pointers into the code. Keep the two from contradicting each other.

## Unbuilt features

- [ ] **Device configuration drawer.** `/device/new` is a stub
      (`libnodes/routes/config_view.py:138`) and there is no per-device edit; `devices.yaml`
      is hand-edited today. This is the first code that would *write* that file, so it needs
      validate-then-atomic-rename rather than an in-place write — and note that the inotify
      watcher will fire on our own save, so the reload path must tolerate seeing its own
      write come back.

- [ ] **Presets.** Saved selections that can be re-pushed in one action. Stub at
      `libnodes/routes/config_view.py:117`; the nav item is already live and linked at
      `libnodes/templates/base.html:34`, so the empty page is reachable from every view.

- [ ] **Wake-on-LAN.** Needs a `mac:` field on `Device` (`libnodes/models.py`) and a row
      action next to the existing ones. It pairs with the *sleeping* state the probe already
      computes from `sleeping_window` — a device that answered recently but not now is
      exactly the one worth waking.

- [ ] **Keys page.** Currently an informational stub
      (`libnodes/routes/config_view.py:127`). It could list the identities in the service
      user's `~/.ssh` and offer a per-device "test key" that runs the existing
      `ssh_argv` (`libnodes/probe.py:448`) and reports the exit status — the machinery is
      already there, only the view is missing.

## Engineering hygiene

- [ ] **`Scanner` and `JobRunner` deregister a subprocess while being cancelled**, the same
      way `DeviceProbe` did before it was fixed. `scan.py:205` (`self._procs.pop(...)` in a
      `finally`) and `jobs.py:862` run on the `CancelledError` path too, so the proc leaves
      the registry a moment before their `stop()` reaps it — and the one process that needs
      reaping is the one missing from the set. The fixed form is at `probe.py:264`:
      deregister only when `proc.returncode is not None`, and leave a still-running child
      for `stop()`. Not currently observable — the probe is the only one of the three the
      suite exercises hard enough — but it is the same `RuntimeError: Event loop is closed`
      with an rsync or a scan behind it instead of a `df`.

- [ ] **Add ruff (lint + format).** No `pyproject.toml` or `ruff.toml` exists, yet the code
      already carries `# noqa: BLE001` (`libnodes/state.py:62`) — a linter was assumed and
      never wired up. Add the config, add ruff to `requirements-dev.in`, recompile.

- [ ] **Add CI.** There is no `.github/`. The suite needs no network and runs in ~10 s, so a
      workflow that does `uv pip sync requirements-dev.txt && pytest` costs almost nothing
      and would catch the class of break that only shows up on a clean checkout.

- [ ] **Resolve the dead schema fields.** `formats` (`libnodes/models.py:141`) and
      `rsync_flags` (`libnodes/models.py:150`, and in `Defaults`) are accepted and ignored.
      Someone who sets `rsync_flags:` today is silently misled into thinking it does
      something. Either surface them in the validation strip as *ignored*, or drop them and
      say so in the seed `devices.yaml` comment block.

- [ ] **`deploy/README.md` is out of date about the Pi bind mount.** Its opening section,
      "Before the first deploy: make `~/libnodes` writable", asserts that
      `/home/pi/libnodes` is a read-only bind mount and prescribes a `sed` against
      `/etc/fstab`. The Pi's fstab has no ro/rw problem, so that section now sends a reader
      to edit fstab for no reason. Demote it to a one-line troubleshooting note and let the
      writability probe in `deploy/deploy.sh:21` be the actual guard. The same stale claim
      is echoed in `deploy/libnodes.service:6` and in `deploy.sh`'s error message — fix all
      three together.
