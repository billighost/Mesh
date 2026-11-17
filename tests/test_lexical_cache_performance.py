"""Value, privacy, allocation and bounded-work contracts for lexical caches."""
from __future__ import annotations

import json

import pytest

from mesh import memory as memory_module
from mesh import store as store_module


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name, folder in (("MESH_HOME", "mesh"), ("HOME", "home"), ("USERPROFILE", "home"),
                         ("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata"), ("XDG_CONFIG_HOME", "xdg")):
        location = tmp_path / folder
        location.mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(location))
    for name in ("MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_FORCE_HASH_EMBED"):
        monkeypatch.setenv(name, "1")
    for name in ("MESH_BG_ONNX", "MESH_BG_ONNX_DRAIN_ON_EXIT", "MESH_AUTO_APPROVE", "MESH_VAULT_SECRETS"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setattr(memory_module, "_trigger_background_bootstrap", lambda: None)
    monkeypatch.setattr(memory_module, "_get_model", lambda: (memory_module._hash_embed_384, "hash-fallback"))
    yield


def test_repeated_words_keep_exact_counts_without_repeated_python_stems(monkeypatch):
    original = store_module._fast_stem
    calls = []

    def stem(token):
        calls.append(token)
        return original(token)

    monkeypatch.setattr(store_module, "_fast_stem", stem)
    text = "Deployment deployments deployment deployed deploying HTTP/2 Caf\u00e9 " * 1000
    lexical = store_module._build_row_lexical_entry(text, [])
    assert lexical["doc_len"] == 8000
    assert lexical["term_freq"]["deployment"] == 3000
    assert lexical["term_freq"]["deploy"] == 2000
    assert lexical["term_freq"]["caf\u00e9"] == 1000
    assert lexical["term_freq"]["http/2"] == 1
    assert len(calls) < 32


@pytest.mark.parametrize("text,tags", [
    ("", []), ("the and or", []),
    ("Caf\u00e9\u2019s previous endpoint was 9; it is not the current endpoint at 80.", ["previous-not", "CAF\u00c9"]),
    ("dbCachePool db.connectionPool HTTP/2 api/v2 QUEUE_RETRY_4821 worker_count=12.", ["release-deployments", "routing.caf\u00e9"]),
    ("\uff43\uff41\uff46\uff45 \u0130zmir \u03b1\u03bb\u03c6\u03b1 cafe\u0301 STRA\u00dfE X9Gate.", ["08:00", "route_name"]),
])
def test_statistics_projection_keeps_literal_stems_and_lengths(text, tags):
    lexical = store_module._build_row_lexical_entry(text, tags)
    assert store_module._build_row_lexical_statistics(text, tags) == (lexical["doc_len"], lexical["df_stems"])


def test_statistics_projection_does_not_construct_term_frequencies(monkeypatch):
    monkeypatch.setattr(store_module, "Counter", lambda *args, **kwargs: pytest.fail("Statistics built term frequencies"))
    length, stems = store_module._build_row_lexical_statistics("Cedar deployments never use api/v2 in staging.", ["release-deployments"])
    assert length > 0
    assert {"never", "api/v2", "deployment"}.issubset(stems)


def test_row_stem_memo_has_entry_byte_and_lifetime_bounds():
    memo = store_module._RowStemMemo(max_entries=4, max_bytes=2048)
    for index in range(1200):
        token = "\u03b1" * (index % 127 + 1) + str(index) + "deployments"
        assert memo(token) == store_module._fast_stem(token)
        assert len(memo._cache) <= 4
        assert memo.retained_bytes <= 2048
    uncached = store_module._RowStemMemo(max_entries=0, max_bytes=2048)
    assert uncached("deployments") == "deployment"
    assert not uncached._cache
    assert store_module._RowStemMemo()._cache == {}


@pytest.mark.parametrize("entries,bytes_", [(True, 2048), (-1, 2048), (4, True), (4, 0)])
def test_row_stem_memo_rejects_invalid_limits(entries, bytes_):
    with pytest.raises(ValueError):
        store_module._RowStemMemo(max_entries=entries, max_bytes=bytes_)


def _fingerprint(cache):
    with cache.read_snapshot():
        return {
            "tokens": {token: set(ids) for token, ids in cache.token_to_ids.items()},
            "stems": {stem: set(ids) for stem, ids in cache.stem_to_ids.items()},
            "df": dict(cache.stem_df), "visible_df": dict(cache.visible_stem_df),
            "length": cache.doc_len_total, "visible_length": cache.visible_doc_len_total,
        }


def test_identical_postings_share_storage_then_split_before_tag_divergence(isolated):
    with memory_module.Mesh(namespace="postings-diverge") as mesh:
        first = mesh.learn("Cedar recovery retries for ninety seconds.", deduplicate=False)["memory_id"]
        other = mesh.learn("Birch recovery retries for thirty seconds.", deduplicate=False)["memory_id"]
        private = mesh.learn("Pine recovery private northern setting.", local_only=True, deduplicate=False)["memory_id"]
        cache = mesh.store._get_vector_cache()
        assert cache.token_to_ids is cache.stem_to_ids
        mesh.update(first, tags=["release-deployments"])
        cache = mesh.store._get_vector_cache()
        assert cache.token_to_ids is not cache.stem_to_ids
        assert first in cache.token_to_ids["release-deployments"]
        assert "release-deployments" not in cache.stem_to_ids
        mesh.store.delete(other)
        actual = _fingerprint(cache)
        store_module._evict_vector_cache_for_db_path(mesh.store.db_path)
        rebuilt = mesh.store._get_vector_cache()
        assert _fingerprint(rebuilt) == actual
        public = mesh.recall("recovery", n=10, include_private=False)
        assert private not in {row["id"] for row in public}
        assert "northern" not in json.dumps(public)


def test_shared_and_split_postings_are_conservatively_charged(isolated):
    with memory_module.Mesh(namespace="postings-accounting") as mesh:
        identifier = mesh.learn("Cedar recovery uses bounded workers.", deduplicate=False)["memory_id"]
        mesh.learn("Birch recovery uses bounded workers.", deduplicate=False)
        for tags in ([], ["release-deployments"]):
            if tags:
                mesh.update(identifier, tags=tags)
            cache = mesh.store._get_vector_cache()
            with cache.read_snapshot():
                assert cache._cache_bytes == cache._top_bytes() + cache._rows_bytes + cache._index_bytes
                fields = {name: getattr(cache, name) for name in store_module._CACHE_TOP_FIELDS}
                actual = store_module._object_graph_bytes(fields)
                for name in ("_matrix_buf", "_hash_matrix_buf"):
                    buffer = getattr(cache, name)
                    if buffer is not None:
                        actual += int(buffer.nbytes) + 128
                assert cache._cache_bytes >= actual
                assert store_module.get_vector_cache_stats()["charged_bytes"] >= cache._cache_bytes


def test_index_split_exhaustion_preserves_a_committed_update(isolated, monkeypatch):
    with memory_module.Mesh(namespace="postings-bounded-split") as mesh:
        identifier = mesh.learn("Cedar recovery retries ninety seconds.", deduplicate=False)["memory_id"]
        mesh.learn("Birch recovery retries thirty seconds.", deduplicate=False)
        cache = mesh.store._get_vector_cache()
        assert cache.token_to_ids is cache.stem_to_ids
        before = cache._cache_bytes
        monkeypatch.setenv("MESH_MAX_VECTOR_CACHE_BYTES", str(before + 16384 + 512))
        mesh.update(identifier, tags=["release-deployments"])
        assert json.loads(mesh.get(identifier)["tags"]) == ["release-deployments"]
        assert not cache._valid


def test_streaming_statistics_avoid_one_full_lexical_pass(isolated, monkeypatch):
    with memory_module.Mesh(namespace="postings-stream-statistics") as mesh:
        for index in range(12):
            mesh.learn(f"Cedar recovery evidence record {index} uses bounded workers.", deduplicate=False)

        def no_cache():
            raise store_module.CacheBudgetExceededError("Fixture bounded streaming")

        monkeypatch.setattr(mesh.store, "_get_vector_cache", no_cache)
        original_full = store_module._build_row_lexical_entry
        original_statistics = store_module._build_row_lexical_statistics
        calls = {"full": 0, "statistics": 0}

        def full(*args):
            calls["full"] += 1
            return original_full(*args)

        def statistics(*args):
            calls["statistics"] += 1
            return original_statistics(*args)

        monkeypatch.setattr(store_module, "_build_row_lexical_entry", full)
        monkeypatch.setattr(store_module, "_build_row_lexical_statistics", statistics)
        rows = mesh.store.hybrid_query("Cedar recovery evidence", memory_module._hash_embed_384("Cedar recovery evidence").tolist(),
                                       n=3, query_embedding_model="hash-384", include_private=False)
        assert len(rows) == 3
        assert calls == {"full": 24, "statistics": 12}
