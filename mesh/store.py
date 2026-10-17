"""
mesh/store.py — High-performance unified persistence & hybrid search engine for Mesh.

Storage architecture:
  - SQLite (WAL mode) single-source-of-truth per namespace (`~/.mesh/mesh_<namespace>.db`)
  - Strict namespace (`validate_namespace`) and memory ID (`validate_memory_id`) validation
  - Enforced 0700 directory & 0600 database file permissions on POSIX
  - Persistent connection pooling with automatic teardown on namespace eviction/deletion
  - Normalized 384-dim float32 vector embeddings stored as compact binary BLOBs in `memories`
    alongside `embedding_model` provenance and O(1) `needs_healing_ids` tracking
  - Process-local in-memory capacity-doubling NumPy matrix cache + pre-tokenized lexical/IDF
    metadata cache with copy-on-write row updates & composite (.db + .db-wal + data_version) sync
  - SQLite FTS5 virtual table (`memories_fts`) indexed by integer `rowid` for O(log N) updates/deletes
"""
from __future__ import annotations

import atexit
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

from .audit import (
    MESH_DIR,
    REDACTED_CONTENT_PLACEHOLDER,
    UPDATED_CONTENT_PLACEHOLDER,
    ensure_secure_mesh_dir,
    get_mesh_dir,
    purge_memory_from_audit,
    scrub_memory_update_from_audit,
    secure_file_permissions,
    validate_namespace,
)

EMBEDDING_DIM = 384
STALE_THRESHOLD = 0.3
DEFAULT_MAX_QUERY_ROWS = 10000

_VALID_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Columns returned by metadata queries (explicitly excludes the raw binary `embedding` BLOB
# so row dictionaries remain 100% JSON-serializable across MCP, HTTP, CLI, and Dashboard).
_MEMORY_COLUMNS = (
    "id, namespace, content, confidence, memory_type, source_agent, "
    "tags, created_at, accessed_at, access_count, ttl_days, decay_start_at, local_only, embedding_model"
)

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_./:-]{2,}")
_WORD_RE = re.compile(r"[a-z0-9]+")
_PUNCT_SPLIT_RE = re.compile(r"[^a-z0-9]+")

_LEXICAL_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "about",
    "as", "into", "like", "through", "after", "over", "between", "out",
    "against", "during", "without", "before", "under", "around", "among",
    "what", "where", "when", "who", "whom", "which", "how", "why",
    "do", "does", "did", "have", "has", "had", "having", "can", "could",
    "should", "would", "may", "might", "must", "shall", "will", "that",
    "this", "these", "those", "there", "their", "they", "them", "it",
    "its", "we", "our", "you", "your", "i", "me", "my", "and", "or",
    "but", "if", "then", "else", "any", "all", "some", "such", "very",
    "tell", "know", "find", "get", "show", "give",
}

# Generic agent-memory framing words that appear in almost every personal/project memory.
_FRAMING_TOKENS = {"user", "users", "agent", "agents", "assistant", "human", "person", "system"}

# Composite file stamp: (db_mtime_ns, db_size, wal_mtime_ns, wal_size)
DbStamp = tuple[int, int, int, int]


def validate_memory_id(memory_id: str) -> str:
    """
    Validate a memory or contradiction identifier to prevent stored XSS, SQL/HTML injection,
    or control-character corruption.
    Allowed: 1-128 characters of ASCII letters, digits, '.', '_', ':', '-', starting with alphanumeric.
    """
    if not isinstance(memory_id, str):
        raise ValueError("Memory ID must be a string.")
    clean = memory_id.strip()
    if not clean or not _VALID_MEMORY_ID_RE.match(clean):
        raise ValueError(
            f"Invalid memory ID '{memory_id}'. Must match ^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$."
        )
    return clean


def _clean_possessives(text: str) -> str:
    """Normalize curly/straight apostrophe possessives ('user's' -> 'user')."""
    s = text.lower().replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    return re.sub(r"'s\b", "", s)


def _fast_stem(w: str) -> str:
    """Deterministic lightweight stemmer for lexical matching."""
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ing") and len(w) > 5:
        base = w[:-3]
        return base[:-1] if len(base) >= 3 and base[-1] == base[-2] else base
    if w.endswith("ed") and len(w) > 4:
        return w[:-2]
    if w.endswith("es") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


# --- Bidirectional Domain Synonym & Compound Expansion Engine ---
_SYNONYM_GROUPS: list[set[str]] = [
    # Colors & Visual Themes
    {"color", "colour", "hue", "shade", "tint"},
    {"blue", "navy", "azure", "cyan", "cobalt", "teal", "sapphire", "indigo"},
    {"red", "crimson", "scarlet", "maroon", "ruby", "vermilion", "burgundy"},
    {"green", "emerald", "lime", "olive", "jade", "mint"},
    {"yellow", "gold", "golden", "amber", "lemon", "mustard"},
    {"purple", "violet", "lavender", "magenta", "plum", "lilac"},
    {"dark", "night", "black", "dim", "obsidian", "charcoal"},
    {"light", "bright", "day", "white", "ivory", "pale"},
    # Everyday & Personal Objects / Preferences
    {"pen", "pens", "biro", "ballpoint", "marker", "stylus", "highlighter", "stationery", "pencil"},
    {"anime", "manga", "animation", "otaku", "cartoon", "series", "show"},
    {"favorite", "favourite", "preferred", "best", "top", "beloved", "choice", "likes", "loves"},
    {"laptop", "notebook", "macbook", "computer", "pc", "workstation", "machine"},
    {"phone", "smartphone", "mobile", "cellphone", "iphone", "android", "handset"},
    # Databases & Persistence
    {"db", "database", "databases", "datastore", "storage", "persistence"},
    {"postgres", "postgresql", "psql", "pg", "pgbouncer"},
    {"redis", "valkey", "memcached", "cache", "caching", "inmemory"},
    {"sqlite", "sqlite3", "wal", "fts", "fts5"},
    {"mongo", "mongodb", "nosql", "documentdb", "dynamodb"},
    # Auth & Security
    {"auth", "authentication", "authorization", "login", "signin", "sso", "oauth", "oidc", "rbac"},
    {"jwt", "token", "tokens", "bearer", "session", "cookie", "cookies"},
    {"password", "passphrase", "secret", "secrets", "credential", "credentials", "apikey", "key", "vault"},
    # Infrastructure, Networking & Environments
    {"prod", "production", "live", "main", "release"},
    {"stage", "staging", "preprod", "qa", "uat", "canary"},
    {"dev", "development", "local", "localhost", "sandbox", "loopback"},
    {"deploy", "deployment", "release", "rollout", "ship", "ci", "cd", "pipeline"},
    {"port", "socket", "listen", "endpoint", "url", "uri", "host", "hostname", "ip", "address"},
    {"k8s", "kubernetes", "cluster", "pod", "pods", "container", "docker", "helm"},
    # Engineering, Errors & Performance
    {"bug", "bugs", "error", "errors", "exception", "crash", "failure", "fault", "issue", "defect", "incident"},
    {"fix", "fixed", "patch", "resolve", "resolved", "workaround", "solution", "remedy", "repair"},
    {"config", "configuration", "settings", "env", "environment", "options", "parameters", "flags"},
    {"limit", "limits", "rate", "ratelimit", "quota", "throttle", "throttling", "cap", "timeout"},
    {"fast", "latency", "performance", "speed", "throughput", "slow", "bottleneck", "benchmark"},
    {"test", "tests", "testing", "pytest", "unittest", "spec", "suite", "verification"},
]

# Compound token splits: maps joined token -> tuple of split tokens (and vice versa)
_COMPOUND_SPLITS: dict[str, tuple[str, ...]] = {
    "onepiece": ("one", "piece"),
    "postgresql": ("postgres", "sql"),
    "pgbouncer": ("pg", "bouncer"),
    "typescript": ("type", "script"),
    "javascript": ("java", "script"),
    "apikey": ("api", "key"),
    "localstorage": ("local", "storage"),
    "httponly": ("http", "only"),
    "ratelimit": ("rate", "limit"),
    "darkmode": ("dark", "mode"),
    "lightmode": ("light", "mode"),
    "codebase": ("code", "base"),
    "healthcheck": ("health", "check"),
}

_SYNONYM_MAP: dict[str, set[str]] = {}
for _grp in _SYNONYM_GROUPS:
    _normalized_grp = {t.lower() for t in _grp}
    _stemmed_grp = {_fast_stem(t) for t in _normalized_grp}
    _all_forms = _normalized_grp | _stemmed_grp
    for _tok in _all_forms:
        _SYNONYM_MAP.setdefault(_tok, set()).update(_all_forms - {_tok})

_SPLIT_TO_COMPOUND: dict[tuple[str, ...], str] = {v: k for k, v in _COMPOUND_SPLITS.items()}


def _extract_search_tokens(text: str) -> tuple[list[str], list[str], set[str]]:
    """
    Return (raw_tokens, content_tokens, stemmed_content_set) from text.
    Preserves punctuated technical identifiers (`pg-bouncer`, `db.internal`, `ERR_CODE_9482`),
    their compact forms (`pgbouncer`, `dbinternal`), and constituent word tokens.
    """
    norm = _clean_possessives(text)
    raw_tokens = [t.strip("._:-/") for t in _TOKEN_RE.findall(norm)]
    raw_tokens = [t for t in raw_tokens if len(t) >= 2]
    word_tokens = _WORD_RE.findall(norm)

    compact_ident_tokens: list[str] = []
    for rt in raw_tokens:
        if any(ch in rt for ch in ".-_:/"):
            compact = _PUNCT_SPLIT_RE.sub("", rt)
            if len(compact) >= 3 and compact != rt:
                compact_ident_tokens.append(compact)

    combined = list(dict.fromkeys([*raw_tokens, *compact_ident_tokens, *word_tokens]))
    content_tokens = [t for t in combined if t not in _LEXICAL_STOPWORDS and len(t) >= 2]
    if not content_tokens:
        content_tokens = [t for t in combined if len(t) >= 2]
    stemmed_set = {_fast_stem(t) for t in content_tokens} | set(content_tokens)
    return combined, content_tokens, stemmed_set


def expand_query_tokens(content_tokens: list[str]) -> dict[str, set[str]]:
    """
    Given a list of cleaned query `content_tokens`, return a mapping:
        { original_token: {synonym_or_compound_1, synonym_or_compound_2, ...} }
    Includes domain synonyms, stemmed equivalents, punctuated identifier forms, and compound variants.
    """
    expansions: dict[str, set[str]] = {}
    lower_tokens = [t.lower() for t in content_tokens]

    adjacent_compounds: dict[int, str] = {}
    for i in range(len(lower_tokens) - 1):
        pair = (lower_tokens[i], lower_tokens[i + 1])
        if pair in _SPLIT_TO_COMPOUND:
            comp = _SPLIT_TO_COMPOUND[pair]
            adjacent_compounds[i] = comp
            adjacent_compounds[i + 1] = comp

    for idx, tok in enumerate(lower_tokens):
        stem = _fast_stem(tok)
        syns: set[str] = set()
        if tok in _SYNONYM_MAP:
            syns.update(_SYNONYM_MAP[tok])
        if stem in _SYNONYM_MAP:
            syns.update(_SYNONYM_MAP[stem])
        if tok in _COMPOUND_SPLITS:
            syns.update(_COMPOUND_SPLITS[tok])

        if any(c in tok for c in ".-_:/"):
            compact = _PUNCT_SPLIT_RE.sub("", tok)
            if len(compact) >= 3:
                syns.add(compact)
                if compact in _COMPOUND_SPLITS:
                    syns.update(_COMPOUND_SPLITS[compact])
                if compact in _SYNONYM_MAP:
                    syns.update(_SYNONYM_MAP[compact])
            parts = [p for p in _PUNCT_SPLIT_RE.split(tok) if len(p) >= 2]
            syns.update(parts)

        if idx in adjacent_compounds:
            syns.add(adjacent_compounds[idx])

        syns.discard(tok)
        syns.discard(stem)
        expansions[tok] = syns
    return expansions


def _make_snippet(content: str, max_len: int = 110) -> str:
    """Create a compact single-line preview snippet of a memory's content."""
    clean = " ".join(str(content or "").strip().split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3].rstrip() + "..."


def _build_row_lexical_entry(content: str, tags_list: list[str]) -> dict[str, Any]:
    """
    Pre-compute lexical tokens, stems, and compact identifier forms for a memory row once
    so `hybrid_query()` never re-tokenizes the corpus on every search.
    """
    doc_norm = _clean_possessives(content)
    doc_compact = re.sub(r"[^a-z0-9]", "", doc_norm)
    _, doc_tokens, doc_stems = _extract_search_tokens(content)
    tag_tokens = {str(t).strip().lower() for t in tags_list if str(t).strip()}
    tag_stems = {_fast_stem(t) for t in tag_tokens}
    tag_compacts = {re.sub(r"[^a-z0-9]", "", t) for t in tag_tokens if len(t) >= 2}
    doc_compacts = {re.sub(r"[^a-z0-9]", "", t) for t in doc_tokens if len(t) >= 2}

    all_doc_tokens = set(doc_tokens) | tag_tokens | doc_compacts | tag_compacts
    all_doc_stems = doc_stems | tag_stems
    combined_df_stems = all_doc_stems | set(doc_tokens)
    return {
        "doc_norm": doc_norm,
        "doc_compact": doc_compact,
        "doc_len": max(1, len(doc_tokens)),
        "tag_tokens": tag_tokens,
        "tag_stems": tag_stems,
        "tag_compacts": tag_compacts,
        "all_doc_tokens": all_doc_tokens,
        "all_doc_stems": all_doc_stems,
        "df_stems": combined_df_stems,
    }


def _row_needs_healing(row_meta: dict[str, Any], vec: Optional[np.ndarray]) -> bool:
    """Return True if a row's embedding was generated by the warm-up hash fallback or is missing."""
    emb_model = row_meta.get("embedding_model")
    if emb_model in ("hash-384", "needs-reembed", "unknown", None):
        return True
    if vec is None or vec.size != EMBEDDING_DIM:
        return True
    if np.all(vec[368:] == 0.0) and int(np.count_nonzero(vec == 0.0)) >= 16:
        return True
    return False


class _PooledConnection(sqlite3.Connection):
    """
    Persistent SQLite WAL connection that commits/rolls back when exiting a `with` block
    without closing the underlying OS file handle until explicitly evicted.
    """

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return super().__exit__(exc_type, exc_val, exc_tb)

    def close(self) -> None:
        pass

    def close_pooled(self) -> None:
        try:
            super().close()
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


def _compute_row_effective_confidence(
    row: dict[str, Any],
    now: Optional[datetime] = None,
) -> tuple[float, bool]:
    """Compute (effective_confidence, is_stale) for a row dictionary (unified staleness definition)."""
    stored_conf = float(row.get("confidence", 1.0))
    ttl_raw = row.get("ttl_days")
    if ttl_raw is None:
        return round(stored_conf, 4), stored_conf < STALE_THRESHOLD
    try:
        ttl = float(ttl_raw)
        if ttl <= 0.0:
            return 0.0, True
        decay_start = row.get("decay_start_at") or row.get("created_at")
        start_dt = _parse_iso_utc(decay_start)
        if start_dt is None:
            return round(stored_conf, 4), stored_conf < STALE_THRESHOLD
        ref_now = now if now is not None else _utc_now()
        days_elapsed = max(0.0, (ref_now - start_dt).total_seconds() / 86400.0)
        decay_factor = max(0.0, min(1.0, 1.0 - (days_elapsed / ttl)))
        effective_conf = round(stored_conf * decay_factor, 4)
        return effective_conf, effective_conf < STALE_THRESHOLD
    except Exception:
        return round(stored_conf, 4), stored_conf < STALE_THRESHOLD


def _get_db_stamp(db_path: Path) -> Optional[DbStamp]:
    """
    Return a composite `(db_mtime_ns, db_size, wal_mtime_ns, wal_size)` stamp.
    Tracking the `-wal` file alongside the main `.db` file guarantees that writes
    from other processes are detected immediately in O(1) time.
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


def _normalize_vector(vec: list[float] | np.ndarray, enforce_dim: bool = True) -> np.ndarray:
    """Convert vector to contiguous 1D float32 NumPy array, verify 384-dim, and L2-normalize it."""
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if enforce_dim and arr.size != EMBEDDING_DIM:
        raise ValueError(
            f"Invalid embedding dimension {arr.size}; Mesh requires {EMBEDDING_DIM}-dimensional vectors."
        )
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


def _parse_tags_json(raw_tags: Any) -> list[str]:
    if not raw_tags:
        return []
    if isinstance(raw_tags, list):
        return [str(t).strip() for t in raw_tags if str(t).strip()]
    if isinstance(raw_tags, str):
        s = raw_tags.strip()
        if not s or s == "[]":
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if str(t).strip()]
        except Exception:
            pass
    return []


class _VectorCacheEntry:
    """
    Holds the in-memory normalized embedding matrix (with capacity-doubling buffer),
    ID index, full row metadata dictionary, pre-tokenized lexical cache, incremental
    document-frequency (`stem_df`) counter, and `needs_healing_ids` set for a namespace.
    """

    __slots__ = (
        "stamp",
        "data_version",
        "mtime_ns",
        "file_size",
        "ids",
        "id_to_idx",
        "_matrix_buf",
        "dim",
        "all_ids",
        "rows_by_id",
        "lexical_by_id",
        "stem_df",
        "stem_to_ids",
        "needs_healing_ids",
    )

    def __init__(
        self,
        stamp: DbStamp,
        ids: list[str],
        matrix: Optional[np.ndarray],
        all_ids: Optional[set[str]] = None,
        rows_by_id: Optional[dict[str, dict[str, Any]]] = None,
        data_version: int = 0,
        needs_healing_ids: Optional[set[str]] = None,
        reused_lexical: Optional[dict[str, dict[str, Any]]] = None,
    ):
        self.stamp: DbStamp = stamp
        self.data_version: int = data_version
        self.mtime_ns: int = stamp[0]
        self.file_size: int = stamp[1]
        self.ids: list[str] = ids
        self.id_to_idx: dict[str, int] = {mid: idx for idx, mid in enumerate(ids)}
        self.all_ids: set[str] = all_ids if all_ids is not None else set(ids)
        self.rows_by_id: dict[str, dict[str, Any]] = rows_by_id if rows_by_id is not None else {}
        self.lexical_by_id: dict[str, dict[str, Any]] = {}
        self.stem_df: dict[str, int] = {}
        self.stem_to_ids: dict[str, set[str]] = {}
        self.needs_healing_ids: set[str] = (
            set(needs_healing_ids) if needs_healing_ids is not None else set()
        )

        if matrix is not None and matrix.ndim == 2 and matrix.shape[0] > 0:
            n_rows, dim = int(matrix.shape[0]), int(matrix.shape[1])
            cap = max(64, 1 << (n_rows - 1).bit_length())
            buf = np.empty((cap, dim), dtype=np.float32)
            buf[:n_rows] = matrix
            self._matrix_buf: Optional[np.ndarray] = buf
            self.dim: int = dim
        else:
            self._matrix_buf = None
            self.dim = 0

        for mid, row_meta in self.rows_by_id.items():
            if reused_lexical and mid in reused_lexical:
                lex = reused_lexical[mid]
                self.lexical_by_id[mid] = lex
                for s in lex["df_stems"]:
                    self.stem_df[s] = self.stem_df.get(s, 0) + 1
                    self.stem_to_ids.setdefault(s, set()).add(mid)
            else:
                self._index_row_lexical(mid, row_meta)

            if needs_healing_ids is None:
                idx = self.id_to_idx.get(mid)
                vec = self._matrix_buf[idx] if (idx is not None and self._matrix_buf is not None) else None
                if _row_needs_healing(row_meta, vec):
                    self.needs_healing_ids.add(mid)

    def _index_row_lexical(self, memory_id: str, row_meta: dict[str, Any]) -> None:
        old_lex = self.lexical_by_id.pop(memory_id, None)
        if old_lex is not None:
            for s in old_lex["df_stems"]:
                new_cnt = self.stem_df.get(s, 1) - 1
                if new_cnt <= 0:
                    self.stem_df.pop(s, None)
                else:
                    self.stem_df[s] = new_cnt
                s_set = self.stem_to_ids.get(s)
                if s_set is not None:
                    s_set.discard(memory_id)
                    if not s_set:
                        self.stem_to_ids.pop(s, None)

        content = str(row_meta.get("content", ""))
        tags_list = row_meta.get("tags_list") or _parse_tags_json(row_meta.get("tags"))
        new_lex = _build_row_lexical_entry(content, tags_list)
        self.lexical_by_id[memory_id] = new_lex
        for s in new_lex["df_stems"]:
            self.stem_df[s] = self.stem_df.get(s, 0) + 1
            self.stem_to_ids.setdefault(s, set()).add(memory_id)

    def _unindex_row_lexical(self, memory_id: str) -> None:
        old_lex = self.lexical_by_id.pop(memory_id, None)
        if old_lex is not None:
            for s in old_lex["df_stems"]:
                new_cnt = self.stem_df.get(s, 1) - 1
                if new_cnt <= 0:
                    self.stem_df.pop(s, None)
                else:
                    self.stem_df[s] = new_cnt
                s_set = self.stem_to_ids.get(s)
                if s_set is not None:
                    s_set.discard(memory_id)
                    if not s_set:
                        self.stem_to_ids.pop(s, None)

    @property
    def matrix(self) -> Optional[np.ndarray]:
        n = len(self.ids)
        if self._matrix_buf is None or n == 0:
            return None
        return self._matrix_buf[:n]

    def snapshot(
        self,
        copy_matrix: bool = False,
    ) -> tuple[list[str], Optional[np.ndarray], dict[str, dict[str, Any]]]:
        """Return a thread-safe snapshot of (ids, matrix_view, rows_by_id)."""
        with _CACHE_LOCK:
            ids_copy = list(self.ids)
            if self._matrix_buf is not None and ids_copy:
                mat_slice = self._matrix_buf[: len(ids_copy)]
                mat_view = mat_slice.copy() if copy_matrix else mat_slice
            else:
                mat_view = None
            rows_copy = dict(self.rows_by_id)
            return ids_copy, mat_view, rows_copy

    def update_stamp(self, stamp: DbStamp, data_version: Optional[int] = None) -> None:
        self.stamp = stamp
        self.mtime_ns = stamp[0]
        self.file_size = stamp[1]
        if data_version is not None:
            self.data_version = data_version

    def upsert_vector(
        self,
        memory_id: str,
        vec: Optional[np.ndarray],
        row_meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        Insert or update a normalized 1D float32 vector and its row metadata in-place
        using the capacity-doubling buffer (O(1) amortized).
        """
        self.all_ids.add(memory_id)
        if row_meta is not None:
            self.rows_by_id[memory_id] = dict(row_meta)
            self._index_row_lexical(memory_id, row_meta)
            if _row_needs_healing(row_meta, vec):
                self.needs_healing_ids.add(memory_id)
            else:
                self.needs_healing_ids.discard(memory_id)

        if vec is None or vec.size == 0:
            return True

        vec_1d = np.ascontiguousarray(vec.reshape(-1), dtype=np.float32)
        if vec_1d.shape[0] != EMBEDDING_DIM:
            return False

        existing_idx = self.id_to_idx.get(memory_id)
        if existing_idx is not None:
            if self._matrix_buf is None or vec_1d.shape[0] != self.dim:
                return False
            self._matrix_buf[existing_idx] = vec_1d
            return True

        n = len(self.ids)
        if self._matrix_buf is None or n == 0:
            dim = int(vec_1d.shape[0])
            buf = np.empty((64, dim), dtype=np.float32)
            buf[0] = vec_1d
            self._matrix_buf = buf
            self.dim = dim
            self.ids = [memory_id]
            self.id_to_idx = {memory_id: 0}
            return True

        if vec_1d.shape[0] != self.dim:
            return False

        if n >= self._matrix_buf.shape[0]:
            new_cap = max(64, self._matrix_buf.shape[0] * 2)
            new_buf = np.empty((new_cap, self.dim), dtype=np.float32)
            new_buf[:n] = self._matrix_buf[:n]
            self._matrix_buf = new_buf

        self._matrix_buf[n] = vec_1d
        self.id_to_idx[memory_id] = n
        self.ids.append(memory_id)
        return True

    def delete_vector(self, memory_id: str) -> None:
        """Remove a memory's vector, lexical index, and metadata from the in-memory cache in-place."""
        self.all_ids.discard(memory_id)
        self.needs_healing_ids.discard(memory_id)
        self.rows_by_id.pop(memory_id, None)
        self._unindex_row_lexical(memory_id)
        idx = self.id_to_idx.pop(memory_id, None)
        if idx is None:
            return
        n_before = len(self.ids)
        self.ids.pop(idx)
        for i in range(idx, len(self.ids)):
            self.id_to_idx[self.ids[i]] = i
        if not self.ids or self._matrix_buf is None or n_before <= 1:
            self._matrix_buf = None
            self.dim = 0
        else:
            if idx < n_before - 1:
                new_buf = self._matrix_buf.copy()
                new_buf[idx : n_before - 1] = self._matrix_buf[idx + 1 : n_before]
                self._matrix_buf = new_buf


# Process-wide caches protected by a re-entrant lock
_CACHE_LOCK = threading.RLock()
_CONNECTION_POOL: dict[Path, _PooledConnection] = {}
_VECTOR_CACHE: dict[Path, _VectorCacheEntry] = {}
_QUERY_WRITE_COUNTS: dict[Path, int] = {}
# Retained as empty dict for backward-compatibility with namespaces.py imports; never populated on SQLite writes
_TRANSIENT_EMBEDDINGS: dict[tuple[str, str], np.ndarray] = {}


def close_store_connection(db_path: Path) -> None:
    """Close and remove any pooled SQLite connection for `db_path`."""
    with _CACHE_LOCK:
        _QUERY_WRITE_COUNTS.pop(db_path, None)
        conn = _CONNECTION_POOL.pop(db_path, None)
        if conn is not None:
            conn.close_pooled()


class _InitializedDbRegistry(dict):
    """
    Dictionary tracking initialized DB stamps that automatically closes pooled
    SQLite connections when a DB path is popped or cleared.
    """

    def pop(self, key: Any, *args: Any) -> Any:
        close_store_connection(key)
        return super().pop(key, *args)

    def clear(self) -> None:
        for k in list(self.keys()):
            close_store_connection(k)
        super().clear()


_INITIALIZED_DBS: dict[Path, DbStamp] = _InitializedDbRegistry()


@atexit.register
def _close_all_pooled_connections() -> None:
    with _CACHE_LOCK:
        for conn in list(_CONNECTION_POOL.values()):
            conn.close_pooled()
        _CONNECTION_POOL.clear()


def _max_query_rows() -> int:
    try:
        val = int(os.environ.get("MESH_MAX_QUERY_ROWS", str(DEFAULT_MAX_QUERY_ROWS)))
        return max(100, val)
    except ValueError:
        return DEFAULT_MAX_QUERY_ROWS


def _prune_queries_table_locked(conn: sqlite3.Connection, namespace: str, max_rows: int) -> None:
    """Bound the `queries` table to `max_rows` entries per namespace."""
    try:
        cnt_row = conn.execute(
            "SELECT COUNT(*) FROM queries WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        if not cnt_row or int(cnt_row[0]) <= max_rows:
            return
        cutoff_row = conn.execute(
            """
            SELECT timestamp FROM queries
            WHERE namespace = ?
            ORDER BY timestamp DESC
            LIMIT 1 OFFSET ?
            """,
            (namespace, max_rows),
        ).fetchone()
        if cutoff_row and cutoff_row[0]:
            conn.execute(
                "DELETE FROM queries WHERE namespace = ? AND timestamp <= ?",
                (namespace, cutoff_row[0]),
            )
    except Exception:
        pass


class _CollectionCompatShim:
    """
    Backward-compatibility shim emulating ChromaDB's `store.collection` API.
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

        with _CACHE_LOCK:
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
                        "embedding_model": r["embedding_model"],
                    }
                )
            if want_embeddings:
                blob = r["embedding"]
                if blob:
                    out_embs.append(_blob_to_vector(blob).tolist())
                else:
                    out_embs.append(None)

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
                for i, raw_mid in enumerate(ids):
                    mid = validate_memory_id(raw_mid)
                    doc = documents[i] if documents and i < len(documents) else ""
                    meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                    emb = embeddings[i] if embeddings and i < len(embeddings) else None
                    norm_vec = _normalize_vector(emb) if emb is not None else None
                    blob = norm_vec.tobytes() if norm_vec is not None else None

                    tags_raw = meta.get("tags", "[]")
                    tags_str = tags_raw if isinstance(tags_raw, str) else json.dumps(tags_raw)
                    tags_list = _parse_tags_json(tags_raw)
                    mem_type = str(meta.get("memory_type", "fact"))
                    source_agent = str(meta.get("source_agent", "unknown"))
                    conf = float(meta.get("confidence", 1.0))
                    created_at = meta.get("created_at", now)
                    accessed_at = meta.get("accessed_at", now)
                    access_count = int(meta.get("access_count", 0))
                    ttl_days = meta.get("ttl_days")
                    decay_start_at = meta.get("decay_start_at")
                    local_flag = int(meta.get("local_only", 0))
                    emb_model = str(
                        meta.get("embedding_model")
                        or ("onnx-minilm-l6-v2" if blob else "needs-reembed")
                    )

                    old_rowid = self._store._get_memory_rowid(conn, mid)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO memories
                        (id, namespace, content, confidence, memory_type,
                         source_agent, tags, created_at, accessed_at, access_count,
                         ttl_days, decay_start_at, local_only, embedding, embedding_model)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            mid,
                            self._store.namespace,
                            doc,
                            conf,
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
                            emb_model,
                        ),
                    )
                    new_rowid = self._store._get_memory_rowid(conn, mid)
                    self._store._sync_fts_row(
                        conn,
                        memory_id=mid,
                        content=doc,
                        tags=tags_str,
                        memory_type=mem_type,
                        source_agent=source_agent,
                        old_rowid=old_rowid,
                        new_rowid=new_rowid,
                    )
                    if in_place_ok and synced_cache is not None:
                        row_meta = {
                            "id": mid,
                            "namespace": self._store.namespace,
                            "content": doc,
                            "confidence": conf,
                            "memory_type": mem_type,
                            "source_agent": source_agent,
                            "tags": tags_str,
                            "tags_list": tags_list,
                            "created_at": created_at,
                            "accessed_at": accessed_at,
                            "access_count": access_count,
                            "ttl_days": ttl_days,
                            "decay_start_at": decay_start_at,
                            "local_only": bool(local_flag),
                            "embedding_model": emb_model,
                        }
                        if not synced_cache.upsert_vector(mid, norm_vec, row_meta=row_meta):
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
    in-memory row & lexical metadata caching, and SQLite FTS5 BM25 full-text search
    in a single atomic database file.
    """

    def __init__(self, namespace: str = "default"):
        self.namespace = validate_namespace(namespace)
        base_dir = ensure_secure_mesh_dir().resolve()
        resolved_db = (base_dir / f"mesh_{self.namespace}.db").resolve()
        if resolved_db.parent != base_dir:
            raise ValueError(f"Invalid namespace path resolution for '{namespace}'.")
        self.db_path = resolved_db
        self._fts_enabled = True
        self._ensure_initialized()
        self.collection = _CollectionCompatShim(self)

    # --- Connection & Schema Management ---

    def _connect(self) -> _PooledConnection:
        """Return a persistent, concurrency-safe SQLite WAL connection for `self.db_path`."""
        with _CACHE_LOCK:
            conn = _CONNECTION_POOL.get(self.db_path)
            if conn is not None and self.db_path.exists():
                return conn
            if conn is not None:
                conn.close_pooled()
                _CONNECTION_POOL.pop(self.db_path, None)
            ensure_secure_mesh_dir(self.db_path.parent)
            conn = sqlite3.connect(
                self.db_path,
                timeout=10.0,
                check_same_thread=False,
                factory=_PooledConnection,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA secure_delete=ON")
            try:
                if os.name != "nt":
                    conn.execute("PRAGMA mmap_size=268435456")
                conn.execute("PRAGMA cache_size=-32000")
            except sqlite3.OperationalError:
                pass
            secure_file_permissions(self.db_path)
            _CONNECTION_POOL[self.db_path] = conn
            return conn

    def _get_data_version_locked(self) -> int:
        conn = _CONNECTION_POOL.get(self.db_path)
        if conn is None:
            return 0
        try:
            row = conn.execute("PRAGMA data_version").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _is_cache_synced_locked(self) -> Optional[_VectorCacheEntry]:
        """
        Return the active `_VectorCacheEntry` if both its composite DB+WAL stamp
        AND SQLite `PRAGMA data_version` match disk.
        """
        cached = _VECTOR_CACHE.get(self.db_path)
        if cached is None:
            return None
        stamp = _get_db_stamp(self.db_path)
        if stamp is None or cached.stamp != stamp:
            return None
        dv = self._get_data_version_locked()
        if dv != 0 and cached.data_version != 0 and dv != cached.data_version:
            return None
        return cached

    def _refresh_stamps_after_write_locked(
        self,
        synced_cache: Optional[_VectorCacheEntry],
    ) -> None:
        """
        Update `_INITIALIZED_DBS` and `synced_cache` composite file stamps after an in-process write.
        """
        secure_file_permissions(self.db_path)
        stamp = _get_db_stamp(self.db_path)
        if stamp is None:
            _INITIALIZED_DBS.pop(self.db_path, None)
            _VECTOR_CACHE.pop(self.db_path, None)
            return
        _INITIALIZED_DBS[self.db_path] = stamp
        if synced_cache is not None:
            dv = self._get_data_version_locked()
            synced_cache.update_stamp(stamp, data_version=dv)

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
        with _CACHE_LOCK:
            with self._connect() as conn:
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
                        embedding       BLOB,
                        embedding_model TEXT NOT NULL DEFAULT 'unknown'
                    )
                    """
                )

                existing_cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()
                }
                for col_name, col_sql in (
                    ("ttl_days", "ALTER TABLE memories ADD COLUMN ttl_days REAL"),
                    ("decay_start_at", "ALTER TABLE memories ADD COLUMN decay_start_at TEXT"),
                    ("local_only", "ALTER TABLE memories ADD COLUMN local_only INTEGER DEFAULT 0"),
                    ("embedding", "ALTER TABLE memories ADD COLUMN embedding BLOB"),
                    ("embedding_model", "ALTER TABLE memories ADD COLUMN embedding_model TEXT DEFAULT 'unknown'"),
                ):
                    if col_name not in existing_cols:
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

                # Full-Text Search (FTS5) table synchronized with `memories.rowid` for O(log N) deletes
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
                    if mem_count > 0:
                        fts_count = int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0])
                        misaligned = conn.execute(
                            """
                            SELECT 1 FROM memories m
                            LEFT JOIN memories_fts f ON f.rowid = m.rowid
                            WHERE f.rowid IS NULL OR f.id != m.id
                            LIMIT 1
                            """
                        ).fetchone()
                        if fts_count != mem_count or misaligned is not None:
                            conn.execute("DELETE FROM memories_fts")
                            conn.execute(
                                """
                                INSERT INTO memories_fts (rowid, id, content, tags, memory_type, source_agent)
                                SELECT rowid, id, content, tags, memory_type, source_agent FROM memories
                                """
                            )
                except sqlite3.OperationalError:
                    self._fts_enabled = False

                conn.commit()
            secure_file_permissions(self.db_path)

            if mem_count == 0:
                stamp = _get_db_stamp(self.db_path)
                if stamp is not None:
                    _VECTOR_CACHE[self.db_path] = _VectorCacheEntry(
                        stamp=stamp,
                        ids=[],
                        matrix=None,
                        all_ids=set(),
                        rows_by_id={},
                        data_version=self._get_data_version_locked(),
                        needs_healing_ids=set(),
                    )

            if mem_count > 0:
                self._migrate_from_chroma_if_needed()

    def _migrate_from_chroma_if_needed(self) -> None:
        """
        If rows exist without an SQLite embedding BLOB and a legacy `~/.mesh/chroma`
        directory exists, migrate the vectors into SQLite automatically.
        """
        chroma_dir = get_mesh_dir() / "chroma"
        if not chroma_dir.exists():
            return
        with _CACHE_LOCK:
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
                            if emb is not None and len(emb) == EMBEDDING_DIM:
                                conn.execute(
                                    "UPDATE memories SET embedding = ?, embedding_model = 'onnx-minilm-l6-v2' WHERE id = ?",
                                    (_vector_to_blob(emb), mid),
                                )
                        conn.commit()
                    self._invalidate_cache()
            except Exception:
                pass

    @staticmethod
    def _get_memory_rowid(conn: sqlite3.Connection, memory_id: str) -> Optional[int]:
        row = conn.execute("SELECT rowid FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return int(row[0]) if row is not None else None

    def _sync_fts_row(
        self,
        conn: sqlite3.Connection,
        memory_id: str,
        content: str,
        tags: str,
        memory_type: str,
        source_agent: str,
        old_rowid: Optional[int] = None,
        new_rowid: Optional[int] = None,
    ) -> None:
        """Synchronize an FTS5 entry in O(log N) time using integer `rowid`."""
        if not self._fts_enabled:
            return
        try:
            if old_rowid is not None:
                conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (old_rowid,))
            if new_rowid is None:
                new_rowid = self._get_memory_rowid(conn, memory_id)
            if new_rowid is not None:
                conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (new_rowid,))
                conn.execute(
                    """
                    INSERT INTO memories_fts (rowid, id, content, tags, memory_type, source_agent)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (new_rowid, memory_id, content, tags, memory_type, source_agent),
                )
            else:
                conn.execute("DELETE FROM memories_fts WHERE id = ?", (memory_id,))
                conn.execute(
                    "INSERT INTO memories_fts (id, content, tags, memory_type, source_agent) VALUES (?, ?, ?, ?, ?)",
                    (memory_id, content, tags, memory_type, source_agent),
                )
        except sqlite3.OperationalError:
            pass

    def _delete_fts_row(
        self,
        conn: sqlite3.Connection,
        memory_id: str,
        rowid: Optional[int] = None,
    ) -> None:
        if not self._fts_enabled:
            return
        try:
            if rowid is not None:
                conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
            else:
                conn.execute("DELETE FROM memories_fts WHERE id = ?", (memory_id,))
        except sqlite3.OperationalError:
            pass

    # --- In-Memory Vector & Metadata Matrix Cache ---

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
        Return the normalized 384-dim float32 embedding matrix, row metadata cache,
        and pre-computed lexical IDF cache for this namespace.
        """
        with _CACHE_LOCK:
            if not self.db_path.exists():
                self._ensure_initialized()
            synced = self._is_cache_synced_locked()
            if synced is not None:
                _INITIALIZED_DBS[self.db_path] = synced.stamp
                return synced

            prev_cache = _VECTOR_CACHE.get(self.db_path)

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"SELECT {_MEMORY_COLUMNS}, embedding FROM memories WHERE namespace = ?",
                    (self.namespace,),
                ).fetchall()

            ids: list[str] = []
            all_ids: set[str] = set()
            rows_by_id: dict[str, dict[str, Any]] = {}
            vecs: list[np.ndarray] = []
            needs_healing_ids: set[str] = set()
            reused_lexical: dict[str, dict[str, Any]] = {}

            for r in rows:
                mid = r["id"]
                all_ids.add(mid)
                tags_str = r["tags"] or "[]"
                blob = r["embedding"]
                emb_model = str(r["embedding_model"] or "unknown")

                vec: Optional[np.ndarray] = None
                if prev_cache is not None and mid in prev_cache.rows_by_id:
                    prev_meta = prev_cache.rows_by_id[mid]
                    if (
                        prev_meta.get("content") == r["content"]
                        and prev_meta.get("tags") == tags_str
                        and prev_meta.get("embedding_model") == emb_model
                    ):
                        if mid in prev_cache.lexical_by_id:
                            reused_lexical[mid] = prev_cache.lexical_by_id[mid]
                        prev_idx = prev_cache.id_to_idx.get(mid)
                        if prev_idx is not None and prev_cache._matrix_buf is not None:
                            vec = prev_cache._matrix_buf[prev_idx]

                if vec is None:
                    if blob:
                        arr = _blob_to_vector(blob)
                        if arr.size == EMBEDDING_DIM:
                            vec = arr
                            if emb_model == "unknown":
                                if np.all(arr[368:] == 0.0) and int(np.count_nonzero(arr == 0.0)) >= 16:
                                    emb_model = "hash-384"
                                else:
                                    emb_model = "onnx-minilm-l6-v2"
                        else:
                            emb_model = "needs-reembed"
                    else:
                        emb_model = "needs-reembed"

                row_meta = {
                    "id": mid,
                    "namespace": r["namespace"],
                    "content": r["content"],
                    "confidence": float(r["confidence"]),
                    "memory_type": r["memory_type"],
                    "source_agent": r["source_agent"],
                    "tags": tags_str,
                    "tags_list": _parse_tags_json(tags_str),
                    "created_at": r["created_at"],
                    "accessed_at": r["accessed_at"],
                    "access_count": int(r["access_count"] or 0),
                    "ttl_days": r["ttl_days"],
                    "decay_start_at": r["decay_start_at"],
                    "local_only": bool(r["local_only"]),
                    "embedding_model": emb_model,
                }
                rows_by_id[mid] = row_meta
                if _row_needs_healing(row_meta, vec):
                    needs_healing_ids.add(mid)

                if vec is not None:
                    ids.append(mid)
                    vecs.append(vec)

            stamp = _get_db_stamp(self.db_path) or (0, 0, 0, 0)
            dv = self._get_data_version_locked()
            matrix = np.ascontiguousarray(np.vstack(vecs), dtype=np.float32) if vecs else None
            entry = _VectorCacheEntry(
                stamp=stamp,
                ids=ids,
                matrix=matrix,
                all_ids=all_ids,
                rows_by_id=rows_by_id,
                data_version=dv,
                needs_healing_ids=needs_healing_ids,
                reused_lexical=reused_lexical,
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
        embedding_model: Optional[str] = None,
    ) -> None:
        """
        Store or overwrite a memory and its normalized 384-dim vector in a single atomic transaction,
        updating the in-memory vector matrix and lexical metadata cache in-place in O(1) time.
        """
        memory_id = validate_memory_id(memory_id)
        self._ensure_initialized()
        now = _utc_now_iso()
        norm_vec = _normalize_vector(embedding, enforce_dim=True)
        blob = norm_vec.tobytes()

        tags_raw = metadata.get("tags", [])
        if isinstance(tags_raw, str):
            tags_str = tags_raw
            tags_list = _parse_tags_json(tags_raw)
        else:
            tags_list = [str(t).strip() for t in (tags_raw or []) if str(t).strip()]
            tags_str = json.dumps(tags_list)

        mem_type = str(metadata.get("memory_type", "fact"))
        source_agent = str(metadata.get("source_agent", "unknown"))
        confidence = float(metadata.get("confidence", 1.0))
        created_at = metadata.get("created_at") or now
        accessed_at = metadata.get("accessed_at") or now
        access_count = int(metadata.get("access_count", 0))
        ttl_days = metadata.get("ttl_days")
        decay_start_at = metadata.get("decay_start_at")
        local_flag = 1 if (local_only or metadata.get("local_only")) else 0
        emb_model = str(
            embedding_model
            or metadata.get("embedding_model")
            or ("hash-384" if np.all(norm_vec[368:] == 0.0) else "onnx-minilm-l6-v2")
        )

        row_meta = {
            "id": memory_id,
            "namespace": self.namespace,
            "content": content,
            "confidence": confidence,
            "memory_type": mem_type,
            "source_agent": source_agent,
            "tags": tags_str,
            "tags_list": tags_list,
            "created_at": created_at,
            "accessed_at": accessed_at,
            "access_count": access_count,
            "ttl_days": ttl_days,
            "decay_start_at": decay_start_at,
            "local_only": bool(local_flag),
            "embedding_model": emb_model,
        }

        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            with self._connect() as conn:
                old_rowid = self._get_memory_rowid(conn, memory_id)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO memories
                    (id, namespace, content, confidence, memory_type,
                     source_agent, tags, created_at, accessed_at, access_count,
                     ttl_days, decay_start_at, local_only, embedding, embedding_model)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        emb_model,
                    ),
                )
                new_rowid = self._get_memory_rowid(conn, memory_id)
                self._sync_fts_row(
                    conn,
                    memory_id=memory_id,
                    content=content,
                    tags=tags_str,
                    memory_type=mem_type,
                    source_agent=source_agent,
                    old_rowid=old_rowid,
                    new_rowid=new_rowid,
                )
                conn.commit()

            if synced_cache is not None and synced_cache.upsert_vector(
                memory_id, norm_vec, row_meta=row_meta
            ):
                self._refresh_stamps_after_write_locked(synced_cache)
            else:
                self._invalidate_cache()

    def upsert_batch(
        self,
        items: list[tuple[str, str, list[float], dict[str, Any], bool]],
    ) -> None:
        """
        Batch-insert or overwrite multiple memories in a single SQLite WAL transaction
        with in-place vector and lexical metadata cache updates.
        """
        if not items:
            return
        self._ensure_initialized()
        now = _utc_now_iso()

        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            in_place_ok = synced_cache is not None
            with self._connect() as conn:
                for raw_memory_id, content, embedding, metadata, local_only in items:
                    memory_id = validate_memory_id(raw_memory_id)
                    norm_vec = _normalize_vector(embedding, enforce_dim=True)
                    blob = norm_vec.tobytes()

                    tags_raw = metadata.get("tags", [])
                    if isinstance(tags_raw, str):
                        tags_str = tags_raw
                        tags_list = _parse_tags_json(tags_raw)
                    else:
                        tags_list = [str(t).strip() for t in (tags_raw or []) if str(t).strip()]
                        tags_str = json.dumps(tags_list)

                    mem_type = str(metadata.get("memory_type", "fact"))
                    source_agent = str(metadata.get("source_agent", "unknown"))
                    confidence = float(metadata.get("confidence", 1.0))
                    created_at = metadata.get("created_at") or now
                    accessed_at = metadata.get("accessed_at") or now
                    access_count = int(metadata.get("access_count", 0))
                    ttl_days = metadata.get("ttl_days")
                    decay_start_at = metadata.get("decay_start_at")
                    local_flag = 1 if (local_only or metadata.get("local_only")) else 0
                    emb_model = str(
                        metadata.get("embedding_model")
                        or ("hash-384" if np.all(norm_vec[368:] == 0.0) else "onnx-minilm-l6-v2")
                    )

                    old_rowid = self._get_memory_rowid(conn, memory_id)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO memories
                        (id, namespace, content, confidence, memory_type,
                         source_agent, tags, created_at, accessed_at, access_count,
                         ttl_days, decay_start_at, local_only, embedding, embedding_model)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            emb_model,
                        ),
                    )
                    new_rowid = self._get_memory_rowid(conn, memory_id)
                    self._sync_fts_row(
                        conn,
                        memory_id=memory_id,
                        content=content,
                        tags=tags_str,
                        memory_type=mem_type,
                        source_agent=source_agent,
                        old_rowid=old_rowid,
                        new_rowid=new_rowid,
                    )
                    if in_place_ok and synced_cache is not None:
                        row_meta = {
                            "id": memory_id,
                            "namespace": self.namespace,
                            "content": content,
                            "confidence": confidence,
                            "memory_type": mem_type,
                            "source_agent": source_agent,
                            "tags": tags_str,
                            "tags_list": tags_list,
                            "created_at": created_at,
                            "accessed_at": accessed_at,
                            "access_count": access_count,
                            "ttl_days": ttl_days,
                            "decay_start_at": decay_start_at,
                            "local_only": bool(local_flag),
                            "embedding_model": emb_model,
                        }
                        if not synced_cache.upsert_vector(memory_id, norm_vec, row_meta=row_meta):
                            in_place_ok = False
                conn.commit()

            if in_place_ok and synced_cache is not None:
                self._refresh_stamps_after_write_locked(synced_cache)
            else:
                self._invalidate_cache()

    def resolve_id_prefix(self, prefix: str) -> Optional[str]:
        """
        Resolve a full memory ID from a 4+ character short ID prefix in O(1) time
        via the in-memory vector cache. Raises ValueError if ambiguous.
        Never uses arbitrary substring matching.
        """
        clean_pref = prefix.strip().lower()
        if not clean_pref:
            return None
        with _CACHE_LOCK:
            cache = self._get_vector_cache()
            if clean_pref in cache.rows_by_id:
                return clean_pref
            for mid in cache.all_ids:
                if mid.lower() == clean_pref:
                    return mid
            if len(clean_pref) < 4:
                return None
            matches = [mid for mid in cache.all_ids if mid.lower().startswith(clean_pref)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous ID prefix '{prefix}' matches multiple memories: {matches[:3]}. "
                "Please provide a longer prefix."
            )
        return None

    def get(self, memory_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single memory by ID in O(1) from the synced RAM metadata cache."""
        with _CACHE_LOCK:
            cache = self._get_vector_cache()
            row = cache.rows_by_id.get(memory_id)
            if row is not None:
                return dict(row)
        return None

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
        clear_ttl: bool = False,
        local_only: Optional[bool] = None,
        source_agent: Optional[str] = None,
        embedding_model: Optional[str] = None,
        preserve_accessed_at: bool = False,
    ) -> Optional[dict[str, Any]]:
        """
        Update an existing memory in-place while preserving its `id`, `created_at`,
        `source_agent` (unless explicitly provided), and `access_count`.
        Scrubs old content from `contradictions` and `audit_log` if content is replaced
        or `local_only=True` is set.
        """
        self._ensure_initialized()
        now = _utc_now_iso()
        norm_vec: Optional[np.ndarray] = (
            _normalize_vector(embedding, enforce_dim=True) if embedding is not None else None
        )

        old_content_for_scrub: Optional[str] = None
        should_scrub_audit = False
        new_local_bool = False

        with _CACHE_LOCK:
            cache = self._get_vector_cache()
            existing_meta = cache.rows_by_id.get(memory_id)
            if existing_meta is None:
                return None

            old_content = str(existing_meta.get("content", ""))
            new_content = content if content is not None else old_content
            new_type = memory_type if memory_type is not None else existing_meta["memory_type"]
            new_conf = (
                float(confidence)
                if confidence is not None
                else float(existing_meta["confidence"])
            )
            new_agent = source_agent if source_agent is not None else existing_meta["source_agent"]
            if tags is not None:
                new_tags_list = [str(t).strip() for t in tags if str(t).strip()]
                new_tags_str = json.dumps(new_tags_list)
            else:
                new_tags_str = existing_meta["tags"]
                new_tags_list = existing_meta.get("tags_list") or _parse_tags_json(new_tags_str)

            new_local = (
                (1 if local_only else 0)
                if local_only is not None
                else (1 if existing_meta.get("local_only") else 0)
            )
            new_local_bool = bool(new_local)

            if clear_ttl:
                new_ttl = None
                new_decay_start = None
            elif reset_ttl or ttl_days is not None:
                new_ttl = ttl_days
                new_decay_start = now if ttl_days is not None else None
            else:
                new_ttl = existing_meta["ttl_days"]
                new_decay_start = existing_meta["decay_start_at"]

            if norm_vec is not None:
                new_emb_model = str(
                    embedding_model
                    or ("hash-384" if np.all(norm_vec[368:] == 0.0) else "onnx-minilm-l6-v2")
                )
            else:
                new_emb_model = str(
                    embedding_model or existing_meta.get("embedding_model", "onnx-minilm-l6-v2")
                )

            new_accessed_at = existing_meta["accessed_at"] if preserve_accessed_at else now
            content_changed = new_content != old_content
            became_private = new_local_bool and not bool(existing_meta.get("local_only"))

            if content_changed or new_local_bool:
                old_content_for_scrub = old_content
                should_scrub_audit = True

            with self._connect() as conn:
                rowid = self._get_memory_rowid(conn, memory_id)
                if norm_vec is not None:
                    new_blob = norm_vec.tobytes()
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
                            embedding = ?,
                            embedding_model = ?
                        WHERE id = ? AND namespace = ?
                        """,
                        (
                            new_content,
                            new_conf,
                            new_type,
                            new_agent,
                            new_tags_str,
                            new_accessed_at,
                            new_ttl,
                            new_decay_start,
                            new_local,
                            new_blob,
                            new_emb_model,
                            memory_id,
                            self.namespace,
                        ),
                    )
                else:
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
                            embedding_model = ?
                        WHERE id = ? AND namespace = ?
                        """,
                        (
                            new_content,
                            new_conf,
                            new_type,
                            new_agent,
                            new_tags_str,
                            new_accessed_at,
                            new_ttl,
                            new_decay_start,
                            new_local,
                            new_emb_model,
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
                    old_rowid=rowid,
                    new_rowid=rowid,
                )
                if content_changed or became_private:
                    replacement_text = (
                        REDACTED_CONTENT_PLACEHOLDER if new_local_bool else new_content
                    )
                    conn.execute(
                        """
                        UPDATE contradictions
                        SET new_content = ?
                        WHERE namespace = ? AND new_memory_id = ?
                        """,
                        (replacement_text, self.namespace, memory_id),
                    )
                    conn.execute(
                        """
                        UPDATE contradictions
                        SET existing_content = ?
                        WHERE namespace = ? AND existing_memory_id = ?
                        """,
                        (replacement_text, self.namespace, memory_id),
                    )
                conn.commit()

            updated_row = {
                "id": memory_id,
                "namespace": self.namespace,
                "content": new_content,
                "confidence": new_conf,
                "memory_type": new_type,
                "source_agent": new_agent,
                "tags": new_tags_str,
                "tags_list": new_tags_list,
                "created_at": existing_meta["created_at"],
                "accessed_at": new_accessed_at,
                "access_count": int(existing_meta["access_count"] or 0),
                "ttl_days": new_ttl,
                "decay_start_at": new_decay_start,
                "local_only": new_local_bool,
                "embedding_model": new_emb_model,
            }
            if norm_vec is not None:
                if cache.upsert_vector(memory_id, norm_vec, row_meta=updated_row):
                    self._refresh_stamps_after_write_locked(cache)
                else:
                    self._invalidate_cache()
            else:
                cache.rows_by_id[memory_id] = dict(updated_row)
                cache._index_row_lexical(memory_id, updated_row)
                idx = cache.id_to_idx.get(memory_id)
                existing_vec = (
                    cache._matrix_buf[idx]
                    if (idx is not None and cache._matrix_buf is not None)
                    else None
                )
                if _row_needs_healing(updated_row, existing_vec):
                    cache.needs_healing_ids.add(memory_id)
                else:
                    cache.needs_healing_ids.discard(memory_id)
                self._refresh_stamps_after_write_locked(cache)

        if should_scrub_audit and not preserve_accessed_at:
            scrub_memory_update_from_audit(
                namespace=self.namespace,
                memory_id=memory_id,
                old_content=old_content_for_scrub,
                local_only=new_local_bool,
            )
        return dict(updated_row)

    # --- Vector & Hybrid Search Operations ---

    def query(self, embedding: list[float], n: int = 5) -> dict[str, list[list[Any]]]:
        """
        100% in-memory dense cosine similarity search returning a ChromaDB-compatible dict:
        {"ids": [[...]], "documents": [[...]], "distances": [[...]], "metadatas": [[...]]}
        """
        cache = self._get_vector_cache()
        ids_snap, mat, rows_snap = cache.snapshot(copy_matrix=False)
        if mat is None or len(ids_snap) == 0 or n <= 0:
            return {"documents": [[]], "distances": [[]], "metadatas": [[]], "ids": [[]]}

        q_vec = _normalize_vector(embedding, enforce_dim=False)
        if q_vec.shape[0] != mat.shape[1]:
            return {"documents": [[]], "distances": [[]], "metadatas": [[]], "ids": [[]]}

        sims = mat @ q_vec
        k = min(n, len(ids_snap))
        if k < len(ids_snap):
            top_indices = np.argpartition(-sims, k - 1)[:k]
            top_indices = top_indices[np.argsort(-sims[top_indices])]
        else:
            top_indices = np.argsort(-sims)

        out_ids: list[str] = []
        out_docs: list[str] = []
        out_dists: list[float] = []
        out_metas: list[dict[str, Any]] = []

        for raw_idx in top_indices:
            idx = int(raw_idx)
            mid = ids_snap[idx]
            row = rows_snap.get(mid)
            if not row:
                continue
            sim = max(-1.0, min(1.0, float(sims[idx])))
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
                    "local_only": 1 if row.get("local_only") else 0,
                    "embedding_model": row.get("embedding_model", "onnx-minilm-l6-v2"),
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
        limit: int = 250,
        conn: Optional[sqlite3.Connection] = None,
    ) -> dict[str, float]:
        """
        Run a sanitized FTS5 search over `memories_fts` with automatic synonym and
        punctuated-identifier expansion (`pg-bouncer` -> `"pg" "bouncer"` OR `"pgbouncer"*`).
        """
        if not self._fts_enabled or not query_text:
            return {}

        _, content_tokens, _ = _extract_search_tokens(query_text)
        if not content_tokens:
            return {}

        non_framing = [t for t in content_tokens if _fast_stem(t) not in _FRAMING_TOKENS]
        target_tokens = non_framing if non_framing else content_tokens
        synonym_map = expand_query_tokens(target_tokens)

        safe_clauses: list[str] = []
        for tok in target_tokens[:12]:
            if any(c in tok for c in ".-_:/"):
                parts = [re.sub(r"[^a-zA-Z0-9]", "", p) for p in _PUNCT_SPLIT_RE.split(tok)]
                parts = [p for p in parts if len(p) >= 2]
                if len(parts) >= 2:
                    phrase = " ".join(f'"{p}"' for p in parts)
                    safe_clauses.append(f"({phrase})")
                for p in parts:
                    safe_clauses.append(f'"{p}"*')
                joined = "".join(parts)
                if len(joined) >= 3:
                    safe_clauses.append(f'"{joined}"*')
            else:
                clean = re.sub(r"[^a-zA-Z0-9_]", "", tok)
                if len(clean) >= 2:
                    safe_clauses.append(f'"{clean}"*')
                    stem = _fast_stem(clean)
                    if len(stem) >= 3 and stem != clean:
                        safe_clauses.append(f'"{stem}"*')

        for tok in target_tokens[:8]:
            for syn in sorted(synonym_map.get(tok, set()))[:4]:
                clean_syn = re.sub(r"[^a-zA-Z0-9_]", "", syn)
                if len(clean_syn) >= 3:
                    safe_clauses.append(f'"{clean_syn}"*')
                if len(safe_clauses) >= 32:
                    break

        if not safe_clauses:
            return {}

        fts_query = " OR ".join(dict.fromkeys(safe_clauses))
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
                with _CACHE_LOCK:
                    with self._connect() as own_conn:
                        rows = own_conn.execute(sql, (fts_query, limit)).fetchall()
            if not rows:
                return {}
            raw_positives = [max(0.0, -float(r[1])) for r in rows]
            max_bm25 = max(raw_positives) if raw_positives else 0.0
            if max_bm25 <= 1e-9:
                for rank_idx, (mid, _) in enumerate(rows):
                    scores[str(mid)] = round(max(0.35, 0.85 - 0.1 * rank_idx), 4)
                return scores
            for (mid, _), pos in zip(rows, raw_positives):
                rel_score = pos / max_bm25
                abs_factor = min(1.0, pos / (pos + 2.0))
                scores[str(mid)] = round(0.65 * rel_score + 0.35 * abs_factor, 4)
        except Exception:
            return {}
        return scores

    def _score_candidate_lexical(
        self,
        q_content_tokens: list[str],
        q_stems: list[str],
        q_norm_phrase: str,
        q_compact: str,
        doc_content: str,
        doc_tags: list[str],
        idf_map: dict[str, float],
        total_query_idf: float,
        fts_score: float,
        q_synonyms: Optional[dict[str, set[str]]] = None,
        cached_lex: Optional[dict[str, Any]] = None,
    ) -> tuple[float, float, str]:
        """
        Compute (lexical_score, specific_content_coverage, match_reason) for a single memory row
        using pre-tokenized `cached_lex` metadata when available.
        """
        if not q_content_tokens:
            return fts_score, 0.0, "semantic"

        lex = cached_lex if cached_lex is not None else _build_row_lexical_entry(doc_content, doc_tags)
        doc_norm = lex["doc_norm"]
        doc_compact = lex["doc_compact"]
        doc_len = lex["doc_len"]
        tag_tokens = lex["tag_tokens"]
        tag_stems = lex["tag_stems"]
        tag_compacts = lex["tag_compacts"]
        all_doc_tokens = lex["all_doc_tokens"]
        all_doc_stems = lex["all_doc_stems"]

        syn_lookup = q_synonyms if q_synonyms is not None else expand_query_tokens(q_content_tokens)

        matched_idf = 0.0
        specific_total_idf = 0.0
        specific_matched_idf = 0.0
        exact_hits = 0
        synonym_hits = 0
        tag_hits = 0

        for tok, stem in zip(q_content_tokens, q_stems):
            w_idf = idf_map.get(stem, 1.0)
            is_specific = stem not in _FRAMING_TOKENS
            if is_specific:
                specific_total_idf += w_idf

            tok_compact = re.sub(r"[^a-z0-9]", "", tok)
            tok_syns = syn_lookup.get(tok, set())

            if tok in all_doc_tokens or (len(tok_compact) >= 3 and tok_compact in all_doc_tokens):
                matched_idf += w_idf
                if is_specific:
                    specific_matched_idf += w_idf
                    exact_hits += 1
                if tok in tag_tokens or stem in tag_stems or (tok_compact and tok_compact in tag_compacts):
                    tag_hits += 1
            elif stem in all_doc_stems:
                matched_idf += w_idf * 0.92
                if is_specific:
                    specific_matched_idf += w_idf * 0.92
                    exact_hits += 1
            elif len(tok_compact) >= 4 and (tok in doc_norm or tok_compact in doc_compact):
                matched_idf += w_idf * 0.90
                if is_specific:
                    specific_matched_idf += w_idf * 0.90
                    exact_hits += 1
            elif tok_syns and (tok_syns & all_doc_tokens or tok_syns & all_doc_stems):
                matched_idf += w_idf * 0.72
                if is_specific:
                    specific_matched_idf += w_idf * 0.72
                    synonym_hits += 1
                if tok_syns & tag_tokens or tok_syns & tag_stems:
                    tag_hits += 1
            elif is_specific and len(tok_compact) >= 4:
                for dt in all_doc_tokens:
                    if len(dt) >= 4 and (dt.startswith(tok_compact[:4]) or tok_compact.startswith(dt[:4])):
                        matched_idf += w_idf * 0.55
                        specific_matched_idf += w_idf * 0.55
                        break

        idf_coverage = matched_idf / max(1e-9, total_query_idf)
        if specific_total_idf > 0:
            specific_coverage = specific_matched_idf / specific_total_idf
        else:
            specific_coverage = idf_coverage

        phrase_bonus = 0.0
        if len(q_norm_phrase) >= 4 and q_norm_phrase in doc_norm:
            phrase_bonus = 0.25
        elif len(q_compact) >= 4 and q_compact in doc_compact:
            phrase_bonus = 0.22

        bm25_tf_norm = min(1.0, 6.0 / (doc_len + 4.0)) * 0.12 if specific_coverage > 0 else 0.0
        computed_lex = min(
            1.0,
            0.65 * specific_coverage + 0.35 * idf_coverage + phrase_bonus + bm25_tf_norm,
        )
        if specific_coverage > 0:
            final_lex = max(computed_lex, fts_score * specific_coverage)
        else:
            final_lex = computed_lex * 0.25

        if exact_hits > 0 and specific_coverage >= 0.5:
            reason = "exact+semantic"
        elif synonym_hits > 0 and exact_hits == 0:
            reason = "synonym+semantic"
        elif tag_hits > 0:
            reason = "tag+semantic"
        elif specific_coverage > 0:
            reason = "lexical+semantic"
        else:
            reason = "semantic"

        return round(min(1.0, final_lex), 4), round(min(1.0, specific_coverage), 4), reason

    def hybrid_query(
        self,
        query_text: str,
        embedding: list[float],
        n: int = 5,
        memory_type: Optional[str] = None,
        tags: Optional[list[str]] = None,
        source_agent: Optional[str] = None,
        include_private: bool = True,
        include_stale: bool = True,
        min_confidence: float = 0.0,
        now: Optional[datetime] = None,
        query_embedding_model: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Execute a 5-stage calibrated hybrid retrieval with pre-cached lexical IDF:
          1. Pre-mask candidates by memory_type, tags, source_agent, local_only, stale, and min_confidence
          2. Dense SIMD Cosine similarity across all matching candidates in RAM
          3. O(|Q|) Robertson-Sparck Jones Smoothed IDF lookup from `cache.stem_df` + Exact/Synonym matching
          4. Lexical Grounding Floor + Zero-Overlap Noise Suppression Gate
          5. Match-reason tagging & snippet generation
        """
        cache = self._get_vector_cache()
        with _CACHE_LOCK:
            ids_snap = list(cache.ids)
            mat = cache.matrix
            rows_snap = dict(cache.rows_by_id)
            lex_snap = dict(cache.lexical_by_id)
            stem_df_snap = dict(cache.stem_df)

        if mat is None or len(ids_snap) == 0 or n <= 0:
            return []

        q_vec = _normalize_vector(embedding, enforce_dim=False)
        if q_vec.shape[0] != mat.shape[1]:
            return []

        sims = mat @ q_vec
        lexical_map = self._fts_lexical_scores(query_text, limit=max(len(ids_snap), 50))

        norm_q = _clean_possessives(query_text)
        raw_q_tokens = [t.strip("._:-/") for t in _TOKEN_RE.findall(norm_q)]
        raw_q_tokens = [t for t in raw_q_tokens if len(t) >= 2 and t not in _LEXICAL_STOPWORDS]
        if not raw_q_tokens:
            _, raw_q_tokens, _ = _extract_search_tokens(query_text)
        q_content_tokens = list(dict.fromkeys(raw_q_tokens))
        q_stems = [_fast_stem(t) for t in q_content_tokens]
        q_synonyms = expand_query_tokens(q_content_tokens)
        q_norm_phrase = " ".join(_WORD_RE.findall(norm_q))
        q_compact = re.sub(r"[^a-z0-9]", "", q_norm_phrase)

        total_candidates = len(ids_snap)
        prefiltered_mid_set: Optional[set[str]] = None
        if total_candidates > 400:
            k = min(total_candidates, max(n * 25, 250))
            top_dense_indices = np.argpartition(-sims, k - 1)[:k]
            prefiltered_mid_set = {ids_snap[i] for i in top_dense_indices}
            prefiltered_mid_set.update(lexical_map.keys())
            with _CACHE_LOCK:
                for s in q_stems:
                    s_hits = cache.stem_to_ids.get(s)
                    if s_hits:
                        prefiltered_mid_set.update(s_hits)

        total_docs = max(1, len(rows_snap))
        has_specific_tokens = any(s not in _FRAMING_TOKENS for s in q_stems)
        idf_map: dict[str, float] = {}
        if q_stems:
            for s in set(q_stems):
                df = stem_df_snap.get(s, 0)
                raw_idf = float(np.log(1.0 + (total_docs - df + 0.5) / (df + 0.5)))
                if has_specific_tokens and s in _FRAMING_TOKENS:
                    raw_idf *= 0.12
                idf_map[s] = max(0.05, raw_idf)

        total_query_idf = sum(idf_map.get(s, 1.0) for s in q_stems) or 1.0

        type_filter = memory_type.strip().lower() if memory_type else None
        filter_tags = {t.strip().lower() for t in (tags or []) if t.strip()}
        ref_now = now if now is not None else _utc_now()

        scored_candidates: list[dict[str, Any]] = []
        max_specific_cov_in_pool = 0.0

        for idx, mid in enumerate(ids_snap):
            if prefiltered_mid_set is not None and mid not in prefiltered_mid_set:
                continue
            cached_row = rows_snap.get(mid)
            if cached_row is None:
                continue
            if type_filter and cached_row["memory_type"].lower() != type_filter:
                continue
            if source_agent and cached_row["source_agent"] != source_agent:
                continue
            if not include_private and cached_row.get("local_only"):
                continue

            parsed_tags = cached_row.get("tags_list") or []
            if filter_tags:
                mem_tags_lower = {str(t).lower() for t in parsed_tags}
                if not filter_tags.issubset(mem_tags_lower):
                    continue

            eff_conf, is_stale = _compute_row_effective_confidence(cached_row, now=ref_now)
            if not include_stale and is_stale:
                continue
            if eff_conf < min_confidence:
                continue

            is_mixed_space = False
            if query_embedding_model and cached_row.get("embedding_model"):
                row_model = str(cached_row["embedding_model"]).strip().lower()
                q_model = str(query_embedding_model).strip().lower()
                if (row_model == "hash-384" and q_model == "onnx-minilm-l6-v2") or (
                    row_model == "onnx-minilm-l6-v2" and q_model == "hash-384"
                ):
                    is_mixed_space = True

            sem_sim = 0.0 if is_mixed_space else max(0.0, min(1.0, float(sims[idx])))
            fts_score = lexical_map.get(mid, 0.0)
            lex_score, specific_cov, match_reason = self._score_candidate_lexical(
                q_content_tokens=q_content_tokens,
                q_stems=q_stems,
                q_norm_phrase=q_norm_phrase,
                q_compact=q_compact,
                doc_content=cached_row["content"],
                doc_tags=parsed_tags,
                idf_map=idf_map,
                total_query_idf=total_query_idf,
                fts_score=fts_score,
                q_synonyms=q_synonyms,
                cached_lex=lex_snap.get(mid),
            )
            if specific_cov > max_specific_cov_in_pool:
                max_specific_cov_in_pool = specific_cov

            row_dict = dict(cached_row)
            row_dict["tags_list"] = parsed_tags
            row_dict["local_only"] = bool(cached_row.get("local_only", False))
            row_dict["semantic_similarity"] = round(sem_sim, 4)
            row_dict["lexical_score"] = lex_score
            row_dict["_specific_cov"] = specific_cov
            row_dict["match_reason"] = match_reason
            row_dict["snippet"] = _make_snippet(cached_row["content"])
            scored_candidates.append(row_dict)

        if not scored_candidates:
            return []

        for item in scored_candidates:
            sem_sim = item["semantic_similarity"]
            lex_score = item["lexical_score"]
            specific_cov = item.pop("_specific_cov", 0.0)
            if specific_cov > 0.0:
                lexical_floor = 0.78 * specific_cov + 0.14 * lex_score + 0.08 * sem_sim
                boosted_sem = sem_sim + (1.0 - sem_sim) * (0.58 * lex_score + 0.25 * specific_cov)
                hybrid_sim = min(1.0, max(lexical_floor, boosted_sem))
            else:
                if max_specific_cov_in_pool >= 0.5 and sem_sim < 0.55:
                    hybrid_sim = sem_sim * 0.38
                else:
                    hybrid_sim = sem_sim
            item["similarity"] = round(hybrid_sim, 4)

        scored_candidates.sort(
            key=lambda x: (x["similarity"], x["lexical_score"], x["semantic_similarity"]),
            reverse=True,
        )
        return scored_candidates[:n]

    def discover_other_possible(
        self,
        query_text: str,
        embedding: list[float],
        exclude_ids: set[str],
        limit: int = 5,
        active_memory_type: Optional[str] = None,
        active_tags: Optional[list[str]] = None,
        include_private: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Return compact snippets of other possible memories in the namespace.
        """
        cache = self._get_vector_cache()
        _, _, rows_snap = cache.snapshot(copy_matrix=False)
        if len(rows_snap) == 0 or limit <= 0:
            return []

        suggestions: list[dict[str, Any]] = []
        seen_ids: set[str] = set(exclude_ids)

        unfiltered = self.hybrid_query(
            query_text=query_text,
            embedding=embedding,
            n=max(limit * 3, 15),
            memory_type=None,
            tags=None,
            source_agent=None,
            include_private=include_private,
        )

        had_filters = bool(active_memory_type or active_tags)
        for cand in unfiltered:
            mid = cand.get("id")
            if not mid or mid in seen_ids:
                continue
            sim = float(cand.get("similarity", 0.0))
            lex = float(cand.get("lexical_score", 0.0))
            mtype = cand.get("memory_type", "fact")
            mtags = cand.get("tags_list") or []
            full_content = cand.get("content", "")

            filtered_out_by_meta = False
            if active_memory_type and mtype.lower() != active_memory_type.strip().lower():
                filtered_out_by_meta = True
            if active_tags:
                req_tags = {t.strip().lower() for t in active_tags if t.strip()}
                if not req_tags.issubset({str(t).lower() for t in mtags}):
                    filtered_out_by_meta = True

            if had_filters and filtered_out_by_meta and (sim >= 0.35 or lex >= 0.35):
                reason = f"matched_in_other_type_or_tag (type={mtype})"
            elif sim >= 0.25 or lex > 0.0:
                reason = "related_runner_up"
            else:
                reason = "namespace_context"

            seen_ids.add(mid)
            suggestions.append(
                {
                    "id": mid,
                    "content": full_content,
                    "snippet": _make_snippet(full_content, max_len=120),
                    "memory_type": mtype,
                    "tags": mtags,
                    "confidence": float(cand.get("confidence", 1.0)),
                    "ttl_days": cand.get("ttl_days"),
                    "local_only": bool(cand.get("local_only", False)),
                    "similarity": round(sim, 4),
                    "lexical_score": round(lex, 4),
                    "reason": reason,
                }
            )
            if len(suggestions) >= limit:
                return suggestions

        remaining = [
            r
            for mid, r in rows_snap.items()
            if mid not in seen_ids and (include_private or not r.get("local_only"))
        ]
        remaining.sort(
            key=lambda x: (
                int(x.get("access_count") or 0),
                float(x.get("confidence") or 1.0),
                str(x.get("accessed_at") or ""),
            ),
            reverse=True,
        )
        for r in remaining[: limit - len(suggestions)]:
            mid = r["id"]
            full_content = r.get("content", "")
            seen_ids.add(mid)
            suggestions.append(
                {
                    "id": mid,
                    "content": full_content,
                    "snippet": _make_snippet(full_content, max_len=120),
                    "memory_type": r.get("memory_type", "fact"),
                    "tags": r.get("tags_list") or _parse_tags_json(r.get("tags")),
                    "confidence": float(r.get("confidence", 1.0)),
                    "ttl_days": r.get("ttl_days"),
                    "local_only": bool(r.get("local_only", False)),
                    "similarity": 0.0,
                    "lexical_score": 0.0,
                    "reason": "namespace_context",
                }
            )
        return suggestions

    def list_all(
        self,
        limit: int = 50,
        memory_type: Optional[str] = None,
        tag: Optional[str] = None,
        tags: Optional[list[str]] = None,
        include_private: bool = True,
        include_stale: bool = True,
        min_confidence: float = 0.0,
    ) -> list[dict[str, Any]]:
        """
        Return memories in the namespace sorted by `accessed_at DESC` directly from
        the in-memory metadata cache in O(N log N) time with zero SQLite reads.
        """
        cache = self._get_vector_cache()
        _, _, rows_snap = cache.snapshot(copy_matrix=False)
        if not rows_snap or limit <= 0:
            return []

        type_filter = memory_type.strip().lower() if memory_type else None
        req_tags: set[str] = set()
        if tag and tag.strip():
            req_tags.add(tag.strip().lower())
        if tags:
            for t in tags:
                if str(t).strip():
                    req_tags.add(str(t).strip().lower())

        now = _utc_now()
        filtered: list[dict[str, Any]] = []
        for row in rows_snap.values():
            if not include_private and row.get("local_only"):
                continue
            if type_filter and str(row.get("memory_type", "")).lower() != type_filter:
                continue
            if req_tags:
                row_tags = {
                    str(t).lower()
                    for t in (row.get("tags_list") or _parse_tags_json(row.get("tags")))
                }
                if not req_tags.issubset(row_tags):
                    continue
            if not include_stale or min_confidence > 0.0:
                eff_conf, is_stale = _compute_row_effective_confidence(row, now=now)
                if not include_stale and is_stale:
                    continue
                if eff_conf < min_confidence:
                    continue
            filtered.append(dict(row))

        filtered.sort(
            key=lambda r: (str(r.get("accessed_at") or ""), str(r.get("created_at") or "")),
            reverse=True,
        )
        return filtered[:limit]

    def get_exportable_memories(self, include_embeddings: bool = True) -> list[dict[str, Any]]:
        """
        Return all non-private (`local_only = 0`) memories in the namespace for `io.export_namespace()`.
        """
        with _CACHE_LOCK:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                if include_embeddings:
                    rows = conn.execute(
                        f"SELECT {_MEMORY_COLUMNS}, embedding FROM memories "
                        "WHERE namespace = ? AND (local_only = 0 OR local_only IS NULL) "
                        "ORDER BY created_at ASC",
                        (self.namespace,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT {_MEMORY_COLUMNS} FROM memories "
                        "WHERE namespace = ? AND (local_only = 0 OR local_only IS NULL) "
                        "ORDER BY created_at ASC",
                        (self.namespace,),
                    ).fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            d = {col: r[col] for col in r.keys() if col != "embedding"}
            d["local_only"] = bool(d.get("local_only"))
            d["tags_list"] = _parse_tags_json(d.get("tags"))
            if include_embeddings:
                blob = r["embedding"] if "embedding" in r.keys() else None
                if blob:
                    arr = _blob_to_vector(blob)
                    d["embedding"] = arr.tolist() if arr.size == EMBEDDING_DIM else None
                else:
                    d["embedding"] = None
            results.append(d)
        return results

    def delete(self, memory_id: str) -> bool:
        """
        Delete a memory by ID (or resolved ID), removing its FTS5 entry by `rowid` in O(log N)
        time, scrubbing any contradiction records referencing it, and updating the RAM cache in-place.
        """
        clean_id = (memory_id or "").strip()
        if not clean_id:
            return False

        purged_content: Optional[str] = None
        deleted = False

        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            if synced_cache is not None and clean_id in synced_cache.rows_by_id:
                purged_content = synced_cache.rows_by_id[clean_id].get("content")

            with self._connect() as conn:
                row = conn.execute(
                    "SELECT rowid, content FROM memories WHERE id = ? AND namespace = ?",
                    (clean_id, self.namespace),
                ).fetchone()
                if row is not None:
                    rowid = int(row[0])
                    if purged_content is None:
                        purged_content = str(row[1] or "")
                    cur = conn.execute(
                        "DELETE FROM memories WHERE rowid = ? AND namespace = ?",
                        (rowid, self.namespace),
                    )
                    deleted = cur.rowcount > 0
                    self._delete_fts_row(conn, clean_id, rowid=rowid)
                    conn.execute(
                        "DELETE FROM contradictions WHERE namespace = ? AND (new_memory_id = ? OR existing_memory_id = ?)",
                        (self.namespace, clean_id, clean_id),
                    )
                    if purged_content and purged_content.strip():
                        conn.execute(
                            "DELETE FROM queries WHERE namespace = ? AND query_text = ?",
                            (self.namespace, purged_content.strip()),
                        )
                    conn.commit()
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.OperationalError:
                        pass

            if deleted:
                if synced_cache is not None:
                    synced_cache.delete_vector(clean_id)
                    self._refresh_stamps_after_write_locked(synced_cache)
                else:
                    self._invalidate_cache()

        if deleted:
            purge_memory_from_audit(
                namespace=self.namespace,
                memory_id=clean_id,
                content=purged_content,
            )
        return deleted

    def count(self) -> int:
        """Return the total number of memories in this namespace in O(1) time from the vector cache."""
        cache = self._get_vector_cache()
        with _CACHE_LOCK:
            return len(cache.all_ids)

    def clear_namespace(self) -> None:
        """Delete all memories, FTS entries, queries, and contradictions in this namespace."""
        with _CACHE_LOCK:
            with self._connect() as conn:
                conn.execute("DELETE FROM memories WHERE namespace = ?", (self.namespace,))
                if self._fts_enabled:
                    try:
                        conn.execute("DELETE FROM memories_fts")
                    except sqlite3.OperationalError:
                        pass
                conn.execute("DELETE FROM queries WHERE namespace = ?", (self.namespace,))
                conn.execute("DELETE FROM contradictions WHERE namespace = ?", (self.namespace,))
                conn.commit()
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("VACUUM")
                except sqlite3.OperationalError:
                    pass
            self._invalidate_cache()

    # --- Telemetry, Query Logging & Contradiction Management ---

    def increment_access(self, memory_id: str) -> None:
        self.increment_access_batch([memory_id])

    def increment_access_batch(self, memory_ids: list[str]) -> None:
        """Increment `access_count` and update `accessed_at` using copy-on-write cache replacement."""
        valid_ids = [mid for mid in dict.fromkeys(memory_ids) if mid]
        if not valid_ids:
            return
        now = _utc_now_iso()
        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            placeholders = ",".join("?" for _ in valid_ids)
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE memories
                    SET access_count = access_count + 1,
                        accessed_at = ?
                    WHERE namespace = ? AND id IN ({placeholders})
                    """,
                    [now, self.namespace, *valid_ids],
                )
                conn.commit()
            if synced_cache is not None:
                for mid in valid_ids:
                    rm = synced_cache.rows_by_id.get(mid)
                    if rm is not None:
                        synced_cache.rows_by_id[mid] = {
                            **rm,
                            "access_count": int(rm.get("access_count") or 0) + 1,
                            "accessed_at": now,
                        }
                self._refresh_stamps_after_write_locked(synced_cache)
            else:
                self._invalidate_cache()

    def log_query(
        self,
        query_text: str,
        agent_id: str,
        best_similarity: Optional[float],
        result_count: int,
    ) -> None:
        self.record_recall_telemetry(
            memory_ids=[],
            query_text=query_text,
            agent_id=agent_id,
            best_similarity=best_similarity,
            result_count=result_count,
        )

    def record_recall_telemetry(
        self,
        memory_ids: list[str],
        query_text: str,
        agent_id: str,
        best_similarity: Optional[float],
        result_count: int,
    ) -> None:
        """
        Atomically increment `access_count` on recalled memories, log the query in `queries`,
        and enforce bounded `queries` table retention without invalidating the in-memory vector cache.
        """
        valid_ids = [mid for mid in dict.fromkeys(memory_ids) if mid]
        now = _utc_now_iso()
        qid = str(uuid.uuid4())
        clean_query = (query_text or "").strip()[:512]

        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            q_writes = _QUERY_WRITE_COUNTS.get(self.db_path, 0) + 1
            _QUERY_WRITE_COUNTS[self.db_path] = q_writes

            with self._connect() as conn:
                if valid_ids:
                    placeholders = ",".join("?" for _ in valid_ids)
                    conn.execute(
                        f"""
                        UPDATE memories
                        SET access_count = access_count + 1,
                            accessed_at = ?
                        WHERE namespace = ? AND id IN ({placeholders})
                        """,
                        [now, self.namespace, *valid_ids],
                    )
                if clean_query:
                    conn.execute(
                        """
                        INSERT INTO queries
                        (id, namespace, query_text, agent_id, best_similarity, result_count, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            qid,
                            self.namespace,
                            clean_query,
                            agent_id or "unknown",
                            float(best_similarity) if best_similarity is not None else None,
                            int(result_count),
                            now,
                        ),
                    )
                    if q_writes % 100 == 0:
                        _prune_queries_table_locked(conn, self.namespace, _max_query_rows())
                conn.commit()

            if synced_cache is not None:
                for mid in valid_ids:
                    rm = synced_cache.rows_by_id.get(mid)
                    if rm is not None:
                        synced_cache.rows_by_id[mid] = {
                            **rm,
                            "access_count": int(rm.get("access_count") or 0) + 1,
                            "accessed_at": now,
                        }
                self._refresh_stamps_after_write_locked(synced_cache)
            else:
                self._invalidate_cache()

    def log_contradiction(
        self,
        new_memory_id: str,
        new_content: str,
        existing_memory_id: str,
        existing_content: str,
        similarity: float,
        local_only: bool = False,
        existing_local_only: Optional[bool] = None,
    ) -> str:
        """
        Record a contradiction between a new memory and an existing memory.
        Always redacts `new_content` or `existing_content` if either memory is `local_only=True`,
        even when the in-memory vector cache is cold.
        """
        cid = str(uuid.uuid4())
        now = _utc_now_iso()

        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            existing_is_private = bool(existing_local_only)
            if existing_local_only is None and existing_memory_id:
                if synced_cache is not None and existing_memory_id in synced_cache.rows_by_id:
                    existing_is_private = bool(
                        synced_cache.rows_by_id[existing_memory_id].get("local_only")
                    )
                else:
                    with self._connect() as conn:
                        row = conn.execute(
                            "SELECT local_only FROM memories WHERE id = ? AND namespace = ?",
                            (existing_memory_id, self.namespace),
                        ).fetchone()
                        if row is not None:
                            existing_is_private = bool(row[0])

            safe_new = REDACTED_CONTENT_PLACEHOLDER if local_only else new_content
            safe_existing = (
                REDACTED_CONTENT_PLACEHOLDER if existing_is_private else existing_content
            )

            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO contradictions
                    (id, namespace, new_memory_id, new_content, existing_memory_id,
                     existing_content, similarity, detected_at, resolved)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        cid,
                        self.namespace,
                        validate_memory_id(new_memory_id),
                        safe_new,
                        validate_memory_id(existing_memory_id) if existing_memory_id else "",
                        safe_existing,
                        float(similarity),
                        now,
                    ),
                )
                conn.commit()
            if synced_cache is not None:
                self._refresh_stamps_after_write_locked(synced_cache)
        return cid

    def get_contradictions(self, resolved: bool = False) -> list[dict[str, Any]]:
        """Return unresolved (or resolved) contradictions for this namespace."""
        res_flag = 1 if resolved else 0
        with _CACHE_LOCK:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT id, namespace, new_memory_id, new_content,
                           existing_memory_id, existing_content, similarity,
                           detected_at, resolved
                    FROM contradictions
                    WHERE namespace = ? AND resolved = ?
                    ORDER BY detected_at DESC
                    """,
                    (self.namespace, res_flag),
                ).fetchall()
                return [dict(r) for r in rows]

    def resolve_contradiction(self, contradiction_id: str) -> bool:
        """
        Mark a contradiction as resolved by full UUID or 4+ character short ID prefix.
        Raises ValueError if the contradiction ID does not exist or is ambiguous.
        """
        clean_pref = (contradiction_id or "").strip().lower()
        if not clean_pref:
            raise ValueError("contradiction_id cannot be empty")

        with _CACHE_LOCK:
            synced_cache = self._is_cache_synced_locked()
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id FROM contradictions WHERE namespace = ?",
                    (self.namespace,),
                ).fetchall()
                all_cids = [str(r[0]) for r in rows]

                target_id: Optional[str] = None
                for cid in all_cids:
                    if cid.lower() == clean_pref:
                        target_id = cid
                        break

                if target_id is None:
                    if len(clean_pref) < 4:
                        raise ValueError(
                            f"Contradiction ID '{contradiction_id}' not found in namespace '{self.namespace}'."
                        )
                    matches = [cid for cid in all_cids if cid.lower().startswith(clean_pref)]
                    if len(matches) == 1:
                        target_id = matches[0]
                    elif len(matches) > 1:
                        raise ValueError(
                            f"Ambiguous contradiction ID prefix '{contradiction_id}' matches {matches[:3]}."
                        )
                    else:
                        raise ValueError(
                            f"Contradiction ID '{contradiction_id}' not found in namespace '{self.namespace}'."
                        )

                cur = conn.execute(
                    "UPDATE contradictions SET resolved = 1 WHERE id = ? AND namespace = ?",
                    (target_id, self.namespace),
                )
                conn.commit()
                updated = cur.rowcount > 0

            if synced_cache is not None:
                self._refresh_stamps_after_write_locked(synced_cache)
            return updated

    def get_patterns(self) -> dict[str, Any]:
        """
        Analyze query logs and cached memories to return:
          - top_topics: most frequently searched queries
          - memory_gaps: queries searched >= 2 times with avg_similarity < 0.45
          - unused_memories: memories created > 7 days ago with access_count == 0
          - stale_memories: memories where `_compute_row_effective_confidence` is stale
        """
        with _CACHE_LOCK:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                top_rows = conn.execute(
                    """
                    SELECT LOWER(TRIM(query_text)) AS query_text,
                           COUNT(*) AS query_count,
                           COALESCE(AVG(best_similarity), 0.0) AS avg_similarity
                    FROM queries
                    WHERE namespace = ? AND TRIM(query_text) != ''
                    GROUP BY LOWER(TRIM(query_text))
                    ORDER BY query_count DESC, avg_similarity DESC
                    LIMIT 15
                    """,
                    (self.namespace,),
                ).fetchall()

                gap_rows = conn.execute(
                    """
                    SELECT LOWER(TRIM(query_text)) AS query_text,
                           COUNT(*) AS query_count,
                           COALESCE(AVG(best_similarity), 0.0) AS avg_similarity
                    FROM queries
                    WHERE namespace = ? AND TRIM(query_text) != ''
                    GROUP BY LOWER(TRIM(query_text))
                    HAVING COUNT(*) >= 1 AND COALESCE(AVG(best_similarity), 0.0) < 0.45
                    ORDER BY query_count DESC, avg_similarity ASC
                    LIMIT 15
                    """,
                    (self.namespace,),
                ).fetchall()

        top_topics = [
            {
                "query_text": r["query_text"],
                "query_count": int(r["query_count"]),
                "avg_similarity": round(float(r["avg_similarity"]), 4),
            }
            for r in top_rows
        ]
        memory_gaps = [
            {
                "query_text": r["query_text"],
                "query_count": int(r["query_count"]),
                "avg_similarity": round(float(r["avg_similarity"]), 4),
            }
            for r in gap_rows
        ]

        cache = self._get_vector_cache()
        _, _, rows_snap = cache.snapshot(copy_matrix=False)
        now = _utc_now()
        seven_days_ago = now - timedelta(days=7)

        unused_memories: list[dict[str, Any]] = []
        stale_memories: list[dict[str, Any]] = []

        for row in rows_snap.values():
            eff_conf, is_stale = _compute_row_effective_confidence(row, now=now)
            row_copy = dict(row)
            row_copy["effective_confidence"] = eff_conf
            row_copy["is_stale"] = is_stale
            if is_stale:
                stale_memories.append(row_copy)

            if int(row.get("access_count") or 0) == 0:
                created_dt = _parse_iso_utc(row.get("created_at"))
                if created_dt is not None and created_dt <= seven_days_ago:
                    unused_memories.append(row_copy)

        unused_memories.sort(key=lambda r: str(r.get("created_at") or ""))
        stale_memories.sort(key=lambda r: float(r.get("effective_confidence", 0.0)))

        return {
            "top_topics": top_topics,
            "memory_gaps": memory_gaps,
            "unused_memories": unused_memories[:25],
            "stale_memories": stale_memories[:25],
        }