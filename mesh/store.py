"""
mesh/store.py — High-performance unified persistence & hybrid search engine for Mesh.

Storage architecture:
  - SQLite (WAL mode) single-source-of-truth per namespace (`~/.mesh/mesh_<namespace>.db`)
  - Strict namespace (`validate_namespace`), memory ID (`validate_memory_id`), and symlink (`reject_symlink`) validation
  - Lowest-layer ingestion guard (`guard_ingest` & `guard_tags`) enforced on every write path
  - First-class provenance (`trust_tier`: 'user' | 'agent' | 'log_captured'), quarantine (`quarantined`),
    and secret locator (`secret_ref`) schema columns with automatic migration
  - Transparent at-rest encryption (`mesh.crypto`) for content, tags, embeddings, queries, and contradictions
  - FTS5 leakage defense: `local_only=True` and encrypted rows are never indexed into on-disk `memories_fts`
    shadow tables; hybrid search uses the in-memory decrypted lexical cache instead
  - Cross-process + in-process re-entrant `namespace_write_lock` (`~/.mesh/.mesh_<ns>.lock`, mode 0600)
"""
from __future__ import annotations

import atexit
import contextlib
import json
import logging
import functools
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import uuid
import warnings
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Literal, Optional, Sequence

if TYPE_CHECKING:
    import numpy as np
else:
    class _LazyNumPy:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def __getattr__(self, name: str) -> Any:
            with self._lock:
                import numpy as _np

                globals()["np"] = _np
                return getattr(_np, name)

    np = _LazyNumPy()

from .audit import (
    REDACTED_CONTENT_PLACEHOLDER,
    cached_resolved_mesh_dir,
    clear_audit_log,
    ensure_secure_mesh_dir,
    evict_audit_cache,
    get_mesh_dir,
    purge_memory_from_audit,
    reject_symlink,
    scrub_memory_update_from_audit,
    secure_file_permissions,
    validate_namespace,
)
from .crypto import (
    decrypt_blob_if_encrypted,
    decrypt_text_if_encrypted,
    encrypt_blob_for_storage,
    encrypt_text_for_storage,
    is_encrypted_blob,
    is_encrypted_text,
    is_encryption_enabled,
)
from .audit import _path_recheck_ttl
from .envutil import env_int
from .guards import guard_ingest, guard_tags
from .secrets import is_credential, normalize_secret_ref, redact_secrets_local

EMBEDDING_DIM = 384
STALE_THRESHOLD = 0.3
GAP_MIN_QUERIES = 2
DEFAULT_MAX_QUERY_ROWS = 10000
DEFAULT_MAX_POOL_SIZE = 64
DEFAULT_MAX_VECTOR_CACHE_SIZE = 64
DEFAULT_MAX_NAMESPACES = 256

_MAX_POOL_SIZE = DEFAULT_MAX_POOL_SIZE
_MAX_VECTOR_CACHE_SIZE = DEFAULT_MAX_VECTOR_CACHE_SIZE
_MAX_NAMESPACES = DEFAULT_MAX_NAMESPACES

logger = logging.getLogger(__name__)

# Pre-mask candidate cut threshold (Phase 2.4)
PREMASK_SKIP_CUT_THRESHOLD = 400
PREMASK_TOP_K_MULTIPLIER = 25

_VALID_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REDACTED_PLACEHOLDER_RE = re.compile(
    r"\[REDACTED_(?:SECRET|API_KEY|TOKEN|PASSWORD|JWT|PRIVATE_KEY|content|local_only)[^\]]*\]",
    re.IGNORECASE,
)

VALID_TRUST_TIERS: frozenset[str] = frozenset({"user", "agent", "log_captured"})

# Columns returned by metadata queries (explicitly excludes the raw binary `embedding` BLOB
# so row dictionaries remain 100% JSON-serializable across MCP, HTTP, CLI, and Dashboard).
_MEMORY_COLUMNS = (
    "id, namespace, content, confidence, memory_type, source_agent, "
    "tags, created_at, updated_at, accessed_at, access_count, ttl_days, "
    "decay_start_at, local_only, embedding_model, trust_tier, quarantined, secret_ref"
)

# Whitelisted fields returned in `get_patterns()` for `unused_memories` and `stale_memories` (Phase 1.1)
_PATTERNS_MEMORY_WHITELIST: tuple[str, ...] = (
    "id",
    "content",
    "memory_type",
    "confidence",
    "effective_confidence",
    "is_stale",
    "tags",
    "source_agent",
    "created_at",
    "accessed_at",
    "access_count",
    "ttl_days",
)

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_./:-]{2,}")
SCHEMA_VERSION: str = "3"
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

_FRAMING_TOKENS = {"user", "users", "agent", "agents", "assistant", "human", "person", "system"}

# Composite file stamp: (db_mtime_ns, db_size, wal_mtime_ns, wal_size)
DbStamp = tuple[int, int, int, int]


class NamespaceNotFoundError(ValueError):
    """
    Raised when `MeshStore(namespace, create=False)` or `Mesh(namespace, create=False)`
    is instantiated for a namespace whose SQLite database file does not exist on disk.
    Inherits from `ValueError` for backwards compatibility with existing callers.
    """


class NamespaceLimitExceededError(ValueError):
    """
    Raised when creating a new namespace would exceed the configured namespace cap
    (`MESH_MAX_NAMESPACES`, default 256).
    """


def _normalize_trust_tier(raw_tier: Optional[str], default: str = "user") -> str:
    if not raw_tier:
        return default
    clean = str(raw_tier).strip().lower()
    return clean if clean in VALID_TRUST_TIERS else default


def _parse_secret_ref_json(raw_ref: Any) -> Optional[dict[str, str]]:
    if not raw_ref:
        return None
    try:
        plain = decrypt_text_if_encrypted(raw_ref) if isinstance(raw_ref, str) else raw_ref
        if isinstance(plain, dict):
            return {str(k): str(v) for k, v in plain.items()}
        if isinstance(plain, str) and plain.strip():
            parsed = json.loads(plain)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
    except Exception:
        pass
    return None


def _get_max_pool_size() -> int:
    if os.environ.get("MESH_MAX_POOL_SIZE"):
        return env_int("MESH_MAX_POOL_SIZE", _MAX_POOL_SIZE, minimum=2)
    if os.environ.get("MESH_MAX_OPEN_NAMESPACES"):
        return env_int("MESH_MAX_OPEN_NAMESPACES", _MAX_POOL_SIZE, minimum=2)
    return max(2, int(_MAX_POOL_SIZE))


def _get_max_vector_cache_size() -> int:
    if os.environ.get("MESH_MAX_VECTOR_CACHE_SIZE"):
        return env_int("MESH_MAX_VECTOR_CACHE_SIZE", _MAX_VECTOR_CACHE_SIZE, minimum=2)
    if os.environ.get("MESH_MAX_OPEN_NAMESPACES"):
        return env_int("MESH_MAX_OPEN_NAMESPACES", _MAX_VECTOR_CACHE_SIZE, minimum=2)
    return max(2, int(_MAX_VECTOR_CACHE_SIZE))


def _get_max_namespaces() -> int:
    if os.environ.get("MESH_MAX_NAMESPACES"):
        return env_int("MESH_MAX_NAMESPACES", _MAX_NAMESPACES, minimum=1)
    return max(1, int(_MAX_NAMESPACES))


def _check_namespace_creation_limit(base_dir: Path, namespace: str) -> None:
    """Enforce a cap on total tracked namespaces before creating a new `.db` file."""
    target_db = (base_dir / f"mesh_{namespace}.db").resolve()
    if target_db.exists():
        return
    max_ns = _get_max_namespaces()
    if not base_dir.exists():
        return
    existing_count = 0
    for p in base_dir.glob("mesh_*.db"):
        if p.is_file() and not p.name.endswith(("-wal", "-shm")):
            existing_count += 1
            if existing_count >= max_ns:
                raise NamespaceLimitExceededError(
                    f"Maximum number of tracked namespaces ({max_ns}) exceeded; "
                    f"refusing to create namespace '{namespace}'."
                )


def _evict_external_namespace_caches(namespace: str) -> None:
    """Evict cached state for `namespace` in audit, write-lock, context_builder, HTTP, MCP, and Dashboard."""
    evict_namespace_write_lock(namespace)
    evict_audit_cache(namespace)

    ctx_mod = sys.modules.get("mesh.context_builder")
    if ctx_mod is not None and hasattr(ctx_mod, "evict_context_mesh_cache"):
        try:
            ctx_mod.evict_context_mesh_cache(namespace)
        except Exception:
            pass

    http_mod = sys.modules.get("mesh.http_server")
    if http_mod is not None and hasattr(http_mod, "evict_http_mesh_cache"):
        try:
            http_mod.evict_http_mesh_cache(namespace)
        except Exception:
            pass

    mcp_mod = sys.modules.get("mesh.mcp_server")
    if mcp_mod is not None and hasattr(mcp_mod, "evict_mcp_mesh_cache"):
        try:
            mcp_mod.evict_mcp_mesh_cache(namespace)
        except Exception:
            pass

    dash_mod = sys.modules.get("mesh.dashboard")
    if dash_mod is not None and hasattr(dash_mod, "evict_dashboard_mesh_cache"):
        try:
            dash_mod.evict_dashboard_mesh_cache(namespace)
        except Exception:
            pass


def _finalize_evicted_resources(
    evicted: list[tuple[Path, Optional["_PooledConnection"], Optional[str]]]
) -> None:
    """
    Flush telemetry, close pooled connections, and evict upper-layer caches for
    LRU-evicted namespaces AFTER releasing `_CACHE_LOCK`.
    """
    for db_path, conn, ns in evicted:
        try:
            _flush_telemetry_for_db_path(db_path)
        except Exception:
            pass
        if conn is not None:
            conn.close_pooled()
        with _CACHE_LOCK:
            _QUERY_WRITE_COUNTS.pop(db_path, None)
            dict.pop(_INITIALIZED_DBS, db_path, None)
            extra_conn = _CONNECTION_POOL.pop(db_path, None)
            if extra_conn is not None:
                extra_conn.close_pooled()
            _evict_vector_cache_for_db_path(db_path)
        if ns:
            _evict_external_namespace_caches(ns)


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


# --- Mixed Embedding-Space Guards (Phase 2.1) ---


def _is_mixed_space(row_model: Optional[str], query_model: Optional[str]) -> bool:
    rm = (row_model or "").strip().lower()
    qm = (query_model or "").strip().lower()
    if not rm or not qm:
        return False
    if rm in ("unknown", "needs-reembed") or qm in ("unknown", "needs-reembed"):
        return False
    return rm != qm


def _is_same_known_space(row_model: Optional[str], query_model: Optional[str]) -> bool:
    rm = (row_model or "").strip().lower()
    qm = (query_model or "").strip().lower()
    if not rm or not qm:
        return False
    if rm in ("unknown", "needs-reembed") or qm in ("unknown", "needs-reembed"):
        return False
    return rm == qm


# --- Cross-Process + In-Process Re-Entrant Namespace Write Lock (Phase 2.2) ---


class _NamespaceLockState:
    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.rlock = threading.RLock()
        self.depth = 0
        self.fd: Optional[int] = None
        self.lock_path: Optional[Path] = None


_NS_LOCK_REGISTRY_MUTEX = threading.Lock()
_NS_LOCK_REGISTRY: dict[tuple[Path, str], _NamespaceLockState] = {}
_OPEN_LOCK_FDS: dict[Path, int] = {}
_OPEN_LOCK_FDS_MUTEX = threading.Lock()


def _get_ns_lock_path(namespace: str) -> Path:
    clean_ns = validate_namespace(namespace)
    ensure_secure_mesh_dir()
    base_dir = cached_resolved_mesh_dir()
    lock_path = base_dir / f".mesh_{clean_ns}.lock"
    reject_symlink(lock_path)
    return lock_path


def _acquire_os_file_lock(lock_path: Path, timeout: float) -> int:
    """
    Acquire an exclusive cross-process advisory file lock on `lock_path`.
    Reuses open file descriptors across calls to eliminate OS open/close overhead.
    """
    with _OPEN_LOCK_FDS_MUTEX:
        fd = _OPEN_LOCK_FDS.get(lock_path)
        if fd is None:
            ensure_secure_mesh_dir(lock_path.parent)
            reject_symlink(lock_path)
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_NOFOLLOW", 0)
            mode = stat.S_IRUSR | stat.S_IWUSR if os.name != "nt" else 0o600
            fd = os.open(str(lock_path), flags, mode)
            if os.name != "nt":
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    try:
                        fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
                    except (AttributeError, OSError):
                        pass
            else:
                try:
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\x00")
                except OSError:
                    pass
            _OPEN_LOCK_FDS[lock_path] = fd

    deadline = time.monotonic() + max(0.05, float(timeout))
    sleep_s = 0.002
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                flock = getattr(fcntl, "flock", None)
                if flock is not None:
                    lock_ex = getattr(fcntl, "LOCK_EX", 2)
                    lock_nb = getattr(fcntl, "LOCK_NB", 4)
                    flock(fd, lock_ex | lock_nb)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out after {timeout:.1f}s waiting for namespace write lock '{lock_path.name}'."
                )
            time.sleep(sleep_s)
            sleep_s = min(0.05, sleep_s * 1.4)


def _release_os_file_lock(fd: int, close_fd: bool = False) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            flock = getattr(fcntl, "flock", None)
            if flock is not None:
                lock_un = getattr(fcntl, "LOCK_UN", 8)
                try:
                    flock(fd, lock_un)
                except OSError:
                    pass
    finally:
        if close_fd:
            try:
                os.close(fd)
            except OSError:
                pass


@contextlib.contextmanager
def namespace_write_lock(namespace: str, timeout: float = 15.0) -> Iterator[None]:
    """
    Combined per-namespace in-process `threading.RLock` + cross-process advisory file lock
    on `~/.mesh/.mesh_<ns>.lock` (mode 0600 on POSIX).
    """
    clean_ns = validate_namespace(namespace)
    mesh_dir = cached_resolved_mesh_dir()
    key = (mesh_dir, clean_ns)
    with _NS_LOCK_REGISTRY_MUTEX:
        state = _NS_LOCK_REGISTRY.get(key)
        if state is None:
            state = _NamespaceLockState(clean_ns)
            _NS_LOCK_REGISTRY[key] = state

    wait_timeout = max(0.05, float(timeout))
    deadline = time.monotonic() + wait_timeout
    if not state.rlock.acquire(timeout=wait_timeout):
        raise TimeoutError(
            f"Timed out after {timeout:.1f}s waiting for in-process namespace write lock '{clean_ns}'."
        )
    try:
        if state.depth == 0:
            remaining = max(0.05, deadline - time.monotonic())
            lock_path = _get_ns_lock_path(clean_ns)
            state.fd = _acquire_os_file_lock(lock_path, timeout=remaining)
            state.lock_path = lock_path
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0 and state.fd is not None:
                fd_to_release = state.fd
                state.fd = None
                _release_os_file_lock(fd_to_release, close_fd=False)
    finally:
        state.rlock.release()


def evict_namespace_write_lock(namespace: str) -> None:
    """Evict the in-process lock registry entry for a namespace."""
    try:
        clean_ns = validate_namespace(namespace)
    except ValueError:
        return
    mesh_dir = get_mesh_dir().resolve()
    key = (mesh_dir, clean_ns)
    with _NS_LOCK_REGISTRY_MUTEX:
        state = _NS_LOCK_REGISTRY.get(key)
        if state is None:
            return
        acquired = state.rlock.acquire(timeout=0.05)
        if not acquired:
            return
        try:
            if state.depth > 0:
                return
            _NS_LOCK_REGISTRY.pop(key, None)
            if state.fd is not None:
                _release_os_file_lock(state.fd, close_fd=True)
                state.fd = None
            if state.lock_path is not None:
                with _OPEN_LOCK_FDS_MUTEX:
                    open_fd = _OPEN_LOCK_FDS.pop(state.lock_path, None)
                    if open_fd is not None:
                        try:
                            os.close(open_fd)
                        except OSError:
                            pass
        finally:
            state.rlock.release()


@atexit.register
def _cleanup_open_lock_fds() -> None:
    with _OPEN_LOCK_FDS_MUTEX:
        for fd in _OPEN_LOCK_FDS.values():
            try:
                os.close(fd)
            except OSError:
                pass
        _OPEN_LOCK_FDS.clear()



def _clean_possessives(text: str) -> str:
    s = text.lower().replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    return re.sub(r"'s\b", "", s)


def _fast_stem(w: str) -> str:
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


# --- Bidirectional Tight Domain Synonym & Compound Expansion Engine (Phase 2.5) ---
_SYNONYM_GROUPS: list[set[str]] = [
    {"color", "colour", "hue", "shade", "tint"},
    {"blue", "navy", "azure", "cyan", "cobalt", "teal", "sapphire", "indigo"},
    {"red", "crimson", "scarlet", "maroon", "ruby", "vermilion", "burgundy"},
    {"green", "emerald", "lime", "olive", "jade", "mint"},
    {"yellow", "gold", "golden", "amber", "lemon", "mustard"},
    {"purple", "violet", "lavender", "magenta", "plum", "lilac"},
    {"dark", "night", "black", "dim", "obsidian", "charcoal"},
    {"light", "bright", "day", "white", "ivory", "pale"},
    {"pen", "pens", "biro", "ballpoint", "marker", "stylus", "highlighter", "stationery", "pencil"},
    {"anime", "manga", "animation", "otaku", "cartoon"},
    {"favorite", "favourite", "preferred", "beloved", "likes", "loves"},
    {"laptop", "notebook", "macbook", "computer", "pc", "workstation", "machine"},
    {"phone", "smartphone", "mobile", "cellphone", "iphone", "android", "handset"},
    {"db", "database", "databases", "datastore"},
    {"postgres", "postgresql", "psql", "pg", "pgbouncer"},
    {"redis", "valkey", "memcached", "inmemory"},
    {"sqlite", "sqlite3"},
    {"mongo", "mongodb", "documentdb"},
    {"auth", "authentication", "authorization", "login", "signin", "sso", "oauth", "oidc", "rbac"},
    {"jwt", "token", "tokens", "bearer", "session", "cookie", "cookies"},
    {"password", "passphrase", "secret", "secrets", "credential", "credentials", "apikey", "vault"},
    {"prod", "production"},
    {"stage", "staging", "preprod", "uat"},
    {"dev", "development", "localhost", "loopback"},
    {"deploy", "deployment", "rollout"},
    {"url", "uri", "link", "hyperlink"},
    {"ip", "address", "ipv4", "ipv6"},
    {"host", "hostname"},
    {"k8s", "kubernetes", "kubectl"},
    {"bug", "bugs", "error", "errors", "exception", "crash", "failure", "fault", "defect", "incident"},
    {"fix", "fixed", "patch", "resolve", "resolved", "workaround", "remedy", "repair"},
    {"config", "configuration", "settings", "env", "environment"},
    {"limit", "limits", "ratelimit", "quota", "throttle", "throttling"},
    {"fast", "latency", "performance", "throughput", "bottleneck", "benchmark"},
    {"test", "tests", "testing", "pytest", "unittest"},
]

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
    clean = " ".join(str(content or "").strip().split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3].rstrip() + "..."


def _build_row_lexical_entry(content: str, tags_list: list[str]) -> dict[str, Any]:
    doc_norm = _clean_possessives(content)
    doc_compact = re.sub(r"[^a-z0-9]", "", doc_norm)
    _, doc_tokens, doc_stems = _extract_search_tokens(content)
    tag_tokens = {str(t).strip().lower() for t in tags_list if str(t).strip()}
    tag_stems = {_fast_stem(t) for t in tag_tokens}
    tag_compacts = {re.sub(r"[^a-z0-9]", "", t) for t in tag_tokens if len(t) >= 2}
    doc_compacts = {re.sub(r"[^a-z0-9]", "", t) for t in doc_tokens if len(t) >= 2}

    all_doc_tokens = set(doc_tokens) | tag_tokens | doc_compacts | tag_compacts
    all_doc_stems = doc_stems | tag_stems | {_fast_stem(t) for t in doc_compacts}
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
    emb_model = row_meta.get("embedding_model")
    if emb_model in ("hash-384", "needs-reembed", "unknown", None):
        return True
    if vec is None or vec.size != EMBEDDING_DIM:
        return True
    if np.all(vec[368:] == 0.0) and int(np.count_nonzero(vec == 0.0)) >= 16:
        return True
    return False


class _PooledConnection(sqlite3.Connection):
    """A pooled SQLite connection serialized across threads.

    `check_same_thread=False` disables sqlite3's thread check but does not make
    the connection safe to share: the handle, statement state and row factory
    are all mutable shared state. A per-connection RLock therefore serializes
    every ``with store._connect()`` block.

    Checkouts are reference-counted so LRU eviction can *retire* a connection
    without closing it while another thread is still using it. The physical
    close happens when the last user checks in.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mesh_lock = threading.RLock()
        self._mesh_state_lock = threading.Lock()
        self._mesh_users = 0
        self._mesh_retired = False
        self._mesh_closed = False

    def __enter__(self) -> "_PooledConnection":
        self._mesh_lock.acquire()
        try:
            with self._mesh_state_lock:
                closed = self._mesh_closed
            if closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            super().__enter__()
            return self
        except BaseException:
            self._mesh_lock.release()
            raise

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Literal[False]:
        try:
            super().__exit__(exc_type, exc_val, exc_tb)
            return False
        finally:
            self._mesh_lock.release()

    def close(self) -> None:
        # Pooled connections are closed through the checkout/retire lifecycle.
        pass

    def checkout(self) -> None:
        """Reserve this connection for one caller.

        Must be called while ``_CACHE_LOCK`` is held so LRU eviction cannot
        retire the connection between lookup and checkout.
        """
        with self._mesh_state_lock:
            if self._mesh_closed or self._mesh_retired:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            self._mesh_users += 1

    def checkin(self) -> None:
        should_close = False
        with self._mesh_state_lock:
            if self._mesh_users > 0:
                self._mesh_users -= 1
            if self._mesh_retired and self._mesh_users == 0 and not self._mesh_closed:
                self._mesh_closed = True
                should_close = True
        if should_close:
            self._close_physical()

    def retire(self) -> None:
        """Mark for closure; close immediately only when nobody is using it.

        Never waits for an in-flight user: eviction must not block on a thread
        that is still inside ``with store._connect()``.
        """
        should_close = False
        with self._mesh_state_lock:
            self._mesh_retired = True
            if self._mesh_users == 0 and not self._mesh_closed:
                self._mesh_closed = True
                should_close = True
        if should_close:
            self._close_physical()

    def close_pooled(self) -> None:
        self.retire()

    def _close_physical(self) -> None:
        try:
            super().close()
        except Exception:
            pass


class _ConnectionCheckout:
    """Context manager returned by ``MeshStore._connect()``.

    The reference is taken inside ``_connect()`` while ``_CACHE_LOCK`` is held,
    so an evicting thread cannot close the connection between checkout and
    ``__enter__``.
    """

    __slots__ = ("_conn", "_checked_in")

    def __init__(self, conn: _PooledConnection) -> None:
        self._conn = conn
        self._checked_in = False

    def __enter__(self) -> _PooledConnection:
        try:
            return self._conn.__enter__()
        except BaseException:
            self._checkin()
            raise

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Literal[False]:
        try:
            self._conn.__exit__(exc_type, exc_val, exc_tb)
            return False
        finally:
            self._checkin()

    def _checkin(self) -> None:
        if not self._checked_in:
            self._checked_in = True
            self._conn.checkin()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


@functools.lru_cache(maxsize=16384)
def _parse_iso_utc_cached(ts: str) -> Optional[datetime]:
    return _parse_iso_utc_impl(ts)


def _parse_iso_utc(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    return _parse_iso_utc_cached(ts)


def _parse_iso_utc_impl(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        cleaned = ts.strip()
        if not cleaned:
            return None
        if cleaned.endswith(("Z", "z")):
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
    stored_conf = float(row.get("confidence", 1.0))
    ttl_raw = row.get("ttl_days")
    if ttl_raw is None:
        return round(stored_conf, 4), stored_conf < STALE_THRESHOLD
    try:
        ttl = float(ttl_raw)
        if ttl <= 0.0:
            return round(stored_conf, 4), stored_conf < STALE_THRESHOLD
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


_TOK_COMPACT_CACHE: dict[str, str] = {}
_PHRASE_PATTERN_CACHE: dict[str, "re.Pattern[str]"] = {}


def _phrase_pattern(phrase: str) -> "re.Pattern[str]":
    pat = _PHRASE_PATTERN_CACHE.get(phrase)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(phrase) + r"\b")
        if len(_PHRASE_PATTERN_CACHE) > 512:
            _PHRASE_PATTERN_CACHE.clear()
        _PHRASE_PATTERN_CACHE[phrase] = pat
    return pat


def _get_db_stamp(db_path: Path) -> Optional[DbStamp]:
    # One stat per file (the old exists()+stat() pair doubled the syscalls; on Windows each
    # one is a CreateFile round-trip that Defender may scan).
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    try:
        wst = os.stat(f"{db_path}-wal")
        return (st.st_mtime_ns, st.st_size, wst.st_mtime_ns, wst.st_size)
    except OSError:
        return (st.st_mtime_ns, st.st_size, 0, 0)


def _normalize_vector(vec: Sequence[float] | np.ndarray, enforce_dim: bool = True) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if enforce_dim and arr.size != EMBEDDING_DIM:
        raise ValueError(
            f"Invalid embedding dimension {arr.size}; Mesh requires {EMBEDDING_DIM}-dimensional vectors."
        )
    norm = float(np.linalg.norm(arr))
    if norm > 1e-12:
        arr = arr / norm
    return np.ascontiguousarray(arr, dtype=np.float32)


def _vector_to_blob(vec: Sequence[float] | np.ndarray) -> bytes:
    """Serialize a vector into a normalized float32 byte buffer (encrypting if at-rest encryption is active)."""
    raw = _normalize_vector(vec).tobytes()
    if is_encryption_enabled():
        enc = encrypt_blob_for_storage(raw)
        if enc is None:
            raise RuntimeError("Vector encryption failed while at-rest encryption is active.")
        return enc
    return raw


def _blob_to_vector(blob: bytes) -> np.ndarray:
    """Deserialize a float32 byte buffer (decrypting first if it carries a `MESHB1` header)."""
    if is_encrypted_blob(blob):
        plain = decrypt_blob_if_encrypted(blob)
        if plain is None:
            return np.empty(0, dtype=np.float32)
        return np.frombuffer(plain, dtype=np.float32)
    return np.frombuffer(blob, dtype=np.float32)


def _parse_tags_json(raw_tags: Any) -> list[str]:
    if not raw_tags:
        return []
    if isinstance(raw_tags, list):
        return [str(t).strip() for t in raw_tags if str(t).strip()]
    if isinstance(raw_tags, str):
        s = decrypt_text_if_encrypted(raw_tags).strip()
        if not s or s == "[]":
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if str(t).strip()]
        except Exception:
            pass
    return []


def _enforce_store_guard(
    content: str,
    tags_list: list[str],
    local_only: bool,
    allow_redacted_public: bool = False,
) -> tuple[str, list[str], bool]:
    """
    Finding S11: Enforce `guard_ingest` and `guard_tags` at the `MeshStore` boundary
    so no caller (`store.upsert`, `store.upsert_batch`, `store.update`, `collection.upsert`)
    can bypass prompt-injection rejection or secret redaction.
    """
    g_res = guard_ingest(content, local_only=local_only)
    if g_res.rejected:
        raise ValueError(g_res.reason or "Memory content rejected by ingestion guard.")
    clean_tags, tags_private, _ = guard_tags(tags_list, local_only=g_res.local_only)
    final_local = bool(local_only or g_res.local_only or tags_private)
    if not final_local and not allow_redacted_public and _REDACTED_PLACEHOLDER_RE.search(g_res.content):
        final_local = True
    return g_res.content, clean_tags, final_local


_CACHE_REBUILD_COUNT: int = 0


def get_cache_rebuild_count() -> int:
    with _CACHE_LOCK:
        return _CACHE_REBUILD_COUNT


def reset_cache_rebuild_count() -> None:
    global _CACHE_REBUILD_COUNT
    with _CACHE_LOCK:
        _CACHE_REBUILD_COUNT = 0
        _VectorCacheEntry.build_count = 0


class _VectorCacheEntry:
    build_count: int = 0

    __slots__ = (
        "stamp",
        "data_version",
        "content_version",
        "mtime_ns",
        "file_size",
        "ids",
        "id_to_idx",
        "_matrix_buf",
        "_hash_matrix_buf",
        "dim",
        "all_ids",
        "rows_by_id",
        "lexical_by_id",
        "stem_df",
        "stem_to_ids",
        "token_to_ids",
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
        content_version: Optional[int] = None,
        needs_healing_ids: Optional[set[str]] = None,
        reused_lexical: Optional[dict[str, dict[str, Any]]] = None,
    ):
        global _CACHE_REBUILD_COUNT
        self.stamp: DbStamp = stamp
        self.data_version: int = data_version
        self.content_version: Optional[int] = content_version
        self.mtime_ns: int = stamp[0]
        self.file_size: int = stamp[1]
        self.ids: list[str] = ids
        self.id_to_idx: dict[str, int] = {mid: idx for idx, mid in enumerate(ids)}
        self.all_ids: set[str] = all_ids if all_ids is not None else set(ids)
        self.rows_by_id: dict[str, dict[str, Any]] = rows_by_id if rows_by_id is not None else {}
        self.lexical_by_id: dict[str, dict[str, Any]] = {}
        self.stem_df: dict[str, int] = {}
        self.stem_to_ids: dict[str, set[str]] = {}
        self.token_to_ids: dict[str, set[str]] = {}
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

            hbuf = np.empty((cap, 384), dtype=np.float32)
            self._hash_matrix_buf: Optional[np.ndarray] = hbuf
        else:
            self._matrix_buf = None
            self._hash_matrix_buf = None
            self.dim = 0

        for mid, row_meta in self.rows_by_id.items():
            self._index_row_lexical(mid, row_meta)
            idx = self.id_to_idx.get(mid)
            if idx is not None and self._hash_matrix_buf is not None:
                if row_meta.get("embedding_model") == "hash-384" and self._matrix_buf is not None:
                    self._hash_matrix_buf[idx] = self._matrix_buf[idx]
                else:
                    try:
                        from .memory import _hash_embed_384

                        self._hash_matrix_buf[idx] = _hash_embed_384(str(row_meta.get("content", "")))
                    except Exception:
                        if self._matrix_buf is not None and self._matrix_buf.shape[1] == 384:
                            self._hash_matrix_buf[idx] = self._matrix_buf[idx]

            if needs_healing_ids is None:
                idx = self.id_to_idx.get(mid)
                vec = self._matrix_buf[idx] if (idx is not None and self._matrix_buf is not None) else None
                if _row_needs_healing(row_meta, vec):
                    self.needs_healing_ids.add(mid)

        _VectorCacheEntry.build_count += 1
        _CACHE_REBUILD_COUNT += 1

    def _index_row_lexical(self, memory_id: str, row_meta: dict[str, Any]) -> None:
        old_tokens = row_meta.get("_indexed_tokens")
        if old_tokens:
            for tok in old_tokens:
                t_set = self.token_to_ids.get(tok)
                if t_set is not None:
                    t_set.discard(memory_id)
                    if not t_set:
                        self.token_to_ids.pop(tok, None)

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
        tags_raw = row_meta.get("tags")
        tags_list = row_meta.get("tags_list") or (_parse_tags_json(tags_raw) if tags_raw else [])
        row_meta["tags_list"] = tags_list
        row_meta["_tags_low_set"] = frozenset(t.lower() for t in tags_list)

        search_text = f"{content} {' '.join(tags_list)}" if tags_list else content
        search_tokens = _extract_search_tokens(search_text)
        row_meta["_search_tokens"] = search_tokens

        indexed_tokens = set(search_tokens[0]) | search_tokens[2]
        row_meta["_indexed_tokens"] = indexed_tokens
        for tok in indexed_tokens:
            self.token_to_ids.setdefault(tok, set()).add(memory_id)

        new_lex = _build_row_lexical_entry(content, tags_list)
        self.lexical_by_id[memory_id] = new_lex
        for s in new_lex["df_stems"]:
            self.stem_df[s] = self.stem_df.get(s, 0) + 1
            self.stem_to_ids.setdefault(s, set()).add(memory_id)

    def _unindex_row_lexical(self, memory_id: str) -> None:
        row_meta = self.rows_by_id.get(memory_id)
        if row_meta is not None:
            old_tokens = row_meta.get("_indexed_tokens")
            if old_tokens:
                for tok in old_tokens:
                    t_set = self.token_to_ids.get(tok)
                    if t_set is not None:
                        t_set.discard(memory_id)
                        if not t_set:
                            self.token_to_ids.pop(tok, None)

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
        copy_matrix: bool = True,
    ) -> tuple[list[str], Optional[np.ndarray], dict[str, dict[str, Any]]]:
        with _CACHE_LOCK:
            ids_copy = list(self.ids)
            if self._matrix_buf is not None and ids_copy:
                mat_slice = self._matrix_buf[: len(ids_copy)]
                mat_view = mat_slice.copy() if copy_matrix else mat_slice
            else:
                mat_view = None
            rows_copy = dict(self.rows_by_id)
            return ids_copy, mat_view, rows_copy

    def similarity_snapshot(
        self,
        query_vec: np.ndarray,
        use_hash_matrix: bool = False,
    ) -> tuple[list[str], Optional[np.ndarray], dict[str, dict[str, Any]]]:
        with _CACHE_LOCK:
            ids_copy = list(self.ids)
            buf = self._hash_matrix_buf if use_hash_matrix else self._matrix_buf
            if buf is None or not ids_copy:
                return ids_copy, None, dict(self.rows_by_id)
            mat_slice = buf[: len(ids_copy)]
            if query_vec.ndim != 1 or query_vec.shape[0] != mat_slice.shape[1]:
                return ids_copy, None, dict(self.rows_by_id)
            return ids_copy, mat_slice @ query_vec, dict(self.rows_by_id)

    def update_stamp(
        self,
        stamp: DbStamp,
        data_version: Optional[int] = None,
        content_version: Optional[int] = None,
    ) -> None:
        self.stamp = stamp
        self.mtime_ns = stamp[0]
        self.file_size = stamp[1]
        if data_version is not None:
            self.data_version = data_version
        if content_version is not None:
            self.content_version = content_version

    def upsert_vector(
        self,
        memory_id: str,
        vec: Optional[np.ndarray],
        row_meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        self.all_ids.add(memory_id)
        if row_meta is not None:
            self.rows_by_id[memory_id] = dict(row_meta)
            self._index_row_lexical(memory_id, self.rows_by_id[memory_id])
            if _row_needs_healing(self.rows_by_id[memory_id], vec):
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
            if self._hash_matrix_buf is not None:
                meta = self.rows_by_id.get(memory_id) or row_meta or {}
                if meta.get("embedding_model") == "hash-384" and vec_1d.shape[0] == 384:
                    self._hash_matrix_buf[existing_idx] = vec_1d
                else:
                    try:
                        from .memory import _hash_embed_384

                        self._hash_matrix_buf[existing_idx] = _hash_embed_384(str(meta.get("content", "")))
                    except Exception:
                        pass
            return True

        n = len(self.ids)
        if self._matrix_buf is None or n == 0:
            dim = int(vec_1d.shape[0])
            buf = np.empty((64, dim), dtype=np.float32)
            buf[0] = vec_1d
            self._matrix_buf = buf
            self.dim = dim
            hbuf = np.empty((64, 384), dtype=np.float32)
            meta = self.rows_by_id.get(memory_id) or row_meta or {}
            if meta.get("embedding_model") == "hash-384" and vec_1d.shape[0] == 384:
                hbuf[0] = vec_1d
            else:
                try:
                    from .memory import _hash_embed_384

                    hbuf[0] = _hash_embed_384(str(meta.get("content", "")))
                except Exception:
                    hbuf[0] = vec_1d
            self._hash_matrix_buf = hbuf
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
            if self._hash_matrix_buf is not None:
                new_hbuf = np.empty((new_cap, 384), dtype=np.float32)
                new_hbuf[:n] = self._hash_matrix_buf[:n]
                self._hash_matrix_buf = new_hbuf

        self._matrix_buf[n] = vec_1d
        if self._hash_matrix_buf is not None:
            meta = self.rows_by_id.get(memory_id) or row_meta or {}
            if meta.get("embedding_model") == "hash-384" and vec_1d.shape[0] == 384:
                self._hash_matrix_buf[n] = vec_1d
            else:
                try:
                    from .memory import _hash_embed_384

                    self._hash_matrix_buf[n] = _hash_embed_384(str(meta.get("content", "")))
                except Exception:
                    self._hash_matrix_buf[n] = vec_1d

        self.id_to_idx[memory_id] = n
        self.ids.append(memory_id)
        return True

    def delete_vector(self, memory_id: str) -> None:
        self.all_ids.discard(memory_id)
        self.needs_healing_ids.discard(memory_id)
        self._unindex_row_lexical(memory_id)
        self.rows_by_id.pop(memory_id, None)
        idx = self.id_to_idx.pop(memory_id, None)
        if idx is None:
            return
        last_idx = len(self.ids) - 1
        if idx != last_idx:
            last_id = self.ids[last_idx]
            self.ids[idx] = last_id
            self.id_to_idx[last_id] = idx
            if self._matrix_buf is not None:
                self._matrix_buf[idx] = self._matrix_buf[last_idx]
            if self._hash_matrix_buf is not None:
                self._hash_matrix_buf[idx] = self._hash_matrix_buf[last_idx]
        self.ids.pop()
        if not self.ids or self._matrix_buf is None:
            self._matrix_buf = None
            self._hash_matrix_buf = None
            self.dim = 0


_CACHE_LOCK = threading.RLock()
_CONNECTION_POOL: OrderedDict[Path, _PooledConnection] = OrderedDict()
_DB_PATH_NAMESPACES: dict[Path, str] = {}
_VECTOR_CACHE: OrderedDict[tuple[Path, str], _VectorCacheEntry] = OrderedDict()


def _evict_vector_cache_for_db_path(db_path: Path) -> None:
    for key in [k for k in _VECTOR_CACHE if k[0] == db_path]:
        _VECTOR_CACHE.pop(key, None)


_QUERY_WRITE_COUNTS: dict[Path, int] = {}
_TRANSIENT_EMBEDDINGS: dict[tuple[str, str], np.ndarray] = {}


# --- Batched Access Tracking Buffer ---


class _AccessTrackingBuffer:
    def __init__(self, db_path: Path, namespace: str) -> None:
        self.db_path = db_path
        self.namespace = namespace
        self.access_increments: dict[str, tuple[int, str]] = {}
        self.query_rows: list[tuple[str, str, str, str, Optional[float], int, str, int]] = []
        self.event_count: int = 0
        self.first_event_ts: float = 0.0


_ACCESS_TRACKING_LOCK = threading.RLock()
_ACCESS_TRACKING_BUFFERS: dict[Path, _AccessTrackingBuffer] = {}

# Backward-compatibility aliases
_TelemetryBuffer = _AccessTrackingBuffer
_TELEMETRY_LOCK = _ACCESS_TRACKING_LOCK
_TELEMETRY_BUFFERS = _ACCESS_TRACKING_BUFFERS


def _get_tracking_batch_limit() -> int:
    raw = (
        os.environ.get("MESH_TRACKING_BATCH", "").strip()
        or os.environ.get("MESH_TELEMETRY_BATCH", "").strip()
    )
    if not raw:
        return 128
    try:
        return max(0, int(raw))
    except ValueError:
        return 128


def _get_tracking_flush_seconds() -> float:
    raw = (
        os.environ.get("MESH_TRACKING_FLUSH_SECONDS", "").strip()
        or os.environ.get("MESH_TELEMETRY_FLUSH_SECONDS", "").strip()
    )
    if not raw:
        return 5.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


# Backward-compatibility aliases for config helpers
_get_telemetry_batch_limit = _get_tracking_batch_limit
_get_telemetry_flush_seconds = _get_tracking_flush_seconds


def flush_all_access_tracking() -> None:
    """Flush all pending buffered recall access tracking across all namespaces."""
    with _ACCESS_TRACKING_LOCK:
        paths = list(_ACCESS_TRACKING_BUFFERS.keys())
    for p in paths:
        try:
            _flush_access_tracking_for_db_path(p)
        except Exception:
            continue


# Backward-compatibility alias
flush_all_telemetry = flush_all_access_tracking


def _requeue_access_tracking(
    resolved: Path,
    namespace: str,
    inc_items: list[tuple[str, tuple[int, str]]],
    q_rows: list[tuple],
) -> None:
    with _ACCESS_TRACKING_LOCK:
        buf = _ACCESS_TRACKING_BUFFERS.get(resolved)
        if buf is None:
            buf = _AccessTrackingBuffer(db_path=resolved, namespace=namespace)
            _ACCESS_TRACKING_BUFFERS[resolved] = buf
        for mid, (delta, accessed_at) in inc_items:
            prev = buf.access_increments.get(mid)
            if prev is None:
                buf.access_increments[mid] = (delta, accessed_at)
            else:
                buf.access_increments[mid] = (
                    prev[0] + delta,
                    max(prev[1], accessed_at),
                )
        if q_rows:
            buf.query_rows.extend(q_rows)
        buf.event_count += len(inc_items) + len(q_rows)
        if buf.event_count == len(inc_items) + len(q_rows):
            buf.first_event_ts = time.monotonic()


# Backward-compatibility alias
_requeue_telemetry = _requeue_access_tracking


def _flush_access_tracking_for_db_path(db_path: Path) -> None:
    resolved = db_path.resolve()
    with _ACCESS_TRACKING_LOCK:
        buf = _ACCESS_TRACKING_BUFFERS.pop(resolved, None)
        if buf is None or (not buf.access_increments and not buf.query_rows):
            return
        inc_items = list(buf.access_increments.items())
        q_rows = list(buf.query_rows)
        ns = buf.namespace

    if not resolved.exists():
        _requeue_access_tracking(resolved, ns, inc_items, q_rows)
        return

    try:
        enc_on = is_encryption_enabled()
        with _CACHE_LOCK:
            store = MeshStore(namespace=ns, create=False)
            q_writes = _QUERY_WRITE_COUNTS.get(resolved, 0) + len(q_rows)
            _QUERY_WRITE_COUNTS[resolved] = q_writes
            with store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for mid, (delta, accessed_at) in inc_items:
                        conn.execute(
                            """
                            UPDATE memories
                            SET access_count = access_count + ?,
                                accessed_at = ?
                            WHERE namespace = ? AND id = ?
                            """,
                            (delta, accessed_at, ns, mid),
                        )
                    if q_rows:
                        rows_to_insert = [
                            (
                                qid,
                                q_ns,
                                encrypt_text_for_storage(q_text) if enc_on else q_text,
                                aid,
                                b_sim,
                                r_cnt,
                                ts,
                                p_flag,
                            )
                            for (qid, q_ns, q_text, aid, b_sim, r_cnt, ts, p_flag) in q_rows
                        ]
                        conn.executemany(
                            """
                            INSERT INTO queries
                            (id, namespace, query_text, agent_id, best_similarity, result_count, timestamp, private_query)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            rows_to_insert,
                        )
                        if q_writes >= 100:
                            _QUERY_WRITE_COUNTS[resolved] = 0
                            _prune_queries_table_locked(conn, ns, _max_query_rows())
                    conn.commit()
                    store._read_content_version_locked(conn)
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
            new_stamp = _get_db_stamp(resolved)
            with _CACHE_LOCK:
                if new_stamp is not None:
                    _INITIALIZED_DBS[resolved] = new_stamp
                    for (c_path, _), vcache in _VECTOR_CACHE.items():
                        if c_path == resolved:
                            vcache.stamp = new_stamp
                            vcache.mtime_ns = new_stamp[0]
                            vcache.file_size = new_stamp[1]
    except Exception:
        _requeue_access_tracking(resolved, ns, inc_items, q_rows)


# Backward-compatibility alias
_flush_telemetry_for_db_path = _flush_access_tracking_for_db_path


def _scrub_contradictions_in_tx(
    conn: sqlite3.Connection, namespace: str, memory_id: str, replacement_text: str
) -> None:
    """Replace any contradiction text that quotes `memory_id` with `replacement_text` (Finding S5, S12)."""
    stored_rep = (
        encrypt_text_for_storage(replacement_text)
        if is_encryption_enabled()
        else replacement_text
    )
    conn.execute(
        "UPDATE contradictions SET new_content = ? WHERE namespace = ? AND new_memory_id = ?",
        (stored_rep, namespace, memory_id),
    )
    conn.execute(
        "UPDATE contradictions SET existing_content = ? WHERE namespace = ? AND existing_memory_id = ?",
        (stored_rep, namespace, memory_id),
    )


def _scrub_queries_in_tx(conn: sqlite3.Connection, namespace: str, content: str) -> None:
    """
    Delete recall-telemetry rows that reproduce a memory's text verbatim or as a substring,
    including encrypted query rows (Finding S5, S12).
    """
    clean = (content or "").strip()
    if not clean:
        return
    clean_low = clean.lower()
    trunc_low = clean[:512].lower()
    conn.execute(
        "DELETE FROM queries WHERE namespace = ? "
        "AND (LOWER(query_text) = ? OR LOWER(query_text) = ? "
        "OR INSTR(LOWER(query_text), ?) > 0 OR INSTR(?, LOWER(query_text)) > 0)",
        (namespace, clean_low, trunc_low, trunc_low, clean_low),
    )
    # Also scrub encrypted rows in `queries`
    enc_rows = conn.execute(
        "SELECT id, query_text FROM queries WHERE namespace = ? AND query_text LIKE 'enc:v1:%'",
        (namespace,),
    ).fetchall()
    for qid, q_enc in enc_rows:
        try:
            q_dec = decrypt_text_if_encrypted(q_enc).strip().lower()
            if q_dec and (q_dec == clean_low or q_dec == trunc_low or trunc_low in q_dec or (len(q_dec) >= 4 and q_dec in clean_low)):
                conn.execute("DELETE FROM queries WHERE id = ? AND namespace = ?", (qid, namespace))
        except Exception:
            continue


def _scrub_pending_query_tracking(namespace: str, content: Optional[str]) -> None:
    """Drop buffered recall queries that reproduce content being deleted or redacted (Finding S12)."""
    clean = (content or "").strip()
    if not clean:
        return
    clean_low = clean.lower()
    trunc_low = clean[:512].lower()
    with _ACCESS_TRACKING_LOCK:
        for buf in _ACCESS_TRACKING_BUFFERS.values():
            if buf.namespace != namespace or not buf.query_rows:
                continue
            kept = []
            for row in buf.query_rows:
                q_str = decrypt_text_if_encrypted(str(row[2] or "")).strip().lower()
                if q_str and (
                    q_str == clean_low
                    or q_str == trunc_low
                    or trunc_low in q_str
                    or (len(q_str) >= 4 and q_str in clean_low)
                ):
                    continue
                kept.append(row)
            dropped = len(buf.query_rows) - len(kept)
            if dropped:
                buf.event_count = max(0, buf.event_count - dropped)
                buf.query_rows = kept


# Backward-compatibility alias
_scrub_pending_query_telemetry = _scrub_pending_query_tracking


_FTS_PERMANENT_ERROR_MARKERS = (
    "no such table",
    "no such module",
    "not a database",
    "malformed",
    "corrupt",
    "vtable constructor failed",
)


def _is_permanent_fts_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _FTS_PERMANENT_ERROR_MARKERS)


def close_store_connection(db_path: Path) -> None:
    try:
        _flush_access_tracking_for_db_path(db_path)
    except Exception:
        pass
    with _CACHE_LOCK:
        _QUERY_WRITE_COUNTS.pop(db_path, None)
        _DB_PATH_NAMESPACES.pop(db_path, None)
        conn = _CONNECTION_POOL.pop(db_path, None)
        if conn is not None:
            conn.close_pooled()


class _InitializedDbRegistry(dict):
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
    try:
        flush_all_telemetry()
    except Exception:
        pass
    with _CACHE_LOCK:
        for conn in list(_CONNECTION_POOL.values()):
            conn.close_pooled()
        _CONNECTION_POOL.clear()
        _DB_PATH_NAMESPACES.clear()


def _max_query_rows() -> int:
    try:
        val = int(os.environ.get("MESH_MAX_QUERY_ROWS", str(DEFAULT_MAX_QUERY_ROWS)))
        return max(100, val)
    except ValueError:
        return DEFAULT_MAX_QUERY_ROWS


def _prune_queries_table_locked(conn: sqlite3.Connection, namespace: str, max_rows: int) -> None:
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
    """Backward-compatibility shim emulating ChromaDB's `store.collection` API."""

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

        self._store.flush_telemetry()
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
            doc_plain = decrypt_text_if_encrypted(r["content"])
            tags_plain = decrypt_text_if_encrypted(r["tags"] or "[]")
            if want_documents:
                out_docs.append(doc_plain)
            if want_metadatas:
                out_metas.append(
                    {
                        "memory_type": r["memory_type"],
                        "source_agent": r["source_agent"],
                        "confidence": float(r["confidence"]),
                        "tags": tags_plain,
                        "created_at": r["created_at"],
                        "ttl_days": r["ttl_days"],
                        "decay_start_at": r["decay_start_at"],
                        "local_only": int(r["local_only"] or 0),
                        "embedding_model": r["embedding_model"],
                        "trust_tier": r["trust_tier"] if "trust_tier" in r.keys() else "user",
                        "quarantined": int(r["quarantined"] or 0) if "quarantined" in r.keys() else 0,
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
        enc_on = is_encryption_enabled()
        with namespace_write_lock(self._store.namespace):
            with _CACHE_LOCK:
                synced_cache = self._store._is_cache_synced_locked()
                in_place_ok = synced_cache is not None
                pending_vectors: list[tuple[str, Optional[np.ndarray], dict[str, Any]]] = []
                new_cv: Optional[int] = None
                audit_scrubs: list[tuple[str, str, bool]] = []
                with self._store._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        for i, raw_mid in enumerate(ids):
                            mid = validate_memory_id(raw_mid)
                            raw_doc = documents[i] if documents and i < len(documents) else ""
                            meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                            emb = embeddings[i] if embeddings and i < len(embeddings) else None
                            norm_vec = _normalize_vector(emb) if emb is not None else None
                            raw_blob = norm_vec.tobytes() if norm_vec is not None else None

                            tags_raw = meta.get("tags", "[]")
                            raw_tags_list = _parse_tags_json(tags_raw)
                            req_local = bool(meta.get("local_only", 0))

                            # Finding S11: Enforce guard_ingest and guard_tags at lowest layer
                            doc, tags_list, final_local = _enforce_store_guard(
                                raw_doc, raw_tags_list, req_local
                            )
                            tags_str = json.dumps(tags_list)
                            local_flag = 1 if final_local else 0

                            mem_type = str(meta.get("memory_type", "fact"))
                            source_agent = str(meta.get("source_agent", "unknown"))
                            conf = float(meta.get("confidence", 1.0))
                            created_at = meta.get("created_at", now)
                            updated_at = meta.get("updated_at", now)
                            accessed_at = meta.get("accessed_at", now)
                            access_count = int(meta.get("access_count", 0))
                            ttl_days = meta.get("ttl_days")
                            decay_start_at = meta.get("decay_start_at")
                            emb_model = str(
                                meta.get("embedding_model")
                                or ("onnx-minilm-l6-v2" if raw_blob else "needs-reembed")
                            )
                            trust_tier = _normalize_trust_tier(meta.get("trust_tier"), default="user")
                            quarantined_flag = 1 if meta.get("quarantined") else 0
                            norm_ref = normalize_secret_ref(
                                meta.get("secret_ref"), default_namespace=self._store.namespace
                            )
                            secret_ref_json = json.dumps(norm_ref, sort_keys=True) if norm_ref else None

                            db_doc = encrypt_text_for_storage(doc) if enc_on else doc
                            db_tags = encrypt_text_for_storage(tags_str) if enc_on else tags_str
                            db_blob = encrypt_blob_for_storage(raw_blob) if (enc_on and raw_blob) else raw_blob
                            db_ref = (
                                encrypt_text_for_storage(secret_ref_json)
                                if (enc_on and secret_ref_json)
                                else secret_ref_json
                            )

                            old_row = conn.execute(
                                "SELECT content, local_only FROM memories WHERE id = ? AND namespace = ?",
                                (mid, self._store.namespace),
                            ).fetchone()
                            old_rowid = self._store._get_memory_rowid(conn, mid)
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO memories
                                (id, namespace, content, confidence, memory_type,
                                 source_agent, tags, created_at, updated_at, accessed_at, access_count,
                                 ttl_days, decay_start_at, local_only, embedding, embedding_model,
                                 trust_tier, quarantined, secret_ref)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    mid,
                                    self._store.namespace,
                                    db_doc,
                                    conf,
                                    mem_type,
                                    source_agent,
                                    db_tags,
                                    created_at,
                                    updated_at,
                                    accessed_at,
                                    access_count,
                                    ttl_days,
                                    decay_start_at,
                                    local_flag,
                                    db_blob,
                                    emb_model,
                                    trust_tier,
                                    quarantined_flag,
                                    db_ref,
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
                                local_only=bool(local_flag),
                            )
                            if old_row is not None:
                                old_content = decrypt_text_if_encrypted(str(old_row[0] or ""))
                                old_local = bool(old_row[1])
                                if old_content != doc or (local_flag and not old_local):
                                    _scrub_contradictions_in_tx(
                                        conn,
                                        self._store.namespace,
                                        mid,
                                        REDACTED_CONTENT_PLACEHOLDER
                                        if local_flag
                                        else doc,
                                    )
                                    _scrub_queries_in_tx(conn, self._store.namespace, old_content)
                                    _scrub_pending_query_telemetry(self._store.namespace, old_content)
                                    audit_scrubs.append((mid, old_content, bool(local_flag)))
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
                                    "updated_at": updated_at,
                                    "accessed_at": accessed_at,
                                    "access_count": access_count,
                                    "ttl_days": ttl_days,
                                    "decay_start_at": decay_start_at,
                                    "local_only": bool(local_flag),
                                    "embedding_model": emb_model,
                                    "trust_tier": trust_tier,
                                    "quarantined": bool(quarantined_flag),
                                    "secret_ref": norm_ref,
                                }
                                pending_vectors.append((mid, norm_vec, row_meta))
                        new_cv = self._store._bump_content_version_in_tx(conn)
                        conn.commit()
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        raise
                if in_place_ok and synced_cache is not None:
                    for mid, vec, meta in pending_vectors:
                        if not synced_cache.upsert_vector(mid, vec, row_meta=meta):
                            in_place_ok = False
                            break
                if in_place_ok and synced_cache is not None:
                    self._store._refresh_stamps_after_write_locked(synced_cache, content_version=new_cv)
                else:
                    self._store._invalidate_cache()

        for scrubbed_id, scrubbed_old_content, scrubbed_local in audit_scrubs:
            scrub_memory_update_from_audit(
                namespace=self._store.namespace,
                memory_id=scrubbed_id,
                old_content=scrubbed_old_content,
                local_only=scrubbed_local,
            )

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

    def __init__(self, namespace: str = "default", create: bool = True):
        self.namespace = validate_namespace(namespace)
        self.create = bool(create)
        ensure_secure_mesh_dir()
        base_dir = cached_resolved_mesh_dir()
        raw_candidate = base_dir / f"mesh_{self.namespace}.db"
        reject_symlink(raw_candidate)
        resolved_db = raw_candidate.resolve()
        if resolved_db.parent != base_dir:
            raise ValueError(f"Invalid namespace path resolution for '{namespace}'.")
        self.db_path = resolved_db
        self._fts_enabled = True
        self._fts_dirty = False
        if not self.create and not self.db_path.exists():
            raise NamespaceNotFoundError(f"Namespace '{self.namespace}' does not exist.")
        if self.create and not self.db_path.exists():
            _check_namespace_creation_limit(base_dir, self.namespace)
        if self.db_path.exists() or self.create:
            self._ensure_initialized()
        self.collection = _CollectionCompatShim(self)

    # --- Connection, Content Version & Schema Management ---

    def _connect(self) -> _ConnectionCheckout:
        """Check out a persistent, thread-safe SQLite WAL connection for `self.db_path`."""
        reject_symlink(self.db_path)
        evicted: list[tuple[Path, Optional[_PooledConnection], Optional[str]]] = []
        with _CACHE_LOCK:
            if not self.create and not self.db_path.exists():
                raise NamespaceNotFoundError(f"Namespace '{self.namespace}' does not exist.")
            conn = _CONNECTION_POOL.get(self.db_path)
            if conn is not None and conn._mesh_closed:
                _CONNECTION_POOL.pop(self.db_path, None)
                _DB_PATH_NAMESPACES.pop(self.db_path, None)
                conn = None
            if conn is not None and self.db_path.exists():
                _CONNECTION_POOL.move_to_end(self.db_path)
                _DB_PATH_NAMESPACES[self.db_path] = self.namespace
                conn.checkout()
                return _ConnectionCheckout(conn)
            if conn is not None:
                conn.close_pooled()
                _CONNECTION_POOL.pop(self.db_path, None)
                _DB_PATH_NAMESPACES.pop(self.db_path, None)
            if not self.db_path.exists():
                _check_namespace_creation_limit(self.db_path.parent, self.namespace)
            ensure_secure_mesh_dir(self.db_path.parent)
            conn = sqlite3.connect(
                self.db_path,
                timeout=10.0,
                check_same_thread=False,
                isolation_level=None,
                factory=_PooledConnection,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            sync_mode = "NORMAL" if os.environ.get("MESH_STRICT_FSYNC") == "1" else "OFF"
            conn.execute(f"PRAGMA synchronous={sync_mode}")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA secure_delete=ON")
            try:
                if os.name != "nt":
                    conn.execute("PRAGMA mmap_size=67108864")
                conn.execute("PRAGMA cache_size=-32000")
            except sqlite3.OperationalError:
                pass
            secure_file_permissions(self.db_path)
            _CONNECTION_POOL[self.db_path] = conn
            _CONNECTION_POOL.move_to_end(self.db_path)
            _DB_PATH_NAMESPACES[self.db_path] = self.namespace
            conn.checkout()

            max_pool = _get_max_pool_size()
            while len(_CONNECTION_POOL) > max_pool:
                victim_path: Optional[Path] = None
                for cand_path in _CONNECTION_POOL:
                    if cand_path != self.db_path:
                        victim_path = cand_path
                        break
                if victim_path is None:
                    break
                victim_conn = _CONNECTION_POOL.pop(victim_path, None)
                victim_ns = _DB_PATH_NAMESPACES.pop(victim_path, None)
                _QUERY_WRITE_COUNTS.pop(victim_path, None)
                dict.pop(_INITIALIZED_DBS, victim_path, None)
                _evict_vector_cache_for_db_path(victim_path)
                evicted.append((victim_path, victim_conn, victim_ns))

        if evicted:
            _finalize_evicted_resources(evicted)
        return _ConnectionCheckout(conn)

    def _get_data_version_locked(self) -> int:
        conn = _CONNECTION_POOL.get(self.db_path)
        if conn is None:
            return 0
        try:
            with conn._mesh_lock:
                row = conn.execute("PRAGMA data_version").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _read_content_version_locked(self, conn: Optional[sqlite3.Connection] = None) -> Optional[int]:
        c = conn or _CONNECTION_POOL.get(self.db_path)
        if c is None:
            return None
        try:
            lock = getattr(c, "_mesh_lock", None)
            if lock is not None:
                with lock:
                    row = c.execute(
                        "SELECT value FROM mesh_meta WHERE key = 'content_version'"
                    ).fetchone()
            else:
                row = c.execute(
                    "SELECT value FROM mesh_meta WHERE key = 'content_version'"
                ).fetchone()
            if row and row[0] is not None:
                return int(row[0])
        except Exception:
            pass
        return None

    def _bump_content_version_in_tx(self, conn: sqlite3.Connection) -> int:
        cur_val = self._read_content_version_locked(conn) or 0
        new_val = cur_val + 1
        conn.execute(
            "INSERT OR REPLACE INTO mesh_meta (key, value) VALUES ('content_version', ?)",
            (str(new_val),),
        )
        return new_val

    def _is_cache_synced_locked(self) -> Optional[_VectorCacheEntry]:
        key = (self.db_path, self.namespace)
        cached = _VECTOR_CACHE.get(key)
        if cached is None:
            return None
        stamp = _get_db_stamp(self.db_path)
        if stamp is None:
            return None
        dv = self._get_data_version_locked()
        if cached.stamp == stamp and (dv == 0 or cached.data_version == 0 or dv == cached.data_version):
            _VECTOR_CACHE.move_to_end(key)
            if self.db_path in _CONNECTION_POOL:
                _CONNECTION_POOL.move_to_end(self.db_path)
            return cached

        try:
            with self._connect() as conn:
                cv = self._read_content_version_locked(conn)
                if (
                    cv is not None
                    and cached.content_version is not None
                    and cv == cached.content_version
                ):
                    cnt_row = conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE namespace = ?",
                        (self.namespace,),
                    ).fetchone()
                    db_count = int(cnt_row[0]) if cnt_row and cnt_row[0] is not None else 0
                    if db_count == len(cached.all_ids):
                        cached.update_stamp(stamp, data_version=dv, content_version=cv)
                        _INITIALIZED_DBS[self.db_path] = stamp
                        _VECTOR_CACHE.move_to_end(key)
                        if self.db_path in _CONNECTION_POOL:
                            _CONNECTION_POOL.move_to_end(self.db_path)
                        return cached
        except Exception:
            pass
        return None

    def _refresh_stamps_after_write_locked(
        self,
        synced_cache: Optional[_VectorCacheEntry],
        content_version: Optional[int] = None,
    ) -> None:
        secure_file_permissions(self.db_path)
        stamp = _get_db_stamp(self.db_path)
        if stamp is None:
            _INITIALIZED_DBS.pop(self.db_path, None)
            _evict_vector_cache_for_db_path(self.db_path)
            return
        _INITIALIZED_DBS[self.db_path] = stamp
        if synced_cache is not None:
            dv = self._get_data_version_locked()
            cv = content_version if content_version is not None else self._read_content_version_locked()
            synced_cache.update_stamp(stamp, data_version=dv, content_version=cv)

    def _refresh_cache_stamp_after_non_memory_write(self) -> None:
        stamp = _get_db_stamp(self.db_path)
        if stamp is not None:
            with _CACHE_LOCK:
                _INITIALIZED_DBS[self.db_path] = stamp
                for (c_path, _), vcache in _VECTOR_CACHE.items():
                    if c_path == self.db_path:
                        vcache.stamp = stamp
                        vcache.mtime_ns = stamp[0]
                        vcache.file_size = stamp[1]

    def _ensure_initialized(self) -> None:
        if not self.create and not self.db_path.exists():
            raise NamespaceNotFoundError(f"Namespace '{self.namespace}' does not exist.")
        with _CACHE_LOCK:
            stamp = _get_db_stamp(self.db_path)
            if (
                stamp is not None
                and stamp[1] > 0
                and _INITIALIZED_DBS.get(self.db_path) == stamp
                and not self._fts_dirty
            ):
                if self.db_path in _CONNECTION_POOL:
                    _CONNECTION_POOL.move_to_end(self.db_path)
                return
            try:
                _init_budget = max(0.0, float(os.environ.get("MESH_INIT_LOCK_RETRY_SECONDS", "10")))
            except ValueError:
                _init_budget = 10.0
            _init_deadline = time.monotonic() + _init_budget
            _init_delay = 0.01
            while True:
                try:
                    self._init_db()
                    break
                except sqlite3.OperationalError as _exc:
                    _msg = str(_exc).lower()
                    if ("locked" not in _msg and "busy" not in _msg) or time.monotonic() >= _init_deadline:
                        raise
                    time.sleep(_init_delay)
                    _init_delay = min(_init_delay * 2.0, 0.25)
            new_stamp = _get_db_stamp(self.db_path)
            if new_stamp is not None:
                _INITIALIZED_DBS[self.db_path] = new_stamp

    def _init_db(self) -> None:
        mem_count = 0
        public_unencrypted_mem_count = 0
        evicted: list[tuple[Path, Optional[_PooledConnection], Optional[str]]] = []
        enc_on = is_encryption_enabled()
        with _CACHE_LOCK:
            with self._connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mesh_meta (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT OR IGNORE INTO mesh_meta (key, value) VALUES ('content_version', '1')"
                )
                user_ver_cur = conn.execute("PRAGMA user_version").fetchone()
                user_ver = user_ver_cur[0] if user_ver_cur else 0
                if user_ver > int(SCHEMA_VERSION):
                    raise RuntimeError(
                        f"Database '{self.db_path.name}' has schema user_version {user_ver}, which is newer than supported version {SCHEMA_VERSION}. "
                        "Please upgrade Mesh to access this database."
                    )
                cur_ver_row = conn.execute(
                    "SELECT value FROM mesh_meta WHERE key = 'schema_version'"
                ).fetchone()
                cur_ver = cur_ver_row[0] if cur_ver_row else "1"
                try:
                    if int(cur_ver) > int(SCHEMA_VERSION):
                        raise RuntimeError(
                            f"Database '{self.db_path.name}' has schema version {cur_ver}, which is newer than supported version {SCHEMA_VERSION}. "
                            "Please upgrade Mesh to access this database."
                        )
                except ValueError:
                    pass
                if cur_ver != SCHEMA_VERSION or user_ver != int(SCHEMA_VERSION):
                    has_memories = conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='memories'"
                    ).fetchone()[0] > 0
                    if has_memories:
                        mem_rows = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                        if mem_rows > 0 and self.db_path.exists():
                            import shutil
                            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                            backup_file = self.db_path.with_name(f"{self.db_path.name}.backup-v0.7.2-{ts}")
                            if not backup_file.exists():
                                try:
                                    shutil.copy2(self.db_path, backup_file)
                                except Exception as exc:
                                    logger.warning("Could not create schema backup: %s", exc)
                conn.execute(
                    f"INSERT OR REPLACE INTO mesh_meta (key, value) VALUES ('schema_version', '{SCHEMA_VERSION}')"
                )
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
                        updated_at      TEXT,
                        accessed_at     TEXT NOT NULL,
                        access_count    INTEGER NOT NULL DEFAULT 0,
                        ttl_days        REAL,
                        decay_start_at  TEXT,
                        local_only      INTEGER NOT NULL DEFAULT 0,
                        embedding       BLOB,
                        embedding_model TEXT NOT NULL DEFAULT 'unknown',
                        trust_tier      TEXT NOT NULL DEFAULT 'user',
                        quarantined     INTEGER NOT NULL DEFAULT 0,
                        secret_ref      TEXT
                    )
                    """
                )

                existing_cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()
                }
                for col_name, col_sql in (
                    ("updated_at", "ALTER TABLE memories ADD COLUMN updated_at TEXT"),
                    ("ttl_days", "ALTER TABLE memories ADD COLUMN ttl_days REAL"),
                    ("decay_start_at", "ALTER TABLE memories ADD COLUMN decay_start_at TEXT"),
                    ("local_only", "ALTER TABLE memories ADD COLUMN local_only INTEGER DEFAULT 0"),
                    ("embedding", "ALTER TABLE memories ADD COLUMN embedding BLOB"),
                    ("embedding_model", "ALTER TABLE memories ADD COLUMN embedding_model TEXT DEFAULT 'unknown'"),
                    ("trust_tier", "ALTER TABLE memories ADD COLUMN trust_tier TEXT DEFAULT 'user'"),
                    ("quarantined", "ALTER TABLE memories ADD COLUMN quarantined INTEGER DEFAULT 0"),
                    ("secret_ref", "ALTER TABLE memories ADD COLUMN secret_ref TEXT"),
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
                        timestamp       TEXT NOT NULL,
                        private_query   INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                existing_q_cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(queries)").fetchall()
                }
                if "private_query" not in existing_q_cols:
                    try:
                        conn.execute(
                            "ALTER TABLE queries ADD COLUMN private_query INTEGER NOT NULL DEFAULT 0"
                        )
                    except sqlite3.OperationalError:
                        pass

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

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS work_items (
                        id              TEXT PRIMARY KEY,
                        short_id        TEXT NOT NULL,
                        namespace       TEXT NOT NULL,
                        kind            TEXT NOT NULL CHECK(kind IN ('goal', 'task')),
                        parent_id       TEXT,
                        title           TEXT NOT NULL,
                        description     TEXT NOT NULL DEFAULT '',
                        status          TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo', 'in_progress', 'blocked', 'done', 'cancelled')),
                        priority        TEXT NOT NULL DEFAULT 'medium' CHECK(priority IN ('low', 'medium', 'high', 'critical')),
                        created_by      TEXT NOT NULL,
                        created_at      TEXT NOT NULL,
                        started_at      TEXT,
                        completed_at    TEXT,
                        completed_by    TEXT,
                        summary         TEXT,
                        evidence        TEXT NOT NULL DEFAULT '[]',
                        tags            TEXT NOT NULL DEFAULT '[]',
                        local_only      INTEGER NOT NULL DEFAULT 0,
                        FOREIGN KEY(parent_id) REFERENCES work_items(id) ON DELETE SET NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS work_events (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        item_id     TEXT NOT NULL,
                        namespace   TEXT NOT NULL,
                        event_type  TEXT NOT NULL,
                        old_status  TEXT,
                        new_status  TEXT,
                        note        TEXT,
                        agent_id    TEXT NOT NULL,
                        timestamp   TEXT NOT NULL,
                        FOREIGN KEY(item_id) REFERENCES work_items(id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_work_items_ns_status ON work_items (namespace, status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_work_items_ns_completed ON work_items (namespace, completed_at DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_work_items_short_id ON work_items (namespace, short_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_work_events_item ON work_events (item_id, timestamp DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_work_events_ns_ts ON work_events (namespace, timestamp DESC)")

                mem_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE namespace = ?", (self.namespace,)
                    ).fetchone()[0]
                )
                public_unencrypted_mem_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE COALESCE(local_only, 0) = 0 AND content NOT LIKE 'enc:v1:%'"
                    ).fetchone()[0]
                )

                # Finding S5: Full-Text Search (FTS5) table synchronized with `memories.rowid` ONLY for
                # public, unencrypted rows so private (`local_only=1`) and encrypted content never
                # leaks into FTS5 shadow tables on disk.
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
                    if enc_on:
                        conn.execute("DELETE FROM memories_fts")
                    elif public_unencrypted_mem_count > 0:
                        # Purge any historical local_only=1 or encrypted rows from FTS5
                        conn.execute(
                            "DELETE FROM memories_fts WHERE rowid IN "
                            "(SELECT rowid FROM memories WHERE COALESCE(local_only, 0) != 0 OR content LIKE 'enc:v1:%')"
                        )
                        fts_count = int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0])
                        misaligned = conn.execute(
                            """
                            SELECT 1 FROM memories m
                            LEFT JOIN memories_fts f ON f.rowid = m.rowid
                            WHERE COALESCE(m.local_only, 0) = 0
                              AND m.content NOT LIKE 'enc:v1:%'
                              AND (f.rowid IS NULL OR f.id != m.id)
                            LIMIT 1
                            """
                        ).fetchone()
                        if fts_count != public_unencrypted_mem_count or misaligned is not None:
                            conn.execute("DELETE FROM memories_fts")
                            conn.execute(
                                """
                                INSERT INTO memories_fts (rowid, id, content, tags, memory_type, source_agent)
                                SELECT rowid, id, content, tags, memory_type, source_agent
                                FROM memories
                                WHERE COALESCE(local_only, 0) = 0 AND content NOT LIKE 'enc:v1:%'
                                """
                            )
                    else:
                        conn.execute("DELETE FROM memories_fts")
                    self._fts_enabled = True
                    self._fts_dirty = False
                except sqlite3.OperationalError as exc:
                    if _is_permanent_fts_error(exc):
                        self._fts_enabled = False
                    else:
                        self._fts_dirty = True
                    logger.warning("FTS5 synchronization deferred for %s: %s", self.db_path, exc)

                if conn.in_transaction:
                    conn.commit()
            secure_file_permissions(self.db_path)

            if mem_count == 0 and (self.db_path, self.namespace) not in _VECTOR_CACHE:
                stamp = _get_db_stamp(self.db_path)
                if stamp is not None:
                    key = (self.db_path, self.namespace)
                    _VECTOR_CACHE[key] = _VectorCacheEntry(
                        stamp=stamp,
                        ids=[],
                        matrix=None,
                        all_ids=set(),
                        rows_by_id={},
                        data_version=self._get_data_version_locked(),
                        content_version=self._read_content_version_locked() or 1,
                        needs_healing_ids=set(),
                    )
                    _VECTOR_CACHE.move_to_end(key)
                    global _CACHE_REBUILD_COUNT
                    _CACHE_REBUILD_COUNT = max(0, _CACHE_REBUILD_COUNT - 1)
                    _VectorCacheEntry.build_count = max(0, _VectorCacheEntry.build_count - 1)
                    evicted.extend(self._trim_vector_cache_locked())

            if mem_count > 0:
                self._migrate_from_chroma_if_needed()

        if evicted:
            _finalize_evicted_resources(evicted)

    def _trim_vector_cache_locked(
        self,
    ) -> list[tuple[Path, Optional[_PooledConnection], Optional[str]]]:
        evicted: list[tuple[Path, Optional[_PooledConnection], Optional[str]]] = []
        max_vec = _get_max_vector_cache_size()
        active_key = (self.db_path, self.namespace)
        while len(_VECTOR_CACHE) > max_vec:
            victim_key: Optional[tuple[Path, str]] = None
            for cand_key in _VECTOR_CACHE:
                if cand_key != active_key and cand_key[1] != self.namespace:
                    victim_key = cand_key
                    break
            if victim_key is None:
                break
            _VECTOR_CACHE.pop(victim_key, None)
            v_path, v_ns = victim_key
            still_used = any(k[0] == v_path for k in _VECTOR_CACHE)
            v_conn: Optional[_PooledConnection] = None
            if not still_used and v_path != self.db_path:
                v_conn = _CONNECTION_POOL.pop(v_path, None)
                _DB_PATH_NAMESPACES.pop(v_path, None)
                _QUERY_WRITE_COUNTS.pop(v_path, None)
                dict.pop(_INITIALIZED_DBS, v_path, None)
            evicted.append((v_path, v_conn, v_ns))
        return evicted

    def _migrate_from_chroma_if_needed(self) -> None:
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
                import chromadb  # pyright: ignore[reportMissingImports]

                client = chromadb.PersistentClient(path=str(chroma_dir))
                col = client.get_collection(name=f"mesh_{self.namespace}")
                data = col.get(ids=missing_ids, include=["embeddings"])
                got_ids = data.get("ids") or []
                got_embs = data.get("embeddings") or []
                if got_ids and got_embs:
                    with self._connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        for mid, emb in zip(got_ids, got_embs):
                            if emb is not None and len(emb) == EMBEDDING_DIM:
                                conn.execute(
                                    "UPDATE memories SET embedding = ?, embedding_model = 'onnx-minilm-l6-v2' WHERE id = ?",
                                    (_vector_to_blob(emb), mid),
                                )
                        self._bump_content_version_in_tx(conn)
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
        local_only: bool = False,
    ) -> None:
        """
        Synchronize an FTS5 entry in O(log N) time using integer `rowid`.
        Finding S5: Never writes `local_only=True` or encrypted rows into `memories_fts`.
        """
        if not self._fts_enabled:
            return
        try:
            if old_rowid is not None:
                conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (old_rowid,))
            if new_rowid is None:
                new_rowid = self._get_memory_rowid(conn, memory_id)
            if new_rowid is not None:
                conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (new_rowid,))
            else:
                conn.execute("DELETE FROM memories_fts WHERE id = ?", (memory_id,))

            # Suppress on-disk FTS5 indexing for private memories or when at-rest encryption is active
            if local_only or is_encryption_enabled() or is_encrypted_text(content):
                return

            if new_rowid is not None:
                conn.execute(
                    """
                    INSERT INTO memories_fts (rowid, id, content, tags, memory_type, source_agent)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (new_rowid, memory_id, content, tags, memory_type, source_agent),
                )
            else:
                conn.execute(
                    "INSERT INTO memories_fts (id, content, tags, memory_type, source_agent) VALUES (?, ?, ?, ?, ?)",
                    (memory_id, content, tags, memory_type, source_agent),
                )
        except sqlite3.OperationalError as exc:
            if _is_permanent_fts_error(exc):
                self._fts_enabled = False
            else:
                self._fts_dirty = True

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
        except sqlite3.OperationalError as exc:
            if _is_permanent_fts_error(exc):
                self._fts_enabled = False
            else:
                self._fts_dirty = True

    # --- In-Memory Vector & Metadata Matrix Cache ---

    def _invalidate_cache(self) -> None:
        with _CACHE_LOCK:
            _evict_vector_cache_for_db_path(self.db_path)
            stamp = _get_db_stamp(self.db_path)
            if stamp is not None:
                _INITIALIZED_DBS[self.db_path] = stamp
            else:
                _INITIALIZED_DBS.pop(self.db_path, None)

    def _get_vector_cache(self, force_refresh: bool = False) -> _VectorCacheEntry:
        """
        Return the normalized 384-dim float32 embedding matrix, decrypted row metadata cache,
        and pre-computed lexical IDF cache for this namespace.
        """
        evicted: list[tuple[Path, Optional[_PooledConnection], Optional[str]]] = []
        with _CACHE_LOCK:
            if not self.db_path.exists():
                if not self.create:
                    raise NamespaceNotFoundError(f"Namespace '{self.namespace}' does not exist.")
                self._ensure_initialized()
            if not force_refresh:
                synced = self._is_cache_synced_locked()
                if synced is not None:
                    _INITIALIZED_DBS[self.db_path] = synced.stamp
                    return synced

            prev_cache = _VECTOR_CACHE.get((self.db_path, self.namespace))
            stamp_before = _get_db_stamp(self.db_path)

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                try:
                    cv = self._read_content_version_locked(conn)
                    rows = conn.execute(
                        f"SELECT {_MEMORY_COLUMNS}, embedding FROM memories WHERE namespace = ?",
                        (self.namespace,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    self._init_db()
                    stamp_before = _get_db_stamp(self.db_path)
                    cv = self._read_content_version_locked(conn)
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
                r_keys = r.keys()
                content_plain = decrypt_text_if_encrypted(r["content"])
                tags_str = decrypt_text_if_encrypted(r["tags"] or "[]")
                blob = r["embedding"]
                emb_model = str(r["embedding_model"] or "unknown")
                trust_tier = _normalize_trust_tier(
                    r["trust_tier"] if "trust_tier" in r_keys else "user",
                    default="user",
                )
                quarantined_bool = bool(r["quarantined"]) if "quarantined" in r_keys else False
                secret_ref_dict = (
                    _parse_secret_ref_json(r["secret_ref"]) if "secret_ref" in r_keys else None
                )

                vec: Optional[np.ndarray] = None
                if prev_cache is not None and mid in prev_cache.rows_by_id:
                    prev_meta = prev_cache.rows_by_id[mid]
                    if (
                        prev_meta.get("content") == content_plain
                        and prev_meta.get("tags") == tags_str
                        and prev_meta.get("embedding_model") == emb_model
                    ):
                        if mid in prev_cache.lexical_by_id:
                            reused_lexical[mid] = prev_cache.lexical_by_id[mid]

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
                    "content": content_plain,
                    "confidence": float(r["confidence"]),
                    "memory_type": r["memory_type"],
                    "source_agent": r["source_agent"],
                    "tags": tags_str,
                    "tags_list": _parse_tags_json(tags_str),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"] if "updated_at" in r_keys else r["created_at"],
                    "accessed_at": r["accessed_at"],
                    "access_count": int(r["access_count"] or 0),
                    "ttl_days": r["ttl_days"],
                    "decay_start_at": r["decay_start_at"],
                    "local_only": bool(r["local_only"]),
                    "embedding_model": emb_model,
                    "trust_tier": trust_tier,
                    "quarantined": quarantined_bool,
                    "secret_ref": secret_ref_dict,
                }
                rows_by_id[mid] = row_meta
                if _row_needs_healing(row_meta, vec):
                    needs_healing_ids.add(mid)

                if vec is not None:
                    ids.append(mid)
                    vecs.append(vec)

            stamp = stamp_before or (0, 0, 0, 0)
            dv = self._get_data_version_locked()
            matrix = np.ascontiguousarray(np.vstack(vecs), dtype=np.float32) if vecs else None
            entry = _VectorCacheEntry(
                stamp=stamp,
                ids=ids,
                matrix=matrix,
                all_ids=all_ids,
                rows_by_id=rows_by_id,
                data_version=dv,
                content_version=cv,
                needs_healing_ids=needs_healing_ids,
                reused_lexical=reused_lexical,
            )
            key = (self.db_path, self.namespace)
            _VECTOR_CACHE[key] = entry
            _VECTOR_CACHE.move_to_end(key)
            _INITIALIZED_DBS[self.db_path] = stamp
            evicted = self._trim_vector_cache_locked()

        if evicted:
            _finalize_evicted_resources(evicted)
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
        allow_redacted_public: bool = False,
    ) -> bool:
        """
        Store or overwrite a memory and its normalized 384-dim vector in a single atomic transaction,
        enforcing `guard_ingest` & `guard_tags` and encrypting at rest when enabled (Findings S5, S8, S11).

        Returns the effective ``local_only`` value persisted after guards so callers can
        report the stored privacy level (Issue C-002).
        """
        memory_id = validate_memory_id(memory_id)
        self._ensure_initialized()
        now = _utc_now_iso()
        norm_vec = _normalize_vector(embedding, enforce_dim=True)
        raw_blob = norm_vec.tobytes()

        tags_raw = metadata.get("tags", [])
        if isinstance(tags_raw, str):
            raw_tags_list = _parse_tags_json(tags_raw)
        else:
            raw_tags_list = [str(t).strip() for t in (tags_raw or []) if str(t).strip()]

        req_local = bool(local_only or metadata.get("local_only"))
        content, tags_list, final_local = _enforce_store_guard(
            content, raw_tags_list, req_local, allow_redacted_public=allow_redacted_public
        )
        tags_str = json.dumps(tags_list)
        local_flag = 1 if final_local else 0

        mem_type = str(metadata.get("memory_type", "fact"))
        source_agent = str(metadata.get("source_agent", "unknown"))
        confidence = float(metadata.get("confidence", 1.0))
        created_at = metadata.get("created_at") or now
        updated_at = metadata.get("updated_at") or created_at
        accessed_at = metadata.get("accessed_at") or now
        access_count = max(0, int(metadata.get("access_count", 0)))
        ttl_days = metadata.get("ttl_days")
        decay_start_at = metadata.get("decay_start_at")
        emb_model = str(
            embedding_model
            or metadata.get("embedding_model")
            or ("hash-384" if np.all(norm_vec[368:] == 0.0) else "onnx-minilm-l6-v2")
        )
        trust_tier = _normalize_trust_tier(metadata.get("trust_tier"), default="user")
        quarantined_flag = 1 if metadata.get("quarantined") else 0
        norm_ref = normalize_secret_ref(metadata.get("secret_ref"), default_namespace=self.namespace)
        secret_ref_json = json.dumps(norm_ref, sort_keys=True) if norm_ref else None

        enc_on = is_encryption_enabled()
        db_content = encrypt_text_for_storage(content) if enc_on else content
        db_tags = encrypt_text_for_storage(tags_str) if enc_on else tags_str
        db_blob = encrypt_blob_for_storage(raw_blob) if enc_on else raw_blob
        db_ref = (
            encrypt_text_for_storage(secret_ref_json)
            if (enc_on and secret_ref_json)
            else secret_ref_json
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
            "updated_at": updated_at,
            "accessed_at": accessed_at,
            "access_count": access_count,
            "ttl_days": ttl_days,
            "decay_start_at": decay_start_at,
            "local_only": bool(local_flag),
            "embedding_model": emb_model,
            "trust_tier": trust_tier,
            "quarantined": bool(quarantined_flag),
            "secret_ref": norm_ref,
        }

        with namespace_write_lock(self.namespace):
            with _CACHE_LOCK:
                synced_cache = self._is_cache_synced_locked()
                new_cv: Optional[int] = None
                audit_scrub_old_content: Optional[str] = None
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        old_row = conn.execute(
                            "SELECT content, local_only FROM memories WHERE id = ? AND namespace = ?",
                            (memory_id, self.namespace),
                        ).fetchone()
                        old_rowid = self._get_memory_rowid(conn, memory_id)
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO memories
                            (id, namespace, content, confidence, memory_type,
                             source_agent, tags, created_at, updated_at, accessed_at, access_count,
                             ttl_days, decay_start_at, local_only, embedding, embedding_model,
                             trust_tier, quarantined, secret_ref)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                memory_id,
                                self.namespace,
                                db_content,
                                confidence,
                                mem_type,
                                source_agent,
                                db_tags,
                                created_at,
                                updated_at,
                                accessed_at,
                                access_count,
                                ttl_days,
                                decay_start_at,
                                local_flag,
                                db_blob,
                                emb_model,
                                trust_tier,
                                quarantined_flag,
                                db_ref,
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
                            local_only=bool(local_flag),
                        )
                        if old_row is not None:
                            old_content = decrypt_text_if_encrypted(str(old_row[0] or ""))
                            old_local = bool(old_row[1])
                            if old_content != content or (local_flag and not old_local):
                                _scrub_contradictions_in_tx(
                                    conn,
                                    self.namespace,
                                    memory_id,
                                    REDACTED_CONTENT_PLACEHOLDER if local_flag else content,
                                )
                                _scrub_queries_in_tx(conn, self.namespace, old_content)
                                _scrub_pending_query_telemetry(self.namespace, old_content)
                                audit_scrub_old_content = old_content
                        new_cv = self._bump_content_version_in_tx(conn)
                        conn.commit()
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        raise

                if synced_cache is not None and synced_cache.upsert_vector(
                    memory_id, norm_vec, row_meta=row_meta
                ):
                    self._refresh_stamps_after_write_locked(synced_cache, content_version=new_cv)
                else:
                    self._invalidate_cache()

        if audit_scrub_old_content is not None:
            scrub_memory_update_from_audit(
                namespace=self.namespace,
                memory_id=memory_id,
                old_content=audit_scrub_old_content,
                local_only=bool(local_flag),
            )
        return bool(final_local)

    def upsert_batch(
        self,
        items: list[tuple[str, str, list[float], dict[str, Any], bool]],
    ) -> None:
        """
        Batch-insert or overwrite multiple memories in a single SQLite WAL transaction,
        enforcing `guard_ingest` & `guard_tags` and encrypting at rest when enabled.
        """
        if not items:
            return
        self._ensure_initialized()
        now = _utc_now_iso()
        enc_on = is_encryption_enabled()

        with namespace_write_lock(self.namespace):
            with _CACHE_LOCK:
                synced_cache = self._is_cache_synced_locked()
                in_place_ok = synced_cache is not None
                pending_vectors: list[tuple[str, Optional[np.ndarray], dict[str, Any]]] = []
                new_cv: Optional[int] = None
                audit_scrubs: list[tuple[str, str, bool]] = []
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        for raw_memory_id, raw_content, embedding, metadata, local_only in items:
                            memory_id = validate_memory_id(raw_memory_id)
                            norm_vec = _normalize_vector(embedding, enforce_dim=True)
                            raw_blob = norm_vec.tobytes()

                            tags_raw = metadata.get("tags", [])
                            if isinstance(tags_raw, str):
                                raw_tags_list = _parse_tags_json(tags_raw)
                            else:
                                raw_tags_list = [str(t).strip() for t in (tags_raw or []) if str(t).strip()]

                            req_local = bool(local_only or metadata.get("local_only"))
                            content, tags_list, final_local = _enforce_store_guard(
                                raw_content, raw_tags_list, req_local
                            )
                            tags_str = json.dumps(tags_list)
                            local_flag = 1 if final_local else 0

                            mem_type = str(metadata.get("memory_type", "fact"))
                            source_agent = str(metadata.get("source_agent", "unknown"))
                            confidence = float(metadata.get("confidence", 1.0))
                            created_at = metadata.get("created_at") or now
                            updated_at = metadata.get("updated_at") or created_at
                            accessed_at = metadata.get("accessed_at") or now
                            access_count = max(0, int(metadata.get("access_count", 0)))
                            ttl_days = metadata.get("ttl_days")
                            decay_start_at = metadata.get("decay_start_at")
                            emb_model = str(
                                metadata.get("embedding_model")
                                or ("hash-384" if np.all(norm_vec[368:] == 0.0) else "onnx-minilm-l6-v2")
                            )
                            trust_tier = _normalize_trust_tier(metadata.get("trust_tier"), default="user")
                            quarantined_flag = 1 if metadata.get("quarantined") else 0
                            norm_ref = normalize_secret_ref(
                                metadata.get("secret_ref"), default_namespace=self.namespace
                            )
                            secret_ref_json = json.dumps(norm_ref, sort_keys=True) if norm_ref else None

                            db_content = encrypt_text_for_storage(content) if enc_on else content
                            db_tags = encrypt_text_for_storage(tags_str) if enc_on else tags_str
                            db_blob = encrypt_blob_for_storage(raw_blob) if enc_on else raw_blob
                            db_ref = (
                                encrypt_text_for_storage(secret_ref_json)
                                if (enc_on and secret_ref_json)
                                else secret_ref_json
                            )

                            old_row = conn.execute(
                                "SELECT content, local_only FROM memories WHERE id = ? AND namespace = ?",
                                (memory_id, self.namespace),
                            ).fetchone()
                            old_rowid = self._get_memory_rowid(conn, memory_id)
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO memories
                                (id, namespace, content, confidence, memory_type,
                                 source_agent, tags, created_at, updated_at, accessed_at, access_count,
                                 ttl_days, decay_start_at, local_only, embedding, embedding_model,
                                 trust_tier, quarantined, secret_ref)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    memory_id,
                                    self.namespace,
                                    db_content,
                                    confidence,
                                    mem_type,
                                    source_agent,
                                    db_tags,
                                    created_at,
                                    updated_at,
                                    accessed_at,
                                    access_count,
                                    ttl_days,
                                    decay_start_at,
                                    local_flag,
                                    db_blob,
                                    emb_model,
                                    trust_tier,
                                    quarantined_flag,
                                    db_ref,
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
                                local_only=bool(local_flag),
                            )
                            if old_row is not None:
                                old_content = decrypt_text_if_encrypted(str(old_row[0] or ""))
                                old_local = bool(old_row[1])
                                if old_content != content or (local_flag and not old_local):
                                    _scrub_contradictions_in_tx(
                                        conn,
                                        self.namespace,
                                        memory_id,
                                        REDACTED_CONTENT_PLACEHOLDER if local_flag else content,
                                    )
                                    _scrub_queries_in_tx(conn, self.namespace, old_content)
                                    _scrub_pending_query_telemetry(self.namespace, old_content)
                                    audit_scrubs.append((memory_id, old_content, bool(local_flag)))
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
                                    "updated_at": updated_at,
                                    "accessed_at": accessed_at,
                                    "access_count": access_count,
                                    "ttl_days": ttl_days,
                                    "decay_start_at": decay_start_at,
                                    "local_only": bool(local_flag),
                                    "embedding_model": emb_model,
                                    "trust_tier": trust_tier,
                                    "quarantined": bool(quarantined_flag),
                                    "secret_ref": norm_ref,
                                }
                                pending_vectors.append((memory_id, norm_vec, row_meta))
                        new_cv = self._bump_content_version_in_tx(conn)
                        conn.commit()
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        raise

                if in_place_ok and synced_cache is not None:
                    for memory_id, vec, meta in pending_vectors:
                        if not synced_cache.upsert_vector(memory_id, vec, row_meta=meta):
                            in_place_ok = False
                            break
                if in_place_ok and synced_cache is not None:
                    self._refresh_stamps_after_write_locked(synced_cache, content_version=new_cv)
                else:
                    self._invalidate_cache()

        for scrubbed_id, scrubbed_old_content, scrubbed_local in audit_scrubs:
            scrub_memory_update_from_audit(
                namespace=self.namespace,
                memory_id=scrubbed_id,
                old_content=scrubbed_old_content,
                local_only=scrubbed_local,
            )

    def resolve_id_prefix(self, prefix: str) -> Optional[str]:
        clean_pref = (prefix or "").strip().lower()
        if not clean_pref:
            return None
        if not self.db_path.exists():
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
        if not self.db_path.exists():
            return None
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
        trust_tier: Optional[str] = None,
        quarantined: Optional[bool] = None,
        secret_ref: Optional[Any] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Update an existing memory in-place while preserving its `id`, `created_at`,
        `source_agent` (unless explicitly provided), and `access_count`.
        Finding S11: Always enforces `guard_ingest` and `guard_tags`, including when
        `content is None` and `local_only=False` is requested.
        """
        self._ensure_initialized()
        self._maybe_lazy_flush_telemetry()
        now = _utc_now_iso()
        norm_vec: Optional[np.ndarray] = (
            _normalize_vector(embedding, enforce_dim=True) if embedding is not None else None
        )

        old_content_for_scrub: Optional[str] = None
        should_scrub_audit = False
        new_local_bool = False
        enc_on = is_encryption_enabled()

        with namespace_write_lock(self.namespace):
            with _CACHE_LOCK:
                cache = self._get_vector_cache()
                existing_meta = cache.rows_by_id.get(memory_id)
                if existing_meta is None:
                    return None

                old_content = str(existing_meta.get("content", ""))
                candidate_content = content if content is not None else old_content
                candidate_tags = (
                    [str(t).strip() for t in tags if str(t).strip()]
                    if tags is not None
                    else (existing_meta.get("tags_list") or _parse_tags_json(existing_meta["tags"]))
                )
                requested_local = (
                    bool(local_only)
                    if local_only is not None
                    else bool(existing_meta.get("local_only"))
                )

                # Finding S11: Run central guard on both new and existing content/tags
                # If caller explicitly passed new `content` with `[REDACTED]` (e.g. test_v06), allow
                # `local_only=True` as requested; if `content is None` and caller tries to flip
                # a redacted credential memory to `local_only=False`, `_enforce_store_guard` forces `True`.
                allow_redacted_pub = content is not None and local_only is False
                new_content, new_tags_list, guarded_local = _enforce_store_guard(
                    candidate_content,
                    candidate_tags,
                    requested_local,
                    allow_redacted_public=allow_redacted_pub,
                )
                new_tags_str = json.dumps(new_tags_list)
                new_local = 1 if guarded_local else 0
                new_local_bool = bool(new_local)

                new_type = memory_type if memory_type is not None else existing_meta["memory_type"]
                new_conf = (
                    float(confidence)
                    if confidence is not None
                    else float(existing_meta["confidence"])
                )
                new_agent = source_agent if source_agent is not None else existing_meta["source_agent"]
                new_trust_tier = (
                    _normalize_trust_tier(trust_tier, default=existing_meta.get("trust_tier", "user"))
                    if trust_tier is not None
                    else existing_meta.get("trust_tier", "user")
                )
                new_quarantined_bool = (
                    bool(quarantined)
                    if quarantined is not None
                    else bool(existing_meta.get("quarantined", False))
                )
                new_quarantined_flag = 1 if new_quarantined_bool else 0
                new_secret_ref = (
                    normalize_secret_ref(secret_ref, default_namespace=self.namespace)
                    if secret_ref is not None
                    else existing_meta.get("secret_ref")
                )
                new_secret_ref_json = (
                    json.dumps(new_secret_ref, sort_keys=True) if new_secret_ref else None
                )

                if clear_ttl:
                    new_ttl = None
                    new_decay_start = None
                elif reset_ttl or ttl_days is not None:
                    if ttl_days is not None:
                        new_ttl = ttl_days
                        new_decay_start = now if ttl_days is not None else None
                    else:
                        old_ttl = existing_meta["ttl_days"]
                        new_ttl = old_ttl
                        new_decay_start = now if old_ttl is not None else None
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
                new_updated_at = (
                    existing_meta.get("updated_at") or existing_meta["created_at"]
                    if preserve_accessed_at
                    else now
                )
                content_changed = new_content != old_content
                became_private = new_local_bool and not bool(existing_meta.get("local_only"))

                if content_changed or new_local_bool:
                    old_content_for_scrub = old_content
                    should_scrub_audit = True

                db_content = encrypt_text_for_storage(new_content) if enc_on else new_content
                db_tags = encrypt_text_for_storage(new_tags_str) if enc_on else new_tags_str
                db_ref = (
                    encrypt_text_for_storage(new_secret_ref_json)
                    if (enc_on and new_secret_ref_json)
                    else new_secret_ref_json
                )

                new_cv: Optional[int] = None
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        rowid = self._get_memory_rowid(conn, memory_id)
                        if norm_vec is not None:
                            raw_blob = norm_vec.tobytes()
                            db_blob = encrypt_blob_for_storage(raw_blob) if enc_on else raw_blob
                            conn.execute(
                                """
                                UPDATE memories
                                SET content = ?,
                                    confidence = ?,
                                    memory_type = ?,
                                    source_agent = ?,
                                    tags = ?,
                                    updated_at = ?,
                                    accessed_at = ?,
                                    ttl_days = ?,
                                    decay_start_at = ?,
                                    local_only = ?,
                                    embedding = ?,
                                    embedding_model = ?,
                                    trust_tier = ?,
                                    quarantined = ?,
                                    secret_ref = ?
                                WHERE id = ? AND namespace = ?
                                """,
                                (
                                    db_content,
                                    new_conf,
                                    new_type,
                                    new_agent,
                                    db_tags,
                                    new_updated_at,
                                    new_accessed_at,
                                    new_ttl,
                                    new_decay_start,
                                    new_local,
                                    db_blob,
                                    new_emb_model,
                                    new_trust_tier,
                                    new_quarantined_flag,
                                    db_ref,
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
                                    updated_at = ?,
                                    accessed_at = ?,
                                    ttl_days = ?,
                                    decay_start_at = ?,
                                    local_only = ?,
                                    embedding_model = ?,
                                    trust_tier = ?,
                                    quarantined = ?,
                                    secret_ref = ?
                                WHERE id = ? AND namespace = ?
                                """,
                                (
                                    db_content,
                                    new_conf,
                                    new_type,
                                    new_agent,
                                    db_tags,
                                    new_updated_at,
                                    new_accessed_at,
                                    new_ttl,
                                    new_decay_start,
                                    new_local,
                                    new_emb_model,
                                    new_trust_tier,
                                    new_quarantined_flag,
                                    db_ref,
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
                            local_only=new_local_bool,
                        )
                        if content_changed or became_private:
                            replacement_text = (
                                REDACTED_CONTENT_PLACEHOLDER if new_local_bool else new_content
                            )
                            _scrub_contradictions_in_tx(
                                conn, self.namespace, memory_id, replacement_text
                            )
                            _scrub_queries_in_tx(conn, self.namespace, old_content)
                            _scrub_pending_query_telemetry(self.namespace, old_content)
                        new_cv = self._bump_content_version_in_tx(conn)
                        conn.commit()
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        raise

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
                    "updated_at": new_updated_at,
                    "accessed_at": new_accessed_at,
                    "access_count": int(existing_meta["access_count"] or 0),
                    "ttl_days": new_ttl,
                    "decay_start_at": new_decay_start,
                    "local_only": new_local_bool,
                    "embedding_model": new_emb_model,
                    "trust_tier": new_trust_tier,
                    "quarantined": new_quarantined_bool,
                    "secret_ref": new_secret_ref,
                }
                if norm_vec is not None:
                    if cache.upsert_vector(memory_id, norm_vec, row_meta=updated_row):
                        self._refresh_stamps_after_write_locked(cache, content_version=new_cv)
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
                    self._refresh_stamps_after_write_locked(cache, content_version=new_cv)

        if should_scrub_audit and not preserve_accessed_at:
            scrub_memory_update_from_audit(
                namespace=self.namespace,
                memory_id=memory_id,
                old_content=old_content_for_scrub,
                local_only=new_local_bool,
            )
        return dict(updated_row)

    def approve(self, memory_id: str) -> Optional[dict[str, Any]]:
        """Approve a quarantined memory (`quarantined=False`), preserving its provenance `trust_tier` (Finding S8)."""
        resolved = self.resolve_id_prefix(memory_id) or memory_id
        return self.update(memory_id=resolved, quarantined=False, preserve_accessed_at=True)

    # --- Vector & Hybrid Search Operations ---

    def query(
        self,
        embedding: list[float],
        n: int = 5,
        query_embedding_model: Optional[str] = None,
    ) -> dict[str, list[list[Any]]]:
        if not self.db_path.exists():
            return {"documents": [[]], "distances": [[]], "metadatas": [[]], "ids": [[]]}
        cache = self._get_vector_cache()
        if n <= 0:
            return {"documents": [[]], "distances": [[]], "metadatas": [[]], "ids": [[]]}

        q_vec = _normalize_vector(embedding, enforce_dim=False)
        ids_snap, sims, rows_snap = cache.similarity_snapshot(q_vec)
        if sims is None or len(ids_snap) == 0:
            return {"documents": [[]], "distances": [[]], "metadatas": [[]], "ids": [[]]}
        if query_embedding_model is not None:
            # the per-row model tag takes a handful of distinct values: decide each value once, not once per row
            _mix_memo: dict[Any, bool] = {}
            valid_indices = []
            for idx, mid in enumerate(ids_snap):
                _row = rows_snap.get(mid)
                if _row is None:
                    continue
                _em = _row.get("embedding_model")
                _mixed = _mix_memo.get(_em)
                if _mixed is None:
                    _mixed = _is_mixed_space(_em, query_embedding_model)
                    _mix_memo[_em] = _mixed
                if not _mixed:
                    valid_indices.append(idx)
            if len(valid_indices) < len(ids_snap) and not os.environ.get("MESH_QUIET"):
                excluded_cnt = len(ids_snap) - len(valid_indices)
                warnings.warn(
                    f"Excluded {excluded_cnt} memories from semantic search due to mixed embedding space "
                    f"(query uses '{query_embedding_model}'). Run 'mesh reembed' to re-embed historical memories.",
                    UserWarning,
                    stacklevel=2,
                )
            if not valid_indices:
                return {"documents": [[]], "distances": [[]], "metadatas": [[]], "ids": [[]]}
            valid_arr = np.asarray(valid_indices, dtype=np.int64)
            valid_sims = sims[valid_arr]
            k = min(n, len(valid_arr))
            if k < len(valid_arr):
                part = np.argpartition(-valid_sims, k - 1)[:k]
                part = part[np.argsort(-valid_sims[part])]
                top_indices = valid_arr[part]
            else:
                top_indices = valid_arr[np.argsort(-valid_sims)]
        else:
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
                    "embedding_model": row["embedding_model"],
                    "trust_tier": row.get("trust_tier", "user"),
                    "quarantined": 1 if row.get("quarantined") else 0,
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
        if not self._fts_enabled or is_encryption_enabled() or not query_text or not self.db_path.exists():
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
    ) -> tuple[float, float, float, str]:
        if not q_content_tokens:
            return fts_score, 0.0, 0.0, "semantic"

        lex = cached_lex if cached_lex is not None else _build_row_lexical_entry(doc_content, doc_tags)
        doc_norm = lex["doc_norm"]
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
        exact_stem_specific_idf = 0.0
        exact_hits = 0
        synonym_hits = 0
        tag_hits = 0

        for tok, stem in zip(q_content_tokens, q_stems):
            w_idf = idf_map.get(stem, 1.0)
            is_specific = stem not in _FRAMING_TOKENS
            if is_specific:
                specific_total_idf += w_idf

            tok_compact = _TOK_COMPACT_CACHE.get(tok)
            if tok_compact is None:
                tok_compact = re.sub(r"[^a-z0-9]", "", tok)
                if len(_TOK_COMPACT_CACHE) > 4096:
                    _TOK_COMPACT_CACHE.clear()
                _TOK_COMPACT_CACHE[tok] = tok_compact
            tok_syns = syn_lookup.get(tok, set())

            if tok in all_doc_tokens or (len(tok_compact) >= 3 and tok_compact in all_doc_tokens):
                matched_idf += w_idf
                if is_specific:
                    specific_matched_idf += w_idf
                    exact_stem_specific_idf += w_idf
                    exact_hits += 1
                if tok in tag_tokens or stem in tag_stems or (tok_compact and tok_compact in tag_compacts):
                    tag_hits += 1
            elif stem in all_doc_stems:
                matched_idf += w_idf * 0.92
                if is_specific:
                    specific_matched_idf += w_idf * 0.92
                    exact_stem_specific_idf += w_idf * 0.92
                    exact_hits += 1
            else:
                prefix_matched = False
                if is_specific and len(stem) >= 5:
                    for ds in all_doc_stems:
                        if len(ds) >= 5:
                            shorter, longer = (stem, ds) if len(stem) <= len(ds) else (ds, stem)
                            if longer.startswith(shorter) and (len(shorter) / len(longer)) >= 0.70:
                                matched_idf += w_idf * 0.80
                                specific_matched_idf += w_idf * 0.80
                                exact_stem_specific_idf += w_idf * 0.80
                                exact_hits += 1
                                prefix_matched = True
                                break
                if not prefix_matched and tok_syns and (tok_syns & all_doc_tokens or tok_syns & all_doc_stems):
                    matched_idf += w_idf * 0.50
                    if is_specific:
                        specific_matched_idf += w_idf * 0.50
                        synonym_hits += 1
                    if tok_syns & tag_tokens or tok_syns & tag_stems:
                        tag_hits += 1

        idf_coverage = matched_idf / max(1e-9, total_query_idf)
        if specific_total_idf > 0:
            specific_coverage = specific_matched_idf / specific_total_idf
            exact_stem_coverage = exact_stem_specific_idf / specific_total_idf
        else:
            specific_coverage = idf_coverage
            exact_stem_coverage = idf_coverage if exact_hits > 0 else 0.0

        phrase_bonus = 0.0
        if len(q_content_tokens) >= 2 and exact_stem_coverage > 0.0:
            if len(q_norm_phrase) >= 4 and _phrase_pattern(q_norm_phrase).search(doc_norm):
                phrase_bonus = 0.25
            elif len(q_compact) >= 6 and q_compact in all_doc_tokens:
                phrase_bonus = 0.22

        bm25_tf_norm = min(1.0, 6.0 / (doc_len + 4.0)) * 0.12 if exact_stem_coverage > 0 else 0.0
        computed_lex = min(
            1.0,
            0.65 * specific_coverage + 0.35 * idf_coverage + phrase_bonus + bm25_tf_norm,
        )
        if exact_stem_coverage > 0:
            final_lex = max(computed_lex, fts_score * exact_stem_coverage)
        elif specific_coverage > 0:
            final_lex = max(computed_lex, fts_score * specific_coverage * 0.5)
        else:
            final_lex = computed_lex * 0.25

        if exact_hits > 0 and exact_stem_coverage >= 0.5:
            reason = "exact+semantic"
        elif synonym_hits > 0 and exact_hits == 0:
            reason = "synonym+semantic"
        elif tag_hits > 0:
            reason = "tag+semantic"
        elif specific_coverage > 0:
            reason = "lexical+semantic"
        else:
            reason = "semantic"

        return (
            round(min(1.0, final_lex), 4),
            round(min(1.0, specific_coverage), 4),
            round(min(1.0, exact_stem_coverage), 4),
            reason,
        )

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
        include_quarantined: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Execute a 5-stage calibrated hybrid retrieval with pre-cached lexical IDF.
        Finding S8: Excludes `quarantined=True` memories by default (`include_quarantined=False`).
        """
        if not self.db_path.exists() or n <= 0:
            return []
        self._maybe_lazy_flush_telemetry()
        q_vec = _normalize_vector(embedding, enforce_dim=False)
        cache = self._get_vector_cache()
        use_hash_sim = False
        with _CACHE_LOCK:
            ids_snap = list(cache.ids)
            mat = cache.matrix
            if (query_embedding_model == "hash-384" or mat is None) and cache._hash_matrix_buf is not None and len(ids_snap) > 0 and q_vec.ndim == 1 and q_vec.shape[0] == 384:
                mat = cache._hash_matrix_buf[: len(ids_snap)]
                use_hash_sim = True
            if mat is None or len(ids_snap) == 0 or q_vec.ndim != 1 or q_vec.shape[0] != mat.shape[1]:
                return []
            sims = mat @ q_vec
            rows_snap = dict(cache.rows_by_id)
            lex_snap = dict(cache.lexical_by_id)
            stem_df_snap = dict(cache.stem_df)

        type_filter = memory_type.strip().lower() if memory_type else None
        filter_tags = {t.strip().lower() for t in (tags or []) if t.strip()}
        ref_now = now if now is not None else _utc_now()

        masked_indices: list[int] = []
        masked_mid_set: set[str] = set()
        _no_row_filters = (
            not type_filter
            and not source_agent
            and not filter_tags
            and include_private
            and include_stale
            and min_confidence <= 0.0
        )
        if _no_row_filters:
            # Fast path: only the quarantine flag can exclude a row (comprehensions run ~2x faster than the
            # per-row append loop; same rows, same order).
            if include_quarantined:
                masked_indices = [idx for idx, mid in enumerate(ids_snap) if mid in rows_snap]
            else:
                masked_indices = [
                    idx
                    for idx, mid in enumerate(ids_snap)
                    if (_r := rows_snap.get(mid)) is not None and not _r.get("quarantined")
                ]
            masked_mid_set = {ids_snap[idx] for idx in masked_indices}
        else:
            for idx, mid in enumerate(ids_snap):
                cached_row = rows_snap.get(mid)
                if cached_row is None:
                    continue
                if not include_quarantined and cached_row.get("quarantined"):
                    continue
                if type_filter and cached_row["memory_type"].lower() != type_filter:
                    continue
                if source_agent and cached_row["source_agent"] != source_agent:
                    continue
                if not include_private and cached_row.get("local_only"):
                    continue
                if filter_tags:
                    parsed_tags = cached_row.get("tags_list") or []
                    mem_tags_lower = {str(t).lower() for t in parsed_tags}
                    if not filter_tags.issubset(mem_tags_lower):
                        continue
                if not include_stale or min_confidence > 0.0:
                    eff_conf, is_stale = _compute_row_effective_confidence(cached_row, now=ref_now)
                    if not include_stale and is_stale:
                        continue
                    if eff_conf < min_confidence:
                        continue
                masked_indices.append(idx)
                masked_mid_set.add(mid)

        if not masked_indices:
            return []

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

        # Counter.update() counts in C; identical totals to the old per-id Python loop
        # (+1.0 per exact token hit, +0.5 per synonym hit, masked rows only).
        from collections import Counter as _HitCounter

        _exact_hits: _HitCounter[str] = _HitCounter()
        for tok in q_content_tokens:
            t_hits = cache.token_to_ids.get(tok)
            if t_hits:
                _exact_hits.update(t_hits)
        _syn_hits: _HitCounter[str] = _HitCounter()
        for syn_set in q_synonyms.values():
            for syn in syn_set:
                s_hits = cache.token_to_ids.get(syn)
                if s_hits:
                    _syn_hits.update(s_hits)
        lexical_map: dict[str, float] = {
            mid: float(cnt) for mid, cnt in _exact_hits.items() if mid in masked_mid_set
        }
        for mid, cnt in _syn_hits.items():
            if mid in masked_mid_set:
                lexical_map[mid] = lexical_map.get(mid, 0.0) + 0.5 * cnt
        if lexical_map:
            max_hits = max(lexical_map.values())
            lexical_map = {
                mid: round(0.65 * (hits / max_hits) + 0.35 * (hits / (hits + 1.0)), 4)
                for mid, hits in lexical_map.items()
            }

        masked_count = len(masked_indices)
        k = min(masked_count, max(n * 4, 30))
        if masked_count > k:
            masked_arr = np.asarray(masked_indices, dtype=np.int64)
            masked_sims = sims[masked_arr]
            top_local = np.argpartition(-masked_sims, k - 1)[:k]
            candidate_mid_set = {ids_snap[int(masked_arr[i])] for i in top_local}
            candidate_mid_set.update(mid for mid in lexical_map if mid in masked_mid_set)
            with _CACHE_LOCK:
                for s in q_stems:
                    s_hits = cache.stem_to_ids.get(s)
                    if s_hits:
                        candidate_mid_set.update(s_hits & masked_mid_set)
            eval_indices = [idx for idx in masked_indices if ids_snap[idx] in candidate_mid_set]
        else:
            eval_indices = masked_indices

        # df-aware pruning: terms such as "port"/"architecture" occur in nearly every row,
        # so the union above can be the whole namespace. Rank by (lexical hits, semantic sim),
        # keep the best few dozen plus every row that matches a RARE query stem, and only
        # run the expensive per-row lexical scorer on those.
        _prune_cap = max(n * 2, 48)
        if len(eval_indices) > _prune_cap:
            _must: set[str] = set()
            with _CACHE_LOCK:
                for _s in set(q_stems):
                    if 0 < stem_df_snap.get(_s, 0) <= 24:
                        _hits = cache.stem_to_ids.get(_s)
                        if _hits:
                            _must.update(_hits & masked_mid_set)
            _ev = np.asarray(eval_indices, dtype=np.int64)
            _lex_part = np.fromiter(
                (lexical_map.get(ids_snap[_i], 0.0) for _i in eval_indices),
                dtype=np.float64,
                count=len(eval_indices),
            )
            _rank = 0.6 * _lex_part + 0.4 * np.clip(sims[_ev].astype(np.float64), 0.0, 1.0)
            _top = np.argpartition(-_rank, _prune_cap - 1)[:_prune_cap]
            _keep = {eval_indices[int(_i)] for _i in _top}
            eval_indices = [_i for _i in eval_indices if _i in _keep or ids_snap[_i] in _must]

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

        scored_candidates: list[dict[str, Any]] = []
        max_specific_cov_in_pool = 0.0

        for idx in eval_indices:
            mid = ids_snap[idx]
            cached_row = rows_snap.get(mid)
            if cached_row is None:
                continue
            parsed_tags = cached_row.get("tags_list") or []

            mixed_space = False if use_hash_sim else _is_mixed_space(cached_row.get("embedding_model"), query_embedding_model)
            sem_sim = 0.0 if mixed_space else max(0.0, min(1.0, float(sims[idx])))
            fts_score = lexical_map.get(mid, 0.0)
            lex_score, specific_cov, exact_stem_cov, match_reason = self._score_candidate_lexical(
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
            row_dict["_exact_stem_cov"] = exact_stem_cov
            row_dict["match_reason"] = match_reason
            row_dict["snippet"] = _make_snippet(cached_row["content"])
            scored_candidates.append(row_dict)

        if not scored_candidates:
            return []

        for item in scored_candidates:
            sem_sim = item["semantic_similarity"]
            lex_score = item["lexical_score"]
            specific_cov = item.pop("_specific_cov", 0.0)
            exact_stem_cov = item.pop("_exact_stem_cov", 0.0)
            if exact_stem_cov > 0.0:
                lexical_floor = 0.78 * exact_stem_cov + 0.14 * lex_score + 0.08 * sem_sim
                boosted_sem = sem_sim + (1.0 - sem_sim) * (0.58 * lex_score + 0.25 * exact_stem_cov)
                hybrid_sim = min(1.0, max(lexical_floor, boosted_sem))
            elif specific_cov > 0.0:
                hybrid_sim = min(1.0, sem_sim + (1.0 - sem_sim) * (0.42 * lex_score + 0.18 * specific_cov))
            else:
                if max_specific_cov_in_pool >= 0.5 and sem_sim < 0.55:
                    hybrid_sim = sem_sim * 0.38
                else:
                    hybrid_sim = sem_sim
            item["similarity"] = round(hybrid_sim, 4)

        # Sort descending by score components; memory_id ascending breaks exact-score ties
        # deterministically so that two calls with all-equal scores always return the same order.
        # Negating the three floats avoids reverse=True (which would flip the id direction).
        scored_candidates.sort(
            key=lambda x: (-x["similarity"], -x["lexical_score"], -x["semantic_similarity"], x["id"]),
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
        include_private: bool = False,
        query_embedding_model: Optional[str] = None,
        include_quarantined: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Return compact snippets of other possible memories in the namespace.
        Finding N8 & S8: Defaults `include_private=False` and `include_quarantined=False`,
        and never pads `"namespace_context"` with unrelated `local_only=True` or `quarantined=True` memories.
        """
        if not self.db_path.exists() or limit <= 0:
            return []
        cache = self._get_vector_cache()
        _, _, rows_snap = cache.snapshot(copy_matrix=False)
        if len(rows_snap) == 0:
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
            query_embedding_model=query_embedding_model,
            include_quarantined=include_quarantined,
        )

        had_filters = bool(active_memory_type or active_tags)
        for cand in unfiltered:
            mid = cand.get("id")
            if not mid or mid in seen_ids:
                continue
            sim = float(cand.get("similarity", 0.0))
            sem_sim = float(cand.get("semantic_similarity", 0.0))
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
                if cand.get("local_only"):
                    continue
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
                    "trust_tier": cand.get("trust_tier", "user"),
                    "quarantined": bool(cand.get("quarantined", False)),
                    "similarity": round(sim, 4),
                    "semantic_similarity": round(sem_sim, 4),
                    "lexical_score": round(lex, 4),
                    "reason": reason,
                }
            )
            if len(suggestions) >= limit:
                return suggestions

        # Finding N8: Never pad unrelated "namespace_context" items with `local_only=True` or `quarantined=True` rows
        remaining = [
            r
            for mid, r in rows_snap.items()
            if mid not in seen_ids
            and not r.get("local_only")
            and (include_quarantined or not r.get("quarantined"))
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
                    "trust_tier": r.get("trust_tier", "user"),
                    "quarantined": bool(r.get("quarantined", False)),
                    "similarity": 0.0,
                    "semantic_similarity": 0.0,
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
        order_by: str = "accessed",
        include_quarantined: bool = True,
        quarantined_only: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Return memories in the namespace sorted by `accessed_at DESC` (or by
        effective confidence when `order_by="confidence"`).
        """
        if not self.db_path.exists() or limit <= 0:
            return []
        self.flush_telemetry()
        cache = self._get_vector_cache()
        _, _, rows_snap = cache.snapshot(copy_matrix=False)
        if not rows_snap:
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
        for mid, row in rows_snap.items():
            if not include_private and row.get("local_only"):
                continue
            if quarantined_only and not row.get("quarantined"):
                continue
            if not include_quarantined and row.get("quarantined"):
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

        if order_by == "confidence":
            filtered.sort(
                key=lambda r: (
                    _compute_row_effective_confidence(r, now=now)[0],
                    int(r.get("access_count") or 0),
                    str(r.get("created_at") or ""),
                ),
                reverse=True,
            )
        else:
            filtered.sort(
                key=lambda r: (str(r.get("accessed_at") or ""), str(r.get("created_at") or "")),
                reverse=True,
            )
        return filtered[:limit]

    def get_exportable_memories(
        self,
        include_embeddings: bool = True,
        include_quarantined: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Return all non-private (`local_only = 0`) and non-quarantined memories in the namespace
        for `io.export_namespace()`, decrypting fields if at-rest encryption is enabled.
        """
        if not self.db_path.exists():
            return []
        self._maybe_lazy_flush_telemetry()
        with _CACHE_LOCK:
            synced = self._is_cache_synced_locked()
            if synced is not None:
                results: list[dict[str, Any]] = []
                for mid in synced.ids:
                    row = synced.rows_by_id.get(mid)
                    if row is None:
                        continue
                    if row.get("local_only"):
                        continue
                    if not include_quarantined and row.get("quarantined"):
                        continue
                    d = dict(row)
                    if include_embeddings:
                        idx = synced.id_to_idx.get(mid)
                        if idx is not None and synced._matrix_buf is not None:
                            d["embedding"] = synced._matrix_buf[idx].tolist()
                        else:
                            d["embedding"] = None
                    results.append(d)
                results.sort(key=lambda r: str(r.get("created_at") or ""))
                return results

        q_clause = "" if include_quarantined else "AND COALESCE(quarantined, 0) = 0 "
        with _CACHE_LOCK:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                if include_embeddings:
                    rows = conn.execute(
                        f"SELECT {_MEMORY_COLUMNS}, embedding FROM memories "
                        f"WHERE namespace = ? AND (local_only = 0 OR local_only IS NULL) {q_clause}"
                        "ORDER BY created_at ASC",
                        (self.namespace,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT {_MEMORY_COLUMNS} FROM memories "
                        f"WHERE namespace = ? AND (local_only = 0 OR local_only IS NULL) {q_clause}"
                        "ORDER BY created_at ASC",
                        (self.namespace,),
                    ).fetchall()

        db_results: list[dict[str, Any]] = []
        for r in rows:
            d = {col: r[col] for col in r.keys() if col != "embedding"}
            d["content"] = decrypt_text_if_encrypted(d.get("content"))
            d["tags"] = decrypt_text_if_encrypted(d.get("tags") or "[]")
            d["local_only"] = bool(d.get("local_only"))
            d["quarantined"] = bool(d.get("quarantined"))
            d["trust_tier"] = _normalize_trust_tier(d.get("trust_tier"), default="user")
            d["secret_ref"] = _parse_secret_ref_json(d.get("secret_ref"))
            d["tags_list"] = _parse_tags_json(d.get("tags"))
            if include_embeddings:
                blob = r["embedding"] if "embedding" in r.keys() else None
                if blob:
                    arr = _blob_to_vector(blob)
                    d["embedding"] = arr.tolist() if arr.size == EMBEDDING_DIM else None
                else:
                    d["embedding"] = None
            db_results.append(d)
        return db_results

    def get_exportable_work_items(
        self,
        include_private: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
        """
        Return (exportable_items, exportable_events, total_count, skipped_private_count)
        for this namespace.
        Private items (local_only == 1) are excluded unless include_private=True.
        Events for private items are excluded if the item is excluded.
        """
        if not self.db_path.exists():
            return [], [], 0, 0
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            try:
                all_items = conn.execute(
                    "SELECT * FROM work_items WHERE namespace = ? ORDER BY created_at ASC",
                    (self.namespace,),
                ).fetchall()
            except sqlite3.OperationalError:
                return [], [], 0, 0

            total_count = len(all_items)
            exportable_items = []
            skipped_private = 0
            allowed_ids = set()
            for r in all_items:
                d = dict(r)
                is_priv = bool(d.get("local_only"))
                if is_priv and not include_private:
                    skipped_private += 1
                    continue
                d["title"] = decrypt_text_if_encrypted(d.get("title"))
                d["description"] = decrypt_text_if_encrypted(d.get("description") or "")
                if d.get("summary"):
                    d["summary"] = decrypt_text_if_encrypted(d["summary"])
                d["evidence"] = json.loads(d.get("evidence") or "[]")
                d["tags"] = json.loads(d.get("tags") or "[]")
                d["local_only"] = is_priv
                exportable_items.append(d)
                allowed_ids.add(d["id"])

            if not allowed_ids:
                return exportable_items, [], total_count, skipped_private

            placeholders = ",".join("?" for _ in allowed_ids)
            event_rows = conn.execute(
                f"SELECT * FROM work_events WHERE namespace = ? AND item_id IN ({placeholders}) ORDER BY timestamp ASC",
                (self.namespace, *allowed_ids),
            ).fetchall()
            exportable_events = []
            for er in event_rows:
                ed = dict(er)
                if ed.get("note"):
                    ed["note"] = decrypt_text_if_encrypted(ed["note"])
                exportable_events.append(ed)

            return exportable_items, exportable_events, total_count, skipped_private

    def import_work_items(
        self,
        items: list[dict[str, Any]],
        events: list[dict[str, Any]],
        merge_strategy: str = "skip_existing",
    ) -> dict[str, int]:
        """
        Import work items and work events into this namespace.
        Enforces guard_ingest and guard_tags.
        """
        self._ensure_initialized()
        stats = {"imported_items": 0, "skipped_items": 0, "imported_events": 0, "errors": 0}
        with self._connect() as conn:
            existing_ids = {r[0] for r in conn.execute("SELECT id FROM work_items WHERE namespace = ?", (self.namespace,)).fetchall()}
            existing_short_ids = {r[0] for r in conn.execute("SELECT short_id FROM work_items WHERE namespace = ?", (self.namespace,)).fetchall()}

            for it in items:
                try:
                    raw_id = str(it.get("id") or "").strip()
                    if not raw_id:
                        stats["errors"] += 1
                        continue
                    if merge_strategy == "skip_existing" and raw_id in existing_ids:
                        stats["skipped_items"] += 1
                        continue

                    title = str(it.get("title") or "").strip()
                    if not title:
                        stats["errors"] += 1
                        continue

                    title_res = guard_ingest(title, local_only=bool(it.get("local_only")))

                    desc_str = str(it.get("description") or "").strip()
                    if desc_str:
                        desc_res = guard_ingest(desc_str, local_only=title_res.local_only)
                        clean_desc = desc_res.content.strip()
                        desc_priv = desc_res.local_only
                    else:
                        clean_desc = ""
                        desc_priv = False

                    summary_text = it.get("summary")
                    if summary_text and str(summary_text).strip():
                        summary_res = guard_ingest(str(summary_text), local_only=desc_priv)
                        clean_summary = summary_res.content.strip()
                        summary_priv = summary_res.local_only
                    else:
                        clean_summary = None
                        summary_priv = False

                    raw_tags = it.get("tags") or []
                    clean_tags, tags_priv, _ = guard_tags(
                        raw_tags if isinstance(raw_tags, list) else json.loads(raw_tags),
                        local_only=desc_priv,
                    )
                    final_local = 1 if (title_res.local_only or desc_priv or tags_priv or summary_priv) else 0

                    short_id = str(it.get("short_id") or "").strip()
                    kind = str(it.get("kind") or "task").lower()
                    if not short_id or (short_id in existing_short_ids and raw_id not in existing_ids):
                        prefix = "goal" if kind == "goal" else "task"
                        short_id = self._generate_short_id_locked(conn, prefix)

                    enc_on = is_encryption_enabled()
                    title_to_store = encrypt_text_for_storage(title_res.content) if enc_on else title_res.content
                    desc_to_store = encrypt_text_for_storage(clean_desc) if (enc_on and clean_desc) else clean_desc
                    summary_to_store = (
                        encrypt_text_for_storage(clean_summary)
                        if (enc_on and clean_summary)
                        else clean_summary
                    )

                    evidence_json = json.dumps(it.get("evidence") or []) if isinstance(it.get("evidence"), list) else str(it.get("evidence") or "[]")
                    tags_json = json.dumps(clean_tags)

                    if raw_id in existing_ids and merge_strategy == "overwrite":
                        conn.execute(
                            """
                            UPDATE work_items
                            SET short_id = ?, kind = ?, parent_id = ?, title = ?, description = ?, status = ?,
                                priority = ?, created_by = ?, created_at = ?, started_at = ?, completed_at = ?,
                                completed_by = ?, summary = ?, evidence = ?, tags = ?, local_only = ?
                            WHERE id = ? AND namespace = ?
                            """,
                            (
                                short_id, kind, it.get("parent_id"), title_to_store, desc_to_store,
                                it.get("status", "todo"), it.get("priority", "medium"), it.get("created_by", "mesh-import"),
                                it.get("created_at") or _utc_now_iso(), it.get("started_at"), it.get("completed_at"),
                                it.get("completed_by"), summary_to_store, evidence_json, tags_json, final_local,
                                raw_id, self.namespace
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO work_items (
                                id, short_id, namespace, kind, parent_id, title, description, status,
                                priority, created_by, created_at, started_at, completed_at, completed_by,
                                summary, evidence, tags, local_only
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                raw_id, short_id, self.namespace, kind, it.get("parent_id"), title_to_store, desc_to_store,
                                it.get("status", "todo"), it.get("priority", "medium"), it.get("created_by", "mesh-import"),
                                it.get("created_at") or _utc_now_iso(), it.get("started_at"), it.get("completed_at"),
                                it.get("completed_by"), summary_to_store, evidence_json, tags_json, final_local
                            ),
                        )
                    existing_ids.add(raw_id)
                    existing_short_ids.add(short_id)
                    stats["imported_items"] += 1
                except Exception as exc:
                    logger.warning("Error importing work item %s: %s", it, exc)
                    stats["errors"] += 1

            for ev in events:
                try:
                    item_id = str(ev.get("item_id") or "").strip()
                    if item_id not in existing_ids:
                        continue
                    note = ev.get("note")
                    enc_on = is_encryption_enabled()
                    note_to_store = (
                        encrypt_text_for_storage(str(note))
                        if (enc_on and note)
                        else (str(note) if note else None)
                    )
                    conn.execute(
                        """
                        INSERT INTO work_events (
                            item_id, namespace, event_type, old_status, new_status, note, agent_id, timestamp
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item_id, self.namespace, ev.get("event_type", "status_change"),
                            ev.get("old_status"), ev.get("new_status"), note_to_store,
                            ev.get("agent_id", "mesh-import"), ev.get("timestamp") or _utc_now_iso()
                        ),
                    )
                    stats["imported_events"] += 1
                except Exception:
                    pass
            conn.commit()
        return stats

    def delete(self, memory_id: str) -> bool:
        """
        Delete a memory by ID, removing its FTS5 entry by `rowid`, scrubbing contradiction
        and query records referencing it, and updating the RAM cache in-place.
        """
        clean_id = (memory_id or "").strip()
        if not clean_id or not self.db_path.exists():
            return False

        # Pending access tracking only needs flushing when it could still reference this row;
        # queries mentioning the content are scrubbed from the buffer in-memory below.
        with _ACCESS_TRACKING_LOCK:
            _tb = _ACCESS_TRACKING_BUFFERS.get(self.db_path)
            _has_pending = bool(_tb is not None and (_tb.access_increments or _tb.query_rows))
        if _has_pending:
            self.flush_access_tracking()
        purged_content: Optional[str] = None
        deleted = False

        with namespace_write_lock(self.namespace):
            with _CACHE_LOCK:
                synced_cache = self._is_cache_synced_locked()
                if synced_cache is not None and clean_id in synced_cache.rows_by_id:
                    purged_content = synced_cache.rows_by_id[clean_id].get("content")

                new_cv: Optional[int] = None
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        row = conn.execute(
                            "SELECT rowid, content FROM memories WHERE id = ? AND namespace = ?",
                            (clean_id, self.namespace),
                        ).fetchone()
                        if row is not None:
                            rowid = int(row[0])
                            if purged_content is None:
                                purged_content = decrypt_text_if_encrypted(str(row[1] or ""))
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
                                _scrub_queries_in_tx(conn, self.namespace, purged_content)
                                _scrub_pending_query_telemetry(self.namespace, purged_content)
                            new_cv = self._bump_content_version_in_tx(conn)
                        conn.commit()
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        raise

                if deleted:
                    if synced_cache is not None:
                        synced_cache.delete_vector(clean_id)
                        self._refresh_stamps_after_write_locked(synced_cache, content_version=new_cv)
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
        if not self.db_path.exists():
            return 0
        self._maybe_lazy_flush_telemetry()
        cache = self._get_vector_cache()
        with _CACHE_LOCK:
            return len(cache.all_ids)

    def count_matching(
        self,
        memory_type: Optional[str] = None,
        tag: Optional[str] = None,
        tags: Optional[list[str]] = None,
        include_private: bool = True,
        source_agent: Optional[str] = None,
        include_quarantined: bool = True,
    ) -> int:
        """
        Compute the number of matching memories using the decrypted in-memory metadata cache
        so tag filters work identically whether the database is encrypted or plaintext.
        """
        if not self.db_path.exists():
            return 0
        self._maybe_lazy_flush_telemetry()
        cache = self._get_vector_cache()
        _, _, rows_snap = cache.snapshot(copy_matrix=False)
        if not rows_snap:
            return 0

        type_filter = str(memory_type).strip().lower() if (memory_type and str(memory_type).strip()) else None
        agent_filter = str(source_agent).strip() if (source_agent and str(source_agent).strip()) else None
        req_tags: set[str] = set()
        if tag and str(tag).strip():
            req_tags.add(str(tag).strip().lower())
        if tags:
            for t in tags:
                if str(t).strip():
                    req_tags.add(str(t).strip().lower())

        matched = 0
        for row in rows_snap.values():
            if not include_private and row.get("local_only"):
                continue
            if not include_quarantined and row.get("quarantined"):
                continue
            if type_filter and str(row.get("memory_type", "")).lower() != type_filter:
                continue
            if agent_filter and str(row.get("source_agent", "")) != agent_filter:
                continue
            if req_tags:
                row_tags = {
                    str(t).lower()
                    for t in (row.get("tags_list") or _parse_tags_json(row.get("tags")))
                }
                if not req_tags.issubset(row_tags):
                    continue
            matched += 1
        return matched

    def clear_namespace(self) -> None:
        """
        Delete all memories, FTS entries, queries, contradictions, AND audit_log entries
        in this namespace (Finding S12).
        """
        if not self.db_path.exists():
            clear_audit_log(self.namespace)
            return
        self.flush_access_tracking()
        with namespace_write_lock(self.namespace):
            with _ACCESS_TRACKING_LOCK:
                resolved = self.db_path.resolve()
                buf = _ACCESS_TRACKING_BUFFERS.get(resolved)
                if buf is not None and buf.namespace == self.namespace:
                    _ACCESS_TRACKING_BUFFERS.pop(resolved, None)
            with _CACHE_LOCK:
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        if self._fts_enabled:
                            try:
                                conn.execute(
                                    "DELETE FROM memories_fts WHERE rowid IN "
                                    "(SELECT rowid FROM memories WHERE namespace = ?)",
                                    (self.namespace,),
                                )
                            except sqlite3.OperationalError:
                                pass
                        conn.execute("DELETE FROM memories WHERE namespace = ?", (self.namespace,))
                        conn.execute("DELETE FROM queries WHERE namespace = ?", (self.namespace,))
                        conn.execute("DELETE FROM contradictions WHERE namespace = ?", (self.namespace,))
                        self._bump_content_version_in_tx(conn)
                        conn.commit()
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        raise
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        conn.execute("VACUUM")
                    except sqlite3.OperationalError:
                        pass
                self._invalidate_cache()
        clear_audit_log(self.namespace)

    # --- Access Tracking, Query Logging & Contradiction Management ---

    def flush_telemetry(self) -> None:
        """Flush pending buffered access counts and queries for this store's DB."""
        _flush_access_tracking_for_db_path(self.db_path)

    def flush_access_tracking(self) -> None:
        """Flush pending buffered access counts and queries for this store's DB."""
        self.flush_telemetry()

    def _resolved_db_path(self) -> Path:
        """`self.db_path.resolve()` memoised per store for the path-trust TTL."""
        ttl = _path_recheck_ttl()
        if ttl > 0.0:
            ent = self.__dict__.get("_rdb_cache")
            if ent is not None and (time.monotonic() - ent[0]) < ttl:
                return ent[1]
        resolved = self.db_path.resolve()
        if ttl > 0.0:
            self.__dict__["_rdb_cache"] = (time.monotonic(), resolved)
        return resolved

    def _maybe_lazy_flush_access_tracking(self) -> None:
        resolved = self._resolved_db_path()
        batch_limit = _get_tracking_batch_limit()
        flush_secs = _get_tracking_flush_seconds()
        should_flush = False
        with _ACCESS_TRACKING_LOCK:
            buf = _ACCESS_TRACKING_BUFFERS.get(resolved)
            if buf is not None and buf.event_count > 0:
                if batch_limit <= 0 or flush_secs <= 0.0:
                    should_flush = True
                elif buf.event_count >= batch_limit:
                    should_flush = True
                elif (time.monotonic() - buf.first_event_ts) >= flush_secs:
                    should_flush = True
        if should_flush:
            _flush_access_tracking_for_db_path(resolved)

    # Backward-compatibility alias
    _maybe_lazy_flush_telemetry = _maybe_lazy_flush_access_tracking

    def increment_access(self, memory_id: str) -> None:
        self.increment_access_batch([memory_id])

    def increment_access_batch(self, memory_ids: list[str]) -> None:
        valid_ids = [mid for mid in dict.fromkeys(memory_ids) if mid]
        if not valid_ids:
            return
        self.record_recall_access(
            memory_ids=valid_ids,
            query_text="",
            agent_id="unknown",
            best_similarity=None,
            result_count=len(valid_ids),
            private_query=False,
        )
        self.flush_access_tracking()

    def log_query(
        self,
        query_text: str,
        agent_id: str = "unknown",
        best_similarity: Optional[float] = None,
        result_count: int = 0,
        private_query: bool = False,
    ) -> None:
        self.record_recall_access(
            memory_ids=[],
            query_text=query_text,
            agent_id=agent_id,
            best_similarity=best_similarity,
            result_count=result_count,
            private_query=private_query,
        )
        self.flush_access_tracking()

    def record_recall_access(
        self,
        memory_ids: list[str],
        query_text: str,
        agent_id: str,
        best_similarity: Optional[float],
        result_count: int,
        private_query: bool = False,
    ) -> None:
        """
        Update in-memory `access_count`/`accessed_at` via copy-on-write immediately and
        buffer SQLite access tracking writes.
        All query text is passed through `redact_secrets_local` before storage.
        If `MESH_LOG_QUERY_TEXT` is disabled (0/false/no), query text is omitted entirely.
        """
        if not self.db_path.exists():
            if not self.create:
                return
            self._ensure_initialized()

        valid_ids = [mid for mid in dict.fromkeys(memory_ids) if mid]
        now = _utc_now_iso()
        qid = str(uuid.uuid4())
        raw_query = (query_text or "").strip()[:512]
        is_priv_q = bool(private_query)

        # Secret redaction on query text and check setting
        log_query_text_enabled = os.environ.get("MESH_LOG_QUERY_TEXT", "1").strip().lower() not in ("0", "false", "no")
        if raw_query:
            if is_credential(raw_query):
                is_priv_q = True
            # Always pass through secret redaction
            clean_query = redact_secrets_local(raw_query) if log_query_text_enabled else ""
        else:
            clean_query = ""

        resolved = self._resolved_db_path()

        with _CACHE_LOCK:
            cached = _VECTOR_CACHE.get((self.db_path, self.namespace))
            if cached is not None:
                for mid in valid_ids:
                    rm = cached.rows_by_id.get(mid)
                    if rm is not None:
                        cached.rows_by_id[mid] = {
                            **rm,
                            "access_count": int(rm.get("access_count") or 0) + 1,
                            "accessed_at": now,
                        }

        overflow_path: Optional[Path] = None
        with _ACCESS_TRACKING_LOCK:
            buf = _ACCESS_TRACKING_BUFFERS.get(resolved)
            if buf is None:
                if len(_ACCESS_TRACKING_BUFFERS) >= _get_max_pool_size():
                    for cand_p in _ACCESS_TRACKING_BUFFERS:
                        if cand_p != resolved:
                            overflow_path = cand_p
                            break
                buf = _AccessTrackingBuffer(resolved, self.namespace)
                _ACCESS_TRACKING_BUFFERS[resolved] = buf
            if buf.event_count == 0:
                buf.first_event_ts = time.monotonic()
            buf.event_count += 1
            for mid in valid_ids:
                prev_cnt, _ = buf.access_increments.get(mid, (0, now))
                buf.access_increments[mid] = (prev_cnt + 1, now)
            if clean_query or not log_query_text_enabled:
                buf.query_rows.append(
                    (
                        qid,
                        self.namespace,
                        clean_query,
                        agent_id or "unknown",
                        float(best_similarity) if best_similarity is not None else None,
                        int(result_count),
                        now,
                        1 if is_priv_q else 0,
                    )
                )

        if overflow_path is not None:
            try:
                _flush_access_tracking_for_db_path(overflow_path)
            except Exception:
                pass
        self._maybe_lazy_flush_access_tracking()

    # Backward-compatibility alias
    record_recall_telemetry = record_recall_access

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
        Redacts `new_content` or `existing_content` if either memory is `local_only=True`,
        and encrypts at rest when enabled (Finding S5, S12).
        """
        cid = str(uuid.uuid4())
        now = _utc_now_iso()
        enc_on = is_encryption_enabled()

        with namespace_write_lock(self.namespace):
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

                safe_new = REDACTED_CONTENT_PLACEHOLDER if local_only else redact_secrets_local(new_content)
                safe_existing = (
                    REDACTED_CONTENT_PLACEHOLDER
                    if existing_is_private
                    else redact_secrets_local(existing_content)
                )
                db_new = encrypt_text_for_storage(safe_new) if enc_on else safe_new
                db_existing = encrypt_text_for_storage(safe_existing) if enc_on else safe_existing

                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
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
                                db_new,
                                validate_memory_id(existing_memory_id) if existing_memory_id else "",
                                db_existing,
                                float(similarity),
                                now,
                            ),
                        )
                        conn.commit()
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        raise
            self._refresh_cache_stamp_after_non_memory_write()
        return cid

    def get_contradictions(self, resolved: bool = False) -> list[dict[str, Any]]:
        """Return unresolved (or resolved) contradictions for this namespace, decrypting fields if needed."""
        if not self.db_path.exists():
            return []
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
                out: list[dict[str, Any]] = []
                for r in rows:
                    d = dict(r)
                    d["new_content"] = decrypt_text_if_encrypted(d.get("new_content"))
                    d["existing_content"] = decrypt_text_if_encrypted(d.get("existing_content"))
                    out.append(d)
                return out

    def resolve_contradiction(self, contradiction_id: str) -> bool:
        clean_pref = (contradiction_id or "").strip().lower()
        if not clean_pref:
            raise ValueError("contradiction_id cannot be empty")
        if not self.db_path.exists():
            raise ValueError(
                f"Contradiction ID '{contradiction_id}' not found in namespace '{self.namespace}'."
            )

        with namespace_write_lock(self.namespace):
            with _CACHE_LOCK:
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
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
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        raise

            self._refresh_cache_stamp_after_non_memory_write()
            return updated

    def get_patterns(self, include_private: bool = False) -> dict[str, Any]:
        """
        Analyze query logs and memories to return `top_topics`, `memory_gaps`,
        `unused_memories`, and `stale_memories`, decrypting encrypted rows transparently (Finding S5, S12).
        """
        if not self.db_path.exists():
            return {
                "top_topics": [],
                "memory_gaps": [],
                "unused_memories": [],
                "stale_memories": [],
            }
        self.flush_telemetry()
        cache = self._get_vector_cache()
        mem_rows = [
            r for r in cache.rows_by_id.values()
            if not r.get("quarantined") and (include_private or not r.get("local_only"))
        ]

        priv_q_filter = "" if include_private else "AND COALESCE(private_query, 0) = 0"
        with _CACHE_LOCK:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                raw_q_rows = conn.execute(
                    f"""
                    SELECT query_text, best_similarity
                    FROM queries
                    WHERE namespace = ? AND TRIM(query_text) != '' {priv_q_filter}
                    """,
                    (self.namespace,),
                ).fetchall()

        # Group queries in Python after decrypting so encrypted queries aggregate properly
        q_groups: dict[str, list[float]] = {}
        for qr in raw_q_rows:
            dec_q = decrypt_text_if_encrypted(qr["query_text"]).strip().lower()
            if not dec_q:
                continue
            if not include_private and is_credential(dec_q):
                continue
            sim_val = float(qr["best_similarity"]) if qr["best_similarity"] is not None else 0.0
            q_groups.setdefault(dec_q, []).append(sim_val)

        aggregated_queries: list[dict[str, Any]] = []
        for q_text, sims in q_groups.items():
            q_cnt = len(sims)
            avg_sim = round(sum(sims) / q_cnt, 4) if q_cnt else 0.0
            aggregated_queries.append(
                {
                    "query_text": q_text,
                    "query_count": q_cnt,
                    "avg_similarity": avg_sim,
                }
            )

        top_topics = sorted(
            aggregated_queries,
            key=lambda x: (x["query_count"], x["avg_similarity"]),
            reverse=True,
        )[:15]

        memory_gaps = sorted(
            [
                item
                for item in aggregated_queries
                if item["query_count"] >= GAP_MIN_QUERIES and item["avg_similarity"] < 0.45
            ],
            key=lambda x: (-x["query_count"], x["avg_similarity"]),
        )[:15]

        now = _utc_now()
        seven_days_ago = now - timedelta(days=7)

        unused_memories: list[dict[str, Any]] = []
        stale_memories: list[dict[str, Any]] = []

        for r in mem_rows:
            row = dict(r)
            if not include_private and bool(row.get("local_only")):
                continue
            dec_content = decrypt_text_if_encrypted(row.get("content", ""))
            dec_tags = decrypt_text_if_encrypted(row.get("tags") or "[]")
            eff_conf, is_stale = _compute_row_effective_confidence(row, now=now)
            candidate = {
                "id": row.get("id"),
                "content": dec_content,
                "memory_type": row.get("memory_type", "fact"),
                "confidence": float(row.get("confidence", 1.0)),
                "effective_confidence": eff_conf,
                "is_stale": is_stale,
                "tags": _parse_tags_json(dec_tags),
                "source_agent": row.get("source_agent", "unknown"),
                "created_at": row.get("created_at", ""),
                "accessed_at": row.get("accessed_at", ""),
                "access_count": int(row.get("access_count") or 0),
                "ttl_days": row.get("ttl_days"),
            }
            whitelisted = {k: candidate[k] for k in _PATTERNS_MEMORY_WHITELIST}

            if is_stale:
                stale_memories.append(whitelisted)

            if whitelisted["access_count"] == 0:
                created_dt = _parse_iso_utc(whitelisted["created_at"])
                if created_dt is not None and created_dt <= seven_days_ago:
                    unused_memories.append(whitelisted)

        unused_memories.sort(key=lambda r: str(r.get("created_at") or ""))
        stale_memories.sort(key=lambda r: float(r.get("effective_confidence", 0.0)))

        return {
            "top_topics": top_topics,
            "memory_gaps": memory_gaps,
            "unused_memories": unused_memories[:25],
            "stale_memories": stale_memories[:25],
        }

    # =========================================================================
    # Work Items & Work Events (Goals, Tasks, Project Brief, Changelog)
    # =========================================================================

    def _resolve_work_item_locked(self, conn: sqlite3.Connection, item_id: str) -> Optional[dict[str, Any]]:
        clean_id = (item_id or "").strip()
        if not clean_id:
            return None
        # 1. Exact UUID
        row = conn.execute(
            "SELECT * FROM work_items WHERE namespace = ? AND id = ?",
            (self.namespace, clean_id),
        ).fetchone()
        if row:
            return dict(row)
        # 2. Exact short_id (e.g. task-1, goal-2)
        row = conn.execute(
            "SELECT * FROM work_items WHERE namespace = ? AND lower(short_id) = lower(?)",
            (self.namespace, clean_id),
        ).fetchone()
        if row:
            return dict(row)
        # 3. Hex prefix match on id (at least 4 chars)
        if len(clean_id) >= 4:
            rows = conn.execute(
                "SELECT * FROM work_items WHERE namespace = ? AND id LIKE ? || '%'",
                (self.namespace, clean_id),
            ).fetchall()
            if len(rows) == 1:
                return dict(rows[0])
        return None

    def _not_found_error_message(self, conn: sqlite3.Connection, item_id: str) -> str:
        rows = conn.execute(
            "SELECT short_id, title FROM work_items WHERE namespace = ? ORDER BY created_at DESC LIMIT 5",
            (self.namespace,),
        ).fetchall()
        if not rows:
            return (
                f"Work item '{item_id}' not found in namespace '{self.namespace}'. "
                "No work items exist yet in this namespace; use mesh_task_create to create one first."
            )
        available = [f"{r[0]} ({r[1][:30]})" for r in rows]
        return (
            f"Work item '{item_id}' not found in namespace '{self.namespace}'. "
            f"Available work items: {', '.join(available)}."
        )

    def _generate_short_id_locked(self, conn: sqlite3.Connection, kind: str) -> str:
        count_row = conn.execute(
            "SELECT COUNT(*) FROM work_items WHERE namespace = ? AND kind = ?",
            (self.namespace, kind),
        ).fetchone()
        seq = int(count_row[0] or 0) + 1
        short_id = f"{kind}-{seq}"
        if conn.execute(
            "SELECT 1 FROM work_items WHERE namespace = ? AND short_id = ?",
            (self.namespace, short_id),
        ).fetchone():
            short_id = f"{kind}-{uuid.uuid4().hex[:6]}"
        return short_id

    def create_work_item(
        self,
        title: str,
        kind: str = "task",
        parent_id: Optional[str] = None,
        description: str = "",
        priority: str = "medium",
        tags: Optional[list[str]] = None,
        created_by: str = "agent",
        local_only: bool = False,
    ) -> dict[str, Any]:
        self._ensure_initialized()
        clean_kind = str(kind or "task").strip().lower()
        if clean_kind not in ("goal", "task"):
            raise ValueError(f"Invalid kind '{kind}'. Must be 'goal' or 'task'.")
        clean_priority = str(priority or "medium").strip().lower()
        if clean_priority not in ("low", "medium", "high", "critical"):
            raise ValueError(f"Invalid priority '{priority}'. Must be 'low', 'medium', 'high', or 'critical'.")

        guard_t = guard_ingest(title or "", local_only=local_only)
        if guard_t.rejected:
            raise ValueError(f"Work item title rejected: {guard_t.reason}")
        clean_title = guard_t.content.strip()
        if not clean_title:
            raise ValueError("Work item title cannot be empty.")

        clean_desc = ""
        is_priv = bool(guard_t.local_only)
        if description and str(description).strip():
            guard_d = guard_ingest(description, local_only=guard_t.local_only)
            if guard_d.rejected:
                raise ValueError(f"Work item description rejected: {guard_d.reason}")
            clean_desc = guard_d.content.strip()
            is_priv = bool(is_priv or guard_d.local_only)

        clean_tags, tags_priv, _ = guard_tags(tags or [], local_only=is_priv)
        is_priv = bool(is_priv or tags_priv)

        item_uuid = uuid.uuid4().hex
        now_iso = _utc_now_iso()

        with namespace_write_lock(self.namespace):
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                resolved_parent: Optional[str] = None
                if parent_id:
                    parent_row = self._resolve_work_item_locked(conn, parent_id)
                    if not parent_row:
                        raise ValueError(f"Parent {self._not_found_error_message(conn, parent_id)}")
                    resolved_parent = parent_row["id"]

                count_row = conn.execute(
                    "SELECT COUNT(*) FROM work_items WHERE namespace = ? AND kind = ?",
                    (self.namespace, clean_kind),
                ).fetchone()
                seq = int(count_row[0] or 0) + 1
                short_id = f"{clean_kind}-{seq}"
                if conn.execute("SELECT 1 FROM work_items WHERE namespace = ? AND short_id = ?", (self.namespace, short_id)).fetchone():
                    short_id = f"{clean_kind}-{item_uuid[:6]}"

                conn.execute(
                    """
                    INSERT INTO work_items
                    (id, short_id, namespace, kind, parent_id, title, description, status, priority,
                     created_by, created_at, started_at, completed_at, completed_by, summary, evidence, tags, local_only)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'todo', ?, ?, ?, NULL, NULL, NULL, NULL, '[]', ?, ?)
                    """,
                    (
                        item_uuid,
                        short_id,
                        self.namespace,
                        clean_kind,
                        resolved_parent,
                        clean_title,
                        clean_desc,
                        clean_priority,
                        str(created_by or "agent"),
                        now_iso,
                        json.dumps(clean_tags),
                        1 if is_priv else 0,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO work_events
                    (item_id, namespace, event_type, old_status, new_status, note, agent_id, timestamp)
                    VALUES (?, ?, 'created', NULL, 'todo', ?, ?, ?)
                    """,
                    (
                        item_uuid,
                        self.namespace,
                        f"Created {clean_kind}: {clean_title}",
                        str(created_by or "agent"),
                        now_iso,
                    ),
                )
                row = conn.execute("SELECT * FROM work_items WHERE id = ?", (item_uuid,)).fetchone()
                item_dict = dict(row)
                item_dict["tags"] = clean_tags
                item_dict["evidence"] = []
            self._refresh_cache_stamp_after_non_memory_write()
            return item_dict

    def update_work_item(
        self,
        item_id: str,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        note: Optional[str] = None,
        agent_id: str = "agent",
    ) -> dict[str, Any]:
        self._ensure_initialized()
        now_iso = _utc_now_iso()
        with namespace_write_lock(self.namespace):
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                item = self._resolve_work_item_locked(conn, item_id)
                if not item:
                    raise ValueError(self._not_found_error_message(conn, item_id))

                full_id = item["id"]
                updates: list[str] = []
                params: list[Any] = []
                old_status = item["status"]
                new_status = old_status
                event_type = "updated"

                if status is not None:
                    clean_status = str(status).strip().lower()
                    if clean_status in ("reopened", "reopen"):
                        new_status = "todo"
                        updates.append("status = ?")
                        params.append("todo")
                        event_type = "reopened"
                        if item["completed_at"]:
                            updates.append("completed_at = NULL")
                        if item["completed_by"]:
                            updates.append("completed_by = NULL")
                    elif clean_status not in ("todo", "in_progress", "blocked", "done", "cancelled"):
                        raise ValueError(f"Invalid status '{status}'. Must be todo, in_progress, blocked, done, cancelled, or reopened.")
                    else:
                        new_status = clean_status
                        updates.append("status = ?")
                        params.append(new_status)
                        event_type = new_status
                        if new_status == "in_progress" and not item["started_at"]:
                            updates.append("started_at = ?")
                            params.append(now_iso)

                if priority is not None:
                    clean_priority = str(priority).strip().lower()
                    if clean_priority not in ("low", "medium", "high", "critical"):
                        raise ValueError(f"Invalid priority '{priority}'.")
                    updates.append("priority = ?")
                    params.append(clean_priority)

                if title is not None:
                    guard_t = guard_ingest(title, local_only=bool(item["local_only"]))
                    if guard_t.rejected:
                        raise ValueError(f"Title rejected: {guard_t.reason}")
                    updates.append("title = ?")
                    params.append(guard_t.content.strip())
                    if guard_t.local_only and not item["local_only"]:
                        updates.append("local_only = 1")

                if description is not None:
                    if str(description).strip():
                        guard_d = guard_ingest(description, local_only=bool(item["local_only"]))
                        if guard_d.rejected:
                            raise ValueError(f"Description rejected: {guard_d.reason}")
                        updates.append("description = ?")
                        params.append(guard_d.content.strip())
                        if guard_d.local_only and not item["local_only"]:
                            updates.append("local_only = 1")
                    else:
                        updates.append("description = ?")
                        params.append("")

                clean_note = None
                if note is not None and str(note).strip():
                    guard_n = guard_ingest(note)
                    clean_note = guard_n.content.strip()
                    if guard_n.local_only and not item["local_only"]:
                        updates.append("local_only = 1")

                if updates:
                    params.append(full_id)
                    conn.execute(f"UPDATE work_items SET {', '.join(updates)} WHERE id = ?", params)

                conn.execute(
                    """
                    INSERT INTO work_events
                    (item_id, namespace, event_type, old_status, new_status, note, agent_id, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        full_id,
                        self.namespace,
                        event_type,
                        old_status if status is not None else None,
                        new_status if status is not None else None,
                        clean_note,
                        str(agent_id or "agent"),
                        now_iso,
                    ),
                )
                updated_row = conn.execute("SELECT * FROM work_items WHERE id = ?", (full_id,)).fetchone()
                res = dict(updated_row)
                res["tags"] = json.loads(res.get("tags") or "[]")
                res["evidence"] = json.loads(res.get("evidence") or "[]")
            self._refresh_cache_stamp_after_non_memory_write()
            return res

    def complete_work_item(
        self,
        item_id: str,
        summary: str,
        evidence: Optional[list[str]] = None,
        note: Optional[str] = None,
        agent_id: str = "agent",
    ) -> dict[str, Any]:
        self._ensure_initialized()
        clean_sum = (summary or "").strip()
        if not clean_sum:
            raise ValueError("A detailed summary of what was completed is required to mark a work item done.")

        guard_s = guard_ingest(clean_sum)
        if guard_s.rejected:
            raise ValueError(f"Summary rejected: {guard_s.reason}")

        now_iso = _utc_now_iso()
        valid_ev: list[str] = [str(x).strip() for x in (evidence or []) if str(x).strip()]

        clean_note = None
        guard_n = None
        if note is not None and str(note).strip():
            guard_n = guard_ingest(note)
            clean_note = guard_n.content.strip()
        else:
            clean_note = guard_s.content.strip()

        with namespace_write_lock(self.namespace):
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                item = self._resolve_work_item_locked(conn, item_id)
                if not item:
                    raise ValueError(self._not_found_error_message(conn, item_id))

                full_id = item["id"]
                old_status = item["status"]
                is_priv = bool(item["local_only"] or guard_s.local_only or (guard_n and guard_n.local_only))
                conn.execute(
                    """
                    UPDATE work_items
                    SET status = 'done',
                        completed_at = ?,
                        completed_by = ?,
                        summary = ?,
                        evidence = ?,
                        local_only = ?
                    WHERE id = ?
                    """,
                    (
                        now_iso,
                        str(agent_id or "agent"),
                        guard_s.content.strip(),
                        json.dumps(valid_ev),
                        1 if is_priv else 0,
                        full_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO work_events
                    (item_id, namespace, event_type, old_status, new_status, note, agent_id, timestamp)
                    VALUES (?, ?, 'completed', ?, 'done', ?, ?, ?)
                    """,
                    (
                        full_id,
                        self.namespace,
                        old_status,
                        clean_note,
                        str(agent_id or "agent"),
                        now_iso,
                    ),
                )
                updated_row = conn.execute("SELECT * FROM work_items WHERE id = ?", (full_id,)).fetchone()
                res = dict(updated_row)
                res["tags"] = json.loads(res.get("tags") or "[]")
                res["evidence"] = valid_ev
            self._refresh_cache_stamp_after_non_memory_write()
            return res

    def _deserialize_work_item_row(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        d["title"] = decrypt_text_if_encrypted(d.get("title"))
        d["description"] = decrypt_text_if_encrypted(d.get("description") or "")
        if d.get("summary"):
            d["summary"] = decrypt_text_if_encrypted(d["summary"])
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["evidence"] = json.loads(d.get("evidence") or "[]")
        d["local_only"] = bool(d.get("local_only"))
        return d

    def _deserialize_work_event_row(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        if d.get("note"):
            d["note"] = decrypt_text_if_encrypted(d["note"])
        return d

    def get_work_item(self, item_id: str, include_private: bool = True) -> Optional[dict[str, Any]]:
        self._ensure_initialized()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            item = self._resolve_work_item_locked(conn, item_id)
            if not item:
                return None
            if not include_private and item["local_only"]:
                return None
            return self._deserialize_work_item_row(item)

    def list_work_items(
        self,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        parent_id: Optional[str] = None,
        include_private: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self._ensure_initialized()
        safe_limit = max(1, min(1000, int(limit or 50)))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM work_items WHERE namespace = ?"
            params: list[Any] = [self.namespace]

            if not include_private:
                query += " AND local_only = 0"
            if status:
                clean_status = str(status).strip().lower()
                if clean_status == "open":
                    query += " AND status IN ('todo', 'in_progress', 'blocked')"
                elif clean_status == "closed":
                    query += " AND status IN ('done', 'cancelled')"
                else:
                    query += " AND status = ?"
                    params.append(clean_status)
            if kind:
                query += " AND kind = ?"
                params.append(str(kind).strip().lower())
            if parent_id:
                p_item = self._resolve_work_item_locked(conn, parent_id)
                p_uuid = p_item["id"] if p_item else parent_id
                query += " AND parent_id = ?"
                params.append(p_uuid)

            query += (
                " ORDER BY CASE priority WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
                "WHEN 'medium' THEN 3 WHEN 'low' THEN 4 ELSE 5 END, "
                "created_at DESC, id DESC LIMIT ?"
            )
            params.append(safe_limit)

            rows = conn.execute(query, params).fetchall()
            return [self._deserialize_work_item_row(r) for r in rows]

    def get_work_events(self, item_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_initialized()
        safe_limit = max(1, min(1000, int(limit or 50)))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM work_events WHERE namespace = ?"
            params: list[Any] = [self.namespace]
            if item_id:
                target = self._resolve_work_item_locked(conn, item_id)
                target_id = target["id"] if target else item_id
                query += " AND item_id = ?"
                params.append(target_id)
            query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
            params.append(safe_limit)
            return [self._deserialize_work_event_row(r) for r in conn.execute(query, params).fetchall()]

    def get_work_item_stats(self) -> dict[str, Any]:
        self._ensure_initialized()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN kind = 'goal' THEN 1 ELSE 0 END), 0) AS goals,
                    COALESCE(SUM(CASE WHEN kind = 'task' THEN 1 ELSE 0 END), 0) AS tasks,
                    COALESCE(SUM(CASE WHEN status = 'todo' THEN 1 ELSE 0 END), 0) AS todo,
                    COALESCE(SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END), 0) AS in_progress,
                    COALESCE(SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END), 0) AS blocked,
                    COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS done,
                    COALESCE(SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END), 0) AS cancelled
                FROM work_items
                WHERE namespace = ?
                """,
                (self.namespace,),
            ).fetchone()
            if not row:
                return {
                    "total": 0,
                    "by_kind": {"goal": 0, "task": 0},
                    "by_status": {"todo": 0, "in_progress": 0, "blocked": 0, "done": 0, "cancelled": 0},
                }
            return {
                "total": int(row[0]),
                "by_kind": {"goal": int(row[1]), "task": int(row[2])},
                "by_status": {
                    "todo": int(row[3]),
                    "in_progress": int(row[4]),
                    "blocked": int(row[5]),
                    "done": int(row[6]),
                    "cancelled": int(row[7]),
                },
            }

    def get_schema_version(self) -> str:
        self._ensure_initialized()
        with self._connect() as conn:
            try:
                row = conn.execute("SELECT value FROM mesh_meta WHERE key = 'schema_version'").fetchone()
                if row:
                    return str(row[0])
            except sqlite3.OperationalError:
                pass
            try:
                row = conn.execute("SELECT version FROM schema_info").fetchone()
                if row:
                    return str(row[0])
            except sqlite3.OperationalError:
                pass
            return "1"

    def get_project_brief(
        self,
        max_tokens: int = 500,
        include_work_items: bool = True,
        include_private: bool = False,
    ) -> dict[str, Any]:
        self._ensure_initialized()
        open_items = (
            self.list_work_items(status="open", include_private=include_private, limit=10)
            if include_work_items
            else []
        )
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            total_mems = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE namespace = ?",
                (self.namespace,),
            ).fetchone()[0]

            completed_recent = []
            if include_work_items:
                two_days_ago = datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() - 172800, tz=timezone.utc
                ).isoformat()
                c_rows = conn.execute(
                    """
                    SELECT * FROM work_items
                    WHERE namespace = ? AND status = 'done' AND completed_at >= ?
                    ORDER BY completed_at DESC LIMIT 5
                    """,
                    (self.namespace, two_days_ago),
                ).fetchall()
                for r in c_rows:
                    cd = dict(r)
                    cd["tags"] = json.loads(cd.get("tags") or "[]")
                    cd["evidence"] = json.loads(cd.get("evidence") or "[]")
                    completed_recent.append(cd)

            contradictions_count = conn.execute(
                "SELECT COUNT(*) FROM contradictions WHERE namespace = ? AND resolved = 0",
                (self.namespace,),
            ).fetchone()[0]

            if total_mems == 0 and not open_items and not completed_recent:
                msg = (
                    f"# Project Brief: {self.namespace}\n\n"
                    f"*Namespace state:* `new_or_empty` (empty)\n"
                    f"No memories, goals, or tasks exist yet in this namespace. "
                    f"Use `mesh_learn` to record durable conventions/facts and `mesh_task_create` to track work."
                )
                return {
                    "namespace": self.namespace,
                    "namespace_state": "new_or_empty",
                    "total_memories": 0,
                    "open_tasks_count": 0,
                    "completed_recent_count": 0,
                    "pending_contradictions": 0,
                    "brief_markdown": msg,
                    "estimated_tokens": max(1, len(msg) // 4),
                }

            key_mems = []
            priv_filter = "AND local_only = 0" if not include_private else ""
            km_rows = conn.execute(
                f"""
                SELECT id, content, memory_type, confidence, tags
                FROM memories
                WHERE namespace = ? AND quarantined = 0 {priv_filter}
                  AND memory_type IN ('fact', 'decision', 'process', 'preference')
                ORDER BY confidence DESC, accessed_at DESC LIMIT 5
                """,
                (self.namespace,),
            ).fetchall()
            for r in km_rows:
                dec_c = decrypt_text_if_encrypted(r["content"])
                key_mems.append({
                    "id": r["id"],
                    "content": dec_c[:160] + "..." if len(dec_c) > 160 else dec_c,
                    "type": r["memory_type"],
                })

            lines: list[str] = [
                f"# Project Brief: {self.namespace}",
                f"**Health:** {total_mems} memories | {len(open_items)} open work items | {contradictions_count} pending contradictions",
                "",
            ]

            if open_items:
                lines.append("## Open Goals & Tasks")
                for it in open_items[:5]:
                    p_badge = f"[{it['priority'].upper()}]" if it["priority"] in ("high", "critical") else ""
                    lines.append(f"- **{it['short_id']}** {p_badge} ({it['status']}): {it['title']}")
                lines.append("")

            if completed_recent:
                lines.append("## Recently Completed (Past 48h)")
                for it in completed_recent[:4]:
                    summ = it.get("summary") or "Done"
                    if len(summ) > 120:
                        summ = summ[:117] + "..."
                    lines.append(f"- **{it['short_id']}**: {it['title']} — *{summ}*")
                lines.append("")

            if key_mems:
                lines.append("## Key Project Facts & Decisions")
                for km in key_mems[:4]:
                    extra_hint = ""
                    if km["type"] == "decision":
                        content_low = km["content"].lower()
                        causal_markers = ("because", "since", "due to", "as ", "rationale:", "reason:")
                        if not any(marker in content_low for marker in causal_markers):
                            extra_hint = " *(needs rationale — add 'because <reason>')*"
                    lines.append(f"- [{km['type']}] {km['content']}{extra_hint}")
                lines.append("")

            if contradictions_count > 0:
                lines.append(f"> [!WARNING]\n> {contradictions_count} unresolved contradiction(s) detected. Run `mesh_contradictions` to review.")
                lines.append("")

            full_md = "\n".join(lines).strip()
            budget_chars = max_tokens * 4
            if len(full_md) > budget_chars:
                full_md = full_md[:budget_chars].rsplit("\n", 1)[0]
                full_md += "\n\n*(Brief capped at token budget. Call `mesh_recall` or `mesh_task_list` for full details.)*"

            return {
                "namespace": self.namespace,
                "namespace_state": "active",
                "total_memories": total_mems,
                "open_tasks_count": len(open_items),
                "completed_recent_count": len(completed_recent),
                "pending_contradictions": contradictions_count,
                "key_facts_count": len(key_mems),
                "brief_markdown": full_md,
                "estimated_tokens": max(1, len(full_md) // 4),
            }

    def get_changelog(
        self,
        since: str = "7d",
        format: str = "markdown",
        include_private: bool = False,
    ) -> dict[str, Any]:
        self._ensure_initialized()
        now = datetime.now(timezone.utc)
        since_str = str(since or "7d").strip()
        rel_match = re.match(r"^(\d+)\s*([smhdw])$", since_str.lower())
        if rel_match:
            val, unit = int(rel_match.group(1)), rel_match.group(2)
            secs = val * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
            cutoff_iso = datetime.fromtimestamp(now.timestamp() - secs, tz=timezone.utc).isoformat()
        else:
            cutoff_iso = since_str

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            priv_filter = "AND local_only = 0" if not include_private else ""

            c_rows = conn.execute(
                f"""
                SELECT * FROM work_items
                WHERE namespace = ? AND status = 'done' AND completed_at >= ? {priv_filter}
                ORDER BY completed_at DESC
                """,
                (self.namespace, cutoff_iso),
            ).fetchall()
            completed_items = []
            for r in c_rows:
                d = dict(r)
                d["tags"] = json.loads(d.get("tags") or "[]")
                d["evidence"] = json.loads(d.get("evidence") or "[]")
                completed_items.append(d)

            d_rows = conn.execute(
                f"""
                SELECT id, content, tags, created_at, source_agent
                FROM memories
                WHERE namespace = ? AND memory_type = 'decision' AND created_at >= ? {priv_filter}
                ORDER BY created_at DESC
                """,
                (self.namespace, cutoff_iso),
            ).fetchall()
            decisions = []
            for r in d_rows:
                decisions.append({
                    "id": r["id"],
                    "content": decrypt_text_if_encrypted(r["content"]),
                    "created_at": r["created_at"],
                    "source_agent": r["source_agent"],
                })

            lines = [
                f"# Project Changelog: {self.namespace}",
                f"**Window:** Since `{since_str}` ({cutoff_iso[:10]}) | **Completed Items:** {len(completed_items)} | **Decisions:** {len(decisions)}",
                "",
            ]

            lines.append("## Completed Work")
            if not completed_items:
                lines.append("*No completed work items recorded in this window.*")
            else:
                for it in completed_items:
                    lines.append(f"### {it['short_id']}: {it['title']}")
                    lines.append(f"- **Completed at:** {it['completed_at'][:19]} by `{it.get('completed_by') or 'agent'}`")
                    if it.get("summary"):
                        lines.append(f"- **Summary:** {it['summary']}")
                    if it.get("evidence"):
                        lines.append(f"- **Evidence:** {', '.join(f'`{e}`' for e in it['evidence'])}")
                    lines.append("")

            lines.append("## Architectural & Design Decisions")
            if not decisions:
                lines.append("*No new architectural decisions recorded in this window.*")
            else:
                for dec in decisions:
                    lines.append(f"- **[{dec['created_at'][:10]}]** {dec['content']} *(by `{dec['source_agent']}`)*")
                lines.append("")

            changelog_md = "\n".join(lines).strip()
            return {
                "namespace": self.namespace,
                "since": since_str,
                "cutoff": cutoff_iso,
                "completed_count": len(completed_items),
                "decisions_count": len(decisions),
                "completed_items": completed_items,
                "decisions": decisions,
                "changelog": changelog_md,
                "changelog_markdown": changelog_md,
            }

    # Method aliases mirroring MCP tool names (Issue A-006 / B-004)
    task_create = create_work_item
    task_update = update_work_item
    task_complete = complete_work_item
    task_list = list_work_items
    task_get = get_work_item
    log = get_changelog

