"""
mesh/audit.py — High-speed, fault-isolated audit trail for all memory reads and writes.
Logs recall(), learn(), update(), and forget() calls with timestamp, agent, query/content, and result.
"""
from __future__ import annotations

import atexit
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

MESH_DIR = Path.home() / ".mesh"

_AUDIT_LOCK = threading.RLock()
_INITIALIZED_AUDIT_DBS: dict[Path, tuple[int, int, int, int]] = {}
_AUDIT_CONNECTION_POOL: dict[Path, "_ManagedAuditConnection"] = {}

_AUDIT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,           -- 'recall', 'learn', 'update', or 'forget'
    agent_id TEXT,
    namespace TEXT,
    query TEXT,                     -- for recalls: the query string
    content TEXT,                   -- for learns/updates: the content stored
    top_result TEXT,                -- for recalls: the top result content
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
    return MESH_DIR / f"audit_{namespace}.db"


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
    db_path = _get_audit_db_path(namespace)
    with _AUDIT_LOCK:
        _INITIALIZED_AUDIT_DBS.pop(db_path, None)
        conn = _AUDIT_CONNECTION_POOL.pop(db_path, None)
        if conn is not None:
            conn.close_pooled()


def _connect_audit(db_path: Path) -> _ManagedAuditConnection:
    with _AUDIT_LOCK:
        conn = _AUDIT_CONNECTION_POOL.get(db_path)
        if conn is not None and db_path.exists():
            return conn
        if conn is not None:
            conn.close_pooled()
            _AUDIT_CONNECTION_POOL.pop(db_path, None)

        if not MESH_DIR.exists():
            MESH_DIR.mkdir(parents=True, exist_ok=True)
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
        try:
            conn.execute("PRAGMA cache_size=-8000")
        except sqlite3.OperationalError:
            pass
        _AUDIT_CONNECTION_POOL[db_path] = conn
        return conn


def _apply_audit_schema(conn: sqlite3.Connection) -> None:
    """Create the audit_log table and indexes on an open connection."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_AUDIT_SCHEMA_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON audit_log(timestamp DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_action ON audit_log(action)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent ON audit_log(agent_id)")


def _init_audit_db(namespace: str) -> None:
    db_path = _get_audit_db_path(namespace)
    with _AUDIT_LOCK:
        with _connect_audit(db_path) as conn:
            _apply_audit_schema(conn)
            conn.commit()


def _ensure_audit_db(namespace: str) -> Path:
    """Initialize the audit database schema once per file lifecycle."""
    db_path = _get_audit_db_path(namespace)
    with _AUDIT_LOCK:
        stamp = _get_audit_db_stamp(db_path)
        if stamp is not None and _INITIALIZED_AUDIT_DBS.get(db_path) == stamp:
            return db_path
        _init_audit_db(namespace)
        new_stamp = _get_audit_db_stamp(db_path)
        if new_stamp is not None:
            _INITIALIZED_AUDIT_DBS[db_path] = new_stamp
    return db_path


def _write_audit_entry(namespace: str, sql: str, params: tuple[Any, ...]) -> None:
    """
    Execute an audit log INSERT on the pooled connection and refresh the
    tracked file stamp so subsequent writes never re-run schema initialization.
    """
    db_path = _get_audit_db_path(namespace)
    with _AUDIT_LOCK:
        stamp = _get_audit_db_stamp(db_path)
        needs_init = stamp is None or _INITIALIZED_AUDIT_DBS.get(db_path) != stamp
        with _connect_audit(db_path) as conn:
            if needs_init:
                _apply_audit_schema(conn)
            conn.execute(sql, params)
            conn.commit()
        new_stamp = _get_audit_db_stamp(db_path)
        if new_stamp is not None:
            _INITIALIZED_AUDIT_DBS[db_path] = new_stamp


def log_recall(
    query: str,
    namespace: str,
    agent_id: str,
    results: list[dict[str, Any]],
) -> None:
    """Log a recall event to the audit trail."""
    try:
        top_result = results[0]["content"] if results else None
        top_similarity = float(results[0].get("similarity", 0.0)) if results else None
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
                query,
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
) -> None:
    """Log a learn event to the audit trail."""
    try:
        _write_audit_entry(
            namespace=namespace,
            sql="""
                INSERT INTO audit_log
                (timestamp, action, agent_id, namespace, content, memory_id, had_conflict)
                VALUES (?, 'learn', ?, ?, ?, ?, ?)
            """,
            params=(
                datetime.now(timezone.utc).isoformat(),
                agent_id,
                namespace,
                content,
                result.get("memory_id"),
                1 if result.get("conflict") else 0,
            ),
        )
    except Exception:
        pass


def log_update(
    memory_id: str,
    content: Optional[str],
    namespace: str,
    agent_id: str,
) -> None:
    """Log an in-place memory update event to the audit trail."""
    try:
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
                content or f"[updated metadata for {memory_id}]",
                memory_id,
            ),
        )
    except Exception:
        pass


def log_forget(
    memory_id: str,
    namespace: str,
    agent_id: str,
) -> None:
    """Log a memory deletion event to the audit trail."""
    try:
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
    db_path = _get_audit_db_path(namespace)
    if not db_path.exists():
        return []

    action_sql = _ACTION_FILTER_MAP.get((action_filter or "all").strip().lower())
    with _AUDIT_LOCK:
        with _connect_audit(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if action_sql:
                cur = conn.execute(
                    """
                    SELECT * FROM audit_log
                    WHERE action = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                    """,
                    (action_sql, limit),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT * FROM audit_log
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
            return rows


def get_audit_stats(namespace: str) -> dict[str, Any]:
    """Return aggregate stats from the audit log in a minimal query pass."""
    db_path = _get_audit_db_path(namespace)
    if not db_path.exists():
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
                """
            )
            agg = cur_agg.fetchone()
            cur_agg.close()

            cur_agents = conn.execute(
                "SELECT DISTINCT agent_id FROM audit_log WHERE agent_id IS NOT NULL"
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