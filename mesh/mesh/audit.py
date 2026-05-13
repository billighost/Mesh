"""
mesh/audit.py — Audit trail for all memory reads and writes.
Logs every recall() and learn() call with timestamp, agent, query/content, and result.
"""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MESH_DIR = Path.home() / ".mesh"


def _get_audit_db_path(namespace: str) -> Path:
    return MESH_DIR / f"audit_{namespace}.db"


def _init_audit_db(namespace: str):
    db_path = _get_audit_db_path(namespace)
    MESH_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,           -- 'recall' or 'learn'
            agent_id TEXT,
            namespace TEXT,
            query TEXT,                     -- for recalls: the query string
            content TEXT,                   -- for learns: the content stored
            top_result TEXT,                -- for recalls: the top result content
            top_similarity REAL,            -- for recalls: top result similarity
            result_count INTEGER,           -- for recalls: number of results
            memory_id TEXT,                 -- for learns: the stored memory ID
            had_conflict INTEGER DEFAULT 0  -- for learns: 1 if contradiction detected
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON audit_log(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_action ON audit_log(action)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent ON audit_log(agent_id)")
    conn.commit()
    conn.close()


def log_recall(
    query: str,
    namespace: str,
    agent_id: str,
    results: list
):
    """Log a recall event to the audit trail."""
    try:
        _init_audit_db(namespace)
        db_path = _get_audit_db_path(namespace)
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        top_result = results[0]["content"] if results else None
        top_similarity = results[0].get("similarity", 0.0) if results else None
        
        cursor.execute("""
            INSERT INTO audit_log 
            (timestamp, action, agent_id, namespace, query, top_result, top_similarity, result_count)
            VALUES (?, 'recall', ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            agent_id,
            namespace,
            query,
            top_result,
            top_similarity,
            len(results)
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass  # Audit logging must never crash the main flow


def log_learn(
    content: str,
    namespace: str,
    agent_id: str,
    result: dict
):
    """Log a learn event to the audit trail."""
    try:
        _init_audit_db(namespace)
        db_path = _get_audit_db_path(namespace)
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO audit_log 
            (timestamp, action, agent_id, namespace, content, memory_id, had_conflict)
            VALUES (?, 'learn', ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            agent_id,
            namespace,
            content,
            result.get("memory_id"),
            1 if result.get("conflict") else 0
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_audit_log(
    namespace: str,
    limit: int = 50,
    action_filter: str = "all"
) -> list[dict]:
    """
    Return recent audit entries for a namespace, most recent first.
    
    Args:
        namespace: The namespace to query.
        limit: Max entries to return.
        action_filter: "all", "read" (maps to recall), or "write" (maps to learn).
    """
    db_path = _get_audit_db_path(namespace)
    if not db_path.exists():
        return []
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    action_map = {"read": "recall", "write": "learn"}
    action_sql = action_map.get(action_filter)
    
    if action_sql:
        cursor.execute("""
            SELECT * FROM audit_log 
            WHERE action = ?
            ORDER BY timestamp DESC 
            LIMIT ?
        """, (action_sql, limit))
    else:
        cursor.execute("""
            SELECT * FROM audit_log 
            ORDER BY timestamp DESC 
            LIMIT ?
        """, (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_audit_stats(namespace: str) -> dict:
    """Return aggregate stats from the audit log."""
    db_path = _get_audit_db_path(namespace)
    if not db_path.exists():
        return {"total_recalls": 0, "total_learns": 0, "agents": [], "conflicts_detected": 0}
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) as c FROM audit_log WHERE action = 'recall'")
    recalls = cursor.fetchone()["c"]
    
    cursor.execute("SELECT COUNT(*) as c FROM audit_log WHERE action = 'learn'")
    learns = cursor.fetchone()["c"]
    
    cursor.execute("SELECT DISTINCT agent_id FROM audit_log WHERE agent_id IS NOT NULL")
    agents = [r["agent_id"] for r in cursor.fetchall()]
    
    cursor.execute("SELECT COUNT(*) as c FROM audit_log WHERE had_conflict = 1")
    conflicts = cursor.fetchone()["c"]
    
    cursor.execute("SELECT MAX(timestamp) as last FROM audit_log")
    last = cursor.fetchone()["last"]
    
    conn.close()
    
    return {
        "total_recalls": recalls,
        "total_learns": learns,
        "agents": agents,
        "conflicts_detected": conflicts,
        "last_activity": last
    }
