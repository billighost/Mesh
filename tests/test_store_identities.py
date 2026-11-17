"""Database and memory identity prevent stale work across deletion and migration."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from mesh import store as storage
from mesh.sqlite_runtime import sqlite3
from mesh.store import MeshStore, SCHEMA_VERSION, StaleStoreError
from tests.test_production_storage import fact, isolated_storage as isolated_storage, vector


def snapshot(store, memory_id="fact-1"):
    row = store.get(memory_id)
    return {
        "expected_revision": row["revision"],
        "expected_incarnation": row["incarnation"],
        "expected_store_generation": row["store_generation"],
    }


def publish(store, expected, memory_id="fact-1"):
    return store.publish_embedding(memory_id, vector(), embedding_model="onnx-minilm-l6-v2", **expected)


def test_final_query_revalidation_rejects_reused_id_with_same_revision_model_and_flags():
    store = MeshStore("query-aba")
    fact(store)
    old = store.get("fact-1")
    store.delete("fact-1")
    fact(store)
    new = store.get("fact-1")
    assert old["revision"] == new["revision"]
    assert old["embedding_model"] == new["embedding_model"]
    assert old["local_only"] == new["local_only"]
    assert old["incarnation"] != new["incarnation"]
    assert store.revalidate_query_results([old], include_private=False) == []
    assert store.revalidate_query_results([new], include_private=False) == [new]


@pytest.mark.parametrize("key", ["incarnation", "store_generation"])
def test_final_query_revalidation_rejects_obsolete_identity(key):
    store = MeshStore("query-identity")
    fact(store)
    candidate = store.get("fact-1")
    candidate[key] = uuid.uuid4().hex
    assert store.revalidate_query_results([candidate], include_private=False) == []


@pytest.mark.parametrize("key", ["incarnation", "store_generation", "revision"])
def test_final_query_revalidation_requires_complete_identity(key):
    store = MeshStore("query-incomplete")
    fact(store)
    candidate = store.get("fact-1")
    candidate.pop(key)
    assert store.revalidate_query_results([candidate], include_private=False) == []


@pytest.mark.parametrize("padding", [False, True])
def test_other_possible_results_preserve_complete_identity(monkeypatch, padding):
    store = MeshStore("query-alternatives")
    fact(store)
    candidate = store.get("fact-1")
    candidate.update(similarity=0.9, semantic_similarity=0.9, lexical_score=0.9)
    monkeypatch.setattr(store, "hybrid_query", lambda **kwargs: [] if padding else [candidate])
    result = store.discover_other_possible("fact", vector(), set(), limit=1)
    assert len(result) == 1
    for key in ("incarnation", "store_generation", "revision"):
        assert result[0][key] == candidate[key]


def remove_test_database(store):
    storage.close_store_readers(store.db_path)
    storage.close_store_connection(store.db_path)
    paths = [store.db_path.with_name(store.db_path.name + suffix) for suffix in ("", "-wal", "-shm")]
    print("Removing isolated test database files:", [path.name for path in paths])
    for path in paths:
        assert path.resolve().parent == store.db_path.parent.resolve()
        path.unlink(missing_ok=True)


def test_delete_reinsert_with_same_revision_rejects_old_publication_enqueue_and_retry():
    store = MeshStore("memory-aba")
    fact(store)
    expected = snapshot(store)
    store.delete("fact-1")
    fact(store, private=True)
    replacement = store.get("fact-1")
    assert replacement["revision"] == expected["expected_revision"]
    assert replacement["incarnation"] != expected["expected_incarnation"]
    assert not publish(store, expected)
    assert not store.enqueue_embedding_job("fact-1", **expected)
    assert not store.retry_embedding_job("fact-1", expected_target_model="onnx-minilm-l6-v2", **expected)
    job = store.list_embedding_jobs()[0]
    assert job["incarnation"] == replacement["incarnation"] and job["attempts"] == 0
    assert store.get("fact-1", include_private=False) is None


@pytest.mark.parametrize("operation", ["update", "delete", "approve"])
def test_prepared_crud_rejects_replacement_incarnation(operation):
    store = MeshStore("crud-aba")
    fact(store)
    expected = snapshot(store)
    store.delete("fact-1")
    fact(store)
    before = store.get("fact-1")
    with pytest.raises(ValueError, match="incarnation"):
        if operation == "update":
            store.update("fact-1", content="An obsolete prepared edit.", **expected)
        else:
            getattr(store, operation)("fact-1", **expected)
    assert store.get("fact-1") == before


def test_job_retargeting_refuses_old_model_publication_and_retry():
    store = MeshStore("retarget")
    fact(store)
    expected = snapshot(store)
    assert store.enqueue_embedding_job("fact-1", target_model="future-local-model", **expected)
    assert not publish(store, expected)
    assert not store.retry_embedding_job("fact-1", expected_target_model="onnx-minilm-l6-v2", **expected)
    job = store.list_embedding_jobs()[0]
    assert job["target_model"] == "future-local-model" and job["attempts"] == 0


def test_publication_requires_live_job_and_duplicate_completion_cannot_overwrite_vector():
    store = MeshStore("completed-job")
    fact(store)
    expected = snapshot(store)
    assert publish(store, expected)
    with store._read_connect() as conn:
        before = tuple(conn.execute("SELECT embedding,embedding_model FROM memories").fetchone())
    other_vector = [0.0, 1.0] + [0.0] * 382
    assert not store.publish_embedding("fact-1", other_vector, embedding_model="onnx-minilm-l6-v2", **expected)
    with store._read_connect() as conn:
        assert tuple(conn.execute("SELECT embedding,embedding_model FROM memories").fetchone()) == before
    assert store.list_embedding_jobs() == []


def test_incarnations_are_preserved_on_update_and_batch_replace_but_ignored_in_import_metadata():
    store = MeshStore("identity-upserts")
    fact(store)
    first = store.get("fact-1")
    store._get_vector_cache()
    store.update("fact-1", tags=["updated"])
    assert store.get("fact-1")["incarnation"] == first["incarnation"]
    fake_incarnation = uuid.uuid4().hex
    store.upsert_batch([
        ("fact-1", "Updated durable fact.", None, {"incarnation": fake_incarnation}, False),
        ("new", "New durable fact.", None, {"incarnation": first["incarnation"]}, False),
    ])
    assert store.get("fact-1")["incarnation"] == first["incarnation"]
    assert store.get("new")["incarnation"] not in (first["incarnation"], fake_incarnation)
    cache = store._get_vector_cache()
    assert cache.rows_by_id["fact-1"]["incarnation"] == first["incarnation"]
    assert cache.rows_by_id["new"]["store_generation"] == store.store_generation


def test_store_generation_is_durable_distinct_and_cannot_be_rebound():
    first = MeshStore("generation-one")
    second = MeshStore("generation-two")
    assert first.store_generation != second.store_generation
    storage.close_store_readers(first.db_path)
    storage.close_store_connection(first.db_path)
    reopened = MeshStore("generation-one", create=False)
    assert reopened.store_generation == first.store_generation
    with pytest.raises(AttributeError):
        first.store_generation = second.store_generation


def test_deleted_store_instance_refuses_recreation_before_opening_a_writer(monkeypatch):
    old = MeshStore("deleted-store")
    fact(old)
    remove_test_database(old)
    original = storage.sqlite3.connect
    calls = []

    def observe(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(storage.sqlite3, "connect", observe)
    with pytest.raises(StaleStoreError):
        fact(old, "obsolete-write")
    with pytest.raises(StaleStoreError):
        old.delete("fact-1")
    assert calls == [] and not old.db_path.exists()


def test_recreated_database_rejects_old_identity_even_if_memory_id_revision_match():
    old = MeshStore("database-aba")
    fact(old)
    expected = snapshot(old)
    remove_test_database(old)
    current = MeshStore("database-aba")
    fact(current)
    assert current.store_generation != old.store_generation
    assert current.get("fact-1")["revision"] == expected["expected_revision"]
    assert not publish(current, expected)
    with pytest.raises(StaleStoreError):
        old.create_work_item("Obsolete task", created_by="test-agent")
    with pytest.raises(StaleStoreError):
        old.update("fact-1", content="Obsolete edit")
    assert len(current.list_embedding_jobs()) == 1 and current.list_work_items() == []


def test_generation_rotation_at_same_file_identity_refuses_old_logical_writes():
    old = MeshStore("rotated-generation")
    fact(old)
    expected = snapshot(old)
    with old._connect() as conn:
        conn.execute("UPDATE mesh_meta SET value=? WHERE key='store_generation'", (uuid.uuid4().hex,))
    with pytest.raises(StaleStoreError):
        old.execute_operation("old-operation", "test-agent", "learn", "a" * 64, lambda: fact(old, "old"))
    current = MeshStore("rotated-generation", create=False)
    assert not publish(current, expected)
    assert current.get_operation_receipt("old-operation", "test-agent") is None
    assert current.count() == 1


def test_old_access_buffer_is_dropped_without_replaying_queries_into_recreated_database():
    old = MeshStore("tracking-generation")
    fact(old)
    old.record_recall_access(["fact-1"], "Old generation query marker", "test-agent", .8, 1)
    assert storage.get_access_tracking_stats()["pending_entries"] == 2
    remove_test_database(old)
    current = MeshStore("tracking-generation")
    fact(current)
    assert current.get("fact-1")["access_count"] == 0
    storage._flush_access_tracking_for_db_path(current.db_path)
    with current._read_connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 0
        assert conn.execute("SELECT access_count FROM memories").fetchone()[0] == 0
    stats = storage.get_access_tracking_stats()
    assert stats["pending_entries"] == 0 and stats["dropped_entries"] == 2


def test_file_identity_change_before_commit_rolls_back_content_jobs_receipts_and_callbacks(monkeypatch):
    store = MeshStore("commit-identity")
    callbacks = []
    original = storage._file_identity
    with pytest.raises(StaleStoreError):
        with store.write_transaction():
            fact(store)
            store.after_commit(lambda: callbacks.append(True))
            store.execute_operation("operation", "test-agent", "learn", "a" * 64, lambda: {"memory_id": "fact-1"})
            monkeypatch.setattr(storage, "_file_identity", lambda path: (0, 0) if path == store.db_path else original(path))
    monkeypatch.setattr(storage, "_file_identity", original)
    assert callbacks == [] and store.count() == 0
    assert store.list_embedding_jobs() == [] and store.list_mutation_events() == []
    assert store.get_operation_receipt("operation", "test-agent") is None


def test_rolled_back_incarnation_is_not_reused_and_receipts_keep_safe_identity_fields():
    store = MeshStore("rollback-identities")
    discarded = None
    with pytest.raises(RuntimeError, match="abort"):
        with store.write_transaction():
            fact(store)
            discarded = store.get("fact-1")["incarnation"]
            raise RuntimeError("abort")
    fact(store)
    row = store.get("fact-1")
    assert row["incarnation"] != discarded
    result = {"memory_id": row["id"], "revision": row["revision"], "incarnation": row["incarnation"], "store_generation": row["store_generation"], "content": "Untrusted result body"}
    store.execute_operation("identity-receipt", "test-agent", "learn", "a" * 64, lambda: result)
    receipt = store.get_operation_receipt("identity-receipt", "test-agent")["result"]
    assert receipt["incarnation"] == row["incarnation"] and receipt["store_generation"] == store.store_generation
    assert "content" not in receipt


def legacy_database(root, version):
    path = root / f"mesh_legacy-{version}.db"
    conn = sqlite3.connect(path)
    now = datetime.now(timezone.utc).isoformat()
    try:
        with conn:
            conn.execute(f"PRAGMA user_version={version}")
            conn.execute("CREATE TABLE mesh_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.executemany("INSERT INTO mesh_meta VALUES(?,?)", [("schema_version", str(version)), ("content_version", "7"), ("namespace_owner", f"legacy-{version}")])
            conn.execute("""CREATE TABLE memories(
                id TEXT PRIMARY KEY, namespace TEXT NOT NULL, content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1, memory_type TEXT NOT NULL DEFAULT 'fact',
                source_agent TEXT NOT NULL DEFAULT 'test-agent', tags TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL, updated_at TEXT, accessed_at TEXT NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0, ttl_days REAL, decay_start_at TEXT,
                local_only INTEGER NOT NULL DEFAULT 0, embedding BLOB,
                embedding_model TEXT NOT NULL DEFAULT 'needs-reembed', trust_tier TEXT NOT NULL DEFAULT 'user',
                quarantined INTEGER NOT NULL DEFAULT 0, secret_ref TEXT, revision INTEGER NOT NULL DEFAULT 1
            )""")
            conn.executemany(
                "INSERT INTO memories(id,namespace,content,created_at,updated_at,accessed_at,local_only) VALUES(?,?,?,?,?,?,?)",
                [(f"legacy-{privacy}", f"legacy-{version}", "Legacy durable fact.", now, now, now, privacy) for privacy in (0, 1)],
            )
            if version == 4:
                conn.execute("CREATE TABLE embedding_jobs(memory_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,revision INTEGER NOT NULL,target_model TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,available_at TEXT NOT NULL,created_at TEXT NOT NULL,error_code TEXT)")
                conn.execute("INSERT INTO embedding_jobs VALUES(?,?,?,?,?,?,?,?)", ("legacy-0", "legacy-4", 1, "onnx-minilm-l6-v2", 3, "2099-01-01T00:00:00+00:00", now, "embedding_failed"))
    finally:
        conn.close()
    return path


@pytest.mark.parametrize("version", [3, 4])
def test_schema_upgrade_assigns_unique_identities_preserves_privacy_backoff_and_verified_backup(isolated_storage, version):
    root = isolated_storage / "mesh"
    path = legacy_database(root, version)
    upgraded = MeshStore(f"legacy-{version}", create=False)
    rows = [upgraded.get(f"legacy-{privacy}") for privacy in (0, 1)]
    assert len({row["incarnation"] for row in rows}) == 2
    assert all(row["store_generation"] == upgraded.store_generation for row in rows)
    assert upgraded.get("legacy-1", include_private=False) is None
    with upgraded._read_connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == int(SCHEMA_VERSION)
        jobs = conn.execute("SELECT memory_id,incarnation,attempts,available_at FROM embedding_jobs").fetchall()
        assert {row[0]: row[1] for row in jobs} == {row["id"]: row["incarnation"] for row in rows}
        if version == 4:
            first = next(row for row in jobs if row[0] == "legacy-0")
            assert first[2:] == (3, "2099-01-01T00:00:00+00:00")
    backups = list(root.glob(path.name + ".backup-*"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == version
        assert "incarnation" not in {row[1] for row in backup.execute("PRAGMA table_info(memories)")}
    finally:
        backup.close()


@pytest.mark.parametrize("field,value", [
    ("expected_revision", True), ("expected_revision", 1.5),
    ("expected_incarnation", "missing"), ("expected_incarnation", "F" * 32),
    ("expected_store_generation", None), ("expected_store_generation", "wrong"),
])
def test_invalid_identity_arguments_are_rejected_before_job_changes(field, value):
    store = MeshStore("invalid-identities")
    fact(store)
    expected = snapshot(store)
    expected[field] = value
    with pytest.raises(ValueError):
        publish(store, expected)
    assert len(store.list_embedding_jobs()) == 1


def test_conflict_snapshot_rejects_recreated_existing_candidate():
    store = MeshStore("conflict-aba")
    fact(store, "existing")
    old = store.get("existing")
    store.delete("existing")
    fact(store, "existing")
    with pytest.raises(ValueError, match="candidate changed"):
        store.upsert("new", "A conflicting durable fact.", None, {}, conflict={"existing_memory_id": "existing", "existing_revision": 1, "existing_incarnation": old["incarnation"], "similarity": .9})
    assert store.get("new") is None and store.count() == 1
