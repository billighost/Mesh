"""
mesh/namespaces.py — Namespace listing, deletion, and renaming.
"""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

MESH_DIR = Path.home() / ".mesh"


def _get_all_db_paths() -> list[Path]:
    """Return paths to all SQLite databases in ~/.mesh/"""
    if not MESH_DIR.exists():
        return []
    return list(MESH_DIR.glob("mesh_*.db"))


def _namespace_from_db_path(path: Path) -> str:
    """Extract namespace name from db filename: mesh_shared.db → shared"""
    name = path.stem  # mesh_shared
    return name[5:] if name.startswith("mesh_") else name


def list_namespaces() -> list[dict]:
    """
    List all namespaces with their memory counts and last updated time.
    
    Returns:
        List of dicts with keys: namespace, memory_count, local_only_count, last_updated
    """
    results = []
    
    for db_path in _get_all_db_paths():
        namespace = _namespace_from_db_path(db_path)
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) as count FROM memories")
            total = cursor.fetchone()["count"]
            
            cursor.execute("SELECT COUNT(*) as count FROM memories WHERE local_only = 1")
            local_only = cursor.fetchone()["count"]
            
            cursor.execute("SELECT MAX(created_at) as last FROM memories")
            row = cursor.fetchone()
            last_updated = row["last"] or "never"
            
            conn.close()
            
            results.append({
                "namespace": namespace,
                "memory_count": total,
                "local_only_count": local_only,
                "last_updated": last_updated
            })
        except Exception as e:
            results.append({
                "namespace": namespace,
                "memory_count": 0,
                "local_only_count": 0,
                "last_updated": "error"
            })
    
    results.sort(key=lambda x: x["namespace"])
    return results


def delete_namespace(namespace: str) -> int:
    """
    Delete a namespace and ALL its memories (both SQLite and ChromaDB).
    
    Returns:
        Number of memories deleted.
    """
    import chromadb
    
    db_path = MESH_DIR / f"mesh_{namespace}.db"
    count = 0
    
    # Count first
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM memories")
        count = cursor.fetchone()[0]
        conn.close()
        db_path.unlink()
    
    # Delete ChromaDB collection
    try:
        chroma = chromadb.PersistentClient(path=str(MESH_DIR / "chroma"))
        chroma.delete_collection(name=f"mesh_{namespace}")
    except Exception:
        pass  # Collection may not exist
    
    return count


def rename_namespace(old_name: str, new_name: str) -> int:
    """
    Rename a namespace. Copies all data to new namespace and deletes the old one.
    
    Returns:
        Number of memories moved.
    """
    import chromadb
    from sentence_transformers import SentenceTransformer
    
    old_db = MESH_DIR / f"mesh_{old_name}.db"
    new_db = MESH_DIR / f"mesh_{new_name}.db"
    
    if not old_db.exists():
        raise ValueError(f"Namespace '{old_name}' does not exist")
    if new_db.exists():
        raise ValueError(f"Namespace '{new_name}' already exists. Delete it first.")
    
    # Copy SQLite
    import shutil
    shutil.copy2(old_db, new_db)
    
    # Copy ChromaDB collection
    chroma = chromadb.PersistentClient(path=str(MESH_DIR / "chroma"))
    
    try:
        old_collection = chroma.get_collection(name=f"mesh_{old_name}")
        all_data = old_collection.get(include=["documents", "embeddings", "metadatas"])
        
        if all_data["ids"]:
            new_collection = chroma.get_or_create_collection(
                name=f"mesh_{new_name}",
                metadata={"hnsw:space": "cosine"}
            )
            new_collection.upsert(
                ids=all_data["ids"],
                documents=all_data["documents"],
                embeddings=all_data["embeddings"],
                metadatas=all_data["metadatas"]
            )
        
        count = len(all_data["ids"])
    except Exception:
        count = 0
    
    # Delete old
    old_db.unlink()
    try:
        chroma.delete_collection(name=f"mesh_{old_name}")
    except Exception:
        pass
    
    return count


def namespace_stats(namespace: str) -> dict:
    """
    Return detailed statistics for a namespace.
    """
    from datetime import datetime, timezone
    
    db_path = MESH_DIR / f"mesh_{namespace}.db"
    if not db_path.exists():
        raise ValueError(f"Namespace '{namespace}' does not exist")
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) as c FROM memories")
    total = cursor.fetchone()["c"]
    
    cursor.execute("SELECT COUNT(*) as c FROM memories WHERE local_only = 1")
    local_only = cursor.fetchone()["c"]
    
    cursor.execute("SELECT memory_type, COUNT(*) as c FROM memories GROUP BY memory_type")
    type_rows = cursor.fetchall()
    type_breakdown = {r["memory_type"]: r["c"] for r in type_rows}
    
    cursor.execute("SELECT AVG(confidence) as avg FROM memories")
    avg_conf_row = cursor.fetchone()
    avg_conf = float(avg_conf_row["avg"] or 0)
    
    cursor.execute("SELECT SUM(access_count) as total FROM memories")
    recall_row = cursor.fetchone()
    total_recalls = int(recall_row["total"] or 0)
    
    cursor.execute("SELECT MIN(created_at) as oldest FROM memories")
    oldest = cursor.fetchone()["oldest"] or "none"
    
    cursor.execute("SELECT MAX(created_at) as newest FROM memories")
    newest = cursor.fetchone()["newest"] or "none"
    
    # Count stale memories (ttl_days set, and past ttl)
    now_iso = datetime.now(timezone.utc).isoformat()
    cursor.execute("""
        SELECT COUNT(*) as c FROM memories 
        WHERE ttl_days IS NOT NULL 
        AND created_at IS NOT NULL
        AND julianday('now') - julianday(created_at) > ttl_days
    """)
    stale = cursor.fetchone()["c"]
    
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
        "stale_count": stale
    }
