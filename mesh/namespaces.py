"""
mesh/namespaces.py — Namespace listing, deletion, renaming, and statistics.

Security & Integrity:
  - Validates all namespace identifiers against path traversal via `validate_namespace()`
  - Uses `_compute_row_effective_confidence()` from `store.py` for a single unified staleness definition
  - Evicts all in-memory vector, connection, audit, context, HTTP, MCP, and Dashboard caches on delete/rename
  - Enforces 0600 POSIX file permissions on renamed SQLite databases
"""
from __future__ import annotations

import gc
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from .audit import (
    MESH_DIR,
    _get_audit_db_path,
    ensure_secure_mesh_dir,
    evict_audit_cache,
    get_mesh_dir,
    secure_file_permissions,
    validate_namespace,
)
from .store import (
    _CACHE_LOCK,
    _INITIALIZED_DBS,
    _TRANSIENT_EMBEDDINGS,
    _VECTOR_CACHE,
    _compute_row_effective_confidence,
    _utc_now,
    close_store_connection,
)


def _get_all_db_paths() -> list[Path]:
    """Return paths to all valid namespace SQLite databases in ~/.mesh/"""
    mesh_dir = get_mesh_dir()
    if not mesh_dir.exists():
        return []
    results: list[Path] = []
    for p in mesh_dir.glob("mesh_*.db"):
        if not (p.is_file() and not p.name.endswith(("-wal", "-shm"))):
            continue
        name = p.stem
        ns = name[5:] if name.startswith("mesh_") else name
        try:
            validate_namespace(ns)
            results.append(p)
        except ValueError:
            continue
    return results


def _namespace_from_db_path(path: Path) -> str:
    """Extract namespace name from db filename: mesh_shared.db -> shared"""
    name = path.stem
    return name[5:] if name.startswith("mesh_") else name


def _evict_namespace_caches(namespace: str, db_path: Path) -> None:
    """
    Clear all in-memory caches and close pooled SQLite connections for `namespace`
    across store, audit, context_builder, http_server, mcp_server, and dashboard.
    Only touches already-imported server modules in `sys.modules` to preserve lazy loading.
    """
    clean_ns = validate_namespace(namespace)
    resolved_db = db_path.resolve()
    with _CACHE_LOCK:
        _VECTOR_CACHE.pop(resolved_db, None)
        _VECTOR_CACHE.pop(db_path, None)
        _INITIALIZED_DBS.pop(resolved_db, None)
        _INITIALIZED_DBS.pop(db_path, None)
        close_store_connection(resolved_db)
        close_store_connection(db_path)
        stale_keys = [k for k in _TRANSIENT_EMBEDDINGS if k[0] == clean_ns]
        for k in stale_keys:
            _TRANSIENT_EMBEDDINGS.pop(k, None)

    evict_audit_cache(clean_ns)

    ctx_mod = sys.modules.get("mesh.context_builder")
    if ctx_mod is not None and hasattr(ctx_mod, "evict_context_mesh_cache"):
        try:
            ctx_mod.evict_context_mesh_cache(clean_ns)
        except Exception:
            pass

    http_mod = sys.modules.get("mesh.http_server")
    if http_mod is not None and hasattr(http_mod, "evict_http_mesh_cache"):
        try:
            http_mod.evict_http_mesh_cache(clean_ns)
        except Exception:
            pass

    mcp_mod = sys.modules.get("mesh.mcp_server")
    if mcp_mod is not None and hasattr(mcp_mod, "evict_mcp_mesh_cache"):
        try:
            mcp_mod.evict_mcp_mesh_cache(clean_ns)
        except Exception:
            pass

    dash_mod = sys.modules.get("mesh.dashboard")
    if dash_mod is not None and hasattr(dash_mod, "evict_dashboard_mesh_cache"):
        try:
            dash_mod.evict_dashboard_mesh_cache(clean_ns)
        except Exception:
            pass


def _remove_sqlite_files(db_path: Path, strict: bool = False) -> bool:
    """Delete a SQLite database file along with any WAL or SHM sidecar files."""
    all_removed = True
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        for attempt in range(5):
            if not candidate.exists():
                break
            try:
                candidate.unlink()
                break
            except OSError:
                if attempt == 0:
                    gc.collect()
                time.sleep(0.02 * (attempt + 1))
        if candidate.exists():
            all_removed = False
            if strict and suffix == "":
                raise RuntimeError(
                    f"Failed to delete SQLite database '{candidate}'. Another process may still hold the file open."
                )
    return all_removed


def list_namespaces() -> list[dict[str, Any]]:
    """
    List all validated namespaces with their memory counts and last updated time.
    Returns:
        List of dicts with keys: namespace, memory_count, local_only_count, last_updated
    """
    results: list[dict[str, Any]] = []

    for db_path in _get_all_db_paths():
        namespace = _namespace_from_db_path(db_path)
        try:
            clean_ns = validate_namespace(namespace)
            conn = sqlite3.connect(db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) as count FROM memories WHERE namespace = ?",
                    (clean_ns,),
                )
                total = int(cursor.fetchone()["count"] or 0)

                cursor.execute(
                    "SELECT COUNT(*) as count FROM memories WHERE namespace = ? AND local_only = 1",
                    (clean_ns,),
                )
                local_only = int(cursor.fetchone()["count"] or 0)

                cursor.execute(
                    "SELECT MAX(created_at) as last FROM memories WHERE namespace = ?",
                    (clean_ns,),
                )
                row = cursor.fetchone()
                last_updated = row["last"] if row and row["last"] else "never"
                cursor.close()
            finally:
                conn.close()

            results.append(
                {
                    "namespace": clean_ns,
                    "memory_count": total,
                    "local_only_count": local_only,
                    "last_updated": last_updated,
                }
            )
        except Exception:
            pass

    results.sort(key=lambda x: x["namespace"])
    return results


def delete_namespace(namespace: str) -> int:
    """
    Delete a validated namespace and ALL its memories (plus its isolated audit trail DB).
    Returns:
        Number of memories deleted.
    """
    clean_ns = validate_namespace(namespace)
    base_dir = ensure_secure_mesh_dir().resolve()
    db_path = (base_dir / f"mesh_{clean_ns}.db").resolve()
    audit_db_path = _get_audit_db_path(clean_ns)
    count = 0

    _evict_namespace_caches(clean_ns, db_path)

    if db_path.exists():
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM memories WHERE namespace = ?", (clean_ns,))
            row = cursor.fetchone()
            count = int(row[0]) if row else 0
            cursor.close()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
        except Exception:
            count = 0
        finally:
            conn.close()

        _evict_namespace_caches(clean_ns, db_path)
        _remove_sqlite_files(db_path, strict=True)

    if audit_db_path.exists():
        conn_audit = sqlite3.connect(audit_db_path, timeout=5.0)
        try:
            try:
                conn_audit.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
        except Exception:
            pass
        finally:
            conn_audit.close()

        evict_audit_cache(clean_ns)
        _remove_sqlite_files(audit_db_path, strict=True)

    return count


def rename_namespace(old_name: str, new_name: str) -> int:
    """
    Rename a namespace safely with full validation and cache eviction.
    Returns:
        Number of memories moved.
    """
    clean_old = validate_namespace(old_name)
    clean_new = validate_namespace(new_name)
    base_dir = ensure_secure_mesh_dir().resolve()
    old_db = (base_dir / f"mesh_{clean_old}.db").resolve()
    new_db = (base_dir / f"mesh_{clean_new}.db").resolve()
    old_audit = _get_audit_db_path(clean_old)
    new_audit = _get_audit_db_path(clean_new)

    if not old_db.exists():
        raise ValueError(f"Namespace '{old_name}' does not exist")
    if new_db.exists() or new_audit.exists():
        raise ValueError(f"Namespace '{new_name}' already exists. Delete it first.")

    _evict_namespace_caches(clean_old, old_db)
    _evict_namespace_caches(clean_new, new_db)

    conn_old = sqlite3.connect(old_db, timeout=5.0)
    try:
        try:
            conn_old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
    finally:
        conn_old.close()

    shutil.copy2(old_db, new_db)
    secure_file_permissions(new_db)

    count = 0
    conn_new = sqlite3.connect(new_db, timeout=5.0)
    try:
        conn_new.execute("UPDATE memories SET namespace = ? WHERE namespace = ?", (clean_new, clean_old))
        conn_new.execute("UPDATE queries SET namespace = ? WHERE namespace = ?", (clean_new, clean_old))
        conn_new.execute(
            "UPDATE contradictions SET namespace = ? WHERE namespace = ?", (clean_new, clean_old)
        )
        conn_new.commit()

        cur = conn_new.execute(
            "SELECT COUNT(*) FROM memories WHERE namespace = ?", (clean_new,)
        )
        row = cur.fetchone()
        count = int(row[0]) if row else 0
        cur.close()
    finally:
        conn_new.close()

    secure_file_permissions(new_db)
    _evict_namespace_caches(clean_old, old_db)
    _remove_sqlite_files(old_db)

    old_audit = _get_audit_db_path(clean_old)
    new_audit = _get_audit_db_path(clean_new)
    if old_audit.exists():
        conn_audit_old = sqlite3.connect(old_audit, timeout=5.0)
        try:
            try:
                conn_audit_old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
        finally:
            conn_audit_old.close()

        shutil.copy2(old_audit, new_audit)
        secure_file_permissions(new_audit)
        conn_audit_new = sqlite3.connect(new_audit, timeout=5.0)
        try:
            conn_audit_new.execute(
                "UPDATE audit_log SET namespace = ? WHERE namespace = ?",
                (clean_new, clean_old),
            )
            conn_audit_new.commit()
        except Exception:
            pass
        finally:
            conn_audit_new.close()

        secure_file_permissions(new_audit)
        evict_audit_cache(clean_old)
        _remove_sqlite_files(old_audit)

    return count


def namespace_stats(namespace: str) -> dict[str, Any]:
    """
    Return detailed statistics for a validated namespace using the unified
    `_compute_row_effective_confidence()` staleness definition.
    """
    clean_ns = validate_namespace(namespace)
    base_dir = ensure_secure_mesh_dir().resolve()
    db_path = (base_dir / f"mesh_{clean_ns}.db").resolve()
    if not db_path.exists():
        raise ValueError(f"Namespace '{namespace}' does not exist")

    now = _utc_now()
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as c FROM memories WHERE namespace = ?", (clean_ns,))
        total = int(cursor.fetchone()["c"] or 0)

        cursor.execute(
            "SELECT COUNT(*) as c FROM memories WHERE namespace = ? AND local_only = 1",
            (clean_ns,),
        )
        local_only = int(cursor.fetchone()["c"] or 0)

        cursor.execute(
            "SELECT memory_type, COUNT(*) as c FROM memories WHERE namespace = ? GROUP BY memory_type",
            (clean_ns,),
        )
        type_rows = cursor.fetchall()
        type_breakdown = {r["memory_type"]: int(r["c"]) for r in type_rows}

        cursor.execute("SELECT AVG(confidence) as avg FROM memories WHERE namespace = ?", (clean_ns,))
        avg_conf_row = cursor.fetchone()
        avg_conf = float(avg_conf_row["avg"] or 0.0)

        cursor.execute(
            "SELECT SUM(access_count) as total FROM memories WHERE namespace = ?", (clean_ns,)
        )
        recall_row = cursor.fetchone()
        total_recalls = int(recall_row["total"] or 0)

        cursor.execute("SELECT MIN(created_at) as oldest FROM memories WHERE namespace = ?", (clean_ns,))
        oldest = cursor.fetchone()["oldest"] or "none"

        cursor.execute("SELECT MAX(created_at) as newest FROM memories WHERE namespace = ?", (clean_ns,))
        newest = cursor.fetchone()["newest"] or "none"

        cursor.execute(
            """
            SELECT created_at, decay_start_at, ttl_days, confidence
            FROM memories
            WHERE namespace = ?
            """,
            (clean_ns,),
        )
        stale = 0
        for r in cursor.fetchall():
            _, is_stale = _compute_row_effective_confidence(dict(r), now=now)
            if is_stale:
                stale += 1

        cursor.close()
    finally:
        conn.close()

    return {
        "memory_count": total,
        "local_only_count": local_only,
        "exportable_count": total - local_only,
        "type_breakdown": type_breakdown,
        "avg_confidence": avg_conf,
        "total_recalls": total_recalls,
        "oldest_memory": oldest,
        "newest_memory": newest,
        "stale_count": stale,
    }