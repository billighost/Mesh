"""
mesh/digest.py — Timezone-safe End-of-Session Digest for Mesh.

Summarizes recent activity: what was learned, contradictions detected, and stale memories.
Security & Consistency:
  - Validates namespace identifiers via `validate_namespace()` to prevent path traversal
  - Flushes buffered telemetry (`flush_all_telemetry`) before reading database state
  - Uses `_compute_row_effective_confidence()` from `store.py` across all rows for a single unified staleness definition
  - Excludes rows with unparseable `created_at` timestamps from `new_memories` and counts them in `unparseable_count`
  - Includes 8-character `short_id`s in `new_memories` and `stale_memories`
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
    flush_all_telemetry,
)


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def generate_digest(
    namespace: str = "shared",
    hours: int = 24,
    include_private: bool = False,
    filter_stale_by_window: bool = False,
) -> dict[str, Any]:
    """
    Generate a session digest for the given namespace and time window using timezone-safe UTC timestamps.
    Never creates a `.db` file if the namespace does not exist.
    """
    clean_ns = validate_namespace(namespace)
    flush_all_telemetry()
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
    unparseable_count = 0

    if db_path.exists():
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            if _has_table(conn, "memories"):
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, content, memory_type, source_agent, confidence,
                           created_at, decay_start_at, ttl_days, local_only
                    FROM memories
                    WHERE namespace = ?
                    ORDER BY created_at DESC
                    """,
                    (clean_ns,),
                )
                all_rows = [dict(r) for r in cursor.fetchall()]
                cursor.close()

                for row in all_rows:
                    mid = str(row.get("id") or "")
                    short_id = mid[:8]
                    is_priv = bool(row.get("local_only"))
                    display_content = (
                        REDACTED_CONTENT_PLACEHOLDER
                        if (is_priv and not include_private)
                        else row.get("content", "")
                    )

                    created_dt = _parse_iso_utc(row.get("created_at"))
                    if created_dt is None:
                        unparseable_count += 1
                    elif created_dt >= since_dt:
                        new_memories.append(
                            {
                                "id": mid,
                                "short_id": short_id,
                                "content": display_content,
                                "memory_type": row.get("memory_type", "fact"),
                                "source_agent": row.get("source_agent", "unknown"),
                                "confidence": float(row.get("confidence", 1.0)),
                                "created_at": row.get("created_at", ""),
                                "local_only": is_priv,
                            }
                        )

                    eff_conf, is_stale = _compute_row_effective_confidence(row, now=now)
                    if is_stale:
                        if filter_stale_by_window and (created_dt is None or created_dt < since_dt):
                            continue
                        stale_memories.append(
                            {
                                "id": mid,
                                "short_id": short_id,
                                "content": display_content,
                                "memory_type": row.get("memory_type", "fact"),
                                "created_at": row.get("created_at", ""),
                                "decay_start_at": row.get("decay_start_at"),
                                "ttl_days": row.get("ttl_days"),
                                "confidence": float(row.get("confidence", 1.0)),
                                "effective_confidence": eff_conf,
                                "local_only": is_priv,
                            }
                        )
        except sqlite3.Error:
            pass
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
            if _has_table(conn, "audit_log"):
                cursor = conn.cursor()
                # Push coarse ISO date prefix filter into SQL, then verify exact UTC datetime in Python
                sql_floor = (since_dt - timedelta(days=1)).strftime("%Y-%m-%d")
                cursor.execute(
                    """
                    SELECT action, content, agent_id, timestamp, had_conflict
                    FROM audit_log
                    WHERE timestamp >= ?
                    ORDER BY timestamp DESC
                    """,
                    (sql_floor,),
                )
                active_set: set[str] = set()
                for r in cursor.fetchall():
                    ts = _parse_iso_utc(r["timestamp"])
                    if ts is None or ts < since_dt:
                        continue
                    act = r["action"]
                    aid = r["agent_id"]
                    if aid:
                        active_set.add(str(aid))
                    if act == "recall":
                        recalls += 1
                    elif act == "learn" and int(r["had_conflict"] or 0) == 1:
                        contradictions.append(
                            {
                                "content": r["content"],
                                "agent_id": aid,
                                "timestamp": r["timestamp"],
                            }
                        )
                cursor.close()
                agents_active = sorted(active_set)
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    return {
        "namespace": clean_ns,
        "hours": safe_hours,
        "since": since_iso,
        "new_memories": new_memories,
        "new_count": len(new_memories),
        "unparseable_count": unparseable_count,
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
    hours = int(digest["hours"])
    window = f"last {hours}h"
    ns = digest["namespace"]
    print(f"\n{'='*50}")
    print(f"  Mesh Digest — {window} — namespace: {ns}")
    print(f"{'='*50}\n")

    if digest["new_count"] == 0:
        print(f"  [OK] No new memories stored in the {window}.")
    else:
        print(f"  [INPUT] {digest['new_count']} new memor{'y' if digest['new_count'] == 1 else 'ies'} stored ({window}):\n")
        for mem in digest["new_memories"][:10]:
            private = " [PRIVATE]" if mem.get("local_only") else ""
            short_id = mem.get("short_id") or str(mem.get("id") or "")[:8]
            id_label = f" (id: {short_id})" if short_id else ""
            print(f"     [{mem['memory_type']}]{private}{id_label} {_one_line(mem.get('content'))}")
            print(f"      -> from {mem.get('source_agent')} at {str(mem.get('created_at', ''))[:16]}")
        if digest["new_count"] > 10:
            print(f"     ... and {digest['new_count'] - 10} more")
    print()

    unparseable = int(digest.get("unparseable_count") or 0)
    if unparseable > 0:
        print(f"  [NOTE] Skipped {unparseable} memor{'y' if unparseable == 1 else 'ies'} with unparseable created_at timestamps.\n")

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
        print(f"  [STALE] {digest['stale_count']} memor{'y' if digest['stale_count'] == 1 else 'ies'} going stale (all time):\n")
        for mem in digest["stale_memories"][:5]:
            short_id = mem.get("short_id") or str(mem.get("id") or "")[:8]
            id_label = f" [id: {short_id}]" if short_id else ""
            ttl_label = f"TTL was {mem['ttl_days']} days" if mem.get("ttl_days") is not None else "low confidence"
            print(f"    {id_label} \"{_one_line(mem.get('content'))}\"")
            print(f"      -> {ttl_label}, stored {str(mem.get('created_at', ''))[:10]}")
        print()
        print("     Run 'mesh ask' to review and update these, or 'mesh forget' to remove them.")

    if (
        digest["new_count"] == 0
        and digest["contradiction_count"] == 0
        and digest["stale_count"] == 0
    ):
        print("  [OK] All clear. Mesh is up to date.")
    print()


def digest_cmd() -> None:
    """Entry point for: mesh-digest / mesh digest"""
    parser = argparse.ArgumentParser(
        description="Show a digest of recent Mesh activity.",
        epilog="""Examples:
  mesh digest                  # Last 24 hours
  mesh digest --hours 2        # Last 2 hours
  mesh digest --namespace work
  mesh digest --json           # Machine-readable output
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
    parser.add_argument(
        "--window-stale",
        action="store_true",
        help="Filter stale memories to only those created within the time window",
    )
    args = parser.parse_args()
    ns = validate_namespace(args.namespace or os.environ.get("MESH_NAMESPACE", "shared"))
    digest = generate_digest(
        namespace=ns,
        hours=args.hours,
        include_private=args.include_private,
        filter_stale_by_window=args.window_stale,
    )
    if args.json:
        print(json.dumps(digest, indent=2))
    else:
        print_digest(digest)