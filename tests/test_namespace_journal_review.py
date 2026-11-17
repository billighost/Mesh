"""Real process death tests for namespace publication and rollback recovery."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from mesh import audit, namespace_maintenance as maintenance, store as storage
from mesh.store import MeshStore
from tests.test_production_storage import fact, isolated_storage as isolated_storage


CHILD = """
import os
import sys
from pathlib import Path
project = Path(sys.argv[1])
sys.path[:0] = [str(project), str(project / '.work' / 'production-deps')]
from mesh import audit, namespace_maintenance as maintenance, store as storage
mode = sys.argv[2]
root = Path(os.environ['MESH_HOME']).resolve()
old, new = 'journal-before', 'journal-after'
def extra_write(namespace, memory_id):
    instance = storage.MeshStore(namespace, create=False)
    instance.upsert(memory_id, 'An acknowledged write survives interrupted cleanup.', None, {'embedding_model': 'needs-reembed'})
if mode in ('prepared_marker_gap', 'activation_marker_gap', 'rollback_marker_gap'):
    original_pending = maintenance._set_pending
    def interrupted_pending(path, operation_id, **kwargs):
        if mode == 'prepared_marker_gap' and path.name == 'mesh_' + old + '.db' and operation_id is not None:
            os._exit(77)
        result = original_pending(path, operation_id, **kwargs)
        if mode == 'activation_marker_gap' and path.name == 'mesh_' + new + '.db' and operation_id is None:
            extra_write(new, 'after-activation')
            os._exit(77)
        if mode == 'rollback_marker_gap' and path.name == 'mesh_' + old + '.db' and operation_id is None:
            extra_write(old, 'after-rollback')
            os._exit(77)
        return result
    maintenance._set_pending = interrupted_pending
    if mode == 'rollback_marker_gap':
        def fail_stage(*args):
            raise RuntimeError('An isolated failure starts rollback')
        maintenance._stage = fail_stage
else:
    def terminate_at_phase(phase, manifest):
        if phase == mode:
            if phase == 'activated':
                extra_write(new, 'after-activation')
            os._exit(77)
    maintenance._phase_hook = terminate_at_phase
with audit.mesh_dir_context(root):
    maintenance.rename_namespace(old, new, root)
raise SystemExit('The requested death point was not reached')
"""


def seed_namespace():
    source = MeshStore("journal-before")
    fact(source, "first", private=True)
    source.create_work_item("A task preserved across rename")
    audit.log_recall("Content-free journal fixture", source.namespace, "journal-agent", [])
    audit.flush_audit_log(source.namespace)
    identity = source.get("first")
    root = source.db_path.parent
    audit.stop_audit_worker()
    storage._close_all_pooled_connections()
    audit._close_all_audit_connections()
    return root, identity


def run_to_death(mode):
    project = Path(storage.__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(sys.path)
    completed = subprocess.run([sys.executable, "-c", CHILD, str(project), mode], env=environment, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 77, completed.stdout + completed.stderr


@pytest.mark.parametrize("mode", ["prepared", "staged", "source_hidden", "published", "activated", "prepared_marker_gap", "activation_marker_gap", "rollback_marker_gap"])
def test_namespace_journal_recovers_actual_process_death(mode):
    root, original = seed_namespace()
    run_to_death(mode)
    manifests = list(root.glob(".mesh-rename-*.json"))
    assert len(manifests) == 1
    journal = json.loads(manifests[0].read_text(encoding="utf-8"))
    maintenance.recover_namespace_renames(root)
    rollback = mode in ("prepared", "prepared_marker_gap", "rollback_marker_gap")
    active_name = "journal-before" if rollback else "journal-after"
    inactive_name = "journal-after" if rollback else "journal-before"
    survivor = MeshStore(active_name, create=False)
    first = survivor.get("first")
    assert first["incarnation"] == original["incarnation"]
    assert first["namespace"] == active_name and first["local_only"]
    assert first["store_generation"] == journal["source_generation" if rollback else "destination_generation"]
    assert survivor.count() == (2 if mode in ("activated", "activation_marker_gap", "rollback_marker_gap") else 1)
    assert not (root / f"mesh_{inactive_name}.db").exists()
    assert not (root / f"audit_{inactive_name}.db").exists()
    assert len(survivor.list_work_items()) == 1
    assert len(survivor.list_embedding_jobs()) == survivor.count()
    with survivor._read_connect() as connection:
        assert connection.execute("SELECT value FROM mesh_meta WHERE key='namespace_rename_pending'").fetchone() is None
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    assert not list(root.glob(".mesh-rename-*.json"))


def test_namespace_recovery_refuses_forged_source_generation():
    root, original = seed_namespace()
    operation_id = uuid.uuid4().hex
    fake_source = uuid.uuid4().hex
    assert fake_source != original["store_generation"]
    manifest = {"version": 1, "operation_id": operation_id, "old": "journal-before", "new": "journal-after", "source_generation": fake_source, "destination_generation": uuid.uuid4().hex, "count": 1, "had_audit": False, "phase": "prepared"}
    path = root / f".mesh-rename-{operation_id}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="identity"):
        maintenance.recover_namespace_renames(root)
    assert path.exists()
    assert (root / "mesh_journal-before.db").exists()
    assert not (root / "mesh_journal-after.db").exists()
