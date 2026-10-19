"""
mesh/namespaces.py — Namespace listing, deletion, renaming, and statistics.

Security & Integrity:
  - Validates all namespace identifiers against path traversal via `validate_namespace()`
  - Flushes buffered telemetry (`flush_all_telemetry`) before reading stats, deleting, or renaming
  - Uses `_compute_row_effective_confidence()` from `store.py` for a single unified staleness definition
  - Evicts all in-memory vector, connection, lock, audit, context, HTTP, MCP, and Dashboard caches on delete/rename
  - Cleans up advisory `.mesh_<ns>.lock` files and enforces atomic rollback on failed `rename_namespace()`
  - Enforces 0600 POSIX file permissions on renamed SQLite databases
"""
from __future__ import annotations

import gc
import logging
import shutil
import sqlite3
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Optional

from .audit import (
    _get_audit_db_path,
    ensure_secure_mesh_dir,
    evict_audit_cache,
    get_mesh_dir,
    secure_file_permissions,
    validate_namespace,
)
from .store import (
    NamespaceLimitExceededError,
    NamespaceNotFoundError,
    _CACHE_LOCK,
    _CONNECTION_POOL,
    _INITIALIZED_DBS,
    _TRANSIENT_EMBEDDINGS,
    _VECTOR_CACHE,
    _compute_row_effective_confidence,
    _evict_vector_cache_for_db_path,
    _utc_now,
    close_store_connection,
    evict_namespace_write_lock,
    flush_all_telemetry,
    namespace_write_lock,
)

logger = logging.getLogger(__name__)


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


def _safe_mesh_db_path(base_dir: Path, namespace: str) -> Path:
    """
    Resolve `mesh_<namespace>.db` inside `base_dir`, refusing symlinks that escape it.
    """
    resolved = (base_dir / f"mesh_{namespace}.db").resolve()
    if resolved.parent != base_dir:
        raise ValueError(
            f"Refusing to operate on '{resolved}': it resolves outside the Mesh data directory."
        )
    return resolved


def _distinct_memory_namespaces(db_path: Path) -> list[str]:
    """Return the distinct `namespace` values stored in a database (empty on error)."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            rows = conn.execute("SELECT DISTINCT namespace FROM memories").fetchall()
        finally:
            conn.close()
        return [str(r[0]) for r in rows if r[0] is not None]
    except sqlite3.Error:
        return []


def _reject_namespace_collision(db_path: Path, clean_ns: str, action: str) -> None:
    """
    Refuse a destructive operation when the database's rows do not belong to `clean_ns`.

    On case-insensitive filesystems (NTFS, default APFS) `mesh_Work.db` and
    `mesh_work.db` are the same file. Without this check, deleting one casing
    would destroy the other casing's memories.
    """
    names = _distinct_memory_namespaces(db_path)
    if not names:
        return
    variants = sorted(
        {
            n
            for n in names
            if n != clean_ns and n.lower() == clean_ns.lower()
        }
    )
    if variants or clean_ns not in names:
        found = variants[0] if variants else names[0]
        raise ValueError(
            f"Cannot {action} namespace '{clean_ns}': '{db_path.name}' contains rows for "
            f"{found!r}. This filesystem is case-insensitive, so the two names share one "
            f"database. Use the exact casing ({found!r}) or migrate both casings first."
        )


def _checkpoint_truncate_or_raise(conn: sqlite3.Connection, db_path: Path) -> None:
    """Checkpoint the WAL and fail loudly if another process prevents truncation."""
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Could not checkpoint '{db_path}' ({exc}). Stop the Mesh daemon and retry."
        ) from exc
    if row and int(row[0]) != 0:
        raise RuntimeError(
            f"Could not checkpoint '{db_path}' because another process is using it. "
            "Stop the Mesh daemon and other agents, then retry."
        )


def _evict_namespace_caches(namespace: str, db_path: Optional[Path] = None) -> None:
    """
    Clear all in-memory caches and close pooled SQLite connections for `namespace`
    across store, audit, context_builder, http_server, mcp_server, and dashboard.
    Only touches already-imported server modules in `sys.modules` to preserve lazy loading.
    """
    clean_ns = validate_namespace(namespace)
    if db_path is None:
        base_dir = ensure_secure_mesh_dir().resolve()
        db_path = _safe_mesh_db_path(base_dir, clean_ns)
    resolved_db = db_path.resolve()

    evict_namespace_write_lock(clean_ns)

    with _CACHE_LOCK:
        # Clear every namespace spelling that shares this file; a single
        # `_VECTOR_CACHE.pop(path)` would miss case-variant keys.
        _evict_vector_cache_for_db_path(resolved_db)
        _evict_vector_cache_for_db_path(db_path)
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


def evict_lru_namespace(exclude_namespace: Optional[str] = None) -> Optional[str]:
    """
    Evict the least-recently-used namespace (other than `exclude_namespace`) across
    store, vector, audit, context_builder, HTTP, MCP, and dashboard caches.
    """
    excl = validate_namespace(exclude_namespace) if exclude_namespace else None
    victim_ns: Optional[str] = None
    victim_db: Optional[Path] = None
    with _CACHE_LOCK:
        for db_path, ns in _VECTOR_CACHE.keys():
            if ns != excl:
                victim_ns = ns
                victim_db = db_path
                break
        if victim_ns is None:
            for db_path in _CONNECTION_POOL.keys():
                cand_ns = _namespace_from_db_path(db_path)
                if cand_ns != excl:
                    victim_ns = cand_ns
                    victim_db = db_path
                    break
    if victim_ns is not None:
        _evict_namespace_caches(victim_ns, victim_db)
    return victim_ns


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
            if strict:
                raise RuntimeError(
                    f"Failed to delete SQLite file '{candidate}'. Another process may still hold the file open."
                )
    return all_removed


def list_namespaces() -> list[dict[str, Any]]:
    """
    List all validated namespaces with their memory counts and last updated time.
    - Never silently drops a namespace whose DB file errors; includes it with `"error": "<message>"` (Phase 4.3).
    - Computes `last_updated` taking edits and accesses into account (`MAX(created_at, updated_at, accessed_at)`).
    """
    flush_all_telemetry()
    results: list[dict[str, Any]] = []
    for db_path in _get_all_db_paths():
        namespace = _namespace_from_db_path(db_path)
        try:
            clean_ns = validate_namespace(namespace)
        except ValueError:
            continue
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.cursor()
                cols = {r[1] for r in cursor.execute("PRAGMA table_info(memories)").fetchall()}
                if not cols:
                    raise sqlite3.DatabaseError(f"Database '{db_path.name}' is missing 'memories' table")
                cursor.execute("SELECT DISTINCT namespace FROM memories")
                row_names = [str(r[0]) for r in cursor.fetchall() if r[0] is not None]
                # Aggregate every namespace value in the file. On a case-insensitive
                # filesystem the same DB can hold rows for both 'Work' and 'work';
                # counting only the filename's casing would under-report.
                cursor.execute("SELECT COUNT(*) as count FROM memories")
                total = int(cursor.fetchone()["count"] or 0)
                cursor.execute("SELECT COUNT(*) as count FROM memories WHERE local_only = 1")
                local_only = int(cursor.fetchone()["count"] or 0)
                if "updated_at" in cols:
                    cursor.execute(
                        """
                        SELECT MAX(
                            CASE
                                WHEN COALESCE(updated_at, '') >= COALESCE(created_at, '')
                                     AND COALESCE(updated_at, '') >= COALESCE(accessed_at, '')
                                THEN updated_at
                                WHEN COALESCE(accessed_at, '') >= COALESCE(created_at, '')
                                THEN accessed_at
                                ELSE created_at
                            END
                        ) as last
                        FROM memories
                        """,
                    )
                else:
                    cursor.execute(
                        """
                        SELECT MAX(
                            CASE
                                WHEN COALESCE(accessed_at, '') >= COALESCE(created_at, '')
                                THEN accessed_at
                                ELSE created_at
                            END
                        ) as last
                        FROM memories
                        """,
                    )
                row = cursor.fetchone()
                last_updated = row["last"] if row and row["last"] else "never"
                cursor.close()
            finally:
                conn.close()
            entry: dict[str, Any] = {
                "namespace": clean_ns,
                "memory_count": total,
                "local_only_count": local_only,
                "last_updated": last_updated,
                "updated_at": last_updated,
            }
            variants = sorted({n for n in row_names if n != clean_ns})
            if variants:
                entry["namespace_variants"] = variants
            results.append(entry)
        except Exception as exc:
            results.append(
                {
                    "namespace": clean_ns,
                    "memory_count": 0,
                    "local_only_count": 0,
                    "last_updated": "error",
                    "updated_at": "error",
                    "error": str(exc),
                }
            )
    results.sort(key=lambda x: x["namespace"])
    return results


def delete_namespace(namespace: str) -> int:
    """
    Delete a validated namespace and ALL its memories (plus its isolated audit trail DB and `.lock` file).
    Raises `NamespaceNotFoundError` (a `ValueError` subclass) if the namespace does not exist (Phase 4.2).
    """
    clean_ns = validate_namespace(namespace)
    flush_all_telemetry()
    base_dir = ensure_secure_mesh_dir().resolve()
    db_path = _safe_mesh_db_path(base_dir, clean_ns)
    audit_db_path = _get_audit_db_path(clean_ns)

    if not db_path.exists() and not audit_db_path.exists():
        evict_namespace_write_lock(clean_ns)
        raise NamespaceNotFoundError(f"Namespace '{namespace}' does not exist")

    count = 0
    _evict_namespace_caches(clean_ns, db_path)

    if db_path.exists():
        _reject_namespace_collision(db_path, clean_ns, "delete")
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM memories WHERE namespace = ?", (clean_ns,))
                row = cursor.fetchone()
                count = int(row[0]) if row else 0
                cursor.close()
            except sqlite3.Error as exc:
                logger.warning(
                    "Could not read memory count before deleting namespace '%s': %s",
                    clean_ns,
                    exc,
                )
                count = 0
            # Refuse to delete if another process prevents a clean checkpoint:
            # deleting only the main file would orphan plaintext WAL frames.
            _checkpoint_truncate_or_raise(conn, db_path)
        finally:
            conn.close()

        _evict_namespace_caches(clean_ns, db_path)

    if audit_db_path.exists():
        conn_audit = sqlite3.connect(audit_db_path, timeout=5.0)
        try:
            _checkpoint_truncate_or_raise(conn_audit, audit_db_path)
        finally:
            conn_audit.close()
        evict_audit_cache(clean_ns)

    # Preflight succeeded for both databases. Remove the audit trail first: if
    # memory deletion then fails, no plaintext audit copy survives a "deleted"
    # report, and the memory database is still intact for a retry.
    if audit_db_path.exists():
        _remove_sqlite_files(audit_db_path, strict=True)
    if db_path.exists():
        _remove_sqlite_files(db_path, strict=True)

    evict_namespace_write_lock(clean_ns)
    return count


def rename_namespace(old_name: str, new_name: str) -> int:
    """
    Rename a namespace safely with full validation, cache eviction, and atomic rollback on failure (Phase 4.3).
    """
    clean_old = validate_namespace(old_name)
    clean_new = validate_namespace(new_name)
    if clean_old == clean_new:
        raise ValueError(f"Old and new namespace names are identical ('{clean_old}').")

    flush_all_telemetry()
    base_dir = ensure_secure_mesh_dir().resolve()
    old_db = _safe_mesh_db_path(base_dir, clean_old)
    new_db = _safe_mesh_db_path(base_dir, clean_new)
    old_audit = _get_audit_db_path(clean_old)
    new_audit = _get_audit_db_path(clean_new)

    if not old_db.exists():
        raise NamespaceNotFoundError(f"Namespace '{old_name}' does not exist")
    if new_db.exists() or new_audit.exists():
        raise ValueError(f"Namespace '{new_name}' already exists. Delete it first.")
    _reject_namespace_collision(old_db, clean_old, "rename")

    _evict_namespace_caches(clean_old, old_db)
    _evict_namespace_caches(clean_new, new_db)

    count = 0
    # Serialize the checkpoint/copy/delete sequence against store writers in
    # this and other processes. Canonical lock order prevents deadlock between
    # two concurrent renames.
    lock_names = sorted({clean_old, clean_new})
    with ExitStack() as stack:
        for lock_ns in lock_names:
            stack.enter_context(namespace_write_lock(lock_ns, timeout=30.0))

        conn_old = sqlite3.connect(old_db, timeout=5.0)
        try:
            # A busy checkpoint leaves committed transactions only in the WAL;
            # copying just the main file would silently lose them.
            _checkpoint_truncate_or_raise(conn_old, old_db)
        finally:
            conn_old.close()

        try:
            shutil.copy2(old_db, new_db)
            secure_file_permissions(new_db)

            conn_new = sqlite3.connect(new_db, timeout=5.0)
            try:
                conn_new.execute("UPDATE memories SET namespace = ? WHERE namespace = ?", (clean_new, clean_old))
                conn_new.execute("UPDATE queries SET namespace = ? WHERE namespace = ?", (clean_new, clean_old))
                conn_new.execute(
                    "UPDATE contradictions SET namespace = ? WHERE namespace = ?", (clean_new, clean_old)
                )
                # Bump content_version in mesh_meta if present
                tables = {
                    r[0]
                    for r in conn_new.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "mesh_meta" in tables:
                    conn_new.execute(
                        "INSERT OR REPLACE INTO mesh_meta (key, value) VALUES ('content_version', COALESCE((SELECT CAST(value AS INTEGER) + 1 FROM mesh_meta WHERE key = 'content_version'), '1'))"
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

            if old_audit.exists():
                conn_audit_old = sqlite3.connect(old_audit, timeout=5.0)
                try:
                    _checkpoint_truncate_or_raise(conn_audit_old, old_audit)
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
                finally:
                    conn_audit_new.close()
                secure_file_permissions(new_audit)
        except Exception:
            # Roll back partially created new DB and audit DB so old namespace remains untouched.
            # Do not call `_evict_namespace_caches` here: it would drop the write locks we hold.
            close_store_connection(new_db)
            _remove_sqlite_files(new_db)
            if new_audit.exists():
                _remove_sqlite_files(new_audit)
            raise

        # Both DB and Audit DB succeeded — now remove old files while the lock
        # is still held, and fail loudly if deletion is blocked (otherwise two
        # live copies would silently diverge).
        removed = _remove_sqlite_files(old_db, strict=True)
        if old_audit.exists():
            removed = _remove_sqlite_files(old_audit, strict=True) and removed
        if not removed:
            raise RuntimeError(
                f"Renamed data to '{clean_new}', but could not fully remove the old namespace files. "
                "Close the Mesh daemon and retry."
            )

    evict_namespace_write_lock(clean_old)
    evict_namespace_write_lock(clean_new)
    return count


def namespace_stats(namespace: str) -> dict[str, Any]:
    """
    Return detailed statistics for a validated namespace using the unified
    `_compute_row_effective_confidence()` staleness definition.
    Never creates a `.db` file if the namespace does not exist.
    """
    clean_ns = validate_namespace(namespace)
    flush_all_telemetry()
    base_dir = ensure_secure_mesh_dir().resolve()
    db_path = _safe_mesh_db_path(base_dir, clean_ns)
    if not db_path.exists():
        raise NamespaceNotFoundError(f"Namespace '{namespace}' does not exist")

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
        # SUM(access_count) counts memory *hits*, not recall calls. Keep the key
        # for API compatibility but expose the real search count separately.
        total_recalls = int(recall_row["total"] or 0)

        total_searches = 0
        try:
            cursor.execute(
                "SELECT COUNT(*) as total FROM queries WHERE namespace = ?", (clean_ns,)
            )
            total_searches = int(cursor.fetchone()["total"] or 0)
        except sqlite3.OperationalError:
            # Databases created before recall telemetry existed have no table.
            total_searches = 0

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
        "total_searches": total_searches,
        "oldest_memory": oldest,
        "newest_memory": newest,
        "stale_count": stale,
    }