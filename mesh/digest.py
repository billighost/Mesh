"""
mesh/digest.py — Timezone-safe End-of-Session Digest for Mesh.
Summarizes recent activity: what was learned, contradictions detected, and stale memories.

Security & Consistency:
  - Validates namespace identifiers via `validate_namespace()` to prevent path traversal
  - Uses `_compute_row_effective_confidence()` from `store.py` across all rows for a single unified staleness definition
  - Redacts `local_only=True` memory content by default unless `include_private=True`
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import timedelta
from typing import Any

from .audit import (
    REDACTED_CONTENT_PLACEHOLDER,
    _get_audit_db_path,
    ensure_secure_mesh_dir,
    validate_namespace,
)
from .store import (
    _compute_row_effective_confidence,
    _parse_iso_utc,
    _utc_now,
)


def generate_digest(
    namespace: str = "shared",
    hours: int = 24,
    include_private: bool = False,
) -> dict[str, Any]:
    """
    Generate a session digest for the given namespace and time window using timezone-safe UTC timestamps.
    """
    clean_ns = validate_namespace(namespace)
    safe_hours = max(1, int(hours or 24))
    now = _utc_now()
    since_dt = now - timedelta(hours=safe_hours)
    since_iso = since_dt.isoformat()

    base_dir = ensure_secure_mesh_dir().resolve()
    db_path = (base_dir / f"mesh_{clean_ns}.db").resolve()
    if db_path.parent != base_dir:
        raise ValueError(f"Invalid namespace path resolution for '{namespace}'.")

    new_memories: list[dict[str, Any]] = []
    stale_memories: list[dict[str, Any]] = []

    if db_path.exists():
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT content, memory_type, source_agent, confidence, created_at, local_only
                FROM memories
                WHERE namespace = ?
                ORDER BY created_at DESC
                """,
                (clean_ns,),
            )
            for r in cursor.fetchall():
                d = dict(r)
                dt = _parse_iso_utc(d.get("created_at"))
                if dt is not None and dt < since_dt:
                    continue
                d["local_only"] = bool(d.get("local_only"))
                if d["local_only"] and not include_private:
                    d["content"] = REDACTED_CONTENT_PLACEHOLDER
                new_memories.append(d)

            cursor.execute(
                """
                SELECT content, memory_type, created_at, decay_start_at, ttl_days, confidence, local_only
                FROM memories
                WHERE namespace = ?
                """,
                (clean_ns,),
            )
            for r in cursor.fetchall():
                row_dict = dict(r)
                eff_conf, is_stale = _compute_row_effective_confidence(row_dict, now=now)
                if is_stale:
                    row_dict["local_only"] = bool(row_dict.get("local_only"))
                    row_dict["effective_confidence"] = eff_conf
                    if row_dict["local_only"] and not include_private:
                        row_dict["content"] = REDACTED_CONTENT_PLACEHOLDER
                    stale_memories.append(row_dict)
        finally:
            conn.close()

    audit_db = _get_audit_db_path(clean_ns)
    contradictions: list[dict[str, Any]] = []
    recalls = 0
    agents_active: list[str] = []

    if audit_db.exists():
        conn = sqlite3.connect(audit_db, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT content, agent_id, timestamp
                FROM audit_log
                WHERE action = 'learn' AND had_conflict = 1
                ORDER BY timestamp DESC
                """
            )
            for r in cursor.fetchall():
                d = dict(r)
                ts = _parse_iso_utc(d.get("timestamp"))
                if ts is not None and ts >= since_dt:
                    contradictions.append(d)

            cursor.execute(
                """
                SELECT timestamp FROM audit_log
                WHERE action = 'recall'
                """
            )
            for r in cursor.fetchall():
                ts = _parse_iso_utc(r["timestamp"])
                if ts is not None and ts >= since_dt:
                    recalls += 1

            cursor.execute(
                """
                SELECT DISTINCT agent_id, timestamp FROM audit_log
                WHERE agent_id IS NOT NULL
                """
            )
            active_set: set[str] = set()
            for r in cursor.fetchall():
                ts = _parse_iso_utc(r["timestamp"])
                if ts is not None and ts >= since_dt:
                    active_set.add(r["agent_id"])
            agents_active = sorted(active_set)
        finally:
            conn.close()

    return {
        "namespace": clean_ns,
        "hours": safe_hours,
        "since": since_iso,
        "new_memories": new_memories,
        "new_count": len(new_memories),
        "contradictions": contradictions,
        "contradiction_count": len(contradictions),
        "stale_memories": stale_memories,
        "stale_count": len(stale_memories),
        "total_recalls": recalls,
        "agents_active": agents_active,
    }


def _one_line(text: Any, max_len: int = 75) -> str:
    clean = " ".join(str(text or "").split())
    return clean[:max_len]


def print_digest(digest: dict[str, Any]) -> None:
    """Print a human-readable digest to stdout."""
    hours = digest["hours"]
    window = f"last {hours}h" if hours < 24 else "today"
    ns = digest["namespace"]

    print(f"\n{'='*50}")
    print(f"  Mesh Digest — {window} — namespace: {ns}")
    print(f"{'='*50}\n")

    if digest["new_count"] == 0:
        print(f"  [OK] No new memories stored {window}.")
    else:
        print(f"  [INPUT] {digest['new_count']} new memor{'y' if digest['new_count'] == 1 else 'ies'} stored:\n")
        for mem in digest["new_memories"][:10]:
            private = " [PRIVATE]" if mem.get("local_only") else ""
            print(f"     [{mem['memory_type']}]{private} {_one_line(mem.get('content'))}")
            print(f"      -> from {mem.get('source_agent')} at {str(mem.get('created_at', ''))[:16]}")
        if digest["new_count"] > 10:
            print(f"     ... and {digest['new_count'] - 10} more")

    print()

    if digest["total_recalls"] > 0:
        agents = ", ".join(digest["agents_active"]) if digest["agents_active"] else "unknown"
        print(f"  [SEARCH] {digest['total_recalls']} memory reads by: {agents}")
        print()

    if digest["contradiction_count"] > 0:
        print(f"  [WARNING] {digest['contradiction_count']} contradiction(s) detected:\n")
        for c in digest["contradictions"]:
            print(f"     \"{_one_line(c.get('content'))}\"")
            print(f"      -> flagged by {c.get('agent_id')} at {str(c.get('timestamp', ''))[:16]}")
        print()

    if digest["stale_count"] > 0:
        print(f"  [STALE] {digest['stale_count']} memor{'y' if digest['stale_count'] == 1 else 'ies'} going stale:\n")
        for mem in digest["stale_memories"][:5]:
            ttl_label = f"TTL was {mem['ttl_days']} days" if mem.get("ttl_days") is not None else "low confidence"
            print(f"     \"{_one_line(mem.get('content'))}\"")
            print(f"      -> {ttl_label}, stored {str(mem.get('created_at', ''))[:10]}")
        print()
        print("     Run 'mesh-ask' to review and update these, or 'mesh-forget' to remove them.")

    if (
        digest["new_count"] == 0
        and digest["contradiction_count"] == 0
        and digest["stale_count"] == 0
    ):
        print("  [OK] All clear. Mesh is up to date.")

    print()


def digest_cmd() -> None:
    """Entry point for: mesh-digest"""
    parser = argparse.ArgumentParser(
        description="Show a digest of recent Mesh activity.",
        epilog="""Examples:
  mesh-digest                  # Last 24 hours
  mesh-digest --hours 2        # Last 2 hours
  mesh-digest --namespace work
  mesh-digest --json           # Machine-readable output
        """,
    )
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--hours", type=int, default=24, help="Time window in hours (default: 24)")
    parser.add_argument("--json", action="store_true", help="Output as JSON instead of human-readable")
    parser.add_argument(
        "--include-private",
        action="store_true",
        help="Include unredacted local_only memory content in the digest",
    )
    args = parser.parse_args()

    ns = validate_namespace(args.namespace or os.environ.get("MESH_NAMESPACE", "shared"))
    digest = generate_digest(namespace=ns, hours=args.hours, include_private=args.include_private)

    if args.json:
        print(json.dumps(digest, indent=2))
    else:
        print_digest(digest)