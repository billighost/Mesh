from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional



from .store import MeshStore

_model: Optional[SentenceTransformer] = None
_MODEL_NAME = "all-MiniLM-L6-v2"

# Similarity threshold above which two memories are considered potentially contradictory
CONTRADICTION_THRESHOLD = 0.82

# Effective confidence below which a memory is considered stale
STALE_THRESHOLD = 0.3


def _get_model() -> "SentenceTransformer":
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _embed(text: str) -> list[float]:
    return _get_model().encode(text, show_progress_bar=False).tolist()


def _compute_decay_factor(ttl_days: Optional[float],
                           decay_start_at: Optional[str]) -> float:
    """
    Return a multiplier between 0.0 and 1.0.
    1.0 = full confidence, 0.0 = fully decayed.
    Decays linearly from 1.0 to 0.0 over ttl_days.
    """
    if ttl_days is None or decay_start_at is None:
        return 1.0
    try:
        start = datetime.fromisoformat(decay_start_at)
        days_elapsed = (datetime.utcnow() - start).total_seconds() / 86400
        return max(0.0, 1.0 - (days_elapsed / ttl_days))
    except Exception:
        return 1.0


VALID_MEMORY_TYPES = {"fact", "preference", "context", "result", "instruction"}


class Mesh:
    """
    Shared semantic memory for AI agents — v0.2.

    New in v0.2:
      - learn() detects contradictions with existing memories before storing
      - learn() accepts ttl_days for time-limited memories with confidence decay
      - recall() returns effective_confidence and is_stale for each result
      - recall() logs every query for pattern analysis
      - patterns() surfaces what agents ask about, what's missing, what's unused
      - contradictions() returns unresolved memory conflicts
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
    ) -> dict:
        """
        Store information in shared memory.

        v0.2: Now returns a dict instead of a plain string,
        so callers can see contradiction warnings.

        Args:
            content:     The text to remember.
            memory_type: One of: fact, preference, context, result, instruction
            confidence:  0.0–1.0. Stored confidence (before any decay).
            tags:        Optional string labels.
            ttl_days:    If set, confidence decays linearly to 0 over this many days.
                         After ttl_days days, effective_confidence reaches 0.
                         Example: ttl_days=30 means the memory is stale after a month.
            local_only:  If True, this memory will never be included in cloud sync or exports.
                         Use for API keys, passwords, and sensitive project-specific data.

        Returns:
            {
                "memory_id": "uuid",
                "status": "stored" | "stored_with_conflict",
                "conflict": None | {
                    "existing_memory_id": "uuid",
                    "existing_content": "...",
                    "similarity": 0.91,
                    "stored_by": "agent-name",
                    "contradiction_id": "uuid"
                }
            }
        """
        if not content or not content.strip():
            raise ValueError("content cannot be empty")

        memory_type = memory_type.lower()
        if memory_type not in VALID_MEMORY_TYPES:
            raise ValueError(
                f"memory_type must be one of {VALID_MEMORY_TYPES}, got '{memory_type}'"
            )

        confidence = max(0.0, min(1.0, float(confidence)))
        now = datetime.utcnow().isoformat()
        memory_id = str(uuid.uuid4())
        embedding = _embed(content)

        # ── Contradiction detection ──────────────────────────────────
        conflict = None
        if self.store.count() > 0:
            raw = self.store.query(embedding, n=3)
            docs = raw.get("documents", [[]])[0]
            distances = raw.get("distances", [[]])[0]
            ids = raw.get("ids", [[]])[0]
            metadatas = raw.get("metadatas", [[]])[0]

            for i, doc in enumerate(docs):
                similarity = round(1.0 - distances[i], 4)
                if similarity >= CONTRADICTION_THRESHOLD:
                    meta = metadatas[i] if metadatas else {}
                    existing_id = ids[i] if ids else None

                    # Record the contradiction in SQLite
                    contradiction_id = self.store.log_contradiction(
                        new_memory_id=memory_id,
                        new_content=content,
                        existing_memory_id=existing_id or "",
                        existing_content=doc,
                        similarity=similarity,
                    )

                    conflict = {
                        "existing_memory_id": existing_id,
                        "existing_content": doc,
                        "similarity": similarity,
                        "stored_by": meta.get("source_agent", "unknown"),
                        "stored_at": meta.get("created_at", ""),
                        "contradiction_id": contradiction_id,
                    }
                    break  # Report first conflict only

        # ── Store the memory regardless of conflict ──────────────────
        # Always store — the agent decides what to do with the warning
        decay_start_at = now if ttl_days is not None else None

        self.store.upsert(
            memory_id=memory_id,
            content=content,
            embedding=embedding,
            metadata={
                "memory_type": memory_type,
                "confidence": confidence,
                "source_agent": self.agent_id,
                "tags": tags or [],
                "created_at": now,
                "ttl_days": ttl_days,
                "decay_start_at": decay_start_at,
            },
            local_only=local_only
        )

        return {
            "memory_id": memory_id,
            "status": "stored_with_conflict" if conflict else "stored",
            "conflict": conflict,
        }

    def recall(
        self,
        query: str,
        n: int = 5,
        min_similarity: float = 0.0,
        min_confidence: float = 0.0,
        include_stale: bool = True,
    ) -> list[dict]:
        """
        Retrieve the most semantically relevant memories.

        v0.2: Each result now includes:
          - effective_confidence: confidence after decay is applied
          - decay_factor: how much the confidence has decayed (1.0 = fresh)
          - is_stale: True if effective_confidence < 0.3
          - days_until_stale: estimated days remaining before stale (if ttl set)

        Args:
            query:           Natural language search.
            n:               Max results.
            min_similarity:  Filter below this similarity score.
            min_confidence:  Filter below this effective confidence.
            include_stale:   If False, stale memories are excluded from results.
        """
        if not query or not query.strip():
            raise ValueError("query cannot be empty")

        embedding = _embed(query)
        raw = self.store.query(embedding, n=n)

        ids = raw.get("ids", [[]])[0]
        docs = raw.get("documents", [[]])[0]
        distances = raw.get("distances", [[]])[0]
        metadatas = raw.get("metadatas", [[]])[0]

        memories = []
        best_similarity = None

        for i, doc in enumerate(docs):
            similarity = round(1.0 - distances[i], 4)
            meta = metadatas[i] if metadatas else {}

            # Track best similarity for query logging
            if best_similarity is None or similarity > best_similarity:
                best_similarity = similarity

            if similarity < min_similarity:
                continue

            # ── Decay calculation ────────────────────────────────────
            stored_confidence = float(meta.get("confidence", 1.0))

            # ttl_days and decay_start_at come from SQLite metadata
            # ChromaDB strips them if they're not str/int/float/bool,
            # but they are floats/strings so they survive
            ttl_days_raw = meta.get("ttl_days")
            ttl_days = float(ttl_days_raw) if ttl_days_raw is not None else None
            decay_start = meta.get("decay_start_at")

            decay_factor = _compute_decay_factor(ttl_days, decay_start)
            effective_confidence = round(stored_confidence * decay_factor, 4)
            is_stale = effective_confidence < STALE_THRESHOLD

            if not include_stale and is_stale:
                continue

            if effective_confidence < min_confidence:
                continue

            # Days until stale (only meaningful if ttl_days is set)
            days_until_stale = None
            if ttl_days is not None and decay_start:
                try:
                    start = datetime.fromisoformat(decay_start)
                    days_elapsed = (datetime.utcnow() - start).total_seconds() / 86400
                    remaining = ttl_days - days_elapsed
                    stale_threshold_day = ttl_days * (1 - STALE_THRESHOLD)
                    days_until_stale = max(0, round(stale_threshold_day - days_elapsed))
                except Exception:
                    pass

            memory = {
                "id": ids[i] if ids else None,
                "content": doc,
                "similarity": similarity,
                "source_agent": meta.get("source_agent", "unknown"),
                "memory_type": meta.get("memory_type", "fact"),
                "confidence": stored_confidence,
                "effective_confidence": effective_confidence,
                "decay_factor": round(decay_factor, 4),
                "is_stale": is_stale,
                "days_until_stale": days_until_stale,
                "local_only": bool(meta.get("local_only", 0)),
                "created_at": meta.get("created_at", ""),
            }
            memories.append(memory)

            # Track access
            if ids and ids[i]:
                try:
                    self.store.increment_access(ids[i])
                except Exception:
                    pass

        # ── Log the query for pattern analysis ──────────────────────
        try:
            self.store.log_query(
                query_text=query,
                agent_id=self.agent_id,
                best_similarity=best_similarity,
                result_count=len(memories),
            )
        except Exception:
            pass  # Never fail a recall because of logging

        return sorted(memories, key=lambda x: x["similarity"], reverse=True)

    def forget(self, memory_id: str) -> None:
        """Delete a specific memory by ID."""
        self.store.delete(memory_id)

    def patterns(self) -> dict:
        """
        Surface patterns in agent memory usage.

        Returns:
            {
                "top_topics": [{"query_text": ..., "query_count": ..., "avg_similarity": ...}],
                "memory_gaps": [...],   # Frequently asked, rarely answered well
                "unused_memories": [...],  # Created 7+ days ago, never recalled
                "stale_memories": [...],   # Effective confidence below 0.3
            }
        """
        return self.store.get_patterns()

    def contradictions(self, resolved: bool = False) -> list[dict]:
        """
        Return unresolved memory conflicts.
        Set resolved=True to see already-resolved ones.
        """
        return self.store.get_contradictions(resolved=resolved)

    def resolve_contradiction(self, contradiction_id: str) -> None:
        """Mark a contradiction as resolved."""
        self.store.resolve_contradiction(contradiction_id)

    def inspect(self, limit: int = 20) -> list[dict]:
        return self.store.list_all(limit=limit)

    def clear(self) -> None:
        self.store.clear_namespace()

    def count(self) -> int:
        return self.store.count()

    def __repr__(self) -> str:
        return (
            f"Mesh(namespace='{self.namespace}', "
            f"agent_id='{self.agent_id}', "
            f"memories={self.count()})"
        )
