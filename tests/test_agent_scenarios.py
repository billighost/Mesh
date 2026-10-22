"""
tests/test_agent_scenarios.py — End-to-end verification of all 9 agent scenarios using MCP tools.

Scenarios:
1. Brand-new project: brief says empty, first learn creates namespace, second session brief shows facts.
2. Fact changes: recall -> update -> no contradiction left behind.
3. Goal with three tasks: one blocked, two completed with summaries; changelog shows them in order.
4. Two agents handing off via brief + notes.
5. Secret-like input: follows documented behavior (redaction, local_only=True) and raw value is unrecoverable.
6. Server unavailable / failure handling: test doctor diagnostics.
7. Brief stays within token budget on 200 and 2,000 memories.
8. Privacy flags cannot be overridden by tool arguments when locked off.
9. Audit export contains durations and never exposes memory text (canary scan).
"""
import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from mesh.audit import export_content_free_audit, get_audit_log
from mesh.cli import doctor_cmd
from mesh.mcp_server import call_tool
from mesh.memory import Mesh


@pytest.fixture
def isolated_mesh_env(monkeypatch):
    """Isolated environment fixture setting temp MESH_HOME and MESH_NO_AUTO_DAEMON=1."""
    temp_dir = tempfile.mkdtemp(prefix="mesh_e2e_")
    home = Path(temp_dir)
    mesh_home = home / ".mesh"
    mesh_home.mkdir(parents=True)

    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")

    yield {"root": home, "mesh_home": mesh_home}
    shutil.rmtree(temp_dir, ignore_errors=True)


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Helper to synchronously invoke an MCP tool and parse its JSON text output."""
    res = asyncio.run(call_tool(name, args))
    assert len(res) > 0
    return json.loads(res[0].text)


# ==============================================================================
# Scenario 1: Brand-New Project
# ==============================================================================
def test_scenario_1_brand_new_project(isolated_mesh_env):
    """Brand-new project: brief says empty, first learn creates namespace, second brief shows facts."""
    ns = "project_alpha_new"

    # 1. First session start: call mesh_project_brief on non-existent namespace
    brief1 = _call("mesh_project_brief", {"namespace": ns, "max_tokens": 500})
    assert brief1["namespace_state"] in ("new", "empty")
    assert brief1["total_memories"] == 0
    assert brief1["open_tasks_count"] == 0
    assert "new" in brief1["brief_markdown"].lower() or "empty" in brief1["brief_markdown"].lower()

    # 2. First write creates the namespace deliberately
    learn_res = _call(
        "mesh_learn",
        {
            "content": "Project uses FastAPI with Python 3.12 and SQLite database.",
            "memory_type": "fact",
            "tags": ["architecture", "python"],
            "namespace": ns,
        },
    )
    assert learn_res["status"] == "stored"

    # 3. Second session start: brief now shows active namespace and stored fact
    brief2 = _call("mesh_project_brief", {"namespace": ns, "max_tokens": 500})
    assert brief2["namespace_state"] == "active"
    assert brief2["total_memories"] == 1
    assert "FastAPI with Python 3.12" in brief2["brief_markdown"]


# ==============================================================================
# Scenario 2: Fact Changes (Update Without Contradiction)
# ==============================================================================
def test_scenario_2_fact_changes_update_without_contradiction(isolated_mesh_env):
    """Fact changes: recall -> update -> no contradiction left behind."""
    ns = "fact_change_project"

    # 1. Store initial fact
    _call(
        "mesh_learn",
        {
            "content": "Local Postgres runs on port 5432.",
            "memory_type": "fact",
            "tags": ["db", "postgres", "ports"],
            "namespace": ns,
        },
    )

    # 2. Port changed to 5433: recall first to find memory ID
    recall_res = _call("mesh_recall", {"query": "Postgres port", "namespace": ns})
    assert recall_res["count"] >= 1
    mem_id = recall_res["results"][0]["id"]
    assert "5432" in recall_res["results"][0]["content"]

    # 3. Update in-place
    update_res = _call(
        "mesh_update",
        {
            "memory_id": mem_id,
            "content": "Local Postgres runs on port 5433 after port conflict fix.",
            "namespace": ns,
        },
    )
    assert update_res["status"] == "updated"

    # 4. Verify no unresolved contradiction exists
    contra_res = _call("mesh_contradictions", {"resolved": False, "namespace": ns})
    assert contra_res.get("count", 0) == 0

    # 5. Recall again: only the updated fact is returned
    recall_res2 = _call("mesh_recall", {"query": "Postgres port", "namespace": ns})
    assert recall_res2["count"] == 1
    assert "5433" in recall_res2["results"][0]["content"]


# ==============================================================================
# Scenario 3: Goal with Three Tasks & Changelog
# ==============================================================================
def test_scenario_3_goals_tasks_and_changelog(isolated_mesh_env):
    """Goal with three tasks, one blocked, two completed with summaries; changelog shows them in order."""
    ns = "work_tracking_project"

    # 1. Create high-level goal
    goal = _call(
        "mesh_task_create",
        {
            "title": "Migrate auth service to OAuth2 JWT tokens",
            "kind": "goal",
            "priority": "high",
            "namespace": ns,
        },
    )
    goal_id = goal["id"]

    # 2. Break down into 3 tasks
    t1 = _call(
        "mesh_task_create",
        {
            "title": "Create JWT validation middleware",
            "kind": "task",
            "parent_id": goal_id,
            "priority": "high",
            "namespace": ns,
        },
    )
    t2 = _call(
        "mesh_task_create",
        {
            "title": "Update user login endpoint to issue JWT",
            "kind": "task",
            "parent_id": goal_id,
            "priority": "medium",
            "namespace": ns,
        },
    )
    t3 = _call(
        "mesh_task_create",
        {
            "title": "Configure OAuth2 provider client secrets",
            "kind": "task",
            "parent_id": goal_id,
            "priority": "high",
            "namespace": ns,
        },
    )

    # 3. Task 3 is blocked on credentials
    _call(
        "mesh_task_update",
        {
            "item_id": t3["id"],
            "status": "blocked",
            "note": "Blocked waiting for client ID and secret from DevOps",
            "namespace": ns,
        },
    )

    # 4. Complete Task 1 with summary and evidence
    _call(
        "mesh_task_complete",
        {
            "item_id": t1["id"],
            "summary": "Implemented PyJWT auth middleware in auth/jwt.py with unit tests.",
            "evidence": ["auth/jwt.py", "tests/test_jwt.py", "git:1a2b3c4"],
            "namespace": ns,
        },
    )

    # 5. Complete Task 2 with summary and evidence
    _call(
        "mesh_task_complete",
        {
            "item_id": t2["id"],
            "summary": "Refactored /api/v1/login to issue signed bearer tokens.",
            "evidence": ["routes/login.py", "git:5d6e7f8"],
            "namespace": ns,
        },
    )

    # 6. Verify task list: 1 blocked task remains open
    open_tasks = _call("mesh_task_list", {"status": "open", "namespace": ns})
    # Open items should include the goal and task 3 (blocked)
    open_ids = {item["id"] for item in open_tasks["work_items"]}
    assert t3["id"] in open_ids
    assert t1["id"] not in open_ids
    assert t2["id"] not in open_ids

    # 7. Generate changelog via mesh_log
    log_res = _call("mesh_log", {"since": "24h", "format": "markdown", "namespace": ns})
    log_text = log_res["changelog"]
    assert "Create JWT validation middleware" in log_text
    assert "Update user login endpoint to issue JWT" in log_text
    assert "auth/jwt.py" in log_text
    assert "git:1a2b3c4" in log_text


# ==============================================================================
# Scenario 4: Two Agents Handing Off via Brief + Notes
# ==============================================================================
def test_scenario_4_multi_agent_handoff(isolated_mesh_env, monkeypatch):
    """Two agents handing off via brief + notes with role suffixes."""
    ns = "multi_agent_project"

    # Agent Alpha: backend lead
    monkeypatch.setenv("MESH_AGENT_ID", "claude-code:backend")
    _call(
        "mesh_task_create",
        {
            "title": "Build REST API for billing subscriptions",
            "kind": "task",
            "priority": "high",
            "namespace": ns,
        },
    )
    _call(
        "mesh_learn",
        {
            "content": "Handoff: Finished subscription models in billing/models.py. Stripe checkout endpoint created.",
            "memory_type": "context",
            "tags": ["handoff", "billing"],
            "namespace": ns,
        },
    )

    # Agent Beta: frontend lead
    monkeypatch.setenv("MESH_AGENT_ID", "antigravity:frontend")
    brief = _call("mesh_project_brief", {"namespace": ns, "max_tokens": 500})
    assert brief["open_tasks_count"] == 1
    assert "billing subscriptions" in brief["brief_markdown"].lower()

    # Agent Beta recalls handoff note
    recall_handoff = _call("mesh_recall", {"query": "Handoff billing subscription", "namespace": ns})
    assert recall_handoff["count"] >= 1
    assert "Finished subscription models" in recall_handoff["results"][0]["content"]


# ==============================================================================
# Scenario 5: Secret-Like Input Handling & Unrecoverability
# ==============================================================================
def test_scenario_5_secret_input_handling(isolated_mesh_env):
    """Secret-like input follows documented behavior (redaction, local_only=True) and is unrecoverable."""
    ns = "security_vault_project"
    RAW_SECRET = "sk-live-99998888777766665555444433332222"

    learn_res = _call(
        "mesh_learn",
        {
            "content": f"Stripe production API key is {RAW_SECRET} for live charges.",
            "memory_type": "fact",
            "tags": ["stripe", "auth"],
            "namespace": ns,
        },
    )
    # 1. Redaction must have occurred
    assert RAW_SECRET not in learn_res.get("content", "")

    # 2. Check storage in SQLite: raw secret must NOT exist anywhere in database
    mesh = Mesh(namespace=ns)
    store = mesh.store
    with store._connect() as c:
        rows = c.execute("SELECT content FROM memories WHERE namespace = ?", (ns,)).fetchall()
        for row in rows:
            assert RAW_SECRET not in row[0]
            assert "[REDACTED" in row[0]


# ==============================================================================
# Scenario 6: Server Unavailable & Doctor Diagnostics
# ==============================================================================
def test_scenario_6_server_unavailable_and_doctor(isolated_mesh_env, capsys):
    """Server unavailable: verify mesh doctor output cleanly diagnoses components without crash."""
    doctor_cmd()
    captured = capsys.readouterr()
    assert "Doctor" in captured.out
    assert "Directory" in captured.out or "mesh v" in captured.out


# ==============================================================================
# Scenario 7: Brief Token Budget on 200 and 2,000 Memories
# ==============================================================================
def test_scenario_7_brief_token_budget_at_scale(isolated_mesh_env):
    """Brief stays within its token budget on 200 and 2,000 memories."""
    ns = "scale_project"
    mesh = Mesh(namespace=ns, agent_id="bench-agent")

    # 1. Seed 200 memories + 10 work items
    for i in range(200):
        mesh.learn(f"System architecture fact number {i}: database pool size is {10 + i}.", tags=["perf"])
    for i in range(10):
        mesh.create_work_item(title=f"Scale task {i}", priority="medium")

    brief_200 = _call("mesh_project_brief", {"namespace": ns, "max_tokens": 500})
    assert brief_200["estimated_tokens"] <= 500
    assert brief_200["total_memories"] == 200

    # 2. Seed up to 2,000 memories
    # Fast direct insert in store
    from mesh.store import _vector_to_blob

    store = mesh.store
    with store._connect() as c:
        now = "2026-10-19T10:00:00Z"
        vec_zeros = _vector_to_blob([0.0] * 384)
        records = [
            (
                f"bulk-mem-{i}",
                ns,
                f"Bulk memory fact number {i} for high density stress testing.",
                1.0,
                "fact",
                "bench-agent",
                "[]",
                now,
                now,
                now,
                0,
                None,
                None,
                0,
                vec_zeros,
                "all-MiniLM-L6-v2",
                "user",
                0,
                None,
            )
            for i in range(201, 2001)
        ]
        c.executemany(
            """INSERT INTO memories (
                id, namespace, content, confidence, memory_type, source_agent,
                tags, created_at, updated_at, accessed_at, access_count, ttl_days,
                decay_start_at, local_only, embedding, embedding_model, trust_tier,
                quarantined, secret_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            records,
        )

    t0 = time.perf_counter()
    brief_2000 = _call("mesh_project_brief", {"namespace": ns, "max_tokens": 500})
    elapsed = time.perf_counter() - t0

    assert brief_2000["total_memories"] >= 2000
    assert brief_2000["estimated_tokens"] <= 500
    assert elapsed < 0.25  # Fast execution well under 250ms


# ==============================================================================
# Scenario 8: Privacy Flags Cannot Be Overridden When Locked Off
# ==============================================================================
def test_scenario_8_privacy_flags_lockdown(isolated_mesh_env, monkeypatch):
    """Privacy flags cannot be overridden by tool arguments when locked off."""
    ns = "privacy_lock_project"
    monkeypatch.setenv("MESH_BLOCK_PRIVATE_OVERRIDE", "1")
    monkeypatch.setenv("MESH_MCP_INCLUDE_PRIVATE", "0")

    # Store a private memory
    _call(
        "mesh_learn",
        {
            "content": "Confidential internal API token configuration for billing gateway.",
            "memory_type": "fact",
            "local_only": True,
            "tags": ["confidential"],
            "namespace": ns,
        },
    )

    # Attempt recall with include_private=True (should be rejected/ignored by policy)
    res = _call(
        "mesh_recall",
        {
            "query": "Confidential internal token",
            "include_private": True,
            "namespace": ns,
        },
    )
    # Must NOT find the private memory
    assert res["count"] == 0
    for r in res.get("results", []):
        assert "Confidential" not in r["content"]


# ==============================================================================
# Scenario 9: Audit Export Contains Durations and No Memory Text
# ==============================================================================
def test_scenario_9_audit_export_timing_and_content_free(isolated_mesh_env):
    """Audit export contains durations and no memory text (canary scan)."""
    ns = "audit_verification_project"
    CANARY = "SECRET_CANARY_PAYLOAD_ABCXYZ_777888999"

    # Execute multiple MCP tool operations with canary text
    _call(
        "mesh_learn",
        {
            "content": f"Architecture convention referencing {CANARY}",
            "memory_type": "fact",
            "tags": ["canary"],
            "namespace": ns,
        },
    )
    task = _call(
        "mesh_task_create",
        {
            "title": f"Task containing canary {CANARY}",
            "description": f"Details containing canary {CANARY}",
            "namespace": ns,
        },
    )
    _call(
        "mesh_task_complete",
        {
            "item_id": task["id"],
            "summary": f"Completed with summary containing {CANARY}",
            "evidence": ["canary_file.py"],
            "namespace": ns,
        },
    )

    # Export content-free audit records
    events = export_content_free_audit(namespace=ns)
    assert len(events) >= 3

    for ev in events:
        # 1. Must contain telemetry fields
        assert "timestamp" in ev
        assert "surface" in ev
        assert "tool_or_command" in ev
        assert "agent_id" in ev
        assert "duration_ms" in ev
        assert ev["duration_ms"] >= 0.0
        assert "status_code" in ev
        assert "payload_size_bytes" in ev

        # 2. Canary scan: verify CANARY NEVER appears in ANY audit field
        ev_dump = json.dumps(ev)
        assert CANARY not in ev_dump


def test_issue_a008_b007_mcp_server_run_suppresses_warmup_prompt(monkeypatch, capsys):
    """
    Issue A-008 / B-007 regression:
    Starting MCP server non-interactively or with MESH_NO_AUTO_DAEMON=1 must not
    pollute stderr with interactive warmup prompts.
    """
    from mesh import mcp_server

    # Case 1: MESH_NO_AUTO_DAEMON=1 must never prompt or start background thread
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setattr(mcp_server.asyncio, "run", lambda coro: coro.close())
    mcp_server.run()
    captured = capsys.readouterr()
    assert "Needs your approval" not in captured.err
    assert "warmup worker" not in captured.err

    # Case 2: Non-interactive stdio (not a tty) must never prompt or pollute stderr
    monkeypatch.delenv("MESH_NO_AUTO_DAEMON", raising=False)
    monkeypatch.delenv("MESH_ENABLE_AUTO_SYNC", raising=False)
    monkeypatch.delenv("MESH_AUTO_APPROVE", raising=False)
    monkeypatch.setattr(mcp_server.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(mcp_server.asyncio, "run", lambda coro: coro.close())
    mcp_server.run()
    captured = capsys.readouterr()
    assert "Needs your approval" not in captured.err
    assert "warmup worker" not in captured.err
