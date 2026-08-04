"""Key-value logging on top of the stdlib ``logging`` module.

Keeps the ``log.warning("Event happened", dataset=name)`` call style used
throughout the package, rendering the keywords as ``key=value`` suffixes.
Only the CLI calls ``configure()``: as a library, we attach no handlers and
leave the application's logging setup alone.
"""

import logging
from typing import Any

_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _value(value: Any) -> str:
    """Render one value, quoting only what would otherwise be ambiguous."""
    text = str(value)
    if not text or any(c.isspace() for c in text):
        return repr(value)
    return text


def _render(event: str, fields: dict[str, Any]) -> str:
    if not fields:
        return event
    return event + " " + " ".join(f"{k}={_value(v)}" for k, v in fields.items())


class Logger:
    """The five log methods the package uses, each taking keyword fields."""

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    # stacklevel=2 so %(filename)s/%(lineno)d point at the caller, not here.
    def debug(self, event: str, **fields: Any) -> None:
        self._log.debug(_render(event, fields), stacklevel=2)

    def info(self, event: str, **fields: Any) -> None:
        self._log.info(_render(event, fields), stacklevel=2)

    def warning(self, event: str, **fields: Any) -> None:
        self._log.warning(_render(event, fields), stacklevel=2)

    def error(self, event: str, **fields: Any) -> None:
        self._log.error(_render(event, fields), stacklevel=2)

    def exception(self, event: str, **fields: Any) -> None:
        self._log.error(_render(event, fields), exc_info=True, stacklevel=2)


def get_logger(name: str) -> Logger:
    return Logger(name)


def configure(level: str) -> None:
    """Send log records to stderr. Raises ValueError on an unknown level."""
    logging.basicConfig(level=level, format=_FORMAT, datefmt=_DATEFMT)
