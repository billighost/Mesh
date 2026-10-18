"""
mesh/audit.py — High-speed, fault-isolated audit trail for all memory reads and writes.
Logs recall(), learn(), update(), and forget() calls with timestamp, agent, query/content, and result.
Protects `local_only` memories via automatic redaction, scrubs historical content on `update()`,
purges deleted memories on `forget()`, and enforces bounded table and connection retention.
"""
from __future__ import annotations

import atexit
import os
import re
import sqlite3
import stat
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def get_mesh_dir() -> Path:
    """Return the active Mesh data directory, respecting MESH_HOME / MESH_DIR env overrides dynamically."""
    override = (os.environ.get("MESH_HOME") or os.environ.get("MESH_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".mesh"


MESH_DIR = get_mesh_dir()
_AUDIT_LOCK = threading.RLock()
_INITIALIZED_AUDIT_DBS: dict[Path, tuple[int, int, int, int]] = {}
_AUDIT_CONNECTION_POOL: OrderedDict[Path, "_ManagedAuditConnection"] = OrderedDict()
_AUDIT_WRITE_COUNTS: dict[Path, int] = {}
_MAX_AUDIT_POOL_SIZE = 64

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

_ACTION_FILTER_MAP = {
    "read": "recall",
    "write": "learn",
    "recall": "recall",
    "learn": "learn",
    "update": "update",
    "forget": "forget",
    "delete": "forget",
}


def ensure_secure_mesh_dir(path: Optional[Path] = None) -> Path:
    """
    Create `~/.mesh` (or `path`) with mode 0700 on POSIX systems so other local users
    cannot list or read SQLite memory/audit databases or authentication tokens.
    """
    target = path if path is not None else get_mesh_dir()
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        try:
            current_mode = stat.S_IMODE(target.stat().st_mode)
            if current_mode & (stat.S_IRWXG | stat.S_IRWXO):
                os.chmod(target, stat.S_IRWXU)
        except OSError:
            pass
    return target


def secure_file_permissions(file_path: Path) -> None:
    """Enforce 0600 permissions on a SQLite database and any WAL/SHM sidecar files on POSIX."""
    if os.name == "nt":
        return
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{file_path}{suffix}")
        if candidate.exists():
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
    db_path = (base_dir / f"audit_{clean_ns}.db").resolve()
    if db_path.parent != base_dir:
        raise ValueError(f"Invalid namespace path resolution for '{namespace}'.")
    return db_path


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
    """Create the audit_log table and indexes on an open connection."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_AUDIT_SCHEMA_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON audit_log(timestamp DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_action ON audit_log(action)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent ON audit_log(agent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_id ON audit_log(memory_id)")


def _connect_audit(db_path: Path) -> _ManagedAuditConnection:
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
    """Keep at most `max_rows` recent rows in `audit_log` so the database never grows without bound."""
    try:
        row = conn.execute("SELECT MAX(id), COUNT(*) FROM audit_log").fetchone()
        if not row or row[0] is None:
            return
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
    """Log a recall event to the audit trail, redacting local_only top results and queries."""
    try:
        top_result: Optional[str] = None
        top_similarity: Optional[float] = None
        if results:
            first = results[0]
            top_similarity = float(first.get("similarity", 0.0))
            if first.get("local_only"):
                top_result = REDACTED_CONTENT_PLACEHOLDER
            else:
                top_result = first.get("content")
        # If any returned memory is private, the query may itself reproduce private
        # content (a search for the exact text). Redact it rather than logging it.
        logged_query = (
            REDACTED_CONTENT_PLACEHOLDER
            if any(r.get("local_only") for r in results)
            else query
        )
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
                logged_query,
                top_result,
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
        logged_content = REDACTED_CONTENT_PLACEHOLDER if is_private else content
        had_conflict = 1 if result.get("conflict") else 0
        now_ts = datetime.now(timezone.utc).isoformat()
        if mem_id and not result.get("deduplicated"):
            db_path = _get_audit_db_path(namespace)
            with _AUDIT_LOCK:
                if db_path.exists() and _get_audit_db_stamp(db_path) is not None:
                    with _connect_audit(db_path) as conn:
                        row = conn.execute(
                            "SELECT id, action, memory_id FROM audit_log "
                            "WHERE namespace = ? ORDER BY id DESC LIMIT 1",
                            (namespace,),
                        ).fetchone()
                        if row and row[1] == "learn" and row[2] == mem_id:
                            conn.execute(
                                """
                                UPDATE audit_log
                                SET timestamp = ?, agent_id = ?, content = ?, had_conflict = ?
                                WHERE id = ?
                                """,
                                (now_ts, agent_id, logged_content, had_conflict, row[0]),
                            )
                            conn.commit()
                            secure_file_permissions(db_path)
                            new_stamp = _get_audit_db_stamp(db_path)
                            if new_stamp is not None:
                                _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
                            return

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
                logged_content,
                mem_id,
                had_conflict,
            ),
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
        db_path = _get_audit_db_path(namespace)
        if not db_path.exists() or _get_audit_db_stamp(db_path) is None:
            return
        placeholder = REDACTED_CONTENT_PLACEHOLDER if local_only else UPDATED_CONTENT_PLACEHOLDER
        with _AUDIT_LOCK:
            with _connect_audit(db_path) as conn:
                if local_only or (old_content is not None and old_content.strip()):
                    conn.execute(
                        """
                        UPDATE audit_log
                        SET content = ?
                        WHERE namespace = ? AND memory_id = ? AND action IN ('learn', 'update')
                        """,
                        (placeholder, namespace, memory_id),
                    )
                if old_content and old_content.strip():
                    clean_old = old_content.strip()
                    conn.execute(
                        "UPDATE audit_log SET content = ? WHERE namespace = ? AND content = ?",
                        (placeholder, namespace, clean_old),
                    )
                    conn.execute(
                        "UPDATE audit_log SET top_result = ? WHERE namespace = ? AND top_result = ?",
                        (placeholder, namespace, clean_old),
                    )
                    conn.execute(
                        "UPDATE audit_log SET query = ? WHERE namespace = ? AND query = ?",
                        (placeholder, namespace, clean_old),
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
            logged_content = content or f"[updated metadata for {memory_id}]"
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
                logged_content,
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
    Scrub all historical occurrences of a forgotten memory's content and top_result
    from the audit trail so `forget()` guarantees complete erasure.
    """
    try:
        namespace = validate_namespace(namespace)
        db_path = _get_audit_db_path(namespace)
        if not db_path.exists() or _get_audit_db_stamp(db_path) is None:
            return
        with _AUDIT_LOCK:
            with _connect_audit(db_path) as conn:
                conn.execute(
                    """
                    UPDATE audit_log
                    SET content = ?
                    WHERE namespace = ? AND memory_id = ? AND action IN ('learn', 'update')
                    """,
                    (PURGED_CONTENT_PLACEHOLDER, namespace, memory_id),
                )
                if content and content.strip():
                    clean = content.strip()
                    conn.execute(
                        "UPDATE audit_log SET content = ? WHERE namespace = ? AND content = ?",
                        (PURGED_CONTENT_PLACEHOLDER, namespace, clean),
                    )
                    conn.execute(
                        "UPDATE audit_log SET top_result = ? WHERE namespace = ? AND top_result = ?",
                        (PURGED_CONTENT_PLACEHOLDER, namespace, clean),
                    )
                    conn.execute(
                        "UPDATE audit_log SET query = ? WHERE namespace = ? AND query = ?",
                        (PURGED_CONTENT_PLACEHOLDER, namespace, clean),
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
                f"[deleted memory {memory_id}]",
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
    Return recent audit entries for a namespace, most recent first.

    Args:
        namespace: The namespace to query.
        limit: Max entries to return.
        action_filter: "all", "read" (maps to recall), "write" (maps to learn),
                       "delete" (maps to forget), or an explicit action
                       ("recall", "learn", "update", "forget").
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