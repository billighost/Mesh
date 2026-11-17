"""Context-local diagnostic output that leaves concurrent MCP protocol writes intact."""

from __future__ import annotations

import contextlib
import contextvars
import sys
import threading
from typing import Any, Iterator

_TARGET: contextvars.ContextVar[Any] = contextvars.ContextVar("mesh_stdout_target", default=None)
_INSTALL_LOCK = threading.Lock()


class ContextStream:
    def __init__(self, original: Any):
        self.original = original

    def _target(self) -> Any:
        target = _TARGET.get()
        return self.original if target is None else target

    def write(self, value: str) -> Any:
        return self._target().write(value)

    def flush(self) -> Any:
        return self._target().flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)


def install_stdout_router() -> None:
    with _INSTALL_LOCK:
        if not isinstance(sys.stdout, ContextStream):
            sys.stdout = ContextStream(sys.stdout)


@contextlib.contextmanager
def diagnostics_to_stderr() -> Iterator[None]:
    install_stdout_router()
    token = _TARGET.set(sys.stderr)
    try:
        yield
    finally:
        _TARGET.reset(token)
