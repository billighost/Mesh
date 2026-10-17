"""
mesh/io.py — High-speed export and import for Mesh memory stores.

Security & Integrity:
  - Validates target namespaces against path traversal (`validate_namespace`)
  - Validates memory IDs via `validate_memory_id()` during imports to prevent stored XSS
  - Verifies export file schema version compatibility via `is_version_compatible()`
  - Enforces 384-dimensional vector validation during imports
  - Fast batch upsert transaction handling
"""
from __future__ import annotations

import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .audit import secure_file_permissions, validate_namespace
from .guards import guard_ingest
from .memory import MAX_CONTENT_LENGTH, VALID_MEMORY_TYPES, _embed_batch, _parse_tags
from .passive import _normalize_memory_type
from .store import EMBEDDING_DIM, MeshStore, _parse_iso_utc, validate_memory_id
from .version import VERSION, is_version_compatible


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
    Returns:
        Export dict (also written to file if output_path is provided).
    """
    clean_ns = validate_namespace(namespace)
    store = MeshStore(namespace=clean_ns)
    total_count = store.count()
    raw_exportable = store.get_exportable_memories(include_embeddings=include_embeddings)

    exportable: list[dict[str, Any]] = []

    for mem in raw_exportable:
        if mem.get("local_only"):
            continue
        mid = mem["id"]
        entry: dict[str, Any] = {
            "id": mid,
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
            "embedding_model": mem.get("embedding_model", "onnx-minilm-l6-v2"),
        }
        if include_embeddings:
            entry["embedding"] = mem.get("embedding")
        exportable.append(entry)

    skipped_count = max(0, total_count - len(exportable))
    export_data: dict[str, Any] = {
        "mesh_version": VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "namespace": clean_ns,
        "memory_count": len(exportable),
        "skipped_local_only": skipped_count,
        "memories": exportable,
    }

    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        content_str = json.dumps(export_data, indent=2, ensure_ascii=False)
        if os.name == "nt":
            with open(path, "w", encoding="utf-8") as f:
                f.write(content_str)
        else:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with open(fd, "w", encoding="utf-8") as f:
                f.write(content_str)
        secure_file_permissions(path)
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
    Import memories from a JSON export file into a validated namespace.
    Args:
        input_path: Path to the JSON export file.
        target_namespace: Override the namespace from the export file.
        merge_strategy: "skip_existing" or "overwrite".
        dry_run: If True, preview import stats without writing anything.
    Returns:
        Dict with import stats: imported, skipped, errors, namespace.
    """
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"Export file not found: {input_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or "memories" not in data:
        raise ValueError("Invalid Mesh export file format.")

    file_version = data.get("mesh_version")
    if not is_version_compatible(file_version, VERSION):
        raise ValueError(
            f"Export file version '{file_version}' is incompatible with Mesh v{VERSION}."
        )

    raw_ns = target_namespace or data.get("namespace", "shared")
    namespace = validate_namespace(raw_ns)
    memories = data.get("memories", [])

    stats: dict[str, Any] = {
        "imported": 0,
        "skipped": 0,
        "errors": 0,
        "flagged": 0,
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
    existing_ids: set[str] = set()
    if merge_strategy == "skip_existing":
        try:
            cache = store._get_vector_cache()
            existing_ids = set(cache.all_ids)
        except Exception:
            existing_ids = set()

    to_import: list[tuple[str, dict[str, Any]]] = []
    texts_to_embed: list[str] = []
    embed_owner_indices: list[int] = []

    for mem in memories:
        try:
            raw_content = str(mem.get("content") or "").strip()
            if not raw_content or len(raw_content) > MAX_CONTENT_LENGTH:
                stats["errors"] += 1
                continue

            orig_local_only = bool(mem.get("local_only", False))
            guard_res = guard_ingest(raw_content, local_only=orig_local_only)
            if guard_res.rejected:
                stats["errors"] += 1
                continue
            if guard_res.local_only and not orig_local_only:
                stats["flagged"] += 1

            content = guard_res.content
            mem["local_only"] = guard_res.local_only
            mem["content"] = content

            raw_id = str(mem.get("id") or uuid.uuid4())
            try:
                mem_id = validate_memory_id(raw_id)
            except ValueError:
                mem_id = validate_memory_id(str(uuid.uuid4()))

            if merge_strategy == "skip_existing":
                if mem_id in existing_ids or (not existing_ids and store.get(mem_id) is not None):
                    stats["skipped"] += 1
                    continue
                existing_ids.add(mem_id)
            elif merge_strategy == "overwrite":
                existing_row = store.get(mem_id)
                if existing_row and existing_row.get("local_only") and not mem.get("local_only"):
                    stats["skipped"] += 1
                    continue

            idx = len(to_import)
            to_import.append((mem_id, mem))

            raw_emb = mem.get("embedding")
            valid_emb = False
            if raw_emb and isinstance(raw_emb, list) and len(raw_emb) == EMBEDDING_DIM:
                if all(isinstance(x, (int, float)) and math.isfinite(x) for x in raw_emb):
                    valid_emb = True

            if not valid_emb:
                embed_owner_indices.append(idx)
                texts_to_embed.append(content)
        except Exception as e:
            stats["errors"] += 1
            print(f"  Error preparing memory: {e}")

    computed_embeddings: dict[int, list[float]] = {}
    if texts_to_embed:
        try:
            vecs = _embed_batch(texts_to_embed)
            for item_idx, vec in zip(embed_owner_indices, vecs):
                if len(vec) == EMBEDDING_DIM and all(math.isfinite(x) for x in vec):
                    computed_embeddings[item_idx] = vec
        except Exception as e:
            print(f"  Error batch-embedding imported memories: {e}")

    now_iso = datetime.now(timezone.utc).isoformat()
    batch_items: list[tuple[str, str, list[float], dict[str, Any], bool]] = []

    for idx, (mem_id, mem) in enumerate(to_import):
        try:
            content = str(mem["content"]).strip()
            raw_emb = mem.get("embedding")
            embedding = None
            if raw_emb and isinstance(raw_emb, list) and len(raw_emb) == EMBEDDING_DIM:
                if all(isinstance(x, (int, float)) and math.isfinite(x) for x in raw_emb):
                    embedding = [float(x) for x in raw_emb]
            if embedding is None:
                embedding = computed_embeddings.get(idx)

            if not embedding or len(embedding) != EMBEDDING_DIM or not all(math.isfinite(x) for x in embedding):
                raise ValueError("Missing or invalid 384-dim embedding vector")

            raw_type = str(mem.get("memory_type", "general")).strip().lower()
            mem_type = (
                raw_type
                if raw_type in VALID_MEMORY_TYPES
                else _normalize_memory_type(raw_type)
            )
            tags = _parse_tags(mem.get("tags", []))

            raw_conf = mem.get("confidence", 1.0)
            try:
                conf_val = float(raw_conf)
                if not math.isfinite(conf_val):
                    conf_val = 1.0
            except (ValueError, TypeError):
                conf_val = 1.0
            confidence = max(0.0, min(1.0, conf_val))

            raw_agent = str(mem.get("source_agent", "mesh-import") or "mesh-import")
            clean_agent = re.sub(r"[\x00-\x1f\x7f]+", "", raw_agent).strip()[:64] or "mesh-import"

            raw_created = mem.get("created_at")
            parsed_created = _parse_iso_utc(raw_created) if raw_created else None
            created_at = parsed_created.isoformat() if parsed_created else now_iso

            ttl_days = mem.get("ttl_days")
            if ttl_days is not None:
                try:
                    ttl_days = float(ttl_days)
                    if not math.isfinite(ttl_days) or ttl_days <= 0:
                        ttl_days = None
                except (ValueError, TypeError):
                    ttl_days = None

            raw_decay = mem.get("decay_start_at")
            parsed_decay = _parse_iso_utc(raw_decay) if raw_decay else None
            decay_start_at = (
                parsed_decay.isoformat()
                if parsed_decay
                else (created_at if ttl_days is not None else None)
            )

            metadata = {
                "memory_type": mem_type,
                "confidence": confidence,
                "source_agent": clean_agent,
                "tags": tags,
                "created_at": created_at,
                "accessed_at": now_iso,
                "access_count": int(mem.get("access_count", 0)),
                "ttl_days": ttl_days,
                "decay_start_at": decay_start_at,
                "embedding_model": mem.get("embedding_model", "onnx-minilm-l6-v2"),
            }
            batch_items.append(
                (mem_id, content, embedding, metadata, bool(mem.get("local_only", False)))
            )
        except Exception as e:
            stats["errors"] += 1
            print(f"  Error importing memory '{str(mem.get('content', ''))[:50]}': {e}")

    if batch_items:
        try:
            store.upsert_batch(batch_items)
            stats["imported"] += len(batch_items)
        except Exception:
            for mem_id, content, embedding, metadata, local_only in batch_items:
                try:
                    store.upsert(
                        memory_id=mem_id,
                        content=content,
                        embedding=embedding,
                        metadata=metadata,
                        local_only=local_only,
                    )
                    stats["imported"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    print(f"  Error importing memory: {e}")

    print(
        f"[OK] Import complete: {stats['imported']} imported, "
        f"{stats['skipped']} skipped, {stats['errors']} errors"
    )
    return stats