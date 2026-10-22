"""
tests/test_work_items.py — Comprehensive tests for Mesh first-class Work Items,
Changelog, Project Brief, Content-Free Audit Telemetry, and Schema v3 Migration.
"""
import concurrent.futures
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from mesh.audit import export_content_free_audit, log_audit_event
from mesh.io import export_namespace, import_namespace
from mesh.memory import Mesh
from mesh.store import MeshStore


@pytest.fixture
def temp_mesh_dir(monkeypatch):
    """Isolate tests into a fresh temporary MESH_HOME."""
    temp_dir = tempfile.mkdtemp(prefix="mesh_test_work_items_")
    monkeypatch.setenv("MESH_HOME", temp_dir)
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_work_item_lifecycle(temp_mesh_dir):
    """Test full work item lifecycle: create -> start -> block -> complete -> reopen."""
    mesh = Mesh(namespace="lifecycle_test", agent_id="agent-alice")

    # 1. Create a goal and a child task
    goal = mesh.create_work_item(
        title="Implement Phase 2 Work Items",
        description="Add first-class work_items and work_events",
        kind="goal",
        priority="high",
        tags=["phase2", "core"],
    )
    assert goal["short_id"].startswith("goal-")
    assert goal["status"] == "todo"
    assert goal["kind"] == "goal"

    task = mesh.create_work_item(
        title="Write tests for work items",
        description="Cover CRUD, lifecycle, filters, brief",
        kind="task",
        parent_id=goal["id"],
        priority="critical",
        tags=["tests"],
    )
    assert task["short_id"].startswith("task-")
    assert task["parent_id"] == goal["id"]
    assert task["status"] == "todo"

    # 2. Start task (todo -> in_progress)
    t_started = mesh.update_work_item(
        item_id=task["short_id"],
        status="in_progress",
        note="Started writing unit tests",
    )
    assert t_started["status"] == "in_progress"
    assert t_started["started_at"] is not None

    # 3. Block task (in_progress -> blocked)
    t_blocked = mesh.update_work_item(
        item_id=task["id"],
        status="blocked",
        note="Blocked on test runner environment",
    )
    assert t_blocked["status"] == "blocked"

    # 4. Resume task (blocked -> in_progress)
    t_resumed = mesh.update_work_item(
        item_id=task["short_id"],
        status="in_progress",
        note="Unblocked, test runner ready",
    )
    assert t_resumed["status"] == "in_progress"

    # 5. Complete task (in_progress -> done, REQUIRES summary)
    t_done = mesh.complete_work_item(
        item_id=task["short_id"],
        summary="Wrote 11 comprehensive unit tests for work items.",
        evidence=["tests/test_work_items.py"],
        note="All tests passing smoothly",
    )
    assert t_done["status"] == "done"
    assert t_done["completed_at"] is not None
    assert t_done["completed_by"] == "agent-alice"
    assert t_done["summary"] == "Wrote 11 comprehensive unit tests for work items."
    assert "tests/test_work_items.py" in t_done["evidence"]

    # 6. Check work events history
    events = mesh.get_work_events(item_id=task["id"])
    assert len(events) >= 5  # created, start, block, resume, complete
    event_types = [e["event_type"] for e in events]
    assert "created" in event_types
    assert "in_progress" in event_types
    assert "completed" in event_types

    # 7. Reopen task (done -> todo)
    t_reopened = mesh.update_work_item(
        item_id=task["short_id"],
        status="todo",
        note="Reopening to add concurrency test",
    )
    assert t_reopened["status"] == "todo"


def test_invalid_transitions_and_validation(temp_mesh_dir):
    """Test required summary on complete, invalid statuses, invalid kinds."""
    mesh = Mesh(namespace="validation_test", agent_id="agent-bob")

    task = mesh.create_work_item(title="Validation task")

    # Complete WITHOUT summary must fail
    with pytest.raises(ValueError, match="summary.*required"):
        mesh.complete_work_item(item_id=task["id"], summary="")

    with pytest.raises(ValueError, match="summary.*required"):
        mesh.complete_work_item(item_id=task["id"], summary="   ")

    # Invalid status
    with pytest.raises(ValueError, match="Invalid status"):
        mesh.update_work_item(item_id=task["id"], status="nonexistent_status")

    # Invalid priority
    with pytest.raises(ValueError, match="Invalid priority"):
        mesh.update_work_item(item_id=task["id"], priority="super_urgent")

    # Invalid kind on create
    with pytest.raises(ValueError, match="Invalid kind"):
        mesh.create_work_item(title="Bad kind", kind="epic")


def test_work_item_not_found_guidance(temp_mesh_dir):
    """
    Issue A-007 regression:
    Updating or completing an unknown work item must provide actionable guidance:
    1. If no items exist: advice to use mesh_task_create first.
    2. If items exist: list available work item IDs in the namespace.
    """
    mesh = Mesh(namespace="guidance_test", agent_id="agent-alice")

    # 1. Empty namespace
    with pytest.raises(ValueError, match="No work items exist yet in this namespace; use mesh_task_create to create one first"):
        mesh.update_work_item(item_id="task-1", status="in_progress")

    # 2. Namespace with an item
    created = mesh.create_work_item(title="Existing Task")
    with pytest.raises(ValueError, match=f"Available work items:.*{created['short_id']}"):
        mesh.update_work_item(item_id="task-99", status="in_progress")



def test_work_item_reopen_status_transition(temp_mesh_dir):
    """
    Issue A-005 / B-006 regression:
    Updating work item with status='reopened' or 'reopen' must transition status to 'todo',
    clear completed_at, and record an event with event_type='reopened'.
    """
    mesh = Mesh(namespace="reopen_test", agent_id="agent-alice")
    task = mesh.create_work_item(title="Fix bug #123")
    mesh.complete_work_item(item_id=task["id"], summary="Fixed bug #123")

    completed_task = mesh.get_work_item(task["id"])
    assert completed_task["status"] == "done"
    assert completed_task["completed_at"] is not None

    # Reopen using status="reopened"
    reopened_task = mesh.update_work_item(
        item_id=task["id"],
        status="reopened",
        note="Bug recurred in production",
    )
    assert reopened_task["status"] == "todo"
    assert reopened_task["completed_at"] is None

    events = mesh.get_work_events(item_id=task["id"])
    reopened_event = [e for e in events if e["event_type"] == "reopened"]
    assert len(reopened_event) == 1
    assert reopened_event[0]["old_status"] == "done"
    assert reopened_event[0]["new_status"] == "todo"
    assert reopened_event[0]["note"] == "Bug recurred in production"

    # Also test status="reopen"
    mesh.complete_work_item(item_id=task["id"], summary="Fixed again")
    reopened2 = mesh.update_work_item(item_id=task["id"], status="reopen")
    assert reopened2["status"] == "todo"
    assert reopened2["completed_at"] is None



def test_work_item_filters_and_ordering(temp_mesh_dir):
    """Test listing work items by status, kind, parent_id, and priority ordering."""
    mesh = Mesh(namespace="filter_test", agent_id="agent-charlie")

    g1 = mesh.create_work_item(title="Goal 1", kind="goal", priority="high")
    t1 = mesh.create_work_item(title="Task Low", kind="task", parent_id=g1["id"], priority="low")
    t2 = mesh.create_work_item(title="Task Critical", kind="task", parent_id=g1["id"], priority="critical")
    t3 = mesh.create_work_item(title="Task Medium", kind="task", parent_id=g1["id"], priority="medium")

    mesh.complete_work_item(t1["id"], summary="Finished low priority task")

    # Filter by kind: goal
    goals = mesh.list_work_items(kind="goal")
    assert len(goals) == 1
    assert goals[0]["id"] == g1["id"]

    # Filter by status: done
    done_items = mesh.list_work_items(status="done")
    assert len(done_items) == 1
    assert done_items[0]["id"] == t1["id"]

    # Filter by parent_id
    child_items = mesh.list_work_items(parent_id=g1["id"])
    assert len(child_items) == 3

    # Priority ordering for open items (critical first, then medium)
    open_items = mesh.list_work_items(status="todo")
    assert len(open_items) == 3  # g1 (high), t2 (critical), t3 (medium)
    assert open_items[0]["id"] == t2["id"]  # critical comes before high and medium


def test_changelog_output(temp_mesh_dir):
    """Test mesh_log / get_changelog output in Markdown and JSON formats."""
    mesh = Mesh(namespace="changelog_test", agent_id="agent-dev")

    # Store an architectural decision memory
    mesh.learn(
        "Decided to use SQLite for work items and append-only event log",
        memory_type="decision",
        tags=["architecture", "phase2"],
    )

    # Create and complete two tasks
    t1 = mesh.create_work_item(title="Schema migration v3", priority="high")
    mesh.complete_work_item(
        t1["id"],
        summary="Added work_items and work_events tables with automatic backup",
        evidence=["mesh/store.py"],
    )

    t2 = mesh.create_work_item(title="Add MCP tools for tasks", priority="medium")
    mesh.complete_work_item(
        t2["id"],
        summary="Added 6 tools to MCP server with timing telemetry",
        evidence=["mesh/mcp_server.py"],
    )

    # Test Markdown changelog
    cl_md = mesh.changelog(since="1d", format="markdown")
    assert cl_md["completed_count"] == 2
    assert cl_md["decisions_count"] == 1
    md_text = cl_md["changelog_markdown"]
    assert "# Project Changelog: changelog_test" in md_text
    assert "Schema migration v3" in md_text
    assert "Decided to use SQLite for work items" in md_text
    assert "mesh/store.py" in md_text

    # Test JSON changelog
    cl_json = mesh.changelog(since="1d", format="json")
    assert len(cl_json["completed_items"]) == 2
    assert len(cl_json["decisions"]) == 1
    assert cl_json["completed_items"][0]["title"] in ("Schema migration v3", "Add MCP tools for tasks")


def test_private_items_excluded_by_default(temp_mesh_dir):
    """Test that private work items (local_only=True) are excluded by default."""
    mesh = Mesh(namespace="privacy_test", agent_id="agent-sec")

    pub_task = mesh.create_work_item(title="Public task", local_only=False)
    priv_task = mesh.create_work_item(title="Private secret task", local_only=True)

    # By default, list_work_items excludes private items
    public_items = mesh.list_work_items(include_private=False)
    item_ids = [it["id"] for it in public_items]
    assert pub_task["id"] in item_ids
    assert priv_task["id"] not in item_ids

    # With include_private=True, it includes private items
    all_items = mesh.list_work_items(include_private=True)
    all_ids = [it["id"] for it in all_items]
    assert pub_task["id"] in all_ids
    assert priv_task["id"] in all_ids

    # Get single item fails without include_private
    assert mesh.get_work_item(priv_task["id"], include_private=False) is None
    assert mesh.get_work_item(priv_task["id"], include_private=True) is not None

    # Complete private item and verify changelog excludes it by default
    mesh.complete_work_item(priv_task["id"], summary="Completed secret task")
    mesh.complete_work_item(pub_task["id"], summary="Completed public task")

    cl_pub = mesh.changelog(since="1d", include_private=False)
    assert cl_pub["completed_count"] == 1
    assert "Private secret task" not in cl_pub["changelog_markdown"]


def test_brief_budget_enforcement(temp_mesh_dir):
    """Test mesh_project_brief token budget capping, health stats, and new namespace handling."""
    mesh = Mesh(namespace="brief_test", agent_id="agent-brief")

    # 1. Brand new / empty namespace
    empty_brief = mesh.project_brief()
    assert empty_brief["namespace_state"] == "new_or_empty"
    assert empty_brief["total_memories"] == 0
    assert empty_brief["open_tasks_count"] == 0

    # 2. Add memories and tasks
    for i in range(10):
        mesh.learn(f"Important architectural fact {i} about system subsystem", memory_type="fact")
    for i in range(10):
        mesh.create_work_item(title=f"Task number {i} for subsystem")

    active_brief = mesh.project_brief(max_tokens=300)
    assert active_brief["namespace_state"] == "active"
    assert active_brief["total_memories"] == 10
    assert active_brief["open_tasks_count"] == 10
    assert active_brief["estimated_tokens"] <= 300
    assert "Project Brief: brief_test" in active_brief["brief_markdown"]


def test_task_state_never_triggers_contradiction_or_decay(temp_mesh_dir):
    """Test that work items are completely separated from vector contradiction and decay."""
    mesh = Mesh(namespace="isolation_test", agent_id="agent-iso")

    # Create opposing task titles
    t1 = mesh.create_work_item(title="Use PostgreSQL database for backend storage")
    t2 = mesh.create_work_item(title="Do not use PostgreSQL database for backend storage, use SQLite")

    # Complete t1, update t2
    mesh.complete_work_item(t1["id"], summary="Decided on SQLite instead, cancelling PostgreSQL")
    mesh.update_work_item(t2["id"], status="in_progress")

    # Check contradictions table in store
    store = mesh.store
    with store._connect() as c:
        contradictions = c.execute(
            "SELECT * FROM contradictions WHERE namespace = ? AND resolved = 0",
            (store.namespace,),
        ).fetchall()
    assert len(contradictions) == 0  # No contradiction created by work items

    # Count memories vs work items
    assert store.count() == 0
    assert len(store.list_work_items()) == 2


def test_content_free_audit_telemetry(temp_mesh_dir):
    """Verify that audit records contain durations, statuses, but NEVER memory or task text."""
    PLANTED_SECRET = "PLANTED_SECRET_KEY_XYZZY_999888777"

    mesh = Mesh(namespace="audit_test", agent_id="agent-audit")
    # Log audit event with secret in argument or payload
    log_audit_event(
        surface="mcp",
        tool_or_command="mesh_task_create",
        agent_id="agent-audit",
        namespace="audit_test",
        duration_ms=42.5,
        status_code="ok",
        payload_size_bytes=len(PLANTED_SECRET),
        result_count=1,
    )

    # Export audit events
    events = export_content_free_audit(namespace="audit_test")
    assert len(events) >= 1
    ev = events[0]
    assert ev["surface"] == "mcp"
    assert ev["tool_or_command"] == "mesh_task_create"
    assert ev["agent_id"] == "agent-audit"
    assert ev["duration_ms"] == 42.5
    assert ev["status_code"] == "ok"
    assert ev["payload_size_bytes"] == len(PLANTED_SECRET)

    # Scan entire JSON export for the planted secret
    export_json_str = json.dumps(events)
    assert PLANTED_SECRET not in export_json_str


def test_schema_migration_v3(temp_mesh_dir):
    """Test backward-compatible migration from schema v2 to v3 with pre-migration backup."""
    import sqlite3

    ns = "mig_test"
    db_path = temp_mesh_dir / f"mesh_{ns}.db"

    # Create a v2 database manually
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE schema_info (version TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO schema_info (version) VALUES ('2')")
    conn.execute(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            namespace TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL NOT NULL,
            source_agent TEXT NOT NULL,
            tags TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            accessed_at TEXT NOT NULL,
            access_count INTEGER NOT NULL,
            ttl_days REAL,
            decay_start_at TEXT,
            local_only INTEGER NOT NULL DEFAULT 0,
            memory_type TEXT NOT NULL DEFAULT 'fact',
            embedding BLOB,
            embedding_model TEXT NOT NULL DEFAULT 'onnx-minilm-l6-v2',
            trust_tier TEXT NOT NULL DEFAULT 'user',
            quarantined INTEGER NOT NULL DEFAULT 0,
            secret_ref TEXT
        )
        """
    )
    conn.execute("CREATE TABLE queries (id TEXT PRIMARY KEY, namespace TEXT, query_text TEXT, timestamp TEXT)")
    conn.execute("CREATE TABLE contradictions (id TEXT PRIMARY KEY, namespace TEXT, resolved INTEGER DEFAULT 0)")
    conn.execute(
        "INSERT INTO memories (id, namespace, content, confidence, source_agent, tags, created_at, accessed_at, access_count) "
        "VALUES ('mem-1', 'mig_test', 'Initial memory v2', 1.0, 'test', '[]', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1)"
    )
    conn.commit()
    conn.close()

    # Now open with MeshStore, which triggers migration to v3
    store = MeshStore(namespace=ns, create=True)
    assert store.get_schema_version() == "3"

    # Verify backup file was created
    backups = list(temp_mesh_dir.glob(f"mesh_{ns}.db.backup-v0.7.2-*"))
    assert len(backups) >= 1

    # Verify work_items and work_events tables now exist
    with store._connect() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "work_items" in tables
        assert "work_events" in tables

    # Verify existing memory is intact
    mem = store.get("mem-1")
    assert mem is not None
    assert mem["content"] == "Initial memory v2"


def test_concurrency_work_items(temp_mesh_dir):
    """Test concurrent work items updates from multiple threads."""
    mesh = Mesh(namespace="concurrent_test", agent_id="agent-main")
    tasks = [mesh.create_work_item(title=f"Concurrent Task {i}") for i in range(10)]

    def worker(task_id, worker_idx):
        m = Mesh(namespace="concurrent_test", agent_id=f"worker-{worker_idx}")
        m.update_work_item(item_id=task_id, status="in_progress", note=f"Worker {worker_idx} started")
        time.sleep(0.01)
        return m.complete_work_item(item_id=task_id, summary=f"Completed by worker {worker_idx}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker, tasks[i]["id"], i) for i in range(10)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert len(results) == 10
    completed_items = mesh.list_work_items(status="done")
    assert len(completed_items) == 10


def test_work_items_export_import(temp_mesh_dir):
    """Test exporting and importing work items and events across namespaces."""
    mesh = Mesh(namespace="src_ns", agent_id="agent-src")

    mesh.learn("Fact memory in src_ns", memory_type="fact")
    t_pub = mesh.create_work_item(title="Public work item in src_ns", local_only=False)
    mesh.complete_work_item(t_pub["id"], summary="Finished src public task")

    t_priv = mesh.create_work_item(title="Private work item in src_ns", local_only=True)

    export_file = temp_mesh_dir / "export.json"
    exported_data = export_namespace(namespace="src_ns", output_path=str(export_file))

    # Verify exported data
    assert exported_data["work_items_count"] == 1
    assert exported_data["skipped_local_only_work_items"] == 1
    assert exported_data["work_items"][0]["title"] == "Public work item in src_ns"

    # Import into target_ns
    stats = import_namespace(input_path=str(export_file), target_namespace="dst_ns")
    assert stats["imported_work_items"] == 1

    dst_mesh = Mesh(namespace="dst_ns")
    dst_tasks = dst_mesh.list_work_items()
    assert len(dst_tasks) == 1
    assert dst_tasks[0]["title"] == "Public work item in src_ns"
    assert dst_tasks[0]["status"] == "done"


def test_brief_decision_rationale_flagging(temp_mesh_dir):
    """
    Finding F2 test:
    Verify that decisions lacking causal rationale are flagged with a hint in the project brief,
    while decisions containing reasons ('because', 'since', etc.) are cleanly displayed.
    """
    mesh = Mesh(namespace="rationale_test", agent_id="agent-dec")

    # 1. Decision without rationale
    mesh.learn("Decided to switch from REST to gRPC for microservices", memory_type="decision")
    # 2. Decision with rationale
    mesh.learn(
        "Decided to use SQLite with WAL mode because concurrency requires non-blocking reads",
        memory_type="decision",
    )

    brief = mesh.project_brief()
    md = brief["brief_markdown"]

    # Decision without reason should have the hint
    assert "needs rationale — add 'because <reason>'" in md
    # Decision with reason should be present without duplicated hint
    assert "Decided to use SQLite with WAL mode because concurrency requires non-blocking reads" in md

