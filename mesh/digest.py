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
import sys
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
    from . import ui

    hours = int(digest["hours"])
    window = f"last {hours}h" if hours != 24 else "last 24h"
    ns = digest["namespace"]
    width = ui.term_width()

    ui.header("Digest", ns, window)

    # ── What's new ──
    ui.section(f"New  {ui.sym('dot')}  {digest['new_count']}")
    if digest["new_count"] == 0:
        ui.hint(f"Nothing stored in the {window}.", stream=sys.stdout)
    else:
        print()
        rows = []
        for mem in digest["new_memories"][:10]:
            flag = ("private", "accent") if mem.get("local_only") else ""
            rows.append(
                [
                    mem.get("short_id") or str(mem.get("id") or "")[:8],
                    (str(mem.get("memory_type", "fact")), "info"),
                    (ui.relative_time(mem.get("created_at")), "muted"),
                    (str(mem.get("source_agent", "unknown")), "muted"),
                    flag,
                    " ".join(str(mem.get("content") or "").split()),
                ]
            )
        ui.table(["ID", "Type", "When", "By", "", "Memory"], rows, flex=5)
        if digest["new_count"] > 10:
            ui.hint(f"…and {digest['new_count'] - 10} more. Browse them with `mesh inspect`.", stream=sys.stdout)

    unparseable = int(digest.get("unparseable_count") or 0)
    if unparseable > 0:
        ui.warn(
            f"{ui.plural(unparseable, 'memory', 'memories')} skipped because the timestamp couldn't be read",
            stdout=True,
        )

    # ── Activity ──
    if digest["total_recalls"] > 0:
        agents = ", ".join(digest["agents_active"]) if digest["agents_active"] else "unknown"
        ui.section("Activity")
        ui.kv(
            [
                ("Searches", f"{digest['total_recalls']}"),
                ("Active agents", agents),
            ],
            indent=ui.IND + "  ",
        )

    # ── Contradictions ──
    if digest["contradiction_count"] > 0:
        ui.section(f"Contradictions  {ui.sym('dot')}  {digest['contradiction_count']}")
        print()
        for c in digest["contradictions"]:
            ts = ui.relative_time(c.get("timestamp"))
            lines = ui.wrap(_one_line(c.get("content"), 300), width - 6, max_lines=2)
            print(f"{ui.IND}  {ui.style(ui.sym('warn'), 'warn', 'bold')} {lines[0]}")
            for extra in lines[1:]:
                print(f"{ui.IND}    {extra}")
            print(f"{ui.IND}    {ui.style('flagged by ' + str(c.get('agent_id') or 'unknown') + (' ' + ui.sym('dot') + ' ' + ts if ts else ''), 'muted')}")
        ui.hint("Resolve them with `mesh conflicts`", stream=sys.stdout)

    # ── Stale ──
    if digest["stale_count"] > 0:
        ui.section(f"Going stale  {ui.sym('dot')}  {digest['stale_count']}")
        print()
        rows = []
        for mem in digest["stale_memories"][:5]:
            why = f"TTL {mem['ttl_days']}d expired" if mem.get("ttl_days") is not None else "low confidence"
            rows.append(
                [
                    mem.get("short_id") or str(mem.get("id") or "")[:8],
                    (why, "warn"),
                    (ui.relative_time(mem.get("created_at")), "muted"),
                    " ".join(str(mem.get("content") or "").split()),
                ]
            )
        ui.table(["ID", "Why", "Stored", "Memory"], rows, flex=3)
        if digest["stale_count"] > 5:
            ui.hint(f"…and {digest['stale_count'] - 5} more.", stream=sys.stdout)
        ui.next_steps(
            [
                ("mesh update <id>", "refresh a memory that's still true"),
                ("mesh forget <id>", "remove one that isn't"),
            ]
        )

    if digest["new_count"] == 0 and digest["contradiction_count"] == 0 and digest["stale_count"] == 0:
        print()
        ui.success("All clear — Mesh is up to date")
    print()


def digest_cmd() -> None:
    """Entry point for: mesh-digest / mesh digest"""
    parser = argparse.ArgumentParser(
        prog="mesh digest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
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