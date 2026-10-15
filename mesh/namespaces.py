"""
mesh/namespaces.py — Namespace listing, deletion, renaming, and statistics.
"""

from __future__ import annotations

import gc
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from .store import (
    MESH_DIR,
    _CACHE_LOCK,
    _INITIALIZED_DBS,
    _TRANSIENT_EMBEDDINGS,
    _VECTOR_CACHE,
    _parse_iso_utc,
    _utc_now,
)


def _get_all_db_paths() -> list[Path]:
    """Return paths to all namespace SQLite databases in ~/.mesh/"""
    if not MESH_DIR.exists():
        return []
    return [
        p
        for p in MESH_DIR.glob("mesh_*.db")
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    ]


def _namespace_from_db_path(path: Path) -> str:
    """Extract namespace name from db filename: mesh_shared.db -> shared"""
    name = path.stem
    return name[5:] if name.startswith("mesh_") else name


def _evict_namespace_caches(namespace: str, db_path: Path) -> None:
    """Clear in-memory caches for a namespace before deleting or renaming its DB file."""
    with _CACHE_LOCK:
        _VECTOR_CACHE.pop(db_path, None)
        _INITIALIZED_DBS.pop(db_path, None)
        stale_keys = [k for k in _TRANSIENT_EMBEDDINGS if k[0] == namespace]
        for k in stale_keys:
            _TRANSIENT_EMBEDDINGS.pop(k, None)


def _remove_sqlite_files(db_path: Path) -> None:
    """Delete a SQLite database file along with any WAL or SHM sidecar files."""
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


def list_namespaces() -> list[dict[str, Any]]:
    """
    List all namespaces with their memory counts and last updated time.

    Returns:
        List of dicts with keys: namespace, memory_count, local_only_count, last_updated
    """
    results: list[dict[str, Any]] = []

    for db_path in _get_all_db_paths():
        namespace = _namespace_from_db_path(db_path)
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) as count FROM memories WHERE namespace = ?",
                    (namespace,),
                )
                total = int(cursor.fetchone()["count"] or 0)

                cursor.execute(
                    "SELECT COUNT(*) as count FROM memories WHERE namespace = ? AND local_only = 1",
                    (namespace,),
                )
                local_only = int(cursor.fetchone()["count"] or 0)

                cursor.execute(
                    "SELECT MAX(created_at) as last FROM memories WHERE namespace = ?",
                    (namespace,),
                )
                row = cursor.fetchone()
                last_updated = row["last"] if row and row["last"] else "never"
                cursor.close()
            finally:
                conn.close()

            results.append(
                {
                    "namespace": namespace,
                    "memory_count": total,
                    "local_only_count": local_only,
                    "last_updated": last_updated,
                }
            )
        except Exception:
            results.append(
                {
                    "namespace": namespace,
                    "memory_count": 0,
                    "local_only_count": 0,
                    "last_updated": "error",
                }
            )

    results.sort(key=lambda x: x["namespace"])
    return results


def delete_namespace(namespace: str) -> int:
    """
    Delete a namespace and ALL its memories.

    Returns:
        Number of memories deleted.
    """
    db_path = MESH_DIR / f"mesh_{namespace}.db"
    count = 0

    _evict_namespace_caches(namespace, db_path)

    if db_path.exists():
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM memories")
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

        _remove_sqlite_files(db_path)

    # Clean up legacy ChromaDB collection only if a chroma directory exists
    chroma_dir = MESH_DIR / "chroma"
    if chroma_dir.exists():
        try:
            import chromadb

            chroma = chromadb.PersistentClient(path=str(chroma_dir))
            chroma.delete_collection(name=f"mesh_{namespace}")
        except Exception:
            pass

    return count


def rename_namespace(old_name: str, new_name: str) -> int:
    """
    Rename a namespace. Copies all data (including embeddings) to the new namespace,
    updates internal namespace columns, and deletes the old namespace.

    Returns:
        Number of memories moved.
    """
    old_db = MESH_DIR / f"mesh_{old_name}.db"
    new_db = MESH_DIR / f"mesh_{new_name}.db"

    if not old_db.exists():
        raise ValueError(f"Namespace '{old_name}' does not exist")
    if new_db.exists():
        raise ValueError(f"Namespace '{new_name}' already exists. Delete it first.")

    _evict_namespace_caches(old_name, old_db)
    _evict_namespace_caches(new_name, new_db)

    # Checkpoint WAL on old_db so all recent writes are flushed to the main file before copying
    conn_old = sqlite3.connect(old_db, timeout=5.0)
    try:
        try:
            conn_old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
    finally:
        conn_old.close()

    shutil.copy2(old_db, new_db)

    # Update namespace column inside the new SQLite database
    count = 0
    conn_new = sqlite3.connect(new_db, timeout=5.0)
    try:
        conn_new.execute("UPDATE memories SET namespace = ? WHERE namespace = ?", (new_name, old_name))
        conn_new.execute("UPDATE queries SET namespace = ? WHERE namespace = ?", (new_name, old_name))
        conn_new.execute(
            "UPDATE contradictions SET namespace = ? WHERE namespace = ?", (new_name, old_name)
        )
        conn_new.commit()
        cur = conn_new.execute(
            "SELECT COUNT(*) FROM memories WHERE namespace = ?", (new_name,)
        )
        row = cur.fetchone()
        count = int(row[0]) if row else 0
        cur.close()
    finally:
        conn_new.close()

    _remove_sqlite_files(old_db)

    # Clean up legacy ChromaDB collection if present
    chroma_dir = MESH_DIR / "chroma"
    if chroma_dir.exists():
        try:
            import chromadb

            chroma = chromadb.PersistentClient(path=str(chroma_dir))
            chroma.delete_collection(name=f"mesh_{old_name}")
        except Exception:
            pass

    return count


def namespace_stats(namespace: str) -> dict[str, Any]:
    """
    Return detailed statistics for a namespace.
    """
    db_path = MESH_DIR / f"mesh_{namespace}.db"
    if not db_path.exists():
        raise ValueError(f"Namespace '{namespace}' does not exist")

    now = _utc_now()
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as c FROM memories WHERE namespace = ?", (namespace,))
        total = int(cursor.fetchone()["c"] or 0)

        cursor.execute(
            "SELECT COUNT(*) as c FROM memories WHERE namespace = ? AND local_only = 1",
            (namespace,),
        )
        local_only = int(cursor.fetchone()["c"] or 0)

        cursor.execute(
            "SELECT memory_type, COUNT(*) as c FROM memories WHERE namespace = ? GROUP BY memory_type",
            (namespace,),
        )
        type_rows = cursor.fetchall()
        type_breakdown = {r["memory_type"]: int(r["c"]) for r in type_rows}

        cursor.execute("SELECT AVG(confidence) as avg FROM memories WHERE namespace = ?", (namespace,))
        avg_conf_row = cursor.fetchone()
        avg_conf = float(avg_conf_row["avg"] or 0.0)

        cursor.execute(
            "SELECT SUM(access_count) as total FROM memories WHERE namespace = ?", (namespace,)
        )
        recall_row = cursor.fetchone()
        total_recalls = int(recall_row["total"] or 0)

        cursor.execute("SELECT MIN(created_at) as oldest FROM memories WHERE namespace = ?", (namespace,))
        oldest = cursor.fetchone()["oldest"] or "none"

        cursor.execute("SELECT MAX(created_at) as newest FROM memories WHERE namespace = ?", (namespace,))
        newest = cursor.fetchone()["newest"] or "none"

        cursor.execute(
            """
            SELECT created_at, decay_start_at, ttl_days, confidence
            FROM memories
            WHERE namespace = ? AND ttl_days IS NOT NULL
            """,
            (namespace,),
        )
        stale = 0
        for r in cursor.fetchall():
            ttl = r["ttl_days"]
            start_dt = _parse_iso_utc(r["decay_start_at"] or r["created_at"])
            if start_dt and ttl and float(ttl) > 0:
                days_elapsed = max(0.0, (now - start_dt).total_seconds() / 86400.0)
                decay_factor = max(0.0, 1.0 - (days_elapsed / float(ttl)))
                effective_conf = float(r["confidence"] or 1.0) * decay_factor
                if days_elapsed > float(ttl) or effective_conf < 0.3:
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