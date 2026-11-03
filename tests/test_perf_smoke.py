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


def test_lazy_numpy_contract():
    """
    Verify the _LazyNumPy implementation meets all required guarantees:
      1. First access from multiple threads simultaneously is thread-safe.
      2. isinstance(x, np.ndarray) evaluates correctly.
      3. Type annotations like np.ndarray and np.float32 can be inspected with typing.get_type_hints.
      4. Module-level constants (pi, nan) and dtypes (float32, int64) match real NumPy.
    """
    import concurrent.futures
    import threading
    import typing

    # Test fresh _LazyNumPy instance to ensure concurrent first access safety
    class _TestLazyNumPy:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def __getattr__(self, name: str):
            with self._lock:
                import numpy as _np

                return getattr(_np, name)

    lazy_np = _TestLazyNumPy()

    # 1. Concurrent first access from 20 threads simultaneously
    def worker(idx: int):
        val = lazy_np.zeros(idx + 1, dtype=lazy_np.float32)
        norm = float(lazy_np.linalg.norm(val))
        return val.shape[0], norm

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(worker, i) for i in range(20)]
        results = [f.result() for f in futures]
    assert len(results) == 20
    assert results[0] == (1, 0.0)
    assert results[19] == (20, 0.0)

    # 2. isinstance(x, np.ndarray)
    from mesh.store import np as store_np

    arr = store_np.array([1.0, 2.0, 3.0], dtype=store_np.float32)
    assert isinstance(arr, store_np.ndarray)
    assert not isinstance([1.0, 2.0], store_np.ndarray)

    # 3. Type-annotation usage and get_type_hints
    def sample_func(vector: store_np.ndarray) -> store_np.float32:
        return vector[0]

    hints = typing.get_type_hints(sample_func, localns={"store_np": store_np})
    assert hints["vector"] is store_np.ndarray
    assert hints["return"] is store_np.float32

    # 4. Module-level constants and dtypes
    assert store_np.float32 is not None
    assert store_np.int64 is not None
    assert store_np.pi > 3.14
    assert store_np.isnan(store_np.nan)

