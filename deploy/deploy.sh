#!/usr/bin/env bash
# Push LibNodes to pi5 and restart it.
#
#   ./deploy/deploy.sh            # sync + deps + restart
#   ./deploy/deploy.sh --no-restart
#
# Deliberately does NOT sync var/ (index, jobs, manifests, logs, devices.yaml) or the
# workstation's x86_64 .venv — pi5 is aarch64 and builds its own.
set -euo pipefail

HOST="${LIBNODES_HOST:-pi5}"
DEST="${LIBNODES_DEST:-/home/tigran/libnodes}"
UV="${LIBNODES_UV:-/usr/local/bin/uv}"
PORT="${LIBNODES_PORT:-8090}"   # nginx owns 80/443 on pi5; urantia-library owns 8000
RESTART=1
[[ "${1:-}" == "--no-restart" ]] && RESTART=0

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
# rsync above (the workstation's is x86_64), so on a host being deployed to for the first
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
        # systemd reports `active` as soon as it has exec'd uvicorn, which is before the
        # app can answer — seconds on a Pi 3, less on pi5, but never zero. Poll the health
        # endpoint instead so a deploy that starts a broken build fails here rather than
        # looking fine.
        ssh -o BatchMode=yes "$HOST" "
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
