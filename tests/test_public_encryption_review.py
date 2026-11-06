"""Public encryption must protect task text and refuse incomplete physical cleanup."""
from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from mesh import audit, crypto
from mesh import store as storage
from mesh.sqlite_runtime import sqlite3
from mesh.store import MeshStore


WORK_FIELDS = ("title", "description", "summary", "tags", "evidence")
CANARY = "PublicTaskEncryptionCanaryForIsolatedReview"
TEST_KEY = bytes.fromhex("42" * 32)


@pytest.mark.parametrize("target", ["memory_header", "memory_metadata", "audit_header"])
def test_public_encryption_refuses_future_schemas_before_transforming_either_database(monkeypatch, target):
    store, _ = create_task()
    audit_path = create_legacy_audit(store)
    path = audit_path if target == "audit_header" else store.db_path
    with contextlib.closing(sqlite3.connect(path)) as conn:
        if target == "memory_metadata":
            conn.execute("UPDATE mesh_meta SET value=? WHERE key='schema_version'", (str(int(storage.SCHEMA_VERSION) + 1),))
        else:
            ceiling = audit.AUDIT_SCHEMA_VERSION if target == "audit_header" else int(storage.SCHEMA_VERSION)
            conn.execute(f"PRAGMA user_version={ceiling + 1}")
        conn.commit()
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", TEST_KEY.hex())
    with pytest.raises(RuntimeError, match="newer"):
        crypto.encrypt_namespace_db(store.namespace)
    fields, notes, pending, encrypted = raw_work(store.db_path)
    assert all(not crypto.is_encrypted_text(value) for value in fields)
    assert all(not crypto.is_encrypted_text(value) for value in notes)
    assert pending is None and encrypted is None
    with contextlib.closing(sqlite3.connect(audit_path)) as conn:
        assert all(not crypto.is_encrypted_text(value) for value in conn.execute("SELECT query,content,top_result FROM audit_log").fetchone())
    assert not list((store.db_path.parent / "backups").glob("*.bak.*"))


@pytest.fixture(autouse=True)
def isolated_encryption(tmp_path, monkeypatch):
    for name, child in {
        "MESH_HOME": "mesh", "MESH_DIR": "mesh", "HOME": "home", "USERPROFILE": "home",
        "APPDATA": "appdata", "LOCALAPPDATA": "localappdata", "XDG_CONFIG_HOME": "xdg",
    }.items():
        target = tmp_path / child
        target.mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(target))
    for name in (
        "MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK",
        "MESH_NO_OUTBOX_WORKER", "MESH_FORCE_HASH_EMBED",
    ):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    monkeypatch.setenv("MESH_KEYRING_BACKEND", "local")
    for name in ("MESH_ENCRYPT_AT_REST", "MESH_ENCRYPTION_KEY", "MESH_PASSPHRASE", "MESH_LOCKED_NAMESPACE", "MESH_NAMESPACE"):
        monkeypatch.delenv(name, raising=False)
    crypto._invalidate_enc_memo()
    audit.stop_audit_worker()
    monkeypatch.setattr(audit, "_ensure_audit_drain_thread", lambda: None)
    yield tmp_path
    audit.stop_audit_worker()
    audit.flush_audit_log()
    storage._close_all_pooled_connections()
    audit._close_all_audit_connections()
    storage._cleanup_open_lock_fds()
    storage._VECTOR_CACHE.clear()


def create_task(namespace="public-encryption"):
    store = MeshStore(namespace)
    task = store.create_work_item(CANARY, description=CANARY + " description", tags=["public-canary"], created_by="test-agent")
    store.complete_work_item(task["id"], CANARY + " summary", evidence=["tests/public-canary.py"], note=CANARY + " event", agent_id="test-agent")
    return store, task


def raw_work(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        row = conn.execute("SELECT title,description,summary,tags,evidence FROM work_items").fetchone()
        notes = [entry[0] for entry in conn.execute("SELECT note FROM work_events ORDER BY id")]
        pending = conn.execute("SELECT value FROM mesh_meta WHERE key='work_encryption_cleanup_pending'").fetchone()
        encrypted = conn.execute("SELECT value FROM mesh_meta WHERE key='encrypted_at_rest'").fetchone()
        return tuple(row), notes, pending[0] if pending else None, encrypted[0] if encrypted else None


def assert_no_canary(path):
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        if target.exists():
            assert CANARY.encode() not in target.read_bytes(), suffix


def create_legacy_audit(store, *, owner=None, event_owner=None):
    path = audit._get_audit_db_path(store.namespace)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL").fetchall()
        conn.execute("CREATE TABLE audit_log(id INTEGER PRIMARY KEY,namespace TEXT,query TEXT,content TEXT,top_result TEXT)")
        conn.execute("INSERT INTO audit_log VALUES(1,?,?,?,?)", (owner or store.namespace, CANARY + " query", CANARY + " content", CANARY + " result"))
        if event_owner is not None:
            conn.execute("CREATE TABLE audit_events(id INTEGER PRIMARY KEY,namespace TEXT)")
            conn.execute("INSERT INTO audit_events VALUES(1,?)", (event_owner,))
        conn.commit()
    return path


def use_short_busy_timeout(monkeypatch):
    original = sqlite3.connect

    def short_connect(database, *args, **kwargs):
        kwargs["timeout"] = 0.02
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", short_connect)
    return original


def test_public_migration_encrypts_tasks_and_events_before_any_store_reopen(monkeypatch):
    store, task = create_task()
    expected, expected_notes, _, _ = raw_work(store.db_path)
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", TEST_KEY.hex())
    result = crypto.encrypt_namespace_db(store.namespace, keep_backup=True)
    assert result["encrypted"] is True
    row, notes, pending, encrypted = raw_work(store.db_path)
    assert all(crypto.is_encrypted_text(value) for value in row)
    assert all(crypto.is_encrypted_text(value) for value in notes)
    assert tuple(crypto.decrypt_text_if_encrypted(value, key=TEST_KEY) for value in row) == expected
    assert [crypto.decrypt_text_if_encrypted(value, key=TEST_KEY) for value in notes] == expected_notes
    assert pending == "0" and encrypted == "1"
    assert_no_canary(store.db_path)
    assert len(result["backups"]) == 1
    with contextlib.closing(sqlite3.connect(result["backups"][0])) as backup:
        assert backup.execute("SELECT title FROM work_items WHERE id=?", (task["id"],)).fetchone()[0] == CANARY
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert CANARY.encode() in Path(result["backups"][0]).read_bytes()
    # Exercise the normal read/export contract only after the immediate raw checks.
    items, events, total, skipped = MeshStore(store.namespace, create=False).get_exportable_work_items()
    assert total == 1 and skipped == 0 and items[0]["title"] == CANARY
    assert items[0]["tags"] == ["public-canary"]
    assert events[-1]["note"] == CANARY + " event"


def test_public_migration_uses_explicit_passphrase_for_task_and_memory_fields(monkeypatch):
    store, _ = create_task("explicit-migration-key")
    store.upsert("memory-canary", CANARY + " memory", None, {"source_agent": "test-agent"})
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", "24" * 32)
    passphrase = "obviously-fake-isolated-migration-passphrase"
    crypto.encrypt_namespace_db(store.namespace, passphrase=passphrase, keep_backup=False)
    explicit_key = crypto.get_active_master_key(passphrase=passphrase)
    assert explicit_key != crypto.get_active_master_key()
    row, notes, pending, _ = raw_work(store.db_path)
    assert crypto.decrypt_text_if_encrypted(row[0], key=explicit_key) == CANARY
    assert json.loads(crypto.decrypt_text_if_encrypted(row[3], key=explicit_key)) == ["public-canary"]
    assert all(crypto.is_encrypted_text(value) for value in (*row, *notes))
    with contextlib.closing(sqlite3.connect(store.db_path)) as conn:
        memory = conn.execute("SELECT content FROM memories WHERE id='memory-canary'").fetchone()[0]
        assert crypto.decrypt_text_if_encrypted(memory, key=explicit_key) == CANARY + " memory"
    assert pending == "0"
    assert not list((store.db_path.parent / "backups").glob("*.bak.*"))


def test_work_transform_preserves_partial_legacy_schema_nulls_and_existing_ciphertext():
    assert not crypto.is_encryption_enabled()
    existing = crypto.encrypt_text_for_storage("Already protected", key=TEST_KEY)
    with contextlib.closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE work_items(id TEXT PRIMARY KEY,namespace TEXT,title TEXT,summary TEXT,priority INTEGER)")
        conn.execute("CREATE TABLE work_events(id INTEGER PRIMARY KEY,note TEXT,event_type TEXT)")
        conn.executemany("INSERT INTO work_items VALUES(?, 'legacy', ?, NULL, 2)", [(f"task-{number}", f"Legacy title {number}") for number in range(130)])
        conn.execute("INSERT INTO work_items VALUES('protected','legacy',?,NULL,1)", (existing,))
        conn.executemany("INSERT INTO work_events VALUES(?,?, 'created')", [(1, None), (2, "Legacy note"), (3, existing)])
        conn.commit()
        schema = conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
        conn.execute("BEGIN IMMEDIATE")
        assert MeshStore._encrypt_work_tables_in_tx(conn, key=TEST_KEY)
        conn.commit()
        rows = conn.execute("SELECT id,title,summary,priority FROM work_items ORDER BY id").fetchall()
        assert len(rows) == 131
        assert all(crypto.is_encrypted_text(row[1]) and row[2] is None for row in rows)
        assert conn.execute("SELECT title FROM work_items WHERE id='protected'").fetchone()[0] == existing
        assert conn.execute("SELECT note FROM work_events WHERE id=1").fetchone()[0] is None
        assert conn.execute("SELECT note FROM work_events WHERE id=3").fetchone()[0] == existing
        assert crypto.decrypt_text_if_encrypted(conn.execute("SELECT note FROM work_events WHERE id=2").fetchone()[0], key=TEST_KEY) == "Legacy note"
        encrypted_rows = conn.execute("SELECT * FROM work_items ORDER BY id").fetchall()
        conn.execute("BEGIN IMMEDIATE")
        assert not MeshStore._encrypt_work_tables_in_tx(conn, key=TEST_KEY)
        conn.commit()
        assert conn.execute("SELECT * FROM work_items ORDER BY id").fetchall() == encrypted_rows
        assert conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == schema


def test_task_transform_failure_rolls_back_memories_tasks_and_encryption_marker(monkeypatch):
    store, _ = create_task("migration-rollback")
    store.upsert("memory-canary", CANARY + " memory", None, {"source_agent": "test-agent"})
    expected, expected_notes, _, old_encrypted = raw_work(store.db_path)
    original = MeshStore._encrypt_work_tables_in_tx

    def fail_after_transform(conn, **kwargs):
        original(conn, **kwargs)
        raise RuntimeError("injected task encryption failure")

    monkeypatch.setattr(MeshStore, "_encrypt_work_tables_in_tx", staticmethod(fail_after_transform))
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", TEST_KEY.hex())
    with pytest.raises(RuntimeError, match="injected task encryption failure"):
        crypto.encrypt_namespace_db(store.namespace)
    row, notes, pending, encrypted = raw_work(store.db_path)
    assert row == expected and notes == expected_notes and pending is None and encrypted == old_encrypted
    with contextlib.closing(sqlite3.connect(store.db_path)) as conn:
        assert conn.execute("SELECT content FROM memories WHERE id='memory-canary'").fetchone()[0] == CANARY + " memory"
        assert conn.execute("SELECT content FROM memories_fts WHERE id='memory-canary'").fetchone()[0] == CANARY + " memory"


@pytest.mark.parametrize("database", ["main", "audit"])
def test_active_reader_prevents_migration_before_backups_and_encrypted_rows(monkeypatch, database):
    store, _ = create_task("busy-preflight")
    path = create_legacy_audit(store) if database == "audit" else store.db_path
    table, field = ("audit_log", "content") if database == "audit" else ("work_items", "description")
    storage.close_store_connection(store.db_path)
    audit.evict_audit_cache(store.namespace)
    original_connect = use_short_busy_timeout(monkeypatch)
    reader = original_connect(path, timeout=0.02)
    writer = original_connect(path, timeout=0.02)
    try:
        reader.execute("BEGIN")
        reader.execute(f"SELECT {field} FROM {table}").fetchall()
        writer.execute(f"UPDATE {table} SET {field}=?", (CANARY + " pending frame",))
        writer.commit()
        assert Path(str(path) + "-wal").stat().st_size > 0
        monkeypatch.setenv("MESH_ENCRYPTION_KEY", TEST_KEY.hex())
        with pytest.raises(RuntimeError, match="cannot begin.*active readers"):
            crypto.encrypt_namespace_db(store.namespace)
        row, notes, pending, encrypted = raw_work(store.db_path)
        assert not any(crypto.is_encrypted_text(value) for value in (*row, *notes))
        assert pending is None and encrypted != "1"
        assert not list((store.db_path.parent / "backups").glob("*.bak.*"))
    finally:
        reader.rollback()
        reader.close()
        writer.close()


@pytest.mark.parametrize("database,stage,recovery", [
    ("main", "commit", "public"), ("main", "commit", "cold-open"),
    ("main", "vacuum", "public"), ("audit", "commit", "public"), ("audit", "vacuum", "public"),
])
def test_real_readers_block_cleanup_and_successful_retry_removes_plaintext(monkeypatch, database, stage, recovery):
    store, _ = create_task("busy-cleanup")
    path = create_legacy_audit(store) if database == "audit" else store.db_path
    query = "SELECT content FROM audit_log" if database == "audit" else "SELECT title FROM work_items"
    storage.close_store_connection(store.db_path)
    audit.evict_audit_cache(store.namespace)
    original_connect = sqlite3.connect
    reader = original_connect(path, timeout=0.02)
    armed = False

    def pin_reader():
        nonlocal armed
        if not armed:
            reader.execute("BEGIN")
            reader.execute(query).fetchall()
            armed = True

    class ReaderHookConnection(sqlite3.Connection):
        def commit(self):
            if stage == "commit":
                pin_reader()
            return super().commit()

        def execute(self, sql, *args):
            result = super().execute(sql, *args)
            if stage == "vacuum" and sql == "VACUUM":
                pin_reader()
            return result

    def instrumented_connect(database_path, *args, **kwargs):
        kwargs["timeout"] = 0.02
        if str(database_path) == str(path) and "factory" not in kwargs:
            kwargs["factory"] = ReaderHookConnection
        return original_connect(database_path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", instrumented_connect)
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", TEST_KEY.hex())
    try:
        with pytest.raises(RuntimeError, match="encryption committed;.*active readers"):
            crypto.encrypt_namespace_db(store.namespace)
        assert armed
        row, notes, pending, encrypted = raw_work(store.db_path)
        assert all(crypto.is_encrypted_text(value) for value in (*row, *notes))
        assert encrypted == "1"
        assert pending == ("1" if database == "main" else "0")
        if stage == "commit":
            assert CANARY.encode() in path.read_bytes()
        reader.rollback()
        reader.close()
        if recovery == "cold-open":
            recovered = MeshStore(store.namespace, create=False)
            assert recovered.list_work_items()[0]["title"] == CANARY
        else:
            assert crypto.encrypt_namespace_db(store.namespace)["encrypted"] is True
        assert raw_work(store.db_path)[2] == "0"
        assert_no_canary(store.db_path)
        assert_no_canary(path)
    finally:
        reader.close()


@pytest.mark.parametrize("bound", [True, False])
def test_namespace_alias_is_refused_even_for_unbound_legacy_rows(monkeypatch, bound):
    store, _ = create_task("original-owner")
    storage.close_store_connection(store.db_path)
    if not bound:
        with contextlib.closing(sqlite3.connect(store.db_path)) as conn:
            conn.execute("DELETE FROM mesh_meta WHERE key='namespace_owner'")
            conn.commit()
    alias = store.db_path.parent / "mesh_alias-owner.db"
    shutil.copy2(store.db_path, alias)
    before = hashlib.sha256(alias.read_bytes()).hexdigest()
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", TEST_KEY.hex())
    with pytest.raises(ValueError, match="another namespace"):
        crypto.encrypt_namespace_db("alias-owner")
    assert hashlib.sha256(alias.read_bytes()).hexdigest() == before
    assert raw_work(alias)[0][0] == CANARY
    assert raw_work(alias)[3] != "1"
    assert not list((alias.parent / "backups").glob("*.bak.*"))


@pytest.mark.parametrize("audit_kind", ["legacy-log", "event-owner"])
def test_audit_ownership_is_validated_before_memory_database_changes(monkeypatch, audit_kind):
    store, _ = create_task("audit-owner")
    if audit_kind == "legacy-log":
        audit_path = create_legacy_audit(store, owner="different-owner")
    else:
        audit_path = create_legacy_audit(store, event_owner="different-owner")
    storage.close_store_connection(store.db_path)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (store.db_path, audit_path)}
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", TEST_KEY.hex())
    with pytest.raises((ValueError, RuntimeError), match="another namespace|multiple namespaces"):
        crypto.encrypt_namespace_db(store.namespace)
    assert raw_work(store.db_path)[0][0] == CANARY
    assert raw_work(store.db_path)[3] != "1"
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before} == before
    assert not list((store.db_path.parent / "backups").glob("*.bak.*"))
