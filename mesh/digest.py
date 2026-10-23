"""
mesh/digest.py — Timezone-safe End-of-Session Digest for Mesh.

Summarizes recent activity: what was learned, contradictions detected, and stale memories.
Security & Consistency:
  - Validates namespace identifiers via `validate_namespace()` and rejects symlinks (`reject_symlink`)
  - Flushes buffered telemetry (`flush_all_telemetry`) before reading database state
  - Decrypts encrypted-at-rest fields transparently via `mesh.crypto` (Finding S5)
  - Excludes `quarantined=1` memories from active digest counts by default (Finding S8)
  - Reads unresolved contradictions from the `contradictions` table (falling back to `audit_log`
    only when the `contradictions` table is absent) so resolved conflicts are not re-reported (Finding S12)
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
    _connect_audit,
    _get_audit_db_path,
    ensure_secure_mesh_dir,
    reject_symlink,
    validate_namespace,
)
from .crypto import decrypt_text_if_encrypted
from .store import (
    MeshStore,
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
    include_quarantined: bool = False,
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
    raw_candidate = base_dir / f"mesh_{clean_ns}.db"
    reject_symlink(raw_candidate)
    db_path = raw_candidate.resolve()
    if db_path.parent != base_dir:
        raise ValueError(f"Invalid namespace path resolution for '{namespace}'.")

    new_memories: list[dict[str, Any]] = []
    stale_memories: list[dict[str, Any]] = []
    contradictions: list[dict[str, Any]] = []
    has_contradictions_table = False
    unparseable_count = 0
    quarantined_count = 0
    namespace_exists = db_path.exists()
    read_errors: list[str] = []

    if namespace_exists:
        store = None
        all_rows = []
        try:
            store = MeshStore(namespace=clean_ns, create=False)
            cache = store._get_vector_cache()
            if cache is not None and cache.rows_by_id:
                all_rows = sorted(
                    [dict(r) for r in cache.rows_by_id.values()],
                    key=lambda r: str(r.get("created_at") or ""),
                    reverse=True,
                )
        except Exception:
            store = None
            all_rows = []

        try:
            if store is not None and not all_rows:
                with store._connect() as conn:
                    if _has_table(conn, "memories"):
                        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
                        select_cols = (
                            "id, content, memory_type, source_agent, confidence, "
                            "created_at, decay_start_at, ttl_days, local_only"
                        )
                        if "quarantined" in cols:
                            select_cols += ", quarantined"
                        if "trust_tier" in cols:
                            select_cols += ", trust_tier"
                        cursor = conn.cursor()
                        cursor.execute(
                            f"""
                            SELECT {select_cols}
                            FROM memories
                            WHERE namespace = ?
                            ORDER BY created_at DESC
                            """,
                            (clean_ns,),
                        )
                        all_rows = [dict(r) for r in cursor.fetchall()]
                        cursor.close()

            for row in all_rows:
                is_quarantined = bool(row.get("quarantined", 0))
                if is_quarantined:
                    quarantined_count += 1
                    if not include_quarantined:
                        continue

                mid = str(row.get("id") or "")
                short_id = mid[:8]
                is_priv = bool(row.get("local_only"))
                plain_content = decrypt_text_if_encrypted(row.get("content", ""))
                display_content = (
                    REDACTED_CONTENT_PLACEHOLDER
                    if (is_priv and not include_private)
                    else plain_content
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
                            "trust_tier": row.get("trust_tier", "user"),
                            "quarantined": is_quarantined,
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

            if store is not None:
                with store._connect() as conn:
                    if _has_table(conn, "contradictions"):
                        has_contradictions_table = True
                        c_cur = conn.execute(
                            """
                            SELECT id, new_content, existing_content, detected_at
                            FROM contradictions
                            WHERE namespace = ? AND COALESCE(resolved, 0) = 0
                            ORDER BY detected_at DESC
                            """,
                            (clean_ns,),
                        )
                        for cr in c_cur.fetchall():
                            det_dt = _parse_iso_utc(cr["detected_at"])
                            if det_dt is not None and det_dt >= since_dt:
                                new_c = decrypt_text_if_encrypted(cr["new_content"])
                                if not include_private and new_c == REDACTED_CONTENT_PLACEHOLDER:
                                    display_c = REDACTED_CONTENT_PLACEHOLDER
                                else:
                                    display_c = new_c
                                contradictions.append(
                                    {
                                        "id": cr["id"],
                                        "content": display_c,
                                        "agent_id": "memory-store",
                                        "timestamp": cr["detected_at"],
                                    }
                                )
                        c_cur.close()
        except sqlite3.Error as exc:
            read_errors.append(f"memory database: {exc}")

    audit_db = _get_audit_db_path(clean_ns)
    recalls = 0
    agents_active: list[str] = []

    if audit_db.exists():
        try:
            conn = _connect_audit(audit_db)
            conn.row_factory = sqlite3.Row
            if _has_table(conn, "audit_log"):
                cursor = conn.cursor()
                sql_floor = (since_dt - timedelta(days=1)).strftime("%Y-%m-%d")
                cursor.execute(
                    """
                    SELECT action, content, agent_id, timestamp, had_conflict
                    FROM audit_log
                    WHERE namespace = ? AND timestamp >= ?
                    ORDER BY timestamp DESC
                    """,
                    (clean_ns, sql_floor),
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
                    elif (
                        not has_contradictions_table
                        and act == "learn"
                        and int(r["had_conflict"] or 0) == 1
                    ):
                        contradictions.append(
                            {
                                "content": decrypt_text_if_encrypted(r["content"]),
                                "agent_id": aid,
                                "timestamp": r["timestamp"],
                            }
                        )
                cursor.close()
                agents_active = sorted(active_set)
        except sqlite3.Error as exc:
            read_errors.append(f"audit log: {exc}")

    return {
        "namespace": clean_ns,
        "hours": safe_hours,
        "since": since_iso,
        "new_memories": new_memories,
        "new_count": len(new_memories),
        "quarantined_count": quarantined_count,
        "unparseable_count": unparseable_count,
        "contradictions": contradictions,
        "contradiction_count": len(contradictions),
        "stale_memories": stale_memories,
        "stale_count": len(stale_memories),
        "total_recalls": recalls,
        "agents_active": agents_active,
        "namespace_exists": namespace_exists,
        "read_errors": read_errors,
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

    if not digest.get("namespace_exists", True) and not (
        digest["new_count"]
        or digest["contradiction_count"]
        or digest["stale_count"]
        or digest["total_recalls"]
    ):
        ui.hint(f"Namespace '{ns}' has no memories yet.", stream=sys.stdout)
        print()
        return

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

    if int(digest.get("quarantined_count") or 0) > 0:
        q_cnt = int(digest["quarantined_count"])
        ui.warn(
            f"{ui.plural(q_cnt, 'quarantined memory', 'quarantined memories')} awaiting review",
            "Run `mesh inspect --quarantined` and `mesh approve <id>`.",
            stdout=True,
        )

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

    read_errors = digest.get("read_errors") or []
    if read_errors:
        ui.warn(
            "Some Mesh data could not be read; this digest may be incomplete",
            "\n".join(str(err) for err in read_errors),
            stdout=True,
        )

    if (
        digest.get("namespace_exists", True)
        and not read_errors
        and digest["new_count"] == 0
        and digest["contradiction_count"] == 0
        and digest["stale_count"] == 0
        and int(digest.get("unparseable_count") or 0) == 0
        and int(digest.get("quarantined_count") or 0) == 0
    ):
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
