"""Interrupted namespace publication preserves one authoritative store and its history."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from mesh import namespace_maintenance as maintenance
from mesh import namespaces, store
from mesh.sqlite_runtime import sqlite3
from tests.test_production_storage import fact, isolated_storage as isolated_storage


def seed():
    original = store.MeshStore("rename-before")
    fact(original)
    original.create_work_item("A durable public task")
    original.create_work_item("A private task", local_only=True)
    return original


@pytest.mark.parametrize("phase", ["prepared", "staged", "source_hidden", "published", "activated"])
def test_process_death_during_rename_recovers_without_duplicate_active_names(phase):
    original = seed()
    old_generation = original.store_generation
    incarnation = original.get("fact-1")["incarnation"]
    store._close_all_pooled_connections()
    program = """
import os
import sys
from mesh import namespaces, namespace_maintenance
def crash(phase, manifest):
    if phase == sys.argv[1]:
        os._exit(73)
namespace_maintenance._phase_hook = crash
namespaces.rename_namespace('rename-before', 'rename-after')
raise RuntimeError('Crash checkpoint was not reached')
"""
    completed = subprocess.run([sys.executable, "-c", program, phase], env=os.environ.copy(),
                               capture_output=True, text=True, timeout=40)
    assert completed.returncode == 73, completed.stderr
    if phase == "prepared":
        recovered = store.MeshStore("rename-before", create=False)
        assert not (original.db_path.parent / "mesh_rename-after.db").exists()
        assert recovered.store_generation == old_generation
    else:
        recovered = store.MeshStore("rename-after", create=False)
        assert not original.db_path.exists()
        assert recovered.store_generation != old_generation
    row = recovered.get("fact-1")
    assert row["incarnation"] == incarnation
    assert row["content"].startswith("The durable fact")
    assert recovered.count() == 1
    assert len(recovered.list_work_items(include_private=True)) == 2
    assert len(recovered.list_work_items(include_private=False)) == 1
    assert len(recovered.list_embedding_jobs()) == 1
    with recovered._read_connect() as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute("SELECT value FROM mesh_meta WHERE key='namespace_rename_pending'").fetchone() is None
    assert list(original.db_path.parent.glob(".mesh-rename-*.json")) == []
    assert {entry["namespace"] for entry in namespaces.list_namespaces()} == {recovered.namespace}


def test_cleanup_failure_does_not_turn_committed_rename_into_an_error_or_block_new_writes(monkeypatch):
    original = seed()
    real_remove = namespaces._remove_sqlite_files

    def failed_cleanup(path, *args, **kwargs):
        if ".retired" in path.name:
            raise PermissionError("A temporary handle prevents retired-file cleanup")
        return real_remove(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(namespaces, "_remove_sqlite_files", failed_cleanup)
        assert namespaces.rename_namespace("rename-before", "rename-after") == 1
        renamed = store.MeshStore("rename-after", create=False)
        fact(renamed, "later-fact")
        assert renamed.count() == 2 and not original.db_path.exists()
        assert len(list(original.db_path.parent.glob(".mesh-rename-*.json"))) == 1
    reopened = store.MeshStore("rename-after", create=False)
    assert reopened.count() == 2
    assert list(original.db_path.parent.glob(".mesh-rename-*.json")) == []


def test_publication_failure_restores_original_rows_and_visibility(monkeypatch):
    original = seed()
    generation = original.store_generation
    real_replace = maintenance._durable_replace

    def refuse_publication(source, target, **kwargs):
        if target.name == "mesh_rename-after.db":
            raise PermissionError("Publication target is unavailable")
        return real_replace(source, target, **kwargs)

    monkeypatch.setattr(maintenance, "_durable_replace", refuse_publication)
    with pytest.raises(PermissionError, match="Publication target"):
        namespaces.rename_namespace("rename-before", "rename-after")
    reopened = store.MeshStore("rename-before", create=False)
    assert reopened.store_generation == generation
    assert reopened.count() == 1 and len(reopened.list_work_items(include_private=False)) == 1
    assert not (original.db_path.parent / "mesh_rename-after.db").exists()
    assert list(original.db_path.parent.glob(".mesh-rename-*.json")) == []


def test_restart_recognizes_rollback_when_phase_write_did_not_finish(monkeypatch):
    original = seed()
    real_replace = maintenance._durable_replace
    real_write = maintenance._write_manifest

    class ProcessDeath(BaseException):
        pass

    def refuse_publication(source, target, **kwargs):
        if target.name == "mesh_rename-after.db":
            raise PermissionError("Publication interrupted")
        return real_replace(source, target, **kwargs)

    def stop_after_rollback(root, manifest, phase):
        if phase == "rolled_back":
            raise ProcessDeath()
        return real_write(root, manifest, phase)

    with monkeypatch.context() as scoped:
        scoped.setattr(maintenance, "_durable_replace", refuse_publication)
        scoped.setattr(maintenance, "_write_manifest", stop_after_rollback)
        with pytest.raises(ProcessDeath):
            namespaces.rename_namespace("rename-before", "rename-after")
    reopened = store.MeshStore("rename-before", create=False)
    assert reopened.count() == 1
    assert not (original.db_path.parent / "mesh_rename-after.db").exists()
    assert list(original.db_path.parent.glob(".mesh-rename-*.json")) == []


def test_untrusted_manifest_paths_cannot_change_another_namespace():
    original = seed()
    operation_id = "f" * 32
    path = original.db_path.parent / f".mesh-rename-{operation_id}.json"
    path.write_text(json.dumps({"version": 1, "operation_id": operation_id, "old": "rename-before", "new": "../outside",
                               "source_generation": original.store_generation, "destination_generation": "e" * 32,
                               "count": 1, "had_audit": False, "phase": "staged"}), encoding="utf-8")
    with pytest.raises(ValueError):
        maintenance.recover_namespace_renames(original.db_path.parent)
    assert original.db_path.exists() and path.exists()
    connection = sqlite3.connect(original.db_path.as_uri() + "?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    finally:
        connection.close()
