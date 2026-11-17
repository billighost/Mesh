"""Tiny model-free contracts for bounded passage query work and exact results."""
from __future__ import annotations

import gc
from collections import Counter

import numpy as np
import pytest

from mesh import audit
from mesh import passage_search as passages
from mesh import store as storage

MODEL = "onnx-minilm-l6-v2"


@pytest.fixture
def corpus(monkeypatch):
    for name in ("MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_NO_OUTBOX_WORKER"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("MESH_PASSAGE_INDEX", "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    monkeypatch.setenv("MESH_RECALL_CANDIDATES", "16")
    monkeypatch.setattr(audit, "_ensure_audit_drain_thread", lambda: None)
    store = storage.MeshStore("query-work")
    vector = np.ones(384, dtype=np.float32)
    vector /= np.linalg.norm(vector)
    metadata = {"source_agent": "test-agent", "embedding_model": MODEL, "tags": ["visible"], "memory_type": "fact", "confidence": 0.9}
    try:
        with store.write_transaction():
            for number in range(24):
                store.upsert(f"short-{number:02}", "Aurora's blue-envelope retention uses ROUTE_42.", vector, metadata)
            blocks = [" ".join(["Aurora blue retention ROUTE_42."] * 6)] * 2
            content = "\n\n".join(blocks)
            store.upsert("long-ready", content, vector, metadata)
            for identifier, changes in (
                ("hidden-private", {"local_only": True}),
                ("hidden-quarantined", {"quarantined": True}),
                ("hidden-agent", {"source_agent": "other-agent"}),
                ("hidden-type", {"memory_type": "decision"}),
                ("hidden-tags", {"tags": ["other"]}),
                ("hidden-confidence", {"confidence": 0.0}),
            ):
                store.upsert(identifier, "Aurora blue retention ROUTE_42.", vector, {**metadata, **changes})
            row = store.get("long-ready")
            assert store.publish_embedding(
                "long-ready", vector, expected_revision=row["revision"], expected_incarnation=row["incarnation"],
                expected_store_generation=row["store_generation"], embedding_model=MODEL,
                target_representation=passages.PASSAGE_FORMAT_VERSION,
                passage_spans=[(0, len(blocks[0])), (len(blocks[0]) + 2, len(content))],
                passage_embeddings=np.vstack([vector, vector]),
            )
        yield store, vector
    finally:
        storage._VECTOR_CACHE.clear()
        storage._CACHE_REJECTIONS.clear()
        storage._close_all_pooled_connections()
        audit._close_all_audit_connections()
        gc.collect()


def query(store, vector):
    return store.hybrid_query(
        "Aurora blue retention ROUTE_42", vector, n=5, query_embedding_model=MODEL,
        include_private=False, include_quarantined=False, include_stale=False,
        min_confidence=0.5, memory_type="fact", tags=["visible"], source_agent="test-agent",
    )


def projection(rows):
    return [{key: value for key, value in row.items()
             if key not in ("retrieval_mode", "retrieval_degradation_reason")} for row in rows]


def legacy_admit(heap, choices, score, identifier, ordinal, limit):
    storage._keep_best_scored_id(heap, score, identifier, limit)
    live = {item.memory_id for item in heap}
    for stale in choices.keys() - live:
        choices.pop(stale)
    if identifier in live:
        choices[identifier] = ordinal


@pytest.mark.parametrize("mode", ["calibrated", "rrf"])
def test_small_pool_matches_full_scan_reference_and_cached_policy(corpus, monkeypatch, mode):
    store, vector = corpus
    monkeypatch.setenv("MESH_RECALL_FUSION", mode)
    cache = store._get_vector_cache()

    class TracedRows(dict):
        scans = 0

        def items(self):
            self.scans += 1
            assert self.scans <= 2, "final scoring traversed every cached parent"
            return super().items()

    traced = TracedRows(cache.rows_by_id)
    with monkeypatch.context() as context:
        context.setattr(cache, "rows_by_id", traced)
        cached = projection(query(store, vector))
    assert traced.scans == 2
    assert len(cached) == 5
    assert not any(row["id"].startswith("hidden-") for row in cached)
    assert len({row["id"] for row in cached}) == 5
    assert all(row["candidate_count"] < 25 for row in cached)
    tied = [row for row in cached if row["id"].startswith("short-")]
    assert len(tied) >= 2 and [row["id"] for row in tied] == sorted(row["id"] for row in tied)

    def no_cache(*_args, **_kwargs):
        raise storage.CacheBudgetExceededError("forced tiny streaming contract")

    monkeypatch.setattr(store, "_get_vector_cache", no_cache)
    original_rows = passages._parent_memory_rows
    original_entry = passages._build_row_lexical_entry
    original_statistics = passages._build_row_lexical_statistics
    scans, statistics = [], Counter()
    active_scan = None

    def traced_rows(connection, where, params, identifiers=None):
        nonlocal active_scan
        chosen = None if identifiers is None else tuple(identifiers)
        current = {"ids": chosen, "rows": 0}
        scans.append(current)
        active_scan = len(scans)
        try:
            for raw in original_rows(connection, where, params, chosen):
                current["rows"] += 1
                yield raw
        finally:
            active_scan = None

    def traced_entry(content, tags):
        assert active_scan != 1, "statistics pass constructed full term frequencies"
        return original_entry(content, tags)

    def traced_statistics(content, tags):
        assert active_scan == 1
        value = original_statistics(content, tags)
        full = original_entry(content, tags)
        assert value == (full["doc_len"], full["df_stems"])
        statistics[content] += 1
        return value

    with monkeypatch.context() as context:
        context.setattr(passages, "_parent_memory_rows", traced_rows)
        context.setattr(passages, "_build_row_lexical_entry", traced_entry)
        context.setattr(passages, "_build_row_lexical_statistics", traced_statistics)
        streamed = projection(query(store, vector))
    assert streamed == cached
    assert len(scans) == 3 and [scan["ids"] is None for scan in scans] == [True, True, False]
    assert scans[2]["rows"] == len(scans[2]["ids"]) < scans[0]["rows"]
    assert statistics["Aurora's blue-envelope retention uses ROUTE_42."] == 24

    def full_scan_reference(connection, where, params, identifiers=None):
        for raw in original_rows(connection, where, params):
            if identifiers is None or raw["id"] in identifiers:
                yield raw

    def full_statistics(content, tags):
        lexical = original_entry(content, tags)
        return lexical["doc_len"], lexical["df_stems"]

    with monkeypatch.context() as context:
        context.setattr(passages, "_parent_memory_rows", full_scan_reference)
        context.setattr(passages, "_build_row_lexical_statistics", full_statistics)
        context.setattr(passages, "_admit_best_unit", legacy_admit)
        assert projection(query(store, vector)) == streamed


def test_heap_admission_matches_oracle_without_scanning_retained_entries():
    class NoHeapIteration(list):
        def __iter__(self):
            raise AssertionError("heap traversal during admission")

    class NoChoiceIteration(dict):
        def keys(self):
            raise AssertionError("choice traversal during admission")

    entries = [(float((number * 7) % 5 - 2), f"parent-{31 - number:02}", number % 3) for number in range(32)]
    for limit in (1, 3, 16):
        for sequence in (entries, list(reversed(entries))):
            heap, choices, expected_heap, expected_choices = NoHeapIteration(), NoChoiceIteration(), [], {}
            for score, identifier, ordinal in sequence:
                passages._admit_best_unit(heap, choices, score, identifier, ordinal, limit)
                legacy_admit(expected_heap, expected_choices, score, identifier, ordinal, limit)
                assert choices == expected_choices
                actual = sorted((heap[position].score, heap[position].memory_id) for position in range(len(heap)))
                assert actual == sorted((item.score, item.memory_id) for item in expected_heap)


class Cursor:
    def __init__(self, *, interrupted=False):
        self.closed, self.interrupted = False, interrupted

    def __iter__(self):
        yield {"id": "fake-row"}
        if self.interrupted:
            raise storage.sqlite3.OperationalError("interrupted")

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, *, interrupted=False):
        self.calls, self.cursors, self.interrupted = [], [], interrupted

    def execute(self, sql, params):
        self.calls.append((sql, params))
        cursor = Cursor(interrupted=self.interrupted)
        self.cursors.append(cursor)
        return cursor


def test_candidate_batches_bind_ids_and_keep_filters_with_no_empty_query():
    connection = Connection()
    where, params = "namespace=? AND local_only=0 AND source_agent=?", ("fake-project", "fake-agent")
    identifiers = ["fake-id-" + str(number) for number in range(512)] + ["x' OR 1=1 --"]
    assert list(passages._parent_memory_rows(connection, where, params, [])) == []
    assert connection.calls == []
    assert len(list(passages._parent_memory_rows(connection, where, params, identifiers))) == 2
    assert [len(bound) - len(params) for _, bound in connection.calls] == [512, 1]
    for sql, bound in connection.calls:
        assert f"WHERE {where} AND id IN (" in sql
        assert bound[:len(params)] == params
        assert sql.count("?") == len(bound)
        assert "x' OR 1=1 --" not in sql
    assert [identifier for _, bound in connection.calls for identifier in bound[len(params):]] == identifiers
    assert all(cursor.closed for cursor in connection.cursors)


def test_candidate_cursors_close_on_cancellation_and_generator_exit():
    interrupted = Connection(interrupted=True)
    with pytest.raises(storage.sqlite3.OperationalError, match="interrupted"):
        list(passages._parent_memory_rows(interrupted, "namespace=?", ("fake-project",), ["fake-id"]))
    assert interrupted.cursors[0].closed
    connection = Connection()
    rows = passages._parent_memory_rows(connection, "namespace=?", ("fake-project",), ["fake-id"])
    assert next(rows)["id"] == "fake-row"
    rows.close()
    assert connection.cursors[0].closed
