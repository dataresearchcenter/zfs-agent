"""Streaming checks, run as uid 1000 against an agent serving every action.

Replicates a dataset and its child through the agent (full, incremental,
interrupted and resumed), with the send stream crossing a real pipe whose
ends were handed to the agent over the socket.

Not named ``*_test.py`` on purpose: pytest must never collect this, from
any working directory.
"""

import os
import threading

from zfs_agent import (
    zfs_abort,
    zfs_create,
    zfs_receive,
    zfs_send,
    zfs_snapshot,
    zfs_status,
)

POOL_ROOT = os.environ["POOL_ROOT"]
SRC = f"{POOL_ROOT}/src"
DST = f"{POOL_ROOT}/dst"
# prepared by the entrypoint as root: a ``zfs send -p`` stream of a
# dataset whose mountpoint is /evilmnt
EVIL_STREAM = "/tmp/evil.stream"

assert os.getuid() == 1000, f"must run as uid 1000, got {os.getuid()}"


def replicate(src, dst, limit=None, **send_kwargs):
    """Pipe ``zfs send`` of ``src`` into ``zfs receive`` of ``dst``.

    With ``limit``, only that many bytes of the stream get through – an
    interrupted transfer. Returns the receive's error, if any.
    """
    send_r, send_w = os.pipe()
    recv_r, recv_w = os.pipe()
    errors = {}

    def _send():
        try:
            zfs_send(src, send_w, **send_kwargs)
        except RuntimeError as e:
            errors["send"] = e
        finally:
            os.close(send_w)

    def _receive():
        try:
            zfs_receive(dst, recv_r, force=True, resumable=True)
        except RuntimeError as e:
            errors["receive"] = e
        finally:
            # a receive that gave up early must fail our writes, not block them
            os.close(recv_r)

    threads = [threading.Thread(target=_send), threading.Thread(target=_receive)]
    for thread in threads:
        thread.start()
    copied = 0
    while chunk := os.read(send_r, 1 << 20):
        if limit is not None:
            chunk = chunk[: limit - copied]
        try:
            os.write(recv_w, chunk)
        except BrokenPipeError:
            break
        copied += len(chunk)
        if limit is not None and copied >= limit:
            break
    os.close(recv_w)
    os.close(send_r)  # an interrupted send fails on EPIPE instead of blocking
    for thread in threads:
        thread.join(60)
        assert not thread.is_alive(), "transfer thread hung"
    return errors.get("receive")


def write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def guids(dataset):
    return [(s["name"], s["guid"]) for s in zfs_status(dataset)["snapshots"]]


# a dataset with a child, both writable by us (the agent chowns them)
zfs_create(SRC)
zfs_create(f"{SRC}/archive")
write(f"/{SRC}/a.txt", b"one")
write(f"/{SRC}/archive/b.txt", b"blob")
zfs_snapshot(f"{SRC}@s1", f"{SRC}/archive@s1")

# full sends: the parent first, then the child into it
assert zfs_status(DST) == {"exists": False, "snapshots": [], "resume_token": None}
assert replicate(SRC, DST, snapshot="s1") is None
assert replicate(f"{SRC}/archive", f"{DST}/archive", snapshot="s1") is None
assert read(f"/{DST}/a.txt") == b"one"
assert read(f"/{DST}/archive/b.txt") == b"blob"
assert os.stat(f"/{DST}").st_uid == 1000, "received mountpoint not chowned"
assert guids(DST) == guids(SRC), "received snapshot guid differs"

# incremental on top
write(f"/{SRC}/a.txt", b"two")
zfs_snapshot(f"{SRC}@s2")
assert replicate(SRC, DST, snapshot="s2", since="s1") is None
assert read(f"/{DST}/a.txt") == b"two"
assert [name for name, _ in guids(DST)] == ["s1", "s2"]

# an interrupted receive of a new dataset leaves that dataset behind,
# without snapshots, carrying the resume token ...
zfs_create(f"{POOL_ROOT}/big")
write(f"/{POOL_ROOT}/big/data.bin", os.urandom(32 << 20))
zfs_snapshot(f"{POOL_ROOT}/big@s1")
error = replicate(
    f"{POOL_ROOT}/big", f"{POOL_ROOT}/big_copy", snapshot="s1", limit=8 << 20
)
assert error is not None, "truncated stream was accepted"
status = zfs_status(f"{POOL_ROOT}/big_copy")
assert status["resume_token"] and status["snapshots"] == [], status

# the token only resumes what it was made for - not a send of another
# dataset, which ``zfs send -t`` alone would happily stream
r, w = os.pipe()
try:
    zfs_send(SRC, w, token=status["resume_token"])
except RuntimeError as exc:
    assert "resume token is for" in str(exc), exc
else:
    raise AssertionError("agent resumed a token for another dataset")
finally:
    os.close(r)
    os.close(w)

# ... which a resumed send completes
error = replicate(
    f"{POOL_ROOT}/big", f"{POOL_ROOT}/big_copy", token=status["resume_token"]
)
assert error is None, error
assert read(f"/{POOL_ROOT}/big_copy/data.bin") == read(f"/{POOL_ROOT}/big/data.bin")
assert zfs_status(f"{POOL_ROOT}/big_copy")["resume_token"] is None

# an interrupted incremental keeps the dataset as it was – its partial
# state sits in a hidden %recv child – and the token on the dataset
write(f"/{POOL_ROOT}/big/data.bin", os.urandom(32 << 20))
zfs_snapshot(f"{POOL_ROOT}/big@s2")
error = replicate(
    f"{POOL_ROOT}/big",
    f"{POOL_ROOT}/big_copy",
    snapshot="s2",
    since="s1",
    limit=8 << 20,
)
assert error is not None, "truncated incremental was accepted"
status = zfs_status(f"{POOL_ROOT}/big_copy")
assert status["resume_token"], status
assert [s["name"] for s in status["snapshots"]] == ["s1"], status
error = replicate(
    f"{POOL_ROOT}/big", f"{POOL_ROOT}/big_copy", token=status["resume_token"]
)
assert error is None, error
assert read(f"/{POOL_ROOT}/big_copy/data.bin") == read(f"/{POOL_ROOT}/big/data.bin")
assert [s["name"] for s in zfs_status(f"{POOL_ROOT}/big_copy")["snapshots"]] == [
    "s1",
    "s2",
]

# a partial receive that won't be resumed can be discarded – for a new
# dataset, that destroys what the receive had created
replicate(f"{POOL_ROOT}/big", f"{POOL_ROOT}/big_gone", snapshot="s1", limit=8 << 20)
assert zfs_status(f"{POOL_ROOT}/big_gone")["resume_token"]
zfs_abort(f"{POOL_ROOT}/big_gone")
assert zfs_status(f"{POOL_ROOT}/big_gone") == {
    "exists": False,
    "snapshots": [],
    "resume_token": None,
}

# a stream carrying mountpoint=/evilmnt must not get to mount there
with open(EVIL_STREAM, "rb") as fh:
    zfs_receive(f"{POOL_ROOT}/evil_copy", fh.fileno())
assert os.path.ismount(f"/{POOL_ROOT}/evil_copy"), "not mounted under the pool"

print("stream checks passed (uid=1000)")
