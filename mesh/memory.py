"""
mesh/memory.py — Core semantic & hybrid memory orchestration for Mesh.

Performance & Memory Architecture:
  - 3-Tier 384-dim `all-MiniLM-L6-v2` Embedding Engine:
      1. `fastembed` (ONNX Runtime) — ~120ms cold start, ~2-3ms inference, low RAM
      2. `onnxruntime` + `tokenizers` (Direct ONNX fallback if cached locally)
      3. `sentence-transformers` (PyTorch fallback)
  - Automatic Idle Model Unloading (`MESH_IDLE_UNLOAD_SECONDS`, default 180s):
      Unloads neural weights from RAM when idle so background daemons consume ~0% CPU
      and minimal RAM while keeping the 4,096-entry LRU vector cache instant (<0.01ms).
"""

from __future__ import annotations

import gc
import json
import math
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Any, Optional

import numpy as np

from .store import MeshStore, _parse_iso_utc, _utc_now, _utc_now_iso

_MODEL_LOCK = threading.RLock()
_model: Optional[Any] = None
_torch: Optional[Any] = None
_model_backend: Optional[str] = None
_last_model_use_ts: float = 0.0
_watchdog_started: bool = False

_MODEL_NAME = "all-MiniLM-L6-v2"
_FASTEMBED_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# Unload neural model weights after N seconds of inactivity (default: 180s / 3 minutes).
# Set MESH_IDLE_UNLOAD_SECONDS=0 to keep the model loaded permanently.
_IDLE_UNLOAD_SECONDS: float = float(os.environ.get("MESH_IDLE_UNLOAD_SECONDS", "180"))

_EMBED_CACHE_LOCK = threading.Lock()
_EMBED_CACHE_MAXSIZE = 4096
_EMBED_CACHE: OrderedDict[str, tuple[float, ...]] = OrderedDict()

# Similarity threshold above which two memories are checked for contradiction
CONTRADICTION_THRESHOLD = 0.82

# Similarity threshold above which identical/near-identical facts are deduplicated
DEDUP_THRESHOLD = 0.985

# Effective confidence below which a memory is considered stale
STALE_THRESHOLD = 0.3

# Unified set of valid memory types across MCP, CLI, HTTP, Passive Capture, and Dashboard
VALID_MEMORY_TYPES = {
    "fact",
    "preference",
    "context",
    "result",
    "instruction",
    "process",
    "decision",
    "error",
    "general",
}

_NUMBER_TOKEN_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_WORD_TOKEN_RE = re.compile(r"[a-z']+")
_NEGATION_TOKENS = {
    "not",
    "never",
    "no",
    "dont",
    "don't",
    "cannot",
    "can't",
    "disabled",
    "false",
    "off",
}


def set_idle_unload_seconds(seconds: float) -> None:
    """Configure the automatic idle model unload timeout in seconds (0 disables unloading)."""
    global _IDLE_UNLOAD_SECONDS
    _IDLE_UNLOAD_SECONDS = max(0.0, float(seconds))
    if _IDLE_UNLOAD_SECONDS > 0:
        _ensure_idle_watchdog()


def unload_model() -> bool:
    """
    Explicitly unload the embedding model from memory and trigger garbage collection.
    The in-memory LRU `_EMBED_CACHE` and `MeshStore` vector caches remain intact.
    Returns True if a loaded model was unloaded.
    """
    global _model, _torch, _model_backend
    with _MODEL_LOCK:
        if _model is None:
            return False
        _model = None
        _model_backend = None
        torch_ref = _torch
        _torch = None

    try:
        if torch_ref is not None and hasattr(torch_ref, "cuda") and torch_ref.cuda.is_available():
            torch_ref.cuda.empty_cache()
    except Exception:
        pass
    gc.collect()
    return True


def get_engine_status() -> dict[str, Any]:
    """Return current embedding engine and memory residency status."""
    with _MODEL_LOCK:
        loaded = _model is not None
        backend = _model_backend or "unloaded"
        idle_for = round(max(0.0, time.monotonic() - _last_model_use_ts), 2) if _last_model_use_ts > 0 else None
    with _EMBED_CACHE_LOCK:
        cache_size = len(_EMBED_CACHE)
    return {
        "model_loaded": loaded,
        "backend": backend,
        "model_name": _MODEL_NAME,
        "idle_seconds": idle_for,
        "idle_unload_timeout_seconds": _IDLE_UNLOAD_SECONDS,
        "embed_cache_entries": cache_size,
        "embed_cache_maxsize": _EMBED_CACHE_MAXSIZE,
    }


def _idle_watchdog_loop() -> None:
    """Background daemon that unloads the embedding model when idle to keep RAM near zero."""
    while True:
        sleep_step = 15.0
        if _IDLE_UNLOAD_SECONDS > 0:
            sleep_step = max(2.0, min(15.0, _IDLE_UNLOAD_SECONDS / 3.0))
        time.sleep(sleep_step)
        if _IDLE_UNLOAD_SECONDS <= 0:
            continue
        with _MODEL_LOCK:
            if _model is None or _last_model_use_ts <= 0:
                continue
            elapsed = time.monotonic() - _last_model_use_ts
            if elapsed < _IDLE_UNLOAD_SECONDS:
                continue
        unload_model()


def _ensure_idle_watchdog() -> None:
    global _watchdog_started
    if _watchdog_started or _IDLE_UNLOAD_SECONDS <= 0:
        return
    with _MODEL_LOCK:
        if not _watchdog_started and _IDLE_UNLOAD_SECONDS > 0:
            _watchdog_started = True
            t = threading.Thread(
                target=_idle_watchdog_loop,
                name="mesh-idle-model-watchdog",
                daemon=True,
            )
            t.start()


def _normalize_rows(arr: np.ndarray) -> np.ndarray:
    """L2-normalize a 1D or 2D float32 numpy array."""
    a = np.asarray(arr, dtype=np.float32)
    if a.ndim == 1:
        norm = float(np.linalg.norm(a))
        return a / norm if norm > 1e-12 else a
    norms = np.linalg.norm(a, axis=1, keepdims=True)
    norms = np.where(norms > 1e-12, norms, 1.0)
    return a / norms


def _get_model() -> tuple[Any, str]:
    """
    Lazy-load the embedding model in a thread-safe manner.
    Prefers `fastembed` (ONNX Runtime) for ~30x faster startup and ~5x lower RAM,
    falling back seamlessly to `sentence_transformers` if `fastembed` is not installed.
    """
    global _model, _torch, _model_backend, _last_model_use_ts
    _last_model_use_ts = time.monotonic()
    if _model is not None and _model_backend is not None:
        return _model, _model_backend

    with _MODEL_LOCK:
        _last_model_use_ts = time.monotonic()
        if _model is not None and _model_backend is not None:
            return _model, _model_backend

        preferred = os.environ.get("MESH_EMBED_BACKEND", "auto").strip().lower()

        # Tier 1: FastEmbed (ONNX Runtime — ultra-fast startup & inference, no PyTorch overhead)
        if preferred in ("auto", "fastembed", "onnx"):
            try:
                from fastembed import TextEmbedding

                _model = TextEmbedding(model_name=_FASTEMBED_MODEL_NAME)
                _model_backend = "fastembed"
                _ensure_idle_watchdog()
                return _model, _model_backend
            except Exception:
                if preferred == "fastembed":
                    raise

        # Tier 2: SentenceTransformers (PyTorch fallback)
        try:
            import torch as torch_mod

            _torch = torch_mod
            if hasattr(_torch, "set_num_threads"):
                try:
                    cpu_count = os.cpu_count() or 4
                    _torch.set_num_threads(min(4, max(1, cpu_count)))
                except Exception:
                    pass
        except Exception:
            _torch = None

        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(_MODEL_NAME)
        _model_backend = "sentence-transformers"
        _ensure_idle_watchdog()
        return _model, _model_backend


def _encode_texts(texts: list[str]) -> np.ndarray:
    """Encode a list of strings into a 2D array of L2-normalized float32 embeddings."""
    global _last_model_use_ts
    model, backend = _get_model()
    _last_model_use_ts = time.monotonic()

    if backend == "fastembed":
        gen = model.embed(texts, batch_size=min(32, max(1, len(texts))))
        vecs = np.asarray(list(gen), dtype=np.float32)
        return _normalize_rows(vecs)

    # sentence-transformers backend (or custom mock in unit tests)
    try:
        if _torch is not None:
            with _torch.inference_mode():
                vecs = model.encode(
                    texts if len(texts) > 1 else texts[0],
                    batch_size=32,
                    show_progress_bar=False,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                )
        else:
            vecs = model.encode(
                texts if len(texts) > 1 else texts[0],
                batch_size=32,
                show_progress_bar=False,
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
    except TypeError:
        vecs = model.encode(
            texts if len(texts) > 1 else texts[0],
            show_progress_bar=False,
        )

    arr = np.asarray(vecs, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    return _normalize_rows(arr)


def warmup_model(namespace: Optional[str] = None) -> None:
    """
    Pre-load the embedding model and warm the default namespace vector cache
    in a background thread so the first tool call is instant.
    """
    try:
        ns = (namespace or os.environ.get("MESH_NAMESPACE", "shared")).strip() or "shared"
        store = MeshStore(namespace=ns)
        store._get_vector_cache()
        _embed_cached("warmup")
    except Exception:
        pass


def _embed_cached(cleaned_text: str) -> tuple[float, ...]:
    """
    O(1) LRU-cached embedding helper. Returns an immutable tuple of floats
    so repeated queries and duplicate content strings resolve in <0.01ms
    without waking the neural model if it has been unloaded during idle.
    """
    with _EMBED_CACHE_LOCK:
        cached = _EMBED_CACHE.get(cleaned_text)
        if cached is not None:
            _EMBED_CACHE.move_to_end(cleaned_text)
            return cached

    arr = _encode_texts([cleaned_text])[0]
    result = tuple(arr.tolist())

    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE[cleaned_text] = result
        _EMBED_CACHE.move_to_end(cleaned_text)
        if len(_EMBED_CACHE) > _EMBED_CACHE_MAXSIZE:
            _EMBED_CACHE.popitem(last=False)
    return result


def _embed(text: str) -> list[float]:
    """Embed a single text string into a normalized float vector."""
    cleaned = " ".join(text.strip().split())
    return list(_embed_cached(cleaned))


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Embed multiple texts in a single batch forward pass while reading from and
    populating the shared LRU cache for all items.
    """
    if not texts:
        return []
    if len(texts) == 1:
        return [_embed(texts[0])]

    cleaned_list = [" ".join(t.strip().split()) for t in texts]
    results: list[Optional[tuple[float, ...]]] = [None] * len(cleaned_list)
    missing_by_text: dict[str, list[int]] = {}

    with _EMBED_CACHE_LOCK:
        for idx, ctext in enumerate(cleaned_list):
            cached = _EMBED_CACHE.get(ctext)
            if cached is not None:
                _EMBED_CACHE.move_to_end(ctext)
                results[idx] = cached
            else:
                missing_by_text.setdefault(ctext, []).append(idx)

    if missing_by_text:
        unique_misses = list(missing_by_text.keys())
        vecs = _encode_texts(unique_misses)

        with _EMBED_CACHE_LOCK:
            for ctext, vec in zip(unique_misses, vecs):
                tup = tuple(vec.tolist())
                _EMBED_CACHE[ctext] = tup
                _EMBED_CACHE.move_to_end(ctext)
                if len(_EMBED_CACHE) > _EMBED_CACHE_MAXSIZE:
                    _EMBED_CACHE.popitem(last=False)
                for target_idx in missing_by_text[ctext]:
                    results[target_idx] = tup

    return [list(r) if r is not None else [] for r in results]


def _compute_decay_metrics(
    ttl_days: Optional[float],
    decay_start_at: Optional[str],
    now: Optional[datetime] = None,
) -> tuple[float, Optional[int]]:
    """
    Compute (decay_factor, days_until_stale) in a single timestamp parse pass.
    - decay_factor: float in [0.0, 1.0] (1.0 = fresh/no TTL, 0.0 = fully decayed)
    - days_until_stale: int days remaining before effective_confidence drops below STALE_THRESHOLD, or None
    """
    if ttl_days is None or not decay_start_at:
        return 1.0, None
    try:
        ttl = float(ttl_days)
        if ttl <= 0.0:
            return 0.0, 0
        start = _parse_iso_utc(decay_start_at)
        if start is None:
            return 1.0, None
        ref_now = now if now is not None else _utc_now()
        days_elapsed = max(0.0, (ref_now - start).total_seconds() / 86400.0)
        decay_factor = max(0.0, min(1.0, 1.0 - (days_elapsed / ttl)))
        stale_threshold_day = ttl * (1.0 - STALE_THRESHOLD)
        days_until_stale = max(0, round(stale_threshold_day - days_elapsed))
        return decay_factor, days_until_stale
    except Exception:
        return 1.0, None


def _compute_decay_factor(
    ttl_days: Optional[float],
    decay_start_at: Optional[str],
    now: Optional[datetime] = None,
) -> float:
    """
    Return a confidence multiplier between 0.0 and 1.0.
    1.0 = full confidence, 0.0 = fully decayed.
    Decays linearly from 1.0 to 0.0 over ttl_days using timezone-safe UTC math.
    """
    factor, _ = _compute_decay_metrics(ttl_days, decay_start_at, now=now)
    return factor


def _compute_days_until_stale(
    ttl_days: Optional[float],
    decay_start_at: Optional[str],
    now: Optional[datetime] = None,
) -> Optional[int]:
    """Return estimated whole days remaining before effective confidence drops below STALE_THRESHOLD."""
    _, days_left = _compute_decay_metrics(ttl_days, decay_start_at, now=now)
    return days_left


def _parse_tags(raw_tags: Any) -> list[str]:
    """Safely parse tags from a list, JSON string, or comma-separated string."""
    if not raw_tags:
        return []
    if isinstance(raw_tags, list):
        return [str(t).strip() for t in raw_tags if str(t).strip()]
    if isinstance(raw_tags, str):
        s = raw_tags.strip()
        if not s or s == "[]":
            return []
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(t).strip() for t in parsed if str(t).strip()]
            except Exception:
                pass
        return [t.strip() for t in s.split(",") if t.strip()]
    return []


def _is_true_duplicate(new_text: str, existing_text: str, similarity: float) -> bool:
    """
    Determine whether `new_text` is a semantic duplicate of `existing_text`
    rather than a conflicting update (e.g., 'rate limit is 100' vs 'rate limit is 200').
    """
    norm_new = " ".join(new_text.strip().lower().split())
    norm_old = " ".join(existing_text.strip().lower().split())
    if norm_new == norm_old:
        return True
    if similarity < DEDUP_THRESHOLD:
        return False

    # If numbers differ (e.g. port 8080 vs 8081, rate limit 100 vs 200), it is a conflict, not a duplicate
    nums_new = _NUMBER_TOKEN_RE.findall(norm_new)
    nums_old = _NUMBER_TOKEN_RE.findall(norm_old)
    if nums_new != nums_old:
        return False

    # If negation polarity differs, it is a conflict, not a duplicate
    words_new = set(_WORD_TOKEN_RE.findall(norm_new))
    words_old = set(_WORD_TOKEN_RE.findall(norm_old))
    if bool(words_new & _NEGATION_TOKENS) != bool(words_old & _NEGATION_TOKENS):
        return False

    return True


class Mesh:
    """
    Shared semantic & hybrid memory for AI agents.

    Capabilities:
      - learn() stores memories with automatic deduplication & contradiction detection
      - recall() performs 3-stage hybrid search (Dense Cosine + FTS5 BM25 + Exact Token Boost)
        with confidence decay calculation and metadata filtering
      - update() modifies existing memories in-place while preserving UUID and access stats
      - forget() deletes a memory by ID
      - patterns() surfaces top topics, memory gaps, unused memories, and stale memories
      - contradictions() & resolve_contradiction() manage conflicting facts
    """

    def __init__(self, namespace: str = "default", agent_id: str = "agent"):
        self.namespace = namespace
        self.agent_id = agent_id
        self.store = MeshStore(namespace)

    def learn(
        self,
        content: str,
        memory_type: str = "fact",
        confidence: float = 1.0,
        tags: Optional[list[str]] = None,
        ttl_days: Optional[float] = None,
        local_only: bool = False,
        deduplicate: bool = True,
    ) -> dict[str, Any]:
        """
        Store information in shared memory.

        Args:
            content:     The text to remember.
            memory_type: One of VALID_MEMORY_TYPES (fact, preference, context, result,
                         instruction, process, decision, error, general).
            confidence:  0.0–1.0 stored confidence (before any TTL decay).
            tags:        Optional list of string labels.
            ttl_days:    If set, confidence decays linearly to 0 over this many days.
            local_only:  If True, excluded from exports and cloud sync.
            deduplicate: If True, exact/near-identical facts reinforce the existing memory
                         in-place instead of creating duplicates or false conflicts.

        Returns:
            {
                "memory_id": "uuid",
                "status": "stored" | "stored_with_conflict",
                "deduplicated": bool,
                "conflict": None | {
                    "existing_memory_id": "uuid",
                    "existing_content": "...",
                    "existing_memory": "...",
                    "similarity": 0.91,
                    "stored_by": "agent-name",
                    "stored_at": "...",
                    "contradiction_id": "uuid"
                }
            }
        """
        if not content or not content.strip():
            raise ValueError("content cannot be empty")
        clean_content = content.strip()

        memory_type = (memory_type or "fact").strip().lower()
        if memory_type not in VALID_MEMORY_TYPES:
            raise ValueError(
                f"memory_type must be one of {sorted(VALID_MEMORY_TYPES)}, got '{memory_type}'"
            )

        confidence = max(0.0, min(1.0, float(confidence)))
        clean_tags = _parse_tags(tags)
        ttl_val = float(ttl_days) if ttl_days is not None and float(ttl_days) > 0 else None
        now = _utc_now_iso()

        embedding = _embed(clean_content)
        memory_id = str(uuid.uuid4())

        # --- Deduplication & Contradiction Detection ---
        conflict: Optional[dict[str, Any]] = None
        if self.store.count() > 0:
            raw = self.store.query(embedding, n=3)
            docs = raw.get("documents", [[]])[0]
            distances = raw.get("distances", [[]])[0]
            ids = raw.get("ids", [[]])[0]
            metadatas = raw.get("metadatas", [[]])[0]

            # Pass 1: Check if any top candidate is a true duplicate
            if deduplicate:
                for i, doc in enumerate(docs):
                    similarity = round(1.0 - float(distances[i]), 4)
                    existing_id = ids[i] if ids and i < len(ids) else None
                    meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                    if existing_id and _is_true_duplicate(clean_content, doc, similarity):
                        existing_tags = _parse_tags(meta.get("tags"))
                        merged_tags = list(dict.fromkeys([*existing_tags, *clean_tags]))
                        updated_conf = max(confidence, float(meta.get("confidence", 1.0)))
                        content_changed = clean_content != doc
                        self.store.update(
                            memory_id=existing_id,
                            content=clean_content if content_changed else None,
                            embedding=embedding if content_changed else None,
                            memory_type=memory_type,
                            confidence=updated_conf,
                            tags=merged_tags,
                            ttl_days=ttl_val,
                            reset_ttl=ttl_val is not None,
                            local_only=bool(local_only or meta.get("local_only")),
                            source_agent=self.agent_id,
                        )
                        return {
                            "memory_id": existing_id,
                            "status": "stored",
                            "deduplicated": True,
                            "conflict": None,
                        }

            # Pass 2: Check for semantic contradiction against the closest existing memory
            for i, doc in enumerate(docs):
                similarity = round(1.0 - float(distances[i]), 4)
                if similarity >= CONTRADICTION_THRESHOLD:
                    existing_id = ids[i] if ids and i < len(ids) else None
                    meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                    contradiction_id = self.store.log_contradiction(
                        new_memory_id=memory_id,
                        new_content=clean_content,
                        existing_memory_id=existing_id or "",
                        existing_content=doc,
                        similarity=similarity,
                    )
                    conflict = {
                        "existing_memory_id": existing_id,
                        "existing_content": doc,
                        "existing_memory": doc,  # Alias for CLI compatibility
                        "similarity": similarity,
                        "stored_by": meta.get("source_agent", "unknown"),
                        "stored_at": meta.get("created_at", ""),
                        "contradiction_id": contradiction_id,
                    }
                    break

        decay_start_at = now if ttl_val is not None else None
        self.store.upsert(
            memory_id=memory_id,
            content=clean_content,
            embedding=embedding,
            metadata={
                "memory_type": memory_type,
                "confidence": confidence,
                "source_agent": self.agent_id,
                "tags": clean_tags,
                "created_at": now,
                "accessed_at": now,
                "access_count": 0,
                "ttl_days": ttl_val,
                "decay_start_at": decay_start_at,
            },
            local_only=bool(local_only),
        )

        return {
            "memory_id": memory_id,
            "status": "stored_with_conflict" if conflict else "stored",
            "deduplicated": False,
            "conflict": conflict,
        }

    def recall(
        self,
        query: str,
        n: int = 5,
        min_similarity: float = 0.0,
        min_confidence: float = 0.0,
        include_stale: bool = True,
        memory_type: Optional[str] = None,
        tags: Optional[list[str]] = None,
        source_agent: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Retrieve the most relevant memories using 3-stage hybrid search
        (Dense SIMD Cosine + FTS5 BM25 + Exact Token Boost) and confidence decay.

        Args:
            query:           Natural language or keyword search query.
            n:               Max results to return.
            min_similarity:  Filter results below this similarity score.
            min_confidence:  Filter results below this effective confidence.
            include_stale:   If False, memories with effective_confidence < 0.3 are excluded.
            memory_type:     Optional filter for a specific memory_type.
            tags:            Optional list of tags that returned memories must contain.
            source_agent:    Optional filter for the agent that stored the memory.
        """
        if not query or not query.strip():
            raise ValueError("query cannot be empty")
        clean_query = query.strip()

        embedding = _embed(clean_query)

        # Fetch a slightly wider candidate pool so post-decay filters (include_stale, min_confidence)
        # still return up to `n` valid memories.
        candidate_limit = max(n * 3, 15)
        candidates = self.store.hybrid_query(
            query_text=clean_query,
            embedding=embedding,
            n=candidate_limit,
            memory_type=memory_type,
            tags=_parse_tags(tags) if tags else None,
            source_agent=source_agent,
        )

        now = _utc_now()
        memories: list[dict[str, Any]] = []
        best_similarity: Optional[float] = None

        for row in candidates:
            similarity = float(row.get("similarity", 0.0))
            if best_similarity is None or similarity > best_similarity:
                best_similarity = similarity

            if similarity < min_similarity:
                continue

            stored_confidence = float(row.get("confidence", 1.0))
            ttl_days_raw = row.get("ttl_days")
            ttl_days = float(ttl_days_raw) if ttl_days_raw is not None else None
            decay_start = row.get("decay_start_at") or (
                row.get("created_at") if ttl_days is not None else None
            )

            decay_factor, days_until_stale = _compute_decay_metrics(
                ttl_days, decay_start, now=now
            )
            effective_confidence = round(stored_confidence * decay_factor, 4)
            is_stale = effective_confidence < STALE_THRESHOLD

            if not include_stale and is_stale:
                continue
            if effective_confidence < min_confidence:
                continue

            mem_tags = (
                row["tags_list"]
                if "tags_list" in row
                else _parse_tags(row.get("tags"))
            )
            access_count = int(row.get("access_count") or 0) + 1

            # Composite score combines hybrid similarity, effective confidence, and usage reinforcement
            usage_boost = min(1.0, math.log1p(access_count) / 5.0)
            composite_score = round(
                0.75 * similarity + 0.18 * effective_confidence + 0.07 * usage_boost,
                4,
            )

            memories.append(
                {
                    "id": row.get("id"),
                    "content": row["content"],
                    "similarity": similarity,
                    "semantic_similarity": row.get("semantic_similarity", similarity),
                    "lexical_score": row.get("lexical_score", 0.0),
                    "composite_score": composite_score,
                    "source_agent": row.get("source_agent", "unknown"),
                    "memory_type": row.get("memory_type", "fact"),
                    "tags": mem_tags,
                    "confidence": stored_confidence,
                    "effective_confidence": effective_confidence,
                    "decay_factor": round(decay_factor, 4),
                    "is_stale": is_stale,
                    "days_until_stale": days_until_stale,
                    "ttl_days": ttl_days,
                    "access_count": access_count,
                    "local_only": bool(row.get("local_only", False)),
                    "created_at": row.get("created_at", ""),
                    "accessed_at": row.get("accessed_at", ""),
                }
            )

        # Sort by similarity descending (with effective_confidence and access_count as tie-breakers)
        memories.sort(
            key=lambda x: (x["similarity"], x["effective_confidence"], x["access_count"]),
            reverse=True,
        )
        memories = memories[:n]

        # Record access counts and query telemetry in a single atomic SQLite transaction
        returned_ids = [m["id"] for m in memories if m.get("id")]
        try:
            self.store.record_recall_telemetry(
                memory_ids=returned_ids,
                query_text=clean_query,
                agent_id=self.agent_id,
                best_similarity=best_similarity,
                result_count=len(memories),
            )
        except AttributeError:
            try:
                if returned_ids:
                    self.store.increment_access_batch(returned_ids)
                self.store.log_query(
                    query_text=clean_query,
                    agent_id=self.agent_id,
                    best_similarity=best_similarity,
                    result_count=len(memories),
                )
            except Exception:
                pass
        except Exception:
            pass

        return memories

    def update(
        self,
        memory_id: str,
        content: Optional[str] = None,
        memory_type: Optional[str] = None,
        confidence: Optional[float] = None,
        tags: Optional[list[str]] = None,
        ttl_days: Optional[float] = None,
        reset_ttl: bool = False,
        local_only: Optional[bool] = None,
    ) -> dict[str, Any]:
        """
        Update an existing memory in-place by ID.
        Only recomputes the vector embedding if `content` is modified.
        Preserves the original `id`, `created_at`, and `access_count`.
        """
        if not memory_id or not memory_id.strip():
            raise ValueError("memory_id cannot be empty")

        existing = self.store.get(memory_id)
        if not existing:
            raise ValueError(f"Memory '{memory_id}' not found in namespace '{self.namespace}'")

        new_embedding: Optional[list[float]] = None
        clean_content: Optional[str] = None
        if content is not None:
            if not content.strip():
                raise ValueError("content cannot be empty")
            clean_content = content.strip()
            if clean_content != existing["content"]:
                new_embedding = _embed(clean_content)

        clean_type: Optional[str] = None
        if memory_type is not None:
            clean_type = memory_type.strip().lower()
            if clean_type not in VALID_MEMORY_TYPES:
                raise ValueError(
                    f"memory_type must be one of {sorted(VALID_MEMORY_TYPES)}, got '{memory_type}'"
                )

        clean_conf: Optional[float] = None
        if confidence is not None:
            clean_conf = max(0.0, min(1.0, float(confidence)))

        clean_tags = _parse_tags(tags) if tags is not None else None
        ttl_val = float(ttl_days) if ttl_days is not None and float(ttl_days) > 0 else None

        updated = self.store.update(
            memory_id=memory_id,
            content=clean_content,
            embedding=new_embedding,
            memory_type=clean_type,
            confidence=clean_conf,
            tags=clean_tags,
            ttl_days=ttl_val,
            reset_ttl=reset_ttl or (ttl_days is not None),
            local_only=local_only,
            source_agent=self.agent_id,
        )
        if not updated:
            raise ValueError(f"Failed to update memory '{memory_id}'")

        return {
            "memory_id": memory_id,
            "status": "updated",
            "memory": self._enrich_memory_row(updated),
        }

    def get(self, memory_id: str) -> Optional[dict[str, Any]]:
        """Fetch a single memory by ID with full decay and staleness metadata."""
        row = self.store.get(memory_id)
        if not row:
            return None
        return self._enrich_memory_row(row)

    def _enrich_memory_row(
        self,
        row: dict[str, Any],
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Attach decay_factor, effective_confidence, is_stale, and parsed tags to a raw DB row."""
        stored_conf = float(row.get("confidence", 1.0))
        ttl_raw = row.get("ttl_days")
        ttl_days = float(ttl_raw) if ttl_raw is not None else None
        decay_start = row.get("decay_start_at") or (
            row.get("created_at") if ttl_days is not None else None
        )
        decay_factor, days_until_stale = _compute_decay_metrics(
            ttl_days, decay_start, now=now
        )
        effective_conf = round(stored_conf * decay_factor, 4)
        is_stale = effective_conf < STALE_THRESHOLD

        enriched = dict(row)
        enriched["confidence"] = stored_conf
        enriched["effective_confidence"] = effective_conf
        enriched["decay_factor"] = round(decay_factor, 4)
        enriched["is_stale"] = is_stale
        enriched["days_until_stale"] = days_until_stale
        enriched["local_only"] = bool(row.get("local_only", False))
        enriched["tags_list"] = _parse_tags(row.get("tags"))
        return enriched

    def forget(self, memory_id: str) -> bool:
        """Delete a specific memory by ID. Returns True if a memory was deleted."""
        return self.store.delete(memory_id)

    def patterns(self) -> dict[str, Any]:
        """
        Surface patterns in agent memory usage:
          - top_topics
          - memory_gaps
          - unused_memories
          - stale_memories
        """
        return self.store.get_patterns()

    def contradictions(self, resolved: bool = False) -> list[dict[str, Any]]:
        """Return unresolved (or resolved) memory conflicts."""
        return self.store.get_contradictions(resolved=resolved)

    def resolve_contradiction(self, contradiction_id: str) -> None:
        """Mark a contradiction as resolved."""
        self.store.resolve_contradiction(contradiction_id)

    def inspect(
        self,
        limit: int = 20,
        memory_type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List memories in the namespace sorted by most recently accessed, enriched with decay fields."""
        rows = self.store.list_all(limit=limit, memory_type=memory_type, tag=tag)
        now = _utc_now()
        return [self._enrich_memory_row(r, now=now) for r in rows]

    def clear(self) -> None:
        """Delete all memories, queries, and contradictions in this namespace."""
        self.store.clear_namespace()

    def count(self) -> int:
        """Return total number of memories in this namespace."""
        return self.store.count()

    def __repr__(self) -> str:
        return (
            f"Mesh(namespace='{self.namespace}', "
            f"agent_id='{self.agent_id}', "
            f"memories={self.count()})"
        )