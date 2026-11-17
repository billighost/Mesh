"""Real integration boundaries for namespace maintenance and memory incarnation CAS."""
from __future__ import annotations

import json
import uuid

import pytest

from mesh import audit, memory, namespaces, operations, store
from mesh.client import ConnectionProfile, MeshClient
from mesh.sqlite_runtime import sqlite3


@pytest.fixture(autouse=True)
def isolated_lifecycle(tmp_path, monkeypatch):
    root = tmp_path / "mesh"
    root.mkdir()
    monkeypatch.setenv("MESH_HOME", str(root))
    for name in ("MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_NO_OUTBOX_WORKER", "MESH_FORCE_HASH_EMBED"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    for name in ("MESH_LOCKED_NAMESPACE", "MESH_NAMESPACE", "MESH_ENCRYPT_AT_REST", "MESH_ENCRYPTION_KEY", "MESH_PASSPHRASE", "MESH_READ_ONLY", "MESH_AUTHENTICATED_AGENT_ID", "MESH_AGENT_SCOPES"):
        monkeypatch.delenv(name, raising=False)
    audit.stop_audit_worker()
    yield root
    operations.evict_mesh_cache()
    audit.stop_audit_worker()
    store._close_all_pooled_connections()
    audit._close_all_audit_connections()


def dense_vector():
    return [1.0, *([0.0] * 383)]


def replace_same_id(mesh, memory_id):
    assert mesh.store.delete(memory_id)
    mesh.store.upsert(memory_id, "Replacement configuration uses the birch queue.", dense_vector(), {"embedding_model": "onnx-minilm-l6-v2", "source_agent": mesh.agent_id})
    return mesh.store.get(memory_id)


@pytest.mark.parametrize("action", ["mesh_update", "mesh_approve", "mesh_forget"])
def test_public_mutations_forward_revision_guards_and_record_receipts(isolated_lifecycle, action):
    client = MeshClient(ConnectionProfile("actions", isolated_lifecycle, agent_id="actor"), transport="sdk")
    learned = client.call("mesh_learn", {"content": "The cedar queue uses two workers.", "quarantined": action == "mesh_approve"}, "seed-operation")
    arguments = {"memory_id": learned["memory_id"], "expected_revision": learned["revision"]}
    if action == "mesh_update":
        arguments["content"] = "The cedar queue uses three workers."
    result = client.call(action, arguments, "change-operation")
    assert result["status"] == {"mesh_update": "updated", "mesh_approve": "approved", "mesh_forget": "deleted"}[action]
    assert result["receipt"]["operation_id"] == "change-operation"
    replay = client.call(action, arguments, "change-operation")
    assert replay["replayed"] is True
    assert "cedar queue" not in json.dumps(replay["receipt"])


def test_sdk_brief_pagination_reaches_facts_through_public_wrapper():
    mesh = memory.Mesh("brief-pages")
    mesh.learn_many(["Cedar listens on port 7443.", "Birch listens on port 7554."], deduplicate=False)
    first = mesh.project_brief(max_tokens=500, max_facts=1)
    second = mesh.project_brief(max_tokens=500, max_facts=1, fact_offset=first["next_fact_offset"])
    assert first["facts_included"] == second["facts_included"] == 1
    assert first["facts_available"] == second["facts_available"] == 2
    assert first["selection_revision"] == second["selection_revision"]
    assert first["brief_markdown"] != second["brief_markdown"]


@pytest.mark.parametrize("page_size", [1, 2, 3, 5])
def test_brief_mixed_type_pages_cover_every_fact_once(page_size):
    mesh = memory.Mesh("mixed-brief-pages")
    expected = set()
    for kind, count in (("decision", 2), ("process", 3), ("preference", 1), ("fact", 4)):
        for index in range(count):
            result = mesh.learn(f"The {kind} item {index} belongs to this project because it is durable.",
                                memory_type=kind, deduplicate=False)
            expected.add(result["memory_id"])
    offset = 0
    seen = []
    revisions = set()
    while True:
        page = mesh.project_brief(max_tokens=2000, max_facts=page_size, fact_offset=offset)
        revisions.add(page["selection_revision"])
        seen.extend(page["selected_memory_ids"])
        if page["next_fact_offset"] is None:
            break
        assert page["next_fact_offset"] > offset
        offset = page["next_fact_offset"]
    assert len(revisions) == 1
    assert set(seen) == expected and len(seen) == len(expected)


def test_brief_selection_revision_changes_with_eligibility_generation_and_visibility(monkeypatch):
    from datetime import datetime, timedelta, timezone

    mesh = memory.Mesh("brief-revision")
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(store, "_utc_now", lambda: moment)
    mesh.learn("The temporary cedar endpoint is available.", ttl_days=1)
    before = mesh.project_brief(max_tokens=500, include_private=False)
    monkeypatch.setattr(store, "_utc_now", lambda: moment + timedelta(days=2))
    expired = mesh.project_brief(max_tokens=500, include_private=False)
    assert before["facts_available"] == 1 and expired["facts_available"] == 0
    assert before["selection_revision"] != expired["selection_revision"]
    assert expired["selection_revision"] != mesh.project_brief(max_tokens=500, include_private=True)["selection_revision"]
    connection = sqlite3.connect(mesh.store.db_path)
    try:
        connection.execute("UPDATE mesh_meta SET value=? WHERE key='store_generation'", (uuid.uuid4().hex,))
        connection.commit()
    finally:
        connection.close()
    reopened = memory.Mesh("brief-revision")
    assert reopened.project_brief(max_tokens=500)["selection_revision"] != expired["selection_revision"]


def test_namespace_rename_preserves_tasks_jobs_receipts_and_visibility(isolated_lifecycle):
    client = MeshClient(ConnectionProfile("before", isolated_lifecycle, agent_id="actor"), transport="sdk")
    learned = client.call("mesh_learn", {"content": "The cedar service listens on port 7443."}, "learn-original")
    original = store.MeshStore("before", create=False)
    private = original.create_work_item("Private task title", local_only=True)
    public = original.create_work_item("Public task title")
    old_generation = original.store_generation
    assert namespaces.rename_namespace("before", "after") == 1
    renamed = store.MeshStore("after", create=False)
    assert renamed.store_generation != old_generation
    row = renamed.get(learned["memory_id"])
    assert row["incarnation"] == learned["incarnation"]
    assert row["namespace"] == "after"
    assert renamed.get_operation_receipt("learn-original", "local-user") is not None
    assert renamed.list_embedding_jobs()[0]["memory_id"] == learned["memory_id"]
    assert {item["id"] for item in renamed.list_work_items(include_private=False)} == {public["id"]}
    assert renamed.get_work_item(private["id"], include_private=True)["local_only"]
    with renamed._read_connect() as connection:
        for table in ("memories", "work_items", "work_events", "embedding_jobs", "mutation_outbox", "operation_receipts"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table} WHERE namespace='before'").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert not original.db_path.exists()
    with pytest.raises(store.StaleStoreError):
        original.upsert("old-writer", "A stale writer must not recreate the old namespace.", None, {"embedding_model": "needs-reembed"})
    assert not original.db_path.exists()


def test_namespace_rebind_failure_rolls_back_every_table(monkeypatch):
    original = store.MeshStore("rollback")
    original.upsert("fact", "The cedar service uses a bounded queue.", None, {"embedding_model": "needs-reembed"})
    original.create_work_item("A task that must retain its namespace")
    with original._connect() as connection:
        connection.execute("CREATE TRIGGER refuse_rebind BEFORE UPDATE ON work_items BEGIN SELECT RAISE(ABORT,'isolated rollback'); END")
        with pytest.raises(sqlite3.IntegrityError):
            namespaces._rebind_namespace_database(connection, "rollback", "new")
        assert connection.execute("SELECT namespace FROM memories").fetchone()[0] == "rollback"
        assert connection.execute("SELECT value FROM mesh_meta WHERE key='namespace_owner'").fetchone()[0] == "rollback"


def test_prepared_update_rejects_delete_reinsert_even_at_the_same_revision():
    mesh = memory.Mesh("prepared-aba")
    result = mesh.learn("The cedar queue uses two workers.")
    operation = mesh.prepare_update(result["memory_id"], content="A stale prepared update.")
    replacement = replace_same_id(mesh, result["memory_id"])
    assert replacement["revision"] == result["revision"]
    with pytest.raises(ValueError, match="incarnation|changed"):
        mesh.commit_prepared_update(operation)
    assert mesh.get(result["memory_id"])["content"] == replacement["content"]


def test_wait_detects_delete_reinsert_during_its_first_snapshot(monkeypatch):
    mesh = memory.Mesh("wait-aba")
    result = mesh.learn("The cedar queue uses two workers.")
    original = mesh.store.get_indexing_status
    calls = 0

    def status(ids, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            replace_same_id(mesh, result["memory_id"])
        return original(ids, **kwargs)

    monkeypatch.setattr(mesh.store, "get_indexing_status", status)
    waited = mesh.wait_for_indexed(result["memory_id"], timeout=0)
    assert waited["status"] == "superseded"
    assert waited["indexed"] == []


@pytest.mark.parametrize("bulk", [False, True])
def test_learn_wait_uses_the_committed_incarnation_after_a_concurrent_replacement(monkeypatch, bulk):
    mesh = memory.Mesh("learn-wait-aba")
    original = mesh.wait_for_indexed

    def replace_before_wait(ids, **kwargs):
        memory_id = ids[0] if isinstance(ids, list) else ids
        replace_same_id(mesh, memory_id)
        return original(ids, **kwargs)

    monkeypatch.setattr(mesh, "wait_for_indexed", replace_before_wait)
    if bulk:
        result = mesh.learn_many(["The cedar queue uses two workers."], wait_for_indexed=True)[0]
    else:
        result = mesh.learn("The cedar queue uses two workers.", wait_for_indexed=True)
    assert result["indexing_wait"]["status"] == "superseded"
    assert result["indexing_state"] == "pending"


def test_client_refreshes_a_cached_instance_after_namespace_recreation(isolated_lifecycle):
    client = MeshClient(ConnectionProfile("recreated", isolated_lifecycle, agent_id="actor"), transport="sdk")
    old = client.call("mesh_learn", {"content": "The old cedar configuration."}, "old-operation")
    cached = operations._get_mesh("recreated", "actor")
    # Simulate another process replacing the file without local registry eviction.
    store.close_store_readers(cached.store.db_path)
    store.close_store_connection(cached.store.db_path)
    cached.store.db_path.unlink()
    replacement = memory.Mesh("recreated")
    fresh = replacement.learn("The new birch configuration.")
    response = client.call("mesh_inspect", {"memory_id": fresh["memory_id"]})
    assert [row["id"] for row in response["memories"]] == [fresh["memory_id"]]
    assert response["memories"][0]["id"] != old["memory_id"]


def test_client_refreshes_a_cached_instance_after_in_place_generation_rotation(isolated_lifecycle):
    client = MeshClient(ConnectionProfile("rotated", isolated_lifecycle, agent_id="actor"), transport="sdk")
    old = client.call("mesh_learn", {"content": "The cedar service configuration."}, "old-operation")
    cached = operations._get_mesh("rotated", "actor")
    new_generation = uuid.uuid4().hex
    connection = sqlite3.connect(cached.store.db_path)
    try:
        connection.execute("UPDATE mesh_meta SET value=? WHERE key='store_generation'", (new_generation,))
        connection.commit()
    finally:
        connection.close()
    response = client.call("mesh_inspect", {"memory_id": old["memory_id"]})
    assert response["memories"][0]["store_generation"] == new_generation
    assert operations._get_mesh("rotated", "actor") is not cached
