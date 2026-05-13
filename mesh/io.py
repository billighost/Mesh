"""
mesh/io.py — Export and import for Mesh memory stores.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .store import MeshStore
from .memory import Mesh

MESH_VERSION = "0.3"


def export_namespace(
    namespace: str = "shared",
    output_path: Optional[str] = None,
    include_embeddings: bool = True
) -> dict:
    """
    Export all non-private memories from a namespace to a JSON file.
    
    Args:
        namespace: The namespace to export. Defaults to "shared".
        output_path: Path to write the JSON file. If None, returns the dict only.
        include_embeddings: Whether to include embedding vectors. 
                           Set False for human-readable exports (much smaller file).
    
    Returns:
        Export dict (also written to file if output_path is provided).
    """
    store = MeshStore(namespace=namespace)
    memories = store.get_exportable_memories()
    
    skipped_count = sum(1 for m in store.list_all(limit=100000) if m.get("local_only"))
    
    # Get embeddings from ChromaDB if requested
    exportable = []
    for mem in memories:
        if mem.get("local_only"):
            continue
        
        entry = {
            "id": mem["id"],
            "content": mem["content"],
            "memory_type": mem.get("memory_type", "general"),
            "confidence": mem.get("confidence", 1.0),
            "source_agent": mem.get("source_agent", "unknown"),
            "tags": json.loads(mem.get("tags", "[]")),
            "local_only": False,
            "created_at": mem.get("created_at", ""),
            "ttl_days": mem.get("ttl_days"),
            "access_count": mem.get("access_count", 0),
        }
        
        if include_embeddings:
            try:
                result = store.collection.get(ids=[mem["id"]], include=["embeddings"])
                if result and result["embeddings"]:
                    entry["embedding"] = result["embeddings"][0]
            except Exception:
                entry["embedding"] = None
        
        exportable.append(entry)
    
    export_data = {
        "mesh_version": MESH_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "memory_count": len(exportable),
        "skipped_local_only": skipped_count,
        "memories": exportable
    }
    
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
        print(f"✓ Exported {len(exportable)} memories to {path}")
        if skipped_count > 0:
            print(f"  (Skipped {skipped_count} local_only memories — these are never exported)")
    
    return export_data


def import_namespace(
    input_path: str,
    target_namespace: Optional[str] = None,
    merge_strategy: str = "skip_existing",
    dry_run: bool = False
) -> dict:
    """
    Import memories from a JSON export file into a namespace.
    
    Args:
        input_path: Path to the JSON export file.
        target_namespace: Override the namespace from the export file.
                         If None, uses the namespace stored in the export.
        merge_strategy: How to handle memories that already exist.
                       "skip_existing" — skip if ID already exists (default, safe)
                       "overwrite" — overwrite if ID already exists
        dry_run: If True, print what would happen but don't write anything.
    
    Returns:
        Dict with import stats: imported, skipped, errors.
    """
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"Export file not found: {input_path}")
    
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # Validate format
    if "mesh_version" not in data or "memories" not in data:
        raise ValueError("Invalid Mesh export file format.")
    
    namespace = target_namespace or data.get("namespace", "shared")
    memories = data.get("memories", [])
    
    stats = {"imported": 0, "skipped": 0, "errors": 0, "namespace": namespace}
    
    if dry_run:
        print(f"[DRY RUN] Would import {len(memories)} memories into namespace '{namespace}'")
        print(f"[DRY RUN] merge_strategy: {merge_strategy}")
        for mem in memories[:5]:
            print(f"  - [{mem.get('memory_type', 'general')}] {mem['content'][:80]}...")
        if len(memories) > 5:
            print(f"  ... and {len(memories) - 5} more")
        return stats
    
    store = MeshStore(namespace=namespace)
    mesh = Mesh(namespace=namespace, agent_id="mesh-import")
    
    for mem in memories:
        try:
            mem_id = mem.get("id", str(uuid.uuid4()))
            
            # Check if exists
            existing = None
            try:
                result = store.collection.get(ids=[mem_id])
                if result and result["ids"]:
                    existing = True
            except Exception:
                existing = False
            
            if existing and merge_strategy == "skip_existing":
                stats["skipped"] += 1
                continue
            
            # Re-embed if no embedding stored, or use stored embedding
            embedding = mem.get("embedding")
            
            if embedding:
                # Use stored embedding directly
                store.collection.upsert(
                    ids=[mem_id],
                    documents=[mem["content"]],
                    embeddings=[embedding],
                    metadatas=[{
                        "memory_type": mem.get("memory_type", "general"),
                        "source_agent": mem.get("source_agent", "mesh-import"),
                        "confidence": float(mem.get("confidence", 1.0)),
                        "tags": json.dumps(mem.get("tags", [])),
                    }]
                )
            else:
                # Re-embed fresh
                mesh.learn(
                    content=mem["content"],
                    memory_type=mem.get("memory_type", "general"),
                    confidence=mem.get("confidence", 1.0),
                    tags=mem.get("tags", []),
                    local_only=False
                )
            
            # Write SQLite metadata
            import sqlite3
            conn = sqlite3.connect(store.db_path)
            cursor = conn.cursor()
            
            if existing and merge_strategy == "overwrite":
                cursor.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
            
            cursor.execute("""
                INSERT OR IGNORE INTO memories 
                (id, content, memory_type, confidence, source_agent, tags, 
                 created_at, ttl_days, access_count, local_only)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                mem_id,
                mem["content"],
                mem.get("memory_type", "general"),
                mem.get("confidence", 1.0),
                mem.get("source_agent", "mesh-import"),
                json.dumps(mem.get("tags", [])),
                mem.get("created_at", datetime.now(timezone.utc).isoformat()),
                mem.get("ttl_days"),
                mem.get("access_count", 0)
            ))
            conn.commit()
            conn.close()
            
            stats["imported"] += 1
            
        except Exception as e:
            stats["errors"] += 1
            print(f"  Error importing memory '{mem.get('content', '')[:50]}': {e}")
    
    print(f"✓ Import complete: {stats['imported']} imported, {stats['skipped']} skipped, {stats['errors']} errors")
    return stats
