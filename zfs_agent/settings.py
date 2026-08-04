import os


class Settings:
    def __init__(self) -> None:
        self.zfs_owner: str | None = os.environ.get("ZFS_OWNER")
        self.zfs_socket: str | None = os.environ.get("ZFS_SOCKET")
        self.zfs_pool: str | None = os.environ.get("ZFS_POOL")

    # Parsed on access, not in __init__: a malformed value must not break
    # clients that never read the field.
    @property
    def zfs_allowed_uid(self) -> int | None:
        value = os.environ.get("ZFS_ALLOWED_UID")
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            raise ValueError(
                f"ZFS_ALLOWED_UID must be an integer, got {value!r}"
            ) from None

    @property
    def zfs_extra_props(self) -> frozenset[str]:
        """ZFS properties clients may set on top of the built-in allowlist."""
        value = os.environ.get("ZFS_EXTRA_PROPS", "")
        return frozenset(p.strip() for p in value.split(",") if p.strip())
