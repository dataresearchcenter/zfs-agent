[![zfs-agent on pypi](https://img.shields.io/pypi/v/zfs-agent)](https://pypi.org/project/zfs-agent/)
[![PyPI Downloads](https://static.pepy.tech/badge/zfs-agent/month)](https://pepy.tech/projects/zfs-agent)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/zfs-agent)](https://pypi.org/project/zfs-agent/)
[![Python test and package](https://github.com/dataresearchcenter/zfs-agent/actions/workflows/python.yml/badge.svg)](https://github.com/dataresearchcenter/zfs-agent/actions/workflows/python.yml)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![Coverage Status](https://coveralls.io/repos/github/dataresearchcenter/zfs-agent/badge.svg?branch=main)](https://coveralls.io/github/dataresearchcenter/zfs-agent?branch=main)
[![AGPLv3+ License](https://img.shields.io/pypi/l/zfs-agent)](./LICENSE)


# zfs-agent

ZFS dataset management for unprivileged users via a Unix domain socket.

A host-side agent runs with ZFS privileges and executes validated `zfs create` requests on behalf of clients that lack ZFS tools or privileges – typically containers. Used in [ftm-lakehouse](https://openaleph.org/docs/lib/ftm-lakehouse/deployment/zfs/).

Linux only (peer authentication relies on `SO_PEERCRED`), Python 3.10+, no
dependencies.

## Install

    pip install zfs-agent

## Usage

Run the agent on the host (as a user that may run `zfs create`, typically root):

    zfs-agent --socket /run/zfs.sock --pool tank/data --owner 1000:1000 --allowed-uid 1000

- `--pool` restricts requests to datasets below this path (env: `ZFS_POOL`)
- `--owner` chowns new dataset mountpoints to this `uid:gid` (env: `ZFS_OWNER`)
- `--allowed-uid` only accepts connections from this UID, verified via
  `SO_PEERCRED`; defaults to the agent's own UID (env: `ZFS_ALLOWED_UID`)
- `--log-level` sets the agent's log verbosity, default `INFO` (env:
  `ZFS_LOG_LEVEL`). Only the CLI configures logging; imported as a library,
  the package logs through `logging` without attaching handlers.

Clients may only set ZFS properties from a built-in allowlist of tuning
knobs (`compression`, `recordsize`, `atime`, `quota`, …). Set
`ZFS_EXTRA_PROPS` on the agent to add more, comma separated:

    ZFS_EXTRA_PROPS=canmount,readonly zfs-agent --socket /run/zfs.sock --pool tank/data

The effective allowlist is logged at startup.

Create datasets from the client side (e.g. inside a container that mounts the
socket):

```python
from zfs_agent.client import zfs_create_socket

zfs_create_socket("/run/zfs.sock", "tank/data/my_dataset", compression="zstd")
```

Or set `ZFS_SOCKET=/run/zfs.sock` in the environment and let the dispatcher choose between socket and local `zfs create`:

```python
from zfs_agent import zfs_create

zfs_create("tank/data/my_dataset", compression="zstd")
```

## Security

- The socket is created mode `0600`, owned by the allowed UID.
- The peer's UID is verified via `SO_PEERCRED` before the request is read.
- Dataset names are validated (no path traversal, restricted characters) and
  must live under the configured pool.
- ZFS properties are checked against an allowlist. Properties such as
  `mountpoint`, `sharenfs` or `setuid` would otherwise let a client steer
  what the privileged side touches.
- Requests are size capped and time limited, and a malformed one is answered
  with an error rather than taking the agent down.
- The only supported action is `create`.

## Tests

    make test         # unit tests
    make test-docker  # integration test (privileged container, ZFS on the host)

## License

AGPLv3+
