"""
tests/test_namespaces_cold_status.py — Regression test for cold standalone execution of
`mesh status`, `namespace_stats`, and `list_namespaces` on a clean MESH_HOME.
"""
from __future__ import annotations

import os
import subprocess
import sys
import pytest

from mesh.namespaces import list_namespaces, namespace_stats
from mesh.digest import generate_digest
from mesh.memory import Mesh


@pytest.fixture(autouse=True)
def isolated_clean_mesh(tmp_path, monkeypatch):
    mesh_home = tmp_path / "mesh_home"
    mesh_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_NO_MODEL_DOWNLOAD", "1")
    monkeypatch.setenv("MESH_NO_UPDATE_CHECK", "1")
    return mesh_home


def test_namespace_stats_cold_fresh_store(isolated_clean_mesh):
    """Verify namespace_stats succeeds on a newly initialized store."""
    m = Mesh(namespace="cold_test")
    res = m.learn("Cold memory test", confidence=0.95)
    assert "memory_id" in res

    # Call namespace_stats directly
    stats = namespace_stats("cold_test")
    assert stats["memory_count"] == 1
    assert stats["local_only_count"] == 0
    assert stats["avg_confidence"] == pytest.approx(0.95)
    assert stats["oldest_memory"] != "none"
    assert stats["newest_memory"] != "none"


def test_list_namespaces_cold_fresh_store(isolated_clean_mesh):
    """Verify list_namespaces succeeds without row index type errors."""
    m = Mesh(namespace="ns_alpha")
    res = m.learn("Alpha private fact", local_only=True)
    assert "memory_id" in res

    entries = list_namespaces()
    assert len(entries) >= 1
    ns_entry = next((e for e in entries if e["namespace"] == "ns_alpha"), None)
    assert ns_entry is not None
    assert ns_entry["memory_count"] == 1
    assert ns_entry["local_only_count"] == 1


def test_generate_digest_cold_fresh_store(isolated_clean_mesh):
    """Verify generate_digest succeeds on cold store without error."""
    m = Mesh(namespace="ns_digest")
    res = m.learn("Digest memory fact")
    assert "memory_id" in res

    digest = generate_digest("ns_digest", hours=24)
    assert digest["namespace"] == "ns_digest"
    assert digest["new_count"] == 1
    assert len(digest["read_errors"]) == 0


def test_mesh_status_cli_subprocess(isolated_clean_mesh):
    """Verify running `python -m mesh.cli status` exits with 0 and prints status."""
    m = Mesh(namespace="default")
    res = m.learn("Status test memory")
    assert "memory_id" in res

    env = os.environ.copy()
    env["MESH_HOME"] = str(isolated_clean_mesh)
    env["MESH_NO_AUTO_DAEMON"] = "1"
    env["MESH_NO_MODEL_DOWNLOAD"] = "1"

    proc = subprocess.run(
        [sys.executable, "-m", "mesh.cli", "status", "--namespace", "default"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, f"mesh status failed with code {proc.returncode}: {proc.stderr}"
    assert "default" in proc.stdout
