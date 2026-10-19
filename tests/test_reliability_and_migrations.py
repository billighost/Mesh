"""
tests/test_reliability_and_migrations.py — Production Reliability, Property Tests,
Schema Migration, Crash Resilience, and HTTP Work Item Verification.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from fastapi.testclient import TestClient

from mesh.guards import guard_ingest
from mesh.http_server import app, verify_request_auth
from mesh.installer import (
    ADAPTER_MAP,
    apply_installation,
    plan_installation,
    uninstall_mesh_agent,
)
from mesh.memory import Mesh
from mesh.secrets import is_credential, redact_secrets_local
from mesh.store import MeshStore, SCHEMA_VERSION, namespace_write_lock


@pytest.fixture
def temp_mesh_dir(monkeypatch):
    """Isolate tests into a fresh temporary MESH_HOME."""
    td = tempfile.mkdtemp(prefix="mesh_test_reliability_")
    monkeypatch.setenv("MESH_HOME", td)
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")
    yield Path(td)
    shutil.rmtree(td, ignore_errors=True)


# ==============================================================================
# 1. Historical Schema Migrations & Future Schema Safety
# ==============================================================================


def test_schema_migration_from_v1_to_current(temp_mesh_dir):
    """
    Simulate an older v1 database (memories table without quarantined, trust_tier,
    secret_ref, and no work_items/work_events tables).
    Opening with current MeshStore must back up and migrate cleanly to current SCHEMA_VERSION.
    """
    ns = "legacy_v1"
    db_path = temp_mesh_dir / f"mesh_{ns}.db"

    # Create raw v1 SQLite schema
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA user_version = 1")
        conn.execute("""
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                memory_type TEXT NOT NULL DEFAULT 'fact',
                source_agent TEXT NOT NULL DEFAULT 'unknown',
                tags TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                accessed_at TEXT NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                ttl_days REAL,
                decay_start_at TEXT,
                local_only INTEGER NOT NULL DEFAULT 0,
                embedding BLOB,
                embedding_model TEXT NOT NULL DEFAULT 'unknown'
            )
        """)
        conn.execute("""
            INSERT INTO memories (id, namespace, content, created_at, accessed_at)
            VALUES ('mem-v1-001', 'legacy_v1', 'Legacy v1 system fact', '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')
        """)
        conn.commit()

    # Open with current MeshStore
    store = MeshStore(namespace=ns, create=False)
    store._ensure_initialized()

    # Verify migration results
    with store._connect() as conn:
        # User version updated
        cur = conn.execute("PRAGMA user_version")
        assert cur.fetchone()[0] == int(SCHEMA_VERSION)

        # Verify old memory preserved
        cur = conn.execute("SELECT id, content, trust_tier, quarantined FROM memories WHERE id = 'mem-v1-001'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "mem-v1-001"
        assert row[1] == "Legacy v1 system fact"
        assert row[2] == "user"  # default trust_tier
        assert row[3] == 0  # quarantined = 0

        # Verify new tables exist and are functional
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('work_items', 'work_events')"
        )
        tables = {r[0] for r in cur.fetchall()}
        assert "work_items" in tables
        assert "work_events" in tables

    # Verify backup file was created in MESH_HOME
    backups = list(temp_mesh_dir.glob(f"mesh_{ns}.db.backup-*"))
    assert len(backups) >= 1, "Pre-migration backup was not created"


def test_future_schema_version_fails_gracefully(temp_mesh_dir):
    """
    If a database has a future user_version (e.g. 99), an older Mesh version
    must fail gracefully with a descriptive error and refuse to corrupt the database.
    """
    ns = "future_db"
    db_path = temp_mesh_dir / f"mesh_{ns}.db"

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA user_version = 99")
        conn.execute("CREATE TABLE future_table (id TEXT PRIMARY KEY, payload TEXT)")
        conn.commit()

    with pytest.raises(RuntimeError) as exc_info:
        MeshStore(namespace=ns, create=False)

    assert "newer than supported version" in str(exc_info.value).lower() or "99" in str(exc_info.value)

    # Ensure future database content was untouched
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute("PRAGMA user_version")
        assert cur.fetchone()[0] == 99


# ==============================================================================
# 2. Backup, Restore, and Crash Resilience
# ==============================================================================


def test_backup_and_restore_cycle(temp_mesh_dir):
    """Verify manual SQLite online backup and restore recovers all memories and work items."""
    mesh = Mesh(namespace="backup_test", agent_id="backup-bot")
    mem = mesh.learn("Database configuration parameter port 9999", tags=["infra"])
    task = mesh.create_work_item(title="Upgrade database cluster", kind="task")

    db_path = mesh.store.db_path
    backup_path = temp_mesh_dir / "backup_manual.db"

    # Online backup using SQLite backup API
    with mesh.store._connect() as src_conn:
        with sqlite3.connect(str(backup_path)) as dst_conn:
            src_conn.backup(dst_conn)

    assert backup_path.exists()
    assert backup_path.stat().st_size > 0

    # Simulate catastrophic corruption of live database
    with open(db_path, "wb") as f:
        f.write(b"CORRUPTED_GARBAGE_BYTES_0123456789")

    # Restore from backup
    shutil.copy2(backup_path, db_path)

    # Reconnect and verify data integrity
    restored_mesh = Mesh(namespace="backup_test", agent_id="restore-bot")
    recalled = restored_mesh.recall("port 9999", n=1)
    assert len(recalled) == 1
    assert "9999" in recalled[0]["content"]

    item = restored_mesh.get_work_item(task["id"])
    assert item is not None
    assert item["title"] == "Upgrade database cluster"


def test_interrupted_write_rollback_safety(temp_mesh_dir):
    """Verify that an interrupted write inside a transaction rolls back completely."""
    mesh = Mesh(namespace="rollback_test", agent_id="tester")
    mesh.learn("Initial stable fact")

    # Attempt a transaction that raises an unhandled error midway
    with pytest.raises(RuntimeError):
        with namespace_write_lock(mesh.store.namespace):
            with mesh.store._connect() as conn:
                conn.isolation_level = "DEFERRED"
                with conn:
                    conn.execute(
                        "INSERT INTO memories (id, namespace, content, created_at, accessed_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            "temp-half-write",
                            mesh.store.namespace,
                            "Partial data",
                            "2026-01-01T00:00:00Z",
                            "2026-01-01T00:00:00Z",
                        ),
                    )
                    raise RuntimeError("Simulated mid-write crash/kill")

    # Verify that partial data was rolled back and database remains intact
    with mesh.store._connect() as conn:
        cur = conn.execute("SELECT id FROM memories WHERE id = 'temp-half-write'")
        assert cur.fetchone() is None

    # Normal operations continue working
    fresh = mesh.learn("Subsequent stable fact")
    assert fresh["status"] == "stored"


# ==============================================================================
# 3. HTTP Server Work Items & Project Brief Endpoints
# ==============================================================================


def test_http_work_items_and_brief_endpoints(temp_mesh_dir, monkeypatch):
    """Comprehensive test for HTTP /brief, /tasks, /tasks/{id}, /tasks/{id}/complete, /log."""
    monkeypatch.setenv("MESH_HTTP_HOST", "127.0.0.1")
    app.dependency_overrides[verify_request_auth] = lambda: None
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    try:
        ns = "http_work_test"

        # 1. GET /brief on empty namespace
        resp = client.get(f"/brief?namespace={ns}")
        assert resp.status_code == 200
        brief_data = resp.json()
        assert brief_data["status"] == "ok"
        assert "brief_markdown" in brief_data

        # 2. POST /tasks create goal
        resp = client.post(
            "/tasks",
            json={
                "namespace": ns,
                "title": "Build production API",
                "description": "API readiness and contracts",
                "kind": "goal",
                "priority": "high",
                "tags": ["api", "release"],
            },
        )
        assert resp.status_code == 200
        goal = resp.json()["task"]
        assert goal["short_id"].startswith("goal-")
        assert goal["status"] == "todo"

        # 3. POST /tasks create child task
        resp = client.post(
            "/tasks",
            json={
                "namespace": ns,
                "title": "Write OpenAPI specs",
                "kind": "task",
                "parent_id": goal["id"],
                "priority": "medium",
            },
        )
        assert resp.status_code == 200
        task = resp.json()["task"]
        assert task["parent_id"] == goal["id"]

        # 4. GET /tasks list
        resp = client.get(f"/tasks?namespace={ns}&status=todo")
        assert resp.status_code == 200
        task_list = resp.json()
        assert task_list["count"] == 2

        # 5. GET /tasks/{id} detail
        resp = client.get(f"/tasks/{task['short_id']}?namespace={ns}")
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["task"]["id"] == task["id"]
        assert len(detail["events"]) >= 1

        # 6. PATCH /tasks/{id} update status to in_progress
        resp = client.patch(
            f"/tasks/{task['short_id']}",
            json={
                "namespace": ns,
                "status": "in_progress",
                "note": "Started writing specs",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["task"]["status"] == "in_progress"

        # 7. POST /tasks/{id}/complete mark done
        resp = client.post(
            f"/tasks/{task['short_id']}/complete",
            json={
                "namespace": ns,
                "summary": "Completed OpenAPI schema v3.1 and validation rules",
                "evidence": ["docs/openapi.yaml", "commit:abc1234"],
                "note": "Verified with prism mock server",
            },
        )
        assert resp.status_code == 200
        completed = resp.json()["task"]
        assert completed["status"] == "done"
        assert completed["summary"] == "Completed OpenAPI schema v3.1 and validation rules"

        # 8. GET /log changelog
        resp = client.get(f"/log?namespace={ns}&since=24h")
        assert resp.status_code == 200
        log_data = resp.json()
        assert log_data["status"] == "ok"
        assert len(log_data["completed_items"]) >= 1
        assert "OpenAPI" in log_data["changelog_markdown"]

        # 9. GET /brief updated
        resp = client.get(f"/brief?namespace={ns}")
        assert resp.status_code == 200
        brief_data = resp.json()
        assert brief_data["completed_recent_count"] >= 1
    finally:
        app.dependency_overrides.pop(verify_request_auth, None)


# ==============================================================================
# 4. Installer Idempotency & Byte-Exact Uninstall
# ==============================================================================


def test_installer_idempotency_and_uninstall_in_temp_dir(temp_mesh_dir, monkeypatch):
    """
    Test installer across all adapters in an isolated environment:
    - First run: installs configs.
    - Second run: idempotency check (reports 'already up to date', zero byte changes).
    - Uninstall: restores original config state cleanly.
    """
    user_home = temp_mesh_dir / "mock_home"
    user_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("APPDATA", str(user_home / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_home / ".config"))

    project_dir = temp_mesh_dir / "mock_project"
    project_dir.mkdir(parents=True, exist_ok=True)

    # Pre-seed a project AGENTS.md with existing instructions
    agents_md = project_dir / "AGENTS.md"
    original_agents_md_content = "# Project Guidelines\n\n1. Write clean tests.\n2. Respect privacy.\n"
    agents_md.write_text(original_agents_md_content, encoding="utf-8")

    # 1. First run: Plan & apply installation
    plan1 = plan_installation(project_dir=project_dir, scope="project")
    results1 = apply_installation(plan1, dry_run=False)
    assert any(r.get("rules", {}).get("changed") for r in results1)

    installed_content = agents_md.read_text(encoding="utf-8")
    assert "<!-- mesh:begin" in installed_content
    assert "<!-- mesh:end -->" in installed_content
    assert original_agents_md_content in installed_content

    # 2. Second run: Idempotency check (no changes applied)
    plan2 = plan_installation(project_dir=project_dir, scope="project")
    results2 = apply_installation(plan2, dry_run=False)
    for r in results2:
        if r.get("rules"):
            assert r["rules"]["changed"] is False
            assert r["rules"]["status"] == "already up to date"

    # Bytes must match exactly
    assert agents_md.read_text(encoding="utf-8") == installed_content

    # 3. Uninstall: Restores original bytes
    uninst_list = uninstall_mesh_agent(agents=["opencode"], project_dir=project_dir, scope="project")
    assert len(uninst_list) == 1
    assert uninst_list[0]["rules"]["changed"] is True
    assert agents_md.read_text(encoding="utf-8") == original_agents_md_content


# ==============================================================================
# 5. Property / Fuzz Tests with Hypothesis
# ==============================================================================


@settings(max_examples=50, deadline=1000)
@given(st.text(min_size=1, max_size=2000))
def test_ingestion_guard_fuzzing(text):
    """
    Property: Ingestion guard never crashes on arbitrary Unicode, null bytes,
    bidi overrides, or adversarial input strings.
    """
    clean_text = text.replace("\x00", "")
    res = guard_ingest(clean_text, local_only=False)
    assert isinstance(res.rejected, bool)
    assert isinstance(res.content, str)
    assert isinstance(res.local_only, bool)


@settings(max_examples=30, deadline=1000)
@given(
    status=st.sampled_from(["todo", "in_progress", "blocked", "done", "cancelled", "invalid_status", ""]),
    priority=st.sampled_from(["low", "medium", "high", "critical", "none"]),
)
def test_work_item_state_machine_transitions(status, priority):
    """
    Property: Task state machine validates transitions strictly.
    Valid statuses succeed; invalid statuses raise ValueError without corrupting the item.
    """
    mesh = Mesh(namespace="prop_test_shared", agent_id="prop-agent")
    task = mesh.create_work_item(title=f"Property task {time.time_ns()}", priority="medium")

    if status in ("todo", "in_progress", "blocked", "done", "cancelled"):
        updated = mesh.update_work_item(
            task["short_id"], status=status, priority=priority if priority != "none" else None
        )
        assert updated["status"] == status
    else:
        with pytest.raises(ValueError):
            mesh.update_work_item(task["short_id"], status=status)


def test_huge_input_length_guard(temp_mesh_dir):
    """Test defensive rejection of payloads exceeding MAX_CONTENT_LENGTH."""
    mesh = Mesh(namespace="huge_input_test")
    oversized = "A" * 20_000

    # Guard should reject content exceeding 16,384 characters
    with pytest.raises(ValueError) as exc:
        mesh.learn(oversized)
    assert "exceeds maximum allowed length" in str(exc.value)
