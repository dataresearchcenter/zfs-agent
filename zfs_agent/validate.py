"""Validation of everything a client sends: dataset paths and properties."""

import re
from typing import Any

from zfs_agent.settings import Settings

_ZFS_COMPONENT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")
_PROP_VALUE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*\Z")

# ZFS properties a client may set, extended by ``ZFS_EXTRA_PROPS`` on the
# agent. Anything outside the result is refused: ``mountpoint`` steers the
# root-side chown at an arbitrary path, ``sharenfs``/``sharesmb`` export the
# dataset, ``setuid``/``exec``/``devices`` weaken mount options and
# ``keylocation`` makes root read a file of the client's choosing.
_DEFAULT_PROPS = frozenset(
    {
        "atime",
        "checksum",
        "compression",
        "copies",
        "dnodesize",
        "logbias",
        "primarycache",
        "quota",
        "recordsize",
        "redundant_metadata",
        "refquota",
        "refreservation",
        "relatime",
        "reservation",
        "secondarycache",
        "snapdir",
        "sync",
        "xattr",
    }
)


def validate_dataset(dataset: Any, allowed_pool: str | None) -> str | None:
    """Validate a ZFS dataset path. Returns an error string or None if valid.

    Every path component must be a legal ZFS name (alphanumeric, hyphens,
    dots, underscores), which also rules out ``..`` traversal, and the path
    must sit under ``allowed_pool``.
    """
    if not isinstance(dataset, str):
        return f"dataset must be a string, got {type(dataset).__name__}"
    if not dataset:
        return "empty dataset name"

    for part in dataset.split("/"):
        if not _ZFS_COMPONENT_RE.match(part):
            return f"invalid path component: {part!r}"

    # A prefix match alone would let ``tank/database`` pass for pool
    # ``tank/data``, so require the pool itself or a child of it.
    if allowed_pool and not (
        dataset == allowed_pool or dataset.startswith(f"{allowed_pool}/")
    ):
        return f"dataset {dataset!r} not under pool {allowed_pool!r}"
    return None


def allowed_props() -> frozenset[str]:
    """The built-in property allowlist plus ``ZFS_EXTRA_PROPS``."""
    return _DEFAULT_PROPS | Settings().zfs_extra_props


def validate_props(props: Any) -> tuple[dict[str, str], str | None]:
    """Validate client-supplied ZFS properties against the allowlist.

    Returns the cleaned properties and an error string (None if valid).
    """
    if not isinstance(props, dict):
        return {}, "props must be a dict"
    allowed = allowed_props()
    clean: dict[str, str] = {}
    for key, value in props.items():
        if not isinstance(key, str) or key not in allowed:
            return {}, f"property not allowed: {key!r}"
        # bool is an int subclass but ``True`` is not a ZFS value.
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return {}, f"invalid value for property {key!r}"
        text = str(value)
        if not _PROP_VALUE_RE.match(text):
            return {}, f"invalid value for property {key!r}: {text!r}"
        clean[key] = text
    return clean, None
