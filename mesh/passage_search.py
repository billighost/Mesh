"""Exact, parent-capped passage retrieval over one authorized store generation."""
from __future__ import annotations

import contextlib
import heapq
import itertools
import math
import sys
import weakref

from .crypto import decrypt_text_if_encrypted
from .passages import PASSAGE_FORMAT_VERSION, validate_spans
from .store import (
    EMBEDDING_DIM, _FRAMING_TOKENS, _MEMORY_COLUMNS, _CacheBudgetLease,
    _blob_to_vector, _build_row_lexical_entry, _build_row_lexical_statistics,
    _cache_row_peak_bytes, _keep_best_scored_id, _object_graph_bytes, _VectorCacheEntry, np,
)

_PASSAGE_IDENTITY = (
    "p.parent_id=m.id AND p.namespace=m.namespace AND p.parent_revision=m.revision "
    "AND p.parent_incarnation=m.incarnation AND p.embedding_model=m.embedding_model "
    "AND p.representation_version=m.passage_format_version"
)


def verify_passage_rows(connection) -> None:
    """Fail staged publication if derived ownership, identity or row counts differ."""
    if connection.execute(
        "SELECT 1 FROM memory_passages p LEFT JOIN memories m ON " + _PASSAGE_IDENTITY +
        " WHERE m.id IS NULL OR m.passage_format_version!=? OR m.passage_count<=0 "
        "OR typeof(p.ordinal)!='integer' OR typeof(p.start)!='integer' OR typeof(p.end)!='integer' LIMIT 1",
        (PASSAGE_FORMAT_VERSION,),
    ).fetchone():
        raise RuntimeError("Namespace maintenance passage identity check failed.")
    if connection.execute(
        "SELECT 1 FROM memories m WHERE typeof(m.passage_count)!='integer' OR m.passage_count<0 "
        "OR (m.passage_format_version='' AND m.passage_count!=0) "
        "OR (m.passage_count>0 AND (m.passage_format_version!=? "
        "OR m.passage_count!=(SELECT COUNT(*) FROM memory_passages p WHERE " + _PASSAGE_IDENTITY + ") "
        "OR (SELECT MIN(ordinal) FROM memory_passages p WHERE p.parent_id=m.id)!=0 "
        "OR (SELECT MAX(ordinal) FROM memory_passages p WHERE p.parent_id=m.id)!=m.passage_count-1)) LIMIT 1",
        (PASSAGE_FORMAT_VERSION,),
    ).fetchone():
        raise RuntimeError("Namespace maintenance passage generation is incomplete.")
    columns = ("id", "namespace", "content", "revision", "incarnation", "embedding_model", "passage_format_version", "passage_count")
    for raw in connection.execute("SELECT " + ",".join(columns) + " FROM memories WHERE passage_count>0"):
        parent = dict(zip(columns, raw))
        parent["content"] = decrypt_text_if_encrypted(parent["content"])
        if len(passage_metadata(connection, parent)) != parent["passage_count"]:
            raise RuntimeError("Namespace maintenance passage offsets or vectors are invalid.")


def _passage_vectors(connection, parent):
    return connection.execute(
        "SELECT embedding FROM memory_passages WHERE parent_id=? AND namespace=? "
        "AND parent_revision=? AND parent_incarnation=? AND embedding_model=? "
        "AND representation_version=? ORDER BY ordinal",
        (parent["id"], parent["namespace"], parent["revision"], parent["incarnation"], parent["embedding_model"], PASSAGE_FORMAT_VERSION),
    )


def passage_metadata(connection, parent):
    count = parent.get("passage_count", 0)
    if type(count) is not int or parent.get("passage_format_version") != PASSAGE_FORMAT_VERSION or not 0 < count <= len(parent["content"]):
        return []
    records = connection.execute(
        "SELECT ordinal,start,end FROM memory_passages WHERE parent_id=? AND namespace=? "
        "AND parent_revision=? AND parent_incarnation=? AND embedding_model=? "
        "AND representation_version=? ORDER BY ordinal LIMIT ?",
        (parent["id"], parent["namespace"], parent["revision"], parent["incarnation"],
         parent["embedding_model"], PASSAGE_FORMAT_VERSION, count + 1),
    ).fetchall()
    if len(records) != count or any(any(type(value) is not int for value in record) or record[0] != ordinal for ordinal, record in enumerate(records)):
        return []
    try:
        validate_spans(parent["content"], [(record[1], record[2]) for record in records])
        for raw in _passage_vectors(connection, parent):
            vector = _blob_to_vector(raw[0])
            if vector.shape != (EMBEDDING_DIM,) or not np.isfinite(vector).all():
                return []
    except ValueError:
        return []
    return records


def primary_reuse_ready(connection, parent, vector):
    """Validate a zero-row generation against its authoritative primary identity."""
    model = parent.get("embedding_model")
    if parent.get("passage_format_version") != PASSAGE_FORMAT_VERSION or type(parent.get("passage_count")) is not int or parent["passage_count"] != 0:
        return False
    if not isinstance(model, str) or not model or model in ("hash-384", "unknown", "needs-reembed"):
        return False
    if vector is None or vector.shape != (EMBEDDING_DIM,) or not np.isfinite(vector).all():
        return False
    return connection.execute(
        "SELECT 1 FROM memories m WHERE m.id=? AND m.namespace=? AND m.revision=? "
        "AND m.incarnation=? AND m.embedding_model=? AND m.passage_format_version=? "
        "AND m.passage_count=0 AND NOT EXISTS(SELECT 1 FROM memory_passages p "
        "WHERE p.parent_id=m.id AND p.namespace=m.namespace)",
        (parent["id"], parent["namespace"], parent["revision"], parent["incarnation"], model, PASSAGE_FORMAT_VERSION),
    ).fetchone() is not None


def _passage_lexical_charge(index, lexical):
    # The generation owns these strings even after a parent retires its pool.
    # Scratch is bounded by this entry; never copy the whole corpus vocabulary.
    seen = {id(token) for token in lexical["indexed_tokens"]
            if index.lexical_strings.get(token) is token}
    return _object_graph_bytes(lexical, seen)


def _cache_lexical_entry(cache, index, content):
    lexical = _build_row_lexical_entry(content, [])
    fresh = [cache.lexical_strings.get(token, token) for token in lexical["indexed_tokens"]
             if token not in index.lexical_strings]
    if fresh:
        before_size = sys.getsizeof(index.lexical_strings)
        owned = sum(sys.getsizeof(token) for token in fresh)
        # Reserve both dictionary generations before growth. New strings can
        # alias parent storage, but independently retain their passage charge.
        growth_peak = 4 * before_size + max(256, len(fresh) * 128)
        cache._lease.resize(cache._cache_bytes + index.allocated_bytes + owned + growth_peak + cache._build_peak_bytes)
        for token in fresh:
            index.lexical_strings[token] = token
        index.allocated_bytes += owned + sys.getsizeof(index.lexical_strings) - before_size
    else:
        cache._lease.resize(cache._cache_bytes + index.allocated_bytes + cache._build_peak_bytes)
    _VectorCacheEntry._canonicalize_row_lexical(index, lexical)
    return lexical


class PassageCacheIndex:
    """One float32 matrix and compact offsets; lexical entries contain no body copies."""

    def __init__(self, capacity):
        self.representation_version = PASSAGE_FORMAT_VERSION
        self.matrix = np.empty((capacity, EMBEDDING_DIM), dtype=np.float32)
        self.offsets = np.empty((capacity, 2), dtype=np.int64)
        self.lexical = [None] * capacity
        self.ranges = {}
        self.ready_primary_ids = set()
        self.implicit_lexical = {}
        self.lexical_strings = {}
        self.allocated_bytes = 2048 + capacity * (EMBEDDING_DIM * 4 + 16 + 128) + sys.getsizeof(self.lexical_strings)


def load_passage_cache(store, connection, cache, capacity):
    base = cache._cache_bytes
    initial = 2048 + capacity * (EMBEDDING_DIM * 4 + 16 + 128) + sys.getsizeof({})
    cache._lease.resize(base + initial + cache._build_peak_bytes)
    index = PassageCacheIndex(capacity)
    position = 0
    for parent in cache.rows_by_id.values():
        primary_index = cache.id_to_idx.get(parent["id"])
        primary = cache._matrix_buf[primary_index] if primary_index is not None and cache._matrix_buf is not None else None
        if primary_reuse_ready(connection, parent, primary):
            before_size = sys.getsizeof(index.ready_primary_ids)
            charge = _object_graph_bytes(parent["id"])
            # Set growth can keep the old bucket array live during allocation.
            cache._lease.resize(base + index.allocated_bytes + charge + 4 * before_size + cache._build_peak_bytes)
            index.ready_primary_ids.add(parent["id"])
            index.allocated_bytes += charge + sys.getsizeof(index.ready_primary_ids) - before_size
            continue
        records = passage_metadata(connection, parent)
        if not records:
            continue
        first = position
        valid = True
        vectors = _passage_vectors(connection, parent)
        for (ordinal, start, end), raw in zip(records, vectors):
            vector = _blob_to_vector(raw[0])
            if vector.shape != (EMBEDDING_DIM,) or not np.isfinite(vector).all():
                valid = False
                break
            lexical = _cache_lexical_entry(cache, index, parent["content"][start:end])
            charge = _passage_lexical_charge(index, lexical)
            cache._lease.resize(base + index.allocated_bytes + charge + cache._build_peak_bytes)
            index.matrix[position] = vector
            index.offsets[position] = (start, end)
            index.lexical[position] = lexical
            index.allocated_bytes += charge
            position += 1
        if valid:
            index.ranges[parent["id"]] = (first, position)
        else:
            # An incomplete derived bundle contributes no evidence; its parent
            # remains searchable through the existing primary representation.
            for offset in range(first, position):
                index.allocated_bytes -= _passage_lexical_charge(index, index.lexical[offset])
                index.lexical[offset] = None
            position = first
    for parent in cache.rows_by_id.values():
        if parent["tags_list"] and parent["id"] not in index.ranges:
            lexical = _cache_lexical_entry(cache, index, parent["content"])
            charge = _passage_lexical_charge(index, lexical) + 128
            cache._lease.resize(base + index.allocated_bytes + charge + cache._build_peak_bytes)
            index.implicit_lexical[parent["id"]] = lexical
            index.allocated_bytes += charge
    cache._lease.resize(base + index.allocated_bytes + cache._build_peak_bytes)
    return index


class _ResultEntry:
    def __init__(self, rank, similarity, identifier):
        self.rank, self.similarity, self.identifier = rank, similarity, identifier

    def __lt__(self, other):
        if self.rank != other.rank:
            return self.rank < other.rank
        if self.similarity != other.similarity:
            return self.similarity < other.similarity
        return self.identifier > other.identifier


class _PassageResults(list):
    pass


def _admit_best_unit(heap, choices, score, identifier, ordinal, limit):
    evicted = None
    if len(heap) >= limit:
        boundary = heap[0]
        if not (score > boundary.score or (score == boundary.score and identifier < boundary.memory_id)):
            return
        evicted = boundary.memory_id
    _keep_best_scored_id(heap, score, identifier, limit)
    # Each parent contributes once per channel. Only a displaced choice can
    # become stale, so admission never needs to traverse the retained heap.
    if evicted is not None:
        choices.pop(evicted, None)
    choices[identifier] = ordinal


def _parent_memory_rows(connection, where, params, identifiers=None):
    if identifiers is None:
        with contextlib.closing(connection.execute(f"SELECT {_MEMORY_COLUMNS},embedding FROM memories WHERE {where}", params)) as cursor:
            yield from cursor
        return
    selected = iter(identifiers)
    while block := tuple(itertools.islice(selected, 512)):
        placeholders = ",".join("?" for _ in block)
        with contextlib.closing(connection.execute(
            f"SELECT {_MEMORY_COLUMNS},embedding FROM memories WHERE {where} AND id IN ({placeholders})",
            (*params, *block),
        )) as cursor:
            yield from cursor


def passage_query(store, spec, *, cache, n, memory_type, tags, source_agent, include_private,
                  include_quarantined, include_stale, min_confidence, now, query_embedding_model):
    """Use one authorized corpus, or return None to retain the primary scorer."""
    streaming = cache is None
    where, params = store._query_sql_filter(
        memory_type=memory_type, source_agent=source_agent, include_private=include_private,
        include_quarantined=include_quarantined,
    )
    tag_set = {str(tag).strip().lower() for tag in tags or []}
    lease = _CacheBudgetLease((store.db_path, store.namespace))
    retained = 0
    base = 32768 + spec.pool * 3 * 1024 + _object_graph_bytes((spec.tokens, spec.stems, spec.synonyms, spec.stem_synonyms))
    handed_off = False
    try:
        with contextlib.ExitStack() as stack:
            connection = stack.enter_context(store._read_connect()) if streaming else None
            if cache is not None:
                stack.enter_context(cache.read_snapshot())
                if cache.passage_index_dirty:
                    from .store import CacheSnapshotInvalidatedError

                    raise CacheSnapshotInvalidatedError("Passage cache generation changed.")
                eligible = store._query_eligible_ids(
                    cache, memory_type=memory_type, tags=tags, source_agent=source_agent,
                    include_private=include_private, include_quarantined=include_quarantined,
                    include_stale=include_stale, min_confidence=min_confidence, now=now,
                )
                if any(identifier in eligible and (
                    cache.passages is None or identifier not in cache.passages.ranges
                    or (query_embedding_model is not None and cache.rows_by_id[identifier]["embedding_model"] != query_embedding_model)
                ) for identifier in cache.omitted_primary_lexical_ids):
                    return None
                if cache.passages is None or not any(
                    identifier in eligible and (query_embedding_model is None or cache.rows_by_id[identifier]["embedding_model"] == query_embedding_model)
                    for identifier in itertools.chain(cache.passages.ranges, cache.passages.ready_primary_ids)
                ):
                    return None
            lease.resize(base)
            if streaming:
                ready_where = where + " AND passage_format_version=? AND passage_count>=0"
                ready_params = (*params, PASSAGE_FORMAT_VERSION)
                if query_embedding_model is not None:
                    ready_where += " AND embedding_model=?"
                    ready_params = (*ready_params, query_embedding_model)
                for raw in connection.execute(f"SELECT {_MEMORY_COLUMNS},embedding FROM memories WHERE {ready_where}", ready_params):
                    lease.resize(base + _cache_row_peak_bytes(raw))
                    data = dict(raw)
                    blob = data.pop("embedding")
                    parent = store._deserialize_memory_row(data)
                    if not store._stream_row_eligible(parent, tag_set=tag_set, include_stale=include_stale, min_confidence=min_confidence, now=now):
                        continue
                    primary = _blob_to_vector(blob) if parent["passage_count"] == 0 else None
                    if primary_reuse_ready(connection, parent, primary) or passage_metadata(connection, parent):
                        break
                else:
                    return None
                raw = data = blob = parent = primary = None
                lease.resize(base)

            def parents(identifiers=None):
                if cache is not None:
                    selected = cache.rows_by_id.items() if identifiers is None else (
                        (identifier, cache.rows_by_id[identifier]) for identifier in identifiers
                    )
                    for identifier, parent in selected:
                        if identifier in eligible:
                            lease.resize(base + retained + 16384 + len(parent["content"]) * 8)
                            yield parent, None
                else:
                    for raw in _parent_memory_rows(connection, where, params, identifiers):
                        lease.resize(base + retained + _cache_row_peak_bytes(raw))
                        data = dict(raw)
                        blob = data.pop("embedding")
                        parent = store._deserialize_memory_row(data)
                        if store._stream_row_eligible(parent, tag_set=tag_set, include_stale=include_stale, min_confidence=min_confidence, now=now):
                            yield parent, blob

            def units(parent, blob, *, statistics=False):
                compatible = query_embedding_model is None or parent["embedding_model"] == query_embedding_model
                bounds = cache.passages.ranges.get(parent["id"]) if compatible and cache is not None and cache.passages is not None else None
                if bounds is not None:
                    first, end = bounds
                    for position in range(first, end):
                        start, stop = map(int, cache.passages.offsets[position])
                        yield position - first, start, stop, cache.passages.matrix[position] if compatible else None, cache.passages.lexical[position]
                    return
                records = passage_metadata(connection, parent) if compatible and connection is not None else []
                if records:
                    # Decoded vectors live only for the current row. Offset
                    # validation completed before any sibling contributes.
                    vectors = _passage_vectors(connection, parent) if not statistics else None
                    for ordinal, start, stop in records:
                        vector = None
                        if compatible and not statistics:
                            record = vectors.fetchone()
                            vector = _blob_to_vector(record[0])
                            if vector.shape != (EMBEDDING_DIM,) or not np.isfinite(vector).all():
                                raise ValueError("Stored passage vector is invalid.")
                        content = parent["content"][start:stop]
                        if statistics:
                            length, stems = _build_row_lexical_statistics(content, [])
                            lex = {"doc_len": length, "df_stems": stems}
                        else:
                            lex = _build_row_lexical_entry(content, [])
                        yield int(ordinal), int(start), int(stop), vector, lex
                    return
                if cache is not None:
                    position = cache.id_to_idx.get(parent["id"])
                    vector = cache._matrix_buf[position] if compatible and position is not None and cache._matrix_buf is not None else None
                    lexical = cache.passages.implicit_lexical.get(parent["id"])
                    if lexical is None:
                        lexical = cache.lexical_by_id[parent["id"]]
                else:
                    vector = store._stream_row_vector(parent, blob, query_embedding_model) if not statistics else None
                    if statistics:
                        length, stems = _build_row_lexical_statistics(parent["content"], [])
                        lexical = {"doc_len": length, "df_stems": stems}
                    else:
                        lexical = _build_row_lexical_entry(parent["content"], [])
                yield -1, 0, len(parent["content"]), vector, lexical

            query_stems = set(spec.stems)
            frequency = dict.fromkeys(query_stems, 0)
            total_units = total_length = 0
            for parent, blob in parents():
                for _, _, _, _, lexical in units(parent, blob, statistics=True):
                    total_units += 1
                    total_length += lexical["doc_len"]
                    for stem in query_stems:
                        frequency[stem] += stem in lexical["df_stems"]
            if not total_units:
                return []
            has_specific = any(stem not in _FRAMING_TOKENS for stem in spec.stems)
            idf = {stem: max(0.001, math.log1p((total_units - count + 0.5) / (count + 0.5)) *
                            (0.12 if has_specific and stem in _FRAMING_TOKENS else 1.0)) for stem, count in frequency.items()}
            query_idf = sum(idf.get(stem, 0.0) for stem in spec.stems) or 1.0
            average_length = total_length / total_units
            dense_heap, lexical_heap, exact_heap = [], [], []
            dense_units, lexical_units, exact_units = {}, {}, {}

            for parent, blob in parents():
                dense = lexical_best = exact = None
                for ordinal, _, _, vector, lexical in units(parent, blob):
                    semantic = float(vector @ spec.vector) if vector is not None else None
                    bm25 = store._cached_bm25(lexical, spec.stems, spec.stem_synonyms, idf, average_length)
                    if semantic is not None and (dense is None or (semantic, -ordinal) > dense):
                        dense = (semantic, -ordinal)
                    if bm25 > 0.0 and (lexical_best is None or (bm25, -ordinal) > lexical_best):
                        lexical_best = (bm25, -ordinal)
                    if any(token in lexical["indexed_tokens"] for token in spec.literals) and (exact is None or (bm25, -ordinal) > exact):
                        exact = (bm25, -ordinal)
                for heap, choices, best in ((dense_heap, dense_units, dense), (lexical_heap, lexical_units, lexical_best), (exact_heap, exact_units, exact)):
                    if best is not None:
                        _admit_best_unit(heap, choices, best[0], parent["id"], -best[1], spec.pool)
            dense_scores = {item.memory_id: item.score for item in dense_heap}
            bm25_scores = {item.memory_id: item.score for item in lexical_heap}
            exact_best = [item.memory_id for item in sorted(exact_heap, key=lambda item: (-item.score, item.memory_id))]
            candidate_ids = set(dense_scores) | set(bm25_scores) | set(exact_best)
            unit_rrf = {}
            for scores, choices, weight in ((dense_scores, dense_units, 1.0), (bm25_scores, lexical_units, 0.9), ({item.memory_id: item.score for item in exact_heap}, exact_units, 1.5)):
                for rank, identifier in enumerate(sorted(scores, key=lambda mid: (-scores[mid], mid)), 1):
                    key = (identifier, choices[identifier])
                    unit_rrf[key] = unit_rrf.get(key, 0.0) + weight / (60.0 + rank)
            kept, kept_charges, result_heap, rank_scores = {}, {}, [], {}
            evaluated = 0
            for parent, blob in parents(candidate_ids):
                identifier = parent["id"]
                best = None
                for ordinal, start, stop, vector, lexical in units(parent, blob):
                    semantic = float(vector @ spec.vector) if vector is not None else 0.0
                    if ordinal < 0 and vector is None and query_embedding_model == "hash-384":
                        from .memory import _hash_embed_384

                        semantic = float(_hash_embed_384(parent["content"]) @ spec.vector)
                    score_parent = {**parent, "content": parent["content"][start:stop], "tags_list": []}
                    candidate = store._hybrid_candidate(score_parent, lexical, semantic, spec, idf, query_idf, query_embedding_model)
                    if candidate is None:
                        continue
                    rank = candidate[3] if spec.mode == "calibrated" else unit_rrf.get((identifier, ordinal), 0.0)
                    quality = (rank, candidate[3], -ordinal)
                    if best is None or quality > best[0]:
                        candidate[0]["content"] = parent["content"]
                        candidate[0]["tags_list"] = parent["tags_list"]
                        candidate[0]["_passage_bm25_score"] = store._cached_bm25(lexical, spec.stems, spec.stem_synonyms, idf, average_length)
                        if ordinal >= 0:
                            candidate[0]["_supporting_passage"] = (start, stop, ordinal)
                        best = quality, candidate
                if best is None:
                    continue
                evaluated += 1
                quality, candidate = best
                item = _ResultEntry(quality[0], quality[1], identifier)
                if len(result_heap) >= n and not result_heap[0] < item:
                    continue
                charge = _object_graph_bytes(candidate)
                lease.resize(base + retained + charge + _cache_row_peak_bytes(parent))
                if len(result_heap) >= n:
                    worst = heapq.heapreplace(result_heap, item)
                    kept.pop(worst.identifier)
                    rank_scores.pop(worst.identifier)
                    retained -= kept_charges.pop(worst.identifier)
                else:
                    heapq.heappush(result_heap, item)
                kept[identifier], kept_charges[identifier] = candidate, charge
                rank_scores[identifier] = quality[0]
                retained += charge
            results = store._rank_hybrid_results(kept, dense_scores, bm25_scores, exact_best, spec, n,
                                                streaming=streaming, rank_scores=rank_scores, candidate_count=evaluated)
        results = store.revalidate_query_results(
            results, include_private=include_private, include_quarantined=include_quarantined,
            include_stale=include_stale, min_confidence=min_confidence, memory_type=memory_type,
            tags=tags, source_agent=source_agent,
        )
        results.sort(key=lambda row: (-row["rank_score"], -row["similarity"], row["id"]))
        output = _PassageResults(results)
        lease.resize(4096 + _object_graph_bytes(output))
        weakref.finalize(output, lease.release)
        handed_off = True
        return output
    finally:
        if not handed_off:
            lease.release()
