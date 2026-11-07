"""Cache budget rejection must follow the selected representation mode."""
from __future__ import annotations

import gc

import numpy as np
import pytest

from mesh import passage_search
from mesh import store as storage


def test_disabling_passages_retries_primary_cache_after_passage_budget_rejection(monkeypatch):
    monkeypatch.setenv("MESH_PASSAGE_INDEX", "1")
    store = storage.MeshStore("cache-admission-mode")
    vector = np.ones(storage.EMBEDDING_DIM, dtype=np.float32)
    store.upsert("orchid-fact", "Orchid releases use the reviewed staging guide.", vector,
                 {"embedding_model": "hash-384", "source_agent": "test-agent"})
    attempts = []

    def oversized_passages(*args, **kwargs):
        attempts.append(1)
        raise storage.CacheBudgetExceededError(
            "Synthetic passage generation exceeds the cache budget.",
            required_bytes=storage._get_max_vector_cache_bytes() + 1,
        )

    try:
        with monkeypatch.context() as context:
            context.setattr(passage_search, "load_passage_cache", oversized_passages)
            with pytest.raises(storage.CacheBudgetExceededError):
                store._get_vector_cache()
            gc.collect()
            # Identical mode/version keeps the useful negative admission cache.
            with pytest.raises(storage.CacheBudgetExceededError):
                store._get_vector_cache()
            assert len(attempts) == 1

        monkeypatch.setenv("MESH_PASSAGE_INDEX", "0")
        cache = store._get_vector_cache()
        assert cache.passage_index_enabled is False
        assert cache.passages is None
        assert set(cache.rows_by_id) == {"orchid-fact"}
        assert cache.rows_by_id["orchid-fact"]["content"] == "Orchid releases use the reviewed staging guide."
        assert (store.db_path, store.namespace) not in storage._CACHE_REJECTIONS
        del cache
    finally:
        storage._evict_vector_cache_for_db_path(store.db_path)
        storage.close_store_readers(store.db_path)
        storage.close_store_connection(store.db_path)
        storage._CACHE_REJECTIONS.pop((store.db_path, store.namespace), None)
        gc.collect()
