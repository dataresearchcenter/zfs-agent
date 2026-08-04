"""ZFS dataset creation: socket client and settings-based dispatch."""

import socket
from typing import Any

import orjson
from structlog import get_logger

from zfs_agent import settings, zfs

log = get_logger(__name__)

# The agent creates datasets synchronously while we wait for the reply.
_RESPONSE_TIMEOUT = 60.0


def zfs_create_socket(
    socket_path: str, dataset: str, exist_ok: bool = True, **props: str
) -> None:
    """Send a ``zfs create`` request to a remote agent over a Unix socket."""
    log.debug("Requesting zfs create via socket", socket=socket_path, dataset=dataset)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_RESPONSE_TIMEOUT)
        sock.connect(socket_path)
        request = orjson.dumps(
            {
                "action": "create",
                "dataset": dataset,
                "props": props,
                "exist_ok": exist_ok,
            }
        )
        sock.sendall(request + b"\n")
        with sock.makefile("rb") as fh:
            line = fh.readline()

    if not line:
        raise RuntimeError("zfs create failed: no response from agent")
    try:
        response: Any = orjson.loads(line)
    except orjson.JSONDecodeError as e:
        raise RuntimeError(f"zfs create failed: malformed response: {e}") from e
    if not isinstance(response, dict):
        raise RuntimeError("zfs create failed: malformed response")
    if not response.get("ok"):
        error = response.get("error", "unknown")
        log.error("Socket zfs create failed", dataset=dataset, error=error)
        raise RuntimeError(f"zfs create failed: {error}")


def zfs_create(dataset: str, exist_ok: bool = True, **props: str) -> None:
    """Create a ZFS dataset, dispatching to socket or local subprocess."""
    conf = settings.Settings()
    if conf.zfs_socket:
        zfs_create_socket(conf.zfs_socket, dataset, exist_ok=exist_ok, **props)
    else:
        zfs.zfs_create_local(dataset, props, exist_ok, conf.zfs_owner)
