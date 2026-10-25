"""Stage-2 (background ONNX upgrade) worker: correctness, safety and non-interference. Uses a fake ONNX backend."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import numpy as np
import pytest

from mesh import memory as mem

ONNX = "onnx-minilm-l6-v2"


def _fake_vec(text: str) -> np.ndarray:
    seed = int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")
    v = np.random.default_rng(seed).standard_normal(384).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_HOME", str(tmp_path))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_NO_MODEL_DOWNLOAD", "1")
    monkeypatch.setenv("MESH_BG_ONNX_IDLE_MS", "0")
    monkeypatch.delenv("MESH_FORCE_HASH_EMBED", raising=False)
    monkeypatch.delenv("MESH_SYNC_ONNX", raising=False)
    encoded: list[str] = []

    def fake_encode(texts):
        encoded.extend(texts)
        return np.vstack([_fake_vec(t) for t in texts]), ONNX

    monkeypatch.setattr(mem, "_has_local_onnx_pair", lambda: (Path("m.onnx"), Path("t.json")))
    monkeypatch.setattr(mem, "_encode_texts_with_tag", fake_encode)
    monkeypatch.setattr(mem, "_get_model", lambda: (mem._hash_embed_384, "hash-fallback"))
    mem._PENDING_REEMBED_QUEUE.clear()
    mem._REEMBED_STORES.clear()
    yield encoded
    mem.drain_reembed_worker(5.0)
    mem._PENDING_REEMBED_QUEUE.clear()


def _row(m, mid):
    return m.store.get(mid)


def _vector(m, mid) -> np.ndarray:
    """The stored (unit-length) vector for `mid`, read from the namespace's vector cache."""
    cache = m.store._get_vector_cache()
    return np.asarray(cache.matrix[cache.id_to_idx[mid]], dtype=np.float32)


def test_row_is_upgraded_to_onnx_in_background(env, monkeypatch):
    monkeypatch.setenv("MESH_BG_ONNX_IDLE_MS", "400")  # keep the worker parked so we can observe stage 1
    m = mem.Mesh(namespace="s2", agent_id="t", create=True, trusted_local=True)
    res = m.learn("The cache TTL is 300 seconds")
    mid = res["memory_id"]
    assert _row(m, mid)["embedding_model"] == "hash-384"  # stage 1 returned immediately with the fast vector
    assert mem.drain_reembed_worker(10.0) is True
    row = _row(m, mid)
    assert row["embedding_model"] == ONNX
    assert np.allclose(_vector(m, mid), _fake_vec("The cache TTL is 300 seconds"), atol=1e-5)
    assert len(mem._PENDING_REEMBED_QUEUE) == 0


def test_recall_still_works_before_and_after_upgrade(env):
    m = mem.Mesh(namespace="s2r", agent_id="t", create=True, trusted_local=True)
    m.learn("Redis runs on port 6379")
    before = [r["content"] for r in m.recall("redis port")]
    mem.drain_reembed_worker(10.0)
    after = [r["content"] for r in m.recall("redis port")]
    assert before and after and before[0] == after[0] == "Redis runs on port 6379"


def test_edit_before_upgrade_never_gets_stale_vector(env, monkeypatch):
    monkeypatch.setenv("MESH_BG_ONNX_IDLE_MS", "400")
    m = mem.Mesh(namespace="s2e", agent_id="t", create=True, trusted_local=True)
    mid = m.learn("old wording of the fact")["memory_id"]
    m.update(mid, content="completely new wording")
    mem.drain_reembed_worker(10.0)
    row = _row(m, mid)
    assert row["content"] == "completely new wording"
    assert row["embedding_model"] == ONNX
    assert np.allclose(_vector(m, mid), _fake_vec("completely new wording"), atol=1e-5)
    assert "old wording of the fact" not in env  # the stale text was never encoded (queue is deduped by id)


def test_forget_before_upgrade_is_safe_and_scrubbed(env, monkeypatch):
    monkeypatch.setenv("MESH_BG_ONNX_IDLE_MS", "400")
    m = mem.Mesh(namespace="s2f", agent_id="t", create=True, trusted_local=True)
    mid = m.learn("temporary fact to forget")["memory_id"]
    assert m.forget(mid) is True
    assert mem.drain_reembed_worker(10.0) is True
    assert _row(m, mid) is None
    assert "temporary fact to forget" not in env  # forgotten content is never sent through the model


def test_no_worker_without_installed_model(env, monkeypatch):
    monkeypatch.setattr(mem, "_has_local_onnx_pair", lambda: None)
    mem._REEMBED_WORKER = None
    m = mem.Mesh(namespace="s2n", agent_id="t", create=True, trusted_local=True)
    mid = m.learn("no model installed")["memory_id"]
    time.sleep(0.2)
    assert _row(m, mid)["embedding_model"] == "hash-384"
    assert mem._REEMBED_WORKER is None or not mem._REEMBED_WORKER.is_alive()
    assert mem.drain_reembed_worker(0.1) is True  # nothing to wait for


def test_opt_out_env(env, monkeypatch):
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    m = mem.Mesh(namespace="s2o", agent_id="t", create=True, trusted_local=True)
    mid = m.learn("opted out")["memory_id"]
    time.sleep(0.2)
    assert _row(m, mid)["embedding_model"] == "hash-384"


def test_close_drains_when_model_is_resident(env, monkeypatch):
    monkeypatch.setenv("MESH_BG_ONNX_IDLE_MS", "400")
    monkeypatch.setattr(mem, "_model", object())
    monkeypatch.setattr(mem, "_model_backend", "onnx")
    m = mem.Mesh(namespace="s2c", agent_id="t", create=True, trusted_local=True)
    mid = m.learn("drain me on close")["memory_id"]
    m.close()
    assert _row(m, mid)["embedding_model"] == ONNX


def test_close_does_not_load_a_model_by_default(env, monkeypatch):
    monkeypatch.setenv("MESH_BG_ONNX_IDLE_MS", "5000")
    monkeypatch.setattr(mem, "_model", None)
    m = mem.Mesh(namespace="s2d", agent_id="t", create=True, trusted_local=True)
    m.learn("one-shot cli call")
    t0 = time.perf_counter()
    m.close()
    assert time.perf_counter() - t0 < 1.0  # no drain: the row simply stays hash-tagged until healed
    monkeypatch.setenv("MESH_BG_ONNX_DRAIN_ON_EXIT", "1")
    t0 = time.perf_counter()
    m.close()
    assert len(mem._PENDING_REEMBED_QUEUE) == 0  # explicit opt-in drains


def test_foreground_is_not_blocked_by_a_slow_forward_pass(env, monkeypatch):
    def slow_encode(texts):
        time.sleep(0.4)
        return np.vstack([_fake_vec(t) for t in texts]), ONNX

    monkeypatch.setattr(mem, "_encode_texts_with_tag", slow_encode)
    m = mem.Mesh(namespace="s2p", agent_id="t", create=True, trusted_local=True)
    m.learn("first")
    time.sleep(0.05)  # worker is now inside the 0.4 s "forward pass"
    worst = 0.0
    for i in range(15):
        t0 = time.perf_counter()
        m.learn(f"foreground memory number {i}")
        m.recall("foreground memory")
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.15, worst
    assert mem.drain_reembed_worker(20.0) is True
