"""
mesh/audit.py — High-speed, fault-isolated audit trail for all memory reads and writes.
Logs recall(), learn(), update(), and forget() calls with timestamp, agent, query/content, and result.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

MESH_DIR = Path.home() / ".mesh"
_AUDIT_LOCK = threading.RLock()
_INITIALIZED_AUDIT_DBS: dict[Path, int] = {}

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
}


class _ManagedAuditConnection(sqlite3.Connection):
    """SQLite connection that commits/rolls back AND closes when exiting a `with` block."""

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            try:
                self.close()
            except Exception:
                pass


def _get_audit_db_path(namespace: str) -> Path:
    return MESH_DIR / f"audit_{namespace}.db"


def _connect_audit(db_path: Path) -> sqlite3.Connection:
    if not MESH_DIR.exists():
        MESH_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0, factory=_ManagedAuditConnection)
    if db_path not in _INITIALIZED_AUDIT_DBS:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=3000")
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
    with _connect_audit(db_path) as conn:
        _apply_audit_schema(conn)
        conn.commit()


def _ensure_audit_db(namespace: str) -> Path:
    """Initialize the audit database schema once per file lifecycle."""
    db_path = _get_audit_db_path(namespace)
    with _AUDIT_LOCK:
        if db_path.exists():
            try:
                stat = db_path.stat()
                if stat.st_size > 0 and _INITIALIZED_AUDIT_DBS.get(db_path) == stat.st_mtime_ns:
                    return db_path
            except OSError:
                pass
        _init_audit_db(namespace)
        if db_path.exists():
            try:
                _INITIALIZED_AUDIT_DBS[db_path] = db_path.stat().st_mtime_ns
            except OSError:
                pass
    return db_path


def _write_audit_entry(namespace: str, sql: str, params: tuple[Any, ...]) -> None:
    """
    Execute an audit log INSERT in a single connection pass and refresh the
    tracked file mtime so subsequent writes never re-run schema initialization.
    """
    db_path = _get_audit_db_path(namespace)
    with _AUDIT_LOCK:
        needs_init = True
        if db_path.exists():
            try:
                stat = db_path.stat()
                if stat.st_size > 0 and _INITIALIZED_AUDIT_DBS.get(db_path) == stat.st_mtime_ns:
                    needs_init = False
            except OSError:
                needs_init = True

        with _connect_audit(db_path) as conn:
            if needs_init:
                _apply_audit_schema(conn)
            conn.execute(sql, params)
            conn.commit()

        try:
            _INITIALIZED_AUDIT_DBS[db_path] = db_path.stat().st_mtime_ns
        except OSError:
            pass


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
                       or an explicit action ("recall", "learn", "update", "forget").
    """
    db_path = _get_audit_db_path(namespace)
    if not db_path.exists():
        return []

    action_sql = _ACTION_FILTER_MAP.get(action_filter)
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
            "agents": [],
            "conflicts_detected": 0,
            "last_activity": None,
        }

    with _connect_audit(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur_agg = conn.execute(
            """
            SELECT
                 COALESCE(SUM(CASE WHEN action = 'recall' THEN 1 ELSE 0 END), 0) AS recalls,
                 COALESCE(SUM(CASE WHEN action = 'learn' THEN 1 ELSE 0 END), 0) AS learns,
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
            "agents": agents,
            "conflicts_detected": int(agg["conflicts"]) if agg else 0,
            "last_activity": agg["last_activity"] if agg else None,
        }