"""Tiny model-free contracts for role-aware, revision-bound E5 shadow evaluation."""
from __future__ import annotations

from collections import Counter
import copy
import json
from pathlib import Path
import re
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
import embedding_shadow as shadow
from retrieval_c4 import C4Failure
import mesh.memory as memory


class FakeRoleModel:
    model_tag = shadow.MODEL_TAG
    model_revision = shadow.REVISION
    embedding_preprocessing_identity = shadow.PROFILE_SHA256

    def __init__(self):
        self.calls = []

    def encode_for_role(self, texts, *, role, batch_size):
        self.calls.append((list(texts), role))
        vectors = np.zeros((len(texts), 384), dtype=np.float32)
        vectors[:, 0 if role == "document" else 1] = 1
        return vectors


@pytest.fixture
def fake_runtime(monkeypatch):
    model = FakeRoleModel()
    monkeypatch.setenv("MESH_FORCE_HASH_EMBED", "0")
    monkeypatch.setenv("MESH_SYNC_ONNX", "1")
    monkeypatch.setenv("MESH_PASSAGE_INDEX", "0")
    monkeypatch.setenv("MESH_AUTO_HEAL_ON_CALL", "0")
    monkeypatch.setattr(memory, "_get_model", lambda: (model, "onnx"))
    monkeypatch.setattr(memory, "_model", model)
    monkeypatch.setattr(memory, "_model_backend", "onnx")
    monkeypatch.setattr(memory, "_bg_onnx_enabled", lambda: False)
    monkeypatch.setattr(memory, "_trigger_background_bootstrap", lambda: None)
    shadow.clear_query_caches(memory)
    return model


def test_identical_text_keeps_document_query_roles_distinct(fake_runtime):
    document, document_tag = memory._embed_with_tag("  The deployment value  ")
    query, query_tag = memory._embed_query_with_tag("The deployment value")
    assert document != query and document_tag == query_tag == shadow.MODEL_TAG
    assert fake_runtime.calls == [(["The deployment value"], "document"), (["The deployment value"], "query")]
    memory._embed_query_with_tag("The deployment value")
    assert len(fake_runtime.calls) == 2


def test_profile_change_invalidates_cached_queries(fake_runtime):
    memory._embed_query_with_tag("Which value?")
    fake_runtime.embedding_preprocessing_identity = "different-preprocessing"
    memory._embed_query_with_tag("Which value?")
    assert len(fake_runtime.calls) == 2


def test_document_batches_forward_document_role(fake_runtime):
    vectors, tag = memory._encode_texts_with_tag(["first", "second"])
    assert vectors.shape == (2, 384) and tag == shadow.MODEL_TAG
    assert fake_runtime.calls == [(["first", "second"], "document")]


def test_invalid_encoding_role_fails_before_model_call(fake_runtime):
    with pytest.raises(ValueError):
        memory._encode_uncached_texts_with_tag(["text"], role="arbitrary")
    assert not fake_runtime.calls


def test_all_profile_components_affect_durable_space():
    for path in ("roles", "normalization", "content_window_tokens", "pooling", "maximum_sequence_tokens", "model"):
        changed = copy.deepcopy(shadow.PROFILE)
        changed[path] = "changed"
        assert shadow.identity(changed) != shadow.PROFILE_SHA256
    assert len(shadow.MODEL_TAG) <= 256 and shadow.PROFILE_SHA256 in shadow.MODEL_TAG


class FakeTokenizer:
    def encode(self, text, add_special_tokens=True):
        offsets = [match.span() for match in re.finditer(r"\S+", text)]
        return SimpleNamespace(offsets=offsets, ids=list(range(len(offsets) + (2 if add_special_tokens else 0))))


def test_prefix_every_window_without_losing_suffix():
    adapter = shadow.E5ShadowEmbedding.__new__(shadow.E5ShadowEmbedding)
    seen = []

    def raw(texts, batch_size):
        seen.extend(texts)
        return np.ones((len(texts), 384), dtype=np.float32)

    adapter.engine = SimpleNamespace(window_tokenizer=FakeTokenizer(), _encode_raw_batches=raw)
    adapter.memory, adapter.coverage = memory, Counter()
    text = " ".join(f"word{index}" for index in range(254))
    vectors = adapter.encode_for_role([text], role="document", batch_size=4)
    assert vectors.shape == (1, 384) and len(seen) == 2
    assert all(piece.startswith("passage: ") for piece in seen)
    assert "word253" in seen[-1]
    assert set(text.split()) <= set(" ".join(seen).split())
    assert adapter.coverage["maximum_sequence_tokens"] <= 256


def test_missing_tokenizer_coverage_fails_closed():
    tokenizer = SimpleNamespace(encode=lambda *_args, **_kwargs: SimpleNamespace(ids=[1], offsets=[(0, 3)]))
    with pytest.raises(C4Failure):
        shadow.token_windows("one LOST", tokenizer)


def test_shadow_publication_preserves_parent_identity(fake_runtime):
    fake_runtime.model_tag = "onnx-minilm-l6-v2"
    mesh = memory.Mesh(namespace="e5-contract", agent_id="test")
    try:
        learned = mesh.learn("The test configuration keeps the source value.")
        before = mesh.get(learned["memory_id"], include_private=True)
        fake_runtime.model_tag = shadow.MODEL_TAG
        records = [{"acceptance": learned}]
        indexed = shadow.reembed(mesh, records, memory)
        after = mesh.get(learned["memory_id"], include_private=True)
        assert after["embedding_model"] == shadow.MODEL_TAG
        assert all(before[key] == after[key] for key in ("content", "revision", "incarnation", "local_only", "quarantined"))
        assert indexed[0]["source_embedding_model"] == "onnx-minilm-l6-v2"
        assert mesh.store.embedding_job_stats()["pending_count"] == 0
    finally:
        mesh.close()


@pytest.mark.parametrize("stale", ["revision", "incarnation", "store_generation", "target"])
def test_stale_shadow_publication_rejected(fake_runtime, stale):
    fake_runtime.model_tag = "onnx-minilm-l6-v2"
    mesh = memory.Mesh(namespace="e5-stale", agent_id="test")
    try:
        learned = mesh.learn("Stable original value.")
        row = mesh.get(learned["memory_id"], include_private=True)
        identity = {"expected_revision": row["revision"], "expected_incarnation": row["incarnation"],
                    "expected_store_generation": mesh.store.store_generation}
        assert mesh.store.enqueue_embedding_job(row["id"], target_model=shadow.MODEL_TAG, **identity)
        if stale == "target":
            assert mesh.store.enqueue_embedding_job(row["id"], target_model="another-space", **identity)
        else:
            field = "expected_" + stale
            identity[field] = row["revision"] + 1 if stale == "revision" else "f" * 32
        assert not mesh.store.publish_embedding(row["id"], [1.0] * 384, embedding_model=shadow.MODEL_TAG,
                                                expected_model="onnx-minilm-l6-v2", **identity)
        assert mesh.get(row["id"], include_private=True)["embedding_model"] == "onnx-minilm-l6-v2"
    finally:
        mesh.close()


def test_model_space_mask_precedes_dense_ranking(fake_runtime):
    mesh = memory.Mesh(namespace="e5-mask", agent_id="test")
    try:
        fake_runtime.model_tag = "onnx-minilm-l6-v2"
        old = mesh.learn("The previous space is incompatible.")
        fake_runtime.model_tag = shadow.MODEL_TAG
        compatible = mesh.learn("The new space is compatible.")
        results = mesh.store.hybrid_query(query_text="", embedding=[1.0] + [0.0] * 383,
                                          query_embedding_model=shadow.MODEL_TAG, n=10)
        assert compatible["memory_id"] in {row["id"] for row in results}
        assert old["memory_id"] not in {row["id"] for row in results}
    finally:
        mesh.close()


def test_native_provisioning_unavailable_locally(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(C4Failure):
        shadow.provision(tmp_path, tmp_path)


def test_shadow_selection_preserves_every_ordinary_phase():
    import retrieval_ci as ci
    ordinary = ci.initial_summary("prospective")["phases"]
    selected = ci.initial_summary("prospective", e5_shadow=True)["phases"]
    assert [row for row in selected if row["name"] not in ci.E5_SHADOW_PHASES] == ordinary
    assert selected.index({"name": "e5-shadow", "status": "not_run"}) > selected.index({"name": "fresh-default", "status": "not_run"})


def test_invalid_shadow_combination_keeps_failure_artifact(monkeypatch, tmp_path):
    import retrieval_ci as ci
    monkeypatch.setattr(ci, "qualify", lambda *_args: pytest.fail("Native qualification must not start for incompatible selection"))
    args = SimpleNamespace(dataset="prospective", reranker_experiment="c4", runtime_passages=False,
                           runtime_rerank_profile="full-256", e5_shadow=True)
    assert ci.run(args, tmp_path, tmp_path) == 1
    summary = json.loads((tmp_path / "job-summary.json").read_text())
    assert summary["status"] == "failed"
    assert all(phase["status"] == "not_run" for phase in summary["phases"])


def test_cache_cold_stratum_cannot_hide_native_cost(fake_runtime, tmp_path):
    fake_runtime.session = SimpleNamespace(counts=Counter())
    original = fake_runtime.encode_for_role

    def native(texts, **kwargs):
        fake_runtime.session.counts.update(native_attempted_batches=1, native_returned_batches=1,
                                           native_attempted_windows=len(texts), native_returned_windows=len(texts))
        return original(texts, **kwargs)

    fake_runtime.encode_for_role = native
    mesh = memory.Mesh(namespace="e5-samples", agent_id="test")
    try:
        learned = mesh.learn("The deployment setting has one value.")
        records = [{"acceptance": learned, "document_id": "document-one", "faithful_public": True}]
        metadata = {"query_target": 60, "query_model_space_tag": shadow.MODEL_TAG,
                    "completed_queries": 0, "sdk_query_encoding_requests": 0}
        shadow.measure_queries(mesh, [{"id": "q1", "query": "Which deployment setting?"}], records,
                               memory, fake_runtime, tmp_path, metadata, None)
        samples = [json.loads(line) for line in (tmp_path / "shadow-resources.jsonl").read_text().splitlines()]
        normal = [row for row in samples if row["stratum"] == "normal-cache"]
        cold = [row for row in samples if row["stratum"] == "cache-cold"]
        assert normal[1]["query_vector_cache_hit"] is True
        assert all(row["native"]["native_returned_batches"] == 1 and not row["query_vector_cache_hit"] for row in cold)
        assert metadata["sdk_query_encoding_requests"] == 5
    finally:
        mesh.close()


def test_wrong_static_files_cannot_become_a_verified_model(tmp_path):
    (tmp_path / "model.onnx").write_bytes(b"arbitrary fake bytes")
    (tmp_path / "tokenizer.json").write_text("{}")
    with pytest.raises(C4Failure):
        shadow.verify_fixture(tmp_path)


def test_failed_native_measurement_keeps_only_resource_evidence(tmp_path):
    from retrieval_c4 import C4Aborted
    exc = C4Aborted("ProcessTreeRSSLimitExceeded", evidence={
        "process_tree_peak_rss_bytes": 900 * 1024**2, "process_tree_sample_count": 12,
        "rss_limit_bytes": 768 * 1024**2, "child_returncode": -1, "phase_wall_seconds": 3.5,
        "untrusted_message": "fake text that must not be exported", "private_path": "fake/path"})
    phase = shadow.failed_native_phase(exc, 4.0, 768 * 1024**2)
    assert phase == {"status": "failed", "phase_wall_seconds": 3.5, "rss_limit_bytes": 768 * 1024**2,
                     "process_tree_peak_rss_bytes": 900 * 1024**2, "process_tree_sample_count": 12,
                     "child_returncode": -1, "failure_code": "ProcessTreeRSSLimitExceeded"}
    (tmp_path / "run-manifest.json").write_text(json.dumps({"active_phase": "e5", "phases": {"e5": phase}}))
    projected = shadow.failure_projection(tmp_path)
    assert projected["active_phase"] == "e5"
    assert projected["phases"]["e5"]["failed_native_measurement"] == phase


def test_provision_failure_without_native_directory_is_visible(tmp_path):
    projected = shadow.failure_projection(tmp_path / "absent-experiment")
    assert projected["quality_scoring_status"] == "not_run_failed_or_interrupted"
    assert projected["phases"] == {phase: {"status": "not_run", "completed_queries": 0} for phase in ("baseline", "e5")}


def test_exact_parent_aliases_spans_and_privacy_cannot_be_forged():
    identifier, incarnation, generation = "a" * 32, "b" * 32, "c" * 32
    records = [{"acceptance": {"memory_id": identifier}, "current_revision": 1, "current_incarnation": incarnation,
                "store_generation": generation, "stored_characters": 40, "neural_indexed": True,
                "private": False, "quarantined": False, "faithful_public": True, "document_id": "known-evidence"}]
    row = {"id": identifier, "revision": 1, "incarnation": incarnation, "store_generation": generation,
           "embedding_model": shadow.MODEL_TAG, "faithful_document_ids": ["known-evidence"],
           "local_only": False, "quarantined": False, "selected_passage_ordinal": None,
           "supporting_spans": [{"memory_id": identifier, "revision": 1, "incarnation": incarnation,
                                 "store_generation": generation, "start": 0, "end": 40}]}
    shadow.validate_returned_parents([row], records, shadow.MODEL_TAG)
    changes = [{"faithful_document_ids": ["unrelated-gold"]}, {"selected_passage_ordinal": 0}, {"local_only": True},
               {"supporting_spans": [dict(row["supporting_spans"][0], end=41)]},
               {"supporting_spans": [dict(row["supporting_spans"][0], incarnation="d" * 32)]}]
    for change in changes:
        with pytest.raises(ValueError):
            shadow.validate_returned_parents([dict(row, **change)], records, shadow.MODEL_TAG)
    with pytest.raises(ValueError):
        shadow.validate_returned_parents([row, row], records, shadow.MODEL_TAG)


def test_default_direct_encoding_preserves_legacy_hook_kwargs(monkeypatch, fake_runtime):
    direct = memory._DirectOnnxEmbedding.__new__(memory._DirectOnnxEmbedding)
    direct.encode = lambda texts, batch_size: np.ones((len(texts), 384), dtype=np.float32)
    monkeypatch.setattr(memory, "_get_model", lambda: (direct, "onnx"))
    original = memory._encode_uncached_texts_with_tag
    calls = []

    def legacy_hook(texts, *, model_context=None):
        calls.append(list(texts))
        return original(texts, model_context=model_context)

    monkeypatch.setattr(memory, "_encode_uncached_texts_with_tag", legacy_hook)
    vector, tag = memory._embed_query_with_tag("ordinary default query")
    assert tag == "onnx-minilm-l6-v2" and len(vector) == 384 and calls == [["ordinary default query"]]


def test_native_child_failure_code_survives_sanitized_projection(tmp_path):
    phase = tmp_path / "e5"
    phase.mkdir()
    (phase / "run-manifest.json").write_text(json.dumps({"status": "failed", "failure_code": "ShadowUnexpectedModelContract",
                                                         "untrusted_message": "fake model output text must be discarded"}))
    projected = shadow.failure_projection(tmp_path)
    assert projected["phases"]["e5"]["failure_code"] == "ShadowUnexpectedModelContract"
    assert "untrusted_message" not in projected["phases"]["e5"]
    (phase / "run-manifest.json").write_text(json.dumps({"status": "failed", "failure_code": "unregistered-fake-message"}))
    assert "failure_code" not in shadow.failure_projection(tmp_path)["phases"]["e5"]


def test_shadow_baseline_export_matches_instrumented_fresh_default(tmp_path):
    fake_memory = SimpleNamespace(
        _EMBED_CACHE_LOCK=threading.RLock(), _EMBED_CACHE={}, _EMBED_CACHE_BYTES=0,
        _NORM_QUERY_CACHE_LOCK=threading.RLock(), _NORM_QUERY_CACHE={}, _NORM_QUERY_CACHE_BYTES=0,
        _embed_query_with_tag=lambda _query: ([1.0] * 384, shadow.production.MODEL_TAG),
        _encode_texts_with_tag=lambda *_args: None, _process_reembed_batch=lambda *_args: None,
    )
    model = SimpleNamespace(session=SimpleNamespace(counts=Counter()))
    row = {"id": "a" * 32, "revision": 1, "incarnation": "b" * 32, "store_generation": "c" * 32,
           "embedding_model": shadow.production.MODEL_TAG, "similarity": 0.8, "semantic_similarity": 0.75,
           "lexical_score": 0.2, "rank_score": 0.8, "bm25_score": 1.5, "ranking_mode": "calibrated",
           "retrieval_mode": "cached", "match_reason": "lexical+semantic"}

    def recall(query, **_kwargs):
        fake_memory._embed_query_with_tag(query)
        model.session.counts.update(native_attempted_batches=1, native_returned_batches=1,
                                    native_attempted_windows=1, native_returned_windows=1)
        return [copy.deepcopy(row)]

    records = [{"acceptance": {"memory_id": row["id"]}, "document_id": "evidence-one", "faithful_public": True}]
    fresh = {"query_id": "q1", "corpus_id": "complete", "candidate_cap": None,
             "query_embedding_model": shadow.production.MODEL_TAG,
             "final_sdk_top10": shadow.production.decorate_results([row], {row["id"]: records}, score_components=True)}
    metadata = {"query_target": 100, "query_model_space_tag": shadow.production.MODEL_TAG,
                "completed_queries": 0, "sdk_query_encoding_requests": 0}
    shadow.measure_queries(SimpleNamespace(recall=recall), [{"id": "q1", "query": "Which value?"}], records,
                           fake_memory, model, tmp_path, metadata, None)
    actual = list(shadow.helpers.read_jsonl(tmp_path / "results-default.jsonl"))
    assert actual == [fresh]
    assert metadata["completed_queries"] == 1 and metadata["sdk_query_encoding_requests"] == 3


@pytest.mark.parametrize("driver", ["stdlib", "patched"])
def test_shadow_snapshot_stays_immutable_across_readonly_backup_copies(tmp_path, driver):
    if driver == "stdlib":
        import sqlite3
    else:
        from mesh.sqlite_runtime import sqlite3
    source_home, snapshot = tmp_path / "source", tmp_path / "snapshot"
    source_home.mkdir()
    snapshot.mkdir()
    source = sqlite3.connect(str(source_home / "mesh_fixture.db"))
    try:
        assert source.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        source.execute("CREATE TABLE fixture(value TEXT)")
        source.execute("INSERT INTO fixture VALUES ('safe fixture')")
        source.commit()
        fingerprints = shadow.freeze_shadow_snapshots(source_home, snapshot, {"complete": "fixture"}, sqlite3)
        assert fingerprints["complete"]["wal_sha256"] is None
        for phase in ("baseline", "e5"):
            frozen = sqlite3.connect((snapshot / "mesh_fixture.db").as_uri() + "?mode=ro", uri=True)
            query_copy = sqlite3.connect(str(tmp_path / (phase + ".db")))
            try:
                frozen.backup(query_copy)
                assert query_copy.execute("SELECT value FROM fixture").fetchone()[0] == "safe fixture"
                assert query_copy.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
                query_copy.execute("PRAGMA synchronous=FULL")
                assert query_copy.execute("PRAGMA synchronous").fetchone()[0] == 2
            finally:
                query_copy.close()
                frozen.close()
            assert shadow.production.guarded_database_fingerprint(snapshot, "fixture") == fingerprints["complete"]
        assert source.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        source.close()
