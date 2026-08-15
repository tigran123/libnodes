#!/usr/bin/env bash
# Push LibNodes to the Pi and restart it.
#
#   ./deploy/deploy.sh            # sync + deps + restart
#   ./deploy/deploy.sh --no-restart
#
# Deliberately does NOT sync var/ (index, jobs, manifests, logs, devices.yaml) or the
# workstation's x86_64 .venv.
set -euo pipefail

HOST="${LIBNODES_HOST:-pi}"
DEST="${LIBNODES_DEST:-/home/pi/libnodes}"
UV="${LIBNODES_UV:-/home/pi/.local/bin/uv}"
PORT="${LIBNODES_PORT:-8090}"   # 8080 on the Pi is nginx's
RESTART=1
[[ "${1:-}" == "--no-restart" ]] && RESTART=0

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> checking $HOST:$DEST is writable"
if ! ssh -o BatchMode=yes "$HOST" "touch $DEST/.deploy-probe && rm -f $DEST/.deploy-probe" 2>/dev/null; then
    cat >&2 <<EOF
error: $DEST is not writable on $HOST.

If it is a read-only bind mount, /etc/fstab needs rw instead of ro:

    /ext/disk4/work/libnodes  /home/pi/libnodes  none  defaults,rw,bind,nofail  0  0

then:  sudo mount -o remount,rw,bind /home/pi/libnodes
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

echo "==> installing dependencies"
ssh -o BatchMode=yes "$HOST" "cd $DEST && $UV pip sync requirements.txt"

if [[ $RESTART -eq 1 ]]; then
    # `systemctl list-unit-files` exits 0 with an empty list when the unit is absent,
    # so ask about the unit's state instead.
    if ssh -o BatchMode=yes "$HOST" \
        'systemctl cat libnodes.service' >/dev/null 2>&1; then
        echo "==> restarting libnodes.service"
        # systemd reports `active` as soon as it has exec'd uvicorn, which on a Pi 3 is
        # several seconds before the app can answer. Poll the health endpoint instead so
        # a deploy that starts a broken build fails here rather than looking fine.
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
