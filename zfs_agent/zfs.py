"""Privileged side: shell out to ``zfs`` and hand the mountpoint over."""

import re
import signal
import subprocess
from typing import TypedDict

from zfs_agent.logs import get_logger

log = get_logger(__name__)

# ``-w`` keeps encrypted datasets encrypted on the wire; for unencrypted ones
# it is equivalent to ``-L -e -c``, spelled out for readability. No ``-p``:
# a stream carries no properties, the receiving side sets its own.
SEND_FLAGS = ("-c", "-L", "-e", "-w")

# Properties a received stream must not set. The stream comes from the
# client, so these would let it steer the root-side mount or export the
# dataset – the create allowlist's reasoning, applied to ``zfs receive``.
RECEIVE_EXCLUDE = (
    "mountpoint",
    "canmount",
    "sharenfs",
    "sharesmb",
    "setuid",
    "exec",
    "devices",
)


# ``zfs send -nv -t`` prints the token's nvlist, ``toname = pool/ds@snap``
# among it.
_TOKEN_TONAME_RE = re.compile(r"^\s*toname\s*=\s*(\S+)\s*$", re.MULTILINE)


class Snapshot(TypedDict):
    name: str
    guid: str  # a 64-bit unsigned int; a string survives any JSON parser
    createtxg: int


class Status(TypedDict):
    exists: bool
    snapshots: list[Snapshot]
    resume_token: str | None


def _run_zfs(args: list[str]) -> "subprocess.CompletedProcess[str]":
    """Run a ``zfs`` subcommand, turning a missing binary into RuntimeError."""
    try:
        return subprocess.run(["zfs", *args], capture_output=True, text=True)
    except OSError as e:
        raise RuntimeError(f"cannot run zfs: {e}") from e


def _run_zfs_stream(
    args: list[str], *, stdin: int | None = None, stdout: int | None = None
) -> None:
    """Run a ``zfs`` subcommand reading or writing a caller-owned fd.

    Raises RuntimeError with zfs's stderr on failure.
    """
    try:
        result = subprocess.run(
            ["zfs", *args], stdin=stdin, stdout=stdout, stderr=subprocess.PIPE
        )
    except OSError as e:
        raise RuntimeError(f"cannot run zfs: {e}") from e
    if result.returncode != 0:
        error = result.stderr.decode(errors="replace").strip()
        if not error:  # e.g. a send killed by SIGPIPE: its reader went away
            code = result.returncode
            error = (
                f"killed by {signal.Signals(-code).name}"
                if code < 0
                else f"exit code {code}"
            )
        raise RuntimeError(f"zfs {args[0]} failed: {error}")


def _exists(dataset: str) -> bool:
    return _run_zfs(["list", "-H", "-o", "name", dataset]).returncode == 0


def _resume_token(dataset: str) -> str | None:
    result = _run_zfs(["get", "-H", "-o", "value", "receive_resume_token", dataset])
    token = result.stdout.strip()
    if result.returncode != 0 or token in ("", "-"):
        return None
    return token


def _chown_mountpoint(dataset: str, owner: str) -> None:
    """Chown the mountpoint of a ZFS dataset to the given uid:gid.

    Failures are logged, not raised: the dataset itself already exists by
    the time we get here.
    """
    try:
        result = _run_zfs(["list", "-H", "-o", "mountpoint", dataset])
    except RuntimeError as e:
        log.warning("Cannot resolve mountpoint", dataset=dataset, error=str(e))
        return
    if result.returncode != 0:
        log.warning("Cannot resolve mountpoint", dataset=dataset)
        return
    mountpoint = result.stdout.strip()
    # ``-`` for volumes, ``none``/``legacy`` for datasets ZFS doesn't mount
    # itself: anything but an absolute path is not ours to chown.
    if not mountpoint.startswith("/"):
        log.debug("No mountpoint to chown", dataset=dataset, mountpoint=mountpoint)
        return
    log.debug("chown mountpoint", mountpoint=mountpoint, owner=owner)
    try:
        chown = subprocess.run(
            ["chown", owner, mountpoint], capture_output=True, text=True
        )
    except OSError as e:
        log.warning("chown failed", mountpoint=mountpoint, error=str(e))
        return
    if chown.returncode != 0:
        log.warning("chown failed", mountpoint=mountpoint, error=chown.stderr.strip())


def zfs_create_local(
    dataset: str,
    props: dict[str, str] | None = None,
    exist_ok: bool = True,
    owner: str | None = None,
) -> bool:
    """Create a ZFS dataset via local subprocess. Returns True if created."""
    # ``zfs create -p`` exits 0 for an existing dataset, so probe first to
    # keep the created/exists distinction.
    if _run_zfs(["list", "-H", "-o", "name", dataset]).returncode == 0:
        if not exist_ok:
            raise RuntimeError(f"dataset already exists: {dataset}")
        log.debug("ZFS dataset already exists", dataset=dataset)
        return False

    args = ["create", "-p"]
    for k, v in (props or {}).items():
        args.extend(["-o", f"{k}={v}"])
    args.append(dataset)

    result = _run_zfs(args)
    if result.returncode != 0:
        log.error("zfs create failed", dataset=dataset, error=result.stderr.strip())
        raise RuntimeError(f"zfs create failed: {result.stderr.strip()}")

    log.info("Created ZFS dataset", dataset=dataset)
    if owner:
        _chown_mountpoint(dataset, owner)
    return True


def zfs_status_local(dataset: str) -> Status:
    """Snapshots (oldest first) and pending resume token of a dataset.

    An interrupted resumable receive leaves its token on the dataset
    received into. A receive that was creating the dataset leaves it
    behind – without snapshots, until resumed or aborted (``zfs receive -A``
    destroys it again); one into an existing dataset keeps its partial state
    in a hidden ``%recv`` child, the dataset itself untouched.
    """
    if not _exists(dataset):
        return {"exists": False, "snapshots": [], "resume_token": None}
    result = _run_zfs(
        [
            "list",
            "-H",
            "-p",
            "-t",
            "snapshot",
            "-o",
            "name,guid,createtxg",
            "-s",
            "createtxg",
            "-d",
            "1",
            dataset,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"zfs list failed: {result.stderr.strip()}")
    snapshots: list[Snapshot] = []
    for line in result.stdout.splitlines():
        name, guid, createtxg = line.split("\t")
        snapshots.append(
            {
                "name": name.split("@", 1)[1],
                "guid": guid,
                "createtxg": int(createtxg),
            }
        )
    return {
        "exists": True,
        "snapshots": snapshots,
        "resume_token": _resume_token(dataset),
    }


def zfs_snapshot_local(*snapshots: str) -> None:
    """Create snapshots (``dataset@name``) in one atomic ``zfs snapshot``."""
    result = _run_zfs(["snapshot", *snapshots])
    if result.returncode != 0:
        raise RuntimeError(f"zfs snapshot failed: {result.stderr.strip()}")
    log.info("Created ZFS snapshots", snapshots=",".join(snapshots))


def zfs_send_local(
    dataset: str,
    fd: int,
    snapshot: str | None = None,
    since: str | None = None,
    token: str | None = None,
) -> None:
    """Write a send stream of ``dataset@snapshot`` to ``fd``.

    Incremental from ``dataset@since`` if given; ``token`` instead resumes
    an interrupted receive (the token encodes everything else).
    """
    if token:
        _check_token(dataset, token)
        args = ["send", "-t", token]
    elif snapshot:
        args = ["send", *SEND_FLAGS]
        if since:
            args.extend(["-i", f"{dataset}@{since}"])
        args.append(f"{dataset}@{snapshot}")
    else:
        raise ValueError("either snapshot or token is required")
    log.info("zfs send", dataset=dataset, snapshot=snapshot, since=since)
    _run_zfs_stream(args, stdout=fd)


def _check_token(dataset: str, token: str) -> None:
    """Raise unless ``token`` resumes a send of ``dataset``.

    ``zfs send -t`` ignores any dataset argument: the snapshot to send is
    whatever the token names. A client could otherwise hand over a crafted
    token and have any snapshot on the host streamed to it.
    """
    result = _run_zfs(["send", "-n", "-v", "-t", token])
    if result.returncode != 0:
        raise RuntimeError(f"zfs send failed: {result.stderr.strip()}")
    match = _TOKEN_TONAME_RE.search(result.stdout + result.stderr)
    if match is None:
        raise RuntimeError("zfs send failed: resume token names no snapshot")
    target = match.group(1).split("@")[0]
    if target != dataset:
        raise RuntimeError(f"zfs send failed: resume token is for {target}")


def zfs_abort_local(dataset: str) -> None:
    """Discard the partial state an interrupted resumable receive left."""
    result = _run_zfs(["receive", "-A", dataset])
    if result.returncode != 0:
        raise RuntimeError(f"zfs receive -A failed: {result.stderr.strip()}")
    log.info("Aborted partial ZFS receive", dataset=dataset)


def zfs_receive_local(
    dataset: str,
    fd: int,
    props: dict[str, str] | None = None,
    force: bool = False,
    resumable: bool = False,
    owner: str | None = None,
) -> None:
    """Receive a send stream read from ``fd`` into ``dataset``.

    ``props`` are set on the received dataset (``-o``); everything in
    `RECEIVE_EXCLUDE` that ``props`` doesn't set is excluded from the
    stream (``-x``). A dataset the receive created gets its mountpoint
    chowned to ``owner``, as after ``create``.
    """
    props = props or {}
    existed = _exists(dataset)
    args = ["receive"]
    if resumable:
        args.append("-s")
    if force:
        args.append("-F")
    for key, value in props.items():
        args.extend(["-o", f"{key}={value}"])
    for prop in RECEIVE_EXCLUDE:
        if prop not in props:
            args.extend(["-x", prop])
    args.append(dataset)
    log.info("zfs receive", dataset=dataset, force=force, resumable=resumable)
    _run_zfs_stream(args, stdin=fd)
    if owner and not existed:
        _chown_mountpoint(dataset, owner)
