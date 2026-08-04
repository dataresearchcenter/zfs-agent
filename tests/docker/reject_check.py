"""The agent must reject a peer whose UID doesn't match --allowed-uid.

Run as uid 1000 against an agent started with ``--allowed-uid 1001``. The
socket is deliberately world-writable for this check, so that SO_PEERCRED
is what does the rejecting rather than the file mode.
"""

import os

from zfs_agent.client import zfs_create_socket

assert os.getuid() == 1000, f"must run as uid 1000, got {os.getuid()}"

try:
    zfs_create_socket(os.environ["ZFS_SOCKET"], f"{os.environ['POOL_ROOT']}/nope")
except RuntimeError as exc:
    assert "unauthorized peer uid 1000" in str(exc), exc
    print("peer rejection check passed (uid 1000 against --allowed-uid 1001)")
else:
    raise AssertionError("agent accepted a peer with a mismatched UID")
