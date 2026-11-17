"""Cache-local lexical ownership must preserve values, privacy and lifetime."""
from __future__ import annotations

import json
import sys
from collections import Counter

import pytest

from mesh import memory as memory_module
from mesh import store as store_module
from tests.test_lexical_cache_performance import isolated as isolated


def _assert_pool(cache):
    with cache.read_snapshot():
        assert cache.lexical_strings.keys() == cache.token_to_ids.keys()
        for token, canonical in cache.lexical_strings.items():
            assert token is canonical
            assert cache.token_to_ids[token]
        for identifier, lexical in cache.lexical_by_id.items():
            for token in lexical["indexed_tokens"]:
                assert token is cache.lexical_strings[token]
            row = cache.rows_by_id[identifier]
            original = store_module._build_row_lexical_entry(row["content"], row["tags_list"])
            assert {key: value for key, value in lexical.items() if key != "policy"} == original
            assert isinstance(lexical["term_freq"], Counter)


def _assert_charge(cache):
    with cache.read_snapshot():
        assert cache._cache_bytes == cache._top_bytes() + cache._rows_bytes + cache._index_bytes
        graph = store_module._object_graph_bytes({name: getattr(cache, name) for name in store_module._CACHE_TOP_FIELDS})
        for name in ("_matrix_buf", "_hash_matrix_buf"):
            buffer = getattr(cache, name)
            if buffer is not None:
                graph += int(buffer.nbytes) + 128
        assert cache._cache_bytes >= graph
        assert store_module.get_vector_cache_stats()["charged_bytes"] >= cache._cache_bytes


@pytest.mark.parametrize("content,tags", [
    ("Cedar recovery deployments use bounded workers.", []),
    ("Caf\u00e9\u2019s previous endpoint was 9; it is not the current endpoint at 80.", ["previous-not", "CAF\u00c9"]),
    ("dbCachePool db.connectionPool HTTP/2 api/v2 QUEUE_RETRY_4821 worker_count=12.", ["release-deployments", "routing.caf\u00e9"]),
    ("\uff43\uff41\uff46\uff45 \u0130zmir \u03b1\u03bb\u03c6\u03b1 cafe\u0301 STRA\u00dfE X9Gate.", ["08:00", "route_name"]),
])
def test_row_values_and_unique_graph_charge_stay_exact(isolated, content, tags):
    with memory_module.Mesh(namespace="canonical-values") as mesh:
        for prefix in ("First", "Second", "Third"):
            mesh.learn(prefix + " " + content, tags=tags, deduplicate=False)
        cache = mesh.store._get_vector_cache()
        _assert_pool(cache)
        _assert_charge(cache)


def test_pool_ownership_follows_update_delete_and_private_filtering(isolated):
    with memory_module.Mesh(namespace="canonical-lifecycle") as mesh:
        first = mesh.learn("Cedar recovery retries for ninety seconds.", deduplicate=False)["memory_id"]
        other = mesh.learn("Birch recovery retries for thirty seconds.", deduplicate=False)["memory_id"]
        private = mesh.learn("Pine recovery uses the northern private setting.", local_only=True, deduplicate=False)["memory_id"]
        cache = mesh.store._get_vector_cache()
        _assert_pool(cache)
        common = cache.lexical_strings["recovery"]
        mesh.update(first, tags=["release-deployments"])
        _assert_pool(cache)
        assert cache.token_to_ids is not cache.stem_to_ids
        assert cache.lexical_strings["recovery"] is common
        mesh.store.delete(first)
        assert "cedar" not in cache.lexical_strings
        _assert_pool(cache)
        mesh.update(other, content="Birch queue retries for thirty seconds.")
        assert "recovery" in cache.lexical_strings
        public = mesh.recall("recovery", n=10, include_private=False)
        assert private not in {row["id"] for row in public}
        assert "northern" not in json.dumps(public)
        mesh.store.delete(private)
        assert "recovery" not in cache.lexical_strings
        _assert_pool(cache)
        _assert_charge(cache)
        store_module._evict_vector_cache_for_db_path(mesh.store.db_path)
        rebuilt = mesh.store._get_vector_cache()
        _assert_pool(rebuilt)
        _assert_charge(rebuilt)


def test_canonical_strings_are_separate_between_namespaces_and_generations(isolated):
    with memory_module.Mesh(namespace="canonical-first") as first, memory_module.Mesh(namespace="canonical-second") as other:
        for mesh in (first, other):
            mesh.learn("Cedar recovery needs bounded workers.", deduplicate=False)
        old = first.store._get_vector_cache()
        separate = other.store._get_vector_cache()
        assert old.lexical_strings["recovery"] is not separate.lexical_strings["recovery"]
        before = old._cache_bytes
        store_module._evict_vector_cache_for_db_path(first.store.db_path)
        assert store_module.get_vector_cache_stats()["charged_bytes"] >= before
        fresh = first.store._get_vector_cache()
        assert fresh.lexical_strings["recovery"] is not old.lexical_strings["recovery"]
        _assert_pool(fresh)
        _assert_charge(fresh)


def test_counter_only_tag_stem_keeps_its_row_owner_after_indexed_owner_deletion(isolated):
    with memory_module.Mesh(namespace="canonical-counter-owner") as mesh:
        owner = mesh.learn("Cedar releasedeployment has a recorded policy.", deduplicate=False)["memory_id"]
        receiver = mesh.learn("Birch has documented retry rules.", tags=["release-deployments"], deduplicate=False)["memory_id"]
        cache = mesh.store._get_vector_cache()
        orphan = "releasedeployment"
        lexical = cache.lexical_by_id[receiver]
        assert orphan in lexical["term_freq"]
        assert orphan not in lexical["indexed_tokens"]
        owned = next(token for token in lexical["term_freq"] if token == orphan)
        assert owned is not cache.lexical_strings[orphan]
        mesh.store.delete(owner)
        assert orphan not in cache.lexical_strings
        assert lexical["term_freq"][owned] == 1
        _assert_pool(cache)
        _assert_charge(cache)
        store_module._evict_vector_cache_for_db_path(mesh.store.db_path)
        rebuilt = mesh.store._get_vector_cache()
        assert orphan not in rebuilt.lexical_strings
        _assert_pool(rebuilt)
        _assert_charge(rebuilt)


def test_pool_strings_and_primary_posting_keys_have_one_owner(isolated):
    with memory_module.Mesh(namespace="canonical-accounting") as mesh:
        mesh.learn("Cedar recovery needs bounded workers.", deduplicate=False)
        mesh.learn("Birch recovery needs bounded workers.", deduplicate=False)
        cache = mesh.store._get_vector_cache()
        selected = set(cache.lexical_strings)
        names = ("lexical_strings", "stem_to_ids", "token_to_ids", "stem_df", "visible_stem_df")
        keys = {name: selected for name in names}
        indexes = {id(getattr(cache, name)): getattr(cache, name) for name in names}
        expected = sum(sys.getsizeof(index[key]) for index in indexes.values() for key in selected if key in index)
        assert cache._index_value_bytes(keys) == expected
        assert cache._index_value_bytes({"lexical_strings": selected}) == sum(sys.getsizeof(token) for token in cache.lexical_strings.values())
        _assert_pool(cache)
        _assert_charge(cache)


def test_pool_reservation_exhaustion_preserves_the_committed_update(isolated, monkeypatch):
    with memory_module.Mesh(namespace="canonical-bounded-update") as mesh:
        identifier = mesh.learn("Cedar recovery needs bounded workers.", deduplicate=False)["memory_id"]
        cache = mesh.store._get_vector_cache()
        before = set(cache.lexical_strings)
        monkeypatch.setenv("MESH_MAX_VECTOR_CACHE_BYTES", str(cache._cache_bytes + 16384 + 512))
        mesh.update(identifier, tags=["release-deployments"])
        assert json.loads(mesh.get(identifier)["tags"]) == ["release-deployments"]
        assert not cache._valid
        assert cache.lexical_strings.keys() == before
