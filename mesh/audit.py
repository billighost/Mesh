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
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .envutil import env_int


def get_mesh_dir() -> Path:
    """Return the active Mesh data directory, respecting MESH_HOME / MESH_DIR env overrides dynamically."""
    override = (os.environ.get("MESH_HOME") or os.environ.get("MESH_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".mesh"


_AUDIT_LOCK = threading.RLock()
_INITIALIZED_AUDIT_DBS: dict[Path, tuple[int, int, int, int]] = {}
_AUDIT_CONNECTION_POOL: OrderedDict[Path, "_ManagedAuditConnection"] = OrderedDict()
_AUDIT_WRITE_COUNTS: dict[Path, int] = {}
_MAX_AUDIT_POOL_SIZE = 64
_DEFAULT_MAX_NAMESPACES = 256

# Finding N5: Memoize Windows ACL application by resolved path so we don't spawn icacls repeatedly
_WIN_ACL_LOCK = threading.Lock()
_WIN_ACL_APPLIED: set[str] = set()

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
        if hasattr(path, "is_junction") and path.is_junction():
            return True
        if os.name == "nt" and path.exists():
            st = path.lstat()
            if getattr(st, "st_file_attributes", 0) & 0x400:
                return True
    except OSError:
        return False
    return False


def reject_symlink(path: Path) -> None:
    """
    Refuse to operate on a symlink (or sidecar `-wal`/`-shm` symlink) to prevent
    symlink-following arbitrary file clobbering or chmod attacks (Finding N6).
    """
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}") if suffix else path
        if _is_symlink_or_reparse(candidate):
            raise ValueError(f"Refusing to operate on symlinked path: '{candidate}'.")


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
        key = str(target.resolve())
    except OSError:
        return
    with _WIN_ACL_LOCK:
        if key in _WIN_ACL_APPLIED:
            return
        _WIN_ACL_APPLIED.add(key)
    username = (os.environ.get("USERNAME") or "").strip()
    if not username:
        return
    userdomain = (os.environ.get("USERDOMAIN") or "").strip()
    principal = f"{userdomain}\\{username}" if userdomain else username
    grant_perm = f"{principal}:(OI)(CI)F" if target.is_dir() else f"{principal}:F"
    try:
        subprocess.run(
            ["icacls", str(target), "/inheritance:r", "/grant:r", grant_perm],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            timeout=2.5,
            check=False,
        )
    except Exception:
        pass


def ensure_secure_mesh_dir(path: Optional[Path] = None) -> Path:
    """
    Create `~/.mesh` (or `path`) with mode 0700 on POSIX and owner-only ACLs on Windows (Finding N5, N6).
    """
    target = path if path is not None else get_mesh_dir()
    reject_symlink(target)
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
    return target


def secure_file_permissions(file_path: Path) -> None:
    """
    Enforce 0600 permissions on POSIX and owner-only ACLs on Windows for a database
    and any WAL/SHM sidecar files, rejecting symlinks first (Findings N5, N6).
    """
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

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return super().__exit__(exc_type, exc_val, exc_tb)

    def close(self) -> None:
        pass

    def close_pooled(self) -> None:
        try:
            super().close()
        except Exception:
            pass


@atexit.register
def _close_all_audit_connections() -> None:
    with _AUDIT_LOCK:
        for conn in list(_AUDIT_CONNECTION_POOL.values()):
            conn.close_pooled()
        _AUDIT_CONNECTION_POOL.clear()


def _get_audit_db_path(namespace: str) -> Path:
    clean_ns = validate_namespace(namespace)
    base_dir = ensure_secure_mesh_dir().resolve()
    raw_candidate = base_dir / f"audit_{clean_ns}.db"
    reject_symlink(raw_candidate)
    db_path = raw_candidate.resolve()
    if db_path.parent != base_dir:
        raise ValueError(f"Invalid namespace path resolution for '{namespace}'.")
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
    if not db_path.exists():
        return None
    try:
        st = db_path.stat()
        if st.st_size <= 0:
            return None
        wal_path = Path(f"{db_path}-wal")
        if wal_path.exists():
            wst = wal_path.stat()
            return (st.st_mtime_ns, st.st_size, wst.st_mtime_ns, wst.st_size)
        return (st.st_mtime_ns, st.st_size, 0, 0)
    except OSError:
        return None


def evict_audit_cache(namespace: str) -> None:
    """Evict tracked initialization state and close pooled connections for a namespace's audit DB."""
    try:
        clean_ns = validate_namespace(namespace)
        base_dir = get_mesh_dir().resolve()
        db_path = (base_dir / f"audit_{clean_ns}.db").resolve()
    except ValueError:
        return
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
        conn.execute("PRAGMA synchronous=NORMAL")
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
    try:
        from .crypto import encrypt_text_for_storage, is_encryption_enabled

        if is_encryption_enabled():
            return encrypt_text_for_storage(val)
    except Exception:
        pass
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


def _write_audit_entry(namespace: str, sql: str, params: tuple[Any, ...]) -> None:
    """
    Execute an audit log statement on the pooled connection, enforce retention limits,
    and refresh the tracked file stamp so subsequent writes never re-run schema initialization.
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
    conn.execute(
        """
        UPDATE audit_log
        SET content = ?
        WHERE namespace = ? AND memory_id = ? AND action IN ('learn', 'update')
        """,
        (enc_placeholder, namespace, memory_id),
    )
    clean_needle = (needle_text or "").strip()
    if not clean_needle:
        return
    needle_low = clean_needle.lower()
    for col in ("content", "top_result", "query"):
        conn.execute(
            f"UPDATE audit_log SET {col} = ? "
            f"WHERE namespace = ? AND {col} IS NOT NULL "
            f"AND (LOWER({col}) = ? OR INSTR(LOWER({col}), ?) > 0)",
            (enc_placeholder, namespace, needle_low, needle_low),
        )

    # Also inspect any encrypted rows (`enc:v1:...`) in Python so encrypted side channels are scrubbed
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
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.OperationalError:
                    pass
            new_stamp = _get_audit_db_stamp(db_path)
            if new_stamp is not None:
                _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
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
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.OperationalError:
                    pass
            new_stamp = _get_audit_db_stamp(db_path)
            if new_stamp is not None:
                _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
    except Exception:
        pass


def clear_audit_log(namespace: str) -> None:
    """
    Purge all rows for `namespace` from `audit_<namespace>.db`, checkpoint the WAL,
    and `VACUUM` so `Mesh.clear()` leaves zero historical audit side-channel data (Finding S12).
    """
    try:
        clean_ns = validate_namespace(namespace)
        db_path = _get_audit_db_path(clean_ns)
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
    """Record an operational telemetry event (surface, tool/cmd, status, duration, payload size, NEVER memory text)."""
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
