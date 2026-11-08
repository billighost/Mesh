"""Production storage invariants, using temporary homes and the bundled SQLite driver."""
from __future__ import annotations

import contextvars
import gc
import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from mesh import audit
from mesh import store as storage
from mesh.crypto import is_encrypted_text
from mesh.sqlite_runtime import sqlite3
from mesh.store import CacheBudgetExceededError, MeshStore, SCHEMA_VERSION


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
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
        "MESH_PASSPHRASE", "MESH_MAX_VECTOR_CACHE_BYTES", "MESH_MAX_VECTOR_CACHE_SIZE",
        "MESH_MAX_POOL_SIZE", "MESH_MAX_OPEN_NAMESPACES", "MESH_MAX_BULK_ITEMS",
        "MESH_MAX_CACHE_DELTA_BYTES", "MESH_MAX_PENDING_EMBEDDING_JOBS", "MESH_MAX_MUTATION_OUTBOX",
        "MESH_MAX_OPERATION_RECEIPTS", "MESH_MAX_OPERATION_RECEIPT_BYTES", "MESH_MAX_LIST_ITEMS",
        "MESH_MAX_ACCESS_TRACKING_ENTRIES", "MESH_MAX_ACCESS_TRACKING_BYTES",
        "MESH_MAX_AUDIT_QUEUE_EVENTS", "MESH_MAX_AUDIT_QUEUE_BYTES", "MESH_READERS_PER_NAMESPACE",
        "MESH_MAX_READER_CONNECTIONS", "MESH_TELEMETRY_BATCH", "MESH_TELEMETRY_FLUSH_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MESH_LOG_QUERY_TEXT", "1")
    monkeypatch.setenv("MESH_TRACKING_BATCH", "100000")
    monkeypatch.setenv("MESH_TRACKING_FLUSH_SECONDS", "100000")
    audit.stop_audit_worker()
    # Queue tests drive explicit snapshots, never an automatically running worker.
    monkeypatch.setattr(audit, "_ensure_audit_drain_thread", lambda: None)
    with audit._AUDIT_QUEUE_LOCK:
        audit._AUDIT_PENDING.clear()
        audit._AUDIT_PENDING_COUNT = audit._AUDIT_PENDING_BYTES = 0
        audit._AUDIT_DROPPED_COUNT = audit._AUDIT_RETRY_COUNT = 0
        audit._AUDIT_LAST_ERROR = None
    with storage._ACCESS_TRACKING_LOCK:
        storage._ACCESS_TRACKING_BUFFERS.clear()
        storage._ACCESS_TRACKING_INFLIGHT_ENTRIES = storage._ACCESS_TRACKING_INFLIGHT_BYTES = 0
        storage._ACCESS_TRACKING_DROPPED_ENTRIES = 0
    storage._VECTOR_CACHE.clear()
    storage._CACHE_REJECTIONS.clear()
    gc.collect()
    yield tmp_path
    audit.stop_audit_worker()
    storage._close_all_pooled_connections()
    audit._close_all_audit_connections()
    storage._VECTOR_CACHE.clear()
    storage._CACHE_REJECTIONS.clear()
    gc.collect()


def vector() -> list[float]:
    result = [0.0] * 384
    result[0] = 1.0
    return result


def fact(store: MeshStore, memory_id: str = "fact-1", *, private=False, dense=False):
    store.upsert(
        memory_id, f"The durable fact {memory_id} uses the production storage contract.",
        vector() if dense else None,
        {"source_agent": "test-agent", "tags": ["contract"], "embedding_model": "onnx-minilm-l6-v2" if dense else "needs-reembed"},
        local_only=private,
    )


def scalar(conn, sql: str):
    return conn.execute(sql).fetchone()[0]


def identity(store: MeshStore, memory_id: str = "fact-1") -> dict:
    row = store.get(memory_id)
    return {"expected_incarnation": row["incarnation"], "expected_store_generation": row["store_generation"]}


def test_full_sync_schema_jobs_and_content_free_outbox():
    store = MeshStore("durable")
    fact(store)
    with store._connect() as conn:
        assert scalar(conn, "PRAGMA synchronous") == 2
        assert scalar(conn, "PRAGMA user_version") == int(SCHEMA_VERSION)
        assert scalar(conn, "SELECT value FROM mesh_meta WHERE key='namespace_owner'") == "durable"
        assert scalar(conn, "SELECT value FROM mesh_meta WHERE key='mutation_outbox_count'") == "1"
    jobs = store.list_embedding_jobs()
    assert [(job["memory_id"], job["revision"]) for job in jobs] == [("fact-1", 1)]
    events = store.list_mutation_events()
    assert len(events) == 1 and events[0]["revision"] == 1
    assert "production storage contract" not in json.dumps(events)


def test_nested_checkout_does_not_commit_enclosing_transaction():
    store = MeshStore("nested")
    with pytest.raises(RuntimeError, match="abort"):
        with store.write_transaction():
            fact(store)
            with store._connect() as conn:
                assert scalar(conn, "SELECT COUNT(*) FROM memories") == 1
            assert store.get("fact-1") is not None
            raise RuntimeError("abort")
    assert store.count() == 0
    assert store.embedding_job_stats()["pending_count"] == 0
    assert store.list_mutation_events() == []


def _count_committed_refreshes(store, monkeypatch):
    refreshed = []
    original = store._refresh_cache_stamp_after_non_memory_write

    def refresh():
        with store._connect() as conn:
            assert not conn.in_transaction
        refreshed.append(True)
        return original()

    monkeypatch.setattr(store, "_refresh_cache_stamp_after_non_memory_write", refresh)
    return refreshed


def test_nested_receipt_refreshes_once_and_retains_transaction_reads(monkeypatch):
    store = MeshStore("refresh-receipt")
    fact(store, "existing", dense=True)
    store._get_vector_cache()
    refreshed = _count_committed_refreshes(store, monkeypatch)

    def write():
        with store.write_transaction():
            fact(store, "new", dense=True)
            assert store.get("new") is not None
            with store._read_connect() as conn:
                assert conn.in_transaction and scalar(conn, "SELECT COUNT(*) FROM memories") == 2
        assert not refreshed
        return {"memory_id": "new", "revision": 1}

    with store.write_transaction():
        receipt = store.execute_operation("op-refresh", "test-agent", "learn", "a" * 64, write)
        assert not receipt["replayed"] and not refreshed
        assert store.get_operation_receipt("op-refresh", "test-agent") is not None
    assert len(refreshed) == 1
    cache = store._get_vector_cache()
    with cache.read_snapshot():
        assert set(cache.rows_by_id) == {"existing", "new"}
    replay = store.execute_operation("op-refresh", "test-agent", "learn", "a" * 64,
                                     lambda: pytest.fail("A replay must not run its callback."))
    assert replay["replayed"] and len(refreshed) == 2
    assert store.count() == 2 and len(store.list_mutation_events()) == 2


def test_inner_rollback_keeps_outer_cache_and_callbacks_coherent(monkeypatch):
    store = MeshStore("refresh-inner")
    fact(store, "existing", dense=True)
    store._get_vector_cache()
    refreshed = _count_committed_refreshes(store, monkeypatch)
    callbacks = []
    with store.write_transaction():
        fact(store, "first", dense=True)
        store.after_commit(lambda: callbacks.append("first"))
        with pytest.raises(RuntimeError, match="inner abort"):
            with store.write_transaction():
                fact(store, "rolled-back", dense=True)
                store.after_commit(lambda: callbacks.append("rolled-back"))
                raise RuntimeError("inner abort")
        assert store.get("rolled-back") is None and store.get("first") is not None
        assert not refreshed and not callbacks
        fact(store, "last", dense=True)
        store.after_commit(lambda: callbacks.append("last"))
    assert len(refreshed) == 1 and callbacks == ["first", "last"]
    cache = store._get_vector_cache()
    with cache.read_snapshot():
        assert set(cache.rows_by_id) == {"existing", "first", "last"}
    assert len(store.list_mutation_events()) == 3


@pytest.mark.parametrize("failure", ["body", "commit"])
def test_outer_failure_refreshes_rolled_back_state_once(monkeypatch, failure):
    store = MeshStore("refresh-outer")
    fact(store, "existing", dense=True)
    store._get_vector_cache()
    refreshed = _count_committed_refreshes(store, monkeypatch)
    callbacks = []
    if failure == "commit":
        commit = storage._commit_write

        def fail_outer_commit(conn, marker):
            if marker is None:
                raise RuntimeError("outer abort")
            return commit(conn, marker)

        monkeypatch.setattr(storage, "_commit_write", fail_outer_commit)
    with pytest.raises(RuntimeError, match="outer abort"):
        with store.write_transaction():
            with store.write_transaction():
                fact(store, "new", dense=True)
                store.after_commit(lambda: callbacks.append("new"))
            store.execute_operation("op-abort", "test-agent", "learn", "a" * 64,
                                    lambda: {"memory_id": "new", "revision": 1})
            assert not refreshed and store.get("new") is not None
            if failure == "body":
                raise RuntimeError("outer abort")
    assert len(refreshed) == 1 and not callbacks
    assert store.get("new") is None and store.get_operation_receipt("op-abort", "test-agent") is None
    assert len(store.list_mutation_events()) == 1 and store.embedding_job_stats()["pending_count"] == 0
    cache = store._get_vector_cache()
    with cache.read_snapshot():
        assert set(cache.rows_by_id) == {"existing"}


def test_group_receipts_share_one_committed_refresh(monkeypatch):
    from mesh.group_commit import GroupCommitBinding, GroupCommitCoordinator, GroupCommitLimits

    store = MeshStore("refresh-group")
    fact(store, "existing", dense=True)
    store._get_vector_cache()
    refreshed = _count_committed_refreshes(store, monkeypatch)
    coordinator = GroupCommitCoordinator(store.db_path.parent, limits=GroupCommitLimits(coalesce_seconds=0),
                                         autostart=False)
    tickets = []
    try:
        for index in range(2):
            memory_id = f"new-{index}"
            operation_id = f"op-{index}"
            binding = GroupCommitBinding(store.namespace, "local-user", "test-agent", operation_id,
                                         "mesh_learn", "a" * 64)
            ticket = coordinator.reserve(binding, byte_cost=256)
            assert ticket.begin_preparation()

            def write(memory_id=memory_id):
                fact(store, memory_id, dense=True)
                return {"memory_id": memory_id, "revision": 1}

            def execute(operation_id=operation_id, write=write):
                result = store.execute_operation(operation_id, "local-user", "mesh_learn", "a" * 64, write)
                assert not refreshed
                return result

            ticket.prepared(store, execute)
            tickets.append(ticket)
        batch = coordinator._next_batch()
        assert len(batch) == 2
        coordinator._execute_batch(batch)
        assert len(refreshed) == 1
        assert all(not ticket.future.result(timeout=0)["replayed"] for ticket in tickets)
        assert coordinator.stats()["committed_batches"] == 1
        assert store.count() == 3 and len(store.list_mutation_events()) == 3
        with store._connect() as conn:
            assert scalar(conn, "PRAGMA synchronous") == 2
            assert scalar(conn, "PRAGMA journal_mode") == "wal"
        cache = store._get_vector_cache()
        with cache.read_snapshot():
            assert set(cache.rows_by_id) == {"existing", "new-0", "new-1"}
    finally:
        assert coordinator.close()


def test_memory_task_conflict_and_receipt_rollback_together():
    store = MeshStore("rollback")
    fact(store, "existing")
    before_events = store.list_mutation_events()
    with pytest.raises(RuntimeError, match="abort"):
        with store.write_transaction():
            store.upsert("new", "A contradictory durable fact.", None, {"source_agent": "test-agent"}, conflict={"existing_memory_id": "existing", "existing_revision": 1, "similarity": .9})
            store.create_work_item("Track the change", created_by="test-agent")
            store.execute_operation("op-1", "test-agent", "learn", "a" * 64, lambda: {"memory_id": "new", "revision": 1})
            raise RuntimeError("abort")
    assert store.count() == 1 and store.get("new") is None
    assert store.list_work_items() == []
    assert store.get_operation_receipt("op-1", "test-agent") is None
    assert store.list_mutation_events() == before_events
    with store._read_connect() as conn:
        assert scalar(conn, "SELECT COUNT(*) FROM contradictions") == 0


def test_after_commit_context_is_captured_and_savepoint_rollback_discards_callback():
    store = MeshStore("callbacks")
    caller = contextvars.ContextVar("storage_test_caller", default="outside")
    seen = []
    with store.write_transaction():
        token = caller.set("first-owner-private")
        store.after_commit(lambda: seen.append(caller.get()))
        caller.reset(token)
        with pytest.raises(ValueError, match="discard"):
            with store.write_transaction():
                token = caller.set("discarded-owner")
                store.after_commit(lambda: seen.append(caller.get()))
                caller.reset(token)
                raise ValueError("discard")
        token = caller.set("second-owner-public")
        store.after_commit(lambda: seen.append(caller.get()))
        caller.reset(token)
        assert seen == []
    assert seen == ["first-owner-private", "second-owner-public"]
    assert caller.get() == "outside"


def test_scalar_after_commit_callbacks_run_after_namespace_locks_release(monkeypatch):
    store = MeshStore("scalar-callback")
    original = store._record_mutation_in_tx
    finished = []
    def record(conn, *args):
        original(conn, *args)
        def committed():
            with ThreadPoolExecutor(max_workers=1) as pool:
                observed = pool.submit(store.count).result(timeout=2)
            finished.append(observed)
        store.after_commit(committed)
    monkeypatch.setattr(store, "_record_mutation_in_tx", record)
    fact(store)
    assert finished == [1]


def test_revision_cas_and_embedding_target_reject_stale_publication():
    store = MeshStore("cas")
    fact(store)
    store.update("fact-1", content="The edited authoritative fact.")
    assert not store.publish_embedding("fact-1", vector(), expected_revision=1, embedding_model="onnx-minilm-l6-v2", **identity(store))
    assert not store.publish_embedding("fact-1", vector(), expected_revision=2, embedding_model="other-model", **identity(store))
    assert store.get("fact-1")["embedding_model"] == "needs-reembed"
    assert store.publish_embedding("fact-1", vector(), expected_revision=2, embedding_model="onnx-minilm-l6-v2", **identity(store))
    assert store.get("fact-1")["revision"] == 2
    assert store.embedding_job_stats()["pending_count"] == 0
    with pytest.raises(ValueError, match="revision changed"):
        store.delete("fact-1", expected_revision=1)
    assert store.get("fact-1") is not None


def test_embedding_retry_survives_same_revision_same_target_enqueue():
    store = MeshStore("backoff")
    fact(store)
    store.retry_embedding_job("fact-1", expected_revision=1, expected_target_model="onnx-minilm-l6-v2", delay_seconds=3600, **identity(store))
    with store._read_connect() as conn:
        before = tuple(conn.execute("SELECT attempts,available_at FROM embedding_jobs").fetchone())
    assert store.enqueue_embedding_job("fact-1", expected_revision=1, **identity(store))
    with store._read_connect() as conn:
        assert tuple(conn.execute("SELECT attempts,available_at FROM embedding_jobs").fetchone()) == before
    assert store.list_embedding_jobs() == []
    store.update("fact-1", content="New revision resets the retry schedule.")
    assert store.list_embedding_jobs()[0]["attempts"] == 0


@pytest.mark.parametrize("capacity_env", ["MESH_MAX_PENDING_EMBEDDING_JOBS", "MESH_MAX_MUTATION_OUTBOX"])
def test_durable_queue_capacity_rejects_entire_mutation(capacity_env, monkeypatch):
    store = MeshStore("capacity")
    monkeypatch.setenv(capacity_env, "1")
    fact(store, "first")
    with pytest.raises(RuntimeError, match="capacity"):
        fact(store, "second")
    assert store.count() == 1 and store.get("second") is None
    assert store.embedding_job_stats()["pending_count"] == 1
    assert len(store.list_mutation_events()) == 1


def test_idempotent_receipt_replay_digest_refusal_and_sanitized_results():
    store = MeshStore("receipts")
    calls = []
    def write():
        calls.append(True)
        fact(store)
        return {"count": 1, "errors": "untrusted error body", "content": "body", "results": [{"memory_id": "fact-1", "revision": 1, "content": "body", "status": "untrusted body with spaces"}]}
    first = store.execute_operation("op-1", "test-agent", "learn_many", "a" * 64, write)
    replay = store.execute_operation("op-1", "test-agent", "learn_many", "a" * 64, write)
    assert not first["replayed"] and replay["replayed"] and len(calls) == 1
    assert replay["result"] == {"count": 1, "results": [{"memory_id": "fact-1", "revision": 1}]}
    with pytest.raises(ValueError, match="different request"):
        store.execute_operation("op-1", "test-agent", "learn_many", "b" * 64, write)
    assert store.count() == 1
    with store._read_connect() as conn:
        assert "body" not in scalar(conn, "SELECT result_json FROM operation_receipts")


def test_receipt_nesting_limit_rolls_back_callback_mutation():
    store = MeshStore("receipt-depth")
    nested = {"id": "fact-1"}
    for _ in range(10):
        nested = {"results": [nested]}
    def callback():
        fact(store)
        return nested
    with pytest.raises(ValueError, match="nesting"):
        store.execute_operation("op-1", "test-agent", "learn", "a" * 64, callback)
    assert store.count() == 0 and store.list_mutation_events() == []


def test_private_creation_metadata_only_and_existing_private_mutation_refusal():
    store = MeshStore("privacy")
    fact(store, private=True)
    assert store.get("fact-1", include_private=False) is None
    for operation in (
        lambda: store.update("fact-1", tags=["new"], include_private=False),
        lambda: store.delete("fact-1", include_private=False),
        lambda: store.upsert("fact-1", "Replacement public fact.", None, {"source_agent": "test-agent"}, include_private=False),
    ):
        with pytest.raises(ValueError, match="unavailable|permitted"):
            operation()
    task = store.create_work_item("Private task", local_only=True, include_private=False, created_by="test-agent")
    assert task["local_only"] and "title" not in task
    assert store.get_work_item(task["id"], include_private=False) is None
    assert store.get_work_events(task["id"], include_private=False) == []
    assert store.get_project_brief(max_tokens=500)["facts_included"] == 0


def test_task_owner_checks_private_parent_inheritance_and_evidence_guard():
    store = MeshStore("task-security")
    parent = store.create_work_item("Private parent", kind="goal", local_only=True, created_by="owner")
    child = store.create_work_item("Child task", parent_id=parent["id"], created_by="owner")
    assert child["local_only"]
    with pytest.raises(ValueError, match="unavailable|permitted"):
        store.update_work_item(child["id"], title="Changed", expected_source_agent="other")
    task = store.create_work_item("Evidence task", created_by="owner")
    original_events = store.get_work_events(task["id"])
    with pytest.raises(ValueError, match="Evidence"):
        store.complete_work_item(task["id"], "A complete summary.", evidence=[1])
    assert store.get_work_item(task["id"])["status"] == "todo"
    assert store.get_work_events(task["id"]) == original_events
    fake_token = "ghp_" + "A" * 36
    completed = store.complete_work_item(task["id"], "A complete summary.", evidence=[fake_token], agent_id="owner")
    assert completed["local_only"]
    assert fake_token not in json.dumps(completed["evidence"])


def test_task_transition_and_event_rollback_when_outbox_is_full(monkeypatch):
    store = MeshStore("task-capacity")
    task = store.create_work_item("Unfinished task", created_by="owner")
    monkeypatch.setenv("MESH_MAX_MUTATION_OUTBOX", "1")
    with pytest.raises(RuntimeError, match="capacity"):
        store.complete_work_item(task["id"], "The work was completed.")
    assert store.get_work_item(task["id"])["status"] == "todo"
    assert len(store.get_work_events(task["id"])) == 1


def test_twenty_current_visible_facts_and_continuation_budget():
    store = MeshStore("brief")
    for index in range(26):
        fact(store, f"fact-{index:02}")
    fact(store, "private", private=True)
    store.upsert("quarantine", "Quarantined fact.", None, {"quarantined": True})
    start = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    store.upsert("expired", "Expired fact.", None, {"ttl_days": 1, "created_at": start, "decay_start_at": start})
    first = store.get_project_brief(max_tokens=2000)
    second = store.get_project_brief(max_tokens=2000, fact_offset=first["next_fact_offset"])
    assert first["facts_available"] == 26
    assert first["facts_included"] == 20 and second["facts_included"] == 6
    assert set(first["selected_memory_ids"]).isdisjoint(second["selected_memory_ids"])
    assert second["next_fact_offset"] is None
    assert first["budget_scope"] == "brief_markdown" and first["estimated_tokens"] <= 2000
    compact = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    assert first["payload_estimated_tokens"] == (len(compact) + 3) // 4
    tiny = store.get_project_brief(max_tokens=1)
    assert tiny["facts_included"] == 0 and tiny["next_fact_offset"] == 0
    assert len(tiny["brief_markdown"]) <= 4


@pytest.mark.parametrize("arguments", [{"max_tokens": 0}, {"max_tokens": 32769}, {"max_facts": 101}, {"fact_offset": -1}])
def test_brief_parameter_bounds(arguments):
    with pytest.raises(ValueError):
        MeshStore("brief-bounds").get_project_brief(**arguments)


def test_native_operations_work_when_cache_is_unavailable(monkeypatch):
    store = MeshStore("native")
    fact(store, "First_id")
    fact(store, "FirstXid")
    monkeypatch.setenv("MESH_MAX_VECTOR_CACHE_BYTES", "1024")
    with pytest.raises(CacheBudgetExceededError):
        store._get_vector_cache()
    assert store.resolve_id_prefix("FIRST_ID") == "First_id"
    assert store.resolve_id_prefix("First_") == "First_id"
    assert store.update("First_id", tags=["new-tag"])["revision"] == 2
    assert store.count_matching(tags=["new-tag"]) == 1
    assert len(store.list_all(limit=1)) == 1
    assert len(store.get_exportable_memories()) == 2
    assert store.get_project_brief(max_tokens=500)["facts_available"] == 2
    assert store.delete("First_id") and store.count() == 1


def test_prefix_ambiguity_is_bounded_and_does_not_expose_other_ids():
    store = MeshStore("prefix")
    fact(store, "same-one")
    fact(store, "same-two")
    fact(store, "same-private", private=True)
    with pytest.raises(ValueError) as failure:
        store.resolve_id_prefix("same", include_private=False)
    assert "same-one" not in str(failure.value) and "same-two" not in str(failure.value)
    assert store.resolve_id_prefix("same-private", include_private=False) is None
    assert store.resolve_id_prefix("same-one", expected_source_agent="other") is None


def test_cache_preflight_avoids_loading_rows_for_oversized_namespace(monkeypatch):
    store = MeshStore("preflight")
    fact(store)
    monkeypatch.setenv("MESH_MAX_VECTOR_CACHE_BYTES", "1024")
    def forbidden(*args):
        raise AssertionError("Row bodies must not be loaded after preflight refusal.")
    monkeypatch.setattr(store, "_deserialize_memory_row", forbidden)
    with pytest.raises(CacheBudgetExceededError):
        store._get_vector_cache()
    assert storage.get_vector_cache_stats()["charged_bytes"] == 0


def test_aggregate_cache_budget_counts_pinned_generations_until_release(monkeypatch):
    store = MeshStore("pinned")
    for index in range(20):
        fact(store, f"fact-{index}")
    old = store._get_vector_cache()
    budget = old._cache_bytes * 2
    monkeypatch.setenv("MESH_MAX_VECTOR_CACHE_BYTES", str(budget))
    with pytest.raises(CacheBudgetExceededError) as refusal:
        store._get_vector_cache(force_refresh=True)
    refusal.value.__traceback__ = None
    del refusal
    gc.collect()
    stats = storage.get_vector_cache_stats()
    assert old._cache_bytes <= stats["charged_bytes"] <= budget
    del old
    gc.collect()
    assert storage.get_vector_cache_stats()["charged_bytes"] == 0
    assert len(store._get_vector_cache().all_ids) == 20
    assert storage.get_vector_cache_stats()["charged_bytes"] <= budget


def test_exact_initial_matrix_and_bounded_growth():
    store = MeshStore("matrix")
    for index in range(100):
        fact(store, f"fact-{index}", dense=True)
    cached = store._get_vector_cache()
    assert cached._matrix_buf.shape == (100, 384)
    fact(store, "extra", dense=True)
    assert 101 <= cached._matrix_buf.shape[0] <= 116
    assert cached._hash_matrix_buf is None


def test_grouped_writes_publish_warm_cache_once_after_commit():
    store = MeshStore("group-cache")
    fact(store, dense=True)
    cached = store._get_vector_cache()
    builds = storage.get_cache_rebuild_count()
    with store.write_transaction():
        store.upsert_batch([(f"added-{i}", f"Added durable fact {i}.", vector(), {"source_agent": "test-agent", "embedding_model": "onnx-minilm-l6-v2"}, False) for i in range(10)])
        store.update("fact-1", tags=["new-tag"])
        store.delete("added-9")
        assert len(cached.all_ids) == 1 and cached.rows_by_id["fact-1"]["revision"] == 1
        assert store.count() == 10
    assert store._get_vector_cache() is cached
    assert storage.get_cache_rebuild_count() == builds
    assert len(cached.all_ids) == len(cached.ids) == 10
    assert cached.rows_by_id["fact-1"]["tags_list"] == ["new-tag"]
    assert "added-9" not in cached.rows_by_id
    assert storage.get_vector_cache_stats()["live_generations_and_builds"] == 1


def test_dense_publication_updates_warm_generation_without_content_revision_change():
    store = MeshStore("dense-publish")
    fact(store)
    cached = store._get_vector_cache()
    builds = storage.get_cache_rebuild_count()
    assert store.publish_embedding("fact-1", vector(), expected_revision=1, embedding_model="onnx-minilm-l6-v2", **identity(store))
    assert store._get_vector_cache() is cached
    assert storage.get_cache_rebuild_count() == builds
    assert cached.rows_by_id["fact-1"]["revision"] == 1
    assert cached.rows_by_id["fact-1"]["embedding_model"] == "onnx-minilm-l6-v2"
    assert cached.matrix.shape == (1, 384)


def test_cache_delta_pressure_preserves_committed_memory(monkeypatch):
    store = MeshStore("cache-pressure")
    fact(store)
    old = store._get_vector_cache()
    monkeypatch.setenv("MESH_MAX_CACHE_DELTA_BYTES", "1024")
    with store.write_transaction():
        fact(store, "second")
    assert store.count() == 2 and store.get("second") is not None
    assert not old._valid
    assert len(store.list_mutation_events()) == 2


def test_same_namespace_readers_see_committed_snapshot_while_writer_pauses():
    store = MeshStore("readers")
    fact(store)
    cached = store._get_vector_cache()
    entered, release = threading.Event(), threading.Event()
    def writer():
        with store.write_transaction():
            fact(store, "pending")
            assert store.get("pending") is not None and store.count() == 2
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(writer)
        try:
            assert entered.wait(2)
            observed = pool.submit(lambda: (store.count(), store.get("pending"), store._get_vector_cache() is cached, store.get_project_brief(max_tokens=500)["facts_available"])).result(timeout=2)
            assert observed == (1, None, True, 1)
        finally:
            release.set()
        pending.result(timeout=3)
    assert store.count() == 2


def test_cold_connection_open_does_not_block_another_namespace(monkeypatch):
    ready = MeshStore("ready")
    original = sqlite3.connect
    entered, release = threading.Event(), threading.Event()
    def connect(path, *args, **kwargs):
        if str(path).endswith("mesh_slow.db"):
            entered.set()
            assert release.wait(5)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(MeshStore, "slow")
        try:
            assert entered.wait(2)
            assert pool.submit(ready.count).result(timeout=2) == 0
        finally:
            release.set()
        pending.result(timeout=3)


def test_vector_eviction_keeps_an_active_writer_available_for_nested_calls(monkeypatch):
    monkeypatch.setenv("MESH_MAX_VECTOR_CACHE_SIZE", "2")
    active = MeshStore("active")
    fact(active)
    active._get_vector_cache()
    entered, release = threading.Event(), threading.Event()
    def writer():
        with active.write_transaction():
            fact(active, "pending")
            entered.set()
            assert release.wait(5)
            fact(active, "after-eviction")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(writer)
        try:
            assert entered.wait(2)
            for name in ("other-one", "other-two"):
                store = MeshStore(name)
                fact(store)
                store._get_vector_cache()
            assert active.db_path in storage._CONNECTION_POOL
        finally:
            release.set()
        pending.result(timeout=3)
    assert active.count() == 3


def test_reader_admission_waits_for_a_lease_and_obeys_connection_cap(monkeypatch):
    store = MeshStore("reader-cap")
    monkeypatch.setenv("MESH_READERS_PER_NAMESPACE", "2")
    monkeypatch.setenv("MESH_MAX_READER_CONNECTIONS", "2")
    ready = [threading.Event(), threading.Event()]
    release = threading.Event()
    third_started, third_finished = threading.Event(), threading.Event()
    def hold(index):
        with store._read_connect() as conn:
            scalar(conn, "SELECT COUNT(*) FROM memories")
            ready[index].set()
            assert release.wait(5)
    def third():
        third_started.set()
        result = store.count()
        third_finished.set()
        return result
    with ThreadPoolExecutor(max_workers=3) as pool:
        holders = [pool.submit(hold, index) for index in range(2)]
        try:
            assert all(event.wait(2) for event in ready)
            waiting = pool.submit(third)
            assert third_started.wait(2)
            assert not third_finished.wait(.1)
            assert len(storage._READER_CONNECTION_POOL) == 2
        finally:
            release.set()
        assert waiting.result(timeout=2) == 0
        for holder in holders:
            holder.result(timeout=2)


def test_pending_access_statistics_are_visible_without_cache_rebuild():
    store = MeshStore("access-overlay")
    fact(store)
    store._get_vector_cache()
    store.record_recall_access(["fact-1"], "A bounded query", "test-agent", .8, 1)
    assert store.get("fact-1")["access_count"] == 1
    store.update("fact-1", tags=["new-tag"])
    assert store.get("fact-1")["access_count"] == 1
    store.flush_access_tracking()
    assert store.get("fact-1")["access_count"] == 1


def test_access_buffer_admission_and_failed_flush_keep_work_bounded(monkeypatch):
    store = MeshStore("tracking")
    fact(store)
    monkeypatch.setenv("MESH_MAX_ACCESS_TRACKING_ENTRIES", "2")
    for index in range(10):
        store.record_recall_access(["fact-1"], f"Query {index}", "test-agent", .9, 1)
    before = storage.get_access_tracking_stats()
    assert before["pending_entries"] == 2 and before["dropped_entries"] == 9
    original = storage._begin_write
    def fail(*args):
        raise sqlite3.OperationalError("synthetic I/O failure")
    monkeypatch.setattr(storage, "_begin_write", fail)
    store.flush_access_tracking()
    assert storage.get_access_tracking_stats()["pending_entries"] == 2
    monkeypatch.setattr(storage, "_begin_write", original)
    store.flush_access_tracking()
    assert storage.get_access_tracking_stats()["pending_entries"] == 0
    assert store.get("fact-1")["access_count"] == 10


def test_access_flush_uses_buffer_root_after_caller_root_changes(tmp_path):
    first_root, other_root = tmp_path / "first-root", tmp_path / "other-root"
    with audit.mesh_dir_context(first_root):
        first = MeshStore("same")
        fact(first)
        first.record_recall_access(["fact-1"], "First root query", "test-agent", .8, 1)
    with audit.mesh_dir_context(other_root):
        other = MeshStore("same")
        fact(other)
        first.flush_access_tracking()
        with first._read_connect() as conn:
            assert scalar(conn, "SELECT access_count FROM memories") == 1
        assert other.get("fact-1")["access_count"] == 0


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nonfinite_vector_input_is_rejected_before_content_is_written(bad):
    store = MeshStore("finite")
    invalid = vector()
    invalid[10] = bad
    with pytest.raises(ValueError, match="finite"):
        store.upsert("bad", "Invalid vector input.", invalid, {})
    assert store.count() == 0 and store.list_mutation_events() == []


@pytest.mark.parametrize("blob", [b"broken", np.full(384, np.nan, dtype=np.float32).tobytes()])
def test_malformed_derived_vector_is_healable_and_does_not_break_cache(blob):
    store = MeshStore("malformed")
    fact(store, dense=True)
    with store.write_transaction(), store._connect() as conn:
        conn.execute("UPDATE memories SET embedding=? WHERE id='fact-1'", (blob,))
        store._bump_content_version_in_tx(conn)
        store._invalidate_cache()
    cached = store._get_vector_cache()
    assert cached.matrix is None and "fact-1" in cached.needs_healing_ids
    assert cached.rows_by_id["fact-1"]["embedding_model"] == "needs-reembed"


def test_work_import_savepoints_guarding_and_outer_rollback(caplog):
    store = MeshStore("work-import")
    good = {"id": "work-1", "title": "Valid task", "created_by": "test-agent", "evidence": ["tests/test_production_storage.py"]}
    bad = {"id": "bad", "title": "", "description": "A body that must never appear in logs."}
    with pytest.raises(RuntimeError, match="abort"):
        with store.write_transaction():
            result = store.import_work_items([good, bad], [])
            assert result["imported_items"] == 1 and result["errors"] == 1
            raise RuntimeError("abort")
    assert store.list_work_items() == [] and store.list_mutation_events() == []
    result = store.import_work_items([good, bad], [])
    assert result == {"imported_items": 1, "skipped_items": 0, "imported_events": 0, "errors": 1}
    assert store.get_work_item("work-1")["evidence"] == ["tests/test_production_storage.py"]
    assert bad["description"] not in caplog.text


def test_work_import_io_failure_rolls_back_previously_valid_records(monkeypatch):
    store = MeshStore("import-io")
    original = store._record_mutation_in_tx
    calls = 0
    def record(conn, *args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("synthetic I/O failure")
        original(conn, *args)
    monkeypatch.setattr(store, "_record_mutation_in_tx", record)
    with pytest.raises(sqlite3.OperationalError):
        store.import_work_items([{"id": "first", "title": "First task"}, {"id": "second", "title": "Second task"}], [])
    assert store.list_work_items() == [] and store.list_mutation_events() == []


def test_work_import_events_are_guarded_private_and_not_duplicated():
    store = MeshStore("import-events")
    item = {"id": "work-1", "title": "Imported task", "created_by": "test-agent"}
    fake_token = "ghp_" + "B" * 36
    event = {"item_id": "work-1", "event_type": "updated", "note": fake_token, "agent_id": "test-agent", "timestamp": datetime.now(timezone.utc).isoformat()}
    result = store.import_work_items([item], [event])
    assert result["imported_events"] == 1 and store.get_work_item("work-1")["local_only"]
    assert store.get_work_events("work-1") == []
    assert fake_token not in store.get_work_events("work-1", include_private=True)[0]["note"]
    assert store.import_work_items([item], [event])["skipped_items"] == 1
    assert len(store.get_work_events("work-1", include_private=True)) == 1
    assert store.import_work_items([item], [event], merge_strategy="overwrite")["imported_events"] == 0


def test_encrypted_tasks_and_events_export_decode_all_text_fields(monkeypatch):
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", "42" * 32)  # Obviously fake deterministic test key.
    store = MeshStore("encrypted-work")
    task = store.create_work_item("Encrypted task", description="Encrypted description", tags=["safe"], created_by="test-agent")
    store.complete_work_item(task["id"], "Encrypted summary", evidence=["tests/test_production_storage.py"], note="Encrypted note")
    with store._read_connect() as conn:
        row = conn.execute("SELECT title,description,summary,tags,evidence FROM work_items").fetchone()
        assert all(is_encrypted_text(value) for value in row)
        assert all(is_encrypted_text(row[0]) for row in conn.execute("SELECT note FROM work_events"))
    items, events, total, skipped = store.get_exportable_work_items()
    assert total == 1 and skipped == 0
    assert items[0]["tags"] == ["safe"] and items[0]["evidence"] == ["tests/test_production_storage.py"]
    assert events[-1]["note"] == "Encrypted note"
    assert store.count_matching(tags=["safe"]) == 0  # Task tags never become memory tags.


def test_encrypted_legacy_task_upgrade_has_verified_backup_and_no_plaintext_residual(monkeypatch):
    store = MeshStore("legacy-encryption")
    marker = "LEGACY_PLAINTEXT_TASK_MARKER_FOR_ISOLATED_TEST"
    task = store.create_work_item(marker, description="Legacy description", tags=["legacy"])
    store.complete_work_item(task["id"], "Legacy summary", evidence=["legacy-evidence"], note=marker)
    storage.close_store_connection(store.db_path)
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("UPDATE mesh_meta SET value='3' WHERE key='schema_version'")
        conn.execute("PRAGMA user_version=3")
        conn.commit()
    finally:
        conn.close()
    assert marker.encode() in store.db_path.read_bytes()
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", "42" * 32)
    upgraded = MeshStore("legacy-encryption")
    assert upgraded.get_work_item(task["id"])["title"] == marker
    with upgraded._read_connect() as reader:
        assert scalar(reader, "PRAGMA user_version") == int(SCHEMA_VERSION)
        assert scalar(reader, "SELECT value FROM mesh_meta WHERE key='work_encryption_cleanup_pending'") == "0"
    backups = list(upgraded.db_path.parent.glob("*.backup-v0.7.2-*"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert scalar(backup, "PRAGMA integrity_check") == "ok"
        assert is_encrypted_text(scalar(backup, "SELECT title FROM work_items"))
    finally:
        backup.close()
    for base in (upgraded.db_path, backups[0]):
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(base) + suffix)
            if path.exists():
                assert marker.encode() not in path.read_bytes()


def test_future_store_and_audit_schemas_are_refused_without_downgrade():
    store = MeshStore("future")
    with store._connect() as conn:
        conn.execute(f"PRAGMA user_version={int(SCHEMA_VERSION) + 1}")
    with pytest.raises(RuntimeError, match="newer"):
        MeshStore("future")
    with store._connect() as conn:
        assert scalar(conn, "PRAGMA user_version") == int(SCHEMA_VERSION) + 1
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA user_version=3")
        with pytest.raises(RuntimeError, match="newer"):
            audit._apply_audit_schema(conn)
        assert scalar(conn, "PRAGMA user_version") == 3
    finally:
        conn.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem namespace aliases")
def test_case_alias_cannot_replace_private_memory_in_another_namespace():
    owner = MeshStore("CaseBoundary")
    fact(owner, private=True)
    with pytest.raises(ValueError, match="namespace spelling"):
        MeshStore("caseboundary")
    assert owner.count() == 1 and owner.get("fact-1")["local_only"]


def test_transaction_namespace_binding_survives_constructor_bypass():
    store = MeshStore("bound")
    fact(store, private=True)
    store.namespace = "other"
    with pytest.raises(ValueError, match="namespace spelling"):
        store.upsert("fact-1", "Untrusted replacement.", None, {"source_agent": "other"}, include_private=False)
    with store._read_connect() as conn:
        row = conn.execute("SELECT namespace,local_only FROM memories").fetchone()
        assert tuple(row) == ("bound", 1)


def test_audit_delivery_commits_before_acknowledgement_and_retry_deduplicates(monkeypatch):
    store = MeshStore("audit-delivery")
    fact(store)
    original = store.acknowledge_mutation_events
    def fail_ack(event_ids):
        raise sqlite3.OperationalError("synthetic acknowledgement failure")
    monkeypatch.setattr(store, "acknowledge_mutation_events", fail_ack)
    with pytest.raises(sqlite3.OperationalError):
        audit.deliver_mutation_events(store)
    assert len(store.list_mutation_events()) == 1
    db = audit._connect_audit(store.db_path.parent / "audit_audit-delivery.db")
    assert scalar(db, "SELECT COUNT(*) FROM audit_events") == 1
    monkeypatch.setattr(store, "acknowledge_mutation_events", original)
    assert audit.deliver_mutation_events(store) == 1
    assert store.list_mutation_events() == []
    assert scalar(db, "SELECT COUNT(*) FROM audit_events") == 1
    assert "production storage contract" not in json.dumps([tuple(row) for row in db.execute("SELECT * FROM audit_events")])


def test_failed_audit_commit_retains_outbox_and_active_transactions_cannot_deliver(monkeypatch):
    store = MeshStore("audit-failure")
    fact(store)
    with store.write_transaction(), pytest.raises(RuntimeError, match="after"):
        audit.deliver_mutation_events(store)
    original = audit._persist_audit_entries
    def fail(*args):
        raise sqlite3.OperationalError("synthetic audit commit failure")
    monkeypatch.setattr(audit, "_persist_audit_entries", fail)
    with pytest.raises(sqlite3.OperationalError):
        audit.deliver_mutation_events(store)
    assert len(store.list_mutation_events()) == 1
    monkeypatch.setattr(audit, "_persist_audit_entries", original)
    assert audit.deliver_mutation_events(store) == 1


def test_audit_telemetry_failure_requeues_within_admitted_limits(monkeypatch):
    monkeypatch.setenv("MESH_MAX_AUDIT_QUEUE_EVENTS", "2")
    original = audit._persist_audit_entries
    for index in range(3):
        audit.log_recall(f"Private-free query {index}", "audit-queue", "test-agent", [])
    before = audit.get_audit_queue_stats()
    assert before["pending_events"] == 2 and before["dropped_events"] == 1
    def fail(*args):
        raise sqlite3.OperationalError("untrusted body must not enter diagnostics")
    monkeypatch.setattr(audit, "_persist_audit_entries", fail)
    audit.flush_audit_log("audit-queue")
    failed = audit.get_audit_queue_stats()
    assert failed["pending_events"] == 2 and failed["pending_bytes"] == before["pending_bytes"]
    assert failed["last_error_code"] == "OperationalError"
    monkeypatch.setattr(audit, "_persist_audit_entries", original)
    audit.flush_audit_log("audit-queue")
    assert audit.get_audit_queue_stats()["pending_events"] == 0


def test_audit_queue_and_delivery_keep_same_namespace_roots_separate(tmp_path):
    first_root, second_root = tmp_path / "audit-first", tmp_path / "audit-second"
    with audit.mesh_dir_context(first_root):
        first = MeshStore("same")
        fact(first, "first")
        audit.log_recall("First root query", "same", "first-agent", [])
    with audit.mesh_dir_context(second_root):
        second = MeshStore("same")
        fact(second, "second")
        audit.log_recall("Second root query", "same", "second-agent", [])
        audit.deliver_mutation_events(first)
        audit.flush_audit_log()
    for root, expected, object_id in ((first_root, "first-agent", "first"), (second_root, "second-agent", None)):
        conn = audit._connect_audit(root / "audit_same.db")
        assert scalar(conn, "SELECT agent_id FROM audit_log") == expected
        assert scalar(conn, "SELECT query FROM audit_log") is None
        events = conn.execute("SELECT object_id FROM audit_events").fetchall()
        assert [row[0] for row in events] == ([] if object_id is None else [object_id])
    assert len(second.list_mutation_events()) == 1


def test_new_and_legacy_audit_reads_are_content_free():
    audit.log_recall("Untrusted query body", "body-free", "test-agent", [{"id": "fact", "content": "Untrusted top result", "similarity": .9}])
    audit.flush_audit_log("body-free")
    db = audit._connect_audit(audit._get_audit_db_path("body-free"))
    with db:
        db.execute("INSERT INTO audit_log(timestamp,action,namespace,query,content,top_result) VALUES(?,?,?,?,?,?)", (datetime.now(timezone.utc).isoformat(), "recall", "body-free", "Legacy query body", "Legacy content body", "Legacy top body"))
    entries = audit.get_audit_log(namespace="body-free", limit=10)
    rendered = json.dumps(entries)
    for content in ("Untrusted query body", "Untrusted top result", "Legacy query body", "Legacy content body", "Legacy top body"):
        assert content not in rendered
    assert scalar(db, "SELECT query FROM audit_log ORDER BY id LIMIT 1") is None


def test_bulk_input_limit_refuses_before_any_rows_commit(monkeypatch):
    store = MeshStore("bulk-bound")
    monkeypatch.setenv("MESH_MAX_BULK_ITEMS", "1")
    with pytest.raises(ValueError, match="Bulk item"):
        store.upsert_batch([("one", "First fact", None, {}, False), ("two", "Second fact", None, {}, False)])
    assert store.count() == 0 and store.list_mutation_events() == []


def test_vectorless_memories_export_identically_before_and_after_warming():
    store = MeshStore("export-vectorless")
    fact(store)
    before = store.get_exportable_memories()
    store._get_vector_cache()
    assert store.get_exportable_memories() == before
    assert before[0]["embedding"] is None
