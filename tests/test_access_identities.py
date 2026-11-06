"""Recall statistics stay bound to the memory incarnation observed by the caller."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mesh import store as storage
from mesh.store import MeshStore
from tests.test_production_storage import isolated_storage as isolated_storage, vector


def write_fact(store, *, content="The cedar queue has two workers."):
    store.upsert("same-id", content, vector(), {"embedding_model": "onnx-minilm-l6-v2"})
    return store.get("same-id")


def test_access_counts_do_not_follow_an_id_recreated_by_another_process():
    store = MeshStore("review-access")
    old = write_fact(store)
    store._get_vector_cache()
    store.record_recall_access(["same-id"], "cedar workers", "review", 0.9, 1)
    project = Path(storage.__file__).resolve().parents[1]
    child = """
import sys
from pathlib import Path
project = Path(sys.argv[1])
sys.path.insert(0, str(project))
from mesh import audit, store as storage
s = storage.MeshStore('review-access', create=False)
s.delete('same-id')
s.upsert('same-id', 'The birch queue has three workers.', [1.0] + [0.0] * 383, {'embedding_model': 'onnx-minilm-l6-v2'})
audit.stop_audit_worker()
storage._close_all_pooled_connections()
audit._close_all_audit_connections()
"""
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = os.pathsep.join(sys.path)
    completed = subprocess.run([sys.executable, "-c", child, str(project)], timeout=20, capture_output=True, text=True, env=child_env)
    assert completed.returncode == 0, completed.stderr
    replacement = store.get("same-id")
    assert replacement["incarnation"] != old["incarnation"]
    assert replacement["access_count"] == 0
    store.flush_access_tracking()
    assert store.get("same-id")["access_count"] == 0


def test_access_forwarded_old_incarnation_cannot_count_against_replacement():
    store = MeshStore("review-late-access")
    old = write_fact(store)
    store.delete("same-id")
    new = write_fact(store, content="The birch queue has three workers.")
    store._get_vector_cache()
    store.record_recall_access(["same-id"], "cedar workers", "review", 0.9, 1, expected_incarnations={"same-id": old["incarnation"]})
    assert store.get("same-id")["access_count"] == 0
    store.flush_access_tracking()
    assert store.get("same-id")["access_count"] == 0
    store.record_recall_access(["same-id"], "birch workers", "review", 0.9, 1, expected_incarnations={"same-id": new["incarnation"]})
    assert store.get("same-id")["access_count"] == 1
    store.flush_access_tracking()
    assert store.get("same-id")["access_count"] == 1


def test_access_validated_tokens_avoid_an_extra_reader_snapshot(monkeypatch):
    store = MeshStore("review-access-fast-path")
    row = write_fact(store)
    store._get_vector_cache()

    def unexpected_reader():
        pytest.fail("A caller forwarding validated incarnation tokens should not reread SQL")

    with monkeypatch.context() as temporary_patch:
        temporary_patch.setattr(store, "_read_connect", unexpected_reader)
        store.record_recall_access(["same-id"], "cedar workers", "review", 0.9, 1, expected_incarnations={"same-id": row["incarnation"]})
    assert store.get("same-id")["access_count"] == 1


def test_access_failed_flush_does_not_overwrite_a_newer_incarnation_buffer(monkeypatch):
    store = MeshStore("review-inflight-access")
    row = write_fact(store)
    store.record_recall_access(["same-id"], "cedar workers", "review", 0.9, 1, expected_incarnations={"same-id": row["incarnation"]})
    original_begin = storage._begin_write

    def interrupted_flush(*args, **kwargs):
        monkeypatch.setattr(storage, "_begin_write", original_begin)
        store.delete("same-id")
        replacement = write_fact(store, content="The birch queue has three workers.")
        store.record_recall_access(["same-id"], "birch workers", "review", 0.9, 1, expected_incarnations={"same-id": replacement["incarnation"]})
        raise storage.sqlite3.OperationalError("Isolated old flush failure")

    monkeypatch.setattr(storage, "_begin_write", interrupted_flush)
    store.flush_access_tracking()
    assert store.get("same-id")["incarnation"] != row["incarnation"]
    assert store.get("same-id")["access_count"] == 1
    store.flush_access_tracking()
    assert store.get("same-id")["access_count"] == 1
