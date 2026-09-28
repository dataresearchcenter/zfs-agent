"""ZFS operations: socket client and settings-based dispatch."""

import json
import socket
from typing import Any

from zfs_agent import settings, zfs
from zfs_agent.logs import get_logger
from zfs_agent.zfs import Status

log = get_logger(__name__)

# The agent creates datasets synchronously while we wait for the reply.
_RESPONSE_TIMEOUT = 60.0


def _request(
    socket_path: str,
    request: dict[str, Any],
    fd: int | None = None,
    timeout: float | None = _RESPONSE_TIMEOUT,
) -> dict[str, Any]:
    """Send one request (plus ``fd``, if given) and return the response.

    Raises RuntimeError unless the agent answers ``{"ok": true, ...}``.
    """
    action = request["action"]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        payload = json.dumps(request).encode("ascii") + b"\n"
        if fd is None:
            sock.sendall(payload)
        else:
            # The agent reads descriptors from the first chunk only, so the
            # whole (small) request goes out in this one sendmsg.
            sent = socket.send_fds(sock, [payload], [fd])
            if sent != len(payload):
                raise RuntimeError(f"zfs {action} failed: short write to agent")
        with sock.makefile("rb") as fh:
            line = fh.readline()

    if not line:
        raise RuntimeError(f"zfs {action} failed: no response from agent")
    try:
        response: Any = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RuntimeError(f"zfs {action} failed: malformed response: {e}") from e
    if not isinstance(response, dict):
        raise RuntimeError(f"zfs {action} failed: malformed response")
    if not response.get("ok"):
        error = response.get("error", "unknown")
        log.error(f"Socket zfs {action} failed", error=error)
        raise RuntimeError(f"zfs {action} failed: {error}")
    return response


def zfs_create_socket(
    socket_path: str, dataset: str, exist_ok: bool = True, **props: str
) -> None:
    """Send a ``zfs create`` request to a remote agent over a Unix socket."""
    log.debug("Requesting zfs create via socket", socket=socket_path, dataset=dataset)
    _request(
        socket_path,
        {
            "action": "create",
            "dataset": dataset,
            "props": props,
            "exist_ok": exist_ok,
        },
    )


def zfs_create(dataset: str, exist_ok: bool = True, **props: str) -> None:
    """Create a ZFS dataset, dispatching to socket or local subprocess."""
    conf = settings.Settings()
    if conf.zfs_socket:
        zfs_create_socket(conf.zfs_socket, dataset, exist_ok=exist_ok, **props)
    else:
        zfs.zfs_create_local(dataset, props, exist_ok, conf.zfs_owner)


def zfs_status(dataset: str) -> Status:
    """Snapshots and pending resume token of ``dataset``."""
    conf = settings.Settings()
    if conf.zfs_socket:
        response = _request(conf.zfs_socket, {"action": "status", "dataset": dataset})
        status: Status = response["status"]
        return status
    return zfs.zfs_status_local(dataset)


def zfs_snapshot(*snapshots: str) -> None:
    """Create ``dataset@name`` snapshots atomically."""
    conf = settings.Settings()
    if conf.zfs_socket:
        _request(conf.zfs_socket, {"action": "snapshot", "snapshots": list(snapshots)})
    else:
        zfs.zfs_snapshot_local(*snapshots)


def zfs_send(
    dataset: str,
    fd: int,
    snapshot: str | None = None,
    since: str | None = None,
    token: str | None = None,
) -> None:
    """Write a send stream of ``dataset@snapshot`` to ``fd``.

    Blocks until ``zfs send`` exits. The caller keeps ownership of ``fd``;
    closing the reading end makes the send fail instead of blocking.
    """
    conf = settings.Settings()
    if conf.zfs_socket:
        request = {
            "action": "send",
            "dataset": dataset,
            "snapshot": snapshot,
            "since": since,
            "token": token,
        }
        # No timeout: a send lasts as long as the stream does.
        _request(conf.zfs_socket, request, fd=fd, timeout=None)
    else:
        zfs.zfs_send_local(dataset, fd, snapshot=snapshot, since=since, token=token)


def zfs_abort(dataset: str) -> None:
    """Discard the partial state of an interrupted resumable receive."""
    conf = settings.Settings()
    if conf.zfs_socket:
        _request(conf.zfs_socket, {"action": "abort", "dataset": dataset})
    else:
        zfs.zfs_abort_local(dataset)


def zfs_receive(
    dataset: str,
    fd: int,
    props: dict[str, str] | None = None,
    force: bool = False,
    resumable: bool = False,
) -> None:
    """Receive a send stream read from ``fd`` into ``dataset``.

    Blocks until ``zfs receive`` exits. The caller keeps ownership of ``fd``.
    """
    conf = settings.Settings()
    if conf.zfs_socket:
        request = {
            "action": "receive",
            "dataset": dataset,
            "props": props or {},
            "force": force,
            "resumable": resumable,
        }
        _request(conf.zfs_socket, request, fd=fd, timeout=None)
    else:
        zfs.zfs_receive_local(
            dataset,
            fd,
            props,
            force=force,
            resumable=resumable,
            owner=conf.zfs_owner,
        )
