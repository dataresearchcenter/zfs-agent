"""ZFS CLI: the socket agent.

zfs-agent --socket /run/zfs.sock --pool tank/data    # host-side
"""

import os
from typing import Annotated, Optional

import typer

from zfs_agent.server import serve
from zfs_agent.settings import Settings

cli = typer.Typer(no_args_is_help=True)


def _required(value: Optional[str], name: str, flag: str, env: str) -> str:
    """Exit with a usage error if neither the flag nor the env var is set."""
    if not value:
        typer.secho(
            f"No {name} specified. Use {flag} or set {env}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    return value


@cli.command("agent")
def cli_zfs_agent(
    socket_path: Annotated[
        Optional[str],
        typer.Option("--socket", "-s", help="Unix socket path to listen on"),
    ] = None,
    pool: Annotated[
        Optional[str],
        typer.Option(
            "--pool",
            "-p",
            help="ZFS pool path (or set ZFS_POOL)",
        ),
    ] = None,
    owner: Annotated[
        Optional[str],
        typer.Option(
            "--owner",
            "-o",
            help="uid:gid to chown new mountpoints to (or set ZFS_OWNER)",
        ),
    ] = None,
    allowed_uid: Annotated[
        Optional[int],
        typer.Option(
            "--allowed-uid",
            help=(
                "Only accept connections from this UID (checked via "
                "SO_PEERCRED). Defaults to the agent's own UID, i.e. only "
                "the user running the agent can call it. Override to "
                "grant a different client UID (e.g. the container user)."
            ),
        ),
    ] = None,
) -> None:
    """Start a ZFS socket agent for container-based deployments.

    Listens on a Unix socket and executes ``zfs create`` commands on behalf
    of containerized clients that lack local ZFS tools.
    """
    settings = Settings()
    sock_path = _required(
        socket_path or settings.zfs_socket, "socket path", "--socket", "ZFS_SOCKET"
    )
    zfs_pool = _required(pool or settings.zfs_pool, "pool", "--pool", "ZFS_POOL")

    if allowed_uid is None:
        allowed_uid = (
            settings.zfs_allowed_uid
            if settings.zfs_allowed_uid is not None
            else os.getuid()
        )

    serve(
        sock_path,
        zfs_pool,
        owner or settings.zfs_owner,
        allowed_uid=allowed_uid,
    )
