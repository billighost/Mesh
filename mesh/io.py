"""
mesh/io.py — High-speed export and import for Mesh memory stores.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .memory import VALID_MEMORY_TYPES, _embed_batch, _parse_tags
from . passive import _normalize_memory_type
from .store import MeshStore

MESH_VERSION = "0.3"


def export_namespace(
    namespace: str = "shared",
    output_path: Optional[str] = None,
    include_embeddings: bool = True,
) -> dict[str, Any]:
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
    all_memories = store.list_all(limit=100000)
    skipped_count = sum(1 for m in all_memories if m.get("local_only"))

    raw_exportable = store.get_exportable_memories(include_embeddings=include_embeddings)

    exportable: list[dict[str, Any]] = []
    for mem in raw_exportable:
        if mem.get("local_only"):
            continue

        entry: dict[str, Any] = {
            "id": mem["id"],
            "content": mem["content"],
            "memory_type": mem.get("memory_type", "general"),
            "confidence": float(mem.get("confidence", 1.0)),
            "source_agent": mem.get("source_agent", "unknown"),
            "tags": _parse_tags(mem.get("tags", "[]")),
            "local_only": False,
            "created_at": mem.get("created_at", ""),
            "ttl_days": mem.get("ttl_days"),
            "decay_start_at": mem.get("decay_start_at"),
            "access_count": int(mem.get("access_count", 0)),
        }

        if include_embeddings:
            emb = mem.get("embedding")
            if emb is None:
                try:
                    res = store.collection.get(ids=[mem["id"]], include=["embeddings"])
                    if res and res.get("embeddings"):
                        emb = res["embeddings"][0]
                except Exception:
                    emb = None
            entry["embedding"] = emb

        exportable.append(entry)

    export_data: dict[str, Any] = {
        "mesh_version": MESH_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "memory_count": len(exportable),
        "skipped_local_only": skipped_count,
        "memories": exportable,
    }

    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
        print(f"[OK] Exported {len(exportable)} memories to {path}")
        if skipped_count > 0:
            print(f"  (Skipped {skipped_count} local_only memories — these are never exported)")

    return export_data


def import_namespace(
    input_path: str,
    target_namespace: Optional[str] = None,
    merge_strategy: str = "skip_existing",
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Import memories from a JSON export file into a namespace.

    Args:
        input_path: Path to the JSON export file.
        target_namespace: Override the namespace from the export file.
                          If None, uses the namespace stored in the export.
        merge_strategy: How to handle memories that already exist:
                        "skip_existing" — skip if ID already exists (default, safe)
                        "overwrite" — overwrite if ID already exists
        dry_run: If True, print what would happen without writing anything.

    Returns:
        Dict with import stats: imported, skipped, errors, namespace.
    """
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"Export file not found: {input_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or "mesh_version" not in data or "memories" not in data:
        raise ValueError("Invalid Mesh export file format.")

    namespace = target_namespace or data.get("namespace", "shared")
    memories = data.get("memories", [])

    stats: dict[str, Any] = {
        "imported": 0,
        "skipped": 0,
        "errors": 0,
        "namespace": namespace,
    }

    if dry_run:
        print(f"[DRY RUN] Would import {len(memories)} memories into namespace '{namespace}'")
        print(f"[DRY RUN] merge_strategy: {merge_strategy}")
        for mem in memories[:5]:
            mtype = mem.get("memory_type", "general")
            content_preview = str(mem.get("content", ""))[:80]
            print(f"  - [{mtype}] {content_preview}...")
        if len(memories) > 5:
            print(f"  ... and {len(memories) - 5} more")
        return stats

    store = MeshStore(namespace=namespace)

    # First pass: determine which memories need importing and which need fresh embeddings
    to_import: list[tuple[str, dict[str, Any]]] = []
    texts_to_embed: list[str] = []
    embed_Owner_indices: list[int] = []

    for mem in memories:
        try:
            content = str(mem.get("content") or "").strip()
            if not content:
                stats["errors"] += 1
                continue

            mem_id = str(mem.get("id") or uuid.uuid4())
            existing = store.get(mem_id) is not None

            if existing and merge_strategy == "skip_existing":
                stats["skipped"] += 1
                continue

            idx = len(to_import)
            to_import.append((mem_id, mem))

            if not mem.get("embedding"):
                embed_Owner_indices.append(idx)
                texts_to_embed.append(content)
        except Exception as e:
            stats["errors"] += 1
            print(f"  Error preparing memory '{str(mem.get('content', ''))[:50]}': {e}")

    # Batch-compute any missing embeddings in one forward pass
    computed_embeddings: dict[int, list[float]] = {}
    if texts_to_embed:
        try:
            vecs = _embed_batch(texts_to_embed)
            for item_idx, vec in zip(embed_Owner_indices, vecs):
                computed_embeddings[item_idx] = vec
        except Exception as e:
            print(f"  Error batch-embedding imported memories: {e}")

    now_iso = datetime.now(timezone.utc).isoformat()

    for idx, (mem_id, mem) in enumerate(to_import):
        try:
            content = str(mem["content"]).strip()
            embedding = mem.get("embedding") or computed_embeddings.get(idx)
            if not embedding:
                raise ValueError("Missing embedding vector")

            raw_type = str(mem.get("memory_type", "general")).strip().lower()
            mem_type = raw_type if raw_type in VALID_MEMORY_TYPES else _normalize_memory_type(raw_type)
            tags = _parse_tags(mem.get("tags", []))
            created_at = mem.get("created_at") or now_iso
            ttl_days = mem.get("ttl_days")
            decay_start_at = mem.get("decay_start_at") or (created_at if ttl_days is not None else None)

            store.upsert(
                memory_id=mem_id,
                content=content,
                embedding=embedding,
                metadata={
                    "memory_type": mem_type,
                    "confidence": float(mem.get("confidence", 1.0)),
                    "source_agent": mem.get("source_agent", "mesh-import"),
                    "tags": tags,
                    "created_at": created_at,
                    "accessed_at": now_iso,
                    "access_count": int(mem.get("access_count", 0)),
                    "ttl_days": ttl_days,
                    "decay_start_at": decay_start_at,
                },
                local_only=bool(mem.get("local_only", False)),
            )
            stats["imported"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"  Error importing memory '{str(mem.get('content', ''))[:50]}': {e}")

    print(
        f"[OK] Import complete: {stats['imported']} imported, "
        f"{stats['skipped']} skipped, {stats['errors']} errors"
    )
    return stats