"""Audit admission, checkout and namespace maintenance share one ordering contract."""
from __future__ import annotations

import contextlib
import os
import threading

import pytest

from mesh import audit, store as storage
from mesh.sqlite_runtime import sqlite3
from mesh.store import MeshStore
from tests.test_production_storage import fact, isolated_storage as isolated_storage


def retire_audit_file(namespace):
    path = audit._get_audit_db_path(namespace)
    retired = path.with_name("retired-" + path.name)
    with audit._AUDIT_LOCK:
        connection = audit._AUDIT_CONNECTION_POOL.pop(path, None)
        if connection is not None:
            connection.close_pooled()
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as connection:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    print("Moving isolated audit file:", path.name, "to", retired.name)
    assert path.parent == retired.parent
    os.replace(path, retired)
    return path, retired


def queued_read(namespace, actor="old-reader"):
    audit.log_recall("The cedar queue uses two workers.", namespace, actor, [])
    assert audit.get_audit_queue_stats()["pending_events"] >= 1


def test_queued_read_history_cannot_recreate_a_retired_audit_file():
    namespace = "audit-retired-read"
    queued_read(namespace)
    path, retired = retire_audit_file(namespace)
    audit.flush_audit_log(namespace)
    assert not path.exists()
    assert retired.exists()
    stats = audit.get_audit_queue_stats()
    assert stats["pending_events"] == stats["pending_bytes"] == 0
    assert stats["dropped_events"] == 1


def test_queued_read_history_cannot_transfer_to_a_replacement_file():
    namespace = "audit-replaced-read"
    queued_read(namespace)
    path, _ = retire_audit_file(namespace)
    connection = audit._connect_audit(path)
    audit.flush_audit_log(namespace)
    assert connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
    assert audit.get_audit_queue_stats()["dropped_events"] == 1


def test_mixed_queue_identities_commit_only_the_current_events():
    namespace = "audit-mixed-identities"
    queued_read(namespace)
    path, _ = retire_audit_file(namespace)
    queued_read(namespace, actor="new-reader")
    audit.flush_audit_log(namespace)
    connection = audit._connect_audit(path)
    assert [row[0] for row in connection.execute("SELECT agent_id FROM audit_log")] == ["new-reader"]
    stats = audit.get_audit_queue_stats()
    assert stats["pending_events"] == stats["pending_bytes"] == 0
    assert stats["dropped_events"] == 1


def test_audit_retry_keeps_admission_identity_and_drops_after_retirement(monkeypatch):
    namespace = "audit-retired-retry"
    queued_read(namespace)
    original = audit._persist_audit_entries

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("Isolated sink outage")

    monkeypatch.setattr(audit, "_persist_audit_entries", unavailable)
    audit.flush_audit_log(namespace)
    before = audit.get_audit_queue_stats()
    assert before["pending_events"] == 1 and before["retried_events"] == 1
    path, _ = retire_audit_file(namespace)
    monkeypatch.setattr(audit, "_persist_audit_entries", original)
    audit.flush_audit_log(namespace)
    assert not path.exists()
    assert audit.get_audit_queue_stats()["dropped_events"] == 1


def test_cached_audit_native_lease_refuses_a_changed_file_identity(monkeypatch):
    namespace = "audit-native-identity"
    path = audit._get_audit_db_path(namespace)
    original = audit._connect_audit(path)
    status = path.stat()
    changed_identity = (status.st_dev, status.st_ino + 1)
    monkeypatch.setattr(audit, "_audit_file_identity", lambda candidate: changed_identity, raising=False)
    with pytest.raises(RuntimeError, match="retired|identity"):
        with original:
            original.execute("SELECT 1")
    replacement = audit._connect_audit(path)
    assert replacement is not original
    assert original._closed
    assert replacement.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_direct_durable_delivery_waits_for_namespace_before_touching_audit(monkeypatch):
    source = MeshStore("audit-direct-lease")
    fact(source)
    entered_persistence = threading.Event()
    completed = threading.Event()
    outcomes = []
    original = audit._persist_audit_entries

    def observe(*args, **kwargs):
        entered_persistence.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(audit, "_persist_audit_entries", observe)

    def deliver():
        try:
            outcomes.append(audit.deliver_mutation_events(source))
        except BaseException as exc:
            outcomes.append(exc)
        finally:
            completed.set()

    worker = threading.Thread(target=deliver)
    try:
        with storage.namespace_write_lock(source.namespace, data_root=source.db_path.parent):
            worker.start()
            assert not entered_persistence.wait(0.2)
            assert not completed.is_set()
    finally:
        if worker.ident is not None:
            worker.join(timeout=10)
    assert not worker.is_alive()
    assert outcomes == [1]
    assert source.list_mutation_events() == []


def test_worker_waiting_for_namespace_does_not_hold_global_flush_lock(monkeypatch):
    source = MeshStore("audit-flush-order")
    queued_read(source.namespace)
    waiting = threading.Event()
    evicted = threading.Event()
    errors = []
    original = audit._persist_audit_entries

    def cooperating_writer(*args, **kwargs):
        waiting.set()
        with storage.namespace_write_lock(source.namespace, data_root=source.db_path.parent):
            return original(*args, **kwargs)

    monkeypatch.setattr(audit, "_persist_audit_entries", cooperating_writer)

    def flush():
        try:
            audit.flush_audit_log(source.namespace)
        except BaseException as exc:
            errors.append(exc)

    def evict():
        try:
            audit.evict_audit_cache(source.namespace)
        except BaseException as exc:
            errors.append(exc)
        finally:
            evicted.set()

    drain = threading.Thread(target=flush)
    eviction = threading.Thread(target=evict)
    try:
        with storage.namespace_write_lock(source.namespace, data_root=source.db_path.parent):
            drain.start()
            assert waiting.wait(5)
            eviction.start()
            assert evicted.wait(0.3)
    finally:
        drain.join(timeout=10)
        if eviction.ident is not None:
            eviction.join(timeout=10)
    assert not drain.is_alive() and not eviction.is_alive()
    assert not errors


def test_scrub_deduplication_cannot_skip_erasure_in_another_data_root(isolated_storage):
    first_root = isolated_storage / "first-audit-root"
    second_root = isolated_storage / "second-audit-root"
    sql = "INSERT INTO audit_log(timestamp,action,agent_id,namespace,memory_id,query,content) VALUES(?,?,?,?,?,?,?)"
    arguments = ("2026-01-01T00:00:00+00:00", "learn", "test-agent", "same", "same-id", "Archived cedar note", "Archived cedar note")
    for root in (first_root, second_root):
        with audit.mesh_dir_context(root):
            path = audit._get_audit_db_path("same")
            audit._persist_audit_entries(path, [(sql, arguments)])
    for root in (first_root, second_root):
        with audit.mesh_dir_context(root):
            audit.purge_memory_from_audit("same", "same-id", content="Archived cedar note")
            connection = audit._connect_audit(audit._get_audit_db_path("same"))
            row = connection.execute("SELECT query,content FROM audit_log").fetchone()
            assert all("Archived cedar note" not in str(value) for value in row)
