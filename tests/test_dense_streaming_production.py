"""Production contracts for bounded empty-query retrieval and index lifecycle."""
from __future__ import annotations

import numpy as np
import pytest

from mesh import memory as memory_module
from mesh import store as store_module


@pytest.fixture()
def isolated_dense_store(tmp_path, monkeypatch):
    for name, folder in (("MESH_HOME", "mesh"), ("HOME", "home"), ("USERPROFILE", "home"),
                         ("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata"), ("XDG_CONFIG_HOME", "xdg")):
        location = tmp_path / folder
        location.mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(location))
    for name in ("MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_FORCE_HASH_EMBED"):
        monkeypatch.setenv(name, "1")
    for name in ("MESH_BG_ONNX", "MESH_BG_ONNX_DRAIN_ON_EXIT", "MESH_AUTO_APPROVE", "MESH_VAULT_SECRETS"):
        monkeypatch.setenv(name, "0")
    for name in ("MESH_ENCRYPT_AT_REST", "MESH_ENCRYPTION_KEY", "MESH_PASSPHRASE", "MESH_AUTO_HEAL_ON_CALL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(memory_module, "_trigger_background_bootstrap", lambda: None)
    monkeypatch.setattr(memory_module, "_get_model", lambda: (memory_module._hash_embed_384, "hash-fallback"))
    yield


def _dense_vector(score):
    vector = np.zeros(384, dtype=np.float32)
    vector[0], vector[1] = score, np.sqrt(1.0 - score * score)
    return vector.tolist()


def _force_streaming(monkeypatch, store):
    def oversized():
        raise store_module.CacheBudgetExceededError("Isolated bounded retrieval condition")

    monkeypatch.setattr(store, "_get_vector_cache", oversized)


def test_empty_dense_query_materializes_only_authorized_compatible_winners(isolated_dense_store, monkeypatch):
    with memory_module.Mesh(namespace="dense-empty-bounds") as mesh:
        allowed = set()
        for index in range(12):
            identifier = f"eligible-{index:02d}"
            allowed.add(identifier)
            mesh.store.upsert(identifier, "Cedar deploy evidence. " * 50, _dense_vector(0.75 - index * 0.02),
                              {"embedding_model": "onnx-minilm-l6-v2", "tags": ["deploy"], "source_agent": "reader"})
        for identifier, metadata in (
            ("private", {"local_only": True}), ("quarantined", {"quarantined": True}),
            ("other-model", {"embedding_model": "onnx-other"}), ("other-agent", {"source_agent": "other"}),
            ("other-tag", {"tags": ["other"]}), ("stale", {"confidence": 0.1}),
        ):
            mesh.store.upsert(identifier, "Cedar excluded fixture.", _dense_vector(0.99),
                              {"embedding_model": "onnx-minilm-l6-v2", "tags": ["deploy"], "source_agent": "reader", **metadata})
        _force_streaming(monkeypatch, mesh.store)
        decoded = []
        original_deserialize = mesh.store._deserialize_memory_row

        def deserialize(row):
            decoded.append(row["id"])
            return original_deserialize(row)

        monkeypatch.setattr(mesh.store, "_deserialize_memory_row", deserialize)
        monkeypatch.setattr(store_module, "_build_row_lexical_entry", lambda *args: pytest.fail("Empty query built lexical entries"))
        results = mesh.store.hybrid_query("", np.eye(384, dtype=np.float32)[0].tolist(), n=4,
                                          query_embedding_model="onnx-minilm-l6-v2", tags=["deploy"], source_agent="reader",
                                          include_private=False, include_quarantined=False, include_stale=False, min_confidence=0.5)
        assert len(results) == 4
        assert {row["id"] for row in results}.issubset(allowed)
        assert len(decoded) == 4
        assert set(decoded) == {row["id"] for row in results}


def test_empty_dense_query_rejects_a_recreated_identity_after_ranking(isolated_dense_store, monkeypatch):
    with memory_module.Mesh(namespace="dense-empty-identity") as mesh:
        identifier = "recreated-memory"
        metadata = {"embedding_model": "onnx-minilm-l6-v2"}
        mesh.store.upsert(identifier, "Cedar original evidence.", _dense_vector(0.99), metadata)
        original_incarnation = mesh.store.get(identifier)["incarnation"]
        _force_streaming(monkeypatch, mesh.store)
        original_rank = mesh.store._rank_hybrid_results

        def rank_and_recreate(*args, **kwargs):
            results = original_rank(*args, **kwargs)
            assert results
            mesh.store.delete(identifier)
            mesh.store.upsert(identifier, "Cedar replacement evidence.", _dense_vector(0.99), metadata)
            assert mesh.store.get(identifier)["incarnation"] != original_incarnation
            return results

        monkeypatch.setattr(mesh.store, "_rank_hybrid_results", rank_and_recreate)
        results = mesh.store.hybrid_query("", np.eye(384, dtype=np.float32)[0].tolist(), n=1,
                                          query_embedding_model="onnx-minilm-l6-v2", include_private=False)
        assert results == []


def _lexical_fingerprint(cache):
    with cache.read_snapshot():
        return {
            "tokens": {token: set(ids) for token, ids in cache.token_to_ids.items()},
            "stems": {stem: set(ids) for stem, ids in cache.stem_to_ids.items()},
            "stem_df": dict(cache.stem_df), "visible_stem_df": dict(cache.visible_stem_df),
            "doc_len": cache.doc_len_total, "visible_doc_len": cache.visible_doc_len_total,
            "rows": {identifier: set(lex["indexed_tokens"]) for identifier, lex in cache.lexical_by_id.items()},
        }


def test_shared_lexical_sets_survive_updates_deletes_and_fresh_rebuilds(isolated_dense_store):
    with memory_module.Mesh(namespace="dense-lexical-lifecycle") as mesh:
        first = mesh.learn("Cedar deployments use dbCachePool for Redis.", tags=["deployments"], deduplicate=False)["memory_id"]
        second = mesh.learn("Birch deployments use worker_count=12.", tags=["routes"], deduplicate=False)["memory_id"]
        mesh.learn("Pine deployments retain Caf\u00e9 evidence.", local_only=True, deduplicate=False)
        mesh.store._get_vector_cache()
        mesh.update(first, content="Cedar now uses Valkey and api/v2.", tags=["routing"])
        mesh.store.delete(second)
        incremental = _lexical_fingerprint(mesh.store._get_vector_cache())
        store_module._evict_vector_cache_for_db_path(mesh.store.db_path)
        fresh = _lexical_fingerprint(mesh.store._get_vector_cache())
        assert incremental == fresh
        assert second not in fresh["rows"]
        assert first in fresh["tokens"]["api/v2"]


def test_empty_dense_query_supports_legacy_empty_model_inference(isolated_dense_store, monkeypatch):
    with memory_module.Mesh(namespace="dense-empty-legacy-model") as mesh:
        identifier = "legacy-empty-model"
        mesh.store.upsert(identifier, "Cedar legacy model record.", _dense_vector(0.99), {"embedding_model": "hash-384"})
        with mesh.store._connect() as connection:
            connection.execute("UPDATE memories SET embedding_model='' WHERE namespace=? AND id=?", (mesh.namespace, identifier))
        _force_streaming(monkeypatch, mesh.store)
        result = mesh.store.hybrid_query("", np.eye(384, dtype=np.float32)[0].tolist(), n=1,
                                         query_embedding_model="hash-384", include_private=False)
        assert [row["id"] for row in result] == [identifier]
        assert result[0]["embedding_model"] == "hash-384"


def test_lexical_frequency_keys_share_owned_stems_without_changing_counts():
    lex = store_module._build_row_lexical_entry(
        "Deployment deployments deployments deployment deployed deploying. "
        "Caf\u00e9 db.connectionPool supports HTTP/2.",
        ["release-deployments", "Caf\u00e9"],
    )
    assert lex["term_freq"]["deployment"] == 4
    assert lex["term_freq"]["deploy"] == 2
    assert lex["term_freq"]["caf\u00e9"] == 1
    owners = {stem: stem for stem in lex["all_doc_stems"]}
    for stem in lex["term_freq"]:
        if stem in owners:
            assert stem is owners[stem], "The row retained a duplicate frequency-key string"
