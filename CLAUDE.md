# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A privilege-separation daemon: an unprivileged (e.g. containerized) client requests `zfs create` operations over a Unix domain socket, and a host-side agent running with ZFS privileges validates and executes them. Linux-only (relies on `SO_PEERCRED`). Python 3.11+, managed with Poetry.

No runtime dependencies, deliberately: `dependencies = []` in `pyproject.toml`, and the package must stay importable in a bare interpreter. Anything new goes in the dev group.

The code is the "socket agent mode" of ftm-lakehouse's ZFS integration, extracted into a standalone package. The upstream design (deployment modes, container setup, security model) is documented at <https://openaleph.org/docs/lib/ftm-lakehouse/deployment/zfs/>; the standalone package renames the env vars from `LAKEHOUSE_ZFS_*` to `ZFS_*`.

## Behaviour rules for code agents

1. Don’t assume. Don’t hide confusion. Surface tradeoffs.
2. Minimum code that solves the problem. Nothing speculative.
3. Touch only what you must. Clean up only your own mess.
4. Define success criteria. Loop until verified.
5. Never use em-dashes in prose, only en-dashes (–).

## Commands

```sh
make install       # poetry install --with dev
make test          # pytest with coverage (unit tests only)
make lint          # flake8 over zfs_agent and tests
make typecheck     # mypy --strict
make pre-commit    # install + run all pre-commit hooks
make test-docker   # ZFS integration test in a privileged Debian container
make build         # poetry build
```

Run a single test: `poetry run pytest tests/test_unit_zfs.py::TestPeerAuth::test_get_peer_uid_returns_current_uid -v`

Formatting/linting is black + isort (`--profile black`) + flake8 (E203/E501/W503 ignored), enforced via pre-commit. `make lint` has no `--exit-zero` pass, so it fails CI on a real finding.

## Architecture

Client/server pair around a one-line JSON protocol, in `zfs_agent/`. The server side is layered `cli → server → agent → validate → zfs`, each module depending only on the ones after it; the four validation layers a request passes through map onto that chain in order.

- **`cli.py`** – `argparse` entry point; flat flags, no subcommand (`zfs-agent --socket ... --pool ...`). Resolves flags against `Settings` env vars and calls `serve()`; no socket handling of its own. Missing required values go through `parser.error()`, so they print usage and exit 2 like any other argparse usage error. `cli(argv=None)` takes an argument list so it is callable without patching `sys.argv`.
- **`server.py`** – `serve()` owns the listening socket and the accept loop. Two things that look incidental but are not: the socket is created `0600` by setting the umask around `bind()` (a `chmod` afterwards leaves a permissions window and follows symlinks) and ownership is handed to `allowed_uid` with `follow_symlinks=False`, since connecting requires write permission on the socket inode. The accept loop catches every exception per connection, and SIGINT/SIGTERM set a flag and close the listener rather than calling `sys.exit()` from the handler, so an in-flight `zfs create` finishes.
- **`agent.py`** – everything about serving one connection. `handle_connection()` authenticates the peer, reads one JSON line, dispatches to `handle_request()`, writes one response line, closes. It is written so that nothing a client can send escapes as an exception: the peer socket is read in binary (a text stream would raise `UnicodeDecodeError` outside any handler), reads are capped by `_MAX_REQUEST` and `_REQUEST_TIMEOUT`, responses go through `_send()` which tolerates a peer that hung up, and `handle_request()` type-checks the payload before touching it. Peer UID comes from `SO_PEERCRED` (`get_peer_uid()`); `allowed_uid` is keyword-only and required, and `ANY_UID` is the explicit opt-out, so omitting it is a `TypeError` rather than a silent fail-open.
- **`validate.py`** – the untrusted-input layer, called by `handle_request()` and by nothing else.
  - `validate_dataset()` rejects non-strings, illegal components (which also covers `..` traversal), and anything not equal to or below `allowed_pool`. The pool check needs the `/` boundary: a bare prefix match lets `tank/database` pass for pool `tank/data`.
  - `validate_props()` is an allowlist, since ZFS properties reach `zfs create -o` on the privileged side. `mountpoint` alone is a root escalation, because it redirects the mountpoint that `--owner` then chowns. The base set in `_DEFAULT_PROPS` is extended by `ZFS_EXTRA_PROPS` on the agent (`allowed_props()`), and the effective set is logged at startup.
- **`zfs.py`** – the only module that shells out. `zfs_create_local()` probes `zfs list` first (a pre-existing dataset is an error only when `exist_ok=False`; `zfs create -p` alone can’t tell, it exits 0 either way), then runs the create. `_chown_mountpoint()` logs failures instead of raising, and skips anything that isn’t an absolute path (`-`, `none`, `legacy`).
- **`client.py`** – `zfs_create_socket(socket_path, dataset, exist_ok=True, **props)` talks to the agent; `zfs_create(dataset, exist_ok=True, **props)` dispatches: socket when `Settings().zfs_socket` is set, else `zfs_create_local`. ZFS properties are always keywords. `Settings` and `zfs_create_local` are resolved through module attributes at call time so test patches (`zfs_agent.settings.Settings`, `zfs_agent.client.zfs_create_socket`, `zfs_agent.zfs.zfs_create_local`) take effect. Tests that drive the server instead patch `zfs_agent.agent.zfs_create_local`, the name `handle_request()` actually calls.
- **`settings.py`** – `Settings` reads `ZFS_SOCKET`, `ZFS_POOL`, `ZFS_OWNER` in `__init__`; `zfs_allowed_uid`, `zfs_log_level` and `zfs_extra_props` are properties parsed on access, so a malformed value only breaks the code that reads it.
- **`logs.py`** – the `log.warning("Event", dataset=name)` call style the package uses, rendered onto stdlib `logging` as `key=value` suffixes (this is what `structlog` used to provide). Only `cli.py` calls `configure()`; imported as a library the package attaches no handlers, so it inherits the application's logging setup.

Protocol: newline-terminated JSON both ways. Requests are `{"action": "create", "dataset": ..., "props": {...}, "exist_ok": bool}`, responses `{"ok": true}` or `{"ok": false, "error": "..."}`. `exist_ok` is on the wire on purpose: without it the flag would mean different things locally and remotely. The only action is `create`.

## Tests

- `tests/test_unit_zfs.py` – unit tests. `zfs_create_local` is mocked, sockets are real (socketpairs and tmp-path sockets). Fake agents run through `_FakeAgent`, which keeps assertions out of the worker thread (an `AssertionError` there only reaches `threading.excepthook` and gets misattributed) and asserts the thread finished.
- `tests/docker/` – integration test (`make test-docker`): a Debian container runs the agent as root and the client as uid 1000 against a real throwaway zpool. Phase 1 covers creation, mountpoint chown, idempotent re-create, `exist_ok=False`, the property allowlist, pool confinement, and a barrage of malformed input that the daemon must survive. Phase 2 starts a second agent with `--allowed-uid 1001` and deliberately opens the socket to `0666`, so it is `SO_PEERCRED` and not the file mode that rejects the uid-1000 client. The check scripts are named `*_check.py`, never `*_test.py`, so pytest cannot collect them from any working directory.

Requires `docker run --privileged` on a host with ZFS (`/dev/zfs` present); CI runs it on ubuntu-latest after `sudo modprobe zfs` (`.github/workflows/integration.yml`). Two constraints baked into the harness:
  - The container’s zfs userland must match the host kernel module’s interface version (trixie-backports 2.4.x here; stock trixie 2.3 against a 2.4 kmod fails with "no such pool or dataset").
  - File vdevs can’t be created from container-local paths (the kernel resolves them in the host mount namespace), so the entrypoint attaches the image to a loop device, mknod’ing the node manually since the container has no udev. Pool and loop device are host-kernel state; the EXIT trap stops the agents first, then destroys and detaches.

## Known gaps

- `.bumpversion.cfg` still points at `ftmq/__init__.py` and carries the old project’s version (4.10.0 vs 0.0.0 in `pyproject.toml`/`VERSION`).
- The agent forwards `zfs` stderr to the client on failure. Useful for debugging, mildly leaky about host paths; deliberate, since the peer is UID-authenticated and confined to its own pool.
