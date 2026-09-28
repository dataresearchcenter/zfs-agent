[![zfs-agent on pypi](https://img.shields.io/pypi/v/zfs-agent)](https://pypi.org/project/zfs-agent/)
[![PyPI Downloads](https://static.pepy.tech/badge/zfs-agent/month)](https://pepy.tech/projects/zfs-agent)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/zfs-agent)](https://pypi.org/project/zfs-agent/)
[![Python test and package](https://github.com/dataresearchcenter/zfs-agent/actions/workflows/python.yml/badge.svg)](https://github.com/dataresearchcenter/zfs-agent/actions/workflows/python.yml)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![Coverage Status](https://coveralls.io/repos/github/dataresearchcenter/zfs-agent/badge.svg?branch=main)](https://coveralls.io/github/dataresearchcenter/zfs-agent?branch=main)
[![AGPLv3+ License](https://img.shields.io/pypi/l/zfs-agent)](./LICENSE)


# zfs-agent

ZFS dataset management for unprivileged users via a Unix domain socket.

A host-side agent runs with ZFS privileges and executes validated `zfs` requests – `create`, and optionally snapshots and streaming `send` / `receive` – on behalf of clients that lack ZFS tools or privileges, typically containers. Used in [ftm-lakehouse](https://openaleph.org/docs/lib/ftm-lakehouse/deployment/zfs/).

Linux only (peer authentication relies on `SO_PEERCRED`), Python 3.10+, no dependencies.

## Install

    pip install zfs-agent

## Usage

Run the agent on the host (as a user that may run `zfs create`, typically root):

    zfs-agent --socket /run/zfs.sock --pool tank/data --owner 1000:1000 --allowed-uid 1000

- `--pool` restricts requests to datasets below this path (env: `ZFS_POOL`)
- `--owner` chowns new dataset mountpoints to this `uid:gid` (env: `ZFS_OWNER`)
- `--allowed-uid` only accepts connections from this UID, verified via
  `SO_PEERCRED`; defaults to the agent's own UID (env: `ZFS_ALLOWED_UID`)
- `--log-level` sets the agent's log verbosity, default `INFO` (env: `ZFS_LOG_LEVEL`). Only the CLI configures logging; imported as a library, the package logs through `logging` without attaching handlers.
- `--actions` sets which actions the agent serves, comma separated, out of `create`, `status`, `snapshot`, `send`, `receive`, `abort`; default `create` (env: `ZFS_ACTIONS`). See [Streaming](#streaming).

Clients may only set ZFS properties from a built-in allowlist of tuning knobs (`compression`, `recordsize`, `atime`, `quota`, …). Set `ZFS_EXTRA_PROPS` on the agent to add more, comma separated:

    ZFS_EXTRA_PROPS=canmount,readonly zfs-agent --socket /run/zfs.sock --pool tank/data

The effective allowlist is logged at startup.

Create datasets from the client side (e.g. inside a container that mounts the socket):

```python
from zfs_agent.client import zfs_create_socket

zfs_create_socket("/run/zfs.sock", "tank/data/my_dataset", compression="zstd")
```

Or set `ZFS_SOCKET=/run/zfs.sock` in the environment and let the dispatcher choose between socket and local `zfs create`:

```python
from zfs_agent import zfs_create

zfs_create("tank/data/my_dataset", compression="zstd")
```

## Streaming

With `--actions create,status,snapshot,send,receive,abort` the agent also reports snapshots, takes them, streams datasets and discards a partial receive (`zfs receive -A`). The stream never passes through the socket: the client hands the agent one end of a pipe (`SCM_RIGHTS`), `zfs send` writes into it or `zfs receive` reads from it, and the agent answers once `zfs` has exited. Every connection is served on its own thread, so they are not blocking each other.

```python
import os
from zfs_agent import zfs_receive, zfs_send, zfs_snapshot, zfs_status

zfs_snapshot("tank/data/ds@s2", "tank/data/ds/child@s2")  # atomic
zfs_status("tank/data/ds")
# {"exists": True, "snapshots": [{"name": "s1", "guid": "…", "createtxg": 5}, …],
#  "resume_token": None}

r, w = os.pipe()
zfs_send("tank/data/ds", w, snapshot="s2", since="s1")  # blocks: run it on a thread
zfs_receive("tank/data/copy", r, force=True, resumable=True)
```

The same functions run `zfs` locally when `ZFS_SOCKET` is unset. The caller owns the descriptor and closes it once the call returns – closing the reading end makes a running `send` fail rather than block. Sends use `zfs send -c -L -e -w` (raw: an encrypted dataset stays encrypted) and never `-p`; `send(…, token=…)` resumes an interrupted resumable receive, whose token `zfs_status` reports – for a dataset the interrupted receive would have created, from its `%recv` partial state. `zfs send -t` ignores any dataset argument, so the agent first reads the token's target (`zfs send -n -v -t`) and refuses a token for any other dataset than the requested one. `zfs_abort(dataset)` discards a partial receive that can't or shouldn't be resumed. Snapshot names may contain colons (sanoid / zrepl timestamps); dataset names may not.

## Security

- The socket is created mode `0600`, owned by the allowed UID.
- The peer's UID is verified via `SO_PEERCRED` before the request is read.
- Dataset names are validated (no path traversal, restricted characters) and must live under the configured pool.
- ZFS properties are checked against an allowlist. Properties such as `mountpoint`, `sharenfs` or `setuid` would otherwise let a client steer what the privileged side touches.
- Requests are size capped and time limited, and a malformed one is answered with an error rather than taking the agent down.
- Only `create` is served unless `--actions` enables more. `snapshot`, `send` and `receive` read and replace whole datasets under the pool, and `receive -F` rolls a dataset back – enable them only where the client UID is trusted with that.
- `receive` excludes `mountpoint`, `canmount`, `sharenfs`, `sharesmb`, `setuid`, `exec` and `devices` from the incoming stream (`-x`): the stream is client data, and those properties would let it choose where root mounts the dataset or export it.

## Tests

    make test         # unit tests
    make test-docker  # integration test (privileged container, ZFS on the host)

## License

AGPLv3+
