"""
mesh/store.py — High-performance unified persistence & hybrid search engine for Mesh.

Storage architecture:
  - SQLite (WAL mode) single-source-of-truth per namespace (`~/.mesh/mesh_<namespace>.db`)
  - Normalized float32 vector embeddings stored as compact binary BLOBs in `memories`
  - Process-local in-memory NumPy matrix cache with in-place O(1) mutations & composite
    (.db + .db-wal) file-stat synchronization across processes
  - SQLite FTS5 virtual table (`memories_fts`) for BM25 lexical & exact-keyword search
  - Automatic one-time migration from legacy ChromaDB collections if present
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

MESH_DIR = Path.home() / ".mesh"

# Columns returned by metadata queries (explicitly excludes the raw binary `embedding` BLOB
# so row dictionaries remain 100% JSON-serializable across MCP, HTTP, CLI, and Dashboard).
_MEMORY_COLUMNS = (
    "id, namespace, content, confidence, memory_type, source_agent, "
    "tags, created_at, accessed_at, access_count, ttl_days, decay_start_at, local_only"
)

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_./:-]{2,}")

# Composite file stamp: (db_mtime_ns, db_size, wal_mtime_ns, wal_size)
DbStamp = tuple[int, int, int, int]


class _ManagedConnection(sqlite3.Connection):
    """SQLite connection that commits/rolls back AND closes when exiting a `with` block."""

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            try:
                self.close()
            except Exception:
                pass


def _utc_now() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return _utc_now().isoformat()


def _parse_iso_utc(ts: Optional[str]) -> Optional[datetime]:
    """
    Parse an ISO-8601 timestamp string into a timezone-aware UTC datetime.
    Safely handles both naive UTC strings (legacy v0.1-v0.4) and offset-aware strings.
    """
    if not ts:
        return None
    try:
        cleaned = ts.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _get_db_stamp(db_path: Path) -> Optional[DbStamp]:
    """
    Return a composite `(db_mtime_ns, db_size, wal_mtime_ns, wal_size)` stamp.
    Tracking the `-wal` file alongside the main `.db` file guarantees that writes
    from other processes (CLI, Dashboard, HTTP daemon, or MCP server) are detected
    immediately in O(1) time even before SQLite checkpoints the WAL.
    """
    if not db_path.exists():
        return None
    try:
        st = db_path.stat()
        wal_path = Path(f"{db_path}-wal")
        if wal_path.exists():
            try:
                wst = wal_path.stat()
                return (st.st_mtime_ns, st.st_size, wst.st_mtime_ns, wst.st_size)
            except OSError:
                pass
        return (st.st_mtime_ns, st.st_size, 0, 0)
    except OSError:
        return None


def _normalize_vector(vec: list[float] | np.ndarray) -> np.ndarray:
    """Convert vector to contiguous 1D float32 NumPy array and L2-normalize it."""
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(arr))
    if norm > 1e-12:
        arr = arr / norm
    return np.ascontiguousarray(arr, dtype=np.float32)


def _vector_to_blob(vec: list[float] | np.ndarray) -> bytes:
    """Serialize a vector into a normalized float32 byte buffer."""
    return _normalize_vector(vec).tobytes()


def _blob_to_vector(blob: bytes) -> np.ndarray:
    """Deserialize a float32 byte buffer into a 1D NumPy array."""
    return np.frombuffer(blob, dtype=np.float32)


class _VectorCacheEntry:
    """Holds the in-memory normalized embedding matrix and ID index for a single namespace."""

    __slots__ = ("stamp", "mtime_ns", "file_size", "ids", "id_to_idx", "matrix", "dim", "all_ids")

    def __init__(
        self,
        stamp: DbStamp,
        ids: list[str],
        matrix: Optional[np.ndarray],
        all_ids: Optional[set[str]] = None,
    ):
        self.stamp: DbStamp = stamp
        self.mtime_ns: int = stamp[0]
        self.file_size: int = stamp[1]
        self.ids = ids
        self.id_to_idx = {mid: idx for idx, mid in enumerate(ids)}
        self.matrix = matrix
        self.dim = (
            int(matrix.shape[1])
            if matrix is not None and matrix.ndim == 2 and matrix.shape[0] > 0
            else 0
        )
        self.all_ids: set[str] = all_ids if all_ids is not None else set(ids)

    def update_stamp(self, stamp: DbStamp) -> None:
        self.stamp = stamp
        self.mtime_ns = stamp[0]
        self.file_size = stamp[1]

    def upsert_vector(self, memory_id: str, vec: Optional[np.ndarray]) -> bool:
        """
        Insert or update a normalized 1D float32 vector in-place.
        Returns True if updated cleanly in-place, or False if a full reload is required.
        """
        self.all_ids.add(memory_id)
        if vec is None or vec.size == 0:
            return True
        vec_1d = np.ascontiguousarray(vec.reshape(-1), dtype=np.float32)
        existing_idx = self.id_to_idx.get(memory_id)
        if existing_idx is not None:
            if self.matrix is None or vec_1d.shape[0] != self.dim:
                return False
            self.matrix[existing_idx] = vec_1d
            return True

        if self.matrix is None or len(self.ids) == 0:
            self.ids = [memory_id]
            self.id_to_idx = {memory_id: 0}
            self.matrix = np.ascontiguousarray(vec_1d.reshape(1, -1), dtype=np.float32)
            self.dim = int(vec_1d.shape[0])
            return True

        if vec_1d.shape[0] != self.dim:
            return False

        self.id_to_idx[memory_id] = len(self.ids)
        self.ids.append(memory_id)
        self.matrix = np.ascontiguousarray(
            np.vstack((self.matrix, vec_1d.reshape(1, -1))),
            dtype=np.float32,
        )
        return True

    def delete_vector(self, memory_id: str) -> None:
        """Remove a memory's vector from the in-memory matrix in-place."""
        self.all_ids.discard(memory_id)
        idx = self.id_to_idx.pop(memory_id, None)
        if idx is None:
            return
        self.ids.pop(idx)
        for i in range(idx, len(self.ids)):
            self.id_to_idx[self.ids[i]] = i
        if not self.ids or self.matrix is None or self.matrix.shape[0] <= 1:
            self.matrix = None
            self.dim = 0
        else:
            self.matrix = np.ascontiguousarray(
                np.delete(self.matrix, idx, axis=0),
                dtype=np.float32,
            )


# Process-wide caches protected by a re-entrant lock
_CACHE_LOCK = threading.RLock()
_VECTOR_CACHE: dict[Path, _VectorCacheEntry] = {}
_INITIALIZED_DBS: dict[Path, DbStamp] = {}
_TRANSIENT_EMBEDDINGS: dict[tuple[str, str], np.ndarray] = {}


class _CollectionCompatShim:
    """
    Backward-compatibility shim emulating ChromaDB's `store.collection` API.
    Ensures legacy callers (e.g. `io.py` or external scripts) work seamlessly
    against the unified SQLite engine.
    """

    def __init__(self, store: "MeshStore"):
        self._store = store

    def count(self) -> int:
        return self._store.count()

    def get(
        self,
        ids: Optional[list[str]] = None,
        include: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        include = include or ["documents", "metadatas"]
        want_embeddings = "embeddings" in include
        want_documents = "documents" in include
        want_metadatas = "metadatas" in include

        with self._store._connect() as conn:
            conn.row_factory = sqlite3.Row
            if ids:
                placeholders = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"SELECT {_MEMORY_COLUMNS}, embedding FROM memories "
                    f"WHERE namespace = ? AND id IN ({placeholders})",
                    [self._store.namespace, *ids],
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_MEMORY_COLUMNS}, embedding FROM memories WHERE namespace = ?",
                    (self._store.namespace,),
                ).fetchall()

        out_ids: list[str] = []
        out_docs: list[str] = []
        out_metas: list[dict[str, Any]] = []
        out_embs: list[Optional[list[float]]] = []

        for r in rows:
            mid = r["id"]
            out_ids.append(mid)
            if want_documents:
                out_docs.append(r["content"])
            if want_metadatas:
                out_metas.append(
                    {
                        "memory_type": r["memory_type"],
                        "source_agent": r["source_agent"],
                        "confidence": float(r["confidence"]),
                        "tags": r["tags"],
                        "created_at": r["created_at"],
                        "ttl_days": r["ttl_days"],
                        "decay_start_at": r["decay_start_at"],
                        "local_only": int(r["local_only"] or 0),
                    }
                )
            if want_embeddings:
                blob = r["embedding"]
                if blob:
                    out_embs.append(_blob_to_vector(blob).tolist())
                else:
                    transient = _TRANSIENT_EMBEDDINGS.get((self._store.namespace, mid))
                    out_embs.append(transient.tolist() if transient is not None else None)

        res: dict[str, Any] = {"ids": out_ids}
        if want_documents:
            res["documents"] = out_docs
        if want_metadatas:
            res["metadatas"] = out_metas
        if want_embeddings:
            res["embeddings"] = out_embs
        return res

    def upsert(
        self,
        ids: list[str],
        embeddings: Optional[list[list[float]]] = None,
        documents: Optional[list[str]] = None,
        metadatas: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        now = _utc_now_iso()
        with _CACHE_LOCK:
            synced_cache = self._store._is_cache_synced_locked()
            in_place_ok = synced_cache is not None
            with self._store._connect() as conn:
                for i, mid in enumerate(ids):
                    doc = documents[i] if documents and i < len(documents) else ""
                    meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                    emb = embeddings[i] if embeddings and i < len(embeddings) else None
                    norm_vec = _normalize_vector(emb) if emb is not None else None
                    blob = norm_vec.tobytes() if norm_vec is not None else None
                    if norm_vec is not None:
                        _TRANSIENT_EMBEDDINGS[(self._store.namespace, mid)] = norm_vec

                    tags_raw = meta.get("tags", "[]")
                    tags_str = tags_raw if isinstance(tags_raw, str) else json.dumps(tags_raw)

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO memories
                        (id, namespace, content, confidence, memory_type,
                         source_agent, tags, created_at, accessed_at, access_count,
                         ttl_days, decay_start_at, local_only, embedding)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            mid,
                            self._store.namespace,
                            doc,
                            float(meta.get("confidence", 1.0)),
                            meta.get("memory_type", "fact"),
                            meta.get("source_agent", "unknown"),
                            tags_str,
                            meta.get("created_at", now),
                            meta.get("accessed_at", now),
                            int(meta.get("access_count", 0)),
                            meta.get("ttl_days"),
                            meta.get("decay_start_at"),
                            int(meta.get("local_only", 0)),
                            blob,
                        ),
                    )
                    self._store._sync_fts_row(
                        conn,
                        memory_id=mid,
                        content=doc,
                        tags=tags_str,
                        memory_type=meta.get("memory_type", "fact"),
                        source_agent=meta.get("source_agent", "unknown"),
                    )
                    if in_place_ok and synced_cache is not None:
                        if not synced_cache.upsert_vector(mid, norm_vec):
                            in_place_ok = False
                conn.commit()

            if in_place_ok and synced_cache is not None:
                self._store._refresh_stamps_after_write_locked(synced_cache)
            else:
                self._store._invalidate_cache()

    def query(
        self,
        query_embeddings: list[list[float]],
        n_results: int = 5,
    ) -> dict[str, Any]:
        if not query_embeddings:
            return {"documents": [[]], "distances": [[]], "metadatas": [[]], "ids": [[]]}
        return self._store.query(query_embeddings[0], n=n_results)

    def delete(self, ids: list[str]) -> None:
        for mid in ids:
            self._store.delete(mid)


class MeshStore:
    """
    Unified Persistence & Hybrid Search Layer for Mesh.
    Combines SQLite WAL metadata storage, in-memory SIMD vector search,
    and SQLite FTS5 BM25 full-text search in a single atomic database file.
    """

    def __init__(self, namespace: str = "default"):
        self.namespace = namespace
        MESH_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = MESH_DIR / f"mesh_{namespace}.db"
        self._fts_enabled = True
        self._ensure_initialized()
        self.collection = _CollectionCompatShim(self)

    # --- Connection & Schema Management ---

    def _connect(self) -> sqlite3.Connection:
        """Open a fast, concurrency-safe SQLite connection configured for WAL."""
        conn = sqlite3.connect(self.db_path, timeout=10.0, factory=_ManagedConnection)
        if self.db_path not in _INITIALIZED_DBS:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            if os.name != "nt":
                conn.execute("PRAGMA mmap_size=268435456")
            conn.execute("PRAGMA cache_size=-32000")
        except sqlite3.OperationalError:
            pass
        return conn

    def _is_cache_synced_locked(self) -> Optional[_VectorCacheEntry]:
        """Return the active `_VectorCacheEntry` if its composite DB+WAL stamp matches disk."""
        cached = _VECTOR_CACHE.get(self.db_path)
        if cached is None:
            return None
        stamp = _get_db_stamp(self.db_path)
        if stamp is not None and cached.stamp == stamp:
            return cached
        return None

    def _refresh_stamps_after_write_locked(
        self,
        synced_cache: Optional[_VectorCacheEntry],
    ) -> None:
        """
        Update `_INITIALIZED_DBS` and `synced_cache` composite file stamps after an in-process write
        so subsequent calls never mistake our own writes for external file modifications.
        """
        stamp = _get_db_stamp(self.db_path)
        if stamp is None:
            _INITIALIZED_DBS.pop(self.db_path, None)
            _VECTOR_CACHE.pop(self.db_path, None)
            return
        _INITIALIZED_DBS[self.db_path] = stamp
        if synced_cache is not None:
            synced_cache.update_stamp(stamp)

    def _ensure_initialized(self) -> None:
        """Initialize schema once per DB file lifecycle."""
        with _CACHE_LOCK:
            stamp = _get_db_stamp(self.db_path)
            if stamp is not None and stamp[1] > 0 and _INITIALIZED_DBS.get(self.db_path) == stamp:
                return
            self._init_db()
            new_stamp = _get_db_stamp(self.db_path)
            if new_stamp is not None:
                _INITIALIZED_DBS[self.db_path] = new_stamp

    def _init_db(self) -> None:
        mem_count = 0
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id              TEXT PRIMARY KEY,
                    namespace       TEXT NOT NULL,
                    content         TEXT NOT NULL,
                    confidence      REAL NOT NULL DEFAULT 1.0,
                    memory_type     TEXT NOT NULL DEFAULT 'fact',
                    source_agent    TEXT NOT NULL DEFAULT 'unknown',
                    tags            TEXT NOT NULL DEFAULT '[]',
                    created_at      TEXT NOT NULL,
                    accessed_at     TEXT NOT NULL,
                    access_count    INTEGER NOT NULL DEFAULT 0,
                    ttl_days        REAL,
                    decay_start_at  TEXT,
                    local_only      INTEGER NOT NULL DEFAULT 0,
                    embedding       BLOB
                )
                """
            )
            # Schema migrations for databases created by earlier Mesh versions
            for col_sql in (
                "ALTER TABLE memories ADD COLUMN ttl_days REAL",
                "ALTER TABLE memories ADD COLUMN decay_start_at TEXT",
                "ALTER TABLE memories ADD COLUMN local_only INTEGER DEFAULT 0",
                "ALTER TABLE memories ADD COLUMN embedding BLOB",
            ):
                try:
                    conn.execute(col_sql)
                except sqlite3.OperationalError:
                    pass

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS queries (
                    id              TEXT PRIMARY KEY,
                    namespace       TEXT NOT NULL,
                    query_text      TEXT NOT NULL,
                    agent_id        TEXT NOT NULL DEFAULT 'unknown',
                    best_similarity REAL,
                    result_count    INTEGER NOT NULL DEFAULT 0,
                    timestamp       TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contradictions (
                    id                  TEXT PRIMARY KEY,
                    namespace           TEXT NOT NULL,
                    new_memory_id       TEXT NOT NULL,
                    new_content         TEXT NOT NULL,
                    existing_memory_id  TEXT NOT NULL,
                    existing_content    TEXT NOT NULL,
                    similarity          REAL NOT NULL,
                    detected_at         TEXT NOT NULL,
                    resolved            INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Indexes for fast filtering and sorting
            conn.execute("CREATE INDEX IF NOT EXISTS idx_namespace ON memories (namespace)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memories (namespace, memory_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accessed_at ON memories (namespace, accessed_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON memories (namespace, created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_queries_namespace ON queries (namespace)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_queries_timestamp ON queries (timestamp)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_contradictions_ns_res ON contradictions (namespace, resolved)"
            )

            mem_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE namespace = ?", (self.namespace,)
                ).fetchone()[0]
            )

            # Full-Text Search (FTS5) table for BM25 keyword retrieval
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                        id UNINDEXED,
                        content,
                        tags,
                        memory_type,
                        source_agent,
                        tokenize='porter unicode61'
                    )
                    """
                )
                # Backfill FTS5 if memories exist but FTS is empty
                if mem_count > 0:
                    fts_count = int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0])
                    if fts_count < mem_count:
                        conn.execute("DELETE FROM memories_fts")
                        conn.execute(
                            """
                            INSERT INTO memories_fts (id, content, tags, memory_type, source_agent)
                            SELECT id, content, tags, memory_type, source_agent FROM memories
                            """
                        )
            except sqlite3.OperationalError:
                self._fts_enabled = False

            conn.commit()
        finally:
            conn.close()

        # Seed an empty vector cache immediately for brand-new empty namespaces
        if mem_count == 0:
            stamp = _get_db_stamp(self.db_path)
            if stamp is not None:
                _VECTOR_CACHE[self.db_path] = _VectorCacheEntry(
                    stamp=stamp,
                    ids=[],
                    matrix=None,
                    all_ids=set(),
                )

        # One-time silent migration from legacy ChromaDB storage if needed
        if mem_count > 0:
            self._migrate_from_chroma_if_needed()

    def _migrate_from_chroma_if_needed(self) -> None:
        """
        If rows exist without an SQLite embedding BLOB and a legacy `~/.mesh/chroma`
        directory exists, migrate the vectors into SQLite automatically.
        """
        chroma_dir = MESH_DIR / "chroma"
        if not chroma_dir.exists():
            return

        with self._connect() as conn:
            missing_rows = conn.execute(
                "SELECT id FROM memories WHERE namespace = ? AND embedding IS NULL",
                (self.namespace,),
            ).fetchall()
        if not missing_rows:
            return

        missing_ids = [r[0] for r in missing_rows]
        try:
            import chromadb

            client = chromadb.PersistentClient(path=str(chroma_dir))
            col = client.get_collection(name=f"mesh_{self.namespace}")
            data = col.get(ids=missing_ids, include=["embeddings"])
            got_ids = data.get("ids") or []
            got_embs = data.get("embeddings") or []
            if got_ids and got_embs:
                with self._connect() as conn:
                    for mid, emb in zip(got_ids, got_embs):
                        if emb is not None:
                            conn.execute(
                                "UPDATE memories SET embedding = ? WHERE id = ?",
                                (_vector_to_blob(emb), mid),
                            )
                    conn.commit()
                self._invalidate_cache()
        except Exception:
            pass

    def _sync_fts_row(
        self,
        conn: sqlite3.Connection,
        memory_id: str,
        content: str,
        tags: str,
        memory_type: str,
        source_agent: str,
    ) -> None:
        if not self._fts_enabled:
            return
        try:
            conn.execute("DELETE FROM memories_fts WHERE id = ?", (memory_id,))
            conn.execute(
                "INSERT INTO memories_fts (id, content, tags, memory_type, source_agent) VALUES (?, ?, ?, ?, ?)",
                (memory_id, content, tags, memory_type, source_agent),
            )
        except sqlite3.OperationalError:
            pass

    def _delete_fts_row(self, conn: sqlite3.Connection, memory_id: str) -> None:
        if not self._fts_enabled:
            return
        try:
            conn.execute("DELETE FROM memories_fts WHERE id = ?", (memory_id,))
        except sqlite3.OperationalError:
            pass

    # --- In-Memory Vector Matrix Cache ---

    def _invalidate_cache(self) -> None:
        with _CACHE_LOCK:
            _VECTOR_CACHE.pop(self.db_path, None)
            stamp = _get_db_stamp(self.db_path)
            if stamp is not None:
                _INITIALIZED_DBS[self.db_path] = stamp
            else:
                _INITIALIZED_DBS.pop(self.db_path, None)

    def _get_vector_cache(self) -> _VectorCacheEntry:
        """
        Return the normalized float32 embedding matrix for this namespace.
        Uses in-memory cache when in sync; automatically reloads from SQLite if an external
        process modified either the `.db` or `.db-wal` file.
        """
        with _CACHE_LOCK:
            if not self.db_path.exists():
                self._ensure_initialized()

            stamp = _get_db_stamp(self.db_path) or (0, 0, 0, 0)
            cached = _VECTOR_CACHE.get(self.db_path)
            if cached is not None and cached.stamp == stamp:
                _INITIALIZED_DBS[self.db_path] = stamp
                return cached

            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, embedding FROM memories WHERE namespace = ?",
                    (self.namespace,),
                ).fetchall()

            ids: list[str] = []
            all_ids: set[str] = set()
            vecs: list[np.ndarray] = []
            expected_dim: Optional[int] = None
            repaired_blobs: list[tuple[bytes, str]] = []

            for mid, blob in rows:
                all_ids.add(mid)
                vec: Optional[np.ndarray] = None
                if blob:
                    arr = _blob_to_vector(blob)
                    if arr.size > 0:
                        vec = arr
                else:
                    transient = _TRANSIENT_EMBEDDINGS.get((self.namespace, mid))
                    if transient is not None:
                        vec = transient
                        repaired_blobs.append((transient.tobytes(), mid))

                if vec is not None:
                    if expected_dim is None:
                        expected_dim = int(vec.shape[0])
                    if vec.shape[0] == expected_dim:
                        ids.append(mid)
                        vecs.append(vec)

            if repaired_blobs:
                try:
                    with self._connect() as conn:
                        conn.executemany(
                            "UPDATE memories SET embedding = ? WHERE id = ?",
                            repaired_blobs,
                        )
                        conn.commit()
                    stamp = _get_db_stamp(self.db_path) or stamp
                except Exception:
                    pass

            matrix = np.ascontiguousarray(np.vstack(vecs), dtype=np.float32) if vecs else None
            entry = _VectorCacheEntry(
                stamp=stamp,
                ids=ids,
                matrix=matrix,
                all_ids=all_ids,
            )
            _VECTOR_CACHE[self.db_path] = entry
            _INITIALIZED_DBS[self.db_path] = stamp
            return entry

    # --- Core Memory CRUD Operations ---

    def upsert(
        self,
        memory_id: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any],
        local_only: bool = False,
    ) -> None:
        """
        Store or overwrite a memory and its normalized vector in a single atomic transaction,
        updating the in-memory vector matrix in-place in O(1) time.
        """
        self._ensure_initialized()
        now = _utc_now_iso()
        norm_vec = _normalize_vector(embedding)
        blob = norm_vec.tobytes()
        _TRANSIENT_EMBEDDINGS[(self.namespace, memory_id)] = norm_vec

        tags_raw = metadata.get("tags", [])
        tags_str = tags_raw if isinstance(tags_raw, str) else json.dumps(tags_raw)

        mem_type = str(metadata.get("memory_type", "fact"))
        source_agent = str(metadata.get("source_agent", "unknown"))
        confidence = float(metadata.get("confidence", 1.0))
        created_at = metadata.get("created_at") or now
        accessed_at = metadata.get("accessed_at") or now
        access_count = int(metadata.get("access_count", 0))
        ttl_days = metadata.get("ttl_days")
        decay_start_at = metadata.get("decay_start_at")
        local_flag = 1 if (local_only or metadata.get("local_only")) else 0

        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO memories
                    (id, namespace, content, confidence, memory_type,
                     source_agent, tags, created_at, accessed_at, access_count,
                     ttl_days, decay_start_at, local_only, embedding)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        self.namespace,
                        content,
                        confidence,
                        mem_type,
                        source_agent,
                        tags_str,
                        created_at,
                        accessed_at,
                        access_count,
                        ttl_days,
                        decay_start_at,
                        local_flag,
                        blob,
                    ),
                )
                self._sync_fts_row(
                    conn,
                    memory_id=memory_id,
                    content=content,
                    tags=tags_str,
                    memory_type=mem_type,
                    source_agent=source_agent,
                )
                conn.commit()

            if synced_cache is not None and synced_cache.upsert_vector(memory_id, norm_vec):
                self._refresh_stamps_after_write_locked(synced_cache)
            else:
                self._invalidate_cache()

    def upsert_batch(
        self,
        items: list[tuple[str, str, list[float], dict[str, Any], bool]],
    ) -> None:
        """
        Batch-insert or overwrite multiple memories in a single SQLite WAL transaction
        with in-place vector cache updates.
        Each item in `items` is: (memory_id, content, embedding, metadata, local_only).
        """
        if not items:
            return
        self._ensure_initialized()
        now = _utc_now_iso()

        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            in_place_ok = synced_cache is not None

            with self._connect() as conn:
                for memory_id, content, embedding, metadata, local_only in items:
                    norm_vec = _normalize_vector(embedding)
                    blob = norm_vec.tobytes()
                    _TRANSIENT_EMBEDDINGS[(self.namespace, memory_id)] = norm_vec

                    tags_raw = metadata.get("tags", [])
                    tags_str = tags_raw if isinstance(tags_raw, str) else json.dumps(tags_raw)
                    mem_type = str(metadata.get("memory_type", "fact"))
                    source_agent = str(metadata.get("source_agent", "unknown"))
                    confidence = float(metadata.get("confidence", 1.0))
                    created_at = metadata.get("created_at") or now
                    accessed_at = metadata.get("accessed_at") or now
                    access_count = int(metadata.get("access_count", 0))
                    ttl_days = metadata.get("ttl_days")
                    decay_start_at = metadata.get("decay_start_at")
                    local_flag = 1 if (local_only or metadata.get("local_only")) else 0

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO memories
                        (id, namespace, content, confidence, memory_type,
                         source_agent, tags, created_at, accessed_at, access_count,
                         ttl_days, decay_start_at, local_only, embedding)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            memory_id,
                            self.namespace,
                            content,
                            confidence,
                            mem_type,
                            source_agent,
                            tags_str,
                            created_at,
                            accessed_at,
                            access_count,
                            ttl_days,
                            decay_start_at,
                            local_flag,
                            blob,
                        ),
                    )
                    self._sync_fts_row(
                        conn,
                        memory_id=memory_id,
                        content=content,
                        tags=tags_str,
                        memory_type=mem_type,
                        source_agent=source_agent,
                    )
                    if in_place_ok and synced_cache is not None:
                        if not synced_cache.upsert_vector(memory_id, norm_vec):
                            in_place_ok = False
                conn.commit()

            if in_place_ok and synced_cache is not None:
                self._refresh_stamps_after_write_locked(synced_cache)
            else:
                self._invalidate_cache()

    def get(self, memory_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single memory by ID, or None if not found."""
        self._ensure_initialized()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE id = ? AND namespace = ?",
                (memory_id, self.namespace),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["local_only"] = bool(d.get("local_only", 0))
        return d

    def update(
        self,
        memory_id: str,
        content: Optional[str] = None,
        embedding: Optional[list[float]] = None,
        memory_type: Optional[str] = None,
        confidence: Optional[float] = None,
        tags: Optional[list[str]] = None,
        ttl_days: Optional[float] = None,
        reset_ttl: bool = False,
        local_only: Optional[bool] = None,
        source_agent: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Update an existing memory in-place while preserving its `id`, `created_at`,
        and `access_count`. Returns the updated memory dict, or None if `memory_id` does not exist.
        """
        self._ensure_initialized()
        now = _utc_now_iso()
        norm_vec: Optional[np.ndarray] = _normalize_vector(embedding) if embedding is not None else None

        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                existing = conn.execute(
                    f"SELECT {_MEMORY_COLUMNS}, embedding FROM memories WHERE id = ? AND namespace = ?",
                    (memory_id, self.namespace),
                ).fetchone()
                if not existing:
                    return None

                new_content = content if content is not None else existing["content"]
                new_type = memory_type if memory_type is not None else existing["memory_type"]
                new_conf = float(confidence) if confidence is not None else float(existing["confidence"])
                new_agent = source_agent if source_agent is not None else existing["source_agent"]
                new_tags_str = json.dumps(tags) if tags is not None else existing["tags"]
                new_local = (
                    (1 if local_only else 0)
                    if local_only is not None
                    else int(existing["local_only"] or 0)
                )

                if reset_ttl or ttl_days is not None:
                    new_ttl = ttl_days
                    new_decay_start = now if ttl_days is not None else None
                else:
                    new_ttl = existing["ttl_days"]
                    new_decay_start = existing["decay_start_at"]

                if norm_vec is not None:
                    new_blob = norm_vec.tobytes()
                    _TRANSIENT_EMBEDDINGS[(self.namespace, memory_id)] = norm_vec
                else:
                    new_blob = existing["embedding"]

                conn.execute(
                    """
                    UPDATE memories
                    SET content = ?,
                        confidence = ?,
                        memory_type = ?,
                        source_agent = ?,
                        tags = ?,
                        accessed_at = ?,
                        ttl_days = ?,
                        decay_start_at = ?,
                        local_only = ?,
                        embedding = ?
                    WHERE id = ? AND namespace = ?
                    """,
                    (
                        new_content,
                        new_conf,
                        new_type,
                        new_agent,
                        new_tags_str,
                        now,
                        new_ttl,
                        new_decay_start,
                        new_local,
                        new_blob,
                        memory_id,
                        self.namespace,
                    ),
                )
                self._sync_fts_row(
                    conn,
                    memory_id=memory_id,
                    content=new_content,
                    tags=new_tags_str,
                    memory_type=new_type,
                    source_agent=new_agent,
                )
                conn.commit()

            if norm_vec is not None:
                if synced_cache is not None and synced_cache.upsert_vector(memory_id, norm_vec):
                    self._refresh_stamps_after_write_locked(synced_cache)
                else:
                    self._invalidate_cache()
            else:
                self._refresh_stamps_after_write_locked(synced_cache)

            return {
                "id": memory_id,
                "namespace": self.namespace,
                "content": new_content,
                "confidence": new_conf,
                "memory_type": new_type,
                "source_agent": new_agent,
                "tags": new_tags_str,
                "created_at": existing["created_at"],
                "accessed_at": now,
                "access_count": int(existing["access_count"] or 0),
                "ttl_days": new_ttl,
                "decay_start_at": new_decay_start,
                "local_only": bool(new_local),
            }

    # --- Vector & Hybrid Search Operations ---

    def query(self, embedding: list[float], n: int = 5) -> dict[str, list[list[Any]]]:
        """
        Dense cosine similarity search returning a ChromaDB-compatible dict:
        {"ids": [[...]], "documents": [[...]], "distances": [[...]], "metadatas": [[...]]}
        """
        cache = self._get_vector_cache()
        if cache.matrix is None or len(cache.ids) == 0 or n <= 0:
            return {"documents": [[]], "distances": [[]], "metadatas": [[]], "ids": [[]]}

        q_vec = _normalize_vector(embedding)
        if q_vec.shape[0] != cache.dim:
            return {"documents": [[]], "distances": [[]], "metadatas": [[]], "ids": [[]]}

        # Fast SIMD matrix-vector multiplication (dot product of L2-normalized vectors = cosine similarity)
        sims = cache.matrix @ q_vec
        k = min(n, len(cache.ids))
        if k < len(cache.ids):
            top_indices = np.argpartition(-sims, k - 1)[:k]
            top_indices = top_indices[np.argsort(-sims[top_indices])]
        else:
            top_indices = np.argsort(-sims)

        top_ids = [cache.ids[int(idx)] for idx in top_indices]
        top_sims = {cache.ids[int(idx)]: float(sims[int(idx)]) for idx in top_indices}

        placeholders = ",".join("?" for _ in top_ids)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE namespace = ? AND id IN ({placeholders})",
                [self.namespace, *top_ids],
            ).fetchall()

        row_map = {r["id"]: dict(r) for r in rows}

        out_ids: list[str] = []
        out_docs: list[str] = []
        out_dists: list[float] = []
        out_metas: list[dict[str, Any]] = []

        for mid in top_ids:
            row = row_map.get(mid)
            if not row:
                continue
            sim = max(-1.0, min(1.0, top_sims[mid]))
            dist = max(0.0, 1.0 - sim)
            out_ids.append(mid)
            out_docs.append(row["content"])
            out_dists.append(dist)
            out_metas.append(
                {
                    "memory_type": row["memory_type"],
                    "confidence": float(row["confidence"]),
                    "source_agent": row["source_agent"],
                    "tags": row["tags"],
                    "created_at": row["created_at"],
                    "accessed_at": row["accessed_at"],
                    "access_count": int(row["access_count"] or 0),
                    "ttl_days": row["ttl_days"],
                    "decay_start_at": row["decay_start_at"],
                    "local_only": int(row["local_only"] or 0),
                }
            )

        return {
            "ids": [out_ids],
            "documents": [out_docs],
            "distances": [out_dists],
            "metadatas": [out_metas],
        }

    def _fts_lexical_scores(
        self,
        query_text: str,
        limit: int = 50,
        conn: Optional[sqlite3.Connection] = None,
    ) -> dict[str, float]:
        """
        Run a BM25 lexical search over `memories_fts` and return normalized scores in [0.0, 1.0].
        Reuses `conn` if provided to avoid opening a second connection during `hybrid_query`.
        """
        if not self._fts_enabled or not query_text:
            return {}

        tokens = [t.lower() for t in _TOKEN_RE.findall(query_text)]
        if not tokens:
            return {}

        # Sanitize tokens for FTS5 query syntax
        safe_tokens: list[str] = []
        for tok in tokens[:16]:
            clean = re.sub(r"[^a-zA-Z0-9_]", "", tok)
            if len(clean) >= 2:
                safe_tokens.append(f'"{clean}"*')
        if not safe_tokens:
            return {}

        fts_query = " OR ".join(safe_tokens)
        sql = """
            SELECT id, bm25(memories_fts, 0.0, 1.0, 0.8, 0.3, 0.2) AS rank_score
            FROM memories_fts
            WHERE memories_fts MATCH ?
            ORDER BY rank_score ASC
            LIMIT ?
        """
        scores: dict[str, float] = {}
        try:
            if conn is not None:
                rows = conn.execute(sql, (fts_query, limit)).fetchall()
            else:
                with self._connect() as own_conn:
                    rows = own_conn.execute(sql, (fts_query, limit)).fetchall()
            if not rows:
                return {}

            # SQLite FTS5 bm25() returns negative numbers where more negative = better match
            raw_positives = [max(0.0, -float(r[1])) for r in rows]
            max_bm25 = max(raw_positives) if raw_positives else 0.0
            if max_bm25 <= 1e-9:
                return {r[0]: 0.5 for r in rows}

            for (mid, _), pos in zip(rows, raw_positives):
                # Calibrate BM25 relative to top hit and absolute saturation
                rel_score = pos / max_bm25
                abs_factor = min(1.0, pos / (pos + 2.5))
                scores[mid] = round(0.6 * rel_score + 0.4 * abs_factor, 4)
        except Exception:
            return {}
        return scores

    def hybrid_query(
        self,
        query_text: str,
        embedding: list[float],
        n: int = 5,
        memory_type: Optional[str] = None,
        tags: Optional[list[str]] = None,
        source_agent: Optional[str] = None,
        include_private: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Execute a 3-stage hybrid retrieval (Dense SIMD Cosine + FTS5 BM25 + Exact Token Boost)
        with optional metadata pre-filtering in a single SQLite connection pass.
        """
        cache = self._get_vector_cache()
        if cache.matrix is None or len(cache.ids) == 0 or n <= 0:
            return []

        q_vec = _normalize_vector(embedding)
        if q_vec.shape[0] != cache.dim:
            return []

        sims = cache.matrix @ q_vec

        # Select top candidate pool from dense hits
        pool_size = min(len(cache.ids), max(n * 8, 50))
        if pool_size < len(cache.ids):
            top_dense_idx = np.argpartition(-sims, pool_size - 1)[:pool_size]
        else:
            top_dense_idx = np.arange(len(cache.ids))

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            lexical_map = self._fts_lexical_scores(query_text, limit=max(n * 6, 40), conn=conn)

            candidate_ids_set = {cache.ids[int(i)] for i in top_dense_idx} | set(lexical_map.keys())
            candidate_ids = [mid for mid in candidate_ids_set if mid in cache.id_to_idx]
            if not candidate_ids:
                return []

            placeholders = ",".join("?" for _ in candidate_ids)
            sql = f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE namespace = ? AND id IN ({placeholders})"
            params: list[Any] = [self.namespace, *candidate_ids]

            if memory_type:
                sql += " AND memory_type = ?"
                params.append(memory_type.lower())
            if source_agent:
                sql += " AND source_agent = ?"
                params.append(source_agent)
            if not include_private:
                sql += " AND local_only = 0"

            rows = conn.execute(sql, params).fetchall()

        query_lower = query_text.strip().lower()
        query_tokens = set(_TOKEN_RE.findall(query_lower))
        filter_tags = {t.strip().lower() for t in (tags or []) if t.strip()}

        results: list[dict[str, Any]] = []
        for r in rows:
            mid = r["id"]
            row_dict = dict(r)

            try:
                parsed_tags = json.loads(row_dict.get("tags") or "[]")
                if not isinstance(parsed_tags, list):
                    parsed_tags = []
            except Exception:
                parsed_tags = []

            if filter_tags:
                mem_tags_lower = {str(t).lower() for t in parsed_tags}
                if not filter_tags.issubset(mem_tags_lower):
                    continue

            idx = cache.id_to_idx[mid]
            sem_sim = max(0.0, min(1.0, float(sims[idx])))
            lex_score = lexical_map.get(mid, 0.0)

            # Exact technical identifier / token overlap bonus
            content_lower = row_dict["content"].lower()
            exact_bonus = 0.0
            if query_tokens:
                doc_tokens = set(_TOKEN_RE.findall(content_lower))
                doc_tokens.update(str(t).lower() for t in parsed_tags)
                overlap = len(query_tokens & doc_tokens) / len(query_tokens)
                if len(query_lower) >= 4 and query_lower in content_lower:
                    overlap = min(1.0, overlap + 0.35)
                exact_bonus = overlap

            # Fuse semantic similarity with lexical/keyword evidence:
            # Preserves semantic similarity as the base while boosting exact/BM25 matches.
            keyword_signal = max(lex_score, exact_bonus)
            if keyword_signal > 0.0:
                hybrid_sim = min(1.0, sem_sim + (1.0 - sem_sim) * 0.28 * keyword_signal)
            else:
                hybrid_sim = sem_sim

            row_dict["tags_list"] = parsed_tags
            row_dict["local_only"] = bool(row_dict.get("local_only", 0))
            row_dict["semantic_similarity"] = round(sem_sim, 4)
            row_dict["lexical_score"] = round(keyword_signal, 4)
            row_dict["similarity"] = round(hybrid_sim, 4)
            results.append(row_dict)

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:n]

    # --- Deletion & Access Tracking ---

    def delete(self, memory_id: str) -> bool:
        """Remove a memory from SQLite, FTS5, and the in-memory vector cache in-place."""
        self._ensure_initialized()
        _TRANSIENT_EMBEDDINGS.pop((self.namespace, memory_id), None)
        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM memories WHERE id = ? AND namespace = ?",
                    (memory_id, self.namespace),
                )
                deleted = cursor.rowcount > 0
                self._delete_fts_row(conn, memory_id)
                conn.commit()

            if deleted:
                if synced_cache is not None:
                    synced_cache.delete_vector(memory_id)
                    self._refresh_stamps_after_write_locked(synced_cache)
                else:
                    self._invalidate_cache()
            else:
                self._refresh_stamps_after_write_locked(synced_cache)
        return deleted

    def increment_access(self, memory_id: str) -> None:
        """Increment access count for a single memory."""
        self.increment_access_batch([memory_id])

    def increment_access_batch(self, memory_ids: list[str]) -> None:
        """
        Increment access counts for multiple memories in a single WAL transaction
        without invalidating the in-memory vector cache.
        """
        if not memory_ids:
            return
        self._ensure_initialized()
        now = _utc_now_iso()
        unique_ids = list(dict.fromkeys(mid for mid in memory_ids if mid))
        if not unique_ids:
            return
        placeholders = ",".join("?" for _ in unique_ids)

        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE memories
                    SET access_count = access_count + 1, accessed_at = ?
                    WHERE namespace = ? AND id IN ({placeholders})
                    """,
                    [now, self.namespace, *unique_ids],
                )
                conn.commit()
            self._refresh_stamps_after_write_locked(synced_cache)

    def record_recall_telemetry(
        self,
        memory_ids: list[str],
        query_text: str,
        agent_id: str,
        best_similarity: Optional[float],
        result_count: int,
    ) -> None:
        """
        Atomically update access_count for recalled memories and log the query in a
        single SQLite WAL transaction while keeping the in-memory vector cache hot.
        """
        self._ensure_initialized()
        now = _utc_now_iso()
        unique_ids = list(dict.fromkeys(mid for mid in memory_ids if mid))

        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            with self._connect() as conn:
                if unique_ids:
                    placeholders = ",".join("?" for _ in unique_ids)
                    conn.execute(
                        f"""
                        UPDATE memories
                        SET access_count = access_count + 1, accessed_at = ?
                        WHERE namespace = ? AND id IN ({placeholders})
                        """,
                        [now, self.namespace, *unique_ids],
                    )
                conn.execute(
                    """
                    INSERT INTO queries
                    (id, namespace, query_text, agent_id, best_similarity,
                     result_count, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        self.namespace,
                        query_text,
                        agent_id,
                        best_similarity,
                        result_count,
                        now,
                    ),
                )
                conn.commit()
            self._refresh_stamps_after_write_locked(synced_cache)

    def count(self) -> int:
        """Return total number of memories in this namespace (O(1) when vector cache is hot)."""
        self._ensure_initialized()
        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            if synced_cache is not None:
                return len(synced_cache.all_ids)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE namespace = ?",
                (self.namespace,),
            ).fetchone()
            return int(row[0]) if row else 0

    def list_all(
        self,
        limit: int = 100,
        memory_type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self._ensure_initialized()
        sql = f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE namespace = ?"
        params: list[Any] = [self.namespace]
        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type.lower())
        sql += " ORDER BY accessed_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()

        res: list[dict[str, Any]] = []
        tag_lower = tag.strip().lower() if tag else None
        for r in rows:
            d = dict(r)
            d["local_only"] = bool(d.get("local_only", 0))
            if tag_lower:
                try:
                    t_list = [str(x).lower() for x in json.loads(d.get("tags") or "[]")]
                except Exception:
                    t_list = []
                if tag_lower not in t_list:
                    continue
            res.append(d)
        return res

    def get_exportable_memories(self, include_embeddings: bool = False) -> list[dict[str, Any]]:
        """Return all memories where local_only is False."""
        self._ensure_initialized()
        cols = f"{_MEMORY_COLUMNS}, embedding" if include_embeddings else _MEMORY_COLUMNS
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT {cols} FROM memories
                WHERE namespace = ? AND local_only = 0
                ORDER BY created_at DESC
                """,
                (self.namespace,),
            ).fetchall()

        res: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["local_only"] = False
            if include_embeddings:
                blob = d.pop("embedding", None)
                d["embedding"] = _blob_to_vector(blob).tolist() if blob else None
            res.append(d)
        return res

    def clear_namespace(self) -> None:
        self._ensure_initialized()
        with _CACHE_LOCK:
            with self._connect() as conn:
                conn.execute("DELETE FROM memories WHERE namespace = ?", (self.namespace,))
                conn.execute("DELETE FROM queries WHERE namespace = ?", (self.namespace,))
                conn.execute("DELETE FROM contradictions WHERE namespace = ?", (self.namespace,))
                if self._fts_enabled:
                    try:
                        conn.execute("DELETE FROM memories_fts")
                    except sqlite3.OperationalError:
                        pass
                conn.commit()

            stale_keys = [k for k in _TRANSIENT_EMBEDDINGS if k[0] == self.namespace]
            for k in stale_keys:
                _TRANSIENT_EMBEDDINGS.pop(k, None)

            stamp = _get_db_stamp(self.db_path)
            if stamp is not None:
                _INITIALIZED_DBS[self.db_path] = stamp
                _VECTOR_CACHE[self.db_path] = _VectorCacheEntry(
                    stamp=stamp,
                    ids=[],
                    matrix=None,
                    all_ids=set(),
                )
                return
            self._invalidate_cache()

    # --- Query Logging (for pattern detection) ---

    def log_query(
        self,
        query_text: str,
        agent_id: str,
        best_similarity: Optional[float],
        result_count: int,
    ) -> None:
        """Log a recall() call for pattern analysis without invalidating the vector cache."""
        self._ensure_initialized()
        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO queries
                    (id, namespace, query_text, agent_id, best_similarity,
                     result_count, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        self.namespace,
                        query_text,
                        agent_id,
                        best_similarity,
                        result_count,
                        _utc_now_iso(),
                    ),
                )
                conn.commit()
            self._refresh_stamps_after_write_locked(synced_cache)

    # --- Contradiction Tracking ---

    def log_contradiction(
        self,
        new_memory_id: str,
        new_content: str,
        existing_memory_id: str,
        existing_content: str,
        similarity: float,
    ) -> str:
        """Record a detected contradiction without invalidating the vector cache."""
        self._ensure_initialized()
        conflict_id = str(uuid.uuid4())
        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO contradictions
                    (id, namespace, new_memory_id, new_content,
                     existing_memory_id, existing_content, similarity, detected_at, resolved)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        conflict_id,
                        self.namespace,
                        new_memory_id,
                        new_content,
                        existing_memory_id,
                        existing_content,
                        similarity,
                        _utc_now_iso(),
                    ),
                )
                conn.commit()
            self._refresh_stamps_after_write_locked(synced_cache)
        return conflict_id

    def get_contradictions(self, resolved: bool = False) -> list[dict[str, Any]]:
        self._ensure_initialized()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM contradictions
                WHERE namespace = ? AND resolved = ?
                ORDER BY detected_at DESC
                """,
                (self.namespace, 1 if resolved else 0),
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_contradiction(self, contradiction_id: str) -> None:
        self._ensure_initialized()
        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            with self._connect() as conn:
                conn.execute(
                    "UPDATE contradictions SET resolved = 1 WHERE id = ? AND namespace = ?",
                    (contradiction_id, self.namespace),
                )
                conn.commit()
            self._refresh_stamps_after_write_locked(synced_cache)

    # --- Pattern Analysis ---

    def get_patterns(self) -> dict[str, list[dict[str, Any]]]:
        """
        Analyse query and memory data to surface:
        - top_topics: most queried subjects
        - memory_gaps: topics queried often (2+ times) with low similarity (<0.4)
        - unused_memories: memories never recalled after 7+ days
        - stale_memories: memories whose effective confidence has decayed below 0.3
        """
        self._ensure_initialized()
        now = _utc_now()
        cutoff = (now - timedelta(days=30)).isoformat()
        week_ago = (now - timedelta(days=7)).isoformat()

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            top_queries = conn.execute(
                """
                SELECT query_text, COUNT(*) as query_count,
                       AVG(best_similarity) as avg_similarity
                FROM queries
                WHERE namespace = ? AND timestamp > ?
                GROUP BY query_text
                ORDER BY query_count DESC
                LIMIT 10
                """,
                (self.namespace, cutoff),
            ).fetchall()

            gaps = conn.execute(
                """
                SELECT query_text, COUNT(*) as query_count,
                       AVG(best_similarity) as avg_similarity
                FROM queries
                WHERE namespace = ? AND timestamp > ?
                  AND best_similarity IS NOT NULL
                GROUP BY query_text
                HAVING COUNT(*) >= 2 AND AVG(best_similarity) < 0.4
                ORDER BY query_count DESC
                LIMIT 10
                """,
                (self.namespace, cutoff),
            ).fetchall()

            unused = conn.execute(
                """
                SELECT id, content, created_at, memory_type, source_agent
                FROM memories
                WHERE namespace = ? AND access_count = 0
                  AND created_at < ?
                ORDER BY created_at ASC
                LIMIT 10
                """,
                (self.namespace, week_ago),
            ).fetchall()

            all_ttl_memories = conn.execute(
                """
                SELECT id, content, confidence, ttl_days, decay_start_at,
                       created_at, memory_type, source_agent
                FROM memories
                WHERE namespace = ? AND ttl_days IS NOT NULL
                """,
                (self.namespace,),
            ).fetchall()

        stale: list[dict[str, Any]] = []
        for row in all_ttl_memories:
            m = dict(row)
            decay_start_str = m.get("decay_start_at") or m.get("created_at")
            ttl_days = m.get("ttl_days")
            start_dt = _parse_iso_utc(decay_start_str)
            if start_dt is not None and ttl_days and float(ttl_days) > 0:
                days_elapsed = max(0.0, (now - start_dt).total_seconds() / 86400.0)
                decay_factor = max(0.0, 1.0 - (days_elapsed / float(ttl_days)))
                effective_conf = float(m.get("confidence", 1.0)) * decay_factor
                if effective_conf < 0.3:
                    m["effective_confidence"] = round(effective_conf, 3)
                    m["days_elapsed"] = round(days_elapsed, 1)
                    stale.append(m)

        return {
            "top_topics": [dict(r) for r in top_queries],
            "memory_gaps": [dict(r) for r in gaps],
            "unused_memories": [dict(r) for r in unused],
            "stale_memories": stale,
        }