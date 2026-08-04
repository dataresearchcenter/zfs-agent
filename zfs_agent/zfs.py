"""Privileged side: shell out to ``zfs`` and hand the mountpoint over."""

import subprocess

from zfs_agent.logs import get_logger

log = get_logger(__name__)


def _run_zfs(args: list[str]) -> "subprocess.CompletedProcess[str]":
    """Run a ``zfs`` subcommand, turning a missing binary into RuntimeError."""
    try:
        return subprocess.run(["zfs", *args], capture_output=True, text=True)
    except OSError as e:
        raise RuntimeError(f"cannot run zfs: {e}") from e


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
