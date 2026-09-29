"""Listening socket: bind it, accept on it, shut it down cleanly."""

import os
import signal
import socket
import threading
from typing import Any

from zfs_agent.agent import DEFAULT_ACTIONS, handle_connection
from zfs_agent.logs import get_logger
from zfs_agent.validate import allowed_props

log = get_logger(__name__)


def _bind(socket_path: str, allowed_uid: int) -> socket.socket:
    """Return a listening socket at ``socket_path``, reachable by that UID."""
    # lexists, not exists: a dangling symlink here would leave a stale path
    # that bind() then fails on with EADDRINUSE.
    if os.path.lexists(socket_path):
        os.unlink(socket_path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Create the socket 0600 in one step. A chmod after bind() leaves a
    # window where the mode follows the umask, and chmod by path follows
    # symlinks, so a path swapped in during that window would be retargeted.
    old_umask = os.umask(0o177)
    try:
        server.bind(socket_path)
    finally:
        os.umask(old_umask)
    if allowed_uid != os.getuid():
        # Connecting requires write permission on the socket inode, so hand
        # it to the client UID (mode stays 0600). lchown so a symlink
        # swapped in after bind() cannot redirect the ownership change.
        os.chown(socket_path, allowed_uid, -1, follow_symlinks=False)
    server.listen(5)
    return server


def serve(
    socket_path: str,
    pool: str,
    owner: str | None = None,
    *,
    allowed_uid: int,
    actions: frozenset[str] = DEFAULT_ACTIONS,
) -> None:
    """Serve requests until SIGINT or SIGTERM arrives.

    Each connection gets its own thread: a ``send`` or ``receive`` lasts as
    long as its stream, which must not hold up anybody's ``create``.
    """
    server = _bind(socket_path, allowed_uid)
    log.info(
        "zfs-agent listening",
        socket=socket_path,
        pool=pool,
        owner=owner,
        allowed_uid=allowed_uid,
        actions=",".join(sorted(actions)),
        allowed_props=sorted(allowed_props()),
    )

    def _handle(conn: socket.socket) -> None:
        try:
            handle_connection(
                conn, pool, owner, allowed_uid=allowed_uid, actions=actions
            )
        except Exception:
            # One bad request must not take the daemon down.
            log.exception("Error handling connection")

    threads: list[threading.Thread] = []
    stopping = False

    def _shutdown(*args: Any) -> None:
        nonlocal stopping
        log.info("Shutting down zfs-agent")
        stopping = True
        # Unblocks accept() so the loop exits; in-flight requests are
        # joined below instead of being killed mid-operation.
        server.close()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while not stopping:
            try:
                conn, _ = server.accept()
            except OSError as e:
                if not stopping:
                    log.error("Cannot accept connections", error=str(e))
                break
            threads = [t for t in threads if t.is_alive()]
            thread = threading.Thread(target=_handle, args=(conn,))
            thread.start()
            threads.append(thread)
    finally:
        server.close()
        if os.path.lexists(socket_path):
            os.unlink(socket_path)
        # A stream can take hours: new clients get ENOENT meanwhile rather
        # than a socket nobody accepts on.
        for thread in threads:
            thread.join()
