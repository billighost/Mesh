"""
mesh/store.py â€” High-performance unified persistence & hybrid search engine for Mesh.

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
import contextvars
import json
import logging
import functools
import heapq
import hashlib
import itertools
import math
import os
import re
from .sqlite_runtime import sqlite3
import stat
import sys
import threading
import time
import uuid
import unicodedata
import warnings
import weakref
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal, Optional, Sequence

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
    mesh_dir_context,
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
from .passages import PASSAGE_FORMAT_VERSION, passage_index_enabled, validate_representation, validate_spans
from .secrets import is_credential, normalize_secret_ref, redact_secrets_local

EMBEDDING_DIM = 384
STALE_THRESHOLD = 0.3
GAP_MIN_QUERIES = 2
DEFAULT_MAX_QUERY_ROWS = 10000
DEFAULT_MAX_POOL_SIZE = 64
DEFAULT_MAX_VECTOR_CACHE_SIZE = 64
DEFAULT_MAX_VECTOR_CACHE_BYTES = 256 * 1024 * 1024
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
    "decay_start_at, local_only, embedding_model, trust_tier, quarantined, secret_ref, revision, incarnation, passage_format_version, passage_count, "
    "(SELECT value FROM mesh_meta WHERE key='store_generation') AS store_generation"
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

_TOKEN_RE = re.compile(r"[\w./:\\-]+", re.UNICODE)
SCHEMA_VERSION: str = "6"
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_PUNCT_SPLIT_RE = re.compile(r"[\W_]+", re.UNICODE)

_LEXICAL_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "about",
    "as", "into", "like", "through", "over", "out",
    "against", "under", "around", "among",
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
    """The configured maximum number of namespace databases would be exceeded."""


class StaleStoreError(RuntimeError):
    """The bound database was deleted or replaced; open a new store deliberately."""


def _file_identity(path: Path) -> Optional[tuple[int, int]]:
    try:
        status = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (status.st_dev, status.st_ino) if stat.S_ISREG(status.st_mode) else None


def _validate_incarnation(value: str, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError(f"{name} must be a lowercase 128-bit identity.")
    return value


def _check_connection_file_identity(conn: sqlite3.Connection) -> None:
    path = getattr(conn, "_mesh_db_path", None)
    if path is not None and _file_identity(path) != conn._mesh_db_identity:
        raise StaleStoreError("The namespace database was deleted or replaced; open a new store.")


def _check_store_generation(conn: sqlite3.Connection, expected: Optional[str]) -> None:
    if expected is None or conn.in_transaction and getattr(conn, "_mesh_active_store_generation", None) == expected:
        return
    try:
        row = conn.execute("SELECT value,(SELECT value FROM mesh_meta WHERE key='namespace_rename_pending') FROM mesh_meta WHERE key='store_generation'").fetchone()
    except sqlite3.OperationalError as exc:
        raise StaleStoreError("The namespace database identity is unavailable; open a new store.") from exc
    if row is None or row[0] != expected:
        raise StaleStoreError("The namespace database was replaced; open a new store.")
    if row[1] is not None:
        raise StaleStoreError("Namespace rename is pending; reopen the store to recover maintenance.")


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
    """Retire LRU database resources without waiting for another namespace.

    Audit and application caches enforce their own bounds. Namespace
    maintenance explicitly invalidates those caches; a writer or vector
    eviction must not flush another namespace while retaining a live lease.
    """
    for db_path, conn, ns in evicted:
        if conn is not None:
            conn.close_pooled()
        with _CACHE_LOCK:
            extra_conn = _CONNECTION_POOL.get(db_path)
            if (extra_conn is None or extra_conn._mesh_users == 0) and not any(key[0] == db_path for key in _VECTOR_CACHE):
                _QUERY_WRITE_COUNTS.pop(db_path, None)
                dict.pop(_INITIALIZED_DBS, db_path, None)
                extra_conn = _CONNECTION_POOL.pop(db_path, None)
                if extra_conn is not None:
                    extra_conn.close_pooled()
        if ns:
            evict_namespace_write_lock(ns, data_root=db_path.parent)


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
        self.users = 0
        self.fd: Optional[int] = None
        self.lock_path: Optional[Path] = None


_NS_LOCK_REGISTRY_MUTEX = threading.Lock()
_NS_LOCK_REGISTRY: dict[tuple[Path, str], _NamespaceLockState] = {}
_OPEN_LOCK_FDS: dict[Path, int] = {}
_OPEN_LOCK_FDS_MUTEX = threading.Lock()


def _get_ns_lock_path(namespace: str, data_root: Optional[Path] = None) -> Path:
    clean_ns = validate_namespace(namespace)
    ensure_secure_mesh_dir(data_root)
    base_dir = data_root.resolve() if data_root is not None else cached_resolved_mesh_dir()
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

    deadline = time.monotonic() + max(0.0, float(timeout))
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
def namespace_write_lock(
    namespace: str, timeout: float = 15.0, *, data_root: Optional[Path] = None,
) -> Iterator[None]:
    """Pin the namespace coordinator before waiting for its thread and process locks."""
    clean_ns = validate_namespace(namespace)
    wait_timeout = float(timeout)
    if not math.isfinite(wait_timeout) or wait_timeout < 0.0:
        raise ValueError("Namespace lock timeout must be finite and nonnegative.")
    mesh_dir = data_root.resolve() if data_root is not None else cached_resolved_mesh_dir()
    key = (mesh_dir, clean_ns.casefold() if os.name == "nt" else clean_ns)
    with _NS_LOCK_REGISTRY_MUTEX:
        state = _NS_LOCK_REGISTRY.get(key)
        if state is None:
            state = _NamespaceLockState(clean_ns)
            _NS_LOCK_REGISTRY[key] = state
        # A waiter owns the reference before acquiring its RLock. Eviction must
        # not split coordinators or close the descriptor while that reference lives.
        state.users += 1

    deadline = time.monotonic() + wait_timeout
    try:
        if not state.rlock.acquire(timeout=wait_timeout):
            raise TimeoutError(
                f"Timed out after {timeout:.1f}s waiting for in-process namespace write lock '{clean_ns}'."
            )
        try:
            if state.depth == 0:
                remaining = max(0.0, deadline - time.monotonic())
                lock_path = _get_ns_lock_path(clean_ns, data_root=mesh_dir)
                state.lock_path = lock_path
                state.fd = _acquire_os_file_lock(lock_path, timeout=remaining)
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
    finally:
        with _NS_LOCK_REGISTRY_MUTEX:
            state.users -= 1

def evict_namespace_write_lock(namespace: str, *, data_root: Optional[Path] = None) -> None:
    """Evict the in-process lock registry entry for a namespace."""
    try:
        clean_ns = validate_namespace(namespace)
    except ValueError:
        return
    mesh_dir = data_root.resolve() if data_root is not None else get_mesh_dir().resolve()
    key = (mesh_dir, clean_ns.casefold() if os.name == "nt" else clean_ns)
    with _NS_LOCK_REGISTRY_MUTEX:
        state = _NS_LOCK_REGISTRY.get(key)
        if state is None or state.users > 0:
            return
        acquired = state.rlock.acquire(timeout=0.0)
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
    s = unicodedata.normalize("NFKC", text).casefold().replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")
    return re.sub(r"'s\b", "", s)


def _fast_stem(w: str) -> str:
    if len(w) <= 3 or not w.isascii():
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


def _extract_search_tokens(
    text: str, *, _normalized: Optional[str] = None,
    _stemmer: Optional[Callable[[str], str]] = None,
    _word_tokens: Optional[list[str]] = None,
) -> tuple[list[str], list[str], set[str]]:
    norm = _clean_possessives(text) if _normalized is None else _normalized
    raw_tokens = [t.strip("._:-/") for t in _TOKEN_RE.findall(norm)]
    raw_tokens = [t for t in raw_tokens if t]
    word_tokens = _WORD_RE.findall(norm) if _word_tokens is None else _word_tokens
    # A no-op camel split has the same normalized words; avoid parsing and
    # normalizing the whole row a second time in that common case.
    if re.search(r"(?<=[a-z0-9])(?=[A-Z])", text):
        camel_words = _WORD_RE.findall(_clean_possessives(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)))
    else:
        camel_words = word_tokens
    compact_ident_tokens: list[str] = []
    for rt in raw_tokens:
        if any(ch in rt for ch in ".-_:/"):
            compact = _PUNCT_SPLIT_RE.sub("", rt)
            if len(compact) >= 3 and compact != rt:
                compact_ident_tokens.append(compact)
    combined = list(dict.fromkeys([*raw_tokens, *compact_ident_tokens, *word_tokens, *camel_words]))
    content_tokens = [t for t in combined if t not in _LEXICAL_STOPWORDS]
    if not content_tokens:
        content_tokens = combined
    stemmer = _fast_stem if _stemmer is None else _stemmer
    stemmed_set = {stemmer(t) for t in content_tokens} | set(content_tokens)
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


def _query_excerpt(content: str, query_tokens: list[str], width: int = 240) -> tuple[str, int, int]:
    """A bounded original-text excerpt near literal evidence, with stable offsets."""
    if len(content) <= width:
        return content, 0, len(content)
    terms = [token for token in query_tokens if token not in _FRAMING_TOKENS][:24]
    if terms:
        pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(token) for token in sorted(terms, key=len, reverse=True)) + r")(?!\w)", re.IGNORECASE)
        matches = list(itertools.islice(pattern.finditer(content), 64))
        best = None
        for match in matches:
            start = max(0, match.start() - width // 3)
            end = min(len(content), start + width)
            excerpt = content[start:end]
            coverage = len({hit.group(0).casefold() for hit in pattern.finditer(excerpt)})
            key = (coverage, len(match.group(0)), -start)
            if best is None or key > best[0]:
                best = (key, excerpt, start, end)
        if best is not None:
            return best[1], best[2], best[3]
    return content[:width], 0, min(width, len(content))


def _content_identity(content: str) -> bytes:
    normalized = " ".join(_clean_possessives(content).split())
    return hashlib.sha256(normalized.encode("utf-8")).digest()


class _RowStemMemo:
    """Short-lived deterministic stems with conservative entry/byte admission.

    A row never shares tokens with another namespace or keeps them after the
    lexical operation. Repeated surface forms need only one Python stem call.
    """

    __slots__ = ("_cache", "_owned_bytes", "_max_entries", "_max_bytes")

    def __init__(self, max_entries: int = 1024, max_bytes: int = 64 * 1024):
        if type(max_entries) is not int or max_entries < 0:
            raise ValueError("Stem memo entry limit must be a nonnegative integer")
        self._cache: dict[str, str] = {}
        self._owned_bytes = 0
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        if type(max_bytes) is not int or max_bytes < self.retained_bytes:
            raise ValueError("Stem memo byte limit cannot be smaller than its empty state")

    @property
    def retained_bytes(self) -> int:
        # Strings and dictionary storage dominate; charge bookkeeping too.
        return self._owned_bytes + sys.getsizeof(self._cache) + sys.getsizeof(self) + 3 * sys.getsizeof(0)

    def __call__(self, token: str) -> str:
        cached = self._cache.get(token)
        if cached is not None:
            return cached
        stem = _fast_stem(token)
        if len(self._cache) >= self._max_entries:
            return stem
        owned = sys.getsizeof(token) + (0 if stem is token else sys.getsizeof(stem)) + 32
        # Reserve dictionary growth before mutation; oversize tokens bypass
        # the memo without changing their lexical value.
        container_peak = max(256, 3 * sys.getsizeof(self._cache))
        projected = self._owned_bytes + owned + container_peak + sys.getsizeof(self) + 3 * sys.getsizeof(0)
        if projected > self._max_bytes:
            return stem
        self._cache[token] = stem
        self._owned_bytes += owned
        if self.retained_bytes > self._max_bytes:
            # Keep the bound on alternative/future Python dictionary layouts.
            self._cache.clear()
            self._owned_bytes = 0
        return stem

def _build_row_lexical_statistics(content: str, tags_list: list[str]) -> tuple[int, set[str]]:
    """Exact DF/length projection for the streaming statistics pass."""
    doc_norm = _clean_possessives(content)
    stemmer = _RowStemMemo()
    word_tokens = _WORD_RE.findall(doc_norm)
    _, doc_tokens, doc_stems = _extract_search_tokens(content, _normalized=doc_norm, _stemmer=stemmer, _word_tokens=word_tokens)
    tag_tokens = {str(t).strip().lower() for t in tags_list if str(t).strip()}
    tag_stems = {stemmer(t) for t in tag_tokens}
    doc_compacts = {_PUNCT_SPLIT_RE.sub("", t) for t in doc_tokens if len(t) >= 2}
    df_stems = doc_stems | tag_stems | {stemmer(t) for t in doc_compacts}
    return max(1, len(word_tokens)), df_stems

def _build_row_lexical_entry(content: str, tags_list: list[str]) -> dict[str, Any]:
    doc_norm = _clean_possessives(content)
    stemmer = _RowStemMemo()
    word_tokens = _WORD_RE.findall(doc_norm)
    _, doc_tokens, doc_stems = _extract_search_tokens(content, _normalized=doc_norm, _stemmer=stemmer, _word_tokens=word_tokens)
    tag_tokens = {str(t).strip().lower() for t in tags_list if str(t).strip()}
    tag_stems = {stemmer(t) for t in tag_tokens}
    tag_compacts = {_PUNCT_SPLIT_RE.sub("", t) for t in tag_tokens if len(t) >= 2}
    doc_compacts = {_PUNCT_SPLIT_RE.sub("", t) for t in doc_tokens if len(t) >= 2}
    all_doc_tokens = set(doc_tokens) | tag_tokens | doc_compacts | tag_compacts
    all_doc_stems = doc_stems | tag_stems | {stemmer(t) for t in doc_compacts}
    canonical_stems = {stem: stem for stem in all_doc_stems}
    # Count repeated words natively, then stem each distinct surface form.
    term_freq = Counter()
    for token, count in Counter(word_tokens).items():
        if token not in _LEXICAL_STOPWORDS:
            stem = stemmer(token)
            term_freq[canonical_stems.get(stem, stem)] += count
    for token in all_doc_tokens:
        stem = stemmer(token)
        term_freq.setdefault(canonical_stems.get(stem, stem), 1)
    return {
        "content_key": hashlib.sha256(" ".join(doc_norm.split()).encode("utf-8")).digest(),
        "doc_len": max(1, len(word_tokens)),
        "term_freq": term_freq,
        "tag_tokens": tag_tokens,
        "tag_stems": tag_stems,
        "tag_compacts": tag_compacts,
        "all_doc_tokens": all_doc_tokens,
        "all_doc_stems": all_doc_stems,
        "df_stems": all_doc_stems,
        "indexed_tokens": all_doc_stems if all_doc_tokens.issubset(all_doc_stems) else all_doc_tokens | all_doc_stems,
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
        self._mesh_db_path: Optional[Path] = None
        self._mesh_db_identity: Optional[tuple[int, int]] = None
        self._mesh_active_store_generation: Optional[str] = None
        self._mesh_owner_thread: Optional[int] = None
        self._mesh_context_transactions: list[bool] = []
        self._mesh_after_commit_actions: list[Callable[[], Any]] = []
        self._mesh_savepoint_actions: dict[str, int] = {}
        self._mesh_cache_dirty = False
        self._mesh_cache_group_active = False
        self._mesh_cache_base: Optional["_VectorCacheEntry"] = None
        self._mesh_cache_base_version: Optional[int] = None
        self._mesh_cache_target_version: Optional[int] = None
        self._mesh_cache_updates: list[tuple[str, str, Any, Any, bool]] = []
        self._mesh_cache_update_bytes = 0
        self._mesh_cache_delta_lease: Optional["_CacheBudgetLease"] = None
        self._mesh_savepoint_cache_states: dict[str, tuple[int, int, Optional[int]]] = {}

    def __enter__(self) -> "_PooledConnection":
        self._mesh_lock.acquire()
        try:
            with self._mesh_state_lock:
                closed = self._mesh_closed
            if closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            super().__enter__()
            self._mesh_context_transactions.append(self.in_transaction)
            self._mesh_owner_thread = threading.get_ident()
            return self
        except BaseException:
            self._mesh_lock.release()
            raise

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Literal[False]:
        try:
            inherited_transaction = self._mesh_context_transactions.pop()
            if not inherited_transaction:
                super().__exit__(exc_type, exc_val, exc_tb)
            return False
        finally:
            if not self._mesh_context_transactions:
                self._mesh_owner_thread = None
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
        became_idle = False
        with self._mesh_state_lock:
            if self._mesh_users > 0:
                self._mesh_users -= 1
                became_idle = self._mesh_users == 0
            if self._mesh_retired and self._mesh_users == 0 and not self._mesh_closed:
                self._mesh_closed = True
                should_close = True
        if should_close:
            self._close_physical()
        elif became_idle:
            # Release the state lock before the registry lock: checkout takes
            # those locks in the opposite order.
            with _WRITER_CONDITION:
                _WRITER_CONDITION.notify_all()

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
        finally:
            with _WRITER_CONDITION:
                _WRITER_CONDITION.notify_all()


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


def _check_namespace_binding(conn: sqlite3.Connection, namespace: str, *, claim: bool = False) -> None:
    try:
        bound = conn.execute("SELECT value FROM mesh_meta WHERE key='namespace_owner'").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return
        raise
    if bound is not None:
        if bound[0] != namespace:
            raise ValueError("Namespace database belongs to another namespace spelling; use the original spelling or a distinct namespace.")
        return
    if not claim:
        return
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    owners: set[str] = set()
    for table in ("memories", "work_items"):
        if table in tables:
            owners.update(str(row[0]) for row in conn.execute(f"SELECT DISTINCT namespace FROM {table} LIMIT 2"))
    if len(owners) > 1:
        raise RuntimeError("Database contains multiple namespaces; isolate them before upgrading Mesh.")
    owner = next(iter(owners), namespace)
    if owner != namespace:
        raise ValueError("Namespace database belongs to another namespace spelling; use the original spelling or a distinct namespace.")
    conn.execute("INSERT OR IGNORE INTO mesh_meta(key,value) VALUES('namespace_owner',?)", (namespace,))


def _begin_write(
    conn: sqlite3.Connection, namespace: Optional[str] = None,
    expected_store_generation: Optional[str] = None,
) -> Optional[str]:
    _check_connection_file_identity(conn)
    if conn.in_transaction:
        _check_store_generation(conn, expected_store_generation)
        if namespace is not None:
            _check_namespace_binding(conn, namespace, claim=True)
        marker = "mesh_" + uuid.uuid4().hex
        conn.execute(f"SAVEPOINT {marker}")
        if hasattr(conn, "_mesh_savepoint_actions"):
            conn._mesh_savepoint_actions[marker] = len(conn._mesh_after_commit_actions)
            conn._mesh_savepoint_cache_states[marker] = (len(conn._mesh_cache_updates), conn._mesh_cache_update_bytes, conn._mesh_cache_target_version)
        return marker
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > int(SCHEMA_VERSION):
            raise RuntimeError("Database schema is newer than supported; no mutation was committed.")
        if namespace is not None:
            _check_namespace_binding(conn, namespace, claim=True)
        _check_store_generation(conn, expected_store_generation)
        if hasattr(conn, "_mesh_active_store_generation"):
            conn._mesh_active_store_generation = expected_store_generation
    except BaseException:
        conn.rollback()
        raise
    return None


def _commit_write(conn: sqlite3.Connection, marker: Optional[str]) -> list[Callable[[], Any]]:
    if marker is None:
        _check_connection_file_identity(conn)
        conn.commit()
        if hasattr(conn, "_mesh_active_store_generation"):
            conn._mesh_active_store_generation = None
        actions = list(getattr(conn, "_mesh_after_commit_actions", ()))
        if hasattr(conn, "_mesh_after_commit_actions"):
            conn._mesh_after_commit_actions.clear()
            conn._mesh_savepoint_actions.clear()
            conn._mesh_savepoint_cache_states.clear()
        return actions
    else:
        conn.execute(f"RELEASE SAVEPOINT {marker}")
        if hasattr(conn, "_mesh_savepoint_actions"):
            conn._mesh_savepoint_actions.pop(marker, None)
            conn._mesh_savepoint_cache_states.pop(marker, None)
        return []


def _rollback_write(conn: sqlite3.Connection, marker: Optional[str]) -> None:
    if not conn.in_transaction:
        if hasattr(conn, "_mesh_active_store_generation"):
            conn._mesh_active_store_generation = None
        return
    if marker is None:
        conn.rollback()
        if hasattr(conn, "_mesh_active_store_generation"):
            conn._mesh_active_store_generation = None
        if hasattr(conn, "_mesh_after_commit_actions"):
            conn._mesh_after_commit_actions.clear()
            conn._mesh_savepoint_actions.clear()
            conn._mesh_savepoint_cache_states.clear()
    else:
        conn.execute(f"ROLLBACK TO SAVEPOINT {marker}")
        conn.execute(f"RELEASE SAVEPOINT {marker}")
        if hasattr(conn, "_mesh_savepoint_actions"):
            offset = conn._mesh_savepoint_actions.pop(marker, len(conn._mesh_after_commit_actions))
            del conn._mesh_after_commit_actions[offset:]
            cache_state = conn._mesh_savepoint_cache_states.pop(marker, None)
            if cache_state is not None:
                length, size, version = cache_state
                del conn._mesh_cache_updates[length:]
                conn._mesh_cache_update_bytes = size if not conn._mesh_cache_dirty else 0
                conn._mesh_cache_target_version = version
                if conn._mesh_cache_delta_lease is not None:
                    conn._mesh_cache_delta_lease.resize(conn._mesh_cache_update_bytes)


@contextlib.contextmanager
def _schema_transaction(conn: sqlite3.Connection, store: "MeshStore") -> Iterator[None]:
    store._prepare_schema_upgrade(conn)
    marker = _begin_write(conn, store.namespace, store._store_generation)
    try:
        yield
        _commit_write(conn, marker)
    except BaseException:
        _rollback_write(conn, marker)
        raise


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
    if not np.all(np.isfinite(arr)):
        raise ValueError("Embedding values must be finite numbers.")
    norm = float(np.linalg.norm(arr.astype(np.float64)))
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
        blob = plain
    if not blob or len(blob) % 4:
        return np.empty(0, dtype=np.float32)
    arr = np.frombuffer(blob, dtype=np.float32)
    return arr if np.all(np.isfinite(arr)) else np.empty(0, dtype=np.float32)


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


class _ExcludedIds:
    """A nonmaterialized union of stable policy index sets."""

    __slots__ = ("groups",)

    def __init__(self, groups: tuple[set[str], ...]):
        self.groups = groups

    def __contains__(self, memory_id: str) -> bool:
        return any(memory_id in group for group in self.groups)

    def __iter__(self) -> Iterator[str]:
        for index, group in enumerate(self.groups):
            for memory_id in group:
                if not any(memory_id in prior for prior in self.groups[:index]):
                    yield memory_id


class _QueryIds:
    """Eligible IDs without copying an entire namespace's metadata or policy sets."""

    __slots__ = ("base", "excluded", "size")

    def __init__(self, base: set[str], excluded: tuple[set[str], ...], *, size: Optional[int] = None):
        self.base = base
        self.excluded = _ExcludedIds(excluded)
        self.size = sum(mid not in self.excluded for mid in base) if size is None else size

    def __contains__(self, memory_id: str) -> bool:
        return memory_id in self.base and memory_id not in self.excluded

    def __iter__(self) -> Iterator[str]:
        return (mid for mid in self.base if mid not in self.excluded)

    def __len__(self) -> int:
        return self.size


class CacheSnapshotInvalidatedError(RuntimeError):
    """The caller must acquire a fresh namespace generation before retrying."""


def _retry_cache_snapshot(method):
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except CacheSnapshotInvalidatedError:
            # Snapshot contexts have unwound. Refreshing here avoids acquiring
            # a registry/SQL lock while holding a namespace read snapshot.
            try:
                self._get_vector_cache(force_refresh=True)
            except CacheBudgetExceededError:
                pass
            return method(self, *args, **kwargs)
    return run


class CacheBudgetExceededError(RuntimeError):
    """Use bounded streaming operations when a namespace cannot fit the cache budget."""

    def __init__(self, message: str, *, required_bytes: int = 0) -> None:
        super().__init__(message)
        self.required_bytes = required_bytes


def _get_max_vector_cache_bytes() -> int:
    return env_int("MESH_MAX_VECTOR_CACHE_BYTES", DEFAULT_MAX_VECTOR_CACHE_BYTES, minimum=1024)


def get_vector_cache_stats() -> dict[str, int]:
    """Return allocation charges, including evicted generations still held by readers."""
    with _CACHE_LOCK:
        return {
            "limit_bytes": _get_max_vector_cache_bytes(),
            "charged_bytes": _CACHE_BUDGET_BYTES,
            "cached_generations": len(_VECTOR_CACHE),
            "live_generations_and_builds": len(_CACHE_BUDGET_LEASES),
            "oversized_versions": len(_CACHE_REJECTIONS),
        }


class _CacheBudgetLease:
    def __init__(self, key: tuple[Path, str]) -> None:
        self.key = key
        self.token = uuid.uuid4().hex
        self.bytes = 0
        self.released = False
        with _CACHE_LOCK:
            _CACHE_BUDGET_LEASES[self.token] = 0

    def resize(self, size: int) -> None:
        global _CACHE_BUDGET_BYTES
        with _CACHE_LOCK:
            if self.released:
                raise RuntimeError("Cache allocation lease has been released.")
            limit = _get_max_vector_cache_bytes()
            growth = size - self.bytes
            if growth > 0:
                # Dropping an LRU reference only reclaims its charge when no reader
                # retains the generation. Never retire writer connections here.
                for victim_key in list(_VECTOR_CACHE):
                    if _CACHE_BUDGET_BYTES + growth <= limit:
                        break
                    if victim_key == self.key:
                        continue
                    victim = _VECTOR_CACHE.pop(victim_key)
                    del victim
                if _CACHE_BUDGET_BYTES + growth > limit:
                    raise CacheBudgetExceededError(
                        "Namespace cache exceeds available allocation budget; use bounded streaming retrieval.",
                        required_bytes=size,
                    )
            _CACHE_BUDGET_BYTES += growth
            self.bytes = size
            _CACHE_BUDGET_LEASES[self.token] = size

    def release(self) -> None:
        global _CACHE_BUDGET_BYTES
        with _CACHE_LOCK:
            if not self.released:
                _CACHE_BUDGET_BYTES -= self.bytes
                _CACHE_BUDGET_LEASES.pop(self.token, None)
                self.bytes = 0
                self.released = True


def _object_graph_bytes(value: Any, seen: Optional[set[int]] = None) -> int:
    """Conservatively charge Python objects without counting shared references twice."""
    if seen is None:
        seen = set()
    ident = id(value)
    if ident in seen:
        return 0
    seen.add(ident)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_object_graph_bytes(key, seen) + _object_graph_bytes(item, seen) for key, item in value.items())
    elif isinstance(value, (tuple, list, set, frozenset)):
        size += sum(_object_graph_bytes(item, seen) for item in value)
    return size


_CACHE_TOP_FIELDS = (
    "ids", "id_to_idx", "all_ids", "rows_by_id", "lexical_by_id", "stem_df",
    "stem_to_ids", "token_to_ids", "lexical_strings", "needs_healing_ids", "private_ids", "quarantine_ids",
    "model_ids", "type_ids", "tag_ids", "agent_ids", "content_ids", "visible_stem_df",
    "_row_charges",
)
_CACHE_TEXT_FIELDS = (
    "id", "namespace", "content", "tags", "memory_type", "source_agent", "created_at",
    "updated_at", "accessed_at", "decay_start_at", "embedding_model", "trust_tier", "secret_ref",
)


def _cache_row_peak_bytes(row: Any) -> int:
    chars = sum(len(str((row.get(name) if isinstance(row, dict) else row[name]) or "")) for name in _CACHE_TEXT_FIELDS)
    # Includes one SQLite row, decoded/encrypted values, lexical construction,
    # object-size traversal scratch space, and simultaneous old/new metadata.
    return 16384 + 256 * chars


def _matrix_growth_capacity(capacity: int, required: int) -> int:
    return max(required, capacity + max(16, capacity // 8))


@dataclass(frozen=True, slots=True)
class _HybridQuerySpec:
    vector: np.ndarray
    tokens: list[str]
    stems: list[str]
    synonyms: dict[str, set[str]]
    stem_synonyms: dict[str, set[str]]
    phrase: str
    compact: str
    literals: list[str]
    identifier_only: bool
    pool: int
    mode: str


@dataclass(slots=True)
class _ScoredMemoryID:
    score: float
    memory_id: str

    def __lt__(self, other: "_ScoredMemoryID") -> bool:
        if self.score != other.score:
            return self.score < other.score
        return self.memory_id > other.memory_id


def _keep_best_scored_id(heap: list[_ScoredMemoryID], score: float, memory_id: str, limit: int) -> None:
    if len(heap) < limit:
        heapq.heappush(heap, _ScoredMemoryID(score, memory_id))
    elif score > heap[0].score or (score == heap[0].score and memory_id < heap[0].memory_id):
        heapq.heapreplace(heap, _ScoredMemoryID(score, memory_id))


class _ReadWriteLock:
    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._readers: dict[int, int] = {}
        self._writer: Optional[int] = None
        self._depth = 0
        self._waiting = 0

    @contextlib.contextmanager
    def read(self) -> Iterator[None]:
        ident = threading.get_ident()
        with self._condition:
            while self._writer not in (None, ident) or (self._waiting and ident not in self._readers and self._writer != ident):
                self._condition.wait()
            self._readers[ident] = self._readers.get(ident, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                count = self._readers[ident] - 1
                if count:
                    self._readers[ident] = count
                else:
                    self._readers.pop(ident)
                self._condition.notify_all()

    @contextlib.contextmanager
    def write(self) -> Iterator[None]:
        ident = threading.get_ident()
        with self._condition:
            if self._writer == ident:
                self._depth += 1
            else:
                if ident in self._readers:
                    raise RuntimeError("A retrieval snapshot cannot be upgraded to a write lock.")
                self._waiting += 1
                try:
                    while self._writer is not None or self._readers:
                        self._condition.wait()
                    self._writer = ident
                    self._depth = 1
                finally:
                    self._waiting -= 1
        try:
            yield
        finally:
            with self._condition:
                self._depth -= 1
                if not self._depth:
                    self._writer = None
                    self._condition.notify_all()


class _VectorCacheEntry:
    build_count: int = 0

    __slots__ = (
        "__weakref__",
        "_lease",
        "_cache_bytes",
        "_build_peak_bytes",
        "_row_charges",
        "_rows_bytes",
        "_index_bytes",
        "_rw",
        "_valid",
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
        "lexical_strings",
        "needs_healing_ids",
        "private_ids",
        "quarantine_ids",
        "model_ids",
        "type_ids",
        "tag_ids",
        "agent_ids",
        "doc_len_total",
        "content_ids",
        "visible_stem_df",
        "visible_doc_len_total",
        "visible_id_count",
        "passages",
        "passage_index_dirty",
        "passage_index_enabled",
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
        owned_matrix: bool = False,
        budget_lease: Optional[_CacheBudgetLease] = None,
    ):
        global _CACHE_REBUILD_COUNT
        self._lease = budget_lease
        self._cache_bytes = 0
        self._build_peak_bytes = 0
        self._row_charges: dict[str, int] = {}
        self._rows_bytes = 0
        self._index_bytes = 0
        self._rw = _ReadWriteLock()
        self._valid = True
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
        self.token_to_ids: dict[str, set[str]] = self.stem_to_ids
        self.lexical_strings: dict[str, str] = {}
        self.private_ids: set[str] = set()
        self.quarantine_ids: set[str] = set()
        self.model_ids: dict[str, set[str]] = {}
        self.type_ids: dict[str, set[str]] = {}
        self.tag_ids: dict[str, set[str]] = {}
        self.agent_ids: dict[str, set[str]] = {}
        self.doc_len_total: int = 0
        self.content_ids: dict[bytes, set[str]] = {}
        self.visible_stem_df: dict[str, int] = {}
        self.visible_doc_len_total = 0
        self.visible_id_count = 0
        self.passages = None
        self.passage_index_dirty = False
        self.passage_index_enabled = passage_index_enabled()
        self.needs_healing_ids: set[str] = (
            set(needs_healing_ids) if needs_healing_ids is not None else set()
        )

        if matrix is not None and matrix.ndim == 2 and matrix.shape[0] > 0:
            n_rows, dim = int(matrix.shape[0]), int(matrix.shape[1])
            cap = n_rows
            if owned_matrix and matrix.dtype == np.float32 and matrix.flags.c_contiguous:
                buf = matrix
            else:
                buf = np.empty((cap, dim), dtype=np.float32)
                buf[:n_rows] = matrix
            self._matrix_buf: Optional[np.ndarray] = buf
            self.dim: int = dim

            self._hash_matrix_buf: Optional[np.ndarray] = None
        else:
            self._matrix_buf = None
            self._hash_matrix_buf = None
            self.dim = 0

        for mid, row_meta in self.rows_by_id.items():
            self._index_row_lexical(mid, row_meta)
            if needs_healing_ids is None:
                idx = self.id_to_idx.get(mid)
                vec = self._matrix_buf[idx] if (idx is not None and self._matrix_buf is not None) else None
                if _row_needs_healing(row_meta, vec):
                    self.needs_healing_ids.add(mid)

        _VectorCacheEntry.build_count += 1
        _CACHE_REBUILD_COUNT += 1
        if budget_lease is not None:
            self._cache_bytes = self._top_bytes()
            weakref.finalize(self, budget_lease.release)

    def _ensure_lexical_posting_mode(self, lexical: dict[str, Any], *, temporary_peak_bytes: int = 0) -> None:
        if self.token_to_ids is not self.stem_to_ids or lexical["indexed_tokens"] == lexical["df_stems"]:
            return
        original = self.stem_to_ids
        working_peak = max(temporary_peak_bytes, self._build_peak_bytes)
        if self._lease is not None:
            clone_peak = 512 + 2 * (sys.getsizeof(original) + sum(sys.getsizeof(key) + sys.getsizeof(value) for key, value in original.items()))
            self._lease.resize(self._cache_bytes + working_peak + clone_peak)
        # Diverging tag literals need independent membership; retain shared
        # strings/IDs but copy every mutable posting set before either changes.
        try:
            copied = {key: set(value) for key, value in original.items()}
        except BaseException:
            if self._lease is not None:
                self._lease.resize(self._cache_bytes + working_peak)
            raise
        self.token_to_ids = copied
        if self._lease is not None:
            # Charge the entire new index, including keys outside this row's
            # selected delta, before incremental accounting resumes.
            self._index_bytes += sum(
                (0 if self.lexical_strings.get(key) is key else sys.getsizeof(key)) + sys.getsizeof(value)
                for key, value in copied.items()
            )
            self._cache_bytes = self._top_bytes() + self._rows_bytes + self._index_bytes
            self._lease.resize(self._cache_bytes + working_peak)

    def _canonicalize_row_lexical(self, lexical: dict[str, Any]) -> dict[str, str]:
        # Only indexed strings have posting membership that can own their
        # lifetime. Counter-only tag stems remain independently row-owned.
        canonical = {token: self.lexical_strings.get(token, token) for token in lexical["indexed_tokens"]}
        remapped: dict[int, set[str]] = {}
        for name in (
            "tag_tokens", "tag_stems", "tag_compacts", "all_doc_tokens",
            "all_doc_stems", "df_stems", "indexed_tokens",
        ):
            original = lexical[name]
            identity = id(original)
            if identity not in remapped:
                remapped[identity] = {canonical.get(token, token) for token in original}
            lexical[name] = remapped[identity]
        lexical["term_freq"] = Counter({canonical.get(token, token): count for token, count in lexical["term_freq"].items()})
        return canonical

    def _index_row_lexical(self, memory_id: str, row_meta: dict[str, Any]) -> None:
        content = str(row_meta.get("content", ""))
        tags_raw = row_meta.get("tags")
        tags_list = row_meta.get("tags_list") or (_parse_tags_json(tags_raw) if tags_raw else [])
        row_meta["tags_list"] = tags_list

        row_meta.pop("_prepared_search_tokens", None)
        new_lex = row_meta.pop("_prepared_lexical", None)
        if new_lex is None:
            new_lex = _build_row_lexical_entry(content, tags_list)
        canonical = self._canonicalize_row_lexical(new_lex)
        lowered_tags = (tag.lower() for tag in tags_list)
        row_meta["_tags_low_set"] = frozenset(canonical.get(tag, tag) for tag in lowered_tags)
        self._ensure_lexical_posting_mode(new_lex)
        self._unindex_row_lexical(memory_id)
        indexed_tokens = new_lex["indexed_tokens"]
        row_meta["_indexed_tokens"] = indexed_tokens
        for tok in indexed_tokens:
            self.lexical_strings.setdefault(tok, tok)
            self.token_to_ids.setdefault(tok, set()).add(memory_id)

        new_lex["policy"] = {
            "private": bool(row_meta.get("local_only")),
            "quarantine": bool(row_meta.get("quarantined")),
            "model": str(row_meta.get("embedding_model") or "unknown"),
            "type": str(row_meta.get("memory_type") or "fact").lower(),
            "agent": str(row_meta.get("source_agent") or "unknown"),
            "tags": row_meta["_tags_low_set"],
        }
        self.lexical_by_id[memory_id] = new_lex
        self.content_ids.setdefault(new_lex["content_key"], set()).add(memory_id)
        self.doc_len_total += new_lex["doc_len"]
        policy = new_lex["policy"]
        if not policy["private"] and not policy["quarantine"]:
            self.visible_doc_len_total += new_lex["doc_len"]
            self.visible_id_count += 1
            for stem in new_lex["df_stems"]:
                self.visible_stem_df[stem] = self.visible_stem_df.get(stem, 0) + 1
        if policy["private"]:
            self.private_ids.add(memory_id)
        if policy["quarantine"]:
            self.quarantine_ids.add(memory_id)
        for index, value in (
            (self.model_ids, policy["model"]), (self.type_ids, policy["type"]), (self.agent_ids, policy["agent"]),
        ):
            index.setdefault(value, set()).add(memory_id)
        for tag in policy["tags"]:
            self.tag_ids.setdefault(tag, set()).add(memory_id)
        for s in new_lex["df_stems"]:
            self.stem_df[s] = self.stem_df.get(s, 0) + 1
            self.stem_to_ids.setdefault(s, set()).add(memory_id)

    def _unindex_row_lexical(self, memory_id: str) -> None:
        old_lex = self.lexical_by_id.pop(memory_id, None)
        if old_lex is not None:
            equal_members = self.content_ids.get(old_lex["content_key"])
            if equal_members is not None:
                equal_members.discard(memory_id)
                if not equal_members:
                    self.content_ids.pop(old_lex["content_key"], None)
            for tok in old_lex["indexed_tokens"]:
                t_set = self.token_to_ids.get(tok)
                if t_set is not None:
                    t_set.discard(memory_id)
                    if not t_set:
                        self.token_to_ids.pop(tok, None)
                        self.lexical_strings.pop(tok, None)
            self.doc_len_total -= old_lex["doc_len"]
            self.private_ids.discard(memory_id)
            self.quarantine_ids.discard(memory_id)
            policy = old_lex["policy"]
            if not policy["private"] and not policy["quarantine"]:
                self.visible_doc_len_total -= old_lex["doc_len"]
                self.visible_id_count -= 1
                for stem in old_lex["df_stems"]:
                    remaining = self.visible_stem_df.get(stem, 1) - 1
                    if remaining > 0:
                        self.visible_stem_df[stem] = remaining
                    else:
                        self.visible_stem_df.pop(stem, None)
            for index, value in (
                (self.model_ids, policy["model"]), (self.type_ids, policy["type"]), (self.agent_ids, policy["agent"]),
            ):
                members = index.get(value)
                if members is not None:
                    members.discard(memory_id)
                    if not members:
                        index.pop(value, None)
            for tag in policy["tags"]:
                members = self.tag_ids.get(tag)
                if members is not None:
                    members.discard(memory_id)
                    if not members:
                        self.tag_ids.pop(tag, None)
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

    @contextlib.contextmanager
    def read_snapshot(self) -> Iterator["_VectorCacheEntry"]:
        with self._rw.read():
            if not self._valid:
                raise CacheSnapshotInvalidatedError("Namespace cache changed; acquire a fresh snapshot.")
            yield self

    def write_snapshot(self) -> Any:
        return self._rw.write()

    def snapshot(
        self,
        copy_matrix: bool = True,
    ) -> tuple[list[str], Optional[np.ndarray], dict[str, dict[str, Any]]]:
        with self.read_snapshot():
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
        with self.read_snapshot():
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
        with self.write_snapshot():
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
        with self.write_snapshot():
            try:
                return self._upsert_vector(memory_id, vec, row_meta)
            except (CacheBudgetExceededError, MemoryError):
                # A cache allocation is optional; an already committed write
                # must still succeed when memory admission is exhausted.
                self._valid = False
                return False

    def _top_bytes(self) -> int:
        total = 4096
        seen = set()
        for name in _CACHE_TOP_FIELDS:
            if not hasattr(self, name):
                continue
            value = getattr(self, name)
            if id(value) not in seen:
                seen.add(id(value))
                total += sys.getsizeof(value)
        for name in ("_matrix_buf", "_hash_matrix_buf"):
            buf = getattr(self, name, None)
            if buf is not None:
                total += int(buf.nbytes) + 128
        if self.passages is not None:
            total += self.passages.allocated_bytes
        return total

    @staticmethod
    def _index_keys(row: Optional[dict[str, Any]], lexical: Optional[dict[str, Any]]) -> dict[str, set[Any]]:
        if row is None:
            return {}
        lex = lexical or row.get("_prepared_lexical") or {}
        search = row.get("_prepared_search_tokens") or row.get("_search_tokens") or ([], [], set())
        tokens = lex.get("indexed_tokens")
        if tokens is None:
            tokens = (set(search[0]) | set(search[2])) if row.get("_prepared_search_tokens") is not None else row.get("_indexed_tokens", set())
        stems = set(lex.get("df_stems", ()))
        tags = row.get("_tags_low_set") or {str(tag).lower() for tag in row.get("tags_list", ())}
        content_key = lex.get("content_key") or hashlib.sha256(str(row.get("content", "")).encode("utf-8")).digest()
        return {
            "stem_df": stems,
            "stem_to_ids": stems,
            "visible_stem_df": stems,
            "token_to_ids": set(tokens),
            "lexical_strings": set(tokens),
            "model_ids": {str(row.get("embedding_model") or "unknown")},
            "type_ids": {str(row.get("memory_type") or "fact").lower()},
            "tag_ids": set(tags),
            "agent_ids": {str(row.get("source_agent") or "unknown")},
            "content_ids": {content_key},
        }

    def _index_value_bytes(self, keys: dict[str, set[Any]]) -> int:
        selected_indexes: dict[int, tuple[Any, set[Any]]] = {}
        for name, selected in keys.items():
            index = getattr(self, name, None)
            if index is None:
                continue
            identity = id(index)
            if identity in selected_indexes:
                selected_indexes[identity][1].update(selected)
            else:
                selected_indexes[identity] = (index, set(selected))
        canonical_indexes = {
            id(self.stem_to_ids), id(self.token_to_ids), id(self.stem_df), id(self.visible_stem_df),
        }
        total = 0
        for index, selected in selected_indexes.values():
            for key in selected:
                if key not in index:
                    continue
                if index is self.lexical_strings:
                    total += sys.getsizeof(index[key])
                else:
                    if id(index) not in canonical_indexes or self.lexical_strings.get(key) is not key:
                        total += sys.getsizeof(key)
                    total += sys.getsizeof(index[key])
        return total

    def _upsert_vector(
        self,
        memory_id: str,
        vec: Optional[np.ndarray],
        row_meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        if self._lease is None:
            return self._upsert_vector_unmetered(memory_id, vec, row_meta)
        peak = _cache_row_peak_bytes(row_meta) if row_meta is not None else 16384
        matrix_peak = 0
        if vec is not None and memory_id not in self.id_to_idx:
            capacity = int(self._matrix_buf.shape[0]) if self._matrix_buf is not None else 0
            if len(self.ids) >= capacity:
                matrix_peak = _matrix_growth_capacity(capacity, len(self.ids) + 1) * EMBEDDING_DIM * 4 + 128
        self._lease.resize(self._cache_bytes + max(peak, self._build_peak_bytes) + matrix_peak)
        prepared = None
        if row_meta is not None:
            prepared = dict(row_meta)
            tags = prepared.get("tags_list") or _parse_tags_json(prepared.get("tags") or "[]")
            prepared["tags_list"] = tags
            text = str(prepared.get("content", ""))
            prepared["_prepared_lexical"] = _build_row_lexical_entry(text, tags)
            self._canonicalize_row_lexical(prepared["_prepared_lexical"])
            if "indexed_tokens" not in prepared["_prepared_lexical"]:
                prepared["_prepared_search_tokens"] = _extract_search_tokens(f"{text} {' '.join(tags)}" if tags else text)
        if prepared is not None:
            self._ensure_lexical_posting_mode(prepared["_prepared_lexical"], temporary_peak_bytes=max(peak, self._build_peak_bytes) + matrix_peak)
        keys = self._index_keys(self.rows_by_id.get(memory_id), self.lexical_by_id.get(memory_id))
        for name, selected in self._index_keys(prepared, None).items():
            keys.setdefault(name, set()).update(selected)
        before_index = self._index_value_bytes(keys)
        array_bytes = sum(int(buf.nbytes) + 128 for buf in (self._matrix_buf, self._hash_matrix_buf) if buf is not None)
        container_peak = 2 * (self._top_bytes() - array_bytes + before_index)
        self._lease.resize(self._cache_bytes + max(peak, self._build_peak_bytes) + matrix_peak + container_peak)
        old_row_charge = self._row_charges.get(memory_id, 0)
        result = self._upsert_vector_unmetered(memory_id, vec, prepared)
        if row_meta is not None:
            # The posting pool owns these strings once for the generation.
            # Counter-only tokens outside indexed membership stay row-owned.
            seen = {
                id(token) for token in self.lexical_by_id[memory_id]["indexed_tokens"]
                if self.lexical_strings.get(token) is token
            }
            row_charge = _object_graph_bytes(self.rows_by_id[memory_id], seen)
            row_charge += _object_graph_bytes(self.lexical_by_id.get(memory_id), seen) + 64
            self._row_charges[memory_id] = row_charge
            self._rows_bytes += row_charge - old_row_charge
        self._index_bytes += self._index_value_bytes(keys) - before_index
        self._cache_bytes = self._top_bytes() + self._rows_bytes + self._index_bytes
        self._lease.resize(self._cache_bytes + self._build_peak_bytes)
        return result

    def _upsert_vector_unmetered(
        self,
        memory_id: str,
        vec: Optional[np.ndarray],
        row_meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        if self.passages is not None and memory_id in self.passages.ranges:
            self.passage_index_dirty = True
        self.all_ids.add(memory_id)
        if row_meta is not None:
            self._unindex_row_lexical(memory_id)
            self.rows_by_id[memory_id] = dict(row_meta)
            self._index_row_lexical(memory_id, self.rows_by_id[memory_id])
            if _row_needs_healing(self.rows_by_id[memory_id], vec):
                self.needs_healing_ids.add(memory_id)
            else:
                self.needs_healing_ids.discard(memory_id)

        if vec is None or vec.size == 0:
            self._remove_vector(memory_id)
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
        if self._matrix_buf is None:
            dim = int(vec_1d.shape[0])
            buf = np.empty((_matrix_growth_capacity(0, 1), dim), dtype=np.float32)
            buf[0] = vec_1d
            self._matrix_buf = buf
            self.dim = dim
            self.ids = [memory_id]
            self.id_to_idx = {memory_id: 0}
            return True

        if vec_1d.shape[0] != self.dim:
            return False

        if n >= self._matrix_buf.shape[0]:
            new_cap = _matrix_growth_capacity(int(self._matrix_buf.shape[0]), n + 1)
            new_buf = np.empty((new_cap, self.dim), dtype=np.float32)
            new_buf[:n] = self._matrix_buf[:n]
            self._matrix_buf = new_buf

        self._matrix_buf[n] = vec_1d

        self.id_to_idx[memory_id] = n
        self.ids.append(memory_id)
        return True

    def delete_vector(self, memory_id: str) -> None:
        with self.write_snapshot():
            self._delete_vector(memory_id)

    def _delete_vector(self, memory_id: str) -> None:
        if self._lease is not None:
            keys = self._index_keys(self.rows_by_id.get(memory_id), self.lexical_by_id.get(memory_id))
            before_index = self._index_value_bytes(keys)
            self._delete_vector_unmetered(memory_id)
            self._rows_bytes -= self._row_charges.pop(memory_id, 0)
            self._index_bytes += self._index_value_bytes(keys) - before_index
            self._cache_bytes = self._top_bytes() + self._rows_bytes + self._index_bytes
            self._lease.resize(self._cache_bytes + self._build_peak_bytes)
            return
        self._delete_vector_unmetered(memory_id)

    def _delete_vector_unmetered(self, memory_id: str) -> None:
        if self.passages is not None and memory_id in self.passages.ranges:
            self.passage_index_dirty = True
        self.all_ids.discard(memory_id)
        self.needs_healing_ids.discard(memory_id)
        self._unindex_row_lexical(memory_id)
        self.rows_by_id.pop(memory_id, None)
        self._remove_vector(memory_id)

    def _remove_vector(self, memory_id: str) -> None:
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
        self.ids.pop()
        if not self.ids or self._matrix_buf is None:
            self._matrix_buf = None
            self._hash_matrix_buf = None
            self.dim = 0


_CACHE_LOCK = threading.RLock()
_WRITER_CONDITION = threading.Condition(_CACHE_LOCK)
_CONNECTION_POOL: OrderedDict[Path, _PooledConnection] = OrderedDict()
_WRITER_OPENING: set[Path] = set()
_DB_PATH_NAMESPACES: dict[Path, str] = {}
_VECTOR_CACHE: OrderedDict[tuple[Path, str], _VectorCacheEntry] = OrderedDict()
_CACHE_BUDGET_BYTES = 0
_CACHE_BUDGET_LEASES: dict[str, int] = {}
_CACHE_REJECTIONS: OrderedDict[tuple[Path, str], tuple[Optional[int], int]] = OrderedDict()
_STORE_COORDINATORS: weakref.WeakValueDictionary[tuple[Path, str], "_StoreCoordinator"] = weakref.WeakValueDictionary()
_READER_CONDITION = threading.Condition(threading.RLock())
_READER_CONNECTION_POOL: OrderedDict[tuple[Path, int], _PooledConnection] = OrderedDict()
_READER_OPENING: dict[Path, int] = {}
_READER_NEXT_ID = 0


class _ReaderCheckout(_ConnectionCheckout):
    def _checkin(self) -> None:
        was_checked_in = self._checked_in
        super()._checkin()
        if not was_checked_in:
            with _READER_CONDITION:
                _READER_CONDITION.notify_all()


def close_store_readers(db_path: Optional[Path] = None) -> None:
    with _READER_CONDITION:
        keys = [key for key in _READER_CONNECTION_POOL if db_path is None or key[0] == db_path]
        retired = [_READER_CONNECTION_POOL.pop(key) for key in keys]
        _READER_CONDITION.notify_all()
    for conn in retired:
        conn.retire()


class _StoreCoordinator:
    def __init__(self) -> None:
        self.lock = threading.RLock()

    def __enter__(self) -> "_StoreCoordinator":
        self.lock.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Literal[False]:
        self.lock.release()
        return False


def _get_store_coordinator(db_path: Path, namespace: str) -> _StoreCoordinator:
    with _CACHE_LOCK:
        key = (db_path, namespace.casefold() if os.name == "nt" else namespace)
        coordinator = _STORE_COORDINATORS.get(key)
        if coordinator is None:
            coordinator = _StoreCoordinator()
            _STORE_COORDINATORS[key] = coordinator
        return coordinator


def _evict_vector_cache_for_db_path(db_path: Path) -> None:
    for key in [k for k in _VECTOR_CACHE if k[0] == db_path]:
        _VECTOR_CACHE.pop(key, None)


_QUERY_WRITE_COUNTS: dict[Path, int] = {}
_TRANSIENT_EMBEDDINGS: dict[tuple[str, str], np.ndarray] = {}


# --- Batched Access Tracking Buffer ---


class _AccessTrackingBuffer:
    def __init__(self, db_path: Path, namespace: str, store_generation: Optional[str] = None) -> None:
        self.db_path = db_path
        self.namespace = namespace
        self.store_generation = store_generation
        self.access_increments: dict[str, tuple[int, str]] = {}
        self.access_incarnations: dict[str, str] = {}
        self.query_rows: list[tuple[str, str, str, str, Optional[float], int, str, int]] = []
        self.event_count: int = 0
        self.first_event_ts: float = 0.0
        self.byte_charge = 1024 + 4 * (len(str(db_path)) + len(namespace))


_ACCESS_TRACKING_LOCK = threading.RLock()
_ACCESS_TRACKING_BUFFERS: dict[Path, _AccessTrackingBuffer] = {}
_ACCESS_TRACKING_INFLIGHT_ENTRIES = 0
_ACCESS_TRACKING_INFLIGHT_BYTES = 0
_ACCESS_TRACKING_DROPPED_ENTRIES = 0


def _access_entry_bytes(memory_id: str, timestamp: str) -> int:
    return 512 + 4 * (len(memory_id) + len(timestamp) + 32)


def _query_entry_bytes(row: tuple[Any, ...]) -> int:
    return 512 + 4 * sum(len(value) for value in row if isinstance(value, str))


def _tracking_charge_locked() -> tuple[int, int]:
    return (
        _ACCESS_TRACKING_INFLIGHT_ENTRIES + sum(len(buffer.access_increments) + len(buffer.query_rows) for buffer in _ACCESS_TRACKING_BUFFERS.values()),
        _ACCESS_TRACKING_INFLIGHT_BYTES + sum(buffer.byte_charge for buffer in _ACCESS_TRACKING_BUFFERS.values()),
    )


def get_access_tracking_stats() -> dict[str, int]:
    """Report separately loss-tolerant, bounded access-statistics admission."""
    with _ACCESS_TRACKING_LOCK:
        count, size = _tracking_charge_locked()
        return {"pending_entries": count, "pending_bytes": size, "dropped_entries": _ACCESS_TRACKING_DROPPED_ENTRIES}

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
    *,
    inflight: tuple[int, int] = (0, 0),
    store_generation: Optional[str] = None,
    access_incarnations: Optional[dict[str, str]] = None,
) -> None:
    global _ACCESS_TRACKING_INFLIGHT_ENTRIES, _ACCESS_TRACKING_INFLIGHT_BYTES, _ACCESS_TRACKING_DROPPED_ENTRIES
    with _ACCESS_TRACKING_LOCK:
        buf = _ACCESS_TRACKING_BUFFERS.get(resolved)
        if buf is not None and store_generation is not None and buf.store_generation not in (None, store_generation):
            _ACCESS_TRACKING_DROPPED_ENTRIES += inflight[0]
            _ACCESS_TRACKING_INFLIGHT_ENTRIES -= inflight[0]
            _ACCESS_TRACKING_INFLIGHT_BYTES -= inflight[1]
            return
        if buf is None:
            buf = _AccessTrackingBuffer(db_path=resolved, namespace=namespace, store_generation=store_generation)
            _ACCESS_TRACKING_BUFFERS[resolved] = buf
        for mid, (delta, accessed_at) in inc_items:
            incarnation = (access_incarnations or {}).get(mid)
            prev = buf.access_increments.get(mid)
            if prev is not None and buf.access_incarnations.get(mid) != incarnation:
                # A newer pending event must survive an obsolete failed flush.
                _ACCESS_TRACKING_DROPPED_ENTRIES += 1
                continue
            if incarnation is not None:
                buf.access_incarnations[mid] = incarnation
            if prev is None:
                buf.access_increments[mid] = (delta, accessed_at)
                buf.byte_charge += _access_entry_bytes(mid, accessed_at)
            else:
                buf.access_increments[mid] = (prev[0] + delta, max(prev[1], accessed_at))
        if q_rows:
            buf.query_rows.extend(q_rows)
            buf.byte_charge += sum(_query_entry_bytes(row) for row in q_rows)
        buf.event_count += len(inc_items) + len(q_rows)
        if buf.event_count == len(inc_items) + len(q_rows):
            buf.first_event_ts = time.monotonic()
        _ACCESS_TRACKING_INFLIGHT_ENTRIES -= inflight[0]
        _ACCESS_TRACKING_INFLIGHT_BYTES -= inflight[1]


# Backward-compatibility alias
_requeue_telemetry = _requeue_access_tracking


def _flush_access_tracking_for_db_path(db_path: Path) -> None:
    global _ACCESS_TRACKING_INFLIGHT_ENTRIES, _ACCESS_TRACKING_INFLIGHT_BYTES, _ACCESS_TRACKING_DROPPED_ENTRIES
    resolved = db_path.resolve()
    with _ACCESS_TRACKING_LOCK:
        buf = _ACCESS_TRACKING_BUFFERS.pop(resolved, None)
        if buf is None or (not buf.access_increments and not buf.query_rows):
            return
        inc_items = list(buf.access_increments.items())
        incarnations = buf.access_incarnations
        q_rows = list(buf.query_rows)
        ns = buf.namespace
        generation = buf.store_generation
        inflight = (len(inc_items) + len(q_rows), buf.byte_charge)
        _ACCESS_TRACKING_INFLIGHT_ENTRIES += inflight[0]
        _ACCESS_TRACKING_INFLIGHT_BYTES += inflight[1]

    if not resolved.exists():
        if generation is not None:
            with _ACCESS_TRACKING_LOCK:
                _ACCESS_TRACKING_DROPPED_ENTRIES += inflight[0]
                _ACCESS_TRACKING_INFLIGHT_ENTRIES -= inflight[0]
                _ACCESS_TRACKING_INFLIGHT_BYTES -= inflight[1]
        else:
            _requeue_access_tracking(resolved, ns, inc_items, q_rows, inflight=inflight, access_incarnations=incarnations)
        return

    try:
        # Background or eviction flushes bind the buffer's real root; they must
        # never resolve another caller's current MESH_HOME.
        with mesh_dir_context(resolved.parent):
            store = MeshStore(namespace=ns, create=False)
            if generation is not None and generation != store.store_generation:
                raise StaleStoreError("Access statistics belong to a retired database generation.")
            with namespace_write_lock(ns, data_root=resolved.parent), store._cache_lock:
                with _CACHE_LOCK:
                    q_writes = _QUERY_WRITE_COUNTS.get(resolved, 0) + len(q_rows)
                with store._connect() as conn:
                    marker = _begin_write(conn, ns, store._store_generation)
                    try:
                        conn.executemany(
                            "UPDATE memories SET access_count=access_count+?,accessed_at=MAX(COALESCE(accessed_at,''),?) WHERE namespace=? AND id=? AND incarnation=?",
                            [(delta, accessed_at, ns, mid, incarnations[mid]) for mid, (delta, accessed_at) in inc_items if mid in incarnations],
                        )
                        if q_rows:
                            enc_on = is_encryption_enabled()
                            rows_to_insert = [
                                (qid, q_ns, encrypt_text_for_storage(text) if enc_on else text, agent, similarity, count, when, private)
                                for qid, q_ns, text, agent, similarity, count, when, private in q_rows
                            ]
                            conn.executemany(
                                "INSERT INTO queries(id,namespace,query_text,agent_id,best_similarity,result_count,timestamp,private_query) VALUES(?,?,?,?,?,?,?,?)",
                                rows_to_insert,
                            )
                            if q_writes >= 100:
                                q_writes = 0
                                _prune_queries_table_locked(conn, ns, _max_query_rows())
                        _commit_write(conn, marker)
                    except BaseException:
                        _rollback_write(conn, marker)
                        raise
                with _CACHE_LOCK:
                    _QUERY_WRITE_COUNTS[resolved] = q_writes
                store._refresh_cache_stamp_after_non_memory_write()
    except StaleStoreError:
        with _ACCESS_TRACKING_LOCK:
            _ACCESS_TRACKING_DROPPED_ENTRIES += inflight[0]
            _ACCESS_TRACKING_INFLIGHT_ENTRIES -= inflight[0]
            _ACCESS_TRACKING_INFLIGHT_BYTES -= inflight[1]
    except Exception:
        _requeue_access_tracking(resolved, ns, inc_items, q_rows, inflight=inflight, store_generation=generation, access_incarnations=incarnations)
    else:
        with _ACCESS_TRACKING_LOCK:
            _ACCESS_TRACKING_INFLIGHT_ENTRIES -= inflight[0]
            _ACCESS_TRACKING_INFLIGHT_BYTES -= inflight[1]


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


def _scrub_pending_query_tracking(namespace: str, content: Optional[str], *, data_root: Optional[Path] = None) -> None:
    """Drop buffered recall queries that reproduce content being deleted or redacted (Finding S12)."""
    clean = (content or "").strip()
    if not clean:
        return
    clean_low = clean.lower()
    trunc_low = clean[:512].lower()
    with _ACCESS_TRACKING_LOCK:
        for buf in _ACCESS_TRACKING_BUFFERS.values():
            if buf.namespace != namespace or not buf.query_rows or (data_root is not None and buf.db_path.parent != data_root):
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
                buf.byte_charge -= sum(_query_entry_bytes(row) for row in buf.query_rows) - sum(_query_entry_bytes(row) for row in kept)
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
    close_store_readers(db_path)
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
    close_store_readers()
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
        *,
        include_private: bool = True,
        expected_source_agent: Optional[str] = None,
    ) -> None:
        for values in (embeddings, documents, metadatas):
            if values is not None and len(values) != len(ids):
                raise ValueError("IDs, documents, metadata, and embeddings must have matching lengths.")
        items = [
            (
                memory_id,
                documents[index] if documents is not None else "",
                embeddings[index] if embeddings is not None else None,
                metadatas[index] if metadatas is not None else {},
                bool(metadatas[index].get("local_only")) if metadatas is not None else False,
            )
            for index, memory_id in enumerate(ids)
        ]
        self._store.upsert_batch(
            items, include_private=include_private, expected_source_agent=expected_source_agent,
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
        self._store_generation: Optional[str] = None
        self._db_identity: Optional[tuple[int, int]] = None
        ensure_secure_mesh_dir()
        base_dir = cached_resolved_mesh_dir()
        if any(base_dir.glob(".mesh-rename-*.json")):
            from .namespace_maintenance import recover_namespace_renames

            recover_namespace_renames(base_dir, self.namespace)
        raw_candidate = base_dir / f"mesh_{self.namespace}.db"
        reject_symlink(raw_candidate)
        resolved_db = raw_candidate.resolve()
        if resolved_db.parent != base_dir:
            raise ValueError(f"Invalid namespace path resolution for '{namespace}'.")
        self.db_path = resolved_db
        self._cache_lock = _get_store_coordinator(self.db_path, self.namespace)
        self._fts_enabled = True
        self._fts_dirty = False
        if not self.create and not self.db_path.exists():
            raise NamespaceNotFoundError(f"Namespace '{self.namespace}' does not exist.")
        if self.create and not self.db_path.exists():
            _check_namespace_creation_limit(base_dir, self.namespace)
        if self.db_path.exists() or self.create:
            self._ensure_initialized()
        self.collection = _CollectionCompatShim(self)
        from .mutation_delivery import register_mutation_store

        register_mutation_store(self)

    # --- Connection, Content Version & Schema Management ---

    @property
    def store_generation(self) -> str:
        """The durable database identity captured when this instance was opened."""
        if self._store_generation is None:
            raise RuntimeError("Store identity has not been initialized.")
        return self._store_generation

    def _assert_instance_file(self) -> Optional[tuple[int, int]]:
        identity = _file_identity(self.db_path)
        if self._store_generation is not None and identity != self._db_identity:
            raise StaleStoreError("The namespace database was deleted or replaced; open a new store.")
        return identity

    def _is_current_instance(self) -> bool:
        """Check a registry candidate without reopening an old store as a writer."""
        try:
            self._assert_instance_file()
            with self._read_connect():
                pass
        except StaleStoreError:
            return False
        return True

    def _bind_store_identity(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value,(SELECT value FROM mesh_meta WHERE key='namespace_rename_pending') FROM mesh_meta WHERE key='store_generation'").fetchone()
        if row is None:
            raise RuntimeError("Database identity is missing; the schema upgrade did not complete.")
        if row[1] is not None:
            raise StaleStoreError("Namespace rename is pending; reopen the store to recover maintenance.")
        generation = _validate_incarnation(row[0], "store_generation")
        identity = _file_identity(self.db_path)
        if identity is None:
            raise StaleStoreError("The namespace database was deleted while opening it.")
        if self._store_generation is not None and (
            generation != self._store_generation or identity != self._db_identity
        ):
            raise StaleStoreError("The namespace database was replaced; open a new store.")
        self._store_generation = generation
        self._db_identity = identity

    def _connect(self, *, wait_timeout: float = 5.0) -> _ConnectionCheckout:
        """Lease a writer; disk opens and initialization never hold the global registry lock."""
        if type(wait_timeout) not in (int, float) or not 0.0 <= wait_timeout <= 5.0:
            raise ValueError("Writer admission wait must be a finite number between 0 and 5 seconds.")
        reject_symlink(self.db_path)
        identity = self._assert_instance_file()
        exists = identity is not None
        if not self.create and not exists:
            raise NamespaceNotFoundError(f"Namespace '{self.namespace}' does not exist.")
        with _CACHE_LOCK:
            conn = _CONNECTION_POOL.get(self.db_path)
            if conn is not None and exists and conn._mesh_db_identity == identity and not conn._mesh_closed and not conn._mesh_retired:
                _CONNECTION_POOL.move_to_end(self.db_path)
                _DB_PATH_NAMESPACES[self.db_path] = self.namespace
                conn.checkout()
                return _ConnectionCheckout(conn)
        evicted: list[tuple[Path, Optional[_PooledConnection], Optional[str]]] = []
        deadline = time.monotonic() + wait_timeout
        with self._cache_lock:
            identity = self._assert_instance_file()
            with _WRITER_CONDITION:
                conn = _CONNECTION_POOL.get(self.db_path)
                if conn is not None and identity is not None and conn._mesh_db_identity == identity and not conn._mesh_closed and not conn._mesh_retired:
                    _CONNECTION_POOL.move_to_end(self.db_path)
                    conn.checkout()
                    return _ConnectionCheckout(conn)
                if conn is not None:
                    _CONNECTION_POOL.pop(self.db_path, None)
                    _DB_PATH_NAMESPACES.pop(self.db_path, None)
                    evicted.append((self.db_path, conn, None))
                maximum = _get_max_pool_size()
                while len(_CONNECTION_POOL) + len(_WRITER_OPENING) >= maximum:
                    victim_path = next((path for path, candidate in _CONNECTION_POOL.items() if candidate._mesh_users == 0), None)
                    if victim_path is None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0.0:
                            if wait_timeout == 0.0:
                                raise TimeoutError("Writer connection capacity exceeded; background work deferred.")
                            raise RuntimeError("Writer connection capacity exceeded; retry after an active operation finishes.")
                        # Condition.wait releases the global registry lock,
                        # allowing active leases and cold opens to finish.
                        _WRITER_CONDITION.wait(timeout=remaining)
                        continue
                    victim = _CONNECTION_POOL.pop(victim_path)
                    namespace = _DB_PATH_NAMESPACES.pop(victim_path, None)
                    _QUERY_WRITE_COUNTS.pop(victim_path, None)
                    dict.pop(_INITIALIZED_DBS, victim_path, None)
                    _evict_vector_cache_for_db_path(victim_path)
                    evicted.append((victim_path, victim, namespace))
                _WRITER_OPENING.add(self.db_path)
            # Idle handles are retired before opening a replacement, so the
            # physical connection count respects admission during cold starts.
            for _, retired, _ in evicted:
                if retired is not None:
                    retired.retire()
            conn = None
            try:
                if self._assert_instance_file() is None:
                    _check_namespace_creation_limit(self.db_path.parent, self.namespace)
                ensure_secure_mesh_dir(self.db_path.parent)
                existing_only = self._store_generation is not None or not self.create
                conn = sqlite3.connect(
                    self.db_path.as_uri() + "?mode=rw" if existing_only else self.db_path,
                    uri=existing_only, timeout=10.0, check_same_thread=False,
                    isolation_level=None, factory=_PooledConnection,
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                if sys.platform == "darwin":
                    conn.execute("PRAGMA fullfsync=ON")
                    conn.execute("PRAGMA checkpoint_fullfsync=ON")
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA secure_delete=ON")
                if os.name != "nt":
                    conn.execute("PRAGMA mmap_size=67108864")
                cache_kib = min(65536, env_int("MESH_SQLITE_CACHE_KIB", 2048, minimum=256))
                conn.execute(f"PRAGMA cache_size=-{cache_kib}")
                conn._mesh_db_path = self.db_path
                conn._mesh_db_identity = self._assert_instance_file()
                _check_store_generation(conn, self._store_generation)
                secure_file_permissions(self.db_path)
                with _CACHE_LOCK:
                    _CONNECTION_POOL[self.db_path] = conn
                    _CONNECTION_POOL.move_to_end(self.db_path)
                    _DB_PATH_NAMESPACES[self.db_path] = self.namespace
                    conn.checkout()
            except BaseException:
                if conn is not None:
                    conn.retire()
                raise
            finally:
                with _WRITER_CONDITION:
                    _WRITER_OPENING.discard(self.db_path)
                    _WRITER_CONDITION.notify_all()
        if evicted:
            _finalize_evicted_resources(evicted)
        return _ConnectionCheckout(conn)

    def _connect_reader(self) -> _ConnectionCheckout:
        """Lease bounded readonly connections; transaction owners read their own writes."""
        global _READER_NEXT_ID
        identity = self._assert_instance_file()
        with _CACHE_LOCK:
            writer = _CONNECTION_POOL.get(self.db_path)
            owns_writer = writer is not None and writer._mesh_owner_thread == threading.get_ident()
        if owns_writer:
            return self._connect()
        if identity is None:
            raise NamespaceNotFoundError(f"Namespace '{self.namespace}' does not exist.")
        reject_symlink(self.db_path)
        stale_readers = []
        with _READER_CONDITION:
            for key, candidate in list(_READER_CONNECTION_POOL.items()):
                if key[0] == self.db_path and candidate._mesh_db_identity != identity and candidate._mesh_users == 0:
                    stale_readers.append(_READER_CONNECTION_POOL.pop(key))
            if stale_readers:
                _READER_CONDITION.notify_all()
        for candidate in stale_readers:
            candidate.retire()
        per_db = env_int("MESH_READERS_PER_NAMESPACE", 2, minimum=1, maximum=8)
        maximum = env_int("MESH_MAX_READER_CONNECTIONS", 32, minimum=1, maximum=256)
        deadline = time.monotonic() + 5.0
        retired: Optional[_PooledConnection] = None
        with _READER_CONDITION:
            while True:
                existing = [key for key in _READER_CONNECTION_POOL if key[0] == self.db_path]
                for key in existing:
                    conn = _READER_CONNECTION_POOL[key]
                    if conn._mesh_db_identity == identity and conn._mesh_users == 0 and not conn._mesh_closed:
                        conn.checkout()
                        _READER_CONNECTION_POOL.move_to_end(key)
                        return _ReaderCheckout(conn)
                opening = _READER_OPENING.get(self.db_path, 0)
                total = len(_READER_CONNECTION_POOL) + sum(_READER_OPENING.values())
                if len(existing) + opening < per_db:
                    if total >= maximum:
                        for key, conn in _READER_CONNECTION_POOL.items():
                            if conn._mesh_users == 0:
                                retired = conn
                                _READER_CONNECTION_POOL.pop(key)
                                total -= 1
                                break
                    if total < maximum:
                        _READER_OPENING[self.db_path] = opening + 1
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Reader connection capacity exceeded; retry after an active read finishes.")
                _READER_CONDITION.wait(timeout=remaining)
        if retired is not None:
            retired.retire()
        conn = None
        try:
            conn = sqlite3.connect(
                self.db_path.as_uri() + "?mode=ro", uri=True, timeout=3.0,
                check_same_thread=False, isolation_level=None, factory=_PooledConnection,
            )
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA busy_timeout=3000")
            cache_kib = env_int("MESH_SQLITE_READER_CACHE_KIB", 512, minimum=256, maximum=4096)
            conn.execute(f"PRAGMA cache_size=-{cache_kib}")
            conn._mesh_db_path = self.db_path
            conn._mesh_db_identity = self._assert_instance_file()
            _check_store_generation(conn, self._store_generation)
            with _READER_CONDITION:
                _READER_NEXT_ID += 1
                _READER_CONNECTION_POOL[(self.db_path, _READER_NEXT_ID)] = conn
                conn.checkout()
            return _ReaderCheckout(conn)
        except BaseException:
            if conn is not None:
                conn.retire()
            raise
        finally:
            with _READER_CONDITION:
                count = _READER_OPENING[self.db_path] - 1
                if count:
                    _READER_OPENING[self.db_path] = count
                else:
                    _READER_OPENING.pop(self.db_path)
                _READER_CONDITION.notify_all()

    @contextlib.contextmanager
    def _read_connect(self) -> Iterator[sqlite3.Connection]:
        """Lease a reader and maintain one snapshot, preserving an owning write transaction."""
        with self._connect_reader() as conn:
            conn.row_factory = sqlite3.Row
            owns_read = not conn.in_transaction
            if owns_read:
                conn.execute("BEGIN")
            try:
                _check_connection_file_identity(conn)
                _check_store_generation(conn, self._store_generation)
                yield conn
            finally:
                if owns_read:
                    conn.rollback()

    @contextlib.contextmanager
    def write_transaction(self) -> Iterator["MeshStore"]:
        """Commit nested guarded writes together, publishing cache state only after commit."""
        self._ensure_initialized()
        actions: list[Callable[[], Any]] = []
        with namespace_write_lock(self.namespace, data_root=self.db_path.parent), self._cache_lock, self._connect() as conn:
            marker = _begin_write(conn, self.namespace, self._store_generation)
            if marker is None:
                conn._mesh_cache_dirty = False
                conn._mesh_cache_group_active = True
                with _CACHE_LOCK:
                    candidate = _VECTOR_CACHE.get((self.db_path, self.namespace))
                if candidate is not None:
                    try:
                        with candidate.read_snapshot():
                            base_version = candidate.content_version
                        if base_version == self._read_content_version_locked(conn):
                            conn._mesh_cache_base = candidate
                            conn._mesh_cache_base_version = base_version
                    except CacheSnapshotInvalidatedError:
                        pass
            try:
                yield self
                actions = _commit_write(conn, marker)
            except BaseException:
                _rollback_write(conn, marker)
                self._invalidate_cache()
                raise
            finally:
                if marker is None:
                    try:
                        if not conn.in_transaction:
                            self._publish_pending_cache(conn)
                    except Exception as exc:
                        logger.warning("Cache publication deferred (%s).", type(exc).__name__)
                        self._invalidate_cache()
                    finally:
                        conn._mesh_cache_updates.clear()
                        conn._mesh_cache_update_bytes = 0
                        conn._mesh_cache_base = None
                        conn._mesh_cache_base_version = None
                        conn._mesh_cache_target_version = None
                        conn._mesh_cache_group_active = False
                        conn._mesh_cache_dirty = False
                        if conn._mesh_cache_delta_lease is not None:
                            conn._mesh_cache_delta_lease.release()
                            conn._mesh_cache_delta_lease = None
                self._refresh_cache_stamp_after_non_memory_write()
        self._run_after_commit_actions(actions)

    def _collects_cache_deltas(self) -> bool:
        with _CACHE_LOCK:
            conn = _CONNECTION_POOL.get(self.db_path)
        return bool(conn is not None and conn._mesh_owner_thread == threading.get_ident() and conn._mesh_cache_group_active and conn._mesh_cache_base is not None and not conn._mesh_cache_dirty)

    def _queue_cache_delta(
        self, operation: str, memory_id: str, vector: Optional[np.ndarray],
        metadata: Optional[dict[str, Any]], version: Optional[int], *, preserve_vector: bool = False,
    ) -> None:
        with _CACHE_LOCK:
            conn = _CONNECTION_POOL[self.db_path]
        if not conn._mesh_cache_group_active or conn._mesh_cache_base is None or conn._mesh_cache_dirty:
            conn._mesh_cache_dirty = True
            return
        charge = 256 + (_object_graph_bytes(metadata) if metadata is not None else len(memory_id) * 4)
        charge += int(vector.nbytes) if vector is not None else 0
        maximum = env_int("MESH_MAX_CACHE_DELTA_BYTES", 8 * 1024 * 1024, minimum=1024)
        try:
            if len(conn._mesh_cache_updates) >= env_int("MESH_MAX_BULK_ITEMS", 1000, minimum=1) or conn._mesh_cache_update_bytes + charge > maximum:
                raise CacheBudgetExceededError("Transaction cache deltas exceed their bounded allocation allowance.")
            if conn._mesh_cache_delta_lease is None:
                conn._mesh_cache_delta_lease = _CacheBudgetLease((self.db_path, self.namespace))
            conn._mesh_cache_delta_lease.resize(conn._mesh_cache_update_bytes + charge)
        except CacheBudgetExceededError:
            conn._mesh_cache_dirty = True
            conn._mesh_cache_updates.clear()
            conn._mesh_cache_update_bytes = 0
            if conn._mesh_cache_delta_lease is not None:
                conn._mesh_cache_delta_lease.release()
                conn._mesh_cache_delta_lease = None
            return
        conn._mesh_cache_updates.append((operation, memory_id, vector, metadata, preserve_vector))
        conn._mesh_cache_update_bytes += charge
        conn._mesh_cache_target_version = version

    def _publish_pending_cache(self, conn: _PooledConnection) -> None:
        if conn._mesh_cache_dirty:
            self._invalidate_cache()
            return
        if not conn._mesh_cache_updates:
            return
        cached = conn._mesh_cache_base
        version = conn._mesh_cache_target_version
        with _CACHE_LOCK:
            current = _VECTOR_CACHE.get((self.db_path, self.namespace))
        if cached is None or current is not cached or self._read_content_version_locked(conn) != version:
            self._invalidate_cache()
            return
        stamp = _get_db_stamp(self.db_path)
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        publish_ok = stamp is not None
        with cached.write_snapshot():
            if not cached._valid or cached.content_version != conn._mesh_cache_base_version:
                publish_ok = False
            if publish_ok:
                for operation, memory_id, vector, metadata, preserve_vector in conn._mesh_cache_updates:
                    if operation == "delete":
                        cached.delete_vector(memory_id)
                    else:
                        if preserve_vector and vector is None:
                            index = cached.id_to_idx.get(memory_id)
                            if index is not None and cached._matrix_buf is not None:
                                vector = cached._matrix_buf[index]
                        if not cached.upsert_vector(memory_id, vector, row_meta=metadata):
                            publish_ok = False
                            break
                if publish_ok:
                    cached.update_stamp(stamp, data_version=data_version, content_version=version)
        if not publish_ok:
            self._invalidate_cache()
        else:
            with _CACHE_LOCK:
                _INITIALIZED_DBS[self.db_path] = stamp

    @staticmethod
    def _run_after_commit_actions(actions: list[Callable[[], Any]]) -> None:
        for callback in actions:
            try:
                callback()
            except Exception as exc:
                logger.warning("Post-commit action failed (%s).", type(exc).__name__)

    def after_commit(self, callback: Callable[[], Any]) -> None:
        """Defer a wakeup until the enclosing write commits; discard it on rollback."""
        if not callable(callback):
            raise ValueError("after_commit requires a callable.")
        captured = contextvars.copy_context()
        action = lambda: captured.run(callback)
        with self._connect() as conn:
            if conn.in_transaction:
                conn._mesh_after_commit_actions.append(action)
                return
        self._run_after_commit_actions([action])

    atomic_write = write_transaction

    @staticmethod
    def _check_mutation_permission(
        local_only: bool,
        owner: Optional[str],
        include_private: bool,
        expected_source_agent: Optional[str],
    ) -> None:
        if (local_only and not include_private) or (
            expected_source_agent is not None and owner != expected_source_agent
        ):
            raise ValueError("Requested object is unavailable or mutation is not permitted.")

    @staticmethod
    def _mutation_result(row: dict[str, Any], include_private: bool) -> dict[str, Any]:
        if include_private or not row.get("local_only"):
            return row
        visible_fields = {
            "id", "short_id", "namespace", "kind", "status", "local_only",
            "revision", "embedding_model", "updated_at", "created_at", "completed_at",
            "incarnation", "store_generation",
        }
        return {key: value for key, value in row.items() if key in visible_fields}

    @staticmethod
    def _receipt_result(value: Any, *, _depth: int = 0, _remaining: Optional[list[int]] = None) -> Any:
        if _remaining is None:
            _remaining = [16 * env_int("MESH_MAX_BULK_ITEMS", 1000, minimum=1)]
        _remaining[0] -= 1
        if _depth > 8 or _remaining[0] < 0:
            raise ValueError("Operation result exceeds the bounded receipt nesting limit.")
        if isinstance(value, list):
            if len(value) > env_int("MESH_MAX_BULK_ITEMS", 1000, minimum=1):
                raise ValueError("Operation result exceeds the bounded bulk limit.")
            return [MeshStore._receipt_result(item, _depth=_depth + 1, _remaining=_remaining) for item in value]
        if not isinstance(value, dict):
            return {"status": "ok"}
        fields = {
            "memory_id", "item_id", "id", "short_id", "revision", "status", "disposition",
            "incarnation", "store_generation",
            "deduplicated", "local_only", "embedding_model", "indexing_state", "forgotten",
            "count", "imported", "imported_items", "imported_events", "skipped_items", "errors",
        }
        result = {key: item for key, item in value.items() if key in fields and isinstance(item, (str, int, float, bool, type(None)))}
        for key in ("memory_id", "item_id", "id", "short_id"):
            if key in result and (not isinstance(result[key], str) or not _VALID_MEMORY_ID_RE.fullmatch(result[key])):
                result.pop(key)
        for key in ("incarnation", "store_generation"):
            if key in result and (not isinstance(result[key], str) or not re.fullmatch(r"[a-f0-9]{32}", result[key])):
                result.pop(key)
        for key in ("status", "disposition", "indexing_state"):
            if key in result and (not isinstance(result[key], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", result[key])):
                result.pop(key)
        if "embedding_model" in result and (not isinstance(result["embedding_model"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", result["embedding_model"])):
            result.pop("embedding_model")
        for key in ("deduplicated", "local_only", "forgotten"):
            if key in result and type(result[key]) is not bool:
                result.pop(key)
        if "revision" in result and (type(result["revision"]) is not int or result["revision"] < 1):
            result.pop("revision")
        for key in ("count", "imported", "imported_items", "imported_events", "skipped_items", "errors"):
            if key in result and (type(result[key]) is not int or result[key] < 0):
                result.pop(key)
        if isinstance(value.get("results"), list):
            result["results"] = MeshStore._receipt_result(value["results"], _depth=_depth + 1, _remaining=_remaining)
        return result

    def execute_operation(
        self,
        operation_id: str,
        producer_scope: str,
        operation: str,
        parameter_digest: str,
        callback: Callable[[], Any],
    ) -> dict[str, Any]:
        operation_id = validate_memory_id(operation_id)
        if not isinstance(producer_scope, str) or not producer_scope.strip() or len(producer_scope) > 128 or any(ord(c) < 32 for c in producer_scope):
            raise ValueError("producer_scope must be a nonempty identifier of at most 128 characters.")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", operation or ""):
            raise ValueError("Invalid operation identifier.")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", parameter_digest or ""):
            raise ValueError("parameter_digest must be a SHA-256 digest.")
        with self.write_transaction():
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT operation, parameter_digest, result_json, created_at FROM operation_receipts WHERE namespace=? AND producer_scope=? AND operation_id=?",
                    (self.namespace, producer_scope, operation_id),
                ).fetchone()
                if row is not None:
                    if row[0] != operation or row[1] != parameter_digest:
                        raise ValueError("Operation ID was already used with a different request.")
                    result = json.loads(row[2])
                    return {"replayed": True, "result": result, "receipt": {"operation_id": operation_id, "created_at": row[3], "result": result}}
                maximum = env_int("MESH_MAX_OPERATION_RECEIPTS", 100000, minimum=1)
                count = int(conn.execute("SELECT value FROM mesh_meta WHERE key='operation_receipts_count'").fetchone()[0])
                if count >= maximum:
                    raise RuntimeError("Operation receipt capacity exceeded; no mutation was admitted.")
                result = callback()
                receipt_result = self._receipt_result(result)
                receipt_json = json.dumps(receipt_result, allow_nan=False)
                if len(receipt_json.encode("utf-8")) > env_int("MESH_MAX_OPERATION_RECEIPT_BYTES", 1024 * 1024, minimum=1024):
                    raise ValueError("Operation result exceeds the receipt byte limit; no mutation was committed.")
                now = _utc_now_iso()
                conn.execute(
                    "INSERT INTO operation_receipts(namespace,producer_scope,operation_id,operation,parameter_digest,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (self.namespace, producer_scope, operation_id, operation, parameter_digest, receipt_json, now),
                )
                return {"replayed": False, "result": result, "receipt": {"operation_id": operation_id, "created_at": now, "result": receipt_result}}

    def get_operation_receipt(self, operation_id: str, producer_scope: str) -> Optional[dict[str, Any]]:
        """Return the content-free receipt in this store and producer scope."""
        operation_id = validate_memory_id(operation_id)
        if not isinstance(producer_scope, str) or not producer_scope.strip() or len(producer_scope) > 128 or any(ord(c) < 32 for c in producer_scope):
            raise ValueError("Invalid producer scope.")
        with self._read_connect() as conn:
            row = conn.execute(
                "SELECT operation,result_json,created_at FROM operation_receipts WHERE namespace=? AND producer_scope=? AND operation_id=?",
                (self.namespace, producer_scope, operation_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "operation_id": operation_id, "operation": row[0],
            "created_at": row[2], "result": json.loads(row[1]),
        }

    def _record_mutation_in_tx(self, conn: sqlite3.Connection, operation: str, object_id: Optional[str] = None, revision: Optional[int] = None) -> None:
        maximum = env_int("MESH_MAX_MUTATION_OUTBOX", 100000, minimum=1)
        count = int(conn.execute("SELECT value FROM mesh_meta WHERE key='mutation_outbox_count'").fetchone()[0])
        if count >= maximum:
            raise RuntimeError("Mutation event capacity exceeded; no mutation was committed.")
        conn.execute(
            "INSERT INTO mutation_outbox(event_id,namespace,operation,object_id,revision,created_at) VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, self.namespace, operation, object_id, revision, _utc_now_iso()),
        )
        from .mutation_delivery import register_mutation_store

        self.after_commit(lambda: register_mutation_store(self))

    def _insert_conflict_in_tx(self, conn: sqlite3.Connection, memory_id: str, content: str, local_only: bool, conflict: dict[str, Any]) -> None:
        existing_id = validate_memory_id(conflict.get("existing_memory_id", ""))
        row = conn.execute("SELECT content,local_only,revision,incarnation FROM memories WHERE namespace=? AND id=?", (self.namespace, existing_id)).fetchone()
        expected = conflict.get("existing_revision")
        expected_incarnation = conflict.get("existing_incarnation")
        if expected_incarnation is not None:
            _validate_incarnation(expected_incarnation, "existing_incarnation")
        if row is None or (expected is not None and int(row[2]) != int(expected)) or (expected_incarnation is not None and row[3] != expected_incarnation):
            raise ValueError("Conflict candidate changed; no memory was committed.")
        score = float(conflict.get("similarity", 0.0))
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError("Conflict similarity must be finite and between zero and one.")
        cid = validate_memory_id(conflict.get("contradiction_id") or str(uuid.uuid4()))
        new_text = REDACTED_CONTENT_PLACEHOLDER if local_only else redact_secrets_local(content)
        old_text = REDACTED_CONTENT_PLACEHOLDER if row[1] else redact_secrets_local(decrypt_text_if_encrypted(row[0]))
        conn.execute(
            "INSERT INTO contradictions(id,namespace,new_memory_id,new_content,existing_memory_id,existing_content,similarity,detected_at,resolved) VALUES(?,?,?,?,?,?,?,?,0)",
            (cid, self.namespace, memory_id, self._encrypt_work_text(new_text), existing_id, self._encrypt_work_text(old_text), score, _utc_now_iso()),
        )
        self._record_mutation_in_tx(conn, "contradiction", cid)

    def list_mutation_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._read_connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM mutation_outbox WHERE namespace=? AND delivered_at IS NULL ORDER BY created_at,event_id LIMIT ?", (self.namespace, max(1, min(1000, int(limit))))).fetchall()
            return [dict(row) for row in rows]

    def acknowledge_mutation_events(self, event_ids: list[str]) -> None:
        ids = [validate_memory_id(item) for item in event_ids]
        with self.write_transaction(), self._connect() as conn:
            conn.executemany("DELETE FROM mutation_outbox WHERE namespace=? AND event_id=?", [(self.namespace, item) for item in ids])

    @staticmethod
    def _validate_model_id(model: str) -> str:
        if not isinstance(model, str) or not model.strip() or len(model) > 256 or any(ord(c) < 32 for c in model):
            raise ValueError("Embedding model must be a nonempty identifier of at most 256 characters.")
        return model.strip()

    def _schedule_embedding_in_tx(
        self, conn: sqlite3.Connection, memory_id: str, revision: int, embedding_model: str,
        target_model: str = "onnx-minilm-l6-v2", *, incarnation: str,
        target_representation: str = "", preserve_job_target: bool = False,
    ) -> None:
        target_representation = validate_representation(target_representation)
        previous = conn.execute("SELECT target_model,target_representation FROM embedding_jobs WHERE namespace=? AND memory_id=? AND incarnation=? AND revision=?", (self.namespace, memory_id, incarnation, revision - 1)).fetchone() if preserve_job_target else None
        if previous is not None:
            target_model, target_representation = tuple(previous)
        if embedding_model not in ("hash-384", "needs-reembed", "unknown"):
            if not passage_index_enabled() and previous is None:
                return
            parent = conn.execute("SELECT passage_format_version FROM memories WHERE namespace=? AND id=?", (self.namespace, memory_id)).fetchone()
            if previous is None and parent is not None and parent[0] == PASSAGE_FORMAT_VERSION:
                return
            target_model, target_representation = tuple(previous) if previous is not None else (embedding_model, PASSAGE_FORMAT_VERSION)
        target_model = self._validate_model_id(target_model)
        maximum = env_int("MESH_MAX_PENDING_EMBEDDING_JOBS", 100000, minimum=1)
        count = int(conn.execute("SELECT value FROM mesh_meta WHERE key='embedding_jobs_count'").fetchone()[0])
        if count >= maximum and conn.execute(
            "SELECT 1 FROM embedding_jobs WHERE namespace=? AND memory_id=?", (self.namespace, memory_id),
        ).fetchone() is None:
            raise RuntimeError("Pending embedding job capacity exceeded; no mutation was committed.")
        now = _utc_now_iso()
        conn.execute(
            """INSERT INTO embedding_jobs(memory_id,namespace,revision,incarnation,target_model,target_representation,attempts,available_at,created_at,error_code)
            VALUES(?,?,?,?,?,?,0,?,?,NULL) ON CONFLICT(memory_id) DO UPDATE SET revision=excluded.revision,incarnation=excluded.incarnation,target_model=excluded.target_model,target_representation=excluded.target_representation,
            attempts=0,available_at=excluded.available_at,created_at=excluded.created_at,error_code=NULL""",
            (memory_id, self.namespace, revision, incarnation, target_model, target_representation, now, now),
        )

    @staticmethod
    def _validate_expected_identity(expected_revision: int, expected_incarnation: str, expected_store_generation: str) -> None:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer.")
        _validate_incarnation(expected_incarnation, "expected_incarnation")
        _validate_incarnation(expected_store_generation, "expected_store_generation")

    def enqueue_embedding_job(
        self, memory_id: str, *, expected_revision: int, expected_incarnation: str,
        expected_store_generation: str, target_model: str = "onnx-minilm-l6-v2",
        target_representation: str = "",
    ) -> bool:
        memory_id = validate_memory_id(memory_id)
        self._validate_expected_identity(expected_revision, expected_incarnation, expected_store_generation)
        target_model = self._validate_model_id(target_model)
        target_representation = validate_representation(target_representation)
        if expected_store_generation != self.store_generation:
            return False
        with self.write_transaction(), self._connect() as conn:
            row = conn.execute("SELECT revision,embedding_model,incarnation FROM memories WHERE namespace=? AND id=?", (self.namespace, memory_id)).fetchone()
            if row is None or int(row[0]) != expected_revision or row[2] != expected_incarnation:
                return False
            job = conn.execute(
                "SELECT revision,target_model,incarnation,target_representation FROM embedding_jobs WHERE namespace=? AND memory_id=?",
                (self.namespace, memory_id),
            ).fetchone()
            if job is not None and int(job[0]) == expected_revision and job[1] == target_model and job[2] == expected_incarnation and job[3] == target_representation:
                return True
            self._schedule_embedding_in_tx(conn, memory_id, int(row[0]), "needs-reembed", target_model, incarnation=row[2], target_representation=target_representation)
            return True

    def backfill_embedding_jobs(self, limit: int = 32, *, target_model: str = "onnx-minilm-l6-v2", target_representation: str = PASSAGE_FORMAT_VERSION) -> int:
        """Discover missing representations in bounded, capacity-aware durable batches."""
        target_model = self._validate_model_id(target_model)
        target_representation = validate_representation(target_representation)
        if target_representation and not passage_index_enabled():
            return 0
        if target_model in ("hash-384", "unknown", "needs-reembed"):
            return 0
        limit = max(1, min(128, int(limit)))
        with self.write_transaction(), self._connect() as conn:
            capacity = env_int("MESH_MAX_PENDING_EMBEDDING_JOBS", 100000, minimum=1) - int(conn.execute("SELECT value FROM mesh_meta WHERE key='embedding_jobs_count'").fetchone()[0])
            if capacity <= 0:
                return 0
            rows = conn.execute(
                "SELECT id,revision,incarnation FROM memories WHERE namespace=? "
                "AND (embedding_model IN ('hash-384','unknown','needs-reembed') "
                "OR (embedding_model=? AND (embedding IS NULL OR (?!='' AND passage_format_version!=?)))) "
                "AND NOT EXISTS(SELECT 1 FROM embedding_jobs WHERE memory_id=memories.id) "
                "ORDER BY passage_format_version,id LIMIT ?",
                (self.namespace, target_model, target_representation, PASSAGE_FORMAT_VERSION, min(limit, capacity)),
            ).fetchall()
            for memory_id, revision, incarnation in rows:
                self._schedule_embedding_in_tx(conn, memory_id, int(revision), "needs-reembed", target_model, incarnation=incarnation, target_representation=target_representation)
            return len(rows)

    def list_embedding_jobs(self, limit: int = 32) -> list[dict[str, Any]]:
        with self._read_connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT jobs.*,(SELECT value FROM mesh_meta WHERE key='store_generation') AS store_generation "
                "FROM embedding_jobs jobs WHERE jobs.namespace=? AND jobs.available_at<=? "
                "AND EXISTS(SELECT 1 FROM memories WHERE id=jobs.memory_id AND namespace=jobs.namespace "
                "AND revision=jobs.revision AND incarnation=jobs.incarnation) "
                "ORDER BY jobs.available_at,jobs.memory_id LIMIT ?",
                (self.namespace, _utc_now_iso(), max(1, min(1000, int(limit)))),
            ).fetchall()
            return [dict(row) for row in rows]

    def embedding_job_stats(self) -> dict[str, Any]:
        with self._read_connect() as conn:
            row = conn.execute("SELECT COUNT(*),MIN(created_at) FROM embedding_jobs WHERE namespace=?", (self.namespace,)).fetchone()
            return {"pending_count": int(row[0]), "oldest_at": row[1]}

    def retry_embedding_job(
        self, memory_id: str, *, expected_revision: int, expected_incarnation: str,
        expected_store_generation: str, expected_target_model: str,
        expected_target_representation: str = "",
        error_code: str = "embedding_failed", delay_seconds: float = 1.0,
    ) -> bool:
        memory_id = validate_memory_id(memory_id)
        self._validate_expected_identity(expected_revision, expected_incarnation, expected_store_generation)
        expected_target_model = self._validate_model_id(expected_target_model)
        expected_target_representation = validate_representation(expected_target_representation)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code or ""):
            raise ValueError("error_code must be a content-free error identifier.")
        if not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError("delay_seconds must be finite and nonnegative.")
        available = (_utc_now() + timedelta(seconds=min(delay_seconds, 3600.0))).isoformat()
        if expected_store_generation != self.store_generation:
            return False
        with self.write_transaction(), self._connect() as conn:
            cursor = conn.execute(
                "UPDATE embedding_jobs SET attempts=attempts+1,available_at=MAX(available_at,?),error_code=? "
                "WHERE namespace=? AND memory_id=? AND revision=? AND incarnation=? AND target_model=? AND target_representation=? "
                "AND EXISTS(SELECT 1 FROM memories WHERE id=memory_id AND namespace=embedding_jobs.namespace "
                "AND revision=embedding_jobs.revision AND incarnation=embedding_jobs.incarnation)",
                (available, error_code, self.namespace, memory_id, expected_revision, expected_incarnation, expected_target_model, expected_target_representation),
            )
            return cursor.rowcount > 0

    def publish_embedding(
        self, memory_id: str, embedding: list[float], *, expected_revision: int,
        expected_incarnation: str, expected_store_generation: str,
        embedding_model: str, expected_model: Optional[str] = None,
        target_representation: str = "", passage_spans: Optional[Sequence[tuple[int, int]]] = None,
        passage_embeddings: Optional[np.ndarray] = None,
        document_pending_passages: bool = False,
    ) -> bool:
        memory_id = validate_memory_id(memory_id)
        self._validate_expected_identity(expected_revision, expected_incarnation, expected_store_generation)
        model = self._validate_model_id(embedding_model)
        target_representation = validate_representation(target_representation)
        if type(document_pending_passages) is not bool or (document_pending_passages and not target_representation):
            raise ValueError("Partial document publication requires an explicit passage target.")
        if document_pending_passages and (passage_spans is not None or passage_embeddings is not None):
            raise ValueError("Partial document publication cannot include passage data.")
        if target_representation and model in ("hash-384", "unknown", "needs-reembed"):
            raise ValueError("Passage publication requires a known local model space.")
        if expected_model is not None:
            expected_model = self._validate_model_id(expected_model)
        vector = _normalize_vector(embedding, enforce_dim=True)
        derived = None
        if passage_embeddings is not None:
            derived = np.asarray(passage_embeddings, dtype=np.float32)
            if passage_spans is None or derived.shape != (len(passage_spans), EMBEDDING_DIM) or not np.isfinite(derived).all():
                raise ValueError("Passage embeddings do not match their complete source spans.")
        if target_representation == "" and (passage_spans is not None or derived is not None):
            raise ValueError("Document-only publication cannot supply passage rows.")
        if expected_store_generation != self.store_generation:
            return False
        with self.write_transaction(), self._connect() as conn:
            row = conn.execute("SELECT revision,embedding_model,incarnation,content,passage_count FROM memories WHERE namespace=? AND id=?", (self.namespace, memory_id)).fetchone()
            if row is None or int(row[0]) != expected_revision or row[2] != expected_incarnation or (expected_model is not None and row[1] != expected_model):
                return False
            job = conn.execute("SELECT revision,target_model,incarnation,target_representation FROM embedding_jobs WHERE namespace=? AND memory_id=?", (self.namespace, memory_id)).fetchone()
            if job is None or int(job[0]) != expected_revision or job[1] != model or job[2] != expected_incarnation or job[3] != target_representation:
                return False
            count = 0
            if target_representation and not document_pending_passages:
                content = decrypt_text_if_encrypted(row[3])
                if passage_spans is None:
                    raise ValueError("Passage publication requires a complete source plan.")
                validate_spans(content, passage_spans)
                if derived is None and list(passage_spans) != [(0, len(content))]:
                    raise ValueError("Only a single whole-source passage may reuse the primary vector.")
                count = len(passage_spans) if derived is not None else 0
            # Validate the full bundle before changing any row. Encryption is
            # resolved inside the publication transaction, after slow inference.
            blob = _vector_to_blob(vector)
            conn.execute("DELETE FROM memory_passages WHERE namespace=? AND parent_id=?", (self.namespace, memory_id))
            if derived is not None:
                for ordinal, ((start, end), passage_vector) in enumerate(zip(passage_spans, derived)):
                    conn.execute(
                        "INSERT INTO memory_passages(parent_id,namespace,parent_revision,parent_incarnation,ordinal,start,end,embedding_model,representation_version,embedding) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (memory_id, self.namespace, expected_revision, expected_incarnation, ordinal, start, end, model, target_representation, _vector_to_blob(passage_vector)),
                    )
            published_format = "" if document_pending_passages else target_representation
            conn.execute("UPDATE memories SET embedding=?,embedding_model=?,passage_format_version=?,passage_count=? WHERE namespace=? AND id=? AND revision=? AND incarnation=?", (blob, model, published_format, count, self.namespace, memory_id, expected_revision, expected_incarnation))
            if not document_pending_passages:
                conn.execute("DELETE FROM embedding_jobs WHERE namespace=? AND memory_id=? AND revision=? AND incarnation=? AND target_model=? AND target_representation=?", (self.namespace, memory_id, expected_revision, expected_incarnation, model, target_representation))
            version = self._bump_content_version_in_tx(conn)
            if count or row[4]:
                self._invalidate_cache()
            else:
                self._queue_cache_delta("upsert", memory_id, vector, self.get(memory_id), version)
            return True

    def _get_data_version_locked(self) -> int:
        with self._connect() as conn:
            row = conn.execute("PRAGMA data_version").fetchone()
            return int(row[0]) if row else 0

    def _read_content_version_locked(self, conn: Optional[sqlite3.Connection] = None) -> Optional[int]:
        if conn is None:
            with self._connect() as checked_out:
                return self._read_content_version_locked(checked_out)
        try:
            row = conn.execute("SELECT value FROM mesh_meta WHERE key='content_version'").fetchone()
            return int(row[0]) if row else None
        except (sqlite3.OperationalError, TypeError, ValueError):
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
        if self._owns_write_transaction():
            return None
        key = (self.db_path, self.namespace)
        with _CACHE_LOCK:
            cached = _VECTOR_CACHE.get(key)
        if cached is None:
            return None
        stamp = _get_db_stamp(self.db_path)
        if stamp is None:
            return None
        try:
            with cached.read_snapshot():
                if cached.passage_index_dirty or cached.passage_index_enabled != passage_index_enabled():
                    return None
                old_stamp = cached.stamp
                old_cv = cached.content_version
                cached_count = len(cached.all_ids)
        except CacheSnapshotInvalidatedError:
            return None
        with self._read_connect() as conn:
            cv = self._read_content_version_locked(conn)
            if cv is None or old_cv is None or cv != old_cv:
                return None
            if stamp != old_stamp:
                count_row = conn.execute("SELECT COUNT(*) FROM memories WHERE namespace=?", (self.namespace,)).fetchone()
                if int(count_row[0]) != cached_count:
                    return None
            dv = int(conn.execute("PRAGMA data_version").fetchone()[0])
        if stamp != old_stamp:
            cached.update_stamp(stamp, data_version=dv, content_version=cv)
        with _CACHE_LOCK:
            if _VECTOR_CACHE.get(key) is not cached:
                return None
            _VECTOR_CACHE.move_to_end(key)
            if self.db_path in _CONNECTION_POOL:
                _CONNECTION_POOL.move_to_end(self.db_path)
            _INITIALIZED_DBS[self.db_path] = stamp
        return cached

    def _refresh_stamps_after_write_locked(
        self,
        synced_cache: Optional[_VectorCacheEntry],
        content_version: Optional[int] = None,
    ) -> None:
        secure_file_permissions(self.db_path)
        stamp = _get_db_stamp(self.db_path)
        if stamp is None:
            with _CACHE_LOCK:
                _INITIALIZED_DBS.pop(self.db_path, None)
                _evict_vector_cache_for_db_path(self.db_path)
            return
        with _CACHE_LOCK:
            _INITIALIZED_DBS[self.db_path] = stamp
        if synced_cache is not None:
            dv = self._get_data_version_locked()
            cv = content_version if content_version is not None else self._read_content_version_locked()
            synced_cache.update_stamp(stamp, data_version=dv, content_version=cv)

    def _refresh_cache_stamp_after_non_memory_write(self) -> None:
        stamp = _get_db_stamp(self.db_path)
        if stamp is None:
            return
        with self._cache_lock:
            cv = self._read_content_version_locked()
            dv = self._get_data_version_locked()
            with _CACHE_LOCK:
                _INITIALIZED_DBS[self.db_path] = stamp
                cached = _VECTOR_CACHE.get((self.db_path, self.namespace))
            if cached is not None:
                with cached.write_snapshot():
                    if cached.content_version == cv:
                        cached.update_stamp(stamp, data_version=dv, content_version=cv)

    def _ensure_initialized(self) -> None:
        self._assert_instance_file()
        enc_on = is_encryption_enabled()
        stamp = _get_db_stamp(self.db_path)
        with _CACHE_LOCK:
            known = _INITIALIZED_DBS.get(self.db_path)
            if self.__dict__.get("_namespace_verified") and self.__dict__.get("_encryption_verified") == enc_on and stamp is not None and stamp[1] > 0 and known == stamp and not self._fts_dirty:
                return
        # A cold encrypted open must perform legacy data and FTS cleanup.
        if stamp is not None and stamp[1] > 0 and not self._fts_dirty and (not enc_on or self.__dict__.get("_encryption_verified") is True):
            schema_current = False
            with self._read_connect() as conn:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if version > int(SCHEMA_VERSION):
                    raise RuntimeError("Database schema is newer than supported; upgrade Mesh before opening it.")
                if version == int(SCHEMA_VERSION):
                    row = conn.execute("SELECT value FROM mesh_meta WHERE key='schema_version'").fetchone()
                    cleanup = conn.execute("SELECT value FROM mesh_meta WHERE key='work_encryption_cleanup_pending'").fetchone()
                    owner = conn.execute("SELECT value FROM mesh_meta WHERE key='namespace_owner'").fetchone()
                    if owner is not None and owner[0] != self.namespace:
                        raise ValueError("Namespace database belongs to another namespace spelling; use the original spelling or a distinct namespace.")
                    if row is not None and row[0] == SCHEMA_VERSION and owner is not None and not (cleanup and cleanup[0] == "1"):
                        conn.execute("SELECT revision,incarnation,passage_format_version,passage_count FROM memories LIMIT 0").close()
                        conn.execute("SELECT incarnation,target_representation FROM embedding_jobs LIMIT 0").close()
                        conn.execute("SELECT parent_id,embedding FROM memory_passages LIMIT 0").close()
                        self._bind_store_identity(conn)
                        schema_current = True
            if schema_current:
                current_stamp = _get_db_stamp(self.db_path)
                with _CACHE_LOCK:
                    if current_stamp is not None:
                        _INITIALIZED_DBS[self.db_path] = current_stamp
                self._namespace_verified = True
                self._encryption_verified = enc_on
                return
        with namespace_write_lock(self.namespace, data_root=self.db_path.parent):
            self._ensure_initialized_locked()
        self._namespace_verified = True
        self._encryption_verified = enc_on

    def _ensure_initialized_locked(self) -> None:
        if not self.create and not self.db_path.exists():
            raise NamespaceNotFoundError(f"Namespace '{self.namespace}' does not exist.")
        with self._cache_lock:
            stamp = _get_db_stamp(self.db_path)
            with _CACHE_LOCK:
                if (
                    stamp is not None
                    and stamp[1] > 0
                    and _INITIALIZED_DBS.get(self.db_path) == stamp
                    and self.__dict__.get("_namespace_verified")
                    and self.__dict__.get("_encryption_verified") == is_encryption_enabled()
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
                with _CACHE_LOCK:
                    _INITIALIZED_DBS[self.db_path] = new_stamp

    def _prepare_schema_upgrade(self, conn: sqlite3.Connection) -> None:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        current = str(user_version or 1)
        if "mesh_meta" in tables:
            row = conn.execute("SELECT value FROM mesh_meta WHERE key='schema_version'").fetchone()
            if row:
                current = str(row[0])
        elif "schema_info" in tables:
            row = conn.execute("SELECT version FROM schema_info").fetchone()
            if row:
                current = str(row[0])
        try:
            schema_version = int(current)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Database schema version is invalid; migration was not started.") from exc
        if user_version > int(SCHEMA_VERSION) or schema_version > int(SCHEMA_VERSION):
            raise RuntimeError(
                f"Database schema {max(user_version, schema_version)} is newer than supported version {SCHEMA_VERSION}; upgrade Mesh before opening it."
            )
        if current == SCHEMA_VERSION and user_version == int(SCHEMA_VERSION):
            return
        populated = any(
            table in tables and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
            for table in ("memories", "work_items", "work_events")
        )
        if not populated:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = self.db_path.with_name(f"{self.db_path.name}.backup-v0.7.2-{stamp}-{uuid.uuid4().hex[:8]}")
        reject_symlink(backup)
        destination: Optional[sqlite3.Connection] = None
        try:
            destination = sqlite3.connect(backup)
            with destination:
                destination.execute("PRAGMA synchronous=FULL")
                destination.execute("PRAGMA secure_delete=ON")
                conn.backup(destination)
                if is_encryption_enabled():
                    self._encrypt_work_tables_in_tx(destination)
                    self._encrypt_passages_in_tx(destination)
                    destination.commit()
                    destination.execute("VACUUM")
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Schema backup integrity check failed.")
            secure_file_permissions(backup)
        except Exception as exc:
            raise RuntimeError("Could not verify the pre-migration backup; schema upgrade was not started.") from exc
        finally:
            if destination is not None:
                destination.close()

    def _init_db(self) -> None:
        with namespace_write_lock(self.namespace, data_root=self.db_path.parent):
            self._init_db_locked()

    def _init_db_locked(self) -> None:
        mem_count = 0
        public_unencrypted_mem_count = 0
        evicted: list[tuple[Path, Optional[_PooledConnection], Optional[str]]] = []
        enc_on = is_encryption_enabled()
        encryption_cleanup_pending = False
        with self._cache_lock:
            with self._connect() as conn, _schema_transaction(conn, self):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mesh_meta (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                _check_namespace_binding(conn, self.namespace, claim=True)
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
                generation = conn.execute("SELECT value FROM mesh_meta WHERE key='store_generation'").fetchone()
                if generation is None:
                    if int(cur_ver) >= 5:
                        raise RuntimeError("Database identity is missing; repair or restore this database before opening it.")
                    conn.execute("INSERT INTO mesh_meta(key,value) VALUES('store_generation',?)", (uuid.uuid4().hex,))
                else:
                    _validate_incarnation(generation[0], "store_generation")
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
                        secret_ref      TEXT,
                        revision        INTEGER NOT NULL DEFAULT 1,
                        incarnation     TEXT NOT NULL,
                        passage_format_version TEXT NOT NULL DEFAULT '',
                        passage_count INTEGER NOT NULL DEFAULT 0 CHECK(passage_count >= 0)
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
                    ("revision", "ALTER TABLE memories ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"),
                    ("incarnation", "ALTER TABLE memories ADD COLUMN incarnation TEXT NOT NULL DEFAULT ''"),
                    ("passage_format_version", "ALTER TABLE memories ADD COLUMN passage_format_version TEXT NOT NULL DEFAULT ''"),
                    ("passage_count", "ALTER TABLE memories ADD COLUMN passage_count INTEGER NOT NULL DEFAULT 0 CHECK(passage_count >= 0)"),
                ):
                    if col_name not in existing_cols:
                        if col_name == "incarnation" and int(cur_ver) >= 5:
                            raise RuntimeError("Memory identities are missing from a current-schema database.")
                        try:
                            conn.execute(col_sql)
                        except sqlite3.OperationalError:
                            if col_name in ("incarnation", "passage_format_version", "passage_count"):
                                raise
                            pass
                if int(cur_ver) < 5:
                    conn.execute("UPDATE memories SET incarnation=lower(hex(randomblob(16))) WHERE incarnation IS NULL OR incarnation=''")
                if conn.execute("SELECT 1 FROM memories WHERE length(incarnation)!=32 OR incarnation GLOB '*[^a-f0-9]*' LIMIT 1").fetchone():
                    raise RuntimeError("Memory identity is malformed; repair or restore this database before opening it.")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_incarnation ON memories(incarnation)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_passage_readiness ON memories(namespace,passage_format_version,id)")
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS memory_passages (
                        parent_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                        namespace TEXT NOT NULL,
                        parent_revision INTEGER NOT NULL CHECK(parent_revision > 0),
                        parent_incarnation TEXT NOT NULL,
                        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                        start INTEGER NOT NULL CHECK(start >= 0),
                        end INTEGER NOT NULL CHECK(end > start),
                        embedding_model TEXT NOT NULL,
                        representation_version TEXT NOT NULL,
                        embedding BLOB NOT NULL,
                        PRIMARY KEY(parent_id,ordinal)
                    )"""
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_passages_scan ON memory_passages(namespace,embedding_model,parent_id,ordinal)")

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
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_id_nocase ON memories(namespace,id COLLATE NOCASE)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memories (namespace, memory_type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_brief ON memories(namespace,memory_type,quarantined,local_only,updated_at DESC,id)")
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
                conn.execute("CREATE INDEX IF NOT EXISTS idx_work_items_id_nocase ON work_items(namespace,id COLLATE NOCASE)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_work_items_short_nocase ON work_items(namespace,short_id COLLATE NOCASE)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_work_events_item ON work_events (item_id, timestamp DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_work_events_ns_ts ON work_events (namespace, timestamp DESC)")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS embedding_jobs (
                        memory_id TEXT PRIMARY KEY,
                        namespace TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK(revision > 0),
                        incarnation TEXT NOT NULL,
                        target_model TEXT NOT NULL,
                        target_representation TEXT NOT NULL DEFAULT '',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        available_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        error_code TEXT
                    )
                    """
                )
                job_columns = {row[1] for row in conn.execute("PRAGMA table_info(embedding_jobs)")}
                if "incarnation" not in job_columns:
                    if int(cur_ver) >= 5:
                        raise RuntimeError("Embedding job identities are missing from a current-schema database.")
                    conn.execute("ALTER TABLE embedding_jobs ADD COLUMN incarnation TEXT NOT NULL DEFAULT ''")
                if "target_representation" not in job_columns:
                    conn.execute("ALTER TABLE embedding_jobs ADD COLUMN target_representation TEXT NOT NULL DEFAULT ''")
                if int(cur_ver) < 5:
                    conn.execute(
                        "UPDATE embedding_jobs SET incarnation=COALESCE((SELECT incarnation FROM memories "
                        "WHERE memories.id=embedding_jobs.memory_id AND memories.namespace=embedding_jobs.namespace "
                        "AND memories.revision=embedding_jobs.revision),'') WHERE incarnation=''"
                    )
                    conn.execute("DELETE FROM embedding_jobs WHERE incarnation='' OR NOT EXISTS(SELECT 1 FROM memories WHERE memories.id=embedding_jobs.memory_id AND memories.namespace=embedding_jobs.namespace AND memories.revision=embedding_jobs.revision AND memories.incarnation=embedding_jobs.incarnation)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_embedding_jobs_ready ON embedding_jobs(namespace, available_at)")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mutation_outbox (
                        event_id TEXT PRIMARY KEY,
                        namespace TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        object_id TEXT,
                        revision INTEGER,
                        created_at TEXT NOT NULL,
                        delivered_at TEXT
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_mutation_outbox_pending ON mutation_outbox(namespace, delivered_at, created_at)")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS operation_receipts (
                        namespace TEXT NOT NULL,
                        producer_scope TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        parameter_digest TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(namespace, producer_scope, operation_id)
                    )
                    """
                )
                for table, key in (
                    ("embedding_jobs", "embedding_jobs_count"),
                    ("mutation_outbox", "mutation_outbox_count"),
                    ("operation_receipts", "operation_receipts_count"),
                ):
                    conn.execute(f"INSERT OR IGNORE INTO mesh_meta(key,value) SELECT ?,CAST(COUNT(*) AS TEXT) FROM {table}", (key,))
                    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_count_insert AFTER INSERT ON {table} BEGIN UPDATE mesh_meta SET value=CAST(value AS INTEGER)+1 WHERE key='{key}'; END")
                    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_count_delete AFTER DELETE ON {table} BEGIN UPDATE mesh_meta SET value=MAX(0,CAST(value AS INTEGER)-1) WHERE key='{key}'; END")
                now_jobs = _utc_now_iso()
                conn.execute(
                    "INSERT OR IGNORE INTO embedding_jobs(memory_id,namespace,revision,incarnation,target_model,available_at,created_at) "
                    "SELECT id,namespace,revision,incarnation,'onnx-minilm-l6-v2',?,? FROM memories "
                    "WHERE embedding_model IN ('hash-384','unknown','needs-reembed') "
                    "AND NOT EXISTS(SELECT 1 FROM embedding_jobs WHERE memory_id=memories.id) ORDER BY id LIMIT ?",
                    (now_jobs, now_jobs, max(0, env_int("MESH_MAX_PENDING_EMBEDDING_JOBS", 100000, minimum=1) - int(conn.execute("SELECT value FROM mesh_meta WHERE key='embedding_jobs_count'").fetchone()[0]))),
                )
                if enc_on:
                    work_changed = self._encrypt_work_tables_in_tx(conn)
                    if self._encrypt_passages_in_tx(conn) or work_changed:
                        conn.execute("INSERT OR REPLACE INTO mesh_meta(key,value) VALUES('work_encryption_cleanup_pending','1')")
                cleanup_row = conn.execute("SELECT value FROM mesh_meta WHERE key='work_encryption_cleanup_pending'").fetchone()
                encryption_cleanup_pending = bool(cleanup_row and cleanup_row[0] == "1")

                mem_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE namespace = ?", (self.namespace,)
                    ).fetchone()[0]
                )
                public_unencrypted_mem_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE COALESCE(local_only, 0) = 0 AND COALESCE(quarantined, 0) = 0 AND content NOT LIKE 'enc:v1:%'"
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
                            "(SELECT rowid FROM memories WHERE COALESCE(local_only, 0) != 0 OR COALESCE(quarantined, 0) != 0 OR content LIKE 'enc:v1:%')"
                        )
                        fts_count = int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0])
                        misaligned = conn.execute(
                            """
                            SELECT 1 FROM memories m
                            LEFT JOIN memories_fts f ON f.rowid = m.rowid
                            WHERE COALESCE(m.local_only, 0) = 0
                              AND COALESCE(m.quarantined, 0) = 0
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
                                WHERE COALESCE(local_only, 0) = 0 AND COALESCE(quarantined, 0) = 0 AND content NOT LIKE 'enc:v1:%'
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

            secure_file_permissions(self.db_path)
            with self._connect() as identity_connection:
                self._bind_store_identity(identity_connection)

            if encryption_cleanup_pending:
                self._finish_work_encryption_cleanup()

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
                if cand_key != active_key:
                    victim_key = cand_key
                    break
            if victim_key is None:
                break
            _VECTOR_CACHE.pop(victim_key, None)
            v_path, v_ns = victim_key
            still_used = any(k[0] == v_path for k in _VECTOR_CACHE)
            v_conn: Optional[_PooledConnection] = None
            if not still_used and v_path != self.db_path:
                candidate = _CONNECTION_POOL.get(v_path)
                # Nested mutation groups must retain their current writer.
                if candidate is None or candidate._mesh_users == 0:
                    v_conn = _CONNECTION_POOL.pop(v_path, None)
                    _DB_PATH_NAMESPACES.pop(v_path, None)
                    _QUERY_WRITE_COUNTS.pop(v_path, None)
                    dict.pop(_INITIALIZED_DBS, v_path, None)
            evicted.append((v_path, v_conn, v_ns))
        return evicted

    def _migrate_from_chroma_if_needed(self) -> None:
        chroma_dir = self.db_path.parent / "chroma"
        if not chroma_dir.exists():
            return
        with self._cache_lock:
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
                        tx_marker = _begin_write(conn, self.namespace, self._store_generation)
                        for mid, emb in zip(got_ids, got_embs):
                            if emb is not None and len(emb) == EMBEDDING_DIM:
                                conn.execute(
                                    "UPDATE memories SET embedding = ?, embedding_model = 'onnx-minilm-l6-v2',passage_format_version='',passage_count=0 WHERE id = ?",
                                    (_vector_to_blob(emb), mid),
                                )
                                conn.execute("DELETE FROM memory_passages WHERE parent_id=?", (mid,))
                        self._bump_content_version_in_tx(conn)
                        _commit_write(conn, tx_marker)
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
        quarantined: bool = False,
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
            if local_only or quarantined or is_encryption_enabled() or is_encrypted_text(content):
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

    def _owns_write_transaction(self) -> bool:
        with _CACHE_LOCK:
            writer = _CONNECTION_POOL.get(self.db_path)
        return bool(writer is not None and writer._mesh_owner_thread == threading.get_ident() and writer.in_transaction)

    def _invalidate_cache(self) -> None:
        if self._owns_write_transaction():
            with _CACHE_LOCK:
                _CONNECTION_POOL[self.db_path]._mesh_cache_dirty = True
            return
        with self._cache_lock:
            with _CACHE_LOCK:
                cached = _VECTOR_CACHE.pop((self.db_path, self.namespace), None)
                stamp = _get_db_stamp(self.db_path)
                if stamp is not None:
                    _INITIALIZED_DBS[self.db_path] = stamp
                else:
                    _INITIALIZED_DBS.pop(self.db_path, None)
            if cached is not None:
                with cached.write_snapshot():
                    cached._valid = False

    def _get_vector_cache(self, force_refresh: bool = False) -> _VectorCacheEntry:
        """Return a metered generation, or request the bounded streaming path."""
        self._ensure_initialized()
        if self._owns_write_transaction():
            raise CacheBudgetExceededError("Use bounded streaming retrieval inside an owning write transaction.")
        if not force_refresh:
            synced = self._is_cache_synced_locked()
            if synced is not None:
                return synced
        key = (self.db_path, self.namespace)
        evicted: list[tuple[Path, Optional[_PooledConnection], Optional[str]]] = []
        with self._cache_lock:
            if not force_refresh:
                synced = self._is_cache_synced_locked()
                if synced is not None:
                    return synced
            with _CACHE_LOCK:
                stale = _VECTOR_CACHE.pop(key, None)
            del stale
            stamp = _get_db_stamp(self.db_path) or (0, 0, 0, 0)
            lease: Optional[_CacheBudgetLease] = None
            entry: Optional[_VectorCacheEntry] = None
            cv: Optional[int] = None
            try:
                with self._read_connect() as conn:
                    dv = int(conn.execute("PRAGMA data_version").fetchone()[0])
                    cv = self._read_content_version_locked(conn)
                    with _CACHE_LOCK:
                        rejected = _CACHE_REJECTIONS.get(key)
                    limit = _get_max_vector_cache_bytes()
                    if rejected is not None and rejected[0] == cv and rejected[1] > limit:
                        raise CacheBudgetExceededError(
                            "Namespace version exceeds the cache allocation budget; use bounded streaming retrieval.",
                            required_bytes=rejected[1],
                        )
                    length_expr = "+".join(f"COALESCE(length({name}),0)" for name in _CACHE_TEXT_FIELDS)
                    summary = conn.execute(
                        "SELECT COUNT(*),COALESCE(SUM(embedding IS NOT NULL),0),"
                        f"COALESCE(SUM({length_expr}),0),COALESCE(MAX({length_expr}),0) "
                        "FROM memories WHERE namespace=?",
                        (self.namespace,),
                    ).fetchone()
                    count, vector_count, text_chars, max_chars = map(int, summary)
                    passage_count = int(conn.execute("SELECT COUNT(*) FROM memory_passages WHERE namespace=?", (self.namespace,)).fetchone()[0]) if passage_index_enabled() else 0
                    matrix_bytes = vector_count * EMBEDDING_DIM * 4
                    minimum = 4096 + matrix_bytes + count * 2048 + text_chars * 4 + passage_count * (EMBEDDING_DIM * 4 + 16 + 512)
                    if minimum > limit:
                        raise CacheBudgetExceededError(
                            "Namespace exceeds the cache allocation budget; use bounded streaming retrieval.",
                            required_bytes=minimum,
                        )
                    peak = 16384 + max_chars * 256
                    lease = _CacheBudgetLease(key)
                    lease.resize(8192 + matrix_bytes + peak)
                    matrix = np.empty((vector_count, EMBEDDING_DIM), dtype=np.float32) if vector_count else None
                    entry = _VectorCacheEntry(
                        stamp=stamp, ids=[], matrix=matrix, all_ids=set(), rows_by_id={},
                        data_version=dv, content_version=cv, needs_healing_ids=set(),
                        owned_matrix=True, budget_lease=lease,
                    )
                    del matrix
                    entry._build_peak_bytes = peak
                    # Keep a single SQL row and vector live. No full fetchall,
                    # list of vectors, or second matrix is allocated on rebuild.
                    cursor = conn.execute(
                        f"SELECT {_MEMORY_COLUMNS},embedding FROM memories WHERE namespace=?",
                        (self.namespace,),
                    )
                    for row in cursor:
                        raw = dict(row)
                        blob = raw.pop("embedding")
                        meta = self._deserialize_memory_row(raw)
                        vector = _blob_to_vector(blob) if blob else None
                        model = str(meta.get("embedding_model") or "unknown")
                        if vector is not None and vector.size == EMBEDDING_DIM:
                            if model == "unknown":
                                model = "hash-384" if np.all(vector[368:] == 0.0) and int(np.count_nonzero(vector == 0.0)) >= 16 else "onnx-minilm-l6-v2"
                        else:
                            vector = None
                            model = "needs-reembed"
                        meta["embedding_model"] = model
                        if not entry._upsert_vector(meta["id"], vector, row_meta=meta):
                            raise RuntimeError("Unable to construct a valid namespace vector cache.")
                    cursor.close()
                    if passage_count:
                        from .passage_search import load_passage_cache

                        entry.passages = load_passage_cache(self, conn, entry, passage_count)
                        entry._cache_bytes += entry.passages.allocated_bytes
                    entry._build_peak_bytes = 0
                    lease.resize(entry._cache_bytes)
                with _CACHE_LOCK:
                    _CACHE_REJECTIONS.pop(key, None)
                    _VECTOR_CACHE[key] = entry
                    _VECTOR_CACHE.move_to_end(key)
                    _INITIALIZED_DBS[self.db_path] = stamp
                    evicted = self._trim_vector_cache_locked()
            except CacheBudgetExceededError as exc:
                if exc.required_bytes > _get_max_vector_cache_bytes():
                    with _CACHE_LOCK:
                        _CACHE_REJECTIONS[key] = (cv, exc.required_bytes)
                        _CACHE_REJECTIONS.move_to_end(key)
                        while len(_CACHE_REJECTIONS) > _get_max_namespaces():
                            _CACHE_REJECTIONS.popitem(last=False)
                # Failed generations were never published. Drop allocator
                # traceback references before the caller admits streaming work.
                exc.__traceback__ = None
                entry = None
                if lease is not None:
                    lease.release()
                raise
            except BaseException:
                if lease is not None and entry is None:
                    lease.release()
                raise
        if evicted:
            _finalize_evicted_resources(evicted)
        return entry

    # --- Core Memory CRUD Operations ---

    def upsert(
        self,
        memory_id: str,
        content: str,
        embedding: Optional[list[float]],
        metadata: dict[str, Any],
        local_only: bool = False,
        embedding_model: Optional[str] = None,
        allow_redacted_public: bool = False,
        conflict: Optional[dict[str, Any]] = None,
        *,
        include_private: bool = True,
        expected_source_agent: Optional[str] = None,
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
        norm_vec = _normalize_vector(embedding, enforce_dim=True) if embedding is not None else None
        raw_blob = norm_vec.tobytes() if norm_vec is not None else None

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
        if expected_source_agent is not None and source_agent != expected_source_agent:
            raise ValueError("Memory mutation is not permitted for this agent.")
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
            or ("needs-reembed" if norm_vec is None else "hash-384" if np.all(norm_vec[368:] == 0.0) else "onnx-minilm-l6-v2")
        )
        trust_tier = _normalize_trust_tier(metadata.get("trust_tier"), default="user")
        quarantined_flag = 1 if metadata.get("quarantined") else 0
        norm_ref = normalize_secret_ref(metadata.get("secret_ref"), default_namespace=self.namespace)
        secret_ref_json = json.dumps(norm_ref, sort_keys=True) if norm_ref else None

        enc_on = is_encryption_enabled()
        db_content = encrypt_text_for_storage(content) if enc_on else content
        db_tags = encrypt_text_for_storage(tags_str) if enc_on else tags_str
        db_blob = encrypt_blob_for_storage(raw_blob) if enc_on and raw_blob is not None else raw_blob
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
            "passage_format_version": "",
            "passage_count": 0,
            "trust_tier": trust_tier,
            "quarantined": bool(quarantined_flag),
            "secret_ref": norm_ref,
        }

        after_commit_actions: list[Callable[[], Any]] = []
        with namespace_write_lock(self.namespace, data_root=self.db_path.parent):
            with self._cache_lock:
                synced_cache = self._is_cache_synced_locked()
                new_cv: Optional[int] = None
                audit_scrub_old_content: Optional[str] = None
                with self._connect() as conn:
                    tx_marker = _begin_write(conn, self.namespace, self._store_generation)
                    try:
                        old_row = conn.execute(
                            "SELECT content, local_only, revision, source_agent, namespace, incarnation FROM memories WHERE id = ?",
                            (memory_id,),
                        ).fetchone()
                        if old_row is not None and old_row[4] != self.namespace:
                            raise ValueError("Requested object is unavailable or mutation is not permitted.")
                        if old_row is not None:
                            self._check_mutation_permission(
                                bool(old_row[1]), old_row[3], include_private, expected_source_agent
                            )
                        revision = int(old_row[2]) + 1 if old_row is not None else 1
                        incarnation = old_row[5] if old_row is not None else uuid.uuid4().hex
                        row_meta["revision"] = revision
                        row_meta["incarnation"] = incarnation
                        row_meta["store_generation"] = self.store_generation
                        old_rowid = self._get_memory_rowid(conn, memory_id)
                        conn.execute("DELETE FROM memory_passages WHERE namespace=? AND parent_id=?", (self.namespace, memory_id))
                        conn.execute("DELETE FROM embedding_jobs WHERE namespace=? AND memory_id=?", (self.namespace, memory_id))
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO memories
                            (id, namespace, content, confidence, memory_type,
                             source_agent, tags, created_at, updated_at, accessed_at, access_count,
                             ttl_days, decay_start_at, local_only, embedding, embedding_model,
                             trust_tier, quarantined, secret_ref, revision, incarnation)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                revision,
                                incarnation,
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
                            quarantined=bool(quarantined_flag),
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
                                _scrub_pending_query_telemetry(self.namespace, old_content, data_root=self.db_path.parent)
                                audit_scrub_old_content = old_content
                        self._schedule_embedding_in_tx(conn, memory_id, revision, emb_model, incarnation=incarnation)
                        self._record_mutation_in_tx(conn, "upsert", memory_id, revision)
                        if conflict is not None:
                            self._insert_conflict_in_tx(conn, memory_id, content, bool(local_flag), conflict)
                        new_cv = self._bump_content_version_in_tx(conn)
                        after_commit_actions = _commit_write(conn, tx_marker)
                    except Exception:
                        if conn.in_transaction:
                            _rollback_write(conn, tx_marker)
                        raise

                if self._owns_write_transaction():
                    self._queue_cache_delta("upsert", memory_id, norm_vec, row_meta, new_cv)
                elif synced_cache is not None:
                    with synced_cache.write_snapshot():
                        if synced_cache.upsert_vector(memory_id, norm_vec, row_meta=row_meta):
                            self._refresh_stamps_after_write_locked(synced_cache, content_version=new_cv)
                        else:
                            self._invalidate_cache()
                else:
                    self._invalidate_cache()

        self._run_after_commit_actions(after_commit_actions)
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
        items: list[tuple[str, str, Optional[list[float]], dict[str, Any], bool]],
        *,
        include_private: bool = True,
        expected_source_agent: Optional[str] = None,
    ) -> None:
        """
        Batch-insert or overwrite multiple memories in a single SQLite WAL transaction,
        enforcing `guard_ingest` & `guard_tags` and encrypting at rest when enabled.
        """
        if not items:
            return
        if len(items) > env_int("MESH_MAX_BULK_ITEMS", 1000, minimum=1):
            raise ValueError("Bulk item limit exceeded; split the request into bounded batches.")
        self._ensure_initialized()
        now = _utc_now_iso()
        enc_on = is_encryption_enabled()

        after_commit_actions: list[Callable[[], Any]] = []
        with namespace_write_lock(self.namespace, data_root=self.db_path.parent):
            with self._cache_lock:
                synced_cache = self._is_cache_synced_locked()
                in_place_ok = synced_cache is not None
                collect_cache_meta = in_place_ok or self._collects_cache_deltas()
                pending_vectors: list[tuple[str, Optional[np.ndarray], dict[str, Any]]] = []
                new_cv: Optional[int] = None
                audit_scrubs: list[tuple[str, str, bool]] = []
                with self._connect() as conn:
                    tx_marker = _begin_write(conn, self.namespace, self._store_generation)
                    try:
                        for raw_memory_id, raw_content, embedding, metadata, local_only in items:
                            memory_id = validate_memory_id(raw_memory_id)
                            norm_vec = _normalize_vector(embedding, enforce_dim=True) if embedding is not None else None
                            raw_blob = norm_vec.tobytes() if norm_vec is not None else None

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
                            if expected_source_agent is not None and source_agent != expected_source_agent:
                                raise ValueError("Memory mutation is not permitted for this agent.")
                            confidence = float(metadata.get("confidence", 1.0))
                            created_at = metadata.get("created_at") or now
                            updated_at = metadata.get("updated_at") or created_at
                            accessed_at = metadata.get("accessed_at") or now
                            access_count = max(0, int(metadata.get("access_count", 0)))
                            ttl_days = metadata.get("ttl_days")
                            decay_start_at = metadata.get("decay_start_at")
                            emb_model = str(
                                metadata.get("embedding_model")
                                or ("needs-reembed" if norm_vec is None else "hash-384" if np.all(norm_vec[368:] == 0.0) else "onnx-minilm-l6-v2")
                            )
                            trust_tier = _normalize_trust_tier(metadata.get("trust_tier"), default="user")
                            quarantined_flag = 1 if metadata.get("quarantined") else 0
                            norm_ref = normalize_secret_ref(
                                metadata.get("secret_ref"), default_namespace=self.namespace
                            )
                            secret_ref_json = json.dumps(norm_ref, sort_keys=True) if norm_ref else None

                            db_content = encrypt_text_for_storage(content) if enc_on else content
                            db_tags = encrypt_text_for_storage(tags_str) if enc_on else tags_str
                            db_blob = encrypt_blob_for_storage(raw_blob) if enc_on and raw_blob is not None else raw_blob
                            db_ref = (
                                encrypt_text_for_storage(secret_ref_json)
                                if (enc_on and secret_ref_json)
                                else secret_ref_json
                            )

                            old_row = conn.execute(
                                "SELECT content, local_only, revision, source_agent, namespace, incarnation FROM memories WHERE id = ?",
                                (memory_id,),
                            ).fetchone()
                            if old_row is not None and old_row[4] != self.namespace:
                                raise ValueError("Requested object is unavailable or mutation is not permitted.")
                            if old_row is not None:
                                self._check_mutation_permission(
                                    bool(old_row[1]), old_row[3], include_private, expected_source_agent,
                                )
                            revision = int(old_row[2]) + 1 if old_row is not None else 1
                            incarnation = old_row[5] if old_row is not None else uuid.uuid4().hex
                            old_rowid = self._get_memory_rowid(conn, memory_id)
                            conn.execute("DELETE FROM memory_passages WHERE namespace=? AND parent_id=?", (self.namespace, memory_id))
                            conn.execute("DELETE FROM embedding_jobs WHERE namespace=? AND memory_id=?", (self.namespace, memory_id))
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO memories
                                (id, namespace, content, confidence, memory_type,
                                 source_agent, tags, created_at, updated_at, accessed_at, access_count,
                                 ttl_days, decay_start_at, local_only, embedding, embedding_model,
                                 trust_tier, quarantined, secret_ref, revision, incarnation)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                    revision,
                                    incarnation,
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
                                quarantined=bool(quarantined_flag),
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
                                    _scrub_pending_query_telemetry(self.namespace, old_content, data_root=self.db_path.parent)
                                    audit_scrubs.append((memory_id, old_content, bool(local_flag)))
                            self._schedule_embedding_in_tx(conn, memory_id, revision, emb_model, incarnation=incarnation)
                            self._record_mutation_in_tx(conn, "upsert", memory_id, revision)
                            if collect_cache_meta:
                                row_meta = {
                                    "id": memory_id,
                                    "revision": revision,
                                    "incarnation": incarnation,
                                    "store_generation": self.store_generation,
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
                                    "passage_format_version": "",
                                    "passage_count": 0,
                                    "trust_tier": trust_tier,
                                    "quarantined": bool(quarantined_flag),
                                    "secret_ref": norm_ref,
                                }
                                pending_vectors.append((memory_id, norm_vec, row_meta))
                        new_cv = self._bump_content_version_in_tx(conn)
                        after_commit_actions = _commit_write(conn, tx_marker)
                    except Exception:
                        if conn.in_transaction:
                            _rollback_write(conn, tx_marker)
                        raise

                if self._owns_write_transaction():
                    for memory_id, vector, metadata in pending_vectors:
                        self._queue_cache_delta("upsert", memory_id, vector, metadata, new_cv)
                    if not collect_cache_meta:
                        self._invalidate_cache()
                elif in_place_ok and synced_cache is not None:
                    with synced_cache.write_snapshot():
                        for memory_id, vec, meta in pending_vectors:
                            if not synced_cache.upsert_vector(memory_id, vec, row_meta=meta):
                                in_place_ok = False
                                break
                        if in_place_ok:
                            self._refresh_stamps_after_write_locked(synced_cache, content_version=new_cv)
                        else:
                            self._invalidate_cache()
                else:
                    self._invalidate_cache()

        self._run_after_commit_actions(after_commit_actions)
        for scrubbed_id, scrubbed_old_content, scrubbed_local in audit_scrubs:
            scrub_memory_update_from_audit(
                namespace=self.namespace,
                memory_id=scrubbed_id,
                old_content=scrubbed_old_content,
                local_only=scrubbed_local,
            )

    def resolve_id_prefix(
        self, prefix: str, *, include_private: bool = True,
        expected_source_agent: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve one canonical ID with bounded, policy-filtered SQL lookups."""
        clean = (prefix or "").strip()
        if not clean or not self.db_path.exists():
            return None
        validate_memory_id(clean)
        self._ensure_initialized()
        predicates = "namespace=? AND (? OR local_only=0) AND (? IS NULL OR source_agent=?)"
        params = (self.namespace, bool(include_private), expected_source_agent, expected_source_agent)
        with self._read_connect() as conn:
            exact = conn.execute(
                f"SELECT id FROM memories WHERE {predicates} AND id=? LIMIT 1", (*params, clean),
            ).fetchone()
            if exact is not None:
                return str(exact[0])
            matches = conn.execute(
                f"SELECT id FROM memories WHERE {predicates} AND id=? COLLATE NOCASE LIMIT 2", (*params, clean),
            ).fetchall()
            if not matches and len(clean) >= 4:
                escaped = clean.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
                matches = conn.execute(
                    f"SELECT id FROM memories WHERE {predicates} AND id LIKE ? ESCAPE '\\' LIMIT 2",
                    (*params, escaped + "%"),
                ).fetchall()
        if len(matches) == 1:
            return str(matches[0][0])
        if len(matches) > 1:
            raise ValueError("Ambiguous memory ID prefix; provide a longer prefix or the complete ID.")
        return None

    @staticmethod
    def _deserialize_memory_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["content"] = decrypt_text_if_encrypted(result["content"])
        result["tags"] = decrypt_text_if_encrypted(result.get("tags") or "[]")
        result["tags_list"] = _parse_tags_json(result["tags"])
        result["local_only"] = bool(result.get("local_only"))
        result["quarantined"] = bool(result.get("quarantined"))
        result["trust_tier"] = _normalize_trust_tier(result.get("trust_tier"))
        result["secret_ref"] = _parse_secret_ref_json(result.get("secret_ref"))
        return result

    def get(self, memory_id: str, *, include_private: bool = True) -> Optional[dict[str, Any]]:
        memory_id = validate_memory_id(memory_id)
        if not self.db_path.exists():
            return None
        self._ensure_initialized()
        with self._read_connect() as conn:
            row = conn.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE namespace=? AND id=? AND (? OR local_only=0)",
                (self.namespace, memory_id, bool(include_private)),
            ).fetchone()
            result = self._deserialize_memory_row(row) if row is not None else None
        return self._overlay_access_tracking(result) if result is not None else None

    def _overlay_access_tracking(self, row: dict[str, Any]) -> dict[str, Any]:
        """Overlay bounded local access statistics while keeping SQL content authoritative."""
        with _ACCESS_TRACKING_LOCK:
            buffered = _ACCESS_TRACKING_BUFFERS.get(self.db_path)
            pending = buffered.access_increments.get(row["id"]) if buffered is not None and buffered.store_generation in (None, row.get("store_generation")) and buffered.access_incarnations.get(row["id"]) == row["incarnation"] else None
        if pending is not None:
            row["access_count"] = int(row.get("access_count") or 0) + pending[0]
            row["accessed_at"] = max(str(row.get("accessed_at") or ""), pending[1])
        with _CACHE_LOCK:
            cached = _VECTOR_CACHE.get((self.db_path, self.namespace))
        if cached is not None:
            try:
                with cached.read_snapshot():
                    metadata = cached.rows_by_id.get(row["id"])
                    if metadata is not None and metadata.get("revision") == row.get("revision") and metadata.get("incarnation") == row.get("incarnation") and metadata.get("store_generation") == row.get("store_generation"):
                        row["access_count"] = max(int(row.get("access_count") or 0), int(metadata.get("access_count") or 0))
                        row["accessed_at"] = max(str(row.get("accessed_at") or ""), str(metadata.get("accessed_at") or ""))
            except CacheSnapshotInvalidatedError:
                pass
        return row

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
        *,
        include_private: bool = True,
        expected_source_agent: Optional[str] = None,
        expected_revision: Optional[int] = None,
        expected_incarnation: Optional[str] = None,
        expected_store_generation: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Update an existing memory in-place while preserving its `id`, `created_at`,
        `source_agent` (unless explicitly provided), and `access_count`.
        Finding S11: Always enforces `guard_ingest` and `guard_tags`, including when
        `content is None` and `local_only=False` is requested.
        """
        memory_id = validate_memory_id(memory_id)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError("expected_revision must be a positive integer.")
        if expected_incarnation is not None:
            _validate_incarnation(expected_incarnation, "expected_incarnation")
        if expected_store_generation is not None:
            _validate_incarnation(expected_store_generation, "expected_store_generation")
            if expected_store_generation != self.store_generation:
                raise StaleStoreError("The requested database generation changed; refresh and retry.")
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

        after_commit_actions: list[Callable[[], Any]] = []
        with namespace_write_lock(self.namespace, data_root=self.db_path.parent):
            with self._cache_lock:
                cache = self._is_cache_synced_locked()
                existing_meta = self.get(memory_id)
                if existing_meta is None:
                    return None
                self._check_mutation_permission(
                    bool(existing_meta.get("local_only")), existing_meta.get("source_agent"),
                    include_private, expected_source_agent,
                )

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
                if expected_source_agent is not None and new_agent != expected_source_agent:
                    raise ValueError("Memory mutation is not permitted for this agent.")
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
                new_revision = int(existing_meta.get("revision", 1)) + 1
                content_changed = new_content != old_content
                if content_changed and norm_vec is None:
                    new_emb_model = "needs-reembed"
                preserve_passages = not content_changed and norm_vec is None and new_emb_model == existing_meta["embedding_model"]
                new_passage_format = existing_meta.get("passage_format_version", "") if preserve_passages else ""
                new_passage_count = int(existing_meta.get("passage_count", 0)) if preserve_passages else 0
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
                    tx_marker = _begin_write(conn, self.namespace, self._store_generation)
                    try:
                        authoritative = conn.execute(
                            "SELECT revision,local_only,source_agent,incarnation FROM memories WHERE namespace=? AND id=?",
                            (self.namespace, memory_id),
                        ).fetchone()
                        if authoritative is None:
                            after_commit_actions = _commit_write(conn, tx_marker)
                            self._invalidate_cache()
                            return None
                        self._check_mutation_permission(
                            bool(authoritative[1]), authoritative[2], include_private, expected_source_agent
                        )
                        if expected_revision is not None and int(authoritative[0]) != expected_revision:
                            raise ValueError("Memory revision changed; no mutation was committed.")
                        if expected_incarnation is not None and authoritative[3] != expected_incarnation:
                            raise ValueError("Memory incarnation changed; no mutation was committed.")
                        if int(authoritative[0]) != int(existing_meta.get("revision", 1)) or authoritative[3] != existing_meta["incarnation"]:
                            raise ValueError("Memory changed before the mutation; refresh and retry.")
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
                                    secret_ref = ?,
                                    revision = ?
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
                                    new_revision,
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
                                    secret_ref = ?,
                                    revision = ?
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
                                    new_revision,
                                    memory_id,
                                    self.namespace,
                                ),
                            )
                        conn.execute("UPDATE memories SET passage_format_version=?,passage_count=? WHERE namespace=? AND id=?", (new_passage_format, new_passage_count, self.namespace, memory_id))
                        if preserve_passages:
                            conn.execute(
                                "UPDATE memory_passages SET parent_revision=? WHERE namespace=? AND parent_id=? AND parent_revision=? AND parent_incarnation=?",
                                (new_revision, self.namespace, memory_id, existing_meta["revision"], existing_meta["incarnation"]),
                            )
                        else:
                            conn.execute("DELETE FROM memory_passages WHERE namespace=? AND parent_id=?", (self.namespace, memory_id))
                            conn.execute("DELETE FROM embedding_jobs WHERE namespace=? AND memory_id=?", (self.namespace, memory_id))
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
                            quarantined=new_quarantined_bool,
                        )
                        if content_changed or became_private:
                            replacement_text = (
                                REDACTED_CONTENT_PLACEHOLDER if new_local_bool else new_content
                            )
                            _scrub_contradictions_in_tx(
                                conn, self.namespace, memory_id, replacement_text
                            )
                            _scrub_queries_in_tx(conn, self.namespace, old_content)
                            _scrub_pending_query_telemetry(self.namespace, old_content, data_root=self.db_path.parent)
                        self._schedule_embedding_in_tx(conn, memory_id, new_revision, new_emb_model, incarnation=existing_meta["incarnation"], preserve_job_target=preserve_passages)
                        self._record_mutation_in_tx(conn, "update", memory_id, new_revision)
                        new_cv = self._bump_content_version_in_tx(conn)
                        _commit_write(conn, tx_marker)
                    except Exception:
                        if conn.in_transaction:
                            _rollback_write(conn, tx_marker)
                        raise

                updated_row = {
                    "id": memory_id,
                    "revision": new_revision,
                    "incarnation": existing_meta["incarnation"],
                    "store_generation": self.store_generation,
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
                    "passage_format_version": new_passage_format,
                    "passage_count": new_passage_count,
                    "trust_tier": new_trust_tier,
                    "quarantined": new_quarantined_bool,
                    "secret_ref": new_secret_ref,
                }
                if self._owns_write_transaction():
                    self._queue_cache_delta("upsert", memory_id, norm_vec, updated_row, new_cv, preserve_vector=norm_vec is None and not content_changed)
                elif cache is not None:
                    with cache.write_snapshot():
                        cache_vector = norm_vec
                        if cache_vector is None and not content_changed:
                            existing_idx = cache.id_to_idx.get(memory_id)
                            if existing_idx is not None and cache._matrix_buf is not None:
                                cache_vector = cache._matrix_buf[existing_idx]
                        if cache.upsert_vector(memory_id, cache_vector, row_meta=updated_row):
                            self._refresh_stamps_after_write_locked(cache, content_version=new_cv)
                        else:
                            self._invalidate_cache()
                else:
                    self._invalidate_cache()

        self._run_after_commit_actions(after_commit_actions)
        if should_scrub_audit and not preserve_accessed_at:
            scrub_memory_update_from_audit(
                namespace=self.namespace,
                memory_id=memory_id,
                old_content=old_content_for_scrub,
                local_only=new_local_bool,
            )
        return self._mutation_result(dict(updated_row), include_private)

    def approve(
        self, memory_id: str, *, include_private: bool = True,
        expected_source_agent: Optional[str] = None,
        expected_revision: Optional[int] = None,
        expected_incarnation: Optional[str] = None,
        expected_store_generation: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Approve a quarantined memory (`quarantined=False`), preserving its provenance `trust_tier` (Finding S8)."""
        resolved = self.resolve_id_prefix(memory_id) or memory_id
        return self.update(
            memory_id=resolved, quarantined=False, preserve_accessed_at=True,
            include_private=include_private, expected_source_agent=expected_source_agent,
            expected_revision=expected_revision,
            expected_incarnation=expected_incarnation,
            expected_store_generation=expected_store_generation,
        )

    # --- Vector & Hybrid Search Operations ---

    @_retry_cache_snapshot
    def query(
        self,
        embedding: list[float],
        n: int = 5,
        query_embedding_model: Optional[str] = None,
    ) -> dict[str, list[list[Any]]]:
        empty = {"documents": [[]], "distances": [[]], "metadatas": [[]], "ids": [[]]}
        if not self.db_path.exists() or n <= 0:
            return empty
        if type(n) is not int or n > 1000:
            raise ValueError("n must be an integer between 1 and 1000")
        q_vec = _normalize_vector(embedding, enforce_dim=False)
        if q_vec.size != EMBEDDING_DIM or not np.isfinite(q_vec).all():
            raise ValueError(f"query embedding must contain {EMBEDDING_DIM} finite values")
        try:
            cache = self._get_vector_cache()
        except CacheBudgetExceededError:
            return self._streaming_vector_query(q_vec, n, query_embedding_model)
        with cache.read_snapshot():
            if cache.matrix is None or not cache.ids:
                return empty
            similarities = cache.matrix @ q_vec
            excluded = 0
            compatible_models = {model for model in cache.model_ids if not _is_mixed_space(model, query_embedding_model)}
            if len(compatible_models) != len(cache.model_ids):
                mask = np.fromiter(
                    (cache.rows_by_id[mid].get("embedding_model") in compatible_models for mid in cache.ids),
                    dtype=np.bool_, count=len(cache.ids),
                )
                excluded = len(cache.ids) - int(mask.sum())
                similarities[~mask] = -np.inf
            count = min(n, len(cache.ids) - excluded)
            if not count:
                rows = []
            else:
                top = np.argpartition(similarities, len(cache.ids) - count)[-count:]
                boundary = float(np.min(similarities[top]))
                above = np.flatnonzero(similarities > boundary)
                tied = heapq.nsmallest(
                    count - len(above),
                    ((cache.ids[int(index)], int(index)) for index in np.flatnonzero(similarities == boundary)),
                )
                best = [*map(int, above), *(index for _, index in tied)]
                best.sort(key=lambda index: (-float(similarities[index]), cache.ids[index]))
                rows = [(dict(cache.rows_by_id[cache.ids[index]]), float(similarities[index])) for index in best]
        self._warn_mixed_query(excluded, query_embedding_model)
        return self._format_vector_query(rows, streaming=False)

    @staticmethod
    def _warn_mixed_query(excluded: int, query_embedding_model: Optional[str]) -> None:
        if excluded and not os.environ.get("MESH_QUIET"):
            warnings.warn(
                f"Excluded {excluded} memories from semantic search due to mixed embedding space "
                f"(query uses '{query_embedding_model}'). Run 'mesh reembed' to re-embed historical memories.",
                UserWarning, stacklevel=3,
            )

    def _streaming_vector_query(self, vector, n, query_embedding_model):
        heap: list[_ScoredMemoryID] = []
        excluded = 0
        with self._read_connect() as conn:
            for raw in conn.execute("SELECT id,embedding_model,embedding FROM memories WHERE namespace=?", (self.namespace,)):
                row = dict(raw)
                blob = row.pop("embedding")
                stored_vector = self._stream_row_vector(row, blob, None)
                if stored_vector is None:
                    continue
                if _is_mixed_space(row["embedding_model"], query_embedding_model):
                    excluded += 1
                    continue
                _keep_best_scored_id(heap, float(stored_vector @ vector), row["id"], n)
            best = sorted(heap, key=lambda item: (-item.score, item.memory_id))
            ids = [item.memory_id for item in best]
            metadata = {}
            for offset in range(0, len(ids), 512):
                block = ids[offset:offset + 512]
                placeholders = ",".join("?" for _ in block)
                raw_rows = conn.execute(
                    f"SELECT {_MEMORY_COLUMNS},embedding FROM memories WHERE namespace=? AND id IN ({placeholders})",
                    (self.namespace, *block),
                )
                for raw in raw_rows:
                    data = dict(raw)
                    blob = data.pop("embedding")
                    row = self._deserialize_memory_row(data)
                    self._stream_row_vector(row, blob, None)
                    metadata[row["id"]] = row
            rows = [(metadata[item.memory_id], item.score) for item in best]
        self._warn_mixed_query(excluded, query_embedding_model)
        return self._format_vector_query(rows, streaming=True)

    @staticmethod
    def _format_vector_query(rows, *, streaming):
        ids, documents, distances, metadatas = [], [], [], []
        for row, score in rows:
            ids.append(row["id"])
            documents.append(row["content"])
            distances.append(max(0.0, 1.0 - max(-1.0, min(1.0, score))))
            metadatas.append({
                "memory_type": row["memory_type"], "confidence": float(row["confidence"]),
                "source_agent": row["source_agent"], "tags": row["tags"],
                "created_at": row["created_at"], "accessed_at": row["accessed_at"],
                "access_count": int(row["access_count"] or 0), "ttl_days": row["ttl_days"],
                "decay_start_at": row["decay_start_at"], "local_only": 1 if row.get("local_only") else 0,
                "embedding_model": row["embedding_model"], "trust_tier": row.get("trust_tier", "user"),
                "quarantined": 1 if row.get("quarantined") else 0,
                "retrieval_mode": "streaming" if streaming else "cached",
                "retrieval_degradation_reason": "cache_budget" if streaming else None,
            })
        return {"ids": [ids], "documents": [documents], "distances": [distances], "metadatas": [metadatas]}

    def _fts_lexical_scores(
        self,
        query_text: str,
        limit: int = 128,
        conn: Optional[sqlite3.Connection] = None,
        *,
        expected_content_version: Optional[int] = None,
    ) -> dict[str, float]:
        if not self._fts_enabled or is_encryption_enabled() or not query_text or not self.db_path.exists():
            return {}
        _, tokens, _ = _extract_search_tokens(query_text)
        non_framing = [t for t in tokens if _fast_stem(t) not in _FRAMING_TOKENS]
        target_tokens = non_framing or tokens
        clauses = ['"' + tok.replace('"', '""') + '"' for tok in target_tokens[:24] if tok]
        if not clauses:
            return {}
        sql = """
            SELECT memories_fts.id,
                   bm25(memories_fts, 0.0, 1.0, 0.8, 0.0, 0.0) AS rank_score
            FROM memories_fts
            JOIN memories AS m ON m.id = memories_fts.id
            WHERE memories_fts MATCH ? AND m.namespace = ?
              AND m.local_only = 0 AND m.quarantined = 0
              AND m.content NOT LIKE 'enc:v1:%'
            ORDER BY rank_score ASC, memories_fts.id ASC LIMIT ?
        """

        def read_scores(handle):
            own_transaction = not handle.in_transaction
            if own_transaction:
                handle.execute("BEGIN")
            try:
                version = self._read_content_version_locked(handle)
                if expected_content_version is not None and version != expected_content_version:
                    return {}
                rows = handle.execute(sql, (" OR ".join(dict.fromkeys(clauses)), self.namespace, limit)).fetchall()
                return {str(mid): max(0.0, -float(score)) for mid, score in rows}
            finally:
                if own_transaction and handle.in_transaction:
                    handle.rollback()

        try:
            if conn is not None:
                return read_scores(conn)
            with self._read_connect() as own_conn:
                return read_scores(own_conn)
        except sqlite3.DatabaseError:
            return {}

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
                tok_compact = _PUNCT_SPLIT_RE.sub("", tok)
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
            if len(q_norm_phrase) >= 4 and _phrase_pattern(q_norm_phrase).search(_clean_possessives(doc_content)):
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

    def _query_eligible_ids(
        self, cache, *, memory_type, tags, source_agent, include_private,
        include_quarantined, include_stale, min_confidence, now,
    ):
        selectors = []
        if memory_type:
            selectors.append(cache.type_ids.get(memory_type.strip().lower(), set()))
        if source_agent:
            selectors.append(cache.agent_ids.get(source_agent, set()))
        for tag in tags or []:
            selectors.append(cache.tag_ids.get(str(tag).strip().lower(), set()))
        if selectors:
            selectors.sort(key=len)
            eligible = set(selectors[0])
            for members in selectors[1:]:
                eligible.intersection_update(members)
        else:
            eligible = cache.all_ids
        denied = tuple(
            members for members, allowed in (
                (cache.private_ids, include_private), (cache.quarantine_ids, include_quarantined),
            ) if not allowed and members
        )
        if denied:
            known_size = None
            if eligible is cache.all_ids:
                if not include_private and not include_quarantined:
                    known_size = cache.visible_id_count
                elif not include_private:
                    known_size = len(cache.all_ids) - len(cache.private_ids)
                else:
                    known_size = len(cache.all_ids) - len(cache.quarantine_ids)
            eligible = _QueryIds(eligible, denied, size=known_size)
        if not include_stale or min_confidence > 0.0:
            eligible = {
                mid for mid in eligible
                if (state := _compute_row_effective_confidence(cache.rows_by_id[mid], now=now))[0] >= min_confidence
                and (include_stale or not state[1])
            }
        return eligible

    @staticmethod
    def _cached_bm25(lex, q_stems, q_synonyms, idf_map, avg_doc_len):
        frequency = lex["term_freq"]
        normalizer = 1.2 * (0.25 + 0.75 * lex["doc_len"] / max(1.0, avg_doc_len))
        score = 0.0
        for stem, alternatives in q_synonyms.items():
            tf = frequency.get(stem, 0)
            if not tf:
                tf = 0.25 * max((frequency.get(s, 0) for s in alternatives), default=0)
            if tf:
                score += idf_map.get(stem, 0.0) * (tf * 2.2 / (tf + normalizer))
        return score

    def get_indexing_status(self, memory_ids: list[str], *, include_private: bool = True) -> dict[str, dict[str, Any]]:
        if not isinstance(memory_ids, (list, tuple)) or len(memory_ids) > 128:
            raise ValueError("Indexing status accepts at most 128 memory IDs")
        ids = list(dict.fromkeys(validate_memory_id(mid) for mid in memory_ids))
        if not ids or not self.db_path.exists():
            return {}
        placeholders = ",".join("?" for _ in ids)
        from .passage_search import passage_metadata

        result = {}
        with self._read_connect() as conn:
            rows = conn.execute(
                f"SELECT id,namespace,content,revision,embedding_model,local_only,embedding IS NOT NULL AS has_vector,incarnation,passage_format_version,passage_count FROM memories WHERE namespace=? AND id IN ({placeholders})",
                (self.namespace, *ids),
            ).fetchall()
            for raw in rows:
                parent = dict(raw)
                if parent["local_only"] and not include_private:
                    continue
                indexed = parent["has_vector"] and parent["embedding_model"] not in ("hash-384", "needs-reembed", "unknown", None)
                complete = False
                if indexed and parent["passage_format_version"] == PASSAGE_FORMAT_VERSION:
                    parent["content"] = decrypt_text_if_encrypted(parent["content"])
                    complete = parent["passage_count"] == 0 or len(passage_metadata(conn, parent)) == parent["passage_count"]
                mid = parent["id"]
                result[mid] = {"id": mid, "revision": int(parent["revision"]), "incarnation": parent["incarnation"], "store_generation": self.store_generation,
                               "embedding_model": parent["embedding_model"], "indexing_state": "indexed" if indexed else "pending",
                               "passage_format_version": parent["passage_format_version"], "passage_count": parent["passage_count"],
                               "passage_indexing_state": "indexed" if complete else "pending"}
        return result

    @_retry_cache_snapshot
    def revalidate_query_results(
        self, results: list[dict[str, Any]], *, include_private: bool = False,
        include_quarantined: bool = False, include_stale: bool = True,
        min_confidence: float = 0.0, memory_type: Optional[str] = None,
        tags: Optional[list[str]] = None, source_agent: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Recheck visibility and complete content identity after slow candidate processing."""
        if not results or not self.db_path.exists():
            return []
        now = _utc_now()
        tag_set = {str(tag).strip().lower() for tag in tags or []}
        accepted = []
        where, params = self._query_sql_filter(
            memory_type=memory_type, source_agent=source_agent,
            include_private=include_private, include_quarantined=include_quarantined,
        )
        columns = "id,revision,incarnation,(SELECT value FROM mesh_meta WHERE key='store_generation') AS store_generation,tags,confidence,ttl_days,decay_start_at,created_at"
        with self._read_connect() as conn:
            for offset in range(0, len(results), 512):
                block = results[offset:offset + 512]
                ids = [result.get("id") for result in block if isinstance(result.get("id"), str)]
                if not ids:
                    continue
                placeholders = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"SELECT {columns} FROM memories WHERE {where} AND id IN ({placeholders})",
                    (*params, *ids),
                )
                metadata = {row["id"]: dict(row) for row in rows}
                for result in block:
                    row = metadata.get(result.get("id"))
                    if row is None or type(result.get("revision")) is not int or row["revision"] != result["revision"]:
                        continue
                    if row["incarnation"] != result.get("incarnation") or row["store_generation"] != result.get("store_generation"):
                        continue
                    if tag_set:
                        current_tags = _parse_tags_json(decrypt_text_if_encrypted(row.get("tags") or "[]"))
                        if not tag_set.issubset(str(tag).lower() for tag in current_tags):
                            continue
                    confidence, stale = _compute_row_effective_confidence(row, now=now)
                    if confidence < min_confidence or (stale and not include_stale):
                        continue
                    accepted.append(result)
        return accepted

    def _query_sql_filter(
        self, *, memory_type: Optional[str] = None, source_agent: Optional[str] = None,
        include_private: bool = False, include_quarantined: bool = False,
    ) -> tuple[str, tuple[Any, ...]]:
        clauses = ["namespace=?"]
        params: list[Any] = [self.namespace]
        if not include_private:
            clauses.append("local_only=0")
        if not include_quarantined:
            clauses.append("quarantined=0")
        if memory_type:
            clauses.append("lower(memory_type)=?")
            params.append(memory_type.strip().lower())
        if source_agent:
            clauses.append("source_agent=?")
            params.append(source_agent)
        return " AND ".join(clauses), tuple(params)

    @staticmethod
    def _stream_row_eligible(row, *, tag_set, include_stale, min_confidence, now):
        if tag_set and not tag_set.issubset(str(tag).lower() for tag in row["tags_list"]):
            return False
        if not include_stale or min_confidence > 0.0:
            confidence, stale = _compute_row_effective_confidence(row, now=now)
            if confidence < min_confidence or (stale and not include_stale):
                return False
        return True

    @staticmethod
    def _stream_row_vector(row, blob, query_embedding_model):
        model = str(row.get("embedding_model") or "unknown")
        if not blob:
            row["embedding_model"] = "needs-reembed"
            return None
        if query_embedding_model is not None and model not in (query_embedding_model, "unknown"):
            return None
        vector = _blob_to_vector(blob)
        if vector.ndim != 1 or vector.size != EMBEDDING_DIM or not np.isfinite(vector).all():
            row["embedding_model"] = "needs-reembed"
            return None
        if model == "unknown":
            model = "hash-384" if np.all(vector[368:] == 0.0) and np.count_nonzero(vector == 0.0) >= 16 else "onnx-minilm-l6-v2"
            row["embedding_model"] = model
        return vector if query_embedding_model is None or model == query_embedding_model else None

    @_retry_cache_snapshot
    def exact_content_ids(
        self, content: str, *, include_private: bool = True,
        include_quarantined: bool = False, source_agent: Optional[str] = None,
        limit: int = 16,
    ) -> list[str]:
        """Backend-independent equality lookup over protected ephemeral cache data."""
        if not self.db_path.exists():
            return []
        content_key = _content_identity(content)
        try:
            cache = self._get_vector_cache()
        except CacheBudgetExceededError:
            where, params = self._query_sql_filter(
                source_agent=source_agent, include_private=include_private,
                include_quarantined=include_quarantined,
            )
            with self._read_connect() as conn:
                rows = conn.execute(f"SELECT id,content FROM memories WHERE {where}", params)
                return heapq.nsmallest(
                    max(1, min(limit, 128)),
                    (str(row["id"]) for row in rows if _content_identity(decrypt_text_if_encrypted(row["content"])) == content_key),
                )
        with cache.read_snapshot():
            return heapq.nsmallest(
                max(1, min(limit, 128)),
                (
                    mid for mid in cache.content_ids.get(content_key, set())
                    if (include_private or mid not in cache.private_ids)
                    and (include_quarantined or mid not in cache.quarantine_ids)
                    and (source_agent is None or mid in cache.agent_ids.get(source_agent, set()))
                ),
            )

    def _hybrid_candidate(self, row, lex, sem, spec, idf_map, total_query_idf, query_embedding_model):
        lex_score, _, _, reason = self._score_candidate_lexical(
            q_content_tokens=spec.tokens, q_stems=spec.stems, q_norm_phrase=spec.phrase,
            q_compact=spec.compact, doc_content=row["content"], doc_tags=row["tags_list"],
            idf_map=idf_map, total_query_idf=total_query_idf,
            fts_score=0.0, q_synonyms=spec.synonyms, cached_lex=lex,
        )
        literal_match = bool(spec.literals) and all(token in lex["all_doc_tokens"] for token in spec.literals)
        if spec.identifier_only and not literal_match:
            return None
        sem = max(0.0, min(1.0, sem))
        lexical_weight = 0.55 if query_embedding_model == "hash-384" else 0.25
        similarity = sem + (1.0 - sem) * lexical_weight * lex_score
        if literal_match and spec.identifier_only:
            similarity = max(similarity, 0.90)
        metadata = {key: value for key, value in row.items() if not key.startswith("_")}
        return metadata, sem, lex_score, similarity, reason, literal_match

    @staticmethod
    def _rank_hybrid_results(candidate_rows, dense_scores, bm25_scores, exact_best, spec, n, *, streaming=False, rank_scores=None, candidate_count=None):
        rrf_scores = Counter()
        channels = (
            (sorted(dense_scores, key=lambda mid: (-dense_scores[mid], mid)), 1.0),
            (sorted(bm25_scores, key=lambda mid: (-bm25_scores[mid], mid)), 0.9),
            (exact_best, 1.5),
        )
        for ranked, weight in channels:
            for rank, mid in enumerate(ranked, 1):
                rrf_scores[mid] += weight / (60.0 + rank)
        results = []
        for mid, (row, sem, lex_score, similarity, reason, literal_match) in candidate_rows.items():
            rank_score = rank_scores[mid] if rank_scores is not None else float(rrf_scores[mid]) if spec.mode == "rrf" else similarity
            passage = row.pop("_supporting_passage", None)
            if passage is None:
                excerpt, span_start, span_end = _query_excerpt(row["content"], spec.tokens)
            else:
                span_start, span_end, ordinal = passage
                excerpt = row["content"][span_start:span_end]
                row["selected_passage_ordinal"] = ordinal
            row.update({
                "semantic_similarity": round(sem, 4),
                "lexical_score": round(lex_score, 4),
                "bm25_score": round(float(row.pop("_passage_bm25_score", bm25_scores.get(mid, 0.0))), 6),
                "similarity": round(similarity, 4),
                "rank_score": round(rank_score, 8),
                "ranking_mode": spec.mode,
                "retrieval_mode": "streaming" if streaming else "cached",
                "retrieval_degradation_reason": "cache_budget" if streaming else None,
                "candidate_count": len(candidate_rows) if candidate_count is None else candidate_count,
                "match_reason": "exact_identifier" if literal_match else reason,
                "snippet": _make_snippet(excerpt),
                "supporting_spans": [{"memory_id": mid, "revision": int(row.get("revision", 1)), "incarnation": row.get("incarnation"), "store_generation": row.get("store_generation"), "start": span_start, "end": span_end, "text": excerpt}],
            })
            results.append(row)
        results.sort(key=lambda row: (-row["rank_score"], -row["similarity"], -row["semantic_similarity"], row["id"]))
        return results[:n]

    def _streaming_hybrid_query(
        self, spec, *, n, memory_type, tags, source_agent, include_private,
        include_quarantined, include_stale, min_confidence, now, query_embedding_model,
    ):
        """Consider every authorized row while retaining only bounded candidates."""
        where, params = self._query_sql_filter(
            memory_type=memory_type, source_agent=source_agent,
            include_private=include_private, include_quarantined=include_quarantined,
        )
        tag_set = {str(tag).strip().lower() for tag in tags or []}
        if passage_index_enabled() and (spec.tokens or spec.literals or spec.phrase or spec.compact):
            from .passage_search import passage_query

            results = passage_query(self, spec, cache=None, n=n, memory_type=memory_type, tags=tags, source_agent=source_agent,
                                    include_private=include_private, include_quarantined=include_quarantined,
                                    include_stale=include_stale, min_confidence=min_confidence, now=now,
                                    query_embedding_model=query_embedding_model)
            if results is not None:
                return results
        # Empty-vector lookups (including guarded deduplication) do not need
        # bodies or lexical statistics for every row in an oversized namespace.
        if not (spec.tokens or spec.literals or spec.phrase or spec.compact):
            vector_where = where + " AND embedding IS NOT NULL"
            vector_params = params
            if query_embedding_model is not None:
                vector_where += " AND COALESCE(NULLIF(embedding_model,''),'unknown') IN (?,'unknown')"
                vector_params = (*params, query_embedding_model)
            columns = "id,embedding_model,embedding"
            if tag_set:
                columns += ",tags"
            if not include_stale or min_confidence > 0.0:
                columns += ",confidence,ttl_days,decay_start_at,created_at"
            dense_heap: list[_ScoredMemoryID] = []
            candidate_rows = {}
            with self._read_connect() as conn:
                for raw in conn.execute(f"SELECT {columns} FROM memories WHERE {vector_where}", vector_params):
                    row = dict(raw)
                    blob = row.pop("embedding")
                    row["tags_list"] = _parse_tags_json(decrypt_text_if_encrypted(row.get("tags") or "[]")) if tag_set else []
                    if not self._stream_row_eligible(row, tag_set=tag_set, include_stale=include_stale, min_confidence=min_confidence, now=now):
                        continue
                    vector = self._stream_row_vector(row, blob, query_embedding_model)
                    if vector is not None:
                        _keep_best_scored_id(dense_heap, float(vector @ spec.vector), row["id"], spec.pool)
                dense_scores = {item.memory_id: item.score for item in dense_heap}
                candidate_ids = sorted(dense_scores)
                for offset in range(0, len(candidate_ids), 512):
                    block = candidate_ids[offset:offset + 512]
                    placeholders = ",".join("?" for _ in block)
                    rows = conn.execute(
                        f"SELECT {_MEMORY_COLUMNS},embedding FROM memories WHERE {where} AND id IN ({placeholders})",
                        (*params, *block),
                    )
                    for raw in rows:
                        data = dict(raw)
                        blob = data.pop("embedding", None)
                        row = self._deserialize_memory_row(data)
                        if not self._stream_row_eligible(row, tag_set=tag_set, include_stale=include_stale, min_confidence=min_confidence, now=now):
                            continue
                        if self._stream_row_vector(row, blob, query_embedding_model) is None:
                            continue
                        candidate = self._hybrid_candidate(row, {}, dense_scores[row["id"]], spec, {}, 1.0, query_embedding_model)
                        if candidate is not None:
                            candidate_rows[row["id"]] = candidate
            results = self._rank_hybrid_results(candidate_rows, dense_scores, {}, [], spec, n, streaming=True)
            # Revalidate current identity and policy after releasing the snapshot.
            # TTL/confidence eligibility already used the caller's reference time.
            return self.revalidate_query_results(
                results, include_private=include_private, include_quarantined=include_quarantined,
                memory_type=memory_type, tags=tags, source_agent=source_agent,
            )
        query_stems = set(spec.stems)
        document_frequency = dict.fromkeys(query_stems, 0)
        total_docs = 0
        total_doc_len = 0
        dense_heap: list[_ScoredMemoryID] = []
        bm25_heap: list[_ScoredMemoryID] = []
        exact_heap: list[_ScoredMemoryID] = []

        def eligible_rows(conn, *, with_vectors):
            columns = _MEMORY_COLUMNS + (", embedding" if with_vectors else "")
            for raw in conn.execute(f"SELECT {columns} FROM memories WHERE {where}", params):
                data = dict(raw)
                blob = data.pop("embedding", None)
                row = self._deserialize_memory_row(data)
                if self._stream_row_eligible(row, tag_set=tag_set, include_stale=include_stale, min_confidence=min_confidence, now=now):
                    yield row, blob

        # Two passes share one dedicated reader snapshot. No ID set, metadata
        # dictionary, lexical index or duplicate matrix grows with corpus size.
        with self._read_connect() as conn:
            for row, blob in eligible_rows(conn, with_vectors=True):
                total_docs += 1
                if spec.tokens:
                    doc_len, df_stems = _build_row_lexical_statistics(row["content"], row["tags_list"])
                    total_doc_len += doc_len
                    for stem in query_stems:
                        if stem in df_stems:
                            document_frequency[stem] += 1
                vector = self._stream_row_vector(row, blob, query_embedding_model)
                if vector is not None:
                    _keep_best_scored_id(dense_heap, float(vector @ spec.vector), row["id"], spec.pool)
            if not total_docs:
                return []
            avg_doc_len = total_doc_len / total_docs
            has_specific = any(stem not in _FRAMING_TOKENS for stem in spec.stems)
            idf_map = {}
            for stem, df in document_frequency.items():
                weight = math.log1p((total_docs - df + 0.5) / (df + 0.5))
                if has_specific and stem in _FRAMING_TOKENS:
                    weight *= 0.12
                idf_map[stem] = max(0.001, weight)
            total_query_idf = sum(idf_map.get(stem, 0.0) for stem in spec.stems) or 1.0
            if spec.tokens or spec.literals:
                for row, _ in eligible_rows(conn, with_vectors=False):
                    lex = _build_row_lexical_entry(row["content"], row["tags_list"])
                    score = self._cached_bm25(lex, spec.stems, spec.stem_synonyms, idf_map, avg_doc_len)
                    if score > 0.0:
                        _keep_best_scored_id(bm25_heap, score, row["id"], spec.pool)
                    if any(token in lex["indexed_tokens"] for token in spec.literals):
                        _keep_best_scored_id(exact_heap, score, row["id"], spec.pool)
            dense_scores = {item.memory_id: item.score for item in dense_heap}
            bm25_scores = {item.memory_id: item.score for item in bm25_heap}
            exact_best = [item.memory_id for item in sorted(exact_heap, key=lambda item: (-item.score, item.memory_id))]
            candidate_ids = sorted(set(dense_scores) | set(bm25_scores) | set(exact_best))
            candidate_rows = {}
            for offset in range(0, len(candidate_ids), 512):
                block = candidate_ids[offset:offset + 512]
                placeholders = ",".join("?" for _ in block)
                rows = conn.execute(
                    f"SELECT {_MEMORY_COLUMNS},embedding FROM memories WHERE {where} AND id IN ({placeholders})",
                    (*params, *block),
                )
                for raw in rows:
                    data = dict(raw)
                    blob = data.pop("embedding", None)
                    row = self._deserialize_memory_row(data)
                    vector = self._stream_row_vector(row, blob, query_embedding_model)
                    sem = dense_scores.get(row["id"])
                    if sem is None:
                        if vector is not None:
                            sem = float(vector @ spec.vector)
                        elif query_embedding_model == "hash-384":
                            from .memory import _hash_embed_384
                            sem = float(_hash_embed_384(row["content"]) @ spec.vector)
                        else:
                            sem = 0.0
                    lex = _build_row_lexical_entry(row["content"], row["tags_list"])
                    candidate = self._hybrid_candidate(row, lex, sem, spec, idf_map, total_query_idf, query_embedding_model)
                    if candidate is not None:
                        candidate_rows[row["id"]] = candidate
        return self._rank_hybrid_results(candidate_rows, dense_scores, bm25_scores, exact_best, spec, n, streaming=True)

    @_retry_cache_snapshot
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
        """Independent, policy-filtered lexical/exact/dense candidate retrieval."""
        if not self.db_path.exists() or n <= 0:
            return []
        if type(n) is not int or n > 1000:
            raise ValueError("n must be an integer between 1 and 1000")
        self._maybe_lazy_flush_telemetry()
        q_vec = _normalize_vector(embedding, enforce_dim=False)
        if q_vec.ndim != 1 or q_vec.size != EMBEDDING_DIM or not np.isfinite(q_vec).all():
            raise ValueError(f"query embedding must contain {EMBEDDING_DIM} values")
        mode = os.environ.get("MESH_RECALL_FUSION", "calibrated").strip().lower()
        if mode not in ("calibrated", "rrf"):
            raise ValueError("MESH_RECALL_FUSION must be calibrated or rrf")
        pool = max(n, env_int("MESH_RECALL_CANDIDATES", 96, minimum=16, maximum=512))
        ref_now = now if now is not None else _utc_now()
        norm_q = _clean_possessives(query_text)
        _, q_tokens, _ = _extract_search_tokens(query_text)
        q_stems = [_fast_stem(t) for t in q_tokens]
        q_synonyms = expand_query_tokens(q_tokens)
        stem_synonyms = {
            stem: {_fast_stem(s) for s in q_synonyms.get(tok, set())}
            for tok, stem in zip(q_tokens, q_stems)
        }
        q_phrase = " ".join(_WORD_RE.findall(norm_q))
        q_compact = _PUNCT_SPLIT_RE.sub("", q_phrase)
        raw_query_tokens = [t.strip("._:-/\\") for t in _TOKEN_RE.findall(norm_q)]
        useful_raw_tokens = [t for t in raw_query_tokens if t and t not in _LEXICAL_STOPWORDS]
        literal_tokens = [
            t for t in useful_raw_tokens
            if t.isdigit() or any(c in t for c in "._:/\\-")
            or (any(c.isdigit() for c in t) and any(c.isalpha() for c in t))
        ]
        identifier_only = bool(literal_tokens) and len(useful_raw_tokens) == 1
        if not query_text:
            pool = n
        spec = _HybridQuerySpec(q_vec, q_tokens, q_stems, q_synonyms, stem_synonyms, q_phrase, q_compact, literal_tokens, identifier_only, pool, mode)
        try:
            cache = self._get_vector_cache()
        except CacheBudgetExceededError:
            cache = None
        if cache is not None and cache.passages is not None and passage_index_enabled() and (spec.tokens or spec.literals or spec.phrase or spec.compact):
            from .passage_search import passage_query

            try:
                results = passage_query(self, spec, cache=cache, n=n, memory_type=memory_type, tags=tags, source_agent=source_agent,
                                        include_private=include_private, include_quarantined=include_quarantined,
                                        include_stale=include_stale, min_confidence=min_confidence, now=ref_now,
                                        query_embedding_model=query_embedding_model)
                if results is not None:
                    return results
            except CacheBudgetExceededError:
                self._invalidate_cache()
                cache = None
        # Active exception tracebacks can retain an abandoned cache generation
        # and its lease until the handler exits, starving streaming workspace.
        if cache is None:
            return self._streaming_hybrid_query(
                spec, n=n, memory_type=memory_type, tags=tags, source_agent=source_agent,
                include_private=include_private, include_quarantined=include_quarantined,
                include_stale=include_stale, min_confidence=min_confidence, now=ref_now,
                query_embedding_model=query_embedding_model,
            )
        with cache.read_snapshot():
            eligible = self._query_eligible_ids(
                cache, memory_type=memory_type, tags=tags, source_agent=source_agent,
                include_private=include_private, include_quarantined=include_quarantined,
                include_stale=include_stale, min_confidence=min_confidence, now=ref_now,
            )
            if not eligible:
                return []
            version = cache.content_version
            # FTS5's corpus statistics cannot be scoped by a WHERE predicate. Its
            # native ranks are safe only when every indexed public row is eligible.
            use_native_fts = (
                not is_encryption_enabled()
                and not memory_type and not tags and not source_agent
                and include_stale and min_confidence <= 0.0
                and (not include_private or not cache.private_ids)
                and (not include_quarantined or not cache.quarantine_ids)
            )
        native_scores = self._fts_lexical_scores(
            query_text, limit=pool, expected_content_version=version,
        ) if use_native_fts else {}

        with cache.read_snapshot():
            eligible = self._query_eligible_ids(
                cache, memory_type=memory_type, tags=tags, source_agent=source_agent,
                include_private=include_private, include_quarantined=include_quarantined,
                include_stale=include_stale, min_confidence=min_confidence, now=ref_now,
            )
            if not eligible:
                return []
            if cache.content_version != version:
                native_scores = {}
            native_scores = {mid: score for mid, score in native_scores.items() if mid in eligible}
            total_docs = len(eligible)
            if not memory_type and not tags and not source_agent and include_stale and min_confidence <= 0.0 and not include_private and not include_quarantined:
                total_doc_len = cache.visible_doc_len_total
            elif eligible is cache.all_ids:
                total_doc_len = cache.doc_len_total
            elif isinstance(eligible, _QueryIds) and eligible.base is cache.all_ids:
                total_doc_len = cache.doc_len_total - sum(
                    cache.lexical_by_id[mid]["doc_len"] for mid in eligible.excluded if mid in cache.all_ids
                )
            elif len(cache.all_ids) - total_docs < total_docs:
                total_doc_len = cache.doc_len_total - sum(
                    cache.lexical_by_id[mid]["doc_len"] for mid in cache.all_ids if mid not in eligible
                )
            else:
                total_doc_len = sum(cache.lexical_by_id[mid]["doc_len"] for mid in eligible)
            avg_doc_len = total_doc_len / max(1, total_docs)
            has_specific = any(s not in _FRAMING_TOKENS for s in q_stems)
            idf_map = {}
            for stem in set(q_stems):
                postings = cache.stem_to_ids.get(stem, set())
                if not memory_type and not tags and not source_agent and include_stale and min_confidence <= 0.0 and not include_private and not include_quarantined:
                    df = cache.visible_stem_df.get(stem, 0)
                elif eligible is cache.all_ids:
                    df = len(postings)
                elif isinstance(eligible, _QueryIds) and eligible.base is cache.all_ids:
                    df = sum(mid in eligible for mid in postings)
                elif len(eligible) < len(postings):
                    df = sum(mid in postings for mid in eligible)
                else:
                    df = sum(mid in eligible for mid in postings)
                weight = math.log1p((total_docs - df + 0.5) / (df + 0.5))
                if has_specific and stem in _FRAMING_TOKENS:
                    weight *= 0.12
                idf_map[stem] = max(0.001, weight)
            total_query_idf = sum(idf_map.get(s, 0.0) for s in q_stems) or 1.0

            def lexical_score(mid):
                return self._cached_bm25(
                    cache.lexical_by_id[mid], q_stems, stem_synonyms, idf_map, avg_doc_len,
                )

            posting_lists = []
            for tok, stem in zip(q_tokens, q_stems):
                posting_lists.append(cache.token_to_ids.get(tok, set()))
                posting_lists.append(cache.stem_to_ids.get(stem, set()))
                for synonym in q_synonyms.get(tok, set()):
                    posting_lists.append(cache.token_to_ids.get(synonym, set()))
            if sum(len(p) for p in posting_lists) <= pool * 8:
                lexical_ids = {mid for members in posting_lists for mid in members if mid in eligible}
            elif native_scores:
                lexical_ids = set(native_scores)
                for members in posting_lists:
                    if len(members) <= pool:
                        lexical_ids.update(mid for mid in members if mid in eligible)
            else:
                # Only the bounded heap is materialized, even for a ubiquitous word.
                lexical_ids = eligible
            lex_best = heapq.nsmallest(pool, lexical_ids, key=lambda mid: (-lexical_score(mid), mid))
            bm25_scores = {}
            for mid in set(lex_best) | set(native_scores):
                score = lexical_score(mid)
                if score > 0.0:
                    bm25_scores[mid] = score

            dense_scores = {}
            matrix = cache.matrix
            if query_embedding_model is None:
                compatible = eligible
            else:
                compatible = cache.model_ids.get(query_embedding_model, set())
            if matrix is not None and compatible:
                all_compatible = query_embedding_model is None or (
                    len(cache.model_ids) == 1 and query_embedding_model in cache.model_ids
                )
                if len(eligible) == len(cache.all_ids) and all_compatible:
                    dense_ids = cache.ids
                    similarities = matrix @ q_vec
                else:
                    dense_ids = [mid for mid in cache.ids if mid in eligible and mid in compatible]
                    similarities = np.empty(len(dense_ids), dtype=np.float32)
                    for offset in range(0, len(dense_ids), 512):
                        block = dense_ids[offset : offset + 512]
                        indices = np.fromiter((cache.id_to_idx[mid] for mid in block), dtype=np.intp, count=len(block))
                        similarities[offset : offset + len(block)] = matrix[indices] @ q_vec
                if dense_ids:
                    if len(dense_ids) > pool:
                        top = np.argpartition(-similarities, pool - 1)[:pool]
                        boundary = float(np.min(similarities[top]))
                        above = np.flatnonzero(similarities > boundary)
                        dense_scores = {dense_ids[int(i)]: float(similarities[int(i)]) for i in above}
                        tied = heapq.nsmallest(
                            pool - len(dense_scores),
                            (mid for mid, score in zip(dense_ids, similarities) if float(score) == boundary),
                        )
                        dense_scores.update((mid, boundary) for mid in tied)
                    else:
                        dense_scores = {mid: float(score) for mid, score in zip(dense_ids, similarities)}

            exact_ids = set()
            for token in literal_tokens:
                exact_ids.update(mid for mid in cache.token_to_ids.get(token, set()) if mid in eligible)
            exact_best = heapq.nsmallest(pool, exact_ids, key=lambda mid: (-lexical_score(mid), mid))
            candidate_ids = set(dense_scores) | set(bm25_scores) | set(exact_best)
            candidate_rows = {}
            for mid in candidate_ids:
                row = cache.rows_by_id[mid]
                lex = cache.lexical_by_id[mid]
                sem = dense_scores.get(mid)
                if sem is None:
                    index = cache.id_to_idx.get(mid)
                    if index is not None and matrix is not None and mid in compatible:
                        sem = float(matrix[index] @ q_vec)
                    elif query_embedding_model == "hash-384":
                        from .memory import _hash_embed_384
                        sem = float(_hash_embed_384(row["content"]) @ q_vec)
                    else:
                        sem = 0.0
                candidate = self._hybrid_candidate(row, lex, sem, spec, idf_map, total_query_idf, query_embedding_model)
                if candidate is not None:
                    candidate_rows[mid] = candidate
        return self._rank_hybrid_results(candidate_rows, dense_scores, bm25_scores, exact_best, spec, n)

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
        if type(limit) is not int or limit > 1000:
            raise ValueError("limit must be an integer between 1 and 1000")

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
                    "revision": int(cand.get("revision", 1)),
                    "incarnation": cand["incarnation"],
                    "store_generation": cand["store_generation"],
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
                return self.revalidate_query_results(suggestions, include_private=include_private, include_quarantined=include_quarantined)

        # Finding N8: Never pad unrelated "namespace_context" items with `local_only=True` or `quarantined=True` rows
        where, params = self._query_sql_filter(include_private=False, include_quarantined=include_quarantined)
        if seen_ids:
            placeholders = ",".join("?" for _ in seen_ids)
            where += f" AND id NOT IN ({placeholders})"
            params = (*params, *seen_ids)
        with self._read_connect() as conn:
            rows = conn.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE {where} "
                "ORDER BY access_count DESC,confidence DESC,accessed_at DESC,id ASC LIMIT ?",
                (*params, limit - len(suggestions)),
            )
            for raw in rows:
                row = self._deserialize_memory_row(raw)
                mid = row["id"]
                full_content = row["content"]
                suggestions.append({
                    "id": mid, "revision": int(row.get("revision", 1)),
                    "incarnation": row["incarnation"], "store_generation": row["store_generation"],
                    "content": full_content, "snippet": _make_snippet(full_content, max_len=120),
                    "memory_type": row.get("memory_type", "fact"), "tags": row["tags_list"],
                    "confidence": float(row.get("confidence", 1.0)), "ttl_days": row.get("ttl_days"),
                    "local_only": False, "trust_tier": row.get("trust_tier", "user"),
                    "quarantined": bool(row.get("quarantined", False)),
                    "similarity": 0.0, "semantic_similarity": 0.0, "lexical_score": 0.0,
                    "reason": "namespace_context",
                })
        return self.revalidate_query_results(suggestions, include_private=include_private, include_quarantined=include_quarantined)

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
        """Read a bounded result page without constructing a full namespace cache."""
        if not self.db_path.exists() or limit <= 0:
            return []
        if type(limit) is not int or limit > env_int("MESH_MAX_LIST_ITEMS", 10000, minimum=1):
            raise ValueError("List limit exceeds the configured result-page limit.")
        self._ensure_initialized()
        self.flush_telemetry()
        predicates = "namespace=? AND (? OR local_only=0) AND (? OR quarantined=0) AND (NOT ? OR quarantined=1)"
        params: list[Any] = [self.namespace, bool(include_private), bool(include_quarantined), bool(quarantined_only)]
        if memory_type and memory_type.strip():
            predicates += " AND LOWER(memory_type)=?"
            params.append(memory_type.strip().lower())
        requested = {str(value).strip().lower() for value in [tag, *(tags or [])] if value and str(value).strip()}
        now = _utc_now()
        with self._read_connect() as conn:
            cursor = conn.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memories WHERE {predicates} ORDER BY accessed_at DESC,created_at DESC,id ASC",
                params,
            )
            def eligible() -> Iterator[dict[str, Any]]:
                for raw in cursor:
                    row = self._deserialize_memory_row(raw)
                    if requested and not requested.issubset({str(value).lower() for value in row["tags_list"]}):
                        continue
                    if not include_stale or min_confidence > 0.0:
                        confidence, stale = _compute_row_effective_confidence(row, now=now)
                        if (not include_stale and stale) or confidence < min_confidence:
                            continue
                    yield row
            try:
                if order_by == "confidence":
                    return heapq.nlargest(limit, eligible(), key=lambda row: (
                        _compute_row_effective_confidence(row, now=now)[0],
                        int(row.get("access_count") or 0), str(row.get("created_at") or ""), row["id"],
                    ))
                return list(itertools.islice(eligible(), limit))
            finally:
                cursor.close()

    def iter_exportable_memories(
        self,
        include_embeddings: bool = True,
        include_quarantined: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Stream visible export rows, including memories awaiting an embedding."""
        if not self.db_path.exists():
            return
        self._ensure_initialized()
        self._maybe_lazy_flush_telemetry()
        columns = _MEMORY_COLUMNS + (",embedding" if include_embeddings else "")
        with self._read_connect() as conn:
            cursor = conn.execute(
                f"SELECT {columns} FROM memories WHERE namespace=? AND local_only=0 AND (? OR quarantined=0) ORDER BY created_at ASC,id ASC",
                (self.namespace, bool(include_quarantined)),
            )
            try:
                for raw in cursor:
                    values = dict(raw)
                    blob = values.pop("embedding", None)
                    row = self._deserialize_memory_row(values)
                    if include_embeddings:
                        row["embedding"] = _blob_to_vector(blob).tolist() if blob else None
                    yield row
            finally:
                cursor.close()

    def iter_export_records(
        self, include_embeddings: bool = True, include_quarantined: bool = False,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Stream one coherent memory/task/history snapshot without constructing lists."""
        self._ensure_initialized()
        self._maybe_lazy_flush_telemetry()
        columns = _MEMORY_COLUMNS + (",embedding" if include_embeddings else "")
        with self._read_connect() as conn:
            memory_counts = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(local_only=0 AND (? OR quarantined=0)),0),"
                "COALESCE(SUM(local_only=0 AND quarantined=1),0) FROM memories WHERE namespace=?",
                (bool(include_quarantined), self.namespace),
            ).fetchone()
            task_counts = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(local_only=0),0) FROM work_items WHERE namespace=?",
                (self.namespace,),
            ).fetchone()
            yield "counts", {
                "memory_count": int(memory_counts[1]),
                "skipped_local_only": int(memory_counts[0]) - int(memory_counts[1]),
                "skipped_quarantined": 0 if include_quarantined else int(memory_counts[2]),
                "work_items_count": int(task_counts[1]),
                "skipped_local_only_work_items": int(task_counts[0]) - int(task_counts[1]),
            }
            statements = (
                ("memory", f"SELECT {columns} FROM memories WHERE namespace=? AND local_only=0 AND (? OR quarantined=0) ORDER BY created_at ASC,id ASC", (self.namespace, bool(include_quarantined))),
                ("work_item", "SELECT * FROM work_items WHERE namespace=? AND local_only=0 ORDER BY created_at ASC,id ASC", (self.namespace,)),
                ("work_event", "SELECT e.* FROM work_events e JOIN work_items i ON e.item_id=i.id AND e.namespace=i.namespace WHERE e.namespace=? AND i.local_only=0 ORDER BY e.timestamp ASC,e.id ASC", (self.namespace,)),
            )
            for kind, statement, parameters in statements:
                cursor = conn.execute(statement, parameters)
                try:
                    for raw in cursor:
                        if kind == "memory":
                            values = dict(raw)
                            blob = values.pop("embedding", None)
                            row = self._deserialize_memory_row(values)
                            if include_embeddings:
                                row["embedding"] = _blob_to_vector(blob).tolist() if blob else None
                        elif kind == "work_item":
                            row = self._deserialize_work_item_row(raw)
                        else:
                            row = self._deserialize_work_event_row(raw)
                        yield kind, row
                finally:
                    cursor.close()

    def get_exportable_memories(
        self,
        include_embeddings: bool = True,
        include_quarantined: bool = False,
    ) -> list[dict[str, Any]]:
        """Compatibility list API; streaming consumers should use the iterator."""
        return list(self.iter_exportable_memories(include_embeddings, include_quarantined))

    def get_exportable_work_items(
        self,
        include_private: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
        """Return visible work rows and events from one SQL snapshot."""
        if not self.db_path.exists():
            return [], [], 0, 0
        self._ensure_initialized()
        with self._read_connect() as conn:
            counts = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(local_only),0) FROM work_items WHERE namespace=?", (self.namespace,),
            ).fetchone()
            items = [self._deserialize_work_item_row(row) for row in conn.execute(
                "SELECT * FROM work_items WHERE namespace=? AND (? OR local_only=0) ORDER BY created_at ASC,id ASC",
                (self.namespace, bool(include_private)),
            )]
            events = [self._deserialize_work_event_row(row) for row in conn.execute(
                "SELECT e.* FROM work_events e JOIN work_items i ON e.item_id=i.id AND e.namespace=i.namespace "
                "WHERE e.namespace=? AND (? OR i.local_only=0) ORDER BY e.timestamp ASC,e.id ASC",
                (self.namespace, bool(include_private)),
            )]
            return items, events, int(counts[0]), 0 if include_private else int(counts[1])

    def import_work_items(
        self,
        items: list[dict[str, Any]],
        events: list[dict[str, Any]],
        merge_strategy: str = "skip_existing",
        *,
        include_private: bool = True,
        expected_source_agent: Optional[str] = None,
    ) -> dict[str, int]:
        """Import valid records atomically; input errors roll back individual savepoints.

        Storage, encryption, and admission failures abort the enclosing import.
        Existing items and their events are skipped with skip_existing.
        """
        if merge_strategy not in {"skip_existing", "overwrite"}:
            raise ValueError("Invalid merge strategy; use skip_existing or overwrite.")
        if not isinstance(items, list) or not isinstance(events, list):
            raise ValueError("Imported work items and events must be lists.")
        self._ensure_initialized()
        stats = {"imported_items": 0, "skipped_items": 0, "imported_events": 0, "errors": 0}
        imported_ids: set[str] = set()
        valid_statuses = {"todo", "in_progress", "blocked", "done", "cancelled"}

        def identifier(value: Any, default: str) -> str:
            result = value if value is not None else default
            if not isinstance(result, str) or not result.strip() or len(result) > 128 or any(ord(char) < 32 for char in result):
                raise ValueError("Invalid imported agent identifier.")
            return result.strip()

        def timestamp(value: Any, default: Optional[str] = None) -> Optional[str]:
            if value is None or value == "":
                return default
            parsed = _parse_iso_utc(value) if isinstance(value, str) else None
            if parsed is None:
                raise ValueError("Invalid imported timestamp.")
            return parsed.isoformat()

        def guarded_text(value: Any, private: bool, *, required: bool = False) -> tuple[str, bool]:
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ValueError("Imported text must be a string.")
            if not value.strip() and not required:
                return "", private
            result = guard_ingest(value, local_only=private)
            if result.rejected or (required and not result.content.strip()):
                raise ValueError("Imported text rejected by ingestion guard.")
            return result.content.strip(), bool(result.local_only)

        def imported_list(value: Any) -> list[Any]:
            parsed = json.loads(value) if isinstance(value, str) else (value or [])
            if not isinstance(parsed, list):
                raise ValueError("Imported tags and evidence must be lists.")
            return parsed

        with self.write_transaction():
            with self._connect() as conn:
                for record in items:
                    marker = _begin_write(conn, self.namespace, self._store_generation)
                    try:
                        if not isinstance(record, dict):
                            raise ValueError("Imported item must be an object.")
                        raw_id = validate_memory_id(record.get("id"))
                        existing = conn.execute(
                            "SELECT local_only,created_by,short_id FROM work_items WHERE namespace=? AND id=?",
                            (self.namespace, raw_id),
                        ).fetchone()
                        if existing is not None and merge_strategy == "skip_existing":
                            _commit_write(conn, marker)
                            stats["skipped_items"] += 1
                            continue
                        if existing is not None:
                            self._check_mutation_permission(bool(existing[0]), existing[1], include_private, expected_source_agent)
                        requested_private = record.get("local_only", False)
                        if type(requested_private) is not bool:
                            raise ValueError("Imported privacy flag must be a boolean.")
                        private = bool(requested_private or (existing and existing[0]))
                        title, title_private = guarded_text(record.get("title"), private, required=True)
                        description, description_private = guarded_text(record.get("description"), private)
                        summary, summary_private = guarded_text(record.get("summary"), private)
                        clean_tags, tags_private, _ = guard_tags(imported_list(record.get("tags")), local_only=private)
                        evidence = imported_list(record.get("evidence"))
                        if len(evidence) > 100:
                            raise ValueError("At most 100 evidence references are permitted.")
                        clean_evidence: list[str] = []
                        evidence_private = False
                        for reference in evidence:
                            if not isinstance(reference, str) or len(reference) > 2048:
                                raise ValueError("Invalid evidence reference.")
                            clean_reference, reference_private = guarded_text(reference, private)
                            if clean_reference:
                                clean_evidence.append(clean_reference)
                            evidence_private = evidence_private or reference_private
                        private = bool(private or title_private or description_private or summary_private or tags_private or evidence_private)
                        kind = str(record.get("kind") or "task").strip().lower()
                        priority = str(record.get("priority") or "medium").strip().lower()
                        status = str(record.get("status") or "todo").strip().lower()
                        if kind not in {"goal", "task"} or priority not in {"low", "medium", "high", "critical"} or status not in valid_statuses:
                            raise ValueError("Invalid imported work item kind, priority, or status.")
                        created_by = identifier(record.get("created_by"), "mesh-import")
                        self._check_mutation_permission(False, created_by, True, expected_source_agent)
                        parent_id = record.get("parent_id")
                        if parent_id:
                            parent = self._resolve_work_item_locked(conn, validate_memory_id(parent_id))
                            if parent is None or parent["id"] == raw_id:
                                raise ValueError("Imported parent must exist in this namespace.")
                            self._check_mutation_permission(bool(parent["local_only"]), parent["created_by"], include_private, expected_source_agent)
                            parent_id = parent["id"]
                            private = bool(private or parent["local_only"])
                        else:
                            parent_id = None
                        short_id = record.get("short_id") or (existing[2] if existing else None)
                        if short_id:
                            short_id = validate_memory_id(short_id)
                        collision = conn.execute(
                            "SELECT id FROM work_items WHERE namespace=? AND short_id=? AND id<>?",
                            (self.namespace, short_id, raw_id),
                        ).fetchone() if short_id else None
                        if not short_id or collision is not None:
                            short_id = self._generate_short_id_locked(conn, kind)
                        now = _utc_now_iso()
                        values = (
                            short_id, kind, parent_id, self._encrypt_work_text(title), self._encrypt_work_text(description),
                            status, priority, created_by, timestamp(record.get("created_at"), now),
                            timestamp(record.get("started_at")), timestamp(record.get("completed_at")),
                            identifier(record.get("completed_by"), "mesh-import") if record.get("completed_by") is not None else None,
                            self._encrypt_work_text(summary) if summary else None,
                            self._encrypt_work_text(json.dumps(clean_evidence)), self._encrypt_work_text(json.dumps(clean_tags)), int(private),
                        )
                        if existing is None:
                            conn.execute(
                                "INSERT INTO work_items(id,namespace,short_id,kind,parent_id,title,description,status,priority,created_by,"
                                "created_at,started_at,completed_at,completed_by,summary,evidence,tags,local_only) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (raw_id, self.namespace, *values),
                            )
                        else:
                            conn.execute(
                                "UPDATE work_items SET short_id=?,kind=?,parent_id=?,title=?,description=?,status=?,priority=?,created_by=?,"
                                "created_at=?,started_at=?,completed_at=?,completed_by=?,summary=?,evidence=?,tags=?,local_only=? WHERE id=? AND namespace=?",
                                (*values, raw_id, self.namespace),
                            )
                        self._record_mutation_in_tx(conn, "task_import", raw_id)
                        _commit_write(conn, marker)
                        imported_ids.add(raw_id)
                        stats["imported_items"] += 1
                    except (ValueError, TypeError, sqlite3.IntegrityError):
                        _rollback_write(conn, marker)
                        stats["errors"] += 1

                for record in events:
                    marker = _begin_write(conn, self.namespace, self._store_generation)
                    try:
                        if not isinstance(record, dict):
                            raise ValueError("Imported event must be an object.")
                        item_id = validate_memory_id(record.get("item_id"))
                        if merge_strategy == "skip_existing" and item_id not in imported_ids:
                            _commit_write(conn, marker)
                            continue
                        item = conn.execute(
                            "SELECT local_only,created_by FROM work_items WHERE namespace=? AND id=?",
                            (self.namespace, item_id),
                        ).fetchone()
                        if item is None:
                            raise ValueError("Imported event target does not exist.")
                        self._check_mutation_permission(bool(item[0]), item[1], include_private, expected_source_agent)
                        note, note_private = guarded_text(record.get("note"), bool(item[0]))
                        event_type = record.get("event_type") or "status_change"
                        if not isinstance(event_type, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", event_type):
                            raise ValueError("Invalid imported event type.")
                        old_status, new_status = record.get("old_status"), record.get("new_status")
                        if any(value is not None and value not in valid_statuses for value in (old_status, new_status)):
                            raise ValueError("Invalid imported event status.")
                        agent = identifier(record.get("agent_id"), "mesh-import")
                        self._check_mutation_permission(False, agent, True, expected_source_agent)
                        when = timestamp(record.get("timestamp"), _utc_now_iso())
                        previous = conn.execute(
                            "SELECT note FROM work_events WHERE namespace=? AND item_id=? AND event_type=? AND agent_id=? "
                            "AND timestamp=? AND old_status IS ? AND new_status IS ?",
                            (self.namespace, item_id, event_type, agent, when, old_status, new_status),
                        )
                        duplicate = any((decrypt_text_if_encrypted(row[0]) or "") == note for row in previous)
                        previous.close()
                        if duplicate:
                            _commit_write(conn, marker)
                            continue
                        if note_private and not item[0]:
                            conn.execute("UPDATE work_items SET local_only=1 WHERE namespace=? AND id=?", (self.namespace, item_id))
                        conn.execute(
                            "INSERT INTO work_events(item_id,namespace,event_type,old_status,new_status,note,agent_id,timestamp) VALUES(?,?,?,?,?,?,?,?)",
                            (item_id, self.namespace, event_type, old_status, new_status, self._encrypt_work_text(note) if note else None, agent, when),
                        )
                        self._record_mutation_in_tx(conn, "task_event_import", item_id)
                        _commit_write(conn, marker)
                        stats["imported_events"] += 1
                    except (ValueError, TypeError, sqlite3.IntegrityError):
                        _rollback_write(conn, marker)
                        stats["errors"] += 1
            self._refresh_cache_stamp_after_non_memory_write()
        return stats

    def delete(
        self, memory_id: str, *, include_private: bool = True,
        expected_source_agent: Optional[str] = None,
        expected_revision: Optional[int] = None,
        expected_incarnation: Optional[str] = None,
        expected_store_generation: Optional[str] = None,
    ) -> bool:
        """
        Delete a memory by ID, removing its FTS5 entry by `rowid`, scrubbing contradiction
        and query records referencing it, and updating the RAM cache in-place.
        """
        clean_id = (memory_id or "").strip()
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError("expected_revision must be a positive integer.")
        if expected_incarnation is not None:
            _validate_incarnation(expected_incarnation, "expected_incarnation")
        if expected_store_generation is not None:
            _validate_incarnation(expected_store_generation, "expected_store_generation")
            if expected_store_generation != self.store_generation:
                raise StaleStoreError("The requested database generation changed; refresh and retry.")
        if clean_id:
            self._assert_instance_file()
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

        after_commit_actions: list[Callable[[], Any]] = []
        with namespace_write_lock(self.namespace, data_root=self.db_path.parent):
            with self._cache_lock:
                synced_cache = self._is_cache_synced_locked()
                if synced_cache is not None and clean_id in synced_cache.rows_by_id:
                    purged_content = synced_cache.rows_by_id[clean_id].get("content")

                new_cv: Optional[int] = None
                with self._connect() as conn:
                    tx_marker = _begin_write(conn, self.namespace, self._store_generation)
                    try:
                        row = conn.execute(
                            "SELECT rowid, content, local_only, source_agent, revision, incarnation FROM memories WHERE id = ? AND namespace = ?",
                            (clean_id, self.namespace),
                        ).fetchone()
                        if row is not None:
                            self._check_mutation_permission(
                                bool(row[2]), row[3], include_private, expected_source_agent,
                            )
                            if expected_revision is not None and int(row[4]) != expected_revision:
                                raise ValueError("Memory revision changed; no mutation was committed.")
                            if expected_incarnation is not None and row[5] != expected_incarnation:
                                raise ValueError("Memory incarnation changed; no mutation was committed.")
                            rowid = int(row[0])
                            if purged_content is None:
                                purged_content = decrypt_text_if_encrypted(str(row[1] or ""))
                            conn.execute("DELETE FROM memory_passages WHERE namespace=? AND parent_id=?", (self.namespace, clean_id))
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
                            conn.execute(
                                "DELETE FROM embedding_jobs WHERE namespace=? AND memory_id=?",
                                (self.namespace, clean_id),
                            )
                            self._record_mutation_in_tx(conn, "delete", clean_id, int(row[4]))
                            if purged_content and purged_content.strip():
                                _scrub_queries_in_tx(conn, self.namespace, purged_content)
                                _scrub_pending_query_telemetry(self.namespace, purged_content, data_root=self.db_path.parent)
                            new_cv = self._bump_content_version_in_tx(conn)
                        after_commit_actions = _commit_write(conn, tx_marker)
                    except Exception:
                        if conn.in_transaction:
                            _rollback_write(conn, tx_marker)
                        raise

                if deleted:
                    if self._owns_write_transaction():
                        self._queue_cache_delta("delete", clean_id, None, None, new_cv)
                    elif synced_cache is not None:
                        with synced_cache.write_snapshot():
                            synced_cache.delete_vector(clean_id)
                            self._refresh_stamps_after_write_locked(synced_cache, content_version=new_cv)
                    else:
                        self._invalidate_cache()

        self._run_after_commit_actions(after_commit_actions)
        if deleted:
            purge_memory_from_audit(
                namespace=self.namespace,
                memory_id=clean_id,
                content=purged_content,
            )
        return deleted

    def count(self, include_private: bool = True, include_quarantined: bool = True) -> int:
        if not self.db_path.exists():
            return 0
        self._ensure_initialized()
        with self._read_connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE namespace=? AND (? OR local_only=0) AND (? OR quarantined=0)",
                (self.namespace, bool(include_private), bool(include_quarantined)),
            ).fetchone()
            return int(row[0])

    def count_matching(
        self,
        memory_type: Optional[str] = None,
        tag: Optional[str] = None,
        tags: Optional[list[str]] = None,
        include_private: bool = True,
        source_agent: Optional[str] = None,
        include_quarantined: bool = True,
    ) -> int:
        """Count in SQL; stream only tag values when encrypted tag filtering is needed."""
        if not self.db_path.exists():
            return 0
        self._ensure_initialized()
        self._maybe_lazy_flush_telemetry()
        requested = {str(value).strip().lower() for value in [tag, *(tags or [])] if value and str(value).strip()}
        type_filter = str(memory_type).strip().lower() if memory_type else None
        agent_filter = str(source_agent).strip() if source_agent else None
        predicates = "namespace=? AND (? OR local_only=0) AND (? OR quarantined=0)"
        params: list[Any] = [self.namespace, bool(include_private), bool(include_quarantined)]
        if type_filter:
            predicates += " AND LOWER(memory_type)=?"
            params.append(type_filter)
        if agent_filter:
            predicates += " AND source_agent=?"
            params.append(agent_filter)
        with self._read_connect() as conn:
            if not requested:
                return int(conn.execute(f"SELECT COUNT(*) FROM memories WHERE {predicates}", params).fetchone()[0])
            cursor = conn.execute(f"SELECT tags FROM memories WHERE {predicates}", params)
            try:
                return sum(requested.issubset({str(value).lower() for value in _parse_tags_json(decrypt_text_if_encrypted(row[0] or "[]"))}) for row in cursor)
            finally:
                cursor.close()

    def clear_namespace(self) -> None:
        """
        Delete all memories, FTS entries, queries, contradictions, AND audit_log entries
        in this namespace (Finding S12).
        """
        if not self.db_path.exists():
            clear_audit_log(self.namespace)
            return
        self.flush_access_tracking()
        with namespace_write_lock(self.namespace, data_root=self.db_path.parent):
            with _ACCESS_TRACKING_LOCK:
                resolved = self.db_path.resolve()
                buf = _ACCESS_TRACKING_BUFFERS.get(resolved)
                if buf is not None and buf.namespace == self.namespace:
                    _ACCESS_TRACKING_BUFFERS.pop(resolved, None)
            with self._cache_lock:
                with self._connect() as conn:
                    tx_marker = _begin_write(conn, self.namespace, self._store_generation)
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
                        conn.execute("DELETE FROM memory_passages WHERE namespace = ?", (self.namespace,))
                        conn.execute("DELETE FROM memories WHERE namespace = ?", (self.namespace,))
                        conn.execute("DELETE FROM embedding_jobs WHERE namespace = ?", (self.namespace,))
                        conn.execute("DELETE FROM queries WHERE namespace = ?", (self.namespace,))
                        conn.execute("DELETE FROM contradictions WHERE namespace = ?", (self.namespace,))
                        self._record_mutation_in_tx(conn, "clear")
                        self._bump_content_version_in_tx(conn)
                        _commit_write(conn, tx_marker)
                    except Exception:
                        if conn.in_transaction:
                            _rollback_write(conn, tx_marker)
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
        *, expected_incarnations: Optional[dict[str, str]] = None,
    ) -> None:
        """Publish synchronized access statistics and admit bounded loss-tolerant writes."""
        global _ACCESS_TRACKING_DROPPED_ENTRIES
        self._assert_instance_file()
        if not self.db_path.exists():
            if not self.create:
                return
            self._ensure_initialized()
        valid_ids = [memory_id for memory_id in dict.fromkeys(memory_ids) if isinstance(memory_id, str) and _VALID_MEMORY_ID_RE.fullmatch(memory_id)]
        incarnations: dict[str, str] = {}
        if expected_incarnations is not None:
            if not isinstance(expected_incarnations, dict):
                raise ValueError("expected_incarnations must map memory IDs to incarnation tokens.")
            for memory_id in valid_ids:
                value = expected_incarnations.get(memory_id)
                if value is not None:
                    incarnations[memory_id] = _validate_incarnation(value, "expected_incarnation")
        elif valid_ids:
            # ID-only callers resolve one bounded reader snapshot. Recall callers
            # already have validated tokens and can forward them without disk I/O.
            with self._read_connect() as conn:
                for offset in range(0, len(valid_ids), 512):
                    block = valid_ids[offset:offset + 512]
                    placeholders = ",".join("?" for _ in block)
                    rows = conn.execute(
                        f"SELECT id,incarnation FROM memories WHERE namespace=? AND id IN ({placeholders})",
                        (self.namespace, *block),
                    )
                    incarnations.update((row["id"], row["incarnation"]) for row in rows)
        valid_ids = [memory_id for memory_id in valid_ids if memory_id in incarnations]
        now = _utc_now_iso()
        raw_query = (query_text or "").strip()[:512]
        is_private = bool(private_query or (raw_query and is_credential(raw_query)))
        log_text = os.environ.get("MESH_LOG_QUERY_TEXT", "1").strip().lower() not in {"0", "false", "no"}
        clean_query = redact_secrets_local(raw_query) if log_text and raw_query else ""
        resolved = self._resolved_db_path()
        with _CACHE_LOCK:
            cached = _VECTOR_CACHE.get((self.db_path, self.namespace))
        if cached is not None:
            with cached.write_snapshot():
                if cached._valid:
                    for memory_id in valid_ids:
                        row = cached.rows_by_id.get(memory_id)
                        if row is not None and row.get("incarnation") == incarnations[memory_id] and row.get("store_generation") == self.store_generation:
                            row["access_count"] = int(row.get("access_count") or 0) + 1
                            row["accessed_at"] = now
        overflow_path: Optional[Path] = None
        with _ACCESS_TRACKING_LOCK:
            buf = _ACCESS_TRACKING_BUFFERS.get(resolved)
            if buf is not None and buf.store_generation not in (None, self.store_generation):
                _ACCESS_TRACKING_DROPPED_ENTRIES += len(buf.access_increments) + len(buf.query_rows)
                _ACCESS_TRACKING_BUFFERS.pop(resolved)
                buf = None
            pending, pending_bytes = _tracking_charge_locked()
            max_entries = env_int("MESH_MAX_ACCESS_TRACKING_ENTRIES", 10000, minimum=1)
            max_bytes = env_int("MESH_MAX_ACCESS_TRACKING_BYTES", 8 * 1024 * 1024, minimum=1024)
            if buf is None:
                if len(_ACCESS_TRACKING_BUFFERS) >= _get_max_pool_size():
                    overflow_path = next(iter(_ACCESS_TRACKING_BUFFERS))
                else:
                    candidate = _AccessTrackingBuffer(resolved, self.namespace, self.store_generation)
                    if pending_bytes + candidate.byte_charge <= max_bytes:
                        buf = candidate
                        pending_bytes += candidate.byte_charge
                        _ACCESS_TRACKING_BUFFERS[resolved] = buf
            if buf is None:
                _ACCESS_TRACKING_DROPPED_ENTRIES += len(valid_ids) + int(bool(clean_query or not log_text))
            else:
                if buf.event_count == 0:
                    buf.first_event_ts = time.monotonic()
                buf.event_count += 1
                for memory_id in valid_ids:
                    previous = buf.access_increments.get(memory_id)
                    if previous is not None and buf.access_incarnations.get(memory_id) != incarnations[memory_id]:
                        _ACCESS_TRACKING_DROPPED_ENTRIES += 1
                        buf.access_increments.pop(memory_id)
                        buf.access_incarnations.pop(memory_id, None)
                        retired_charge = _access_entry_bytes(memory_id, previous[1])
                        buf.byte_charge -= retired_charge
                        pending -= 1
                        pending_bytes -= retired_charge
                        previous = None
                    if previous is not None:
                        buf.access_increments[memory_id] = (previous[0] + 1, now)
                        continue
                    charge = _access_entry_bytes(memory_id, now)
                    if pending >= max_entries or pending_bytes + charge > max_bytes:
                        _ACCESS_TRACKING_DROPPED_ENTRIES += 1
                        continue
                    buf.access_increments[memory_id] = (1, now)
                    buf.access_incarnations[memory_id] = incarnations[memory_id]
                    buf.byte_charge += charge
                    pending += 1
                    pending_bytes += charge
                if clean_query or not log_text:
                    query_row = (
                        str(uuid.uuid4()), self.namespace, clean_query,
                        str(agent_id or "unknown")[:128], float(best_similarity) if best_similarity is not None else None,
                        int(result_count), now, int(is_private),
                    )
                    charge = _query_entry_bytes(query_row)
                    if pending < max_entries and pending_bytes + charge <= max_bytes:
                        buf.query_rows.append(query_row)
                        buf.byte_charge += charge
                    else:
                        _ACCESS_TRACKING_DROPPED_ENTRIES += 1
        if overflow_path is not None:
            _flush_access_tracking_for_db_path(overflow_path)
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

        with namespace_write_lock(self.namespace, data_root=self.db_path.parent):
            with self._cache_lock:
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
                    tx_marker = _begin_write(conn, self.namespace, self._store_generation)
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
                        _commit_write(conn, tx_marker)
                    except Exception:
                        if conn.in_transaction:
                            _rollback_write(conn, tx_marker)
                        raise
            self._refresh_cache_stamp_after_non_memory_write()
        return cid

    def get_contradictions(self, resolved: bool = False) -> list[dict[str, Any]]:
        """Return unresolved (or resolved) contradictions for this namespace, decrypting fields if needed."""
        if not self.db_path.exists():
            return []
        res_flag = 1 if resolved else 0
        with self._cache_lock:
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

        with namespace_write_lock(self.namespace, data_root=self.db_path.parent):
            with self._cache_lock:
                with self._connect() as conn:
                    tx_marker = _begin_write(conn, self.namespace, self._store_generation)
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
                        _commit_write(conn, tx_marker)
                        updated = cur.rowcount > 0
                    except Exception:
                        if conn.in_transaction:
                            _rollback_write(conn, tx_marker)
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
        with self._cache_lock:
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

    @staticmethod
    def _encrypt_work_text(value: Optional[str], *, key: Optional[bytes] = None) -> Optional[str]:
        if value is None or is_encrypted_text(value):
            return value
        if key is None and not is_encryption_enabled():
            return value
        return encrypt_text_for_storage(value, key=key)

    @staticmethod
    def _encrypt_passages_in_tx(conn: sqlite3.Connection, *, key: Optional[bytes] = None) -> bool:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_passages'").fetchone() is None:
            return False
        changed = False
        cursor = conn.execute("SELECT parent_id,ordinal,embedding FROM memory_passages")
        try:
            while batch := cursor.fetchmany(64):
                for parent_id, ordinal, blob in batch:
                    if not is_encrypted_blob(blob):
                        encrypted = encrypt_blob_for_storage(blob, key=key)
                        if not encrypted or not is_encrypted_blob(encrypted):
                            raise RuntimeError("Passage vector encryption failed.")
                        conn.execute("UPDATE memory_passages SET embedding=? WHERE parent_id=? AND ordinal=?", (encrypted, parent_id, ordinal))
                        changed = True
        finally:
            cursor.close()
        return changed

    @staticmethod
    def _encrypt_work_tables_in_tx(conn: sqlite3.Connection, *, key: Optional[bytes] = None) -> bool:
        changed = False
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, fields in (("work_items", ("title", "description", "summary", "tags", "evidence")), ("work_events", ("note",))):
            if table not in tables:
                continue
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            selected_fields = [field for field in fields if field in columns]
            if not selected_fields:
                continue
            cursor = conn.execute(f"SELECT id,{','.join(selected_fields)} FROM {table}")
            try:
                while batch := cursor.fetchmany(64):
                    updates = []
                    for row in batch:
                        values = [MeshStore._encrypt_work_text(value, key=key) for value in row[1:]]
                        if values != list(row[1:]):
                            updates.append([*values, row[0]])
                    if updates:
                        changed = True
                        assignments = ",".join(f"{field}=?" for field in selected_fields)
                        conn.executemany(f"UPDATE {table} SET {assignments} WHERE id=?", updates)
            finally:
                cursor.close()
        return changed

    def _finish_work_encryption_cleanup(self) -> None:
        """Retry physical cleanup until legacy plaintext no longer occupies live files."""
        close_store_readers(self.db_path)
        with self._connect() as conn:
            if conn.in_transaction:
                raise RuntimeError("Encrypted task cleanup requires the enclosing transaction to finish.")
            for vacuum in (True, False):
                result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if int(result[0]) != 0:
                    raise RuntimeError("Encrypted task upgrade committed; physical cleanup is waiting for active readers. Retry after they finish.")
                if vacuum:
                    conn.execute("VACUUM")
            marker = _begin_write(conn, self.namespace, self._store_generation)
            try:
                conn.execute("UPDATE mesh_meta SET value='0' WHERE key='work_encryption_cleanup_pending'")
                _commit_write(conn, marker)
            except BaseException:
                _rollback_write(conn, marker)
                raise
        secure_file_permissions(self.db_path)

    def _resolve_work_item_locked(self, conn: sqlite3.Connection, item_id: str) -> Optional[dict[str, Any]]:
        clean = validate_memory_id(item_id)
        row = conn.execute("SELECT * FROM work_items WHERE namespace=? AND id=?", (self.namespace, clean)).fetchone()
        if row is not None:
            return dict(row)
        rows = conn.execute(
            "SELECT * FROM work_items WHERE namespace=? AND short_id=? COLLATE NOCASE LIMIT 2", (self.namespace, clean),
        ).fetchall()
        if not rows and len(clean) >= 4:
            escaped = clean.replace("_", "\\_").replace("%", "\\%")
            rows = conn.execute(
                "SELECT * FROM work_items WHERE namespace=? AND id LIKE ? ESCAPE '\\' LIMIT 2", (self.namespace, escaped + "%"),
            ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            raise ValueError("Ambiguous work item ID; provide the complete ID.")
        return None

    def _not_found_error_message(self, conn: sqlite3.Connection, item_id: str) -> str:
        rows = conn.execute(
            "SELECT short_id FROM work_items WHERE namespace = ? AND local_only=0 ORDER BY created_at DESC LIMIT 5",
            (self.namespace,),
        ).fetchall()
        if not rows:
            return (
                f"Work item '{item_id}' not found in namespace '{self.namespace}'. "
                "No work items exist yet in this namespace; use mesh_task_create to create one first."
            )
        available = [str(r[0]) for r in rows]
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
        *,
        include_private: bool = True,
        expected_source_agent: Optional[str] = None,
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
        self._check_mutation_permission(False, created_by, True, expected_source_agent)

        item_uuid = uuid.uuid4().hex
        now_iso = _utc_now_iso()

        with self.write_transaction():
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                resolved_parent: Optional[str] = None
                if parent_id:
                    parent_row = self._resolve_work_item_locked(conn, parent_id)
                    if not parent_row:
                        raise ValueError(f"Parent {self._not_found_error_message(conn, parent_id)}")
                    self._check_mutation_permission(
                        bool(parent_row["local_only"]), parent_row["created_by"],
                        include_private, expected_source_agent,
                    )
                    resolved_parent = parent_row["id"]
                    is_priv = bool(is_priv or parent_row["local_only"])

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
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'todo', ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        item_uuid,
                        short_id,
                        self.namespace,
                        clean_kind,
                        resolved_parent,
                        self._encrypt_work_text(clean_title),
                        self._encrypt_work_text(clean_desc),
                        clean_priority,
                        str(created_by or "agent"),
                        now_iso,
                        self._encrypt_work_text("[]"),
                        self._encrypt_work_text(json.dumps(clean_tags)),
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
                        self._encrypt_work_text(f"Created {clean_kind}: {clean_title}"),
                        str(created_by or "agent"),
                        now_iso,
                    ),
                )
                row = conn.execute("SELECT * FROM work_items WHERE id = ?", (item_uuid,)).fetchone()
                item_dict = self._deserialize_work_item_row(row)
                self._record_mutation_in_tx(conn, "task_create", item_uuid)
            self._refresh_cache_stamp_after_non_memory_write()
            return self._mutation_result(item_dict, include_private)

    def update_work_item(
        self,
        item_id: str,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        note: Optional[str] = None,
        agent_id: str = "agent",
        *,
        include_private: bool = True,
        expected_source_agent: Optional[str] = None,
    ) -> dict[str, Any]:
        self._ensure_initialized()
        now_iso = _utc_now_iso()
        with self.write_transaction():
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                item = self._resolve_work_item_locked(conn, item_id)
                if not item:
                    raise ValueError(self._not_found_error_message(conn, item_id))
                self._check_mutation_permission(
                    bool(item["local_only"]), item["created_by"], include_private, expected_source_agent,
                )

                full_id = item["id"]
                updates: list[str] = []
                params: list[Any] = []
                old_status = item["status"]
                new_status = old_status
                event_type = "updated"
                is_priv = bool(item["local_only"])

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
                    params.append(self._encrypt_work_text(guard_t.content.strip()))
                    if guard_t.local_only and not item["local_only"]:
                        updates.append("local_only = 1")
                        is_priv = True

                if description is not None:
                    if str(description).strip():
                        guard_d = guard_ingest(description, local_only=bool(item["local_only"]))
                        if guard_d.rejected:
                            raise ValueError(f"Description rejected: {guard_d.reason}")
                        updates.append("description = ?")
                        params.append(self._encrypt_work_text(guard_d.content.strip()))
                        if guard_d.local_only and not item["local_only"]:
                            updates.append("local_only = 1")
                            is_priv = True
                    else:
                        updates.append("description = ?")
                        params.append(self._encrypt_work_text(""))

                clean_note = None
                if note is not None and str(note).strip():
                    guard_n = guard_ingest(note, local_only=is_priv)
                    if guard_n.rejected:
                        raise ValueError("Work event note rejected by ingestion guard.")
                    clean_note = guard_n.content.strip()
                    if guard_n.local_only and not item["local_only"]:
                        updates.append("local_only = 1")
                        is_priv = True


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
                        self._encrypt_work_text(clean_note),
                        str(agent_id or "agent"),
                        now_iso,
                    ),
                )
                updated_row = conn.execute("SELECT * FROM work_items WHERE id = ?", (full_id,)).fetchone()
                res = self._deserialize_work_item_row(updated_row)
                self._record_mutation_in_tx(conn, "task_update", full_id)
            self._refresh_cache_stamp_after_non_memory_write()
            return self._mutation_result(res, include_private)

    def complete_work_item(
        self,
        item_id: str,
        summary: str,
        evidence: Optional[list[str]] = None,
        note: Optional[str] = None,
        agent_id: str = "agent",
        *,
        include_private: bool = True,
        expected_source_agent: Optional[str] = None,
    ) -> dict[str, Any]:
        self._ensure_initialized()
        clean_sum = (summary or "").strip()
        if not clean_sum:
            raise ValueError("A detailed summary of what was completed is required to mark a work item done.")

        guard_s = guard_ingest(clean_sum)
        if guard_s.rejected:
            raise ValueError(f"Summary rejected: {guard_s.reason}")

        now_iso = _utc_now_iso()
        if evidence is not None and not isinstance(evidence, list):
            raise ValueError("Evidence must be a list of strings.")
        if len(evidence or []) > 100:
            raise ValueError("At most 100 evidence references are permitted.")
        valid_ev: list[str] = []
        evidence_private = False
        for reference in evidence or []:
            if not isinstance(reference, str) or len(reference) > 2048:
                raise ValueError("Evidence references must be strings of at most 2048 characters.")
            if not reference.strip():
                continue
            guarded_reference = guard_ingest(reference)
            if guarded_reference.rejected:
                raise ValueError("Evidence reference rejected by ingestion guard.")
            valid_ev.append(guarded_reference.content.strip())
            evidence_private = evidence_private or guarded_reference.local_only

        clean_note = None
        guard_n = None
        if note is not None and str(note).strip():
            guard_n = guard_ingest(note)
            if guard_n.rejected:
                raise ValueError("Work event note rejected by ingestion guard.")
            clean_note = guard_n.content.strip()
        else:
            clean_note = guard_s.content.strip()

        with self.write_transaction():
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                item = self._resolve_work_item_locked(conn, item_id)
                if not item:
                    raise ValueError(self._not_found_error_message(conn, item_id))
                self._check_mutation_permission(
                    bool(item["local_only"]), item["created_by"], include_private, expected_source_agent,
                )

                full_id = item["id"]
                old_status = item["status"]
                is_priv = bool(item["local_only"] or guard_s.local_only or evidence_private or (guard_n and guard_n.local_only))
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
                        self._encrypt_work_text(guard_s.content.strip()),
                        self._encrypt_work_text(json.dumps(valid_ev)),
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
                        self._encrypt_work_text(clean_note),
                        str(agent_id or "agent"),
                        now_iso,
                    ),
                )
                updated_row = conn.execute("SELECT * FROM work_items WHERE id = ?", (full_id,)).fetchone()
                res = self._deserialize_work_item_row(updated_row)
                self._record_mutation_in_tx(conn, "task_complete", full_id)
            self._refresh_cache_stamp_after_non_memory_write()
            return self._mutation_result(res, include_private)

    def _deserialize_work_item_row(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        d["title"] = decrypt_text_if_encrypted(d.get("title"))
        d["description"] = decrypt_text_if_encrypted(d.get("description") or "")
        if d.get("summary"):
            d["summary"] = decrypt_text_if_encrypted(d["summary"])
        d["tags"] = json.loads(decrypt_text_if_encrypted(d.get("tags")) or "[]")
        d["evidence"] = json.loads(decrypt_text_if_encrypted(d.get("evidence")) or "[]")
        d["local_only"] = bool(d.get("local_only"))
        return d

    def _deserialize_work_event_row(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        if d.get("note"):
            d["note"] = decrypt_text_if_encrypted(d["note"])
        return d

    def get_work_item(self, item_id: str, include_private: bool = True) -> Optional[dict[str, Any]]:
        self._ensure_initialized()
        with self._read_connect() as conn:
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
        with self._read_connect() as conn:
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

    def get_work_events(
        self, item_id: Optional[str] = None, limit: int = 50, *, include_private: bool = False,
    ) -> list[dict[str, Any]]:
        self._ensure_initialized()
        safe_limit = max(1, min(1000, int(limit or 50)))
        with self._read_connect() as conn:
            conn.row_factory = sqlite3.Row
            query = (
                "SELECT e.* FROM work_events e JOIN work_items w ON w.id=e.item_id AND w.namespace=e.namespace "
                "WHERE e.namespace=?"
            )
            params: list[Any] = [self.namespace]
            if not include_private:
                query += " AND w.local_only=0"
            if item_id:
                target = self._resolve_work_item_locked(conn, item_id)
                target_id = target["id"] if target else item_id
                query += " AND e.item_id = ?"
                params.append(target_id)
            query += " ORDER BY e.timestamp DESC, e.id DESC LIMIT ?"
            params.append(safe_limit)
            return [self._deserialize_work_event_row(r) for r in conn.execute(query, params).fetchall()]

    def get_work_item_stats(self, include_private: bool = False) -> dict[str, Any]:
        self._ensure_initialized()
        with self._read_connect() as conn:
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
                WHERE namespace = ? AND (? OR local_only=0)
                """,
                (self.namespace, bool(include_private)),
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
        *,
        max_facts: int = 20,
        fact_offset: int = 0,
    ) -> dict[str, Any]:
        """Select current facts and work in one read snapshot without loading vectors.

        The token limit budgets Markdown; the complete serialized envelope has a
        separate estimate. Both estimates use characters/4, not a model tokenizer.
        """
        from .brief import BriefTextBudget, finalize_project_brief, validate_brief_options

        validate_brief_options(max_tokens, max_facts, fact_offset)
        self._ensure_initialized()
        now = _utc_now()
        privacy = "" if include_private else " AND local_only=0"
        visible = f"namespace=? AND quarantined=0{privacy}"
        effective = (
            "ROUND(confidence * CASE WHEN ttl_days>0 AND "
            "julianday(COALESCE(decay_start_at,created_at)) IS NOT NULL THEN "
            "MAX(0.0,MIN(1.0,1.0-MAX(0.0,julianday(?)-"
            "julianday(COALESCE(decay_start_at,created_at)))/ttl_days)) ELSE 1.0 END,4)"
        )
        current = f"{visible} AND {effective}>=?"
        current_params = (self.namespace, now.isoformat(), STALE_THRESHOLD)
        types = ("decision", "process", "preference", "fact")
        fact_rows: list[dict[str, Any]] = []
        handoff: Optional[dict[str, Any]] = None
        open_items: list[dict[str, Any]] = []
        recent_items: list[dict[str, Any]] = []
        with self._read_connect() as conn:
            conn.row_factory = sqlite3.Row
            owns_read = not conn.in_transaction
            if owns_read:
                conn.execute("BEGIN")
            try:
                total_mems = int(conn.execute(
                    f"SELECT COUNT(*) FROM memories WHERE {visible}", (self.namespace,),
                ).fetchone()[0])
                counts = {kind: 0 for kind in types}
                for row in conn.execute(
                    f"SELECT memory_type,COUNT(*) AS total FROM memories WHERE {current} "
                    "AND memory_type IN ('decision','process','preference','fact') GROUP BY memory_type",
                    current_params,
                ).fetchall():
                    counts[row["memory_type"]] = int(row["total"])
                facts_available = sum(counts.values())
                content_version = self._read_content_version_locked(conn) or 0
                event_revision = int(conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM work_events WHERE namespace=?", (self.namespace,),
                ).fetchone()[0])
                contradiction_privacy = "" if include_private else " AND n.local_only=0 AND e.local_only=0"
                contradictions_count = int(conn.execute(
                    "SELECT COUNT(*) FROM contradictions c JOIN memories n ON n.id=c.new_memory_id "
                    "AND n.namespace=c.namespace JOIN memories e ON e.id=c.existing_memory_id "
                    "AND e.namespace=c.namespace WHERE c.namespace=? AND c.resolved=0 "
                    f"AND n.quarantined=0 AND e.quarantined=0{contradiction_privacy}",
                    (self.namespace,),
                ).fetchone()[0])

                # Skip complete rounds natively; pagination never decrypts skipped bodies.
                target = min(max_facts, max(0, facts_available - fact_offset))
                if target:
                    lo, hi = 0, max(counts.values()) + 1
                    while lo + 1 < hi:
                        level = (lo + hi) // 2
                        if sum(min(count, level) for count in counts.values()) <= fact_offset:
                            lo = level
                        else:
                            hi = level
                    offsets = {kind: min(counts[kind], lo) for kind in types}
                    residual = fact_offset - sum(offsets.values())
                    active_types = [kind for kind in types if offsets[kind] < counts[kind]]
                    for kind in active_types[:residual]:
                        if residual and offsets[kind] < counts[kind]:
                            offsets[kind] += 1
                    ordered_types = active_types[residual:] + active_types[:residual]
                    cursors: list[sqlite3.Cursor] = []
                    try:
                        for kind in ordered_types:
                            cursors.append(conn.execute(
                                f"SELECT id,memory_type,revision,updated_at,created_at FROM memories WHERE {current} "
                                "AND memory_type=? ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
                                (*current_params, kind, target, offsets[kind]),
                            ))
                        while len(fact_rows) < target:
                            progressed = False
                            for cursor in cursors:
                                row = cursor.fetchone()
                                if row is not None:
                                    fact_rows.append(dict(row))
                                    progressed = True
                                    if len(fact_rows) == target:
                                        break
                            if not progressed:
                                break
                    finally:
                        for cursor in cursors:
                            cursor.close()
                    selected_ids = [row["id"] for row in fact_rows]
                    placeholders = ",".join("?" for _ in selected_ids)
                    bodies = {row["id"]: decrypt_text_if_encrypted(row["content"]) for row in conn.execute(
                        f"SELECT id,content FROM memories WHERE namespace=? AND id IN ({placeholders})",
                        (self.namespace, *selected_ids),
                    ).fetchall()}
                    for row in fact_rows:
                        row["content"] = bodies[row["id"]]

                # Only tag metadata is scanned when encryption hides the handoff tag.
                cursor = conn.execute(
                    f"SELECT id,tags,revision,updated_at FROM memories WHERE {current} AND memory_type='context' "
                    "ORDER BY updated_at DESC,id", current_params,
                )
                try:
                    for row in cursor:
                        tags = _parse_tags_json(decrypt_text_if_encrypted(row["tags"]))
                        if "handoff" in {tag.casefold() for tag in tags}:
                            handoff = dict(row)
                            body = conn.execute(
                                "SELECT content FROM memories WHERE namespace=? AND id=?",
                                (self.namespace, row["id"]),
                            ).fetchone()
                            handoff["content"] = decrypt_text_if_encrypted(body[0])
                            break
                finally:
                    cursor.close()

                work_visible = f"namespace=?{privacy}"
                open_count = recent_count = 0
                if include_work_items:
                    open_count = int(conn.execute(
                        f"SELECT COUNT(*) FROM work_items WHERE {work_visible} AND status IN ('todo','in_progress','blocked')",
                        (self.namespace,),
                    ).fetchone()[0])
                    recent_cutoff = (now - timedelta(days=2)).isoformat()
                    recent_count = int(conn.execute(
                        f"SELECT COUNT(*) FROM work_items WHERE {work_visible} AND status='done' AND completed_at>=?",
                        (self.namespace, recent_cutoff),
                    ).fetchone()[0])
                    open_items = [dict(row) for row in conn.execute(
                        f"SELECT id,short_id,title,status,priority FROM work_items WHERE {work_visible} "
                        "AND status IN ('todo','in_progress','blocked') ORDER BY "
                        "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,created_at DESC,id LIMIT 5",
                        (self.namespace,),
                    ).fetchall()]
                    recent_items = [dict(row) for row in conn.execute(
                        f"SELECT id,short_id,title,summary FROM work_items WHERE {work_visible} "
                        "AND status='done' AND completed_at>=? ORDER BY completed_at DESC,id LIMIT 3",
                        (self.namespace, recent_cutoff),
                    ).fetchall()]
            finally:
                if owns_read:
                    conn.rollback()

        text = BriefTextBudget(max_tokens)
        limit_chars = text.limit_chars
        lines = text.lines
        append_line = text.append_line

        def compact(text: Optional[str], limit: int) -> str:
            value = " ".join(str(text or "").split())
            return value if len(value) <= limit else value[:max(0, limit - 1)] + "?"

        append_line(f"# Project Brief: {self.namespace}")
        append_line(f"{total_mems} visible memories | {open_count} open work items | {contradictions_count} pending contradictions")
        included_ids: list[str] = []
        handoff_id: Optional[str] = None
        work_included = recent_included = 0
        active = bool(total_mems or open_count or recent_count)
        if not active:
            append_line("Namespace state: new_or_empty. Record durable facts and create work items when needed.")
        else:
            notice_reserve = min(128, max(0, limit_chars - len("\n".join(lines)))) if facts_available else 0
            remaining = max(0, limit_chars - len("\n".join(lines)) - notice_reserve - 24)
            fact_capacity = int(remaining * 0.75) if handoff or open_items or recent_items else remaining
            row_chars = min(512, max(160, fact_capacity // max(1, len(fact_rows))))
            facts_header = "\n## Current Facts & Decisions"
            if fact_rows and len("\n".join(lines)) + len(facts_header) + row_chars + notice_reserve < limit_chars:
                append_line(facts_header, reserve=notice_reserve)
                facts_start = len("\n".join(lines))
                for row in fact_rows:
                    date = str(row.get("updated_at") or row.get("created_at") or "")[:10]
                    prefix = f"- [{row['memory_type']}] {row['id']}@r{row['revision']} ({date}): "
                    content = compact(row["content"], max(24, row_chars - len(prefix)))
                    hint = ""
                    if row["memory_type"] == "decision" and not re.search(r"\b(because|since|reason|rationale)\b|due to", row["content"], re.IGNORECASE):
                        hint = " *(needs rationale)*"
                    line = prefix + content + hint
                    if len("\n".join(lines)) - facts_start + len(line) + 1 > fact_capacity:
                        break
                    if not append_line(line, reserve=notice_reserve):
                        break
                    included_ids.append(row["id"])
            if handoff:
                line = f"\n## Latest Handoff\n- {handoff['id']}@r{handoff['revision']}: {compact(handoff['content'], 320)}"
                if append_line(line, reserve=notice_reserve):
                    handoff_id = handoff["id"]
            for item in open_items:
                line = f"- {item['short_id']} [{item['status']}, {item['priority']}]: {compact(decrypt_text_if_encrypted(item['title']), 100)}"
                if not work_included and not append_line("\n## Open Goals & Tasks", reserve=notice_reserve + len(line) + 1):
                    break
                if not append_line(line, reserve=notice_reserve):
                    break
                work_included += 1
            for item in recent_items:
                line = f"- {item['short_id']}: {compact(decrypt_text_if_encrypted(item['title']), 80)} ? {compact(decrypt_text_if_encrypted(item['summary']), 120)}"
                if not recent_included and not append_line("\n## Recently Completed (Past 48h)", reserve=notice_reserve + len(line) + 1):
                    break
                if not append_line(line, reserve=notice_reserve):
                    break
                recent_included += 1

        facts_included = len(included_ids)
        next_offset = fact_offset + facts_included if fact_offset + facts_included < facts_available else None
        facts_omitted = max(0, facts_available - facts_included)
        if facts_omitted:
            suffix = f"; continue with fact_offset={next_offset}" if next_offset is not None else ""
            append_line(f"\n*{facts_omitted} facts omitted{suffix}. Increase the budget or use targeted recall.*")
        markdown = text.markdown()
        result = {
            "namespace": self.namespace, "namespace_state": "active" if active else "new_or_empty",
            "total_memories": total_mems, "open_tasks_count": open_count,
            "completed_recent_count": recent_count, "pending_contradictions": contradictions_count,
            "key_facts_count": facts_included, "facts_available": facts_available,
            "facts_included": facts_included, "facts_omitted": facts_omitted,
            "selected_memory_ids": included_ids, "next_fact_offset": next_offset,
            "selection_revision": hashlib.sha256(json.dumps([
                self.store_generation, self.namespace, bool(include_private), content_version,
                event_revision, [counts[kind] for kind in types], total_mems,
                bool(include_work_items), open_count, recent_count,
                [handoff["id"], handoff["revision"]] if handoff else None,
            ], separators=(",", ":")).encode()).hexdigest()[:16], "as_of": now.isoformat(),
            "handoff_memory_id": handoff_id, "open_work_items_included": work_included,
            "completed_recent_included": recent_included, "brief_markdown": markdown,
            "budget_scope": "brief_markdown", "estimated_tokens": (len(markdown) + 3) // 4,
            "payload_estimated_tokens": 0, "untrusted_reference_data": True,
        }
        return finalize_project_brief(result)

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

        with self._read_connect() as conn:
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
                d = self._deserialize_work_item_row(r)
                completed_items.append(d)

            d_rows = conn.execute(
                f"""
                SELECT id, content, tags, created_at, source_agent
                FROM memories
                WHERE namespace = ? AND memory_type = 'decision' AND quarantined = 0 AND created_at >= ? {priv_filter}
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

