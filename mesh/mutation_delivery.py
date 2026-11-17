"""Bounded discovery and retry of durable mutation events, without memory bodies."""
from __future__ import annotations

import contextvars
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .envutil import env_bool

_MAX_ROOTS = 16
_MAX_PENDING_STORES = 256
_LOCK = threading.Lock()
_DRAIN_LOCK = threading.Lock()
_MAINTENANCE: contextvars.ContextVar[bool] = contextvars.ContextVar("mesh_mutation_delivery", default=False)


@dataclass
class _RootWatch:
    iterator: Iterator[Path] | None = None
    next_scan: float = 0.0


@dataclass
class _DeliveryHint:
    store_ref: Any = None
    failures: int = 0
    next_attempt: float = 0.0
    generation: int = 0


_ROOTS: OrderedDict[Path, _RootWatch] = OrderedDict()
_PENDING: OrderedDict[tuple[Path, str], _DeliveryHint] = OrderedDict()
_DELIVERED = 0
_RETRIES = 0
_LAST_ERROR: str | None = None
_DEFERRED_BUSY = 0
_ROOT_EVICTIONS = 0
_HINT_EVICTIONS = 0


def _admit(key: tuple[Path, str], hint: _DeliveryHint) -> None:
    global _HINT_EVICTIONS
    _PENDING[key] = hint
    _PENDING.move_to_end(key)
    while len(_PENDING) > _MAX_PENDING_STORES:
        # Hints can be rediscovered; the authoritative events remain in SQLite.
        _PENDING.popitem(last=False)
        _HINT_EVICTIONS += 1


def register_mutation_store(store: Any) -> None:
    """Watch the bound root for restart recovery and wake existing audit delivery."""
    global _ROOT_EVICTIONS
    if env_bool("MESH_NO_OUTBOX_WORKER", False) or _MAINTENANCE.get():
        return
    root = store.db_path.parent.resolve()
    with _LOCK:
        if root not in _ROOTS:
            _ROOTS[root] = _RootWatch()
        _ROOTS.move_to_end(root)
        while len(_ROOTS) > _MAX_ROOTS:
            # A drain can hold the iterator outside this lock. Let its last
            # reference close it rather than closing an executing generator.
            _ROOTS.popitem(last=False)
            _ROOT_EVICTIONS += 1
        key = (root, store.namespace)
        hint = _PENDING.get(key)
        if hint is None:
            _admit(key, _DeliveryHint(weakref.ref(store)))
        else:
            hint.store_ref = weakref.ref(store)
            hint.generation += 1
            _PENDING.move_to_end(key)
    from .audit import _AUDIT_WAKE_EVENT, _ensure_audit_drain_thread

    _ensure_audit_drain_thread()
    _AUDIT_WAKE_EVENT.set()


def _discover_stores(now: float) -> None:
    from .audit import reject_symlink, validate_namespace

    with _LOCK:
        watches = tuple(_ROOTS.items())
    for root, watch in watches:
            if watch.iterator is None:
                if now < watch.next_scan:
                    continue
                watch.iterator = iter(root.glob("mesh_*.db"))
            for _ in range(4):
                try:
                    path = next(watch.iterator)
                except (StopIteration, OSError):
                    watch.iterator = None
                    watch.next_scan = now + 30.0
                    break
                try:
                    reject_symlink(path)
                    namespace = validate_namespace(path.stem[5:])
                except (OSError, ValueError):
                    continue
                key = (root, namespace)
                with _LOCK:
                    if _ROOTS.get(root) is watch and key not in _PENDING:
                        _admit(key, _DeliveryHint())


def _deliver_ready_store(root: Path, namespace: str, hint: _DeliveryHint, batch_size: int) -> int:
    """Skip busy writers, retaining the lease through durable acknowledgement."""
    from .audit import _AUDIT_LOCK, _connect_audit, deliver_mutation_events, mesh_dir_context
    from .store import MeshStore, _get_store_coordinator, namespace_write_lock

    coordinator = _get_store_coordinator(root / f"mesh_{namespace}.db", namespace)
    with namespace_write_lock(namespace, timeout=0.0, data_root=root):
        if not (root / f"mesh_{namespace}.db").exists():
            return 0
        if not coordinator.lock.acquire(blocking=False):
            raise TimeoutError("Mutation delivery deferred for a busy namespace.")
        try:
            with mesh_dir_context(root):
                store = hint.store_ref() if hint.store_ref is not None else None
                if store is None:
                    store = MeshStore(namespace=namespace, create=False)
                # Background rounds defer a saturated writer immediately. A
                # connection lease must be acquired before the global audit
                # lock, so foreground audit admission can keep progressing.
                with store._connect(wait_timeout=0.0) as source:
                    if not _AUDIT_LOCK.acquire(blocking=False):
                        raise TimeoutError("Mutation delivery deferred for a busy audit sink.")
                    try:
                        source_timeout = int(source.execute("PRAGMA busy_timeout").fetchone()[0])
                        source.execute("PRAGMA busy_timeout=50")
                        sink = None
                        sink_timeout = None
                        try:
                            if store.list_mutation_events(limit=1):
                                sink = _connect_audit(root / f"audit_{namespace}.db")
                                sink_timeout = int(sink.execute("PRAGMA busy_timeout").fetchone()[0])
                                sink.execute("PRAGMA busy_timeout=50")
                            return deliver_mutation_events(store, limit=batch_size)
                        finally:
                            if sink is not None and sink_timeout is not None:
                                sink.execute(f"PRAGMA busy_timeout={sink_timeout}")
                            source.execute(f"PRAGMA busy_timeout={source_timeout}")
                    finally:
                        _AUDIT_LOCK.release()
        finally:
            coordinator.lock.release()



def drain_mutation_delivery(*, max_stores: int = 16, batch_size: int = 256) -> int:
    """Try a bounded round; failures preserve SQLite events for the next attempt."""
    global _DELIVERED, _RETRIES, _LAST_ERROR, _DEFERRED_BUSY
    if type(max_stores) is not int or not 1 <= max_stores <= _MAX_PENDING_STORES:
        raise ValueError("max_stores must be between 1 and 256.")
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000.")
    if env_bool("MESH_NO_OUTBOX_WORKER", False) or not _DRAIN_LOCK.acquire(blocking=False):
        return 0
    total = 0
    try:
        now = time.monotonic()
        _discover_stores(now)
        with _LOCK:
            work = []
            for _ in range(min(len(_PENDING), max_stores)):
                key, hint = _PENDING.popitem(last=False)
                _PENDING[key] = hint
                if now >= hint.next_attempt:
                    work.append((key, hint, hint.generation))

        for (root, namespace), hint, generation in work:
            key = (root, namespace)
            maintenance_token = _MAINTENANCE.set(True)
            try:
                if not (root / f"mesh_{namespace}.db").exists():
                    with _LOCK:
                        if _PENDING.get(key) is hint and hint.generation == generation:
                            _PENDING.pop(key, None)
                    continue
                count = _deliver_ready_store(root, namespace, hint, batch_size)
            except TimeoutError:
                with _LOCK:
                    _DEFERRED_BUSY += 1
                    if _PENDING.get(key) is hint and hint.failures == 0:
                        hint.next_attempt = time.monotonic() + 0.025
            except Exception as exc:
                with _LOCK:
                    if _PENDING.get(key) is hint:
                        hint.failures = min(hint.failures + 1, 8)
                        hint.next_attempt = time.monotonic() + min(30.0, 0.5 * (2 ** hint.failures))
                    _RETRIES += 1
                    _LAST_ERROR = type(exc).__name__
            else:
                total += count
                with _LOCK:
                    _DELIVERED += count
                    _LAST_ERROR = None
                    if _PENDING.get(key) is hint:
                        if count < batch_size and hint.generation == generation:
                            _PENDING.pop(key, None)
                        else:
                            hint.failures = 0
                            hint.next_attempt = 0.0
            finally:
                _MAINTENANCE.reset(maintenance_token)
    finally:
        _DRAIN_LOCK.release()
    return total


def get_mutation_delivery_stats() -> dict[str, Any]:
    with _LOCK:
        return {
            "watched_roots": len(_ROOTS), "pending_store_hints": len(_PENDING),
            "delivered_events": _DELIVERED, "retries": _RETRIES, "last_error_code": _LAST_ERROR,
            "deferred_busy": _DEFERRED_BUSY, "root_evictions": _ROOT_EVICTIONS,
            "hint_evictions": _HINT_EVICTIONS,
            "max_roots": _MAX_ROOTS, "max_store_hints": _MAX_PENDING_STORES,
        }
