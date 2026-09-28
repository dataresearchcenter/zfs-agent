"""ZFS CLI: the socket agent.

zfs-agent --socket /run/zfs.sock --pool tank/data    # host-side
"""

import argparse
import os
from typing import Optional

from zfs_agent.agent import ACTIONS, DEFAULT_ACTIONS
from zfs_agent.logs import configure
from zfs_agent.server import serve
from zfs_agent.settings import Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zfs-agent",
        description=(
            "Start a ZFS socket agent for container-based deployments. "
            "Listens on a Unix socket and executes `zfs` commands on behalf "
            "of containerized clients that lack local ZFS tools."
        ),
    )
    parser.add_argument(
        "-s",
        "--socket",
        dest="socket_path",
        metavar="PATH",
        help="Unix socket path to listen on (or set ZFS_SOCKET)",
    )
    parser.add_argument(
        "-p",
        "--pool",
        metavar="POOL",
        help="ZFS pool path (or set ZFS_POOL)",
    )
    parser.add_argument(
        "-o",
        "--owner",
        metavar="UID:GID",
        help="uid:gid to chown new mountpoints to (or set ZFS_OWNER)",
    )
    parser.add_argument(
        "-l",
        "--log-level",
        metavar="LEVEL",
        help="DEBUG, INFO, WARNING, ERROR (or set ZFS_LOG_LEVEL). Default INFO",
    )
    parser.add_argument(
        "--allowed-uid",
        type=int,
        metavar="UID",
        help=(
            "Only accept connections from this UID (checked via SO_PEERCRED). "
            "Defaults to the agent's own UID, i.e. only the user running the "
            "agent can call it. Override to grant a different client UID "
            "(e.g. the container user). Or set ZFS_ALLOWED_UID."
        ),
    )
    parser.add_argument(
        "--actions",
        metavar="ACTIONS",
        help=(
            "Comma-separated actions to serve, out of "
            f"{','.join(sorted(ACTIONS))}. Default: create "
            "(or set ZFS_ACTIONS)"
        ),
    )
    return parser


def _required(
    parser: argparse.ArgumentParser,
    value: Optional[str],
    name: str,
    flag: str,
    env: str,
) -> str:
    """Bail out with a usage error if neither the flag nor the env var is set."""
    if not value:
        parser.error(f"no {name} specified: use {flag} or set {env}")
    return value


def cli(argv: Optional[list[str]] = None) -> None:
    """Entry point: resolve flags against the environment, then serve."""
    parser = _parser()
    args = parser.parse_args(argv)

    settings = Settings()
    level = (args.log_level or settings.zfs_log_level).upper()
    try:
        configure(level)
    except ValueError:
        parser.error(f"unknown log level: {level!r}")

    sock_path = _required(
        parser,
        args.socket_path or settings.zfs_socket,
        "socket path",
        "--socket",
        "ZFS_SOCKET",
    )
    pool = _required(
        parser, args.pool or settings.zfs_pool, "pool", "--pool", "ZFS_POOL"
    )

    allowed_uid = args.allowed_uid
    if allowed_uid is None:
        allowed_uid = (
            settings.zfs_allowed_uid
            if settings.zfs_allowed_uid is not None
            else os.getuid()
        )

    actions = settings.zfs_actions or DEFAULT_ACTIONS
    if args.actions:
        actions = frozenset(a.strip() for a in args.actions.split(",") if a.strip())
    unknown = actions - ACTIONS
    if unknown:
        parser.error(f"unknown actions: {','.join(sorted(unknown))}")

    serve(
        sock_path,
        pool,
        args.owner or settings.zfs_owner,
        allowed_uid=allowed_uid,
        actions=actions,
    )
