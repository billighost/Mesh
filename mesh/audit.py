"""
mesh/audit.py — High-speed, fault-isolated audit trail for all memory reads and writes.
Logs recall(), learn(), update(), and forget() calls with timestamp, agent, query/content, and result.
Protects `local_only` memories via automatic redaction, scrubs historical content (including substrings)
on `update()`, purges deleted memories on `forget()` and `clear_audit_log()`, supports at-rest
encryption, and enforces bounded table, namespace, symlink, and POSIX/Windows file permission controls.

Addresses Findings:
  - S5: Transparent at-rest encryption for `query`, `content`, and `top_result` when enabled
  - S12: Redacts credentials in `log_recall` queries even on 0 results, scrubs substring matches
         on `update()`/`forget()`, and adds `clear_audit_log()` for `Mesh.clear()`
  - N5: Enforces owner-only Windows ACLs (`icacls`) alongside POSIX 0700/0600 permissions
  - N6: Rejects symlinks (`reject_symlink`) on database and WAL/SHM sidecar files
  - N10: Enforces `MESH_MAX_NAMESPACES` cap before creating a new `audit_<ns>.db` file
"""
from __future__ import annotations

import atexit
import os
import re
import sqlite3
import stat
import subprocess
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from .envutil import env_int


def get_mesh_dir() -> Path:
    """Return the active Mesh data directory, respecting MESH_HOME / MESH_DIR env overrides dynamically."""
    override = (os.environ.get("MESH_HOME") or os.environ.get("MESH_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".mesh"


_AUDIT_LOCK = threading.RLock()
_INITIALIZED_AUDIT_DBS: dict[Path, tuple[int, int, int, int]] = {}
_AUDIT_INITIALIZED_FILES: set[Path] = set()
_AUDIT_CONNECTION_POOL: OrderedDict[Path, "_ManagedAuditConnection"] = OrderedDict()
_AUDIT_WRITE_COUNTS: dict[Path, int] = {}
_MAX_AUDIT_POOL_SIZE = 64
_DEFAULT_MAX_NAMESPACES = 256

# Finding N5: Memoize Windows ACL application by resolved path so we don't spawn icacls repeatedly
_WIN_ACL_LOCK = threading.Lock()
_WIN_ACL_APPLIED: set[str] = set()

# Memoize secure_file_permissions and ensure_secure_mesh_dir
_SECURED_PATHS: dict[Path, int] = {}
_SECURED_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Phase 1 – Write-Behind Async Audit Queue
# Audit entries for recall/learn/update/forget/log_audit_event are enqueued
# in _AUDIT_PENDING and written by a single background daemon thread so the hot path
# never waits on a SQLite commit. scrub_* and purge_* remain synchronous.
# ---------------------------------------------------------------------------

_AUDIT_PENDING: dict[str, list[tuple[str, tuple[Any, ...]]]] = {}
_AUDIT_KNOWN_DB_PATHS: set[Path] = set()
_AUDIT_QUEUE_LOCK = threading.Lock()
_AUDIT_FLUSH_LOCK = threading.Lock()
_AUDIT_WAKE_EVENT = threading.Event()
_AUDIT_DRAIN_THREAD: Optional[threading.Thread] = None
_AUDIT_DRAIN_LOCK = threading.Lock()

_VALID_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

REDACTED_CONTENT_PLACEHOLDER = "[REDACTED local_only memory]"
PURGED_CONTENT_PLACEHOLDER = "[PURGED]"
UPDATED_CONTENT_PLACEHOLDER = "[PREVIOUS VERSION SCRUBBED]"
DEFAULT_MAX_AUDIT_ROWS = 10000

_AUDIT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,           -- 'recall', 'learn', 'update', or 'forget'
    agent_id TEXT,
    namespace TEXT,
    query TEXT,                     -- for recalls: the query string
    content TEXT,                   -- for learns/updates: the content stored (redacted if local_only)
    top_result TEXT,                -- for recalls: the top result content (redacted if local_only)
    top_similarity REAL,            -- for recalls: top result similarity
    result_count INTEGER,           -- for recalls: number of results
    memory_id TEXT,                 -- for learns/updates/forgets: the target memory ID
    had_conflict INTEGER DEFAULT 0  -- for learns: 1 if contradiction detected
)
"""

_AUDIT_EVENTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    surface TEXT NOT NULL,          -- 'mcp', 'cli', 'http', 'sdk'
    tool_or_command TEXT NOT NULL,  -- e.g. 'mesh_learn', 'mesh_project_brief', 'mesh brief'
    agent_id TEXT,
    namespace TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    status_code TEXT NOT NULL,      -- 'ok', 'error', 'rejected', etc.
    payload_size_bytes INTEGER DEFAULT 0,
    result_count INTEGER DEFAULT 0
)
"""

_ACTION_FILTER_MAP = {
    "read": "recall",
    "write": "learn",
    "recall": "recall",
    "learn": "learn",
    "update": "update",
    "forget": "forget",
    "delete": "forget",
}


def _is_symlink_or_reparse(path: Path) -> bool:
    """Return True if `path` is a symlink or Windows reparse point / junction (Finding N6)."""
    try:
        if path.is_symlink():
            return True
        is_junc = getattr(path, "is_junction", None)
        if is_junc is not None and is_junc():
            return True
        if os.name == "nt" and path.exists():
            st = path.lstat()
            if getattr(st, "st_file_attributes", 0) & 0x400:
                return True
    except OSError:
        return False
    return False


# --- Path-trust cache -------------------------------------------------------
# A full reject_symlink() costs ~9-25 syscalls (is_symlink/is_junction/exists/lstat
# x 3 sidecar suffixes), which on NTFS + Defender is milliseconds per call. Paths
# that passed the full check are remembered for MESH_PATH_RECHECK_SECONDS (default
# 5, 0 disables). While trusted, every call still does ONE lstat of the path itself,
# so swapping the db/dir for a symlink or junction is caught immediately; only the
# -wal/-shm sidecar re-check waits for the TTL.
_PATH_TRUST: dict[str, float] = {}
_RESOLVED_DIR_CACHE: dict[str, tuple[float, Path]] = {}
_FILE_PERM_TS: dict[str, float] = {}
_AUDIT_PATH_CACHE: dict[tuple[str, str], Path] = {}


def _path_recheck_ttl() -> float:
    raw = os.environ.get("MESH_PATH_RECHECK_SECONDS")
    if raw is None:
        return 5.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


_PATH_MICRO: dict[str, float] = {}


def _fs_memo_seconds() -> float:
    """Window (default 10 ms) in which a path that was lstat-verified is not re-checked (MESH_FS_MEMO_MS, 0 = off)."""
    raw = os.environ.get("MESH_FS_MEMO_MS")
    try:
        return max(0.0, float(raw) / 1000.0) if raw is not None else 0.01
    except ValueError:
        return 0.01


def clear_path_trust_cache() -> None:
    """Forget every verified path (tests / evict hooks)."""
    _PATH_TRUST.clear()
    _PATH_MICRO.clear()
    _RESOLVED_DIR_CACHE.clear()
    _FILE_PERM_TS.clear()
    _AUDIT_PATH_CACHE.clear()


def _lstat_link_state(key: str) -> Optional[bool]:
    """True = symlink/reparse point, False = plain entry, None = missing/unreadable."""
    try:
        st = os.lstat(key)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or (getattr(st, "st_file_attributes", 0) & 0x400):
        return True
    return False


def cached_resolved_mesh_dir() -> Path:
    """`get_mesh_dir().resolve()` memoized for the path-trust TTL."""
    raw = get_mesh_dir()
    key = os.fspath(raw)
    ttl = _path_recheck_ttl()
    if ttl > 0.0:
        ent = _RESOLVED_DIR_CACHE.get("R:" + key)
        if ent is not None and (time.monotonic() - ent[0]) < ttl:
            return ent[1]
    resolved = raw.resolve()
    if ttl > 0.0:
        _RESOLVED_DIR_CACHE["R:" + key] = (time.monotonic(), resolved)
    return resolved


def reject_symlink(path: Path) -> None:
    """
    Refuse to operate on a symlink (or sidecar `-wal`/`-shm` symlink) to prevent
    symlink-following arbitrary file clobbering or chmod attacks (Finding N6).
    """
    key = os.fspath(path)
    ttl = _path_recheck_ttl()
    if ttl > 0.0:
        ts = _PATH_TRUST.get(key)
        _now = time.monotonic()
        if ts is not None and (_now - ts) < ttl:
            _micro = _fs_memo_seconds()
            if _micro > 0.0:
                _seen = _PATH_MICRO.get(key)
                if _seen is not None and (_now - _seen) < _micro:
                    return  # lstat-verified milliseconds ago, inside the same operation: skip a repeat syscall
            link = _lstat_link_state(key)
            if link is False:
                _PATH_MICRO[key] = _now
                return
            _PATH_MICRO.pop(key, None)
            _PATH_TRUST.pop(key, None)
            if link is True:
                raise ValueError(f"Refusing to operate on symlinked path: '{path}'.")
            # Missing now: fall through to the full check.
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}") if suffix else path
        if _is_symlink_or_reparse(candidate):
            raise ValueError(f"Refusing to operate on symlinked path: '{candidate}'.")
    if ttl > 0.0:
        _PATH_TRUST[key] = time.monotonic()


def _apply_windows_owner_acl(target: Path) -> None:
    """
    Restrict `target` on Windows to the current user via `icacls` once per path lifecycle (Finding N5).
    Skipped only when `MESH_SKIP_WINDOWS_ACL=1` is set.
    """
    if os.name != "nt":
        return
    if os.environ.get("MESH_SKIP_WINDOWS_ACL", "").strip().lower() in ("1", "true", "yes"):
        return
    try:
        resolved = target.resolve()
        key = str(resolved)
    except OSError:
        return
    with _WIN_ACL_LOCK:
        if key in _WIN_ACL_APPLIED:
            return
        _WIN_ACL_APPLIED.add(key)

    if target.is_file():
        parent_key = str(resolved.parent)
        with _WIN_ACL_LOCK:
            if parent_key in _WIN_ACL_APPLIED or (resolved.parent / ".acl_applied").exists():
                return
    elif target.is_dir():
        marker = resolved / ".acl_applied"
        if marker.exists():
            return

    username = (os.environ.get("USERNAME") or "").strip()
    if not username:
        return
    userdomain = (os.environ.get("USERDOMAIN") or "").strip()
    principal = f"{userdomain}\\{username}" if userdomain else username
    grant_perm = f"{principal}:(OI)(CI)F" if target.is_dir() else f"{principal}:F"
    try:
        res = subprocess.run(
            ["icacls", str(target), "/inheritance:r", "/grant:r", grant_perm],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            timeout=2.5,
            check=False,
        )
        if res.returncode == 0 and target.is_dir():
            try:
                (resolved / ".acl_applied").touch(exist_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def ensure_secure_mesh_dir(path: Optional[Path] = None) -> Path:
    """
    Create `~/.mesh` (or `path`) with mode 0700 on POSIX and owner-only ACLs on Windows (Finding N5, N6).
    """
    target = path if path is not None else get_mesh_dir()
    reject_symlink(target)
    _tkey = os.fspath(target)
    _ttl = _path_recheck_ttl()
    if _ttl > 0.0:
        _ent = _RESOLVED_DIR_CACHE.get(_tkey)
        if _ent is not None and (time.monotonic() - _ent[0]) < _ttl and os.path.isdir(_tkey):
            return target
    try:
        resolved = target.resolve()
        with _SECURED_LOCK:
            if resolved in _SECURED_PATHS and target.exists():
                if _ttl > 0.0:
                    _RESOLVED_DIR_CACHE[_tkey] = (time.monotonic(), resolved)
                return target
    except OSError:
        resolved = target

    if not target.exists():
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        try:
            current_mode = stat.S_IMODE(target.stat().st_mode)
            if current_mode & (stat.S_IRWXG | stat.S_IRWXO):
                os.chmod(target, stat.S_IRWXU)
        except OSError:
            pass
    else:
        _apply_windows_owner_acl(target)

    with _SECURED_LOCK:
        _SECURED_PATHS[resolved] = 1
    if _ttl > 0.0:
        _RESOLVED_DIR_CACHE[_tkey] = (time.monotonic(), resolved)
    return target


def secure_file_permissions(file_path: Path) -> None:
    """
    Enforce 0600 permissions on POSIX and owner-only ACLs on Windows for a database
    and any WAL/SHM sidecar files, rejecting symlinks first (Findings N5, N6).
    """
    _fkey = os.fspath(file_path)
    _fttl = _path_recheck_ttl()
    if _fttl > 0.0:
        _fts = _FILE_PERM_TS.get(_fkey)
        if _fts is not None and (time.monotonic() - _fts) < _fttl and _lstat_link_state(_fkey) is False:
            return
    try:
        resolved_base = file_path.resolve()
        with _SECURED_LOCK:
            cached_stamp = _SECURED_PATHS.get(resolved_base)
        if cached_stamp is not None and file_path.exists():
            curr_stamp = int(file_path.stat().st_mtime_ns)
            if curr_stamp == cached_stamp:
                return
    except OSError:
        resolved_base = file_path

    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{file_path}{suffix}")
        if _is_symlink_or_reparse(candidate):
            raise ValueError(f"Refusing to set permissions on symlinked file: '{candidate}'.")
        if candidate.exists():
            if os.name == "nt":
                _apply_windows_owner_acl(candidate)
            else:
                try:
                    current_mode = stat.S_IMODE(candidate.stat().st_mode)
                    if current_mode != (stat.S_IRUSR | stat.S_IWUSR):
                        os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass

    try:
        if file_path.exists():
            new_stamp = int(file_path.stat().st_mtime_ns)
            with _SECURED_LOCK:
                _SECURED_PATHS[resolved_base] = new_stamp
            if _fttl > 0.0:
                _FILE_PERM_TS[_fkey] = time.monotonic()
    except OSError:
        pass


def validate_namespace(namespace: str) -> str:
    """
    Validate a namespace identifier to prevent path traversal or invalid filenames,
    and optionally enforce single-namespace confinement when `MESH_LOCKED_NAMESPACE` is set.
    Allowed: 1-64 characters of ASCII letters, digits, '.', '_', '-', starting with alphanumeric.
    """
    if not isinstance(namespace, str):
        raise ValueError("Namespace must be a string.")
    clean = namespace.strip()
    if not clean:
        raise ValueError("Namespace cannot be empty.")
    if ".." in clean or "/" in clean or "\\" in clean or not _VALID_NAMESPACE_RE.match(clean):
        raise ValueError(
            f"Invalid namespace '{namespace}'. Must match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}$ "
            "and must not contain path separators or '..'."
        )
    locked_raw = os.environ.get("MESH_LOCKED_NAMESPACE", "").strip()
    if locked_raw:
        if locked_raw.lower() in ("1", "true", "yes"):
            allowed_ns = (os.environ.get("MESH_NAMESPACE", "shared") or "shared").strip() or "shared"
        else:
            allowed_ns = locked_raw
        if clean != allowed_ns:
            raise ValueError(
                f"Namespace '{clean}' is blocked by MESH_LOCKED_NAMESPACE (restricted to '{allowed_ns}')."
            )
    return clean


class _ManagedAuditConnection(sqlite3.Connection):
    """
    Persistent SQLite WAL connection that commits/rolls back when exiting a `with` block
    without closing the file handle until explicitly evicted.
    """

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Literal[False]:
        super().__exit__(exc_type, exc_val, exc_tb)
        return False

    def close(self) -> None:
        pass

    def close_pooled(self) -> None:
        try:
            super().close()
        except Exception:
            pass


def flush_audit_log(namespace: Optional[str] = None) -> None:
    """Synchronously drain pending audit queue entries for `namespace` (or all namespaces)."""
    with _AUDIT_FLUSH_LOCK:
        with _AUDIT_QUEUE_LOCK:
            if namespace is not None:
                try:
                    clean_ns = validate_namespace(namespace)
                    items = _AUDIT_PENDING.pop(clean_ns, [])
                    work = [(clean_ns, items)] if items else []
                except ValueError:
                    return
            else:
                work = list(_AUDIT_PENDING.items())
                _AUDIT_PENDING.clear()

        if not work:
            return

        for ns, items in work:
            if not items:
                continue
            try:
                db_path = _get_audit_db_path(ns)
                with _AUDIT_LOCK:
                    stamp = _get_audit_db_stamp(db_path)
                    needs_init = stamp is None or _INITIALIZED_AUDIT_DBS.get(db_path) != stamp
                    write_count = _AUDIT_WRITE_COUNTS.get(db_path, 0) + len(items)
                    _AUDIT_WRITE_COUNTS[db_path] = write_count
                    with _connect_audit(db_path) as conn:
                        if needs_init:
                            _apply_audit_schema(conn)
                        for sql, params in items:
                            conn.execute(sql, params)
                        if needs_init or (write_count % 100 < len(items)):
                            _prune_audit_log_locked(conn, _max_audit_rows())
                        conn.commit()
                    secure_file_permissions(db_path)
                    new_stamp = _get_audit_db_stamp(db_path)
                    if new_stamp is not None:
                        _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
            except Exception:
                pass


flush_pending_audit_writes = flush_audit_log


def _audit_drain_worker() -> None:
    """Background daemon thread: drain _AUDIT_PENDING in batches with debounce."""
    while True:
        _AUDIT_WAKE_EVENT.wait(timeout=0.5)
        _AUDIT_WAKE_EVENT.clear()
        time.sleep(0.030)
        flush_audit_log(None)


def _ensure_audit_drain_thread() -> None:
    global _AUDIT_DRAIN_THREAD
    with _AUDIT_DRAIN_LOCK:
        if _AUDIT_DRAIN_THREAD is not None and _AUDIT_DRAIN_THREAD.is_alive():
            return
        t = threading.Thread(
            target=_audit_drain_worker,
            name="mesh-audit-drain",
            daemon=True,
        )
        t.start()
        _AUDIT_DRAIN_THREAD = t


@atexit.register
def _close_all_audit_connections() -> None:
    try:
        flush_audit_log(None)
    except Exception:
        pass
    with _AUDIT_LOCK:
        for conn in list(_AUDIT_CONNECTION_POOL.values()):
            conn.close_pooled()
        _AUDIT_CONNECTION_POOL.clear()


def _get_audit_db_path(namespace: str) -> Path:
    clean_ns = validate_namespace(namespace)
    _ckey = (clean_ns, os.fspath(get_mesh_dir()))
    _hit = _AUDIT_PATH_CACHE.get(_ckey)
    if _hit is not None:
        _hk = os.fspath(_hit)
        _ts = _PATH_TRUST.get(_hk)
        if _ts is not None and (time.monotonic() - _ts) < _path_recheck_ttl():
            reject_symlink(_hit)  # one lstat while trusted
            return _hit
    ensure_secure_mesh_dir()
    base_dir = cached_resolved_mesh_dir()
    raw_candidate = base_dir / f"audit_{clean_ns}.db"
    reject_symlink(raw_candidate)
    db_path = raw_candidate.resolve()
    if db_path.parent != base_dir:
        raise ValueError(f"Invalid namespace path resolution for '{namespace}'.")
    reject_symlink(db_path)
    _AUDIT_PATH_CACHE[_ckey] = db_path
    return db_path


def _check_audit_namespace_limit(base_dir: Path, db_path: Path) -> None:
    """Enforce `MESH_MAX_NAMESPACES` before creating a new `audit_<ns>.db` file (Finding N10)."""
    if db_path.exists() or not base_dir.exists():
        return
    stem = db_path.stem
    ns = stem[6:] if stem.startswith("audit_") else stem
    if (base_dir / f"mesh_{ns}.db").exists():
        return
    max_ns = env_int("MESH_MAX_NAMESPACES", _DEFAULT_MAX_NAMESPACES, minimum=1)
    tracked: set[str] = set()
    for p in base_dir.glob("mesh_*.db"):
        if p.is_file() and not p.name.endswith(("-wal", "-shm")):
            tracked.add(p.stem[5:])
    for p in base_dir.glob("audit_*.db"):
        if p.is_file() and not p.name.endswith(("-wal", "-shm")):
            tracked.add(p.stem[6:])
    if ns not in tracked and len(tracked) >= max_ns:
        raise ValueError(
            f"Maximum number of tracked namespaces ({max_ns}) exceeded; "
            f"refusing to create audit database for '{ns}'."
        )


def _get_audit_db_stamp(db_path: Path) -> Optional[tuple[int, int, int, int]]:
    """Return a composite (db_mtime_ns, db_size, wal_mtime_ns, wal_size) stamp for WAL-safe sync."""
    # One stat per file: the old exists()+stat() pairs doubled the syscalls (~60 us each on Windows with AV).
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    if st.st_size <= 0:
        return None
    try:
        wst = os.stat(f"{db_path}-wal")
        return (st.st_mtime_ns, st.st_size, wst.st_mtime_ns, wst.st_size)
    except OSError:
        return (st.st_mtime_ns, st.st_size, 0, 0)


def evict_audit_cache(namespace: str) -> None:
    """Evict tracked initialization state and close pooled connections for a namespace's audit DB."""
    try:
        clean_ns = validate_namespace(namespace)
        flush_audit_log(clean_ns)
        base_dir = get_mesh_dir().resolve()
        db_path = (base_dir / f"audit_{clean_ns}.db").resolve()
    except ValueError:
        return
    with _SECURED_LOCK:
        _SECURED_PATHS.pop(db_path, None)
    _AUDIT_INITIALIZED_FILES.discard(db_path)
    with _AUDIT_LOCK:
        _INITIALIZED_AUDIT_DBS.pop(db_path, None)
        _AUDIT_WRITE_COUNTS.pop(db_path, None)
        conn = _AUDIT_CONNECTION_POOL.pop(db_path, None)
        if conn is not None:
            conn.close_pooled()


def _apply_audit_schema(conn: sqlite3.Connection) -> None:
    """Create the audit_log and audit_events tables and indexes on an open connection."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_AUDIT_SCHEMA_SQL)
    conn.execute(_AUDIT_EVENTS_SCHEMA_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON audit_log(timestamp DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_action ON audit_log(action)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent ON audit_log(agent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_id ON audit_log(memory_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_ts ON audit_events(timestamp DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_ns ON audit_events(namespace, timestamp DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_surface ON audit_events(surface)")


def _connect_audit(db_path: Path) -> _ManagedAuditConnection:
    reject_symlink(db_path)
    with _AUDIT_LOCK:
        conn = _AUDIT_CONNECTION_POOL.get(db_path)
        if conn is not None and db_path.exists():
            _AUDIT_CONNECTION_POOL.move_to_end(db_path)
            stamp = _get_audit_db_stamp(db_path)
            if stamp is None or _INITIALIZED_AUDIT_DBS.get(db_path) != stamp:
                _apply_audit_schema(conn)
                new_stamp = _get_audit_db_stamp(db_path)
                if new_stamp is not None:
                    _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
            return conn
        if conn is not None:
            conn.close_pooled()
            _AUDIT_CONNECTION_POOL.pop(db_path, None)

        ensure_secure_mesh_dir(db_path.parent)
        _check_audit_namespace_limit(db_path.parent, db_path)
        conn = sqlite3.connect(
            db_path,
            timeout=5.0,
            check_same_thread=False,
            factory=_ManagedAuditConnection,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        sync_mode = "NORMAL" if os.environ.get("MESH_STRICT_FSYNC") == "1" else "OFF"
        conn.execute(f"PRAGMA synchronous={sync_mode}")
        try:
            conn.execute("PRAGMA mmap_size=67108864")
        except sqlite3.OperationalError:
            pass
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute("PRAGMA secure_delete=ON")
        try:
            conn.execute("PRAGMA cache_size=-8000")
        except sqlite3.OperationalError:
            pass
        _apply_audit_schema(conn)
        secure_file_permissions(db_path)
        _AUDIT_CONNECTION_POOL[db_path] = conn
        _AUDIT_CONNECTION_POOL.move_to_end(db_path)
        while len(_AUDIT_CONNECTION_POOL) > _MAX_AUDIT_POOL_SIZE:
            evicted_path, evicted_conn = _AUDIT_CONNECTION_POOL.popitem(last=False)
            _INITIALIZED_AUDIT_DBS.pop(evicted_path, None)
            _AUDIT_WRITE_COUNTS.pop(evicted_path, None)
            evicted_conn.close_pooled()
        new_stamp = _get_audit_db_stamp(db_path)
        if new_stamp is not None:
            _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
        return conn


def _max_audit_rows() -> int:
    try:
        val = int(os.environ.get("MESH_MAX_AUDIT_ROWS", str(DEFAULT_MAX_AUDIT_ROWS)))
        return max(100, val)
    except ValueError:
        return DEFAULT_MAX_AUDIT_ROWS


def _prune_audit_log_locked(conn: sqlite3.Connection, max_rows: int) -> None:
    """Keep at most `max_rows` recent rows in `audit_log` and `audit_events` so the database never grows without bound."""
    try:
        row = conn.execute("SELECT MAX(id), COUNT(*) FROM audit_log").fetchone()
        if row and row[0] is not None:
            max_id, total_count = int(row[0]), int(row[1])
            if total_count > max_rows:
                cutoff_row = conn.execute(
                    "SELECT id FROM audit_log ORDER BY id DESC LIMIT 1 OFFSET ?",
                    (max_rows,),
                ).fetchone()
                if cutoff_row:
                    conn.execute("DELETE FROM audit_log WHERE id <= ?", (int(cutoff_row[0]),))
                else:
                    conn.execute("DELETE FROM audit_log WHERE id <= ?", (max_id - max_rows,))
    except Exception:
        pass
    try:
        ev_row = conn.execute("SELECT MAX(id), COUNT(*) FROM audit_events").fetchone()
        if ev_row and ev_row[0] is not None:
            max_ev_id, total_ev_count = int(ev_row[0]), int(ev_row[1])
            if total_ev_count > max_rows:
                cutoff_ev = conn.execute(
                    "SELECT id FROM audit_events ORDER BY id DESC LIMIT 1 OFFSET ?",
                    (max_rows,),
                ).fetchone()
                if cutoff_ev:
                    conn.execute("DELETE FROM audit_events WHERE id <= ?", (int(cutoff_ev[0]),))
                else:
                    conn.execute("DELETE FROM audit_events WHERE id <= ?", (max_ev_id - max_rows,))
    except Exception:
        pass


def _maybe_encrypt_audit_field(val: Optional[str]) -> Optional[str]:
    if val is None:
        return None
    from .crypto import encrypt_text_for_storage, is_encryption_enabled

    if is_encryption_enabled():
        return encrypt_text_for_storage(val)
    return val


def _maybe_decrypt_audit_field(val: Optional[str]) -> Optional[str]:
    if val is None:
        return None
    try:
        from .crypto import decrypt_text_if_encrypted, is_encrypted_text

        if is_encrypted_text(val):
            return decrypt_text_if_encrypted(val)
    except Exception:
        pass
    return val


def _sanitize_audit_text(text: Optional[str]) -> Optional[str]:
    """Ensure raw credentials never land in `audit_log` even on public calls (Finding S12)."""
    if text is None:
        return None
    try:
        from .secrets import is_credential, redact_secrets_local

        if is_credential(text):
            return redact_secrets_local(text)
    except Exception:
        pass
    return text


def _write_audit_entry_sync(namespace: str, sql: str, params: tuple[Any, ...]) -> None:
    """
    Execute an audit log statement synchronously on the pooled connection.
    Called by the background drain thread or by flush_pending_audit_writes().
    """
    namespace = validate_namespace(namespace)
    db_path = _get_audit_db_path(namespace)
    with _AUDIT_LOCK:
        stamp = _get_audit_db_stamp(db_path)
        needs_init = stamp is None or _INITIALIZED_AUDIT_DBS.get(db_path) != stamp
        write_count = _AUDIT_WRITE_COUNTS.get(db_path, 0) + 1
        _AUDIT_WRITE_COUNTS[db_path] = write_count
        with _connect_audit(db_path) as conn:
            if needs_init:
                _apply_audit_schema(conn)
            conn.execute(sql, params)
            if needs_init or write_count % 100 == 0:
                _prune_audit_log_locked(conn, _max_audit_rows())
            conn.commit()
        secure_file_permissions(db_path)
        new_stamp = _get_audit_db_stamp(db_path)
        if new_stamp is not None:
            _INITIALIZED_AUDIT_DBS[db_path] = new_stamp


def _write_audit_entry(namespace: str, sql: str, params: tuple[Any, ...]) -> None:
    """
    Enqueue an audit log entry for asynchronous write by the background drain thread.
    Returns immediately; the caller's hot path is never blocked on a SQLite commit.
    First row for a new DB is committed synchronously so audit_<ns>.db is guaranteed to exist.
    """
    _AUDIT_ENQUEUE_SEQ[0] += 1
    try:
        clean_ns = validate_namespace(namespace)
        db_path = _get_audit_db_path(clean_ns)
    except Exception:
        return

    if db_path not in _AUDIT_KNOWN_DB_PATHS:
        if not db_path.exists():
            try:
                with _AUDIT_LOCK:
                    _connect_audit(db_path)
            except Exception:
                pass
        _AUDIT_KNOWN_DB_PATHS.add(db_path)

    if _AUDIT_DRAIN_THREAD is None or not _AUDIT_DRAIN_THREAD.is_alive():
        _ensure_audit_drain_thread()

    with _AUDIT_QUEUE_LOCK:
        if clean_ns not in _AUDIT_PENDING:
            _AUDIT_PENDING[clean_ns] = []
        _AUDIT_PENDING[clean_ns].append((sql, params))
    _AUDIT_WAKE_EVENT.set()


def log_recall(
    query: str,
    namespace: str,
    agent_id: str,
    results: list[dict[str, Any]],
) -> None:
    """
    Log a recall event to the audit trail, redacting `local_only` top results,
    queries that returned a private result, and queries containing credentials (Finding S12).
    """
    try:
        top_result: Optional[str] = None
        top_similarity: Optional[float] = None
        if results:
            first = results[0]
            top_similarity = float(first.get("similarity", 0.0))
            if first.get("local_only"):
                top_result = REDACTED_CONTENT_PLACEHOLDER
            else:
                top_result = _sanitize_audit_text(first.get("content"))

        # Finding S12: Redact the query if any returned memory is private OR if the query itself
        # contains a secret/credential (e.g. searching for a leaked key that returned 0 results).
        from .secrets import is_credential

        if any(r.get("local_only") for r in results) or is_credential(query):
            logged_query = REDACTED_CONTENT_PLACEHOLDER
        else:
            logged_query = query

        _write_audit_entry(
            namespace=namespace,
            sql="""
                INSERT INTO audit_log
                (timestamp, action, agent_id, namespace, query, top_result, top_similarity, result_count)
                VALUES (?, 'recall', ?, ?, ?, ?, ?, ?)
            """,
            params=(
                datetime.now(timezone.utc).isoformat(),
                agent_id,
                namespace,
                _maybe_encrypt_audit_field(logged_query),
                _maybe_encrypt_audit_field(top_result),
                top_similarity,
                len(results),
            ),
        )
    except Exception:
        pass  # Audit logging must never crash the main flow


def log_learn(
    content: str,
    namespace: str,
    agent_id: str,
    result: dict[str, Any],
    local_only: bool = False,
) -> None:
    """Log a learn event to the audit trail, redacting local_only content."""
    try:
        mem_id = result.get("memory_id")
        is_private = bool(local_only or result.get("local_only"))
        logged_content = (
            REDACTED_CONTENT_PLACEHOLDER
            if is_private
            else (_sanitize_audit_text(content) or content)
        )
        had_conflict = 1 if result.get("conflict") else 0
        now_ts = datetime.now(timezone.utc).isoformat()

        _write_audit_entry(
            namespace=namespace,
            sql="""
                INSERT INTO audit_log
                (timestamp, action, agent_id, namespace, content, memory_id, had_conflict)
                VALUES (?, 'learn', ?, ?, ?, ?, ?)
            """,
            params=(
                now_ts,
                agent_id,
                namespace,
                _maybe_encrypt_audit_field(logged_content),
                mem_id,
                had_conflict,
            ),
        )
    except Exception:
        pass


def _scrub_audit_columns_locked(
    conn: sqlite3.Connection,
    namespace: str,
    memory_id: str,
    needle_text: Optional[str],
    placeholder: str,
) -> None:
    """
    Scrub `audit_log` rows matching `memory_id` or containing `needle_text` (exact or substring),
    including encrypted rows when at-rest encryption is active (Findings S5, S12).
    """
    enc_placeholder = _maybe_encrypt_audit_field(placeholder) or placeholder
    clean_needle = (needle_text or "").strip()
    if not clean_needle:
        conn.execute(
            """
            UPDATE audit_log
            SET content = ?
            WHERE namespace = ? AND memory_id = ? AND action IN ('learn', 'update')
            """,
            (enc_placeholder, namespace, memory_id),
        )
        return

    needle_low = clean_needle.lower()
    conn.execute(
        """
        UPDATE audit_log
        SET
            content = CASE WHEN (memory_id = ? AND action IN ('learn', 'update')) OR (content IS NOT NULL AND INSTR(LOWER(content), ?) > 0) THEN ? ELSE content END,
            top_result = CASE WHEN top_result IS NOT NULL AND INSTR(LOWER(top_result), ?) > 0 THEN ? ELSE top_result END,
            query = CASE WHEN query IS NOT NULL AND INSTR(LOWER(query), ?) > 0 THEN ? ELSE query END
        WHERE namespace = ?
        """,
        (memory_id, needle_low, enc_placeholder, needle_low, enc_placeholder, needle_low, enc_placeholder, namespace),
    )

    try:
        from .crypto import is_encryption_enabled

        if is_encryption_enabled():
            rows = conn.execute(
                "SELECT id, query, content, top_result FROM audit_log "
                "WHERE namespace = ? AND (query LIKE 'enc:v1:%' OR content LIKE 'enc:v1:%' OR top_result LIKE 'enc:v1:%')",
                (namespace,),
            ).fetchall()
            for row_id, q_val, c_val, top_val in rows:
                dec_q = _maybe_decrypt_audit_field(q_val)
                dec_c = _maybe_decrypt_audit_field(c_val)
                dec_top = _maybe_decrypt_audit_field(top_val)
                new_q = enc_placeholder if (dec_q and needle_low in dec_q.lower()) else q_val
                new_c = enc_placeholder if (dec_c and needle_low in dec_c.lower()) else c_val
                new_top = enc_placeholder if (dec_top and needle_low in dec_top.lower()) else top_val
                if (new_q, new_c, new_top) != (q_val, c_val, top_val):
                    conn.execute(
                        "UPDATE audit_log SET query = ?, content = ?, top_result = ? WHERE id = ?",
                        (new_q, new_c, new_top, row_id),
                    )
    except Exception:
        pass


def scrub_memory_update_from_audit(
    namespace: str,
    memory_id: str,
    old_content: Optional[str] = None,
    local_only: bool = False,
) -> None:
    """
    Scrub prior content for `memory_id` in `audit_log` when a memory is updated
    (e.g. when a user edits out an accidentally pasted secret or marks a memory `local_only=True`).
    """
    try:
        namespace = validate_namespace(namespace)
        _dedup_text = f"{int(bool(local_only))}:{old_content or ''}"
        if _scrub_recently_done("update", namespace, memory_id, _dedup_text):
            return
        flush_audit_log(namespace)
        db_path = _get_audit_db_path(namespace)
        if not db_path.exists() or _get_audit_db_stamp(db_path) is None:
            return
        placeholder = REDACTED_CONTENT_PLACEHOLDER if local_only else UPDATED_CONTENT_PLACEHOLDER
        with _AUDIT_LOCK:
            with _connect_audit(db_path) as conn:
                if local_only or (old_content is not None and old_content.strip()):
                    _scrub_audit_columns_locked(
                        conn=conn,
                        namespace=namespace,
                        memory_id=memory_id,
                        needle_text=old_content,
                        placeholder=placeholder,
                    )
                conn.commit()
            new_stamp = _get_audit_db_stamp(db_path)
            if new_stamp is not None:
                _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
        _scrub_mark_done("update", namespace, memory_id, _dedup_text)
    except Exception:
        pass


def log_update(
    memory_id: str,
    content: Optional[str],
    namespace: str,
    agent_id: str,
    local_only: bool = False,
    old_content: Optional[str] = None,
) -> None:
    """
    Scrub replaced/private historical content and log an in-place memory update event
    to the audit trail, redacting `local_only` content.
    """
    try:
        if local_only or (content is not None and old_content and content.strip() != old_content.strip()):
            scrub_memory_update_from_audit(
                namespace=namespace,
                memory_id=memory_id,
                old_content=old_content,
                local_only=local_only,
            )
        if local_only:
            logged_content = REDACTED_CONTENT_PLACEHOLDER
        else:
            logged_content = _sanitize_audit_text(content) or f"[updated metadata for {memory_id}]"
        _write_audit_entry(
            namespace=namespace,
            sql="""
                INSERT INTO audit_log
                (timestamp, action, agent_id, namespace, content, memory_id, had_conflict)
                VALUES (?, 'update', ?, ?, ?, ?, 0)
            """,
            params=(
                datetime.now(timezone.utc).isoformat(),
                agent_id,
                namespace,
                _maybe_encrypt_audit_field(logged_content),
                memory_id,
            ),
        )
    except Exception:
        pass


_SCRUB_DEDUP: dict[tuple, tuple[float, int]] = {}
_AUDIT_ENQUEUE_SEQ = [0]


def _scrub_recently_done(kind: str, namespace: str, memory_id: str, text: Optional[str]) -> bool:
    """True when the identical scrub finished <2 s ago and nothing was logged since."""
    ent = _SCRUB_DEDUP.get((kind, namespace, memory_id, hash(text or "")))
    return ent is not None and (time.monotonic() - ent[0]) < 2.0 and ent[1] == _AUDIT_ENQUEUE_SEQ[0]


def _scrub_mark_done(kind: str, namespace: str, memory_id: str, text: Optional[str]) -> None:
    if len(_SCRUB_DEDUP) > 512:
        _SCRUB_DEDUP.clear()
    _SCRUB_DEDUP[(kind, namespace, memory_id, hash(text or ""))] = (time.monotonic(), _AUDIT_ENQUEUE_SEQ[0])


def purge_memory_from_audit(
    namespace: str,
    memory_id: str,
    content: Optional[str] = None,
) -> None:
    """
    Scrub all historical occurrences of a forgotten memory's content, top_result,
    and query from the audit trail so `forget()` guarantees complete erasure.
    """
    try:
        namespace = validate_namespace(namespace)
        if _scrub_recently_done("purge", namespace, memory_id, content):
            return
        flush_audit_log(namespace)
        db_path = _get_audit_db_path(namespace)
        if not db_path.exists() or _get_audit_db_stamp(db_path) is None:
            return
        with _AUDIT_LOCK:
            with _connect_audit(db_path) as conn:
                _scrub_audit_columns_locked(
                    conn=conn,
                    namespace=namespace,
                    memory_id=memory_id,
                    needle_text=content,
                    placeholder=PURGED_CONTENT_PLACEHOLDER,
                )
                conn.commit()
            new_stamp = _get_audit_db_stamp(db_path)
            if new_stamp is not None:
                _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
        _scrub_mark_done("purge", namespace, memory_id, content)
    except Exception:
        pass


def clear_audit_log(namespace: str) -> None:
    """
    Purge all rows for `namespace` from `audit_<namespace>.db`, checkpoint the WAL,
    and `VACUUM` so `Mesh.clear()` leaves zero historical audit side-channel data (Finding S12).
    """
    try:
        clean_ns = validate_namespace(namespace)
        flush_audit_log(clean_ns)
        db_path = _get_audit_db_path(clean_ns)
        _AUDIT_INITIALIZED_FILES.discard(db_path)
        if not db_path.exists():
            return
        with _AUDIT_LOCK:
            with _connect_audit(db_path) as conn:
                conn.execute("DELETE FROM audit_log WHERE namespace = ?", (clean_ns,))
                conn.commit()
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("VACUUM")
                except sqlite3.OperationalError:
                    pass
            new_stamp = _get_audit_db_stamp(db_path)
            if new_stamp is not None:
                _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
    except Exception:
        pass


def log_forget(
    memory_id: str,
    namespace: str,
    agent_id: str,
    purged_content: Optional[str] = None,
) -> None:
    """Purge prior content for `memory_id` and log the deletion event to the audit trail."""
    try:
        purge_memory_from_audit(namespace=namespace, memory_id=memory_id, content=purged_content)
        _write_audit_entry(
            namespace=namespace,
            sql="""
                INSERT INTO audit_log
                (timestamp, action, agent_id, namespace, content, memory_id, had_conflict)
                VALUES (?, 'forget', ?, ?, ?, ?, 0)
            """,
            params=(
                datetime.now(timezone.utc).isoformat(),
                agent_id,
                namespace,
                _maybe_encrypt_audit_field(f"[deleted memory {memory_id}]"),
                memory_id,
            ),
        )
    except Exception:
        pass


def get_audit_log(
    namespace: str,
    limit: int = 50,
    action_filter: str = "all",
) -> list[dict[str, Any]]:
    """
    Return recent audit entries for a namespace, most recent first, decrypting fields if encrypted.
    """
    flush_pending_audit_writes()
    clean_ns = validate_namespace(namespace)
    db_path = _get_audit_db_path(clean_ns)
    clean_filter = (action_filter or "all").strip().lower()
    if clean_filter != "all" and clean_filter not in _ACTION_FILTER_MAP:
        valid_filters = sorted({"all", *_ACTION_FILTER_MAP.keys()})
        raise ValueError(
            f"Invalid action_filter '{action_filter}'. Must be one of {valid_filters}."
        )
    safe_limit = max(1, int(limit or 50))
    if not db_path.exists() or _get_audit_db_stamp(db_path) is None:
        return []
    action_sql = _ACTION_FILTER_MAP.get(clean_filter) if clean_filter != "all" else None
    with _AUDIT_LOCK:
        with _connect_audit(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if action_sql:
                cur = conn.execute(
                    """
                    SELECT * FROM audit_log
                    WHERE namespace = ? AND action = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                    """,
                    (clean_ns, action_sql, safe_limit),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT * FROM audit_log
                    WHERE namespace = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                    """,
                    (clean_ns, safe_limit),
                )
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()

    for r in rows:
        if r.get("query") is not None:
            r["query"] = _maybe_decrypt_audit_field(r["query"])
        if r.get("content") is not None:
            r["content"] = _maybe_decrypt_audit_field(r["content"])
        if r.get("top_result") is not None:
            r["top_result"] = _maybe_decrypt_audit_field(r["top_result"])
    return rows


def get_audit_stats(namespace: str) -> dict[str, Any]:
    """Return aggregate stats from the audit log in a minimal query pass."""
    flush_pending_audit_writes()
    clean_ns = validate_namespace(namespace)
    db_path = _get_audit_db_path(clean_ns)
    if not db_path.exists() or _get_audit_db_stamp(db_path) is None:
        return {
            "total_recalls": 0,
            "total_learns": 0,
            "total_updates": 0,
            "total_forgets": 0,
            "agents": [],
            "conflicts_detected": 0,
            "last_activity": None,
        }
    with _AUDIT_LOCK:
        with _connect_audit(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur_agg = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN action = 'recall' THEN 1 ELSE 0 END), 0) AS recalls,
                    COALESCE(SUM(CASE WHEN action = 'learn' THEN 1 ELSE 0 END), 0) AS learns,
                    COALESCE(SUM(CASE WHEN action = 'update' THEN 1 ELSE 0 END), 0) AS updates,
                    COALESCE(SUM(CASE WHEN action = 'forget' THEN 1 ELSE 0 END), 0) AS forgets,
                    COALESCE(SUM(CASE WHEN had_conflict = 1 THEN 1 ELSE 0 END), 0) AS conflicts,
                    MAX(timestamp) AS last_activity
                FROM audit_log
                WHERE namespace = ?
                """,
                (clean_ns,),
            )
            agg = cur_agg.fetchone()
            cur_agg.close()
            cur_agents = conn.execute(
                "SELECT DISTINCT agent_id FROM audit_log "
                "WHERE namespace = ? AND agent_id IS NOT NULL",
                (clean_ns,),
            )
            agents = [r["agent_id"] for r in cur_agents.fetchall()]
            cur_agents.close()
            return {
                "total_recalls": int(agg["recalls"]) if agg else 0,
                "total_learns": int(agg["learns"]) if agg else 0,
                "total_updates": int(agg["updates"]) if agg else 0,
                "total_forgets": int(agg["forgets"]) if agg else 0,
                "agents": agents,
                "conflicts_detected": int(agg["conflicts"]) if agg else 0,
                "last_activity": agg["last_activity"] if agg else None,
            }


def log_audit_event(
    surface: str,
    tool_or_command: str,
    namespace: str,
    duration_ms: float,
    status_code: str = "ok",
    agent_id: Optional[str] = None,
    payload_size_bytes: int = 0,
    result_count: int = 0,
) -> None:
    """Record an operational audit event (surface, tool/cmd, status, duration, payload size, NEVER memory text)."""
    try:
        clean_ns = validate_namespace(namespace)
        now_iso = datetime.now(timezone.utc).isoformat()
        _write_audit_entry(
            namespace=clean_ns,
            sql="""
                INSERT INTO audit_events
                (timestamp, surface, tool_or_command, agent_id, namespace, duration_ms, status_code, payload_size_bytes, result_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                now_iso,
                str(surface or "unknown"),
                str(tool_or_command or "unknown"),
                str(agent_id or "unknown"),
                clean_ns,
                float(max(0.0, duration_ms)),
                str(status_code or "ok"),
                int(max(0, payload_size_bytes)),
                int(max(0, result_count)),
            ),
        )
    except Exception:
        pass


def export_content_free_audit(
    namespace: Optional[str] = None,
    since: Optional[str] = None,
    surface: Optional[str] = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """
    Export content-free audit events (timing, surface, command, status, sizes, NEVER memory text).
    Aggregates events across all tracked namespaces if namespace is None.
    """
    flush_audit_log(None)
    base_dir = ensure_secure_mesh_dir().resolve()
    target_dbs: list[tuple[str, Path]] = []
    if namespace:
        try:
            clean_ns = validate_namespace(namespace)
            p = base_dir / f"audit_{clean_ns}.db"
            if p.exists():
                target_dbs.append((clean_ns, p))
        except ValueError:
            return []
    else:
        for p in sorted(base_dir.glob("audit_*.db")):
            if p.is_file() and not p.name.endswith(("-wal", "-shm")):
                stem = p.stem
                ns = stem[6:] if stem.startswith("audit_") else stem
                target_dbs.append((ns, p))

    since_iso: Optional[str] = None
    if since:
        since_str = str(since).strip()
        rel_match = re.match(r"^(\d+)\s*([smhdw])$", since_str.lower())
        if rel_match:
            val, unit = int(rel_match.group(1)), rel_match.group(2)
            secs = val * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
            since_iso = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() - secs, tz=timezone.utc
            ).isoformat()
        else:
            since_iso = since_str

    events: list[dict[str, Any]] = []
    safe_limit = max(1, min(10000, int(limit or 1000)))

    for ns, db_path in target_dbs:
        try:
            if not db_path.exists() or _get_audit_db_stamp(db_path) is None:
                continue
            with _AUDIT_LOCK:
                with _connect_audit(db_path) as conn:
                    has_table = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'"
                    ).fetchone()
                    if not has_table:
                        continue
                    conn.row_factory = sqlite3.Row
                    query = "SELECT id, timestamp, surface, tool_or_command, agent_id, namespace, duration_ms, status_code, payload_size_bytes, result_count FROM audit_events WHERE 1=1"
                    params: list[Any] = []
                    if since_iso:
                        query += " AND timestamp >= ?"
                        params.append(since_iso)
                    if surface:
                        query += " AND surface = ?"
                        params.append(str(surface).strip())
                    query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
                    params.append(safe_limit)
                    cur = conn.execute(query, params)
                    for r in cur.fetchall():
                        events.append(dict(r))
                    cur.close()
        except Exception:
            continue

    events.sort(key=lambda x: (str(x.get("timestamp", "")), int(x.get("id", 0))), reverse=True)
    return events[:safe_limit]
