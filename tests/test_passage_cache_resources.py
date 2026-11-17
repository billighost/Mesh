"""Independent ownership, bounded admission and exact ranking for passage caches."""
from __future__ import annotations

import gc
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from mesh import passage_search as passages
from mesh import store as storage

MODEL = "onnx-minilm-l6-v2"


@pytest.fixture
def generation(tmp_path, monkeypatch):
    for name in ("MESH_HOME", "MESH_DIR"):
        monkeypatch.setenv(name, str(tmp_path / "mesh-home"))
    for name in ("MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_NO_OUTBOX_WORKER"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("MESH_PASSAGE_INDEX", "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    store = storage.MeshStore("passage-resources")
    vector = np.arange(1, 385, dtype=np.float32)
    vector /= np.linalg.norm(vector)
    try:
        for number in range(2):
            blocks = [" ".join(["blue", "shipping", "reference", "source", "retention"] * 28) + " unique" + str(number)
                      for _ in range(3)]
            body = "\n\n".join(blocks)
            identifier = "parent-" + str(number)
            store.upsert(identifier, body, vector, {"embedding_model": MODEL, "source_agent": "test-agent", "local_only": number == 1})
            row = store.get(identifier)
            spans, offset = [], 0
            for block in blocks:
                spans.append((offset, offset + len(block)))
                offset += len(block) + 2
            assert store.publish_embedding(identifier, vector, expected_revision=row["revision"],
                                           expected_incarnation=row["incarnation"], expected_store_generation=row["store_generation"],
                                           embedding_model=MODEL, target_representation=passages.PASSAGE_FORMAT_VERSION,
                                           passage_spans=spans, passage_embeddings=np.vstack([vector] * 3))
        cache = store._get_vector_cache()
        yield store, cache, vector
    finally:
        storage._VECTOR_CACHE.clear()
        storage._CACHE_REJECTIONS.clear()
        storage._close_all_pooled_connections()
        gc.collect()


def set_aliases(lexical):
    return {(left, right) for left, value in lexical.items() if isinstance(value, set)
            for right, other in lexical.items() if value is other}


def test_cached_lexical_values_and_aliases_preserved(generation):
    _, cache, _ = generation
    index = cache.passages
    for parent in cache.rows_by_id.values():
        first, last = index.ranges[parent["id"]]
        for position in range(first, last):
            start, end = map(int, index.offsets[position])
            expected = storage._build_row_lexical_entry(parent["content"][start:end], [])
            actual = index.lexical[position]
            assert actual == expected
            assert set_aliases(actual) == set_aliases(expected)
            assert all(type(count) is int for count in actual["term_freq"].values())
            assert all(index.lexical_strings[token] is token for token in actual["indexed_tokens"])
    owned = storage._object_graph_bytes((index.matrix, index.offsets, index.lexical, index.ranges,
                                        index.ready_primary_ids, index.implicit_lexical, index.lexical_strings))
    assert index.allocated_bytes >= owned


def test_generation_owns_strings_after_parent_pool_retirement(generation):
    store, _, vector = generation
    body = "unique0 belongs to the materialized primary source."
    store.upsert("short-owner", body, vector, {"embedding_model": MODEL, "source_agent": "test-agent", "local_only": True})
    parent = store.get("short-owner")
    assert store.publish_embedding("short-owner", vector, expected_revision=parent["revision"],
                                   expected_incarnation=parent["incarnation"], expected_store_generation=parent["store_generation"],
                                   embedding_model=MODEL, target_representation=passages.PASSAGE_FORMAT_VERSION,
                                   passage_spans=[(0, len(body))])
    cache = store._get_vector_cache(force_refresh=True)
    assert cache.omitted_primary_lexical_ids == {"parent-0", "parent-1"}
    assert "short-owner" not in cache.omitted_primary_lexical_ids
    index = cache.passages
    token = index.lexical_strings["unique0"]
    pool_charge = index.allocated_bytes
    assert cache.lexical_strings["unique0"] is token
    updated = dict(cache.rows_by_id["short-owner"])
    updated.update(content="Different replacement body.", revision=updated["revision"] + 1,
                   passage_format_version="", passage_count=0)
    assert cache._upsert_vector("short-owner", vector, row_meta=updated)
    assert cache.passage_index_dirty
    assert "unique0" not in cache.lexical_strings
    assert index.lexical_strings["unique0"] is token
    assert index.allocated_bytes == pool_charge
    assert storage.get_vector_cache_stats()["charged_bytes"] >= cache._cache_bytes


def test_cached_and_streaming_privacy_and_exact_result_parity(generation, monkeypatch):
    store, _, vector = generation
    cached = store.hybrid_query("blue retention shipping", vector, n=3, query_embedding_model=MODEL, include_private=False)
    def force_streaming(*args, **kwargs):
        raise storage.CacheBudgetExceededError("forced invariant test")
    monkeypatch.setattr(store, "_get_vector_cache", force_streaming)
    streamed = store.hybrid_query("blue retention shipping", vector, n=3, query_embedding_model=MODEL, include_private=False)
    assert cached and streamed
    assert all(row["id"] == "parent-0" and not row["local_only"] for row in (*cached, *streamed))
    projection = lambda rows: [{key: value for key, value in row.items()
                               if key not in ("retrieval_mode", "retrieval_degradation_reason")} for row in rows]
    assert projection(cached) == projection(streamed)


class TraceLease:
    def __init__(self, limit):
        self.limit, self.requests = limit, []

    def resize(self, size):
        self.requests.append(size)
        if size > self.limit:
            raise storage.CacheBudgetExceededError("fake allocation cap", required_bytes=size)


def test_unadmitted_pool_growth_is_atomic():
    index = passages.PassageCacheIndex(0)
    initial = index.allocated_bytes
    cache = SimpleNamespace(lexical_strings={}, _cache_bytes=0, _build_peak_bytes=1024, _lease=TraceLease(0))
    with pytest.raises(storage.CacheBudgetExceededError):
        passages._cache_lexical_entry(cache, index, "A source with independently owned words.")
    assert index.lexical_strings == {}
    assert index.allocated_bytes == initial


def test_local_entry_scratch_and_single_pool_charge():
    class NoIterationPool(dict):
        def __iter__(self):
            raise AssertionError("Corpus vocabulary traversal is forbidden")

        def values(self):
            raise AssertionError("Corpus vocabulary traversal is forbidden")

        def keys(self):
            raise AssertionError("Corpus vocabulary traversal is forbidden")

    index = passages.PassageCacheIndex(0)
    values = {"existing" + str(number): "existing" + str(number) for number in range(128)}
    pool = NoIterationPool({value: value for value in values.values()})
    index.allocated_bytes += sum(sys.getsizeof(value) for value in values.values()) + sys.getsizeof(pool) - sys.getsizeof(index.lexical_strings)
    index.lexical_strings = pool
    cache = SimpleNamespace(lexical_strings={}, _cache_bytes=0, _build_peak_bytes=65536, _lease=TraceLease(1024 * 1024))
    first = passages._cache_lexical_entry(cache, index, "blue blue shipping source")
    charged = index.allocated_bytes
    # Reusing known words needs only the existing row working space; reserve no
    # dictionary replacement when the corpus pool cannot grow.
    cache._lease.limit = cache._cache_bytes + charged + cache._build_peak_bytes
    second = passages._cache_lexical_entry(cache, index, "blue blue shipping source")
    assert index.allocated_bytes == charged
    assert first == second
    assert first["term_freq"]["blue"] == 2
    assert len(cache._lease.requests) == 2
    assert max(cache._lease.requests) >= charged + cache._build_peak_bytes
