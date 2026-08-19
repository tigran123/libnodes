#!/usr/bin/env bash
# Push LibNodes to ANOTHER host and restart it there.
#
#   LIBNODES_HOST=somehost ./deploy/deploy.sh
#   LIBNODES_HOST=somehost ./deploy/deploy.sh --no-restart
#
# Development happens on pi5 itself now, in the tree the service runs from, so there is
# nothing to deploy there: edit, `sudo systemctl restart libnodes`, look. Run with the
# default target on pi5 this script refuses — see the guard below.
#
# Deliberately does NOT sync var/ (index, jobs, manifests, logs, devices.yaml) or .venv —
# a venv is per-architecture and the target builds its own.
set -euo pipefail

HOST="${LIBNODES_HOST:-pi5}"
DEST="${LIBNODES_DEST:-/home/tigran/libnodes}"
UV="${LIBNODES_UV:-/usr/local/bin/uv}"
PORT="${LIBNODES_PORT:-8090}"   # nginx owns 80/443 on pi5; urantia-library owns 8000
RESTART=1
# Rejected rather than ignored. The only flag is --no-restart, and a mistyped one used to
# fall through to a full deploy including the restart -- the opposite of what someone
# reaching for a flag usually wants. Note there is no -t to pass: see the restart block.
case "${1:-}" in
    "")           ;;
    --no-restart) RESTART=0 ;;
    *)
        echo "usage: $(basename "$0") [--no-restart]" >&2
        exit 2
        ;;
esac

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Refuse to deploy this tree onto itself. pi5 is both the dev box and the deployment, so the
# default target IS this machine, and the reflex `./deploy/deploy.sh` became a same-path
# rsync --delete against a live tree while the service ran out of it. Today the excludes
# happen to protect var/, tests/ and .venv/ from that deletion; one edit to the exclude list
# is all that stands between a self-run and losing local state, which is why this is a guard
# and not a note in the README.
#
# Both halves have to match before refusing: a genuine deploy to a different path on this
# same host is unusual but legitimate, and so is deploying this path to a different host.
target_ip="$(getent hosts "$HOST" | awk '{print $1; exit}')"
if [[ "$HOST" == "$(hostname)" || "$HOST" == localhost || "$HOST" == 127.0.0.1 \
      || ( -n "$target_ip" && -n "$(ip -o addr show | grep -Fw "$target_ip" || true)" ) ]] \
   && [[ "$DEST" == "$here" ]]; then
    cat >&2 <<EOF
refusing: $HOST:$DEST is this tree on this machine ($(hostname)).

There is nothing to deploy here — the service runs from this tree. To pick up changes:

    sudo systemctl restart libnodes && curl -s localhost:${PORT}/healthz

To deploy elsewhere, name the host:

    LIBNODES_HOST=otherhost LIBNODES_DEST=/path/to/libnodes $(basename "$0")
EOF
    exit 2
fi

# mkdir -p, not just touch: on a host being deployed to for the first time $DEST does not
# exist yet, and a bare touch fails there in a way indistinguishable from a permissions
# problem. Creating it is also the whole of "install" — everything else rsync brings.
echo "==> checking $HOST:$DEST is writable"
if ! ssh -o BatchMode=yes "$HOST" \
    "mkdir -p $DEST && touch $DEST/.deploy-probe && rm -f $DEST/.deploy-probe" 2>/dev/null; then
    cat >&2 <<EOF
error: cannot create or write $DEST on $HOST.

Check that the ssh login works and the parent directory is writable:

    ssh $HOST 'mkdir -p $DEST && touch $DEST/.probe && rm $DEST/.probe && echo writable'
EOF
    exit 1
fi

echo "==> syncing source to $HOST:$DEST"
rsync -az --delete \
    --exclude '.venv/' \
    --exclude 'var/' \
    --exclude '.git/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude 'design_handoff_libnodes/' \
    --exclude 'tests/' \
    "$here/" "$HOST:$DEST/"

# `uv pip sync` refuses to run without a target venv, and .venv/ is excluded from the
# rsync above (a venv is per-architecture), so on a host being deployed to for the first
# time there is nothing for it to sync into. Create it if absent; uv reads .python-version.
echo "==> installing dependencies"
ssh -o BatchMode=yes "$HOST" \
    "cd $DEST && { [ -d .venv ] || $UV venv; } && $UV pip sync requirements.txt"

if [[ $RESTART -eq 1 ]]; then
    # `systemctl list-unit-files` exits 0 with an empty list when the unit is absent,
    # so ask about the unit's state instead.
    if ssh -o BatchMode=yes "$HOST" \
        'systemctl cat libnodes.service' >/dev/null 2>&1; then
        echo "==> restarting libnodes.service"
        # `sudo` on pi5 needs a terminal to prompt for a password, and the ssh above
        # allocates none by default -- which is why a deploy run by a human stopped dead
        # at the last step with "a terminal is required to read the password".
        #
        # -t, and never -tt. -t asks for a PTY and OpenSSH declines when the caller has no
        # terminal, so a scripted deploy keeps today's fast, explicit failure. -tt would
        # force one, and the password prompt would then block a caller with no way to
        # answer it -- a silent hang in place of a clear error.
        #
        # Conditional rather than always-on only to keep the "Pseudo-terminal will not be
        # allocated" notice off stderr in non-interactive runs, where a stderr line from a
        # deploy script reads as a failure.
        tty_flag=()
        if [[ -t 0 ]]; then tty_flag=(-t); fi
        # systemd reports `active` as soon as it has exec'd uvicorn, which is before the
        # app can answer — seconds on a Pi 3, less on pi5, but never zero. Poll the health
        # endpoint instead so a deploy that starts a broken build fails here rather than
        # looking fine.
        ssh "${tty_flag[@]}" -o BatchMode=yes "$HOST" "
            sudo systemctl restart libnodes
            for i in \$(seq 1 30); do
                if curl -fsS -m 2 http://127.0.0.1:$PORT/healthz >/dev/null 2>&1; then
                    echo \"    healthy after \${i}s\"
                    curl -fsS http://127.0.0.1:$PORT/healthz
                    echo
                    exit 0
                fi
                sleep 1
            done
            echo '    ERROR: no healthy response after 30s' >&2
            systemctl --no-pager -n 20 status libnodes >&2 || true
            exit 1
        "
    else
        echo "==> libnodes.service not installed; skipping restart"
        echo "    sudo cp $DEST/deploy/libnodes.service /etc/systemd/system/"
        echo "    sudo systemctl daemon-reload && sudo systemctl enable --now libnodes"
    fi
fi

echo "==> done"
