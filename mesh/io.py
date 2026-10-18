"""
mesh/io.py — High-speed export and import for Mesh memory stores.

Security & Integrity:
  - Validates target namespaces against path traversal (`validate_namespace`)
  - Uses `create=False` on `export_namespace` and `import_namespace(..., dry_run=True)` so read/preview
    paths never create empty `.db` files on disk
  - Validates `merge_strategy` (`"skip_existing"` or `"overwrite"`) upfront
  - Applies `guard_ingest()` to every imported memory (rejecting prompt poison, redacting credentials,
    and forcing `local_only=True`)
  - Validates 384-dimensional vectors (rejecting all-zero vectors, norms outside `0.5..1.5`, unknown
    `embedding_model` tags, or fake `onnx-minilm-l6-v2` tags on `hash-384` watermarked vectors),
    re-embedding with `_embed_batch_with_tag` and storing the actual returned tag
  - Writes all status messages to `sys.stderr` so MCP stdio channels are never polluted
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .audit import ensure_secure_mesh_dir, secure_file_permissions, validate_namespace
from .guards import guard_ingest
from .memory import (
    MAX_CONTENT_LENGTH,
    VALID_MEMORY_TYPES,
    _PENDING_REEMBED_LOCK,
    _PENDING_REEMBED_QUEUE,
    _embed_batch_with_tag,
    _parse_tags,
)
from .passive import _normalize_memory_type
from .store import (
    EMBEDDING_DIM,
    MeshStore,
    _parse_iso_utc,
    flush_all_telemetry,
    validate_memory_id,
)
from .version import VERSION, is_version_compatible

_KNOWN_FILE_EMBEDDING_MODELS = {"onnx-minilm-l6-v2", "hash-384"}
_VALID_MERGE_STRATEGIES = {"skip_existing", "overwrite"}


def _is_valid_file_vector(raw_emb: Any, file_model_tag: str) -> bool:
    """
    Validate a 384-dim vector from an export file (Phase 4.4):
      1. `file_model_tag` must be in `_KNOWN_FILE_EMBEDDING_MODELS` (`onnx-minilm-l6-v2` or `hash-384`).
      2. Must be a 384-element list of finite numbers.
      3. Must not be all zeros, and its L2 norm must be within `[0.5, 1.5]`.
      4. If `file_model_tag == "onnx-minilm-l6-v2"`, reject vectors carrying the `hash-384`
         trailing-zero watermark (`raw_emb[368:]` all zero).
    """
    if file_model_tag not in _KNOWN_FILE_EMBEDDING_MODELS:
        return False
    if not isinstance(raw_emb, list) or len(raw_emb) != EMBEDDING_DIM:
        return False
    sq_sum = 0.0
    non_zero = 0
    for x in raw_emb:
        if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(float(x)):
            return False
        fx = float(x)
        if abs(fx) > 1e-12:
            non_zero += 1
        sq_sum += fx * fx
    if non_zero == 0:
        return False
    norm = math.sqrt(sq_sum)
    if norm < 0.5 or norm > 1.5:
        return False
    if file_model_tag == "onnx-minilm-l6-v2":
        tail_zeros = sum(1 for x in raw_emb[368:] if abs(float(x)) <= 1e-12)
        if tail_zeros == 16:
            return False
    return True


def _load_existing_id_privacy_map(namespace: str) -> dict[str, bool]:
    """Read `{memory_id: is_local_only}` from an existing namespace DB without creating a new DB file."""
    db_path = ensure_secure_mesh_dir().resolve() / f"mesh_{namespace}.db"
    if not db_path.exists():
        return {}
    out: dict[str, bool] = {}
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            for mid, lo in conn.execute(
                "SELECT id, local_only FROM memories WHERE namespace = ?",
                (namespace,),
            ).fetchall():
                out[str(mid)] = bool(lo)
        finally:
            conn.close()
    except Exception:
        pass
    return out


def export_namespace(
    namespace: str = "shared",
    output_path: Optional[str] = None,
    include_embeddings: bool = True,
) -> dict[str, Any]:
    """
    Export all non-private memories from an existing namespace to a JSON file.
    Raises `NamespaceNotFoundError` (a `ValueError` subclass) if the namespace does not exist.
    """
    clean_ns = validate_namespace(namespace)
    flush_all_telemetry()
    store = MeshStore(namespace=clean_ns, create=False)
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
            "access_count": max(0, int(mem.get("access_count") or 0)),
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
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content_str)
        secure_file_permissions(path)
        from . import ui

        ui.success(
            f"Exported {ui.plural(len(exportable), 'memory', 'memories')}",
            ui.short_path(path),
            stream=sys.stderr,
        )
        if skipped_count > 0:
            ui.note(f"{ui.plural(skipped_count, 'private memory', 'private memories')} left out — private memories are never exported.")

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
        merge_strategy: "skip_existing" or "overwrite" (raises ValueError otherwise).
        dry_run: If True, compute real preview stats (`would_import`, `would_skip`, `would_error`)
                 using the exact same validation without creating or writing to any database.

    Returns:
        Dict with import stats: `imported`, `skipped`, `errors`, `flagged`, `namespace`
        (plus `would_import`, `would_skip`, `would_error` on `dry_run=True`).
    """
    clean_strategy = (merge_strategy or "").strip().lower()
    if clean_strategy not in _VALID_MERGE_STRATEGIES:
        raise ValueError(
            f"Invalid merge_strategy '{merge_strategy}'. Must be one of {sorted(_VALID_MERGE_STRATEGIES)}."
        )

    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"Export file not found: {input_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict) or "memories" not in data or not isinstance(data["memories"], list):
        raise ValueError("Invalid Mesh export file format.")

    file_version = data.get("mesh_version")
    if not is_version_compatible(file_version, VERSION):
        raise ValueError(
            f"Export file version '{file_version}' is incompatible with Mesh v{VERSION}."
        )

    raw_ns = target_namespace or data.get("namespace", "shared")
    namespace = validate_namespace(raw_ns)
    memories = data.get("memories", [])

    flush_all_telemetry()
    existing_privacy_map = _load_existing_id_privacy_map(namespace)
    seen_ids: set[str] = set(existing_privacy_map.keys())

    stats: dict[str, Any] = {
        "imported": 0,
        "skipped": 0,
        "errors": 0,
        "flagged": 0,
        "namespace": namespace,
    }

    to_import: list[tuple[str, dict[str, Any], Optional[list[float]], Optional[str]]] = []
    texts_to_embed: list[str] = []
    embed_owner_indices: list[int] = []

    for mem in memories:
        if not isinstance(mem, dict):
            stats["errors"] += 1
            continue
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
            mem_copy = dict(mem)
            mem_copy["local_only"] = guard_res.local_only
            mem_copy["content"] = content

            raw_id = str(mem_copy.get("id") or uuid.uuid4())
            try:
                mem_id = validate_memory_id(raw_id)
            except ValueError:
                mem_id = validate_memory_id(str(uuid.uuid4()))

            if clean_strategy == "skip_existing":
                if mem_id in seen_ids:
                    stats["skipped"] += 1
                    continue
                seen_ids.add(mem_id)
            elif clean_strategy == "overwrite":
                if existing_privacy_map.get(mem_id, False) and not mem_copy.get("local_only"):
                    stats["skipped"] += 1
                    continue
                seen_ids.add(mem_id)

            file_model_tag = str(mem_copy.get("embedding_model") or "").strip().lower()
            raw_emb = mem_copy.get("embedding")
            valid_vec: Optional[list[float]] = None
            valid_tag: Optional[str] = None
            if _is_valid_file_vector(raw_emb, file_model_tag):
                valid_vec = [float(x) for x in raw_emb]
                valid_tag = file_model_tag

            idx = len(to_import)
            to_import.append((mem_id, mem_copy, valid_vec, valid_tag))
            if valid_vec is None and not dry_run:
                embed_owner_indices.append(idx)
                texts_to_embed.append(content)
        except Exception as e:
            stats["errors"] += 1
            from . import ui

            ui.warn(f"Couldn't read one memory in the file: {e}")

    if dry_run:
        stats["would_import"] = len(to_import)
        stats["would_skip"] = stats["skipped"]
        stats["would_error"] = stats["errors"]
        from . import ui

        ui.info(
            f"Dry run: would import {ui.plural(stats['would_import'], 'memory', 'memories')} into '{namespace}'",
            f"{stats['would_skip']} would be skipped  {ui.sym('dot')}  {stats['would_error']} errors  {ui.sym('dot')}  strategy: {clean_strategy}\nNothing was written. Re-run without --dry-run to apply.",
            stream=sys.stderr,
        )
        return stats

    store = MeshStore(namespace=namespace, create=True)

    computed_embeddings: dict[int, tuple[list[float], str]] = {}
    if texts_to_embed:
        try:
            vecs, actual_batch_tag = _embed_batch_with_tag(texts_to_embed)
            for item_idx, vec in zip(embed_owner_indices, vecs):
                if len(vec) == EMBEDDING_DIM and all(math.isfinite(x) for x in vec):
                    computed_embeddings[item_idx] = (vec, actual_batch_tag)
        except Exception as e:
            from . import ui

            ui.warn(f"Couldn't embed the imported memories ({e})", "They'll be imported with fallback embeddings and fixed up later.")

    now_iso = datetime.now(timezone.utc).isoformat()
    batch_items: list[tuple[str, str, list[float], dict[str, Any], bool]] = []
    hash_queued_items: list[tuple[str, str]] = []

    for idx, (mem_id, mem, pre_vec, pre_tag) in enumerate(to_import):
        try:
            content = str(mem["content"]).strip()
            embedding: Optional[list[float]] = pre_vec
            emb_tag: Optional[str] = pre_tag
            if embedding is None or emb_tag is None:
                comp = computed_embeddings.get(idx)
                if comp is not None:
                    embedding, emb_tag = comp
            if not embedding or len(embedding) != EMBEDDING_DIM or not emb_tag:
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

            raw_access = mem.get("access_count", 0)
            try:
                access_val = int(float(raw_access))
            except (ValueError, TypeError):
                access_val = 0
            access_count = max(0, access_val)

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
                "updated_at": now_iso,
                "accessed_at": now_iso,
                "access_count": access_count,
                "ttl_days": ttl_days,
                "decay_start_at": decay_start_at,
                "embedding_model": emb_tag,
            }
            batch_items.append(
                (mem_id, content, embedding, metadata, bool(mem.get("local_only", False)))
            )
            if emb_tag == "hash-384":
                hash_queued_items.append((mem_id, content))
        except Exception as e:
            stats["errors"] += 1
            from . import ui

            ui.warn(f"Couldn't import \"{ui.truncate(str(mem.get('content', '')), 50)}\": {e}")

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
                        embedding_model=metadata.get("embedding_model"),
                    )
                    stats["imported"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    from . import ui

                    ui.warn(f"Couldn't import a memory: {e}")

    if hash_queued_items:
        with _PENDING_REEMBED_LOCK:
            for mid, text in hash_queued_items:
                _PENDING_REEMBED_QUEUE.put(namespace, mid, text)

    from . import ui

    _summary = f"{stats['skipped']} skipped (already present)  {ui.sym('dot')}  {stats['errors']} errors"
    if stats["errors"]:
        ui.warn(f"Imported {ui.plural(stats['imported'], 'memory', 'memories')} with problems", _summary)
    else:
        ui.success(f"Imported {ui.plural(stats['imported'], 'memory', 'memories')}", _summary, stream=sys.stderr)
    return stats