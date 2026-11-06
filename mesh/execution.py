"""Bounded blocking work for async transports, with caller context preserved."""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import functools
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable


class CapacityError(RuntimeError):
    """Work was rejected before execution because the local queue is full."""


class BoundedExecutor:
    def __init__(self, workers: int = 4, capacity: int = 32):
        if type(workers) is not int or type(capacity) is not int or not 1 <= workers <= 32 or not workers <= capacity <= 1024:
            raise ValueError("Use integer workers (1–32) and capacity (workers–1024).")
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mesh-request")
        self._admission = threading.BoundedSemaphore(capacity)
        self._lock = threading.Lock()
        self._closed = False
        self._pending: set[Future[Any]] = set()

    def submit(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        if not self._admission.acquire(blocking=False):
            raise CapacityError("Mesh is busy; this request was not admitted. Retry with the same operation ID.")
        context = contextvars.copy_context()
        future: Future[Any] = Future()

        def run() -> None:
            try:
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    result = context.run(functools.partial(function, *args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                with self._lock:
                    self._pending.discard(future)
                self._admission.release()

        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Mesh request executor is closed.")
                self._pending.add(future)
                # Keep the pool's internal future private. Cancelling the public
                # one cannot free a still-queued work item and grow that queue.
                self._pool.submit(run)
        except BaseException:
            with self._lock:
                self._pending.discard(future)
            self._admission.release()
            raise
        return future

    async def run(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.wrap_future(self.submit(function, *args, **kwargs))

    def close(self, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
            pending = tuple(self._pending)
        for future in pending:
            future.cancel()
        # Queued runners must settle their admission leases even when cancelled.
        self._pool.shutdown(wait=wait, cancel_futures=False)


_EXECUTOR: BoundedExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


async def run_sync(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.wrap_future(submit_sync(function, *args, **kwargs))


def submit_sync(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = BoundedExecutor()
        executor = _EXECUTOR
    return executor.submit(function, *args, **kwargs)


def close_executor(*, wait: bool = True) -> None:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        executor, _EXECUTOR = _EXECUTOR, None
    if executor is not None:
        executor.close(wait=wait)


atexit.register(close_executor)
