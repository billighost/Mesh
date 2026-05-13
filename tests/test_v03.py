"""Tests for Mesh v0.3: privacy flags, export/import, namespace management."""

import json
import pytest
import tempfile
from pathlib import Path
from mesh import Mesh
from mesh.io import export_namespace, import_namespace
from mesh.namespaces import list_namespaces, delete_namespace, rename_namespace, namespace_stats


# --- Privacy Flag Tests ---

def test_local_only_flag_stored():
    """Memory stored with local_only=True should have local_only=True when inspected."""
    mesh = Mesh(namespace="test_privacy", agent_id="test")
    mesh.learn("my secret API key is abc123", local_only=True)
    results = mesh.recall("secret API key", n=1)
    assert len(results) > 0
    assert results[0].get("local_only") == True
    delete_namespace("test_privacy")


def test_non_local_only_is_false_by_default():
    """Memories without local_only flag should default to False."""
    mesh = Mesh(namespace="test_privacy_default", agent_id="test")
    mesh.learn("the sky is blue")
    results = mesh.recall("sky", n=1)
    assert len(results) > 0
    assert results[0].get("local_only") == False
    delete_namespace("test_privacy_default")


def test_local_only_excluded_from_export():
    """Export should never include local_only memories."""
    mesh = Mesh(namespace="test_export_privacy", agent_id="test")
    mesh.learn("public fact about the project")
    mesh.learn("private API key: sk-12345", local_only=True)
    
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        path = f.name
    
    export_namespace(namespace="test_export_privacy", output_path=path, include_embeddings=False)
    
    with open(path) as f:
        data = json.load(f)
    
    assert data["memory_count"] == 1
    assert data["skipped_local_only"] == 1
    assert all(not m["local_only"] for m in data["memories"])
    assert not any("sk-12345" in m["content"] for m in data["memories"])
    
    delete_namespace("test_export_privacy")
    Path(path).unlink()


# --- Export / Import Tests ---

def test_export_creates_valid_json():
    """Export should create a valid JSON file with correct structure."""
    mesh = Mesh(namespace="test_export", agent_id="test")
    mesh.learn("fact one")
    mesh.learn("fact two")
    
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        path = f.name
    
    export_namespace(namespace="test_export", output_path=path, include_embeddings=False)
    
    with open(path) as f:
        data = json.load(f)
    
    assert data["mesh_version"] == "0.3"
    assert data["namespace"] == "test_export"
    assert data["memory_count"] == 2
    assert len(data["memories"]) == 2
    assert "exported_at" in data
    
    for mem in data["memories"]:
        assert "id" in mem
        assert "content" in mem
        assert "memory_type" in mem
        assert "confidence" in mem
    
    delete_namespace("test_export")
    Path(path).unlink()


def test_import_restores_memories():
    """Import should restore memories from an export file."""
    # Export from source
    mesh = Mesh(namespace="test_import_src", agent_id="test")
    mesh.learn("imported fact: the answer is 42")
    
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        path = f.name
    
    export_namespace(namespace="test_import_src", output_path=path, include_embeddings=False)
    
    # Import to new namespace
    stats = import_namespace(input_path=path, target_namespace="test_import_dest")
    assert stats["imported"] == 1
    assert stats["errors"] == 0
    
    # Verify recall works in new namespace
    mesh2 = Mesh(namespace="test_import_dest", agent_id="test")
    results = mesh2.recall("the answer")
    assert len(results) > 0
    assert "42" in results[0]["content"]
    
    delete_namespace("test_import_src")
    delete_namespace("test_import_dest")
    Path(path).unlink()


def test_import_skip_existing():
    """Import with skip_existing should not overwrite memories."""
    ns = "test_import_skip_v2"
    mesh = Mesh(namespace=ns, agent_id="test")
    mesh.learn("original fact")
    
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        path = f.name
    
    export_namespace(namespace=ns, output_path=path, include_embeddings=False)
    
    # Import twice - second should skip
    stats1 = import_namespace(path, target_namespace=ns, merge_strategy="skip_existing")
    stats2 = import_namespace(path, target_namespace=ns, merge_strategy="skip_existing")
    
    assert stats1["skipped"] == 1
    assert stats2["skipped"] == 1
    
    delete_namespace(ns)
    Path(path).unlink()


def test_dry_run_doesnt_write():
    """Dry run should not write any memories."""
    mesh = Mesh(namespace="test_dry_run_src", agent_id="test")
    mesh.learn("dry run test")
    
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        path = f.name
    
    export_namespace(namespace="test_dry_run_src", output_path=path, include_embeddings=False)
    
    # Dry run to new namespace
    import_namespace(path, target_namespace="test_dry_run_dest", dry_run=True)
    
    # Verify nothing was written
    namespaces = [n["namespace"] for n in list_namespaces()]
    assert "test_dry_run_dest" not in namespaces
    
    delete_namespace("test_dry_run_src")
    Path(path).unlink()


# --- Namespace Management Tests ---

def test_list_namespaces_includes_created():
    """list_namespaces should include namespaces that have been written to."""
    mesh = Mesh(namespace="test_ns_list", agent_id="test")
    mesh.learn("something")
    
    namespaces = [n["namespace"] for n in list_namespaces()]
    assert "test_ns_list" in namespaces
    
    delete_namespace("test_ns_list")


def test_delete_namespace_removes_all():
    """delete_namespace should remove all memories and the namespace itself."""
    mesh = Mesh(namespace="test_ns_delete", agent_id="test")
    mesh.learn("memory to delete")
    
    count = delete_namespace("test_ns_delete")
    assert count >= 1
    
    namespaces = [n["namespace"] for n in list_namespaces()]
    assert "test_ns_delete" not in namespaces


def test_rename_namespace():
    """rename_namespace should move all memories to the new namespace."""
    mesh = Mesh(namespace="test_ns_rename_old", agent_id="test")
    mesh.learn("memory in old namespace")
    
    rename_namespace("test_ns_rename_old", "test_ns_rename_new")
    
    namespaces = [n["namespace"] for n in list_namespaces()]
    assert "test_ns_rename_old" not in namespaces
    assert "test_ns_rename_new" in namespaces
    
    mesh2 = Mesh(namespace="test_ns_rename_new", agent_id="test")
    results = mesh2.recall("memory in old namespace")
    assert len(results) > 0
    
    delete_namespace("test_ns_rename_new")


def test_namespace_stats_accuracy():
    """namespace_stats should return accurate counts."""
    mesh = Mesh(namespace="test_ns_stats", agent_id="test")
    mesh.learn("public fact")
    mesh.learn("private fact", local_only=True)
    
    stats = namespace_stats("test_ns_stats")
    assert stats["memory_count"] == 2
    assert stats["local_only_count"] == 1
    assert stats["exportable_count"] == 1
    
    delete_namespace("test_ns_stats")
