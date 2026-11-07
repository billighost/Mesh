"""
mesh/namespaces.py — Namespace listing, deletion, renaming, and statistics.

Security & Integrity:
  - Validates all namespace identifiers against path traversal via `validate_namespace()`
  - Rejects symlinked `.db`, `-wal`, and `-shm` files across listing, stats, rename, and deletion (Finding N6)
  - Flushes buffered telemetry (`flush_all_telemetry`) before reading stats, deleting, or renaming
  - Uses `_compute_row_effective_confidence()` from `store.py` for a single unified staleness definition
  - Evicts all in-memory vector, connection, lock, audit, context, HTTP, MCP, and Dashboard caches on delete/rename
  - Cleans up advisory `.mesh_<ns>.lock` files and enforces atomic rollback on failed `rename_namespace()`
  - Stages namespace publication and recovers durable rename journals before ordinary opens
  - Enforces 0600 POSIX file permissions and Windows owner ACLs on renamed SQLite databases
"""
from __future__ import annotations

import gc
import logging
import os
from .sqlite_runtime import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .audit import (
    _get_audit_db_path,
    ensure_secure_mesh_dir,
    evict_audit_cache,
    get_mesh_dir,
    mesh_dir_context,
    reject_symlink,
    validate_namespace,
)
from .store import (
    MeshStore,
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
    close_store_readers,
    evict_namespace_write_lock,
    flush_all_telemetry,
    namespace_write_lock,
)

logger = logging.getLogger(__name__)


def _get_all_db_paths() -> list[Path]:
    """
    Return paths to all valid, non-symlinked namespace SQLite databases in ~/.mesh/.
    Finding N6: Rejects symlinks and verifies resolved parent confinement inside `mesh_dir`.
    """
    mesh_dir = get_mesh_dir()
    if not mesh_dir.exists() or mesh_dir.is_symlink():
        return []
    resolved_base = mesh_dir.resolve()
    if any(resolved_base.glob(".mesh-rename-*.json")):
        from .namespace_maintenance import recover_namespace_renames

        recover_namespace_renames(resolved_base)
    results: list[Path] = []
    for p in mesh_dir.glob("mesh_*.db"):
        if p.name.endswith(("-wal", "-shm")):
            continue
        try:
            reject_symlink(p)
            if not p.is_file():
                continue
            if p.resolve().parent != resolved_base:
                continue
        except (OSError, ValueError):
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
    Resolve `mesh_<namespace>.db` inside `base_dir`, refusing symlinks on the DB or sidecars (Finding N6).
    """
    candidate = base_dir / f"mesh_{namespace}.db"
    reject_symlink(candidate)
    resolved = candidate.resolve()
    if resolved.parent != base_dir:
        raise ValueError(
            f"Refusing to operate on '{resolved}': it resolves outside the Mesh data directory."
        )
    return resolved


def _distinct_memory_namespaces(db_path: Path) -> list[str]:
    """Return the distinct `namespace` values stored in a database (empty on error)."""
    try:
        reject_symlink(db_path)
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            rows = conn.execute("SELECT DISTINCT namespace FROM memories").fetchall()
        finally:
            conn.close()
        return [str(r[0]) for r in rows if r[0] is not None]
    except (sqlite3.Error, ValueError):
        return []


def _reject_namespace_collision(db_path: Path, clean_ns: str, action: str) -> None:
    """
    Refuse a destructive operation when the database's rows do not belong to `clean_ns`.
    """
    from .store import SCHEMA_VERSION, _check_namespace_binding

    connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    try:
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) > int(SCHEMA_VERSION):
            raise RuntimeError("Database schema is newer than supported; namespace maintenance is refused.")
        _check_namespace_binding(connection, clean_ns)
    finally:
        connection.close()
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


def _sync_namespace_directory(root: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _rebind_namespace_database(conn: sqlite3.Connection, old: str, new: str, *, new_generation: Optional[str] = None) -> int:
    """Rebind authoritative rows together; stale handles keep their old generation."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if new_generation is not None:
        from .store import _validate_incarnation

        _validate_incarnation(new_generation, "new_generation")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Use a static allowlist: database-supplied identifiers are untrusted.
        for table in ("memories", "memory_passages", "queries", "contradictions", "work_items", "work_events", "embedding_jobs", "mutation_outbox", "operation_receipts"):
            if table in tables:
                conn.execute(f"UPDATE {table} SET namespace=? WHERE namespace=?", (new, old))
        if "mesh_meta" in tables:
            conn.execute("INSERT OR REPLACE INTO mesh_meta(key,value) VALUES('namespace_owner',?)", (new,))
            conn.execute("UPDATE mesh_meta SET value=CAST(value AS INTEGER)+1 WHERE key='content_version'")
            if conn.execute("SELECT 1 FROM mesh_meta WHERE key='store_generation'").fetchone():
                conn.execute("UPDATE mesh_meta SET value=? WHERE key='store_generation'", (new_generation or uuid.uuid4().hex,))
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("Namespace rename would leave inconsistent references.")
        if "memory_passages" in tables:
            from .passage_search import verify_passage_rows

            verify_passage_rows(conn)
        count = int(conn.execute("SELECT COUNT(*) FROM memories WHERE namespace=?", (new,)).fetchone()[0])
        conn.commit()
        return count
    except BaseException:
        conn.rollback()
        raise


def _evict_namespace_caches(namespace: str, db_path: Optional[Path] = None) -> None:
    """Invalidate only the bound namespace and retire writer/reader handles."""
    clean_ns = validate_namespace(namespace)
    candidate = db_path or _safe_mesh_db_path(ensure_secure_mesh_dir().resolve(), clean_ns)
    resolved_db = candidate.resolve()
    with mesh_dir_context(resolved_db.parent):
        evict_namespace_write_lock(clean_ns, data_root=resolved_db.parent)
        with _CACHE_LOCK:
            _evict_vector_cache_for_db_path(resolved_db)
            _INITIALIZED_DBS.pop(resolved_db, None)
            for key in [key for key in _TRANSIENT_EMBEDDINGS if key[0] == clean_ns]:
                _TRANSIENT_EMBEDDINGS.pop(key, None)
        # Closing native handles may wait for a checkout; keep registry locks short.
        close_store_readers(resolved_db)
        close_store_connection(resolved_db)
        evict_audit_cache(clean_ns)
        for module_name, function_name in (
            ('mesh.operations', 'evict_mesh_cache'),
            ('mesh.context_builder', 'evict_context_mesh_cache'),
            ('mesh.http_server', 'evict_http_mesh_cache'),
            ('mesh.mcp_server', 'evict_mcp_mesh_cache'),
            ('mesh.dashboard', 'evict_dashboard_mesh_cache'),
            ('mesh.cli', 'evict_cli_mesh_cache'),
            ('mesh.memory', 'evict_memory_namespace_cache'),
        ):
            module = sys.modules.get(module_name)
            function = getattr(module, function_name, None)
            if function is not None:
                function(clean_ns)


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
    """Delete a SQLite database file along with any WAL or SHM sidecar files (rejecting symlinks, Finding N6)."""
    reject_symlink(db_path)
    all_removed = True
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        for attempt in range(10):
            if not candidate.exists() and not candidate.is_symlink():
                break
            try:
                candidate.unlink()
                break
            except OSError:
                if attempt in (0, 3, 6):
                    gc.collect()
                time.sleep(0.04 * (attempt + 1))
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
    - Never silently drops a namespace whose DB file errors; includes it with `"error": "<message>"`.
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
            reject_symlink(db_path)
            store = MeshStore(namespace=clean_ns, create=False)
            with store._read_connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cols = {r[1] for r in cursor.execute("PRAGMA table_info(memories)").fetchall()}
                if not cols:
                    raise sqlite3.DatabaseError(f"Database '{db_path.name}' is missing 'memories' table")
                cursor.execute("SELECT DISTINCT namespace FROM memories")
                row_names = [str(r[0]) for r in cursor.fetchall() if r[0] is not None]
                cursor.execute("SELECT COUNT(*) as count FROM memories")
                r_tot = cursor.fetchone()
                total = int(r_tot[0] or 0) if r_tot else 0
                cursor.execute("SELECT COUNT(*) as count FROM memories WHERE local_only = 1")
                r_loc = cursor.fetchone()
                local_only = int(r_loc[0] or 0) if r_loc else 0
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
                last_updated = str(row[0]) if row and row[0] else "never"
                cursor.close()
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
    """Delete a namespace while cooperating writers and maintenance are excluded."""
    clean_ns = validate_namespace(namespace)
    flush_all_telemetry()
    root = ensure_secure_mesh_dir().resolve()
    if any(root.glob(".mesh-rename-*.json")):
        from .namespace_maintenance import recover_namespace_renames

        recover_namespace_renames(root, clean_ns)
    try:
        with namespace_write_lock(clean_ns, data_root=root):
            return _delete_namespace_locked(clean_ns)
    finally:
        evict_namespace_write_lock(clean_ns, data_root=root)


def _delete_namespace_locked(namespace: str) -> int:
    """
    Delete a validated namespace and ALL its memories (plus its isolated audit trail DB and `.lock` file).
    Raises `NamespaceNotFoundError` (a `ValueError` subclass) if the namespace does not exist.
    """
    clean_ns = validate_namespace(namespace)
    base_dir = ensure_secure_mesh_dir().resolve()
    db_path = _safe_mesh_db_path(base_dir, clean_ns)
    audit_db_path = _get_audit_db_path(clean_ns)

    if not db_path.exists() and not audit_db_path.exists():
        evict_namespace_write_lock(clean_ns)
        raise NamespaceNotFoundError(f"Namespace '{namespace}' does not exist")

    count = 0
    _evict_namespace_caches(clean_ns, db_path)

    if db_path.exists():
        reject_symlink(db_path)
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
            _checkpoint_truncate_or_raise(conn, db_path)
        finally:
            conn.close()

        _evict_namespace_caches(clean_ns, db_path)

    if audit_db_path.exists():
        reject_symlink(audit_db_path)
        conn_audit = sqlite3.connect(audit_db_path, timeout=5.0)
        try:
            _checkpoint_truncate_or_raise(conn_audit, audit_db_path)
        finally:
            conn_audit.close()
        evict_audit_cache(clean_ns)

    if audit_db_path.exists():
        _remove_sqlite_files(audit_db_path, strict=True)
    if db_path.exists():
        _remove_sqlite_files(db_path, strict=True)

    _sync_namespace_directory(base_dir)
    evict_namespace_write_lock(clean_ns)
    return count


def rename_namespace(old_name: str, new_name: str) -> int:
    """Rename through staged, durable publication and validated restart recovery."""
    clean_old = validate_namespace(old_name)
    clean_new = validate_namespace(new_name)
    if clean_old == clean_new:
        raise ValueError(f"Old and new namespace names are identical ('{clean_old}').")
    flush_all_telemetry()
    base_dir = ensure_secure_mesh_dir().resolve()
    from .namespace_maintenance import rename_namespace as rename_with_recovery

    count = rename_with_recovery(clean_old, clean_new, base_dir)
    evict_namespace_write_lock(clean_old, data_root=base_dir)
    evict_namespace_write_lock(clean_new, data_root=base_dir)
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
    store = MeshStore(namespace=clean_ns, create=False)
    with store._read_connect() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as c FROM memories WHERE namespace = ?", (clean_ns,))
        r_tot = cursor.fetchone()
        total = int(r_tot[0] or 0) if r_tot else 0

        cursor.execute(
            "SELECT COUNT(*) as c FROM memories WHERE namespace = ? AND local_only = 1",
            (clean_ns,),
        )
        r_loc = cursor.fetchone()
        local_only = int(r_loc[0] or 0) if r_loc else 0

        cursor.execute(
            "SELECT memory_type, COUNT(*) as c FROM memories WHERE namespace = ? GROUP BY memory_type",
            (clean_ns,),
        )
        type_rows = cursor.fetchall()
        type_breakdown = {str(r[0]): int(r[1]) for r in type_rows}

        cursor.execute("SELECT AVG(confidence) as avg FROM memories WHERE namespace = ?", (clean_ns,))
        avg_conf_row = cursor.fetchone()
        avg_conf = float(avg_conf_row[0] or 0.0) if avg_conf_row else 0.0

        cursor.execute(
            "SELECT SUM(access_count) as total FROM memories WHERE namespace = ?", (clean_ns,)
        )
        recall_row = cursor.fetchone()
        total_recalls = int(recall_row[0] or 0) if recall_row else 0

        total_searches = 0
        try:
            cursor.execute(
                "SELECT COUNT(*) as total FROM queries WHERE namespace = ?", (clean_ns,)
            )
            s_row = cursor.fetchone()
            total_searches = int(s_row[0] or 0) if s_row else 0
        except sqlite3.OperationalError:
            total_searches = 0

        cursor.execute("SELECT MIN(created_at) as oldest FROM memories WHERE namespace = ?", (clean_ns,))
        r_old = cursor.fetchone()
        oldest = str(r_old[0]) if (r_old and r_old[0]) else "none"

        cursor.execute("SELECT MAX(created_at) as newest FROM memories WHERE namespace = ?", (clean_ns,))
        r_new = cursor.fetchone()
        newest = str(r_new[0]) if (r_new and r_new[0]) else "none"

        cursor.execute(
            """
            SELECT created_at, decay_start_at, ttl_days, confidence
            FROM memories
            WHERE namespace = ?
            """,
            (clean_ns,),
        )
        stale = 0
        for r in cursor:
            row_dict = {
                "created_at": r[0],
                "decay_start_at": r[1],
                "ttl_days": r[2],
                "confidence": r[3],
            }
            _, is_stale = _compute_row_effective_confidence(row_dict, now=now)
            if is_stale:
                stale += 1

        cursor.close()

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
