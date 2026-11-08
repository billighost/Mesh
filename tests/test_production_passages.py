"""Tiny synthetic passage invariants; no model, provider, corpus or daemon is used."""
from __future__ import annotations

import gc
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from mesh import audit, memory
from mesh import store as storage
from mesh.crypto import enable_encryption_at_rest, is_encrypted_blob, is_encrypted_text
from mesh.namespaces import rename_namespace
from mesh.passage_search import verify_passage_rows
from mesh.passages import PASSAGE_FORMAT_VERSION, split_passages, validate_spans
from mesh.sqlite_runtime import sqlite3
from mesh.store import CacheBudgetExceededError, MeshStore

MODEL = "onnx-minilm-l6-v2"


class WordTokenizer:
    def encode(self, text, add_special_tokens=False):
        spans = [(match.start(), match.end()) for match in re.finditer(r"[\w.!?]+", text)]
        return SimpleNamespace(ids=list(range(len(spans))), offsets=spans)


@pytest.fixture(autouse=True)
def isolated_passages(monkeypatch):
    for name in ("MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_NO_OUTBOX_WORKER"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    monkeypatch.setenv("MESH_PASSAGE_INDEX", "1")
    monkeypatch.setenv("MESH_FORCE_HASH_EMBED", "1")
    monkeypatch.setattr(memory, "_trigger_background_bootstrap", lambda: None)
    monkeypatch.setattr(audit, "_ensure_audit_drain_thread", lambda: None)
    storage._VECTOR_CACHE.clear()
    storage._CACHE_REJECTIONS.clear()
    yield
    audit.stop_audit_worker()
    storage._close_all_pooled_connections()
    audit._close_all_audit_connections()
    storage._VECTOR_CACHE.clear()
    storage._CACHE_REJECTIONS.clear()
    memory._REEMBED_STORES.clear()
    gc.collect()


@pytest.fixture
def local_encoder(monkeypatch):
    # Construct only a fake Python object: its constructor never loads ONNX.
    encoder = object.__new__(memory._DirectOnnxEmbedding)
    encoder.window_tokenizer = WordTokenizer()
    calls = []

    def encode_raw(texts, batch_size=32):
        calls.extend(texts)
        return np.vstack([memory._hash_embed_384(text) for text in texts])

    encoder._encode_raw_batches = encode_raw
    monkeypatch.setattr(memory, "_model", encoder)
    monkeypatch.setattr(memory, "_model_backend", "direct-onnx")
    monkeypatch.setattr(memory, "_get_model", lambda: (encoder, "direct-onnx"))
    return encoder, calls


def long_content(fact="Aurora retention requires blue envelopes."):
    return " ".join(f"archive{i}" for i in range(140)) + "\n\n" + " ".join(f"ledger{i}" for i in range(140)) + "\n\n" + fact


def put(store, identifier="fact-1", content=None, **metadata):
    content = content or long_content()
    metadata = {"source_agent": "test-agent", "embedding_model": MODEL, **metadata}
    store.upsert(identifier, content, memory._hash_embed_384(content), metadata)
    return store.get(identifier)


def expected(row):
    return {"expected_revision": row["revision"], "expected_incarnation": row["incarnation"], "expected_store_generation": row["store_generation"]}


def run_jobs(store):
    jobs = store.list_embedding_jobs(8)
    hints = [memory._ReembedHint(str(store.db_path.parent), store.namespace, job["memory_id"], job["revision"], job["target_model"], job["attempts"],
                                 incarnation=job["incarnation"], store_generation=job["store_generation"], target_representation=job["target_representation"]) for job in jobs]
    memory._process_reembed_batch(hints)


def test_passage_inference_packs_parents_with_exact_source_mapping(local_encoder):
    store = MeshStore("passage-packed")
    parents = [put(store, f"long-{number}", long_content(f"Aurora policy {number} requires blue envelopes.")) for number in range(6)]
    short = put(store, "short", "Aurora short policy requires green envelopes.")
    batches = []
    original = local_encoder[0]._encode_raw_batches

    def observe(texts, batch_size=32):
        batches.append((len(texts), batch_size))
        return original(texts, batch_size)

    local_encoder[0]._encode_raw_batches = observe
    run_jobs(store)
    assert batches == [(16, 16), (2, 16)]
    expected_texts = [" ".join(parent["content"][start:end].split()) for parent in parents
                      for start, end in split_passages(parent["content"], local_encoder[0].window_tokenizer)]
    assert local_encoder[1] == expected_texts
    previous_vectors = {}
    for parent in parents:
        spans = split_passages(parent["content"], local_encoder[0].window_tokenizer)
        per_parent = np.vstack([memory._hash_embed_384(" ".join(parent["content"][start:end].split())) for start, end in spans])
        previous_vectors[parent["id"]] = memory._validated_embedding_rows(per_parent, len(spans))
    rows = passage_rows(store)
    assert len(rows) == 18
    for (parent_id, revision, incarnation, ordinal, start, end, model, representation, blob), text in zip(rows, expected_texts):
        parent = parents[int(parent_id.rsplit("-", 1)[1])]
        assert (revision, incarnation, model, representation) == (parent["revision"], parent["incarnation"], MODEL, PASSAGE_FORMAT_VERSION)
        assert (start, end) == split_passages(parent["content"], local_encoder[0].window_tokenizer)[ordinal]
        np.testing.assert_array_equal(storage._blob_to_vector(blob), storage._normalize_vector(previous_vectors[parent_id][ordinal]))
    assert store.get_indexing_status([short["id"]])[short["id"]]["passage_indexing_state"] == "indexed"
    assert store.embedding_job_stats()["pending_count"] == 0


def test_packed_inference_revalidates_each_parent_after_edit(local_encoder):
    store = MeshStore("passage-packed-cas")
    changed = put(store, "changed")
    survivor = put(store, "survivor", long_content("Aurora survivor policy requires green envelopes."))
    original = local_encoder[0]._encode_raw_batches

    def concurrent_edit(texts, batch_size=32):
        store.update(changed["id"], content="A replacement accepted source.")
        return original(texts, batch_size)

    local_encoder[0]._encode_raw_batches = concurrent_edit
    run_jobs(store)
    rows = passage_rows(store)
    assert len(rows) == 3 and all(row[0] == survivor["id"] for row in rows)
    assert store.get(changed["id"])["passage_format_version"] == ""
    assert store.get_indexing_status([survivor["id"]])[survivor["id"]]["passage_indexing_state"] == "indexed"
    assert store.embedding_job_stats()["pending_count"] == 1


@pytest.mark.parametrize("invalid", ["count", "nonfinite"])
def test_late_packed_inference_failure_publishes_no_partial_bundle(local_encoder, invalid):
    store = MeshStore("passage-packed-failure")
    for number in range(6):
        put(store, f"long-{number}", long_content(f"Aurora policy {number} requires blue envelopes."))
    original = local_encoder[0]._encode_raw_batches
    calls = 0

    def fail_later(texts, batch_size=32):
        nonlocal calls
        calls += 1
        vectors = original(texts, batch_size)
        if calls == 2:
            if invalid == "count":
                return vectors[:-1]
            vectors[-1, 0] = np.nan
        return vectors

    local_encoder[0]._encode_raw_batches = fail_later
    baseline = storage.get_vector_cache_stats()["charged_bytes"]
    run_jobs(store)
    assert calls == 2 and passage_rows(store) == []
    with store._read_connect() as connection:
        jobs = connection.execute("SELECT attempts,error_code FROM embedding_jobs ORDER BY memory_id").fetchall()
    assert len(jobs) == 6 and all(tuple(row) == (1, "embedding_failed") for row in jobs)
    assert storage.get_vector_cache_stats()["charged_bytes"] == baseline


def passage_rows(store):
    with store._read_connect() as connection:
        return [tuple(row) for row in connection.execute("SELECT parent_id,parent_revision,parent_incarnation,ordinal,start,end,embedding_model,representation_version,embedding FROM memory_passages ORDER BY parent_id,ordinal")]


def query(store, **kwargs):
    return store.hybrid_query("aurora blue retention", memory._hash_embed_384("aurora blue retention"), n=3, query_embedding_model=MODEL, include_private=False, **kwargs)


def test_short_reuse_and_original_late_span(local_encoder):
    store = MeshStore("passage-short")
    short = put(store, "short", "Aurora retention requires blue envelopes.")
    assert store.get_indexing_status(["short"])["short"]["passage_indexing_state"] == "pending"
    run_jobs(store)
    assert local_encoder[1] == []
    assert passage_rows(store) == []
    assert store.get_indexing_status(["short"])["short"]["passage_indexing_state"] == "indexed"
    parent = put(store, "long")
    run_jobs(store)
    spans = passage_rows(store)
    assert len(spans) == 3 and len(local_encoder[1]) == 3
    assert store.get("long")["content"] == parent["content"]
    validate_spans(parent["content"], [(row[4], row[5]) for row in spans])
    result = next(row for row in query(store) if row["id"] == "long")
    span = result["supporting_spans"][0]
    assert result["selected_passage_ordinal"] == 2
    assert span["start"] > len(parent["content"]) // 2
    assert span["text"] == parent["content"][span["start"]:span["end"]]
    assert (span["revision"], span["incarnation"], span["store_generation"]) == (parent["revision"], parent["incarnation"], parent["store_generation"])


@pytest.mark.parametrize("mode", ["calibrated", "rrf"])
def test_all_short_ready_reuse_uses_body_only_scoring_before_and_after_long_parent(local_encoder, monkeypatch, mode):
    monkeypatch.setenv("MESH_RECALL_FUSION", mode)
    store = MeshStore("passage-ready-short")
    parent = put(store, "short", "Unrelated source remains intact.", tags=["aurora", "blue", "retention"])
    assert query(store)[0]["lexical_score"] > 0.0
    run_jobs(store)
    assert store.get_indexing_status(["short"])["short"]["passage_indexing_state"] == "indexed"
    assert local_encoder[1] == [] and passage_rows(store) == []
    cache = store._get_vector_cache()
    assert cache.passages.matrix.shape == (0, 384) and cache.passages.offsets.shape == (0, 2)
    assert cache.passages.ready_primary_ids == {"short"}
    assert cache.passages.allocated_bytes >= storage._object_graph_bytes(cache.passages.ready_primary_ids) + storage._object_graph_bytes(cache.passages.implicit_lexical)
    assert storage.get_vector_cache_stats()["charged_bytes"] >= cache._cache_bytes
    del cache

    def checked_result(streaming):
        with monkeypatch.context() as context:
            if streaming:
                context.setattr(store, "_get_vector_cache", lambda *args, **kwargs: (_ for _ in ()).throw(CacheBudgetExceededError("forced")))
            result = next(row for row in query(store) if row["id"] == "short")
        assert result["lexical_score"] == result["bm25_score"] == 0.0
        assert result["supporting_spans"][0]["text"] == parent["content"]
        return {key: value for key, value in result.items() if key not in ("retrieval_mode", "retrieval_degradation_reason", "candidate_count")}

    before = [checked_result(streaming) for streaming in (False, True)]
    assert before[0] == before[1]
    put(store, "long", long_content("Another unrelated source remains intact."))
    run_jobs(store)
    assert len(passage_rows(store)) == 3
    after = [checked_result(streaming) for streaming in (False, True)]
    assert after[0] == after[1]
    # An additional eligible parent can change RRF channel ranks.
    ignored = {"rank_score"} if mode == "rrf" else set()
    assert [{key: value for key, value in row.items() if key not in ignored} for row in after] == [{key: value for key, value in row.items() if key not in ignored} for row in before]


@pytest.mark.parametrize("invalid", ["pending", "missing", "malformed", "unknown", "hash-384", "needs-reembed", "orphan"])
def test_incomplete_or_corrupt_primary_reuse_cannot_activate_passage_scoring(local_encoder, monkeypatch, invalid):
    store = MeshStore("passage-bad-reuse")
    parent = put(store, "short", "Unrelated source remains intact.", tags=["aurora", "blue", "retention"])
    run_jobs(store)
    with store.atomic_write(), store._connect() as connection:
        if invalid == "pending":
            connection.execute("UPDATE memories SET passage_format_version='' WHERE id='short'")
        elif invalid in ("missing", "malformed"):
            connection.execute("UPDATE memories SET embedding=? WHERE id='short'", (None if invalid == "missing" else b"bad",))
        elif invalid == "orphan":
            connection.execute("INSERT INTO memory_passages VALUES(?,?,?,?,?,?,?,?,?,?)", (parent["id"], store.namespace, parent["revision"], parent["incarnation"], 0, 0, len(parent["content"]), MODEL, PASSAGE_FORMAT_VERSION, memory._hash_embed_384(parent["content"]).tobytes()))
        else:
            # A dense unknown vector would otherwise be inferred as neural in cache.
            vector = storage._normalize_vector(np.ones(384, dtype=np.float32))
            connection.execute("UPDATE memories SET embedding_model=?,embedding=? WHERE id='short'", (invalid, vector.tobytes()))
        store._bump_content_version_in_tx(connection)
        store._invalidate_cache()
    assert store.get_indexing_status(["short"])["short"]["passage_indexing_state"] == "pending"
    for streaming in (False, True):
        with monkeypatch.context() as context:
            if streaming:
                context.setattr(store, "_get_vector_cache", lambda *args, **kwargs: (_ for _ in ()).throw(CacheBudgetExceededError("forced")))
            context.setenv("MESH_PASSAGE_INDEX", "0")
            expected_result = query(store)
            context.setenv("MESH_PASSAGE_INDEX", "1")
            actual = query(store)
        assert actual == expected_result and actual[0]["lexical_score"] > 0.0


def test_warm_reuse_cache_tracks_added_pending_tags_completion_and_deletion(local_encoder, monkeypatch):
    store = MeshStore("passage-reuse-deltas")
    put(store, "ready", "First unrelated source.")
    run_jobs(store)
    put(store, "pending", "Second unrelated source.")
    query(store)
    store.update("pending", tags=["aurora", "blue", "retention"])
    cached = query(store)
    assert all(row["lexical_score"] == row["bm25_score"] == 0.0 for row in cached)
    with monkeypatch.context() as context:
        context.setattr(store, "_get_vector_cache", lambda *args, **kwargs: (_ for _ in ()).throw(CacheBudgetExceededError("forced")))
        streamed = query(store)
    fields = ("id", "similarity", "lexical_score", "bm25_score", "rank_score", "supporting_spans")
    assert [{key: row[key] for key in fields} for row in cached] == [{key: row[key] for key in fields} for row in streamed]
    run_jobs(store)
    assert store._get_vector_cache().passages.ready_primary_ids == {"ready", "pending"}
    assert store.delete("ready") and store.delete("pending")
    assert store._get_vector_cache().passages.ready_primary_ids == set()


def test_boundaries_repeated_paragraphs_ignored_characters_and_short_normalization():
    tokenizer = WordTokenizer()
    content = "§ " + " ".join(f"word{i}" for i in range(480)) + " §\n\n" + "Late support remains intact."
    spans = split_passages(content, tokenizer)
    validate_spans(content, spans)
    assert spans[0][0] == 0 and spans[-1][1] == len(content)
    assert any(spans[index][0] < spans[index - 1][1] for index in range(1, len(spans)))
    repeated = " ".join(f"word{i}" for i in range(130))
    assert len(split_passages(repeated + "\r\n\r\n" + repeated, tokenizer)) == 2
    assert split_passages("  A short\nsource.  ", tokenizer) == [(0, 19)]

    class NormalizationChangesTokens:
        def encode(self, text, add_special_tokens=False):
            return SimpleNamespace(ids=list(range(255 if text == "a b" else 2)), offsets=[(0, 1), (2, 3)])

    with pytest.raises(ValueError, match="Normalized"):
        split_passages("a\n b", NormalizationChangesTokens())


@pytest.mark.parametrize("mode", ["calibrated", "rrf"])
def test_parent_collapse_cache_streaming_parity_and_policy(local_encoder, monkeypatch, mode):
    monkeypatch.setenv("MESH_RECALL_FUSION", mode)
    store = MeshStore("passage-parity")
    put(store, "a", tags=["visible"])
    put(store, "b", tags=["visible"])
    put(store, "private", local_only=True, source_agent="other")
    run_jobs(store)
    cached = query(store, tags=["visible"], source_agent="test-agent")
    assert [row["id"] for row in cached] == ["a", "b"]
    cache = store._get_vector_cache()
    assert cache.passages.matrix.dtype == np.float32 and cache.passages.matrix.flags.c_contiguous
    assert cache.passages.offsets.dtype == np.int64
    assert cache.passages.allocated_bytes >= cache.passages.matrix.nbytes + cache.passages.offsets.nbytes
    assert storage.get_vector_cache_stats()["charged_bytes"] <= storage.get_vector_cache_stats()["limit_bytes"]

    def no_cache(*args, **kwargs):
        raise CacheBudgetExceededError("forced synthetic cache admission failure")

    monkeypatch.setattr(store, "_get_vector_cache", no_cache)
    streamed = query(store, tags=["visible"], source_agent="test-agent")
    fields = ("id", "rank_score", "similarity", "semantic_similarity", "lexical_score", "bm25_score", "candidate_count", "selected_passage_ordinal", "supporting_spans")
    assert [{key: row.get(key) for key in fields} for row in cached] == [{key: row.get(key) for key in fields} for row in streamed]
    assert all(row["retrieval_mode"] == "streaming" for row in streamed)
    assert len(passage_rows(store)) == 9


@pytest.mark.parametrize("mode", ["calibrated", "rrf"])
def test_winning_dense_sibling_reports_its_own_zero_bm25(local_encoder, monkeypatch, mode):
    monkeypatch.setenv("MESH_RECALL_FUSION", mode)
    store = MeshStore("passage-winner-evidence")
    put(store)
    run_jobs(store)
    vector = memory._hash_embed_384("aurora blue retention")
    with store.atomic_write(), store._connect() as connection:
        connection.execute("UPDATE memory_passages SET embedding=? WHERE parent_id='fact-1' AND ordinal=0", (vector.tobytes(),))
        store._bump_content_version_in_tx(connection)
        store._invalidate_cache()
    cached = query(store)[0]
    monkeypatch.setattr(store, "_get_vector_cache", lambda *args, **kwargs: (_ for _ in ()).throw(CacheBudgetExceededError("forced")))
    streamed = query(store)[0]
    for result in (cached, streamed):
        assert result["selected_passage_ordinal"] == 0
        assert result["semantic_similarity"] == 1.0
        assert result["lexical_score"] == result["bm25_score"] == 0.0
        assert "aurora" not in result["supporting_spans"][0]["text"].lower()


@pytest.mark.parametrize("hidden", ["private", "quarantine", "agent", "type", "tags", "expired", "confidence", "model", "invalid"])
@pytest.mark.parametrize("reuse_primary", [False, True])
def test_hidden_only_derived_publication_cannot_change_public_scorer(local_encoder, monkeypatch, hidden, reuse_primary):
    store = MeshStore("passage-hidden-gate")
    put(store, "public", "Aurora retention requires blue envelopes.", tags=["visible"])
    metadata = {"tags": ["visible"]}
    metadata.update({
        "private": {"local_only": True},
        "quarantine": {"quarantined": True},
        "agent": {"source_agent": "other-agent"},
        "type": {"memory_type": "decision"},
        "tags": {"tags": ["hidden"]},
        "expired": {"ttl_days": 1, "created_at": "2000-01-01T00:00:00+00:00"},
        "confidence": {"confidence": 0.1},
        "model": {"embedding_model": "other-local-model"},
        "invalid": {},
    }[hidden])
    content = "Unrelated source remains intact." if reuse_primary else long_content("Unrelated source remains intact.")
    parent = put(store, "hidden", content, **metadata)
    filters = {"tags": ["visible"], "memory_type": "fact", "source_agent": "test-agent", "include_stale": False, "min_confidence": 0.5}

    def public_result(streaming):
        with monkeypatch.context() as context:
            if streaming:
                context.setattr(store, "_get_vector_cache", lambda *args, **kwargs: (_ for _ in ()).throw(CacheBudgetExceededError("forced")))
            return next(row for row in query(store, **filters) if row["id"] == "public")

    before = [public_result(streaming) for streaming in (False, True)]
    spans = split_passages(parent["content"], local_encoder[0].window_tokenizer)
    vectors = np.vstack([memory._hash_embed_384(parent["content"][start:end]) for start, end in spans])
    assert store.publish_embedding(parent["id"], memory._hash_embed_384(parent["content"]), embedding_model=parent["embedding_model"],
                                   target_representation=PASSAGE_FORMAT_VERSION, passage_spans=spans, passage_embeddings=None if reuse_primary else vectors, **expected(parent))
    if hidden == "invalid":
        with store.atomic_write(), store._connect() as connection:
            statement = "UPDATE memories SET embedding=? WHERE id='hidden'" if reuse_primary else "UPDATE memory_passages SET embedding=? WHERE parent_id='hidden' AND ordinal=2"
            connection.execute(statement, (np.full(384, np.nan, dtype=np.float32).tobytes(),))
            store._bump_content_version_in_tx(connection)
            store._invalidate_cache()
        if reuse_primary:
            # Removing a corrupt primary legitimately changes candidate admission.
            with monkeypatch.context() as context:
                context.setenv("MESH_PASSAGE_INDEX", "0")
                before = [public_result(streaming) for streaming in (False, True)]
    after = [public_result(streaming) for streaming in (False, True)]
    # A primary-only query may now stream because compatible long parents no
    # longer retain redundant primary terms. Its scoring and policy are exact.
    execution_fields = {"retrieval_mode", "retrieval_degradation_reason"}
    projection = lambda results: [{key: value for key, value in result.items() if key not in execution_fields} for result in results]
    assert projection(after) == projection(before)
    assert before[0]["retrieval_mode"] == "cached"
    assert after[0]["retrieval_mode"] == ("streaming" if hidden == "model" and not reuse_primary else "cached")
    assert before[1]["retrieval_mode"] == after[1]["retrieval_mode"] == "streaming"
    assert all("selected_passage_ordinal" not in result for result in after)


def test_vector_only_identity_lookup_keeps_primary_evidence_after_passage_publication(local_encoder, monkeypatch):
    store = MeshStore("passage-primary-identity")
    put(store)
    vector = memory._hash_embed_384("aurora blue retention")
    fields = ("id", "similarity", "semantic_similarity", "lexical_score", "bm25_score", "rank_score", "candidate_count", "supporting_spans")

    def primary_results(streaming):
        with monkeypatch.context() as context:
            if streaming:
                context.setattr(store, "_get_vector_cache", lambda *args, **kwargs: (_ for _ in ()).throw(CacheBudgetExceededError("forced")))
            results = store.hybrid_query("", vector, n=3, query_embedding_model=MODEL, include_private=False)
            assert all("selected_passage_ordinal" not in row for row in results)
            return [{key: row[key] for key in fields} for row in results]

    before = [primary_results(streaming) for streaming in (False, True)]
    run_jobs(store)
    assert len(passage_rows(store)) == 3
    assert [primary_results(streaming) for streaming in (False, True)] == before


def test_invalid_late_sibling_and_hidden_model_fallback_have_parity(local_encoder, monkeypatch):
    store = MeshStore("passage-invalid")
    put(store, "public")
    put(store, "private", local_only=True)
    run_jobs(store)
    with store.atomic_write(), store._connect() as connection:
        connection.execute("UPDATE memory_passages SET embedding=? WHERE parent_id='public' AND ordinal=2", (np.full(384, np.nan, dtype=np.float32).tobytes(),))
        store._bump_content_version_in_tx(connection)
        store._invalidate_cache()
    assert store.get_indexing_status(["public"])["public"]["passage_indexing_state"] == "pending"
    cached = query(store)
    hash_cached = store.hybrid_query("aurora blue retention", memory._hash_embed_384("aurora blue retention"), n=3, query_embedding_model="hash-384", include_private=False)
    monkeypatch.setattr(store, "_get_vector_cache", lambda *args, **kwargs: (_ for _ in ()).throw(CacheBudgetExceededError("forced")))
    streamed = query(store)
    hash_streamed = store.hybrid_query("aurora blue retention", memory._hash_embed_384("aurora blue retention"), n=3, query_embedding_model="hash-384", include_private=False)
    for left, right in ((cached, streamed), (hash_cached, hash_streamed)):
        assert [(row["id"], row["rank_score"], row["similarity"], row["bm25_score"], row["supporting_spans"]) for row in left] == [(row["id"], row["rank_score"], row["similarity"], row["bm25_score"], row["supporting_spans"]) for row in right]
        assert left[0]["id"] == "public" and "selected_passage_ordinal" not in left[0]


def test_actual_cache_budget_fallback_releases_failed_and_retired_generations(local_encoder, monkeypatch):
    store = MeshStore("passage-byte-budget")
    # Leave sufficient space for one streamed row even as retained caches shrink.
    for ordinal in range(24):
        put(store, f"parent-{ordinal}")
    run_jobs(store)
    run_jobs(store)
    run_jobs(store)
    warm = query(store)
    fields = ("id", "rank_score", "similarity", "bm25_score", "supporting_spans")
    projected = [{key: row[key] for key in fields} for row in warm]
    cache = store._get_vector_cache()
    cache_bytes = cache._cache_bytes
    index = cache.passages
    owned = storage._object_graph_bytes((
        *(getattr(cache, name) for name in storage._CACHE_TOP_FIELDS),
        cache._matrix_buf, cache._hash_matrix_buf, index.matrix, index.offsets,
        index.lexical, index.ranges, index.ready_primary_ids, index.implicit_lexical, index.lexical_strings,
    ))
    assert cache_bytes >= owned
    del index
    budget = cache_bytes - 65536
    assert budget > 0
    del cache, warm
    store._invalidate_cache()
    gc.collect()
    baseline = storage.get_vector_cache_stats()["charged_bytes"]
    monkeypatch.setenv("MESH_MAX_VECTOR_CACHE_BYTES", str(budget))
    streamed = query(store)
    assert all(row["retrieval_mode"] == "streaming" for row in streamed)
    assert [{key: row[key] for key in fields} for row in streamed] == projected
    assert storage.get_vector_cache_stats()["charged_bytes"] <= budget
    del streamed
    gc.collect()
    assert storage.get_vector_cache_stats()["charged_bytes"] == baseline

    # A warm cache may fit but leave insufficient room for the query lease.
    monkeypatch.delenv("MESH_MAX_VECTOR_CACHE_BYTES")
    store._get_vector_cache()
    monkeypatch.setenv("MESH_MAX_VECTOR_CACHE_BYTES", str(cache_bytes + 20000))
    streamed = query(store)
    assert all(row["retrieval_mode"] == "streaming" for row in streamed)
    assert [{key: row[key] for key in fields} for row in streamed] == projected
    del streamed
    gc.collect()
    assert storage.get_vector_cache_stats()["charged_bytes"] == baseline


@pytest.mark.parametrize("invalid", ["fractional", "outside", "count-text"])
def test_invalid_stored_offsets_never_establish_readiness_or_staged_integrity(local_encoder, monkeypatch, invalid):
    store = MeshStore("passage-offsets")
    parent = put(store)
    run_jobs(store)
    with store.atomic_write(), store._connect() as connection:
        if invalid == "count-text":
            connection.execute("UPDATE memories SET passage_count=? WHERE id=?", ("invalid-count", parent["id"]))
        else:
            connection.execute("UPDATE memory_passages SET end=? WHERE parent_id=? AND ordinal=2", (len(parent["content"]) + (0.5 if invalid == "fractional" else 1), parent["id"]))
        store._bump_content_version_in_tx(connection)
        store._invalidate_cache()
    assert store.get_indexing_status([parent["id"]])[parent["id"]]["passage_indexing_state"] == "pending"
    assert parent["id"] not in store._get_vector_cache().omitted_primary_lexical_ids
    cached = query(store)
    assert "selected_passage_ordinal" not in cached[0]
    with monkeypatch.context() as context:
        context.setattr(store, "_get_vector_cache", lambda *args, **kwargs: (_ for _ in ()).throw(CacheBudgetExceededError("forced")))
        streamed = query(store)
    fields = ("id", "rank_score", "similarity", "lexical_score", "bm25_score", "supporting_spans")
    assert [{key: row[key] for key in fields} for row in cached] == [{key: row[key] for key in fields} for row in streamed]
    with store._read_connect() as connection, pytest.raises(RuntimeError, match="passage"):
        verify_passage_rows(connection)

@pytest.mark.parametrize("edit", ["metadata", "content", "reinsert"])
def test_inference_cas_rejects_every_changed_source(local_encoder, edit):
    store = MeshStore("passage-cas")
    before = put(store)
    original = local_encoder[0]._encode_raw_batches
    changed = False

    def concurrent_edit(texts, batch_size=32):
        nonlocal changed
        if not changed:
            changed = True
            if edit == "metadata":
                store.update("fact-1", tags=["new"])
            elif edit == "content":
                store.update("fact-1", content="A replacement accepted source.")
            else:
                store.delete("fact-1")
                put(store)
        return original(texts, batch_size)

    local_encoder[0]._encode_raw_batches = concurrent_edit
    run_jobs(store)
    assert passage_rows(store) == []
    current = store.get("fact-1")
    assert current["passage_format_version"] == ""
    assert len(store.list_embedding_jobs()) == 1
    assert current["incarnation"] != before["incarnation"] if edit == "reinsert" else current["revision"] == before["revision"] + 1


def test_atomic_bundle_rollback_and_job_representation_cas(local_encoder):
    store = MeshStore("passage-rollback")
    row = put(store)
    plan = split_passages(row["content"], local_encoder[0].window_tokenizer)
    vectors = np.vstack([memory._hash_embed_384(row["content"][start:end]) for start, end in plan])
    assert not store.publish_embedding(row["id"], memory._hash_embed_384(row["content"]), embedding_model=MODEL, **expected(row))
    assert not store.retry_embedding_job(row["id"], expected_target_model=MODEL, **expected(row))
    with pytest.raises(RuntimeError, match="rollback"):
        with store.atomic_write():
            assert store.publish_embedding(row["id"], memory._hash_embed_384(row["content"]), embedding_model=MODEL, target_representation=PASSAGE_FORMAT_VERSION, passage_spans=plan, passage_embeddings=vectors, **expected(row))
            raise RuntimeError("rollback")
    assert passage_rows(store) == [] and store.get(row["id"])["passage_format_version"] == ""
    assert store.list_embedding_jobs()[0]["target_representation"] == PASSAGE_FORMAT_VERSION
    assert not store.publish_embedding(row["id"], memory._hash_embed_384(row["content"]), embedding_model=MODEL, target_representation=PASSAGE_FORMAT_VERSION, passage_spans=plan, passage_embeddings=vectors,
                                       **{**expected(row), "expected_store_generation": "f" * 32})
    bad = vectors.copy()
    bad[-1, 3] = np.nan
    with pytest.raises(ValueError, match="Passage embeddings"):
        store.publish_embedding(row["id"], memory._hash_embed_384(row["content"]), embedding_model=MODEL, target_representation=PASSAGE_FORMAT_VERSION, passage_spans=plan, passage_embeddings=bad, **expected(row))
    assert passage_rows(store) == []


def test_metadata_carry_forward_content_invalidation_and_explicit_cleanup(local_encoder):
    store = MeshStore("passage-edits")
    put(store)
    run_jobs(store)
    old = passage_rows(store)
    updated = store.update("fact-1", tags=["updated"], confidence=0.8)
    carried = passage_rows(store)
    assert all(row[1] == updated["revision"] for row in carried)
    assert [row[3:] for row in old] == [row[3:] for row in carried]
    assert store.list_embedding_jobs() == []
    store.update("fact-1", content="A new source body.")
    assert passage_rows(store) == [] and store.get("fact-1")["passage_count"] == 0
    put(store)
    run_jobs(store)
    old_identity = store.get("fact-1")["incarnation"]
    assert store.delete("fact-1") and passage_rows(store) == []
    assert put(store)["incarnation"] != old_identity
    run_jobs(store)
    put(store, content="Replacement via scalar upsert.")
    assert passage_rows(store) == []
    put(store)
    run_jobs(store)
    store.upsert_batch([("fact-1", "Replacement via batch upsert.", memory._hash_embed_384("Replacement via batch upsert."), {"embedding_model": MODEL}, False)])
    assert passage_rows(store) == []
    put(store)
    run_jobs(store)
    store.clear_namespace()
    assert passage_rows(store) == [] and store.list_embedding_jobs() == []


def test_old_job_target_does_not_cross_model_replacement(local_encoder):
    store = MeshStore("passage-model")
    first = put(store)
    assert store.enqueue_embedding_job("fact-1", target_model="old-local-model", **expected(first))
    second = put(store, embedding_model="new-local-model")
    job = store.list_embedding_jobs()[0]
    assert (job["target_model"], job["target_representation"], job["revision"]) == ("new-local-model", PASSAGE_FORMAT_VERSION, second["revision"])
    assert not store.publish_embedding("fact-1", memory._hash_embed_384(first["content"]), embedding_model="old-local-model", **expected(first))


def test_malformed_primary_is_repaired_without_losing_derived_job(local_encoder):
    store = MeshStore("passage-primary-repair")
    row = put(store)
    with store.atomic_write(), store._connect() as connection:
        connection.execute("UPDATE memories SET embedding=? WHERE id=?", (b"bad", row["id"]))
        store._bump_content_version_in_tx(connection)
        store._invalidate_cache()
    run_jobs(store)
    assert len(passage_rows(store)) == 3
    assert store.get_indexing_status([row["id"]])[row["id"]]["passage_indexing_state"] == "indexed"
    assert store.list_embedding_jobs() == []


def test_non_direct_backend_keeps_document_ready_and_exact_passage_job(monkeypatch):
    store = MeshStore("passage-nondirect")
    row = put(store, embedding_model="hash-384")
    assert store.enqueue_embedding_job(row["id"], target_model=MODEL, target_representation=PASSAGE_FORMAT_VERSION, **expected(row))
    backend = SimpleNamespace(model_tag=MODEL)
    monkeypatch.setattr(memory, "_model", backend)
    monkeypatch.setattr(memory, "_model_backend", "synthetic-local")
    monkeypatch.setattr(memory, "_encode_texts_with_tag", lambda texts: (np.vstack([memory._hash_embed_384(text) for text in texts]), MODEL))
    run_jobs(store)
    status = store.get_indexing_status([row["id"]])[row["id"]]
    assert status["indexing_state"] == "indexed" and status["passage_indexing_state"] == "pending"
    with store._read_connect() as connection:
        job = connection.execute("SELECT revision,incarnation,target_model,target_representation,attempts,error_code FROM embedding_jobs").fetchone()
        assert tuple(job) == (row["revision"], row["incarnation"], MODEL, PASSAGE_FORMAT_VERSION, 1, "representation_unavailable")
        version = store._read_content_version_locked(connection)
        connection.execute("SELECT 1").fetchone()
    with store.atomic_write(), store._connect() as connection:
        connection.execute("UPDATE embedding_jobs SET available_at=created_at")
    run_jobs(store)
    with store._read_connect() as connection:
        assert store._read_content_version_locked(connection) == version
        assert connection.execute("SELECT attempts FROM embedding_jobs").fetchone()[0] == 2


def test_passage_rollout_is_opt_in_and_explicit_wait_reports_disabled(local_encoder, monkeypatch):
    monkeypatch.delenv("MESH_PASSAGE_INDEX")
    mesh = memory.Mesh("passage-disabled", agent_id="test-agent")
    row = put(mesh.store)
    assert mesh.store.list_embedding_jobs() == []
    assert mesh.store.backfill_embedding_jobs(1) == 0
    assert mesh.wait_for_indexed(row["id"], timeout=0, target_representation=PASSAGE_FORMAT_VERSION)["status"] == "disabled"
    assert local_encoder[1] == []


def test_ask_help_names_opt_in_flag_and_exact_readiness_target(monkeypatch, capsys):
    from mesh import cli

    monkeypatch.setattr("sys.argv", ["mesh-ask", "--help"])
    with pytest.raises(SystemExit) as outcome:
        cli.ask_cmd()
    assert outcome.value.code == 0
    help_text = capsys.readouterr().out
    assert "MESH_PASSAGE_INDEX=1" in help_text and "default: 0" in help_text
    assert "target_representation='paragraph-sentences-v1'" in help_text


def test_warm_encryption_transition_converts_work_and_every_passage(local_encoder, monkeypatch):
    store = MeshStore("passage-encryption")
    put(store)
    run_jobs(store)
    store._get_vector_cache()
    task = store.create_work_item("Synthetic plaintext task marker")
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", "17" * 32)
    enable_encryption_at_rest()
    reopened = MeshStore(store.namespace, create=False)
    assert all(is_encrypted_blob(row[-1]) for row in passage_rows(reopened))
    with reopened._read_connect() as connection:
        assert is_encrypted_text(connection.execute("SELECT title FROM work_items WHERE id=?", (task["id"],)).fetchone()[0])
        assert connection.execute("SELECT value FROM mesh_meta WHERE key='work_encryption_cleanup_pending'").fetchone()[0] == "0"
    assert query(reopened)[0]["supporting_spans"][0]["text"] == "Aurora retention requires blue envelopes."


def test_namespace_rename_preserves_derived_rows_and_rejects_old_generation(local_encoder):
    store = MeshStore("passage-old")
    old = put(store)
    run_jobs(store)
    put(store, "pending", "A pending short source.")
    rename_namespace("passage-old", "passage-new")
    renamed = MeshStore("passage-new", create=False)
    with renamed._read_connect() as connection:
        verify_passage_rows(connection)
        assert {row[0] for row in connection.execute("SELECT DISTINCT namespace FROM memory_passages")} == {"passage-new"}
    assert len(passage_rows(renamed)) == 3
    assert query(renamed)[0]["id"] == "fact-1"
    assert renamed.store_generation != old["store_generation"]


def test_schema5_backup_identity_backoff_and_capacity_aware_neural_backfill(monkeypatch):
    root = Path(os.environ["MESH_HOME"])
    root.mkdir(parents=True, exist_ok=True)
    path = root / "mesh_passage-upgrade.db"
    now = datetime.now(timezone.utc).isoformat()
    generation = "a" * 32
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE mesh_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.executemany("INSERT INTO mesh_meta VALUES(?,?)", [("schema_version", "5"), ("namespace_owner", "passage-upgrade"), ("store_generation", generation), ("content_version", "7")])
        connection.execute("PRAGMA user_version=5")
        connection.execute("CREATE TABLE memories(id TEXT PRIMARY KEY,namespace TEXT NOT NULL,content TEXT NOT NULL,confidence REAL NOT NULL DEFAULT 1,memory_type TEXT NOT NULL DEFAULT 'fact',source_agent TEXT NOT NULL DEFAULT 'test-agent',tags TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,accessed_at TEXT NOT NULL,access_count INTEGER NOT NULL DEFAULT 0,embedding BLOB,embedding_model TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,incarnation TEXT NOT NULL)")
        connection.executemany("INSERT INTO memories(id,namespace,content,created_at,accessed_at,embedding,embedding_model,incarnation) VALUES(?,?,?,?,?,?,?,?)",
                               [(identifier, "passage-upgrade", "Original neural source.", now, now, memory._hash_embed_384("Original neural source.").tobytes(), MODEL, incarnation) for identifier, incarnation in (("first", "b" * 32), ("second", "c" * 32))])
        connection.execute("CREATE TABLE embedding_jobs(memory_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,revision INTEGER NOT NULL,incarnation TEXT NOT NULL,target_model TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,available_at TEXT NOT NULL,created_at TEXT NOT NULL,error_code TEXT)")
        connection.execute("INSERT INTO embedding_jobs VALUES(?,?,?,?,?,?,?,?,?)", ("first", "passage-upgrade", 1, "b" * 32, MODEL, 3, "2099-01-01T00:00:00+00:00", now, "embedding_failed"))
    upgraded = MeshStore("passage-upgrade", create=False)
    assert upgraded.store_generation == generation and upgraded.get("first")["incarnation"] == "b" * 32
    assert upgraded.get_indexing_status(["second"])["second"]["passage_indexing_state"] == "pending"
    with upgraded._read_connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        old_job = connection.execute("SELECT attempts,available_at,target_representation FROM embedding_jobs WHERE memory_id='first'").fetchone()
        assert tuple(old_job) == (3, "2099-01-01T00:00:00+00:00", "")
    backup = list(root.glob("mesh_passage-upgrade.db.backup-*"))
    assert len(backup) == 1
    with sqlite3.connect(backup[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
    monkeypatch.setenv("MESH_MAX_PENDING_EMBEDDING_JOBS", "1")
    assert upgraded.backfill_embedding_jobs(1) == 0
    monkeypatch.setenv("MESH_MAX_PENDING_EMBEDDING_JOBS", "2")
    assert upgraded.backfill_embedding_jobs(1) == 1
    assert upgraded.list_embedding_jobs()[0]["target_representation"] == PASSAGE_FORMAT_VERSION
    assert upgraded.backfill_embedding_jobs(1) == 0


def test_explicit_passage_wait_and_final_policy_revalidation(local_encoder, monkeypatch):
    mesh = memory.Mesh("passage-wait", agent_id="test-agent")
    row = put(mesh.store)
    assert mesh.wait_for_indexed(row["id"], timeout=0)["status"] == "indexed"
    assert mesh.wait_for_indexed(row["id"], timeout=0, target_representation=PASSAGE_FORMAT_VERSION)["status"] == "timed_out"
    run_jobs(mesh.store)
    assert mesh.wait_for_indexed(row["id"], timeout=0, target_representation=PASSAGE_FORMAT_VERSION, **{"expected_revisions": {row["id"]: row["revision"]}, "expected_incarnations": {row["id"]: row["incarnation"]}, "expected_store_generation": row["store_generation"]})["status"] == "indexed"
    original = mesh.store.revalidate_query_results

    def changed_policy(results, **kwargs):
        mesh.store.update(row["id"], local_only=True)
        return original(results, **kwargs)

    monkeypatch.setattr(mesh.store, "revalidate_query_results", changed_policy)
    assert query(mesh.store) == []
