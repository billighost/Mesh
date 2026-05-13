"""
mesh/digest.py — End-of-session digest.
Summarizes recent activity: what was learned, contradictions detected, stale memories.

Usage:
    mesh-digest                         # Digest for today
    mesh-digest --hours 2               # Last 2 hours only
    mesh-digest --namespace work
"""

import sys
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

MESH_DIR = Path.home() / ".mesh"


def generate_digest(
    namespace: str = "shared",
    hours: int = 24
) -> dict:
    """
    Generate a session digest for the given namespace and time window.
    
    Returns:
        Dict with: new_memories, contradictions, stale_memories, recalls, agents_active
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    
    # Get new memories from SQLite
    db_path = MESH_DIR / f"mesh_{namespace}.db"
    new_memories = []
    contradictions = []
    stale_memories = []
    
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # New memories in window
        cursor.execute("""
            SELECT content, memory_type, source_agent, confidence, created_at, local_only
            FROM memories 
            WHERE created_at >= ?
            ORDER BY created_at DESC
        """, (since,))
        new_memories = [dict(r) for r in cursor.fetchall()]
        
        # Stale memories (past TTL)
        cursor.execute("""
            SELECT content, memory_type, created_at, ttl_days
            FROM memories
            WHERE ttl_days IS NOT NULL
            AND julianday('now') - julianday(created_at) > ttl_days
            ORDER BY created_at ASC
        """)
        stale_memories = [dict(r) for r in cursor.fetchall()]
        
        conn.close()
    
    # Get contradictions from audit log
    audit_db = MESH_DIR / f"audit_{namespace}.db"
    recalls = 0
    agents_active = []
    
    if audit_db.exists():
        conn = sqlite3.connect(audit_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Contradictions detected during window
        cursor.execute("""
            SELECT content, agent_id, timestamp
            FROM audit_log
            WHERE action = 'learn' AND had_conflict = 1 AND timestamp >= ?
            ORDER BY timestamp DESC
        """, (since,))
        contradictions = [dict(r) for r in cursor.fetchall()]
        
        # Recall count
        cursor.execute("""
            SELECT COUNT(*) as c FROM audit_log 
            WHERE action = 'recall' AND timestamp >= ?
        """, (since,))
        recalls = cursor.fetchone()["c"]
        
        # Active agents
        cursor.execute("""
            SELECT DISTINCT agent_id FROM audit_log
            WHERE timestamp >= ? AND agent_id IS NOT NULL
        """, (since,))
        agents_active = [r["agent_id"] for r in cursor.fetchall()]
        
        conn.close()
    
    return {
        "namespace": namespace,
        "hours": hours,
        "since": since,
        "new_memories": new_memories,
        "new_count": len(new_memories),
        "contradictions": contradictions,
        "contradiction_count": len(contradictions),
        "stale_memories": stale_memories,
        "stale_count": len(stale_memories),
        "total_recalls": recalls,
        "agents_active": agents_active
    }


def print_digest(digest: dict):
    """Print a human-readable digest to stdout."""
    hours = digest["hours"]
    window = f"last {hours}h" if hours < 24 else "today"
    ns = digest["namespace"]
    
    print(f"\n{'='*50}")
    print(f"  Mesh Digest — {window} — namespace: {ns}")
    print(f"{'='*50}\n")
    
    # New memories
    if digest["new_count"] == 0:
        print(f"  📭  No new memories stored {window}.")
    else:
        print(f"  📥  {digest['new_count']} new memor{'y' if digest['new_count'] == 1 else 'ies'} stored:\n")
        for mem in digest["new_memories"][:10]:
            private = " 🔒" if mem.get("local_only") else ""
            print(f"     [{mem['memory_type']}]{private} {mem['content'][:75]}")
            print(f"      → from {mem['source_agent']} at {mem['created_at'][:16]}")
        if digest["new_count"] > 10:
            print(f"     ... and {digest['new_count'] - 10} more")
    
    print()
    
    # Recalls
    if digest["total_recalls"] > 0:
        agents = ", ".join(digest["agents_active"]) if digest["agents_active"] else "unknown"
        print(f"  🔍  {digest['total_recalls']} memory reads by: {agents}")
        print()
    
    # Contradictions
    if digest["contradiction_count"] > 0:
        print(f"  ⚠️   {digest['contradiction_count']} contradiction(s) detected:\n")
        for c in digest["contradictions"]:
            print(f"     \"{c['content'][:75]}\"")
            print(f"      → flagged by {c['agent_id']} at {c['timestamp'][:16]}")
        print()
    
    # Stale memories
    if digest["stale_count"] > 0:
        print(f"  🕰️   {digest['stale_count']} memor{'y' if digest['stale_count'] == 1 else 'ies'} going stale:\n")
        for mem in digest["stale_memories"][:5]:
            print(f"     \"{mem['content'][:75]}\"")
            print(f"      → TTL was {mem['ttl_days']} days, stored {mem['created_at'][:10]}")
        print()
        print(f"     Run 'mesh-ask' to review and update these, or 'mesh-forget' to remove them.")
    
    if digest["new_count"] == 0 and digest["contradiction_count"] == 0 and digest["stale_count"] == 0:
        print(f"  ✓   All clear. Mesh is up to date.")
    
    print()


def digest_cmd():
    """Entry point for: mesh-digest"""
    import argparse
    parser = argparse.ArgumentParser(
        description="Show a digest of recent Mesh activity.",
        epilog="""Examples:
  mesh-digest                  # Last 24 hours
  mesh-digest --hours 2        # Last 2 hours
  mesh-digest --namespace work
  mesh-digest --json           # Machine-readable output
        """
    )
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--hours", type=int, default=24,
                        help="Time window in hours (default: 24)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON instead of human-readable")
    args = parser.parse_args()

    ns = args.namespace or os.environ.get("MESH_NAMESPACE", "shared")
    digest = generate_digest(namespace=ns, hours=args.hours)
    
    if args.json:
        import json
        print(json.dumps(digest, indent=2))
    else:
        print_digest(digest)
