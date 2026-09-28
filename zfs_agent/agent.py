"""Serving one connection: peer authentication, protocol, dispatch."""

import json
import os
import socket
import struct
from typing import Any

from zfs_agent.logs import get_logger
from zfs_agent.validate import (
    validate_dataset,
    validate_props,
    validate_snapshot,
    validate_snapshot_name,
    validate_token,
)
from zfs_agent.zfs import (
    zfs_abort_local,
    zfs_create_local,
    zfs_receive_local,
    zfs_send_local,
    zfs_snapshot_local,
    zfs_status_local,
)

log = get_logger(__name__)

# A peer that connects and stays quiet would hold a thread forever, and an
# endless line would grow the root process without bound.
_REQUEST_TIMEOUT = 10.0
_MAX_REQUEST = 64 * 1024

ACTIONS = frozenset({"create", "status", "snapshot", "send", "receive", "abort"})
# Streaming send/receive and snapshots widen what a client may do with the
# pool, so an agent serves only ``create`` unless told otherwise.
DEFAULT_ACTIONS = frozenset({"create"})
# The actions that stream through a file descriptor passed with the request.
_FD_ACTIONS = frozenset({"send", "receive"})

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


def _optional_bool(data: dict[str, Any], key: str, default: bool) -> bool | str:
    """Return the bool at ``key`` or an error string."""
    value = data.get(key, default)
    if not isinstance(value, bool):
        return f"{key} must be a boolean"
    return value


def _handle_create(
    data: dict[str, Any], pool: str | None, owner: str | None, fd: int | None
) -> dict[str, Any]:
    dataset = data.get("dataset", "")
    props, err = validate_props(data.get("props") or {})
    if err:
        log.warning("Property validation failed", dataset=dataset, error=err)
        return {"ok": False, "error": err}
    exist_ok = _optional_bool(data, "exist_ok", True)
    if isinstance(exist_ok, str):
        return {"ok": False, "error": exist_ok}
    zfs_create_local(dataset, props, exist_ok=exist_ok, owner=owner)
    return {"ok": True}


def _handle_status(
    data: dict[str, Any], pool: str | None, owner: str | None, fd: int | None
) -> dict[str, Any]:
    return {"ok": True, "status": zfs_status_local(data["dataset"])}


def _handle_snapshot(
    data: dict[str, Any], pool: str | None, owner: str | None, fd: int | None
) -> dict[str, Any]:
    snapshots = data.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return {"ok": False, "error": "snapshots must be a non-empty list"}
    for snapshot in snapshots:
        err = validate_snapshot(snapshot, pool)
        if err:
            return {"ok": False, "error": err}
    zfs_snapshot_local(*snapshots)
    return {"ok": True}


def _handle_send(
    data: dict[str, Any], pool: str | None, owner: str | None, fd: int | None
) -> dict[str, Any]:
    assert fd is not None
    token = data.get("token")
    if token is not None:
        err = validate_token(token)
        if err:
            return {"ok": False, "error": err}
        zfs_send_local(data["dataset"], fd, token=token)
        return {"ok": True}
    snapshot, since = data.get("snapshot"), data.get("since")
    err = validate_snapshot_name(snapshot)
    if not err and since is not None:
        err = validate_snapshot_name(since)
    if err:
        return {"ok": False, "error": err}
    zfs_send_local(data["dataset"], fd, snapshot=snapshot, since=since)
    return {"ok": True}


def _handle_receive(
    data: dict[str, Any], pool: str | None, owner: str | None, fd: int | None
) -> dict[str, Any]:
    assert fd is not None
    props, err = validate_props(data.get("props") or {})
    if err:
        return {"ok": False, "error": err}
    force = _optional_bool(data, "force", False)
    if isinstance(force, str):
        return {"ok": False, "error": force}
    resumable = _optional_bool(data, "resumable", False)
    if isinstance(resumable, str):
        return {"ok": False, "error": resumable}
    zfs_receive_local(
        data["dataset"],
        fd,
        props,
        force=force,
        resumable=resumable,
        owner=owner,
    )
    return {"ok": True}


def _handle_abort(
    data: dict[str, Any], pool: str | None, owner: str | None, fd: int | None
) -> dict[str, Any]:
    zfs_abort_local(data["dataset"])
    return {"ok": True}


_HANDLERS = {
    "create": _handle_create,
    "status": _handle_status,
    "snapshot": _handle_snapshot,
    "send": _handle_send,
    "receive": _handle_receive,
    "abort": _handle_abort,
}


def handle_request(
    data: Any,
    allowed_pool: str | None,
    owner: str | None = None,
    fd: int | None = None,
    actions: frozenset[str] = DEFAULT_ACTIONS,
) -> dict[str, Any]:
    """Process a single JSON request and return a response dict.

    ``fd`` is the file descriptor passed along with the request, which
    ``send`` writes its stream to and ``receive`` reads from. The caller
    owns it and closes it afterwards.
    """
    if not isinstance(data, dict):
        log.warning("Malformed request", type=type(data).__name__)
        return {"ok": False, "error": "request must be a JSON object"}

    action = data.get("action")
    if action not in _HANDLERS:
        log.warning("Unknown action requested", action=action)
        return {"ok": False, "error": f"unknown action: {action!r}"}
    if action not in actions:
        log.warning("Action not enabled", action=action)
        return {"ok": False, "error": f"action not allowed: {action!r}"}
    if (fd is not None) != (action in _FD_ACTIONS):
        return {"ok": False, "error": f"file descriptor mismatch for {action!r}"}

    # ``snapshot`` names its datasets inside ``snapshots``.
    if action != "snapshot":
        dataset = data.get("dataset", "")
        err = validate_dataset(dataset, allowed_pool)
        if err:
            log.warning("Dataset validation failed", dataset=dataset, error=err)
            return {"ok": False, "error": err}

    try:
        return _HANDLERS[action](data, allowed_pool, owner, fd)
    except RuntimeError as e:
        log.error(f"zfs {action} failed", error=str(e))
        return {"ok": False, "error": str(e)}
    except Exception as e:
        log.exception(f"Unexpected error in {action}")
        return {"ok": False, "error": f"internal error: {type(e).__name__}"}


def _send(conn: socket.socket, response: dict[str, Any]) -> None:
    """Write one response line, tolerating a peer that already hung up."""
    try:
        # ensure_ascii keeps the payload one line and byte-safe to encode.
        conn.sendall(json.dumps(response).encode("ascii") + b"\n")
    except OSError as e:
        log.debug("Cannot send response", error=str(e))


def _read_request(conn: socket.socket) -> tuple[bytes, list[int]]:
    """Read one request line and the file descriptors sent along with it.

    Binary on purpose: ``json.loads`` decodes the bytes itself, where a text
    stream would raise UnicodeDecodeError outside any handler on a request
    that is merely malformed. Returns at most ``_MAX_REQUEST + 1`` bytes, so
    the caller can tell an oversized line apart. File descriptors arrive
    with the first chunk only – the client sends them in one ``sendmsg``.
    """
    chunk, fds, _flags, _addr = socket.recv_fds(conn, _MAX_REQUEST + 1, 1)
    buf = bytearray(chunk)
    while chunk and b"\n" not in buf and len(buf) <= _MAX_REQUEST:
        chunk = conn.recv(_MAX_REQUEST + 1 - len(buf))
        buf += chunk
    line, newline, _rest = bytes(buf).partition(b"\n")
    return line + newline, fds


def handle_connection(
    conn: socket.socket,
    allowed_pool: str | None,
    owner: str | None = None,
    *,
    allowed_uid: int,
    actions: frozenset[str] = DEFAULT_ACTIONS,
) -> None:
    """Read one JSON line from a connection, process it, write the response.

    The connecting process's UID (via ``SO_PEERCRED``) is checked first and
    any other UID is rejected without touching ``zfs``. Pass
    ``allowed_uid=ANY_UID`` to skip that check.
    """
    fds: list[int] = []
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
            line, fds = _read_request(conn)
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
            fd = fds[0] if fds else None
            response = handle_request(data, allowed_pool, owner, fd, actions)
        _send(conn, response)
    finally:
        for fd in fds:
            os.close(fd)
        conn.close()
