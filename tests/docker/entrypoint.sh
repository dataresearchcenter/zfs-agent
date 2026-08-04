#!/bin/bash
# Integration test: root runs the socket agent, uid 1000 creates datasets
# through it. Requires `docker run --privileged` on a host with ZFS
# (/dev/zfs must be visible in the container).
set -euo pipefail

POOL=zfsagent-test
POOL_ROOT="$POOL/lakehouse"
POOL_IMG=/tank.img
SOCKET=/run/zfs.sock
REJECT_SOCKET=/run/zfs-reject.sock
PYTHON=/opt/venv/bin/python
CHECKS=/src/tests/docker

if [ ! -e /dev/zfs ]; then
    echo "ERROR: /dev/zfs not available - run with --privileged on a ZFS host" >&2
    exit 2
fi

stop_agent() {
    kill "$1" 2>/dev/null || return 0
    wait "$1" 2>/dev/null || true
}

cleanup() {
    # Stop the agents before the pool goes away, so a request in flight
    # can't race zpool destroy.
    if [ -n "${AGENT_PID:-}" ]; then stop_agent "$AGENT_PID"; fi
    if [ -n "${REJECT_PID:-}" ]; then stop_agent "$REJECT_PID"; fi
    zpool destroy -f "$POOL" 2>/dev/null || true
    if [ -n "${LOOP:-}" ]; then losetup -d "$LOOP" 2>/dev/null || true; fi
    return 0
}
trap cleanup EXIT

# The kernel resolves file-vdev paths in the host mount namespace, so a
# container-local image path is ENOENT to it. A loop device is a global
# kernel object instead. There is no udev in here, so the node has to be
# created by hand, and claiming one is a race: try until one sticks.
attach_loop() {
    local n dev
    for n in $(seq 0 63); do
        dev="/dev/loop$n"
        [ -e "$dev" ] || mknod "$dev" b 7 "$n" || continue
        if losetup "$dev" "$1" 2>/dev/null; then
            echo "$dev"
            return 0
        fi
    done
    return 1
}

wait_for_socket() {
    local i
    for i in $(seq 50); do
        if [ -S "$1" ]; then
            # bind() creates the node before listen(); don't race it.
            sleep 0.2
            return 0
        fi
        sleep 0.1
    done
    echo "ERROR: socket $1 never appeared" >&2
    return 1
}

truncate -s 512M "$POOL_IMG"
LOOP=$(attach_loop "$POOL_IMG") || { echo "ERROR: no free loop device" >&2; exit 1; }
zpool create "$POOL" "$LOOP"

# --- Phase 1: the happy path, as the allowed UID ---

/opt/venv/bin/zfs-agent \
    --socket "$SOCKET" \
    --pool "$POOL_ROOT" \
    --owner 1000:1000 \
    --allowed-uid 1000 &
AGENT_PID=$!
wait_for_socket "$SOCKET"

runuser -u appuser -- env ZFS_SOCKET="$SOCKET" POOL_ROOT="$POOL_ROOT" \
    "$PYTHON" "$CHECKS/integration_check.py"

stop_agent "$AGENT_PID"
unset AGENT_PID

# --- Phase 2: a mismatched UID is rejected by SO_PEERCRED ---

/opt/venv/bin/zfs-agent \
    --socket "$REJECT_SOCKET" \
    --pool "$POOL_ROOT" \
    --allowed-uid 1001 &
REJECT_PID=$!
wait_for_socket "$REJECT_SOCKET"
# Open the socket up so the client gets far enough to be rejected by the
# peer-credential check rather than by the file mode.
chmod 0666 "$REJECT_SOCKET"

runuser -u appuser -- env ZFS_SOCKET="$REJECT_SOCKET" POOL_ROOT="$POOL_ROOT" \
    "$PYTHON" "$CHECKS/reject_check.py"

stop_agent "$REJECT_PID"
unset REJECT_PID

echo "OK: zfs-agent integration test passed"
