"""Writer admission waits for real leases while keeping the hard connection cap."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from mesh import audit, store as storage
from mesh.crypto import is_encrypted_text
from mesh.sqlite_runtime import sqlite3
from mesh.store import MeshStore
from tests.test_production_storage import fact, isolated_storage as isolated_storage


def close_writer(store):
    storage.close_store_connection(store.db_path)
    with storage._CACHE_LOCK:
        dict.pop(storage._INITIALIZED_DBS, store.db_path, None)


def hold_writer(store, ready, release):
    with store._connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        ready.set()
        assert release.wait(10)


@pytest.mark.parametrize("initialize", [False, True])
def test_writer_waits_for_last_checkin_without_blocking_a_reader(monkeypatch, initialize):
    first, second, pending = [MeshStore(name) for name in ("writer-a", "writer-b", "writer-c")]
    close_writer(pending)
    monkeypatch.setenv("MESH_MAX_POOL_SIZE", "2")
    ready = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    started, finished = threading.Event(), threading.Event()

    def request():
        started.set()
        try:
            if initialize:
                return MeshStore(pending.namespace + "-cold").count()
            with pending._connect() as connection:
                return connection.execute("SELECT 1").fetchone()[0]
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=4) as executor:
        holders = [executor.submit(hold_writer, store, event, stop) for store, event, stop in zip((first, second), ready, release)]
        try:
            assert all(event.wait(3) for event in ready)
            waiting = executor.submit(request)
            assert started.wait(2)
            assert not finished.wait(0.15), "Writer failed immediately instead of waiting for an active lease."
            assert executor.submit(second.count).result(timeout=2) == 0
            release[0].set()
            assert waiting.result(timeout=3) == (0 if initialize else 1)
            assert not release[1].is_set()
            with storage._CACHE_LOCK:
                assert len(storage._CONNECTION_POOL) <= 2
                assert storage._WRITER_OPENING == set()
        finally:
            for stop in release:
                stop.set()
        for holder in holders:
            holder.result(timeout=3)


@pytest.mark.parametrize("failing_open", [False, True])
def test_writer_waits_for_slow_open_reservations_and_recovers_after_failure(monkeypatch, failing_open):
    ready_store = MeshStore("writer-ready")
    monkeypatch.setenv("MESH_MAX_POOL_SIZE", "2")
    original = sqlite3.connect
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    started, finished = threading.Event(), threading.Event()

    def connect(path, *args, **kwargs):
        for index, name in enumerate(("writer-open-a", "writer-open-b")):
            if str(path).endswith(f"mesh_{name}.db"):
                entered[index].set()
                assert release[index].wait(10)
                if index == 0 and failing_open:
                    raise sqlite3.OperationalError("Isolated connection open failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)

    def request():
        started.set()
        try:
            return MeshStore("writer-open-c").count()
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=4) as executor:
        openers = [executor.submit(MeshStore, name) for name in ("writer-open-a", "writer-open-b")]
        try:
            assert all(event.wait(3) for event in entered)
            waiting = executor.submit(request)
            assert started.wait(2)
            assert not finished.wait(0.15), "Reserved opens incorrectly caused immediate admission failure."
            assert executor.submit(ready_store.count).result(timeout=2) == 0
            release[0].set()
            assert waiting.result(timeout=3) == 0
            assert not release[1].is_set()
        finally:
            for stop in release:
                stop.set()
        if failing_open:
            with pytest.raises(sqlite3.OperationalError, match="Isolated connection open failure"):
                openers[0].result(timeout=3)
        else:
            openers[0].result(timeout=3)
        openers[1].result(timeout=3)
    with storage._CACHE_LOCK:
        assert storage._WRITER_OPENING == set()
        assert len(storage._CONNECTION_POOL) <= 2


def test_writer_wait_has_a_finite_budget_and_leaves_no_open_reservation(monkeypatch):
    first, second, pending = [MeshStore(name) for name in ("writer-timeout-a", "writer-timeout-b", "writer-timeout-c")]
    close_writer(pending)
    monkeypatch.setenv("MESH_MAX_POOL_SIZE", "2")
    ready = [threading.Event(), threading.Event()]
    release = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        holders = [executor.submit(hold_writer, store, event, release) for store, event in zip((first, second), ready)]
        try:
            assert all(event.wait(3) for event in ready)
            start = time.monotonic()
            with pytest.raises(RuntimeError, match="Writer connection capacity exceeded"):
                with pending._connect():
                    pytest.fail("A third writer was admitted beyond the connection cap.")
            elapsed = time.monotonic() - start
            assert 4.8 <= elapsed <= 6.5
            with storage._CACHE_LOCK:
                assert storage._WRITER_OPENING == set()
                assert len(storage._CONNECTION_POOL) == 2
        finally:
            release.set()
        for holder in holders:
            holder.result(timeout=3)
    with pending._connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_writer_eviction_closes_idle_handle_before_opening_replacement(monkeypatch):
    first, second, pending = [MeshStore(name) for name in ("writer-cap-a", "writer-cap-b", "writer-cap-c")]
    close_writer(pending)
    monkeypatch.setenv("MESH_MAX_POOL_SIZE", "2")
    live = {id(connection) for connection in storage._CONNECTION_POOL.values()}
    peaks = [len(live)]
    tracker = threading.Lock()
    original_connect = sqlite3.connect
    original_close = sqlite3.Connection.close

    def connect(path, *args, **kwargs):
        connection = original_connect(path, *args, **kwargs)
        if kwargs.get("factory") is storage._PooledConnection and "mode=ro" not in str(path):
            with tracker:
                live.add(id(connection))
                peaks.append(len(live))
        return connection

    def close(connection):
        original_close(connection)
        with tracker:
            live.discard(id(connection))

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(sqlite3.Connection, "close", close)
    with second._connect():
        with pending._connect() as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
    assert max(peaks) <= 2
    assert len(live) == 2
    assert first.db_path not in storage._CONNECTION_POOL


def test_nested_checkout_keeps_slot_busy_until_final_checkin(monkeypatch):
    first, second, pending = [MeshStore(name) for name in ("writer-nested-a", "writer-nested-b", "writer-nested-c")]
    close_writer(pending)
    monkeypatch.setenv("MESH_MAX_POOL_SIZE", "2")
    first_ready, second_ready = threading.Event(), threading.Event()
    outer_release, inner_release, second_release = threading.Event(), threading.Event(), threading.Event()
    inner_done, started, finished = threading.Event(), threading.Event(), threading.Event()

    def nested():
        with first._connect():
            with first._connect():
                first_ready.set()
                assert inner_release.wait(10)
            inner_done.set()
            assert outer_release.wait(10)

    def request():
        started.set()
        try:
            with pending._connect() as connection:
                return connection.execute("SELECT 1").fetchone()[0]
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=3) as executor:
        first_holder = executor.submit(nested)
        second_holder = executor.submit(hold_writer, second, second_ready, second_release)
        try:
            assert first_ready.wait(3) and second_ready.wait(3)
            waiting = executor.submit(request)
            assert started.wait(2)
            assert not finished.wait(0.15)
            inner_release.set()
            assert inner_done.wait(2)
            assert not finished.wait(0.15), "A nested check-in released an outer writer's capacity."
            outer_release.set()
            assert waiting.result(timeout=3) == 1
        finally:
            for release in (inner_release, outer_release, second_release):
                release.set()
        first_holder.result(timeout=3)
        second_holder.result(timeout=3)


def test_lru_retirement_does_not_wait_for_another_namespace_audit(monkeypatch):
    first, second = [MeshStore(name) for name in ("writer-audit-a", "writer-audit-b")]
    audit.log_recall("A cedar task is complete", first.namespace, "queued-reader", [])
    assert audit.get_audit_queue_stats()["pending_events"] == 1
    monkeypatch.setenv("MESH_MAX_POOL_SIZE", "2")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with storage.namespace_write_lock(first.namespace, data_root=first.db_path.parent):
            pending = executor.submit(MeshStore, "writer-audit-c")
            assert pending.result(timeout=2).count() == 0
            assert audit.get_audit_queue_stats()["pending_events"] == 1
        pending.result(timeout=3)
    assert first.db_path not in storage._CONNECTION_POOL
    assert second.count() == 0


def test_existing_current_schema_open_uses_readers_after_writer_eviction(monkeypatch):
    existing, first, second = [MeshStore(name) for name in ("writer-known", "writer-known-a", "writer-known-b")]
    generation = existing._store_generation
    close_writer(existing)
    monkeypatch.setenv("MESH_MAX_POOL_SIZE", "2")
    ready = [threading.Event(), threading.Event()]
    release = threading.Event()
    with ThreadPoolExecutor(max_workers=3) as executor:
        holders = [executor.submit(hold_writer, store, event, release) for store, event in zip((first, second), ready)]
        try:
            assert all(event.wait(3) for event in ready)
            reopened = executor.submit(MeshStore, existing.namespace).result(timeout=2)
            assert reopened._store_generation == generation
            assert reopened.count() == 0
            assert existing.db_path not in storage._CONNECTION_POOL
        finally:
            release.set()
        for holder in holders:
            holder.result(timeout=3)


def test_cold_current_schema_open_preserves_legacy_encryption_cleanup(monkeypatch):
    existing = MeshStore("writer-encryption-cold")
    marker = "ISOLATED_PLAINTEXT_TASK_FOR_ENCRYPTION_REOPEN_TEST"
    task = existing.create_work_item(marker)
    generation = existing._store_generation
    close_writer(existing)
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", "42" * 32)  # Obviously fake deterministic test key.
    reopened = MeshStore(existing.namespace)
    with reopened._read_connect() as connection:
        raw = connection.execute("SELECT title FROM work_items WHERE id=?", (task["id"],)).fetchone()[0]
        assert is_encrypted_text(raw)
    assert reopened.get_work_item(task["id"])["title"] == marker
    assert reopened._store_generation == generation


def test_saturated_background_delivery_defers_without_blocking_audit_admission(monkeypatch):
    from mesh import mutation_delivery

    pending, first, second = [MeshStore(name) for name in ("writer-delivery", "writer-delivery-a", "writer-delivery-b")]
    fact(pending)
    close_writer(pending)
    for source in (first, second):
        with source._connect():
            pass
    monkeypatch.setenv("MESH_MAX_POOL_SIZE", "2")
    requested = threading.Event()
    original = MeshStore._connect

    def observe(source, *args, **kwargs):
        if source is pending:
            requested.set()
        return original(source, *args, **kwargs)

    monkeypatch.setattr(MeshStore, "_connect", observe)
    ready = [threading.Event(), threading.Event()]
    release = threading.Event()
    hint = SimpleNamespace(store_ref=lambda: pending)

    def deliver():
        try:
            return mutation_delivery._deliver_ready_store(pending.db_path.parent, pending.namespace, hint, 256)
        except TimeoutError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=4) as executor:
        holders = [executor.submit(hold_writer, store, event, release) for store, event in zip((first, second), ready)]
        try:
            assert all(event.wait(3) for event in ready)
            delivery = executor.submit(deliver)
            assert requested.wait(3)
            foreground = executor.submit(audit.log_recall, "A bounded foreground query", "writer-delivery-foreground", "foreground", [])
            assert foreground.result(timeout=1) is None
            assert isinstance(delivery.result(timeout=1), TimeoutError)
            assert len(pending.list_mutation_events()) == 1
        finally:
            release.set()
        for holder in holders:
            holder.result(timeout=3)
    assert audit.deliver_mutation_events(pending) == 1


def test_lru_retirement_releases_idle_namespace_lock_descriptors(monkeypatch):
    first = MeshStore("writer-idle-lock-a")
    MeshStore("writer-idle-lock-b")
    path = storage._get_ns_lock_path(first.namespace, data_root=first.db_path.parent)
    assert path in storage._OPEN_LOCK_FDS
    monkeypatch.setenv("MESH_MAX_POOL_SIZE", "2")
    MeshStore("writer-idle-lock-c")
    assert first.db_path not in storage._CONNECTION_POOL
    assert path not in storage._OPEN_LOCK_FDS
