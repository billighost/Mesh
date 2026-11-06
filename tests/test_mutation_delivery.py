"""Durable mutation delivery under failures, restart, and concurrent writers."""
from __future__ import annotations

import gc
import json
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from mesh import audit
from mesh import mutation_delivery as delivery
from mesh import store as storage
from mesh.sqlite_runtime import sqlite3
from mesh.store import MeshStore

_START_AUDIT_WORKER = audit._ensure_audit_drain_thread


@pytest.fixture(autouse=True)
def isolated_delivery(tmp_path, monkeypatch):
    for name, child in {
        "MESH_HOME": "mesh", "HOME": "home", "USERPROFILE": "home",
        "APPDATA": "appdata", "LOCALAPPDATA": "localappdata", "XDG_CONFIG_HOME": "xdg",
    }.items():
        target = tmp_path / child
        target.mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(target))
    for name in (
        "MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK",
        "MESH_NO_OUTBOX_WORKER",
    ):
        monkeypatch.setenv(name, "1")
    for name in (
        "MESH_LOCKED_NAMESPACE", "MESH_NAMESPACE", "MESH_ENCRYPT_AT_REST", "MESH_ENCRYPTION_KEY",
        "MESH_PASSPHRASE", "MESH_READ_ONLY", "MESH_MAX_MUTATION_OUTBOX",
    ):
        monkeypatch.delenv(name, raising=False)
    audit.stop_audit_worker()
    monkeypatch.setattr(audit, "_ensure_audit_drain_thread", lambda: None)
    with delivery._LOCK:
        delivery._ROOTS.clear()
        delivery._PENDING.clear()
        delivery._DELIVERED = delivery._RETRIES = 0
        delivery._LAST_ERROR = None
    with audit._AUDIT_QUEUE_LOCK:
        audit._AUDIT_PENDING.clear()
        audit._AUDIT_PENDING_COUNT = audit._AUDIT_PENDING_BYTES = 0
    audit._AUDIT_WAKE_EVENT.clear()
    yield tmp_path
    monkeypatch.setenv("MESH_NO_OUTBOX_WORKER", "1")
    audit.stop_audit_worker()
    storage._close_all_pooled_connections()
    audit._close_all_audit_connections()
    with delivery._LOCK:
        for watch in delivery._ROOTS.values():
            close = getattr(watch.iterator, "close", None)
            if close is not None:
                close()
        delivery._ROOTS.clear()
        delivery._PENDING.clear()
    storage._VECTOR_CACHE.clear()
    gc.collect()


def fact(store: MeshStore, memory_id: str = "fact-1", *, private=False) -> None:
    store.upsert(
        memory_id,
        f"Untrusted body for durable mutation {memory_id}.",
        None,
        {"source_agent": "test-agent", "embedding_model": "needs-reembed"},
        local_only=private,
    )


def audit_rows(store: MeshStore) -> list[dict]:
    path = store.db_path.parent / f"audit_{store.namespace}.db"
    if not path.exists():
        return []
    conn = audit._connect_audit(path)
    with conn:
        cursor = conn.execute("SELECT * FROM audit_events ORDER BY id")
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def enable_delivery(monkeypatch) -> None:
    monkeypatch.setenv("MESH_NO_OUTBOX_WORKER", "0")


def test_disable_switch_preserves_durable_outbox_without_starting_worker():
    store = MeshStore("disabled")
    fact(store)
    assert delivery.drain_mutation_delivery() == 0
    assert len(store.list_mutation_events()) == 1
    assert delivery.get_mutation_delivery_stats()["watched_roots"] == 0
    assert audit._AUDIT_DRAIN_THREAD is None


def test_commit_schedules_delivery_after_outer_transaction_and_discards_savepoint(monkeypatch):
    store = MeshStore("commits")
    enable_delivery(monkeypatch)
    with store.write_transaction():
        fact(store, "committed")
        with pytest.raises(RuntimeError, match="savepoint"):
            with store.write_transaction():
                fact(store, "rolled-back")
                raise RuntimeError("savepoint")
        assert delivery.get_mutation_delivery_stats()["pending_store_hints"] == 0
    assert delivery.get_mutation_delivery_stats()["pending_store_hints"] == 1
    assert delivery.drain_mutation_delivery() == 1
    assert store.get("rolled-back") is None
    assert [row["object_id"] for row in audit_rows(store)] == ["committed"]


def test_rollback_never_schedules_or_delivers_an_uncommitted_event(monkeypatch):
    store = MeshStore("rollbacks")
    enable_delivery(monkeypatch)
    with pytest.raises(RuntimeError, match="outer"):
        with store.write_transaction():
            fact(store)
            raise RuntimeError("outer")
    assert delivery.get_mutation_delivery_stats()["pending_store_hints"] == 0
    assert delivery.drain_mutation_delivery() == 0
    assert store.count() == 0 and store.list_mutation_events() == []
    assert audit_rows(store) == []


def test_public_and_private_delivery_remains_content_free(monkeypatch):
    enable_delivery(monkeypatch)
    store = MeshStore("content-free")
    fact(store, "public")
    fact(store, "private", private=True)
    assert delivery.drain_mutation_delivery() == 2
    rows = audit_rows(store)
    assert {row["object_id"] for row in rows} == {"public", "private"}
    assert all(row["namespace"] == "content-free" for row in rows)
    assert "Untrusted body" not in json.dumps(rows)
    assert store.list_mutation_events() == []


def test_delivery_rounds_obey_batch_size_and_keep_full_batches_pending(monkeypatch):
    enable_delivery(monkeypatch)
    store = MeshStore("batches")
    for index in range(5):
        fact(store, f"fact-{index}")
    assert delivery.drain_mutation_delivery(max_stores=1, batch_size=2) == 2
    assert len(store.list_mutation_events()) == 3
    assert delivery.drain_mutation_delivery(max_stores=1, batch_size=2) == 2
    assert delivery.drain_mutation_delivery(max_stores=1, batch_size=2) == 1
    assert delivery.get_mutation_delivery_stats()["pending_store_hints"] == 0
    assert len(audit_rows(store)) == 5


def test_restart_discovers_durable_events_after_store_reference_expires(monkeypatch):
    saved = MeshStore("saved")
    fact(saved)
    root = saved.db_path.parent.resolve()
    store_ref = weakref.ref(saved)
    del saved
    gc.collect()
    assert store_ref() is None
    enable_delivery(monkeypatch)
    with delivery._LOCK:
        delivery._ROOTS[root] = delivery._RootWatch()
    assert delivery.drain_mutation_delivery() == 1
    with audit.mesh_dir_context(root):
        restored = MeshStore("saved", create=False)
    assert restored.get("fact-1") is not None
    assert restored.list_mutation_events() == []
    assert [row["object_id"] for row in audit_rows(restored)] == ["fact-1"]


def test_discovery_refuses_invalid_database_namespace_without_creating_audit(tmp_path, monkeypatch):
    root = tmp_path / "discovery"
    root.mkdir()
    (root / "mesh_invalid namespace.db").write_bytes(b"untrusted database")
    enable_delivery(monkeypatch)
    with delivery._LOCK:
        delivery._ROOTS[root] = delivery._RootWatch()
    assert delivery.drain_mutation_delivery() == 0
    assert delivery.get_mutation_delivery_stats()["pending_store_hints"] == 0
    assert not list(root.glob("audit_*.db"))


def test_failed_commit_retains_events_and_reports_only_error_class(monkeypatch):
    enable_delivery(monkeypatch)
    store = MeshStore("retry")
    fact(store)

    def fail(*args):
        raise sqlite3.OperationalError("Untrusted body must never appear in diagnostics")

    monkeypatch.setattr(audit, "_persist_audit_entries", fail)
    assert delivery.drain_mutation_delivery() == 0
    stats = delivery.get_mutation_delivery_stats()
    assert stats["retries"] == 1 and stats["last_error_code"] == "OperationalError"
    assert "Untrusted body" not in json.dumps(stats)
    assert len(store.list_mutation_events()) == 1
    assert delivery.drain_mutation_delivery() == 0
    assert delivery.get_mutation_delivery_stats()["retries"] == 1


def test_repeated_delivery_failure_keeps_exponential_retry_bounded(monkeypatch):
    enable_delivery(monkeypatch)
    store = MeshStore("bounded-retries")
    fact(store)
    clock = [1000000.0]
    monkeypatch.setattr(delivery, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def fail(*args):
        raise sqlite3.OperationalError("isolated unavailable audit sink")

    monkeypatch.setattr(audit, "_persist_audit_entries", fail)
    key = (store.db_path.parent.resolve(), store.namespace)
    for attempt in range(1, 16):
        assert delivery.drain_mutation_delivery() == 0
        with delivery._LOCK:
            hint = delivery._PENDING[key]
            assert hint.failures == min(attempt, 8)
            assert hint.next_attempt == clock[0] + min(30.0, 0.5 * (2 ** hint.failures))
            clock[0] = hint.next_attempt
    assert delivery.get_mutation_delivery_stats()["retries"] == 15
    assert len(store.list_mutation_events()) == 1


def test_watcher_and_hint_limits_never_remove_authoritative_sql_events(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "_MAX_ROOTS", 2)
    monkeypatch.setattr(delivery, "_MAX_PENDING_STORES", 3)
    stores = []
    for index in range(6):
        with audit.mesh_dir_context(tmp_path / f"root-{index // 2}"):
            store = MeshStore(f"bounded-{index}")
            fact(store)
            stores.append(store)
    enable_delivery(monkeypatch)
    for store in stores:
        delivery.register_mutation_store(store)
    stats = delivery.get_mutation_delivery_stats()
    assert stats["watched_roots"] <= 2 and stats["pending_store_hints"] <= 3
    assert sum(len(store.list_mutation_events()) for store in stores) == 6


def test_new_writes_preserve_failure_backoff_and_do_not_retry_an_unavailable_sink(monkeypatch):
    enable_delivery(monkeypatch)
    store = MeshStore("busy-outage")
    fact(store, "first")

    def fail(*args):
        raise sqlite3.OperationalError("isolated unavailable audit sink")

    monkeypatch.setattr(audit, "_persist_audit_entries", fail)
    assert delivery.drain_mutation_delivery() == 0
    key = (store.db_path.parent.resolve(), store.namespace)
    with delivery._LOCK:
        failures = delivery._PENDING[key].failures
        retry_at = delivery._PENDING[key].next_attempt
    assert failures == 1 and retry_at > time.monotonic()
    fact(store, "second")
    with delivery._LOCK:
        assert delivery._PENDING[key].failures == failures
        assert delivery._PENDING[key].next_attempt == retry_at
    assert delivery.drain_mutation_delivery() == 0
    assert delivery.get_mutation_delivery_stats()["retries"] == 1
    assert len(store.list_mutation_events()) == 2


def test_acknowledgement_failure_retries_without_duplicate_audit_rows(monkeypatch):
    enable_delivery(monkeypatch)
    store = MeshStore("ack-retry")
    fact(store)
    original = store.acknowledge_mutation_events

    def fail(event_ids):
        raise sqlite3.OperationalError("isolated acknowledgement failure")

    monkeypatch.setattr(store, "acknowledge_mutation_events", fail)
    assert delivery.drain_mutation_delivery() == 0
    assert len(audit_rows(store)) == 1 and len(store.list_mutation_events()) == 1
    monkeypatch.setattr(store, "acknowledge_mutation_events", original)
    with delivery._LOCK:
        for hint in delivery._PENDING.values():
            hint.next_attempt = 0.0
    assert delivery.drain_mutation_delivery() == 1
    assert len(audit_rows(store)) == 1 and store.list_mutation_events() == []


def test_same_namespace_delivery_keeps_roots_separate(tmp_path, monkeypatch):
    with audit.mesh_dir_context(tmp_path / "first"):
        first = MeshStore("same")
        fact(first, "first")
    with audit.mesh_dir_context(tmp_path / "second"):
        second = MeshStore("same")
        fact(second, "second", private=True)
    enable_delivery(monkeypatch)
    delivery.register_mutation_store(first)
    delivery.register_mutation_store(second)
    with audit.mesh_dir_context(tmp_path / "unrelated"):
        assert delivery.drain_mutation_delivery() == 2
    assert [row["object_id"] for row in audit_rows(first)] == ["first"]
    assert [row["object_id"] for row in audit_rows(second)] == ["second"]
    assert not (tmp_path / "unrelated" / "audit_same.db").exists()


def test_slow_discovery_does_not_hold_registration_lock_for_other_roots(tmp_path, monkeypatch):
    ready = MeshStore("ready")
    entered, release, registered = threading.Event(), threading.Event(), threading.Event()

    class SlowFilesystemIterator:
        def __iter__(self):
            return self

        def __next__(self):
            entered.set()
            assert release.wait(5)
            raise StopIteration

    enable_delivery(monkeypatch)
    with delivery._LOCK:
        delivery._ROOTS[tmp_path / "slow-root"] = delivery._RootWatch(iterator=SlowFilesystemIterator())

    def register_ready():
        delivery.register_mutation_store(ready)
        registered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        discovering = pool.submit(delivery._discover_stores, time.monotonic())
        try:
            assert entered.wait(2)
            registration = pool.submit(register_ready)
            progressed = registered.wait(1)
        finally:
            release.set()
        discovering.result(timeout=2)
        registration.result(timeout=2)
    assert progressed, "Filesystem iteration blocked post-commit registration in another root."


def test_busy_namespace_does_not_stall_delivery_for_another_namespace(monkeypatch):
    first = MeshStore("busy-first")
    second = MeshStore("ready-second")
    fact(first, "first")
    fact(second, "second")
    enable_delivery(monkeypatch)
    delivery.register_mutation_store(first)
    delivery.register_mutation_store(second)
    writer_entered, release_writer, second_delivered = threading.Event(), threading.Event(), threading.Event()
    original = audit.deliver_mutation_events

    def observe(store, *, limit=256):
        count = original(store, limit=limit)
        if store.namespace == second.namespace and count:
            second_delivered.set()
        return count

    monkeypatch.setattr(audit, "deliver_mutation_events", observe)

    def hold_writer():
        with first.write_transaction():
            writer_entered.set()
            assert release_writer.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(hold_writer)
        try:
            assert writer_entered.wait(2)
            draining = pool.submit(delivery.drain_mutation_delivery)
            progressed = second_delivered.wait(1)
        finally:
            release_writer.set()
        writing.result(timeout=2)
        draining.result(timeout=2)
    assert progressed, "A busy writer blocked delivery for an independent namespace."
    assert second.list_mutation_events() == []


def test_concurrent_commit_during_partial_delivery_remains_hinted(monkeypatch):
    enable_delivery(monkeypatch)
    store = MeshStore("concurrent")
    fact(store, "first")
    delivered, release = threading.Event(), threading.Event()
    original = delivery._deliver_ready_store

    def pause_after_delivery(root, namespace, hint, batch_size):
        count = original(root, namespace, hint, batch_size)
        delivered.set()
        assert release.wait(5)
        return count

    monkeypatch.setattr(delivery, "_deliver_ready_store", pause_after_delivery)
    with ThreadPoolExecutor(max_workers=1) as pool:
        draining = pool.submit(delivery.drain_mutation_delivery)
        try:
            assert delivered.wait(2)
            fact(store, "second")
        finally:
            release.set()
        assert draining.result(timeout=2) == 1
    monkeypatch.setattr(delivery, "_deliver_ready_store", original)
    assert delivery.drain_mutation_delivery() == 1
    assert store.list_mutation_events() == [] and len(audit_rows(store)) == 2


def test_real_audit_worker_delivers_committed_outbox_and_stops(monkeypatch):
    store = MeshStore("worker")
    completed = threading.Event()
    original = store.acknowledge_mutation_events

    def acknowledged(event_ids):
        original(event_ids)
        completed.set()

    monkeypatch.setattr(store, "acknowledge_mutation_events", acknowledged)
    monkeypatch.setattr(audit, "_ensure_audit_drain_thread", _START_AUDIT_WORKER)
    enable_delivery(monkeypatch)
    try:
        fact(store)
        assert completed.wait(3), "The actual audit worker did not drain the committed outbox."
        assert store.list_mutation_events() == []
        assert len(audit_rows(store)) == 1
    finally:
        audit.stop_audit_worker()
    assert audit._AUDIT_DRAIN_THREAD is None


@pytest.mark.parametrize("arguments", [
    {"max_stores": 0}, {"max_stores": 257}, {"max_stores": True},
    {"batch_size": 0}, {"batch_size": 1001}, {"batch_size": 1.5},
])
def test_delivery_rejects_invalid_limits(arguments):
    with pytest.raises(ValueError):
        delivery.drain_mutation_delivery(**arguments)
