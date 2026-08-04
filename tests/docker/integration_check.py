"""Client-side integration checks, run as uid 1000 against the root agent.

Not named ``*_test.py`` on purpose: pytest must never collect this, from
any working directory.
"""

import json
import os
import socket

from zfs_agent.client import zfs_create, zfs_create_socket

SOCKET = os.environ["ZFS_SOCKET"]
POOL_ROOT = os.environ["POOL_ROOT"]
DATASET = f"{POOL_ROOT}/my_dataset"
MOUNTPOINT = f"/{DATASET}"

assert os.getuid() == 1000, f"must run as uid 1000, got {os.getuid()}"


def expect_refused(dataset, expected, **props):
    """The agent must refuse this request with ``expected`` in the error."""
    try:
        zfs_create_socket(SOCKET, dataset, **props)
    except RuntimeError as exc:
        assert expected in str(exc), f"expected {expected!r}, got {exc}"
    else:
        raise AssertionError(f"agent accepted {dataset!r} with props {props}")


def send_raw(payload, read_response=True):
    """Send bytes the client library would never produce."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(SOCKET)
        sock.sendall(payload)
        if read_response:
            with sock.makefile("rb") as fh:
                return fh.readline()
    return None


# create a dataset with props via the socket
zfs_create_socket(SOCKET, DATASET, compression="zstd")

# creating it again is idempotent, and exist_ok=False now reaches the agent
zfs_create_socket(SOCKET, DATASET)
expect_refused(DATASET, "already exists", exist_ok=False)

# the dispatcher picks up ZFS_SOCKET from the environment
zfs_create(f"{POOL_ROOT}/other_dataset")

# the mountpoint was chowned to us and is writable
stat = os.stat(MOUNTPOINT)
assert stat.st_uid == 1000, f"mountpoint owned by uid {stat.st_uid}, expected 1000"
with open(os.path.join(MOUNTPOINT, "probe.txt"), "w") as fh:
    fh.write("hello from uid 1000\n")

# properties that would steer the root-side create or chown are refused
expect_refused(f"{POOL_ROOT}/evil", "not allowed", mountpoint="/etc")
expect_refused(f"{POOL_ROOT}/evil", "not allowed", sharenfs="on")
assert os.stat("/etc").st_uid == 0, "/etc changed owner"

# datasets outside the allowed pool are rejected, including siblings whose
# name merely starts with the pool path
expect_refused("otherpool/evil", "not under pool")
expect_refused(f"{POOL_ROOT}_evil/x", "not under pool")

# command injection attempts are rejected
expect_refused(f"{POOL_ROOT}/x; rm -rf /", "invalid")

# none of this may take the daemon down
for junk in (
    b"null\n",
    b"[]\n",
    b'{"action": "create", "dataset": 123}\n',
    b'{"action": "create", "dataset": "a\xff"}\n',
    b"not json\n",
    b"\n",
    b"x" * 100_000 + b"\n",
):
    send_raw(junk)

# hang up without reading the response
send_raw(
    json.dumps({"action": "create", "dataset": DATASET}).encode() + b"\n",
    read_response=False,
)

# the agent survived all of it
zfs_create_socket(SOCKET, f"{POOL_ROOT}/after_junk")

print("integration checks passed (uid=1000)")
