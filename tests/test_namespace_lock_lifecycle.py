"""Namespace coordinators remain pinned while holders or waiters use them."""
from __future__ import annotations

import threading

import pytest

from mesh import store as storage
from mesh.store import MeshStore
from tests.test_production_storage import isolated_storage as isolated_storage


def test_namespace_lock_eviction_keeps_a_waiting_reference_pinned():
    store = MeshStore("lock-pins")
    root = store.db_path.parent
    key = (root, store.namespace)
    with storage.namespace_write_lock(store.namespace, data_root=root):
        pass
    original = storage._NS_LOCK_REGISTRY[key]
    real_lock = original.rlock
    waiting = threading.Event()
    proceed = threading.Event()
    errors = []

    class GateLock:
        def acquire(self, *args, **kwargs):
            if threading.current_thread().name == "paused-namespace-writer":
                waiting.set()
                if not proceed.wait(5):
                    raise TimeoutError("The waiting writer was not released")
            return real_lock.acquire(*args, **kwargs)

        def release(self):
            real_lock.release()

    original.rlock = GateLock()

    def writer():
        try:
            with storage.namespace_write_lock(store.namespace, data_root=root):
                pass
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=writer, name="paused-namespace-writer")
    worker.start()
    try:
        assert waiting.wait(5)
        storage.evict_namespace_write_lock(store.namespace, data_root=root)
        assert storage._NS_LOCK_REGISTRY.get(key) is original
    finally:
        proceed.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert not errors


def test_namespace_lock_pins_nested_users_and_allows_idle_retirement():
    store = MeshStore("nested-pins")
    root = store.db_path.parent
    key = (root, store.namespace)
    with storage.namespace_write_lock(store.namespace, data_root=root):
        state = storage._NS_LOCK_REGISTRY[key]
        assert state.users == state.depth == 1
        with storage.namespace_write_lock(store.namespace, data_root=root):
            assert state.users == state.depth == 2
            storage.evict_namespace_write_lock(store.namespace, data_root=root)
            assert storage._NS_LOCK_REGISTRY[key] is state
        assert state.users == state.depth == 1
    assert state.users == state.depth == 0
    storage.evict_namespace_write_lock(store.namespace, data_root=root)
    assert key not in storage._NS_LOCK_REGISTRY


def test_namespace_lock_timeout_releases_its_pin():
    store = MeshStore("timeout-pin")
    root = store.db_path.parent
    key = (root, store.namespace)
    ready = threading.Event()
    release = threading.Event()

    def owner():
        with storage.namespace_write_lock(store.namespace, data_root=root):
            ready.set()
            assert release.wait(5)

    worker = threading.Thread(target=owner)
    worker.start()
    try:
        assert ready.wait(5)
        with pytest.raises(TimeoutError):
            with storage.namespace_write_lock(store.namespace, timeout=0.02, data_root=root):
                pytest.fail("The owner must exclude another writer")
        assert storage._NS_LOCK_REGISTRY[key].users == 1
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert storage._NS_LOCK_REGISTRY[key].users == 0


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf")])
def test_invalid_timeout_does_not_create_a_registry_entry(isolated_storage, timeout):
    root = (isolated_storage / "mesh").resolve()
    namespace = "invalid-timeout"
    key = (root, namespace)
    with pytest.raises(ValueError):
        with storage.namespace_write_lock(namespace, timeout=timeout, data_root=root):
            pytest.fail("Invalid timeouts must be rejected")
    assert key not in storage._NS_LOCK_REGISTRY
