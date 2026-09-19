# TODO

The working list. `README.md` §Status is the user-facing summary of what works; this file is
what is left, with pointers into the code. Keep the two from contradicting each other.

## Unbuilt features

- [ ] **Device configuration drawer.** There is no per-device edit and no route to hang
      one on: `/device/new` was a stub page saying "stage 2", and it went with the button
      that linked it — a control that cannot do the thing it names is worse than no
      control. `devices.yaml` is hand-edited today. This is the first code that would
      *write* that file, so it needs validate-then-atomic-rename rather than an in-place
      write — and note that the inotify watcher will fire on our own save, so the reload
      path must tolerate seeing its own write come back.

- [ ] **Presets.** Saved selections that can be re-pushed in one action. The stub page
      and its nav item were removed — they had said "planned for stage 2" in the rail for
      long enough without ever saying anything else — so this now needs a route and a view
      as well as the feature.

- [ ] **Wake-on-LAN.** Needs a `mac:` field on `Device` (`libnodes/models.py`) and a row
      action next to the existing ones. It pairs with the *sleeping* state the probe already
      computes from `sleeping_window` — a device that answered recently but not now is
      exactly the one worth waking.

- [ ] **Keys page.** The informational stub is gone; what it said about `~/.ssh` and
      `BatchMode=yes` now sits at the foot of `/settings`. The real feature is still
      wanted: list the identities in the service user's `~/.ssh` and offer a per-device
      "test key" that runs the existing `ssh_argv` (`libnodes/probe.py:448`) and reports
      the exit status — the machinery is already there, only the view is missing.

- [x] **Orphan blobs after a pull.** *(Done 2026-09-19.)* This asked for a read-only
      report on the grounds that "a pull that could remove local files is a different and
      much more dangerous thing than the one that exists" — and the answer turned out to
      be that the dangerous thing was the right one. A pull now carries `--delete`, bounded
      by the excludes (which rsync protects from deletion for free), by
      `LIBNODES_PULL_MAX_DELETE` and by the Dry run. The orphan blob goes with the symlink
      that stopped referencing it, in the same pass, so there is nothing left to report on.
      What prompted it: a book replaced upstream on 2026-09-19 left both editions here.

- [x] **Top-level non-CAS files are overwritten by a pull without notice.**
      *(Decided 2026-09-19.)* `/Books/CLAUDE.md` and its siblings are ordinary files, not
      links, so the upstream's copies replace this host's — and now that a pull prunes, a
      top-level file the upstream does *not* have is removed rather than merely overwritten.
      That is correct and stays: pi5 is strictly downstream, nothing but a pull writes
      `/Books` here, so a local-only file there is stale by the same definition every
      pruned symlink is. Not added to `config.PULL_EXCLUDES` — an exclude would make them
      the one part of the tree that silently stops replicating.

- [ ] **A mirror's prune does not retract its manifest rows.** `_debit_pull` does this for
      a pull — `Manifests.retract`, from rsync's own `deleting` lines — and a mirror push
      carries the only other `--delete` in the program, outward instead of inward. Its
      stale rows survive: `_update_manifest` re-records the index for each source, and a
      path the replica no longer has is not in the index to be re-recorded, so it keeps
      claiming the replica holds a file `--delete` removed. The count is already collected
      (`self._deleted`, every kind) and the retraction is source-agnostic, so this is a
      call site and a name that no longer says "pull". Surfaced by giving the FILES column
      a deleted count, which now shows a Replicate's prune beside a PRESENT ON fraction
      that has not heard about it.

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

- [ ] **Add CI.** There is no `.github/`. The suite needs no network and runs in ~20 s on
      pi5 (514 tests, measured 2026-08-19), so a workflow that does
      `uv pip sync requirements-dev.txt && pytest` costs almost nothing and would catch the
      class of break that only shows up on a clean checkout. Worth more now that dev happens
      on the deployment host: nothing else exercises a clean tree.

- [ ] **Resolve the dead schema fields.** `formats` (`libnodes/models.py:141`) and
      `rsync_flags` (`libnodes/models.py:150`, and in `Defaults`) are accepted and ignored.
      Someone who sets `rsync_flags:` today is silently misled into thinking it does
      something. Either surface them in the validation strip as *ignored*, or drop them and
      say so in the seed `devices.yaml` comment block.

- [x] **Re-home the docs on pi5 as the dev host.** *(done 2026-08-19.)* Development moved
      onto pi5, so `deploy.sh` is no longer the loop — edit, `sudo systemctl restart
      libnodes`, look. `deploy.sh` was kept, for pushing to some *other* host, and given a
      guard that refuses to run when its target is this tree; the alternative was deleting it
      and losing the first-deploy and health-poll logic. `tools/shot.py` was added because
      this host has no display, and the drive figures were re-measured on the new Gen2 NVMe.

- [x] **`deploy/README.md` is out of date about the Pi bind mount.** *(done 2026-08-17, by
      the pi5 migration.)* All three copies of the stale claim are gone: the opening section
      of `deploy/README.md`, the note at the top of `deploy/libnodes.service`, and the
      `/etc/fstab` advice in `deploy.sh`'s error message. There is no bind mount on pi5 —
      `/home/tigran/libnodes` is plain NVMe — so the writability probe is now the only guard,
      as intended, and it `mkdir -p`s the destination so a first deploy to a new host works.
