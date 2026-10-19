"""
tests/test_perf_smoke.py — Fast performance smoke test for Mesh.
Verifies that core operations meet generous upper-bound latency budgets in CI.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pytest

from mesh.memory import Mesh


@pytest.fixture
def temp_mesh_dir(monkeypatch):
    temp_dir = tempfile.mkdtemp(prefix="mesh_perf_smoke_")
    monkeypatch.setenv("MESH_HOME", temp_dir)
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_core_operations_latency_budget(temp_mesh_dir):
    """
    Ensure essential memory, task, brief, and changelog operations complete
    well within generous safety thresholds (preventing runaway performance regressions).
    """
    mesh = Mesh(namespace="perf_smoke_ns", agent_id="smoke-agent")

    # 1. Learn write latency (< 2.0s even on slow virtualized CI)
    t0 = time.perf_counter()
    m = mesh.learn(
        content="PostgreSQL runs on port 5432 in production",
        memory_type="fact",
        tags=["infra", "db"],
    )
    learn_ms = (time.perf_counter() - t0) * 1000.0
    assert m is not None
    assert learn_ms < 2000.0, f"mesh.learn exceeded budget: {learn_ms:.2f}ms"

    # 2. Recall query latency (< 800ms)
    t0 = time.perf_counter()
    res = mesh.recall("PostgreSQL port", n=5)
    recall_ms = (time.perf_counter() - t0) * 1000.0
    assert len(res) >= 1
    assert recall_ms < 800.0, f"mesh.recall exceeded budget: {recall_ms:.2f}ms"

    # 3. Work item create latency (< 300ms)
    t0 = time.perf_counter()
    task = mesh.create_work_item(
        title="Migrate database cluster to PostgreSQL 16",
        kind="task",
        priority="high",
        tags=["db", "upgrade"],
    )
    create_ms = (time.perf_counter() - t0) * 1000.0
    assert task["short_id"].startswith("task-")
    assert create_ms < 300.0, f"mesh.create_work_item exceeded budget: {create_ms:.2f}ms"

    # 4. Work item complete latency (< 300ms)
    t0 = time.perf_counter()
    completed = mesh.complete_work_item(
        item_id=task["id"],
        summary="Database upgraded and verified",
        evidence=["db/migration.sql"],
    )
    complete_ms = (time.perf_counter() - t0) * 1000.0
    assert completed["status"] == "done"
    assert complete_ms < 300.0, f"mesh.complete_work_item exceeded budget: {complete_ms:.2f}ms"

    # 5. Project brief latency (< 400ms)
    t0 = time.perf_counter()
    brief = mesh.project_brief(max_tokens=1000)
    brief_ms = (time.perf_counter() - t0) * 1000.0
    assert "brief_markdown" in brief
    assert brief_ms < 400.0, f"mesh.project_brief exceeded budget: {brief_ms:.2f}ms"

    # 6. Changelog latency (< 300ms)
    t0 = time.perf_counter()
    log = mesh.changelog(since="7d", format="markdown")
    log_ms = (time.perf_counter() - t0) * 1000.0
    assert "PostgreSQL" in log["changelog_markdown"]
    assert log_ms < 300.0, f"mesh.changelog exceeded budget: {log_ms:.2f}ms"
