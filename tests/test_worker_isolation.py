"""
tests/test_worker_isolation.py — Verify parallel worker harness directory and environment isolation.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
import pytest

from mesh.memory import Mesh


def test_parallel_worker_directory_isolation(tmp_path):
    """Verify two workers operate on completely independent databases and paths."""
    base = tmp_path / "work"
    worker_0 = base / "worker_0"
    worker_1 = base / "worker_1"

    w0_home = worker_0 / "mesh_home"
    w1_home = worker_1 / "mesh_home"
    w0_home.mkdir(parents=True, exist_ok=True)
    w1_home.mkdir(parents=True, exist_ok=True)

    # Worker 0 creates store and learns fact A
    os.environ["MESH_HOME"] = str(w0_home)
    os.environ["MESH_NO_AUTO_DAEMON"] = "1"
    os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
    mesh_0 = Mesh(namespace="iso_test", agent_id="w0")
    mesh_0.learn("Worker 0 secret data: Alpha", tags=["worker_0_tag"])
    assert mesh_0.store.count() == 1

    # Worker 1 creates store and learns fact B
    os.environ["MESH_HOME"] = str(w1_home)
    mesh_1 = Mesh(namespace="iso_test", agent_id="w1")
    mesh_1.learn("Worker 1 secret data: Beta", tags=["worker_1_tag"])
    assert mesh_1.store.count() == 1

    # Verify worker 0 cannot see worker 1's data
    os.environ["MESH_HOME"] = str(w0_home)
    mesh_0_verify = Mesh(namespace="iso_test", agent_id="w0", create=False)
    results_0 = mesh_0_verify.recall("secret data", n=10)
    w0_contents = [r["content"] for r in results_0]
    assert any("Alpha" in c for c in w0_contents)
    assert not any("Beta" in c for c in w0_contents)

    # Verify worker 1 cannot see worker 0's data
    os.environ["MESH_HOME"] = str(w1_home)
    mesh_1_verify = Mesh(namespace="iso_test", agent_id="w1", create=False)
    results_1 = mesh_1_verify.recall("secret data", n=10)
    w1_contents = [r["content"] for r in results_1]
    assert any("Beta" in c for c in w1_contents)
    assert not any("Alpha" in c for c in w1_contents)
