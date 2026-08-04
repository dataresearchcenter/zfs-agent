"""Serving one connection: peer authentication, protocol, dispatch."""

import json
import socket
import struct
from typing import Any

from zfs_agent.logs import get_logger
from zfs_agent.validate import validate_dataset, validate_props
from zfs_agent.zfs import zfs_create_local

log = get_logger(__name__)

# Connections are served sequentially, so a peer that connects and stays
# quiet would starve every other caller, and an endless line would grow the
# root process without bound.
_REQUEST_TIMEOUT = 10.0
_MAX_REQUEST = 64 * 1024

# Linux SO_PEERCRED returns a ``struct ucred`` { pid_t pid; uid_t uid;
# gid_t gid; } – three 32-bit ints in native byte order.
_UCRED_FMT = "iII"

# Explicit opt-out of the peer-credential check. Only correct where the
# caller is the peer by definition, as in unit tests.
ANY_UID = -1


def get_peer_uid(conn: socket.socket) -> int:
    """Return the UID of the peer process at the other end of ``conn``.

    Uses Linux's ``SO_PEERCRED`` on the Unix-domain socket. Raises
    ``OSError`` if the platform doesn't support it (e.g. macOS uses
    ``LOCAL_PEERCRED`` with a different layout – the agent is Linux-only
    per the deployment docs, so we don't bother shimming).
    """
    buf = conn.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(_UCRED_FMT)
    )
    _pid, uid, _gid = struct.unpack(_UCRED_FMT, buf)
    return int(uid)


def handle_request(
    data: Any, allowed_pool: str | None, owner: str | None = None
) -> dict[str, Any]:
    """Process a single JSON request and return a response dict."""
    if not isinstance(data, dict):
        log.warning("Malformed request", type=type(data).__name__)
        return {"ok": False, "error": "request must be a JSON object"}

    action = data.get("action")
    if action != "create":
        log.warning("Unknown action requested", action=action)
        return {"ok": False, "error": f"unknown action: {action!r}"}

    dataset = data.get("dataset", "")
    err = validate_dataset(dataset, allowed_pool)
    if err:
        log.warning("Dataset validation failed", dataset=dataset, error=err)
        return {"ok": False, "error": err}

    props, err = validate_props(data.get("props") or {})
    if err:
        log.warning("Property validation failed", dataset=dataset, error=err)
        return {"ok": False, "error": err}

    exist_ok = data.get("exist_ok", True)
    if not isinstance(exist_ok, bool):
        return {"ok": False, "error": "exist_ok must be a boolean"}

    try:
        zfs_create_local(dataset, props, exist_ok=exist_ok, owner=owner)
    except RuntimeError as e:
        log.error("zfs create failed", dataset=dataset, error=str(e))
        return {"ok": False, "error": str(e)}
    except Exception as e:
        log.exception("Unexpected error creating dataset", dataset=dataset)
        return {"ok": False, "error": f"internal error: {type(e).__name__}"}

    return {"ok": True}


def _send(conn: socket.socket, response: dict[str, Any]) -> None:
    """Write one response line, tolerating a peer that already hung up."""
    try:
        # ensure_ascii keeps the payload one line and byte-safe to encode.
        conn.sendall(json.dumps(response).encode("ascii") + b"\n")
    except OSError as e:
        log.debug("Cannot send response", error=str(e))


def handle_connection(
    conn: socket.socket,
    allowed_pool: str | None,
    owner: str | None = None,
    *,
    allowed_uid: int,
) -> None:
    """Read one JSON line from a connection, process it, write the response.

    The connecting process's UID (via ``SO_PEERCRED``) is checked first and
    any other UID is rejected without touching ``zfs create``. Pass
    ``allowed_uid=ANY_UID`` to skip that check.
    """
    try:
        if allowed_uid != ANY_UID:
            try:
                peer_uid = get_peer_uid(conn)
            except (OSError, struct.error) as e:
                log.warning("SO_PEERCRED unavailable; rejecting peer", error=str(e))
                _send(conn, {"ok": False, "error": "peer auth failed"})
                return
            if peer_uid != allowed_uid:
                log.warning(
                    "Rejected ZFS agent peer",
                    peer_uid=peer_uid,
                    allowed_uid=allowed_uid,
                )
                _send(conn, {"ok": False, "error": f"unauthorized peer uid {peer_uid}"})
                return

        conn.settimeout(_REQUEST_TIMEOUT)
        try:
            # Binary mode: json.loads decodes the bytes itself. A text
            # stream would raise UnicodeDecodeError here, outside any
            # handler, on a request that is merely malformed.
            with conn.makefile("rb") as fh:
                line = fh.readline(_MAX_REQUEST + 1)
        except OSError as e:
            log.warning("Cannot read request", error=str(e))
            return
        if not line:
            log.debug("Empty request, closing connection")
            return
        if len(line) > _MAX_REQUEST:
            log.warning("Request too large", size=len(line))
            _send(conn, {"ok": False, "error": "request too large"})
            return

        try:
            data = json.loads(line)
        # Non-UTF-8 bytes raise UnicodeDecodeError, not JSONDecodeError.
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            log.warning("Received invalid JSON", error=str(e))
            response: dict[str, Any] = {"ok": False, "error": f"invalid JSON: {e}"}
        else:
            response = handle_request(data, allowed_pool, owner)
        _send(conn, response)
    finally:
        conn.close()
