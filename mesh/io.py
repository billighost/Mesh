"""
mesh/io.py — High-speed export and import for Mesh stores.

Addresses Findings:
  - S1: Preserves and validates `secret_ref` locator metadata across export and import
  - S5: Supports authenticated encrypted exports (`encrypt=True`, `passphrase=...`) and
        automatic decryption on `import_namespace()` via `mesh.crypto`
  - S8: Excludes `quarantined=True` memories from exports by default (`include_quarantined=False`)
        and preserves `trust_tier` / `quarantined` metadata on import
  - S11: Applies both `guard_ingest()` (on content) and `guard_tags()` (on tags) to every
         imported memory
  - N6: Rejects symlinked `output_path` and `input_path` via `reject_symlink()` and `O_NOFOLLOW`
"""
from __future__ import annotations

import json
import contextlib
import math
import os
import re
import stat
from .sqlite_runtime import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .audit import (
    ensure_secure_mesh_dir,
    reject_symlink,
    secure_file_permissions,
    validate_namespace,
)
from .crypto import decrypt_export_payload, encrypt_export_payload
from .guards import guard_ingest, guard_tags
from .envutil import env_int
from .memory import (
    MAX_CONTENT_LENGTH,
    VALID_MEMORY_TYPES,
    _embed_batch_with_tag,
    _hash_embed_384,
    _parse_tags,
    _register_reembed_namespace,
)
from .passive import _normalize_memory_type
from .secrets import normalize_secret_ref
from .store import (
    EMBEDDING_DIM,
    MeshStore,
    _normalize_trust_tier,
    _parse_iso_utc,
    flush_all_access_tracking,
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
    """
    Read `{memory_id: is_local_only}` from an existing namespace DB without creating a new DB file.

    Fails *closed*: a genuine read failure (locked/corrupt DB) raises instead of returning
    `{}`, because an empty map would disable both the private-overwrite guard and the
    `skip_existing` check. Only an absent DB/table/column is treated as "nothing exists".
    """
    db_path = ensure_secure_mesh_dir().resolve() / f"mesh_{namespace}.db"
    if not db_path.exists():
        return {}
    reject_symlink(db_path)
    out: dict[str, bool] = {}
    try:
        conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
        try:
            from .store import _check_namespace_binding

            _check_namespace_binding(conn, namespace)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)")}
            if not columns:
                return {}
            if "local_only" in columns:
                rows = conn.execute(
                    "SELECT id, local_only FROM memories WHERE namespace = ?",
                    (namespace,),
                )
            else:
                # Pre-privacy schema: `local_only` did not exist, so no row can be private.
                rows = [
                    (mid, 0)
                    for (mid,) in conn.execute(
                        "SELECT id FROM memories WHERE namespace = ?",
                        (namespace,),
                    )
                ]
            for mid, lo in rows:
                out[str(mid)] = bool(lo)
        finally:
            conn.close()
    except Exception as exc:
        raise RuntimeError(
            f"Could not read existing namespace '{namespace}' before import; "
            f"refusing to merge without the private-memory guard ({exc})."
        ) from exc
    return out


@contextlib.contextmanager
def _atomic_export_file(path: Path):
    """Publish a complete private file; a failed export preserves the old backup."""
    reject_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink(path.parent)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    reject_symlink(temporary)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        secure_file_permissions(temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        reject_symlink(path.parent)
        os.replace(temporary, path)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _export_memory(mem: dict[str, Any], include_embeddings: bool) -> dict[str, Any]:
    entry = {
        "id": mem["id"], "content": mem["content"],
        "memory_type": mem.get("memory_type", "general"),
        "confidence": float(mem.get("confidence", 1.0)),
        "source_agent": mem.get("source_agent", "unknown"),
        "tags": _parse_tags(mem.get("tags", "[]")), "local_only": False,
        "trust_tier": _normalize_trust_tier(mem.get("trust_tier"), default="user"),
        "quarantined": bool(mem.get("quarantined", False)),
        "created_at": mem.get("created_at", ""), "ttl_days": mem.get("ttl_days"),
        "decay_start_at": mem.get("decay_start_at"),
        "access_count": max(0, int(mem.get("access_count") or 0)),
        "embedding_model": mem.get("embedding_model", "needs-reembed"),
    }
    if mem.get("secret_ref"):
        entry["secret_ref"] = mem["secret_ref"]
    if include_embeddings:
        entry["embedding"] = mem.get("embedding")
    return entry


def _stream_json_export(store: MeshStore, path: Path, include_embeddings: bool, include_quarantined: bool) -> dict[str, Any]:
    records = store.iter_export_records(include_embeddings, include_quarantined)
    try:
        kind, counts = next(records)
        if kind != "counts":
            raise RuntimeError("Invalid export snapshot.")
        header = {
            "mesh_version": VERSION, "exported_at": datetime.now(timezone.utc).isoformat(),
            "namespace": store.namespace, **counts,
        }
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        pending = next(records, None)
        with _atomic_export_file(path) as handle:
            handle.write(encoder.encode(header)[:-1])
            for record_kind, field in (("memory", "memories"), ("work_item", "work_items"), ("work_event", "work_events")):
                handle.write(',"' + field + '":[')
                first = True
                while pending is not None and pending[0] == record_kind:
                    if not first:
                        handle.write(",")
                    value = _export_memory(pending[1], include_embeddings) if record_kind == "memory" else pending[1]
                    for fragment in encoder.iterencode(value):
                        handle.write(fragment)
                    first = False
                    pending = next(records, None)
                handle.write("]")
            if pending is not None:
                raise RuntimeError("Invalid export record order.")
            handle.write("}\n")
        return {**header, "streamed": True}
    finally:
        records.close()


class ImportWriteError(RuntimeError):
    """Import stopped without replaying an unconfirmed transaction."""

    def __init__(self, confirmed_imported: int, memory_ids: tuple[str, ...]):
        self.confirmed_imported = confirmed_imported
        self.unknown_outcome_ids = memory_ids
        super().__init__(
            f"Import stopped after {confirmed_imported} confirmed memories; "
            f"inspect the {len(memory_ids)} last-batch IDs before retrying an overwrite."
        )


def _import_metadata(mem: dict[str, Any], namespace: str, now: str, model: str) -> dict[str, Any]:
    raw_type = str(mem.get("memory_type", "general")).strip().lower()
    memory_type = raw_type if raw_type in VALID_MEMORY_TYPES else _normalize_memory_type(raw_type)
    try:
        confidence = float(mem.get("confidence", 1.0))
        if not math.isfinite(confidence):
            confidence = 1.0
    except (ValueError, TypeError):
        confidence = 1.0
    try:
        access_count = max(0, int(float(mem.get("access_count", 0))))
    except (ValueError, TypeError, OverflowError):
        access_count = 0
    raw_agent = str(mem.get("source_agent", "mesh-import") or "mesh-import")
    agent = re.sub(r"[\x00-\x1f\x7f]+", "", raw_agent).strip()[:64] or "mesh-import"
    created = _parse_iso_utc(mem.get("created_at")) if mem.get("created_at") else None
    created_at = created.isoformat() if created else now
    ttl = mem.get("ttl_days")
    if ttl is not None:
        try:
            ttl = float(ttl)
            if not math.isfinite(ttl) or ttl <= 0:
                ttl = None
        except (ValueError, TypeError):
            ttl = None
    decay = _parse_iso_utc(mem.get("decay_start_at")) if mem.get("decay_start_at") else None
    return {
        "memory_type": memory_type, "confidence": max(0.0, min(1.0, confidence)),
        "source_agent": agent, "tags": _parse_tags(mem.get("tags", [])),
        "created_at": created_at, "updated_at": now, "accessed_at": now,
        "access_count": access_count, "ttl_days": ttl,
        "decay_start_at": decay.isoformat() if decay else created_at if ttl is not None else None,
        "embedding_model": model, "trust_tier": _normalize_trust_tier(mem.get("trust_tier"), default="user"),
        "quarantined": bool(mem.get("quarantined", False)),
        "secret_ref": normalize_secret_ref(mem.get("secret_ref"), default_namespace=namespace),
    }


def _commit_import_records(store: MeshStore, records: list[tuple[str, dict[str, Any], Optional[list[float]], Optional[str]]], strategy: str, stats: dict[str, Any]) -> None:
    batch_limit = min(
        env_int("MESH_IMPORT_BATCH_SIZE", 32, minimum=1, maximum=128),
        env_int("MESH_MAX_BULK_ITEMS", 1000, minimum=1),
    )
    byte_limit = env_int("MESH_IMPORT_BATCH_BYTES", 256 * 1024, minimum=65536, maximum=4 * 1024 * 1024)
    offset = 0
    while offset < len(records):
        batch = []
        batch_bytes = 0
        while offset < len(records) and len(batch) < batch_limit:
            record = records[offset]
            charge = len(record[1]["content"].encode("utf-8")) + 16384
            if batch and batch_bytes + charge > byte_limit:
                break
            batch.append(record)
            batch_bytes += charge
            offset += 1
        missing = [index for index, (_, _, vector, model) in enumerate(batch) if vector is None or model is None]
        computed = {}
        if missing:
            try:
                vectors, model = _embed_batch_with_tag([batch[index][1]["content"] for index in missing])
                if len(vectors) != len(missing):
                    raise ValueError("Embedding batch returned an incomplete result.")
                for index, vector in zip(missing, vectors):
                    if len(vector) == EMBEDDING_DIM and all(math.isfinite(value) for value in vector):
                        computed[index] = (vector, model)
            except Exception:
                # Encoding has no database effects; a coherent degraded batch is safe.
                computed.clear()
        now = datetime.now(timezone.utc).isoformat()
        items = []
        for index, (memory_id, mem, vector, model) in enumerate(batch):
            try:
                if vector is None or model is None:
                    vector, model = computed.get(index, (None, None))
                if vector is None or model is None:
                    vector, model = _hash_embed_384(mem["content"]).tolist(), "hash-384"
                metadata = _import_metadata(mem, store.namespace, now, model)
                items.append((memory_id, mem["content"], vector, metadata, bool(mem["local_only"])))
            except (ValueError, TypeError, OverflowError):
                stats["errors"] += 1
        if not items:
            continue
        admitted = items
        skipped = 0
        try:
            with store.write_transaction():
                # Preparation may race another writer. Repeat equality/privacy
                # preconditions inside the transaction that applies the records.
                with store._connect() as conn:
                    placeholders = ",".join("?" for _ in items)
                    existing = dict(conn.execute(
                        f"SELECT id,local_only FROM memories WHERE namespace=? AND id IN ({placeholders})",
                        (store.namespace, *(item[0] for item in items)),
                    ))
                admitted = [item for item in items if item[0] not in existing or strategy == "overwrite" and not existing[item[0]]]
                skipped = len(items) - len(admitted)
                if admitted:
                    store.upsert_batch(admitted, include_private=False)
                    store.after_commit(lambda: _register_reembed_namespace(store))
        except Exception as exc:
            raise ImportWriteError(stats["imported"], tuple(item[0] for item in admitted)) from exc
        stats["imported"] += len(admitted)
        stats["skipped"] += skipped


def sanitize_csv_cell(value: Any) -> Any:
    """
    Neutralize spreadsheet formula injection (CSV Injection / CWE-1236).
    If a string value starts with =, +, -, @, \\t, or \\r (or starts with whitespace
    followed by =, +, -, @), prefix with a single quote (') so spreadsheet applications
    (Excel, Google Sheets, Calc) treat it strictly as text.
    """
    if not isinstance(value, str):
        return value
    if not value:
        return value
    stripped = value.lstrip(" \t\r\n")
    if stripped and stripped[0] in ("=", "+", "-", "@"):
        return "'" + value
    if value[0] in ("\t", "\r"):
        return "'" + value
    return value


def export_to_csv(export_data: dict[str, Any]) -> str:
    """
    Render exported memories to CSV format with formula injection neutralization.
    Vectors are excluded from CSV exports.
    """
    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")

    headers = [
        "id",
        "memory_type",
        "confidence",
        "content",
        "tags",
        "source_agent",
        "trust_tier",
        "created_at",
        "ttl_days",
        "secret_ref",
    ]
    writer.writerow(headers)

    for mem in export_data.get("memories", []):
        tags_raw = mem.get("tags") or []
        tags_str = ", ".join(tags_raw) if isinstance(tags_raw, list) else str(tags_raw)
        secret_ref_str = json.dumps(mem["secret_ref"]) if mem.get("secret_ref") else ""
        row = [
            sanitize_csv_cell(mem.get("id", "")),
            sanitize_csv_cell(mem.get("memory_type", "")),
            mem.get("confidence", 1.0),
            sanitize_csv_cell(mem.get("content", "")),
            sanitize_csv_cell(tags_str),
            sanitize_csv_cell(mem.get("source_agent", "")),
            sanitize_csv_cell(mem.get("trust_tier", "")),
            sanitize_csv_cell(mem.get("created_at", "")),
            mem.get("ttl_days") if mem.get("ttl_days") is not None else "",
            sanitize_csv_cell(secret_ref_str),
        ]
        writer.writerow(row)

    return output.getvalue()


def _fence_markdown(text: str) -> str:
    """
    Safely enclose arbitrary text in a fenced code block so content
    cannot inject markdown headings, links, or HTML tags into exports.
    """
    if not text:
        return ""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}text\n{text}\n{fence}"


def export_to_markdown(export_data: dict[str, Any]) -> str:
    """
    Render export data to structured Markdown format.
    Memory content is rendered in fenced code blocks to prevent markdown or HTML injection.
    Vectors are excluded from Markdown exports.
    """
    ns = export_data.get("namespace", "shared")
    exported_at = export_data.get("exported_at", "")
    memories = export_data.get("memories", [])
    work_items = export_data.get("work_items", [])

    lines = [
        f"# Mesh Export: {ns}",
        "",
        f"- **Namespace**: `{ns}`",
        f"- **Exported at**: {exported_at}",
        f"- **Mesh Version**: v{export_data.get('mesh_version', VERSION)}",
        f"- **Memory Count**: {len(memories)}",
        f"- **Work Items Count**: {len(work_items)}",
        "",
        "---",
        "",
        "## Memories",
        "",
    ]

    if not memories:
        lines.append("*No memories exported.*")
        lines.append("")
    else:
        for mem in memories:
            mid = mem.get("id", "unknown")
            mtype = mem.get("memory_type", "fact").upper()
            conf = mem.get("confidence", 1.0)
            tags = mem.get("tags", [])
            tags_str = ", ".join(f"`{t}`" for t in tags) if tags else "none"
            agent = mem.get("source_agent", "unknown")
            tier = mem.get("trust_tier", "user")
            created = mem.get("created_at", "")
            content = mem.get("content", "")

            lines.append(f"### [{mid}] {mtype} (Confidence: {conf})")
            lines.append(f"- **Tags**: {tags_str}")
            lines.append(f"- **Source**: `{agent}` | **Tier**: `{tier}` | **Created**: {created}")
            if mem.get("secret_ref"):
                lines.append(f"- **Secret Reference**: `{json.dumps(mem['secret_ref'])}`")
            lines.append("")
            lines.append(_fence_markdown(content))
            lines.append("")

    if work_items:
        lines.append("---")
        lines.append("")
        lines.append("## Work Items")
        lines.append("")
        for w in work_items:
            wid = w.get("short_id") or w.get("id") or "task"
            raw_title = str(w.get("title", ""))
            safe_title = raw_title.replace("\r", " ").replace("\n", " ").replace("`", "'")
            status = str(w.get("status", "todo")).upper()
            priority = w.get("priority", "normal")
            summary = w.get("completion_summary") or ""
            evidence = w.get("evidence") or []

            lines.append(f"### [{wid}] {safe_title}")
            lines.append(f"- **Status**: `{status}` | **Priority**: `{priority}`")
            if summary:
                lines.append(f"- **Completion Summary**:\n{_fence_markdown(summary)}")
            if evidence:
                lines.append(f"- **Evidence**: {', '.join(evidence)}")
            lines.append("")

    return "\n".join(lines) + "\n"


def export_namespace(
    namespace: str = "shared",
    output_path: Optional[str] = None,
    format: str = "json",
    include_embeddings: Optional[bool] = None,
    encrypt: bool = False,
    passphrase: Optional[str] = None,
    include_quarantined: bool = False,
    *,
    materialize: bool = True,
) -> dict[str, Any]:
    """
    Export all non-private (and by default non-quarantined) memories from an existing
    namespace to a JSON, Markdown, or CSV file.

    Format notes:
      - 'json': Lossless round-trip export format supported by `mesh import`. Supports --encrypt.
      - 'markdown': One-way human-readable summary. Embeddings are omitted by default.
      - 'csv': One-way spreadsheet format. Embeddings are omitted by default. Neutralizes
               spreadsheet formula injection (CWE-1236) by prepending single quotes (') to
               cells beginning with =, +, -, @, \\t, or \\r.
    """
    clean_ns = validate_namespace(namespace)
    clean_fmt = (format or "json").lower().strip()
    if output_path and not format:
        p_str = output_path.lower()
        if p_str.endswith(".md") or p_str.endswith(".markdown"):
            clean_fmt = "markdown"
        elif p_str.endswith(".csv"):
            clean_fmt = "csv"
        else:
            clean_fmt = "json"

    if clean_fmt not in ("json", "markdown", "csv"):
        raise ValueError(
            f"Unsupported export format '{clean_fmt}'. Supported formats: 'json', 'markdown', 'csv'."
        )

    if encrypt and clean_fmt != "json":
        raise ValueError(
            f"Encrypted export is currently only supported for JSON format (requested '{clean_fmt}')."
        )

    if include_embeddings is None:
        incl_emb = (clean_fmt == "json")
    else:
        incl_emb = bool(include_embeddings)

    if type(materialize) is not bool:
        raise ValueError("materialize must be a boolean.")
    if not materialize and (not output_path or clean_fmt != "json" or encrypt or passphrase is not None and passphrase.strip()):
        raise ValueError("Streaming export requires an output path and unencrypted JSON.")

    flush_all_access_tracking()
    store = MeshStore(namespace=clean_ns, create=False)
    if not materialize:
        return _stream_json_export(store, Path(output_path), incl_emb, include_quarantined)
    total_count = store.count()
    raw_exportable = store.iter_exportable_memories(
        include_embeddings=incl_emb,
        include_quarantined=include_quarantined,
    )

    exportable: list[dict[str, Any]] = []
    for mem in raw_exportable:
        if mem.get("local_only"):
            continue
        if not include_quarantined and mem.get("quarantined"):
            continue
        exportable.append(_export_memory(mem, incl_emb))

    exportable_work_items, exportable_work_events, total_work_items, skipped_private_work_items = (
        store.get_exportable_work_items(include_private=False)
    )

    skipped_count = max(0, total_count - len(exportable))
    export_data: dict[str, Any] = {
        "mesh_version": VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "namespace": clean_ns,
        "memory_count": len(exportable),
        "skipped_local_only": skipped_count,
        "memories": exportable,
        "work_items_count": len(exportable_work_items),
        "skipped_local_only_work_items": skipped_private_work_items,
        "work_items": exportable_work_items,
        "work_events": exportable_work_events,
    }

    payload_to_write = (
        encrypt_export_payload(export_data, passphrase=passphrase)
        if (encrypt or (passphrase is not None and passphrase.strip()))
        else export_data
    )

    if output_path:
        path = Path(output_path)
        with _atomic_export_file(path) as handle:
            if clean_fmt == "markdown":
                handle.write(export_to_markdown(export_data))
            elif clean_fmt == "csv":
                handle.write(export_to_csv(export_data))
            else:
                encoder = json.JSONEncoder(indent=2, ensure_ascii=False, allow_nan=False)
                for fragment in encoder.iterencode(payload_to_write):
                    handle.write(fragment)
                handle.write("\n")
        if hasattr(sys.stderr, "isatty") and sys.stderr.isatty() and not os.environ.get("MESH_QUIET"):
            from . import ui

            ui.success(
                f"Exported {ui.plural(len(exportable), 'memory', 'memories')} ({clean_fmt})",
                ui.short_path(path),
                stream=sys.stderr,
            )
            if skipped_count > 0:
                ui.note(f"{ui.plural(skipped_count, 'private memory', 'private memories')} left out — private memories are never exported.")

    return payload_to_write


def import_namespace(
    input_path: str,
    target_namespace: Optional[str] = None,
    merge_strategy: str = "skip_existing",
    dry_run: bool = False,
    passphrase: Optional[str] = None,
) -> dict[str, Any]:
    """
    Import memories from a JSON export file (plaintext or encrypted) into a validated namespace.

    Args:
        input_path: Path to the JSON export file.
        target_namespace: Override the namespace from the export file.
        merge_strategy: "skip_existing" or "overwrite" (raises ValueError otherwise).
        dry_run: If True, compute real preview stats (`would_import`, `would_skip`, `would_error`)
                 using the exact same validation without creating or writing to any database.
        passphrase: Optional passphrase to decrypt a `mesh_encrypted_export` file.

    Returns:
        Dict with import stats: `imported`, `skipped`, `errors`, `flagged`, `namespace`
        (plus `would_import`, `would_skip`, `would_error` on `dry_run=True`).
    """
    clean_strategy = (merge_strategy or "").strip().lower()
    if clean_strategy not in _VALID_MERGE_STRATEGIES:
        raise ValueError(
            f"Invalid merge_strategy '{merge_strategy}'. Must be one of {sorted(_VALID_MERGE_STRATEGIES)}."
        )

    path = Path(input_path).expanduser()
    reject_symlink(path)
    if not path.exists():
        raise FileNotFoundError(f"Export file not found: {input_path}")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Import requires a regular JSON file.")
    maximum = env_int("MESH_MAX_IMPORT_BYTES", 128 * 1024 * 1024, minimum=65536)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Import requires a regular JSON file.")
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("Import exceeds MESH_MAX_IMPORT_BYTES; split the file or set an explicit larger limit.")
    # Never cache plaintext from encrypted exports or bypass a new passphrase.
    raw_data = json.loads(raw)
    del raw
    data = decrypt_export_payload(raw_data, passphrase=passphrase) if isinstance(raw_data, dict) and raw_data.get("mesh_encrypted_export") else raw_data

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

    flush_all_access_tracking()
    existing_privacy_map = _load_existing_id_privacy_map(namespace)
    seen_ids: set[str] = set(existing_privacy_map.keys())
    file_seen_ids: set[str] = set()

    stats: dict[str, Any] = {
        "imported": 0,
        "skipped": 0,
        "errors": 0,
        "flagged": 0,
        "namespace": namespace,
    }

    to_import: list[tuple[str, dict[str, Any], Optional[list[float]], Optional[str]]] = []

    for mem in memories:
        if not isinstance(mem, dict):
            stats["errors"] += 1
            continue
        try:
            raw_id_early = str(mem.get("id") or "").strip()
            if raw_id_early:
                try:
                    cand_id_early = validate_memory_id(raw_id_early)
                    if cand_id_early in file_seen_ids:
                        stats["skipped"] += 1
                        continue
                    if clean_strategy == "skip_existing" and cand_id_early in seen_ids:
                        file_seen_ids.add(cand_id_early)
                        stats["skipped"] += 1
                        continue
                except ValueError:
                    pass

            raw_content = str(mem.get("content") or "").strip()
            if not raw_content or len(raw_content) > MAX_CONTENT_LENGTH:
                stats["errors"] += 1
                continue

            orig_local_only = bool(mem.get("local_only", False))
            guard_res = guard_ingest(raw_content, local_only=orig_local_only)
            if guard_res.rejected:
                stats["errors"] += 1
                continue

            # Finding S11: Validate tags through `guard_tags` on import
            raw_tags = _parse_tags(mem.get("tags", []))
            try:
                clean_tags, tags_priv, _ = guard_tags(raw_tags, local_only=guard_res.local_only)
            except ValueError:
                stats["errors"] += 1
                continue

            final_local = bool(guard_res.local_only or tags_priv)
            if final_local and not orig_local_only:
                stats["flagged"] += 1

            content = guard_res.content
            mem_copy = dict(mem)
            mem_copy["local_only"] = final_local
            mem_copy["content"] = content
            mem_copy["tags"] = clean_tags

            raw_id = str(mem_copy.get("id") or "").strip()
            mem_id: Optional[str] = None
            if raw_id:
                try:
                    mem_id = validate_memory_id(raw_id)
                except ValueError:
                    mem_id = None
            if mem_id is None:
                mem_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mesh:{namespace}:{content}"))

            if mem_id in file_seen_ids:
                stats["skipped"] += 1
                continue
            file_seen_ids.add(mem_id)

            if clean_strategy == "skip_existing":
                if mem_id in seen_ids:
                    stats["skipped"] += 1
                    continue
                seen_ids.add(mem_id)
            elif clean_strategy == "overwrite":
                if existing_privacy_map.get(mem_id, False):
                    stats["skipped"] += 1
                    continue
                seen_ids.add(mem_id)

            file_model_tag = str(mem_copy.get("embedding_model") or "").strip().lower()
            raw_emb = mem_copy.get("embedding")
            valid_vec: Optional[list[float]] = None
            valid_tag: Optional[str] = None
            if raw_emb is not None and content == raw_content and _is_valid_file_vector(raw_emb, file_model_tag):
                valid_vec = [float(x) for x in raw_emb]
                valid_tag = file_model_tag

            to_import.append((mem_id, mem_copy, valid_vec, valid_tag))
        except Exception as e:
            stats["errors"] += 1
            from . import ui

            ui.warn(f"Couldn't read one memory in the file: {e}")

    file_work_items = data.get("work_items") or []
    file_work_events = data.get("work_events") or []

    if dry_run:
        stats["would_import"] = len(to_import)
        stats["would_skip"] = stats["skipped"]
        stats["would_error"] = stats["errors"]
        stats["would_import_work_items"] = len(file_work_items)
        from . import ui

        ui.info(
            f"Dry run: would import {ui.plural(stats['would_import'], 'memory', 'memories')} into '{namespace}'",
            f"{stats['would_skip']} would be skipped  {ui.sym('dot')}  {stats['would_error']} errors  {ui.sym('dot')}  strategy: {clean_strategy}\nNothing was written. Re-run without --dry-run to apply.",
            stream=sys.stderr,
        )
        return stats

    store = MeshStore(namespace=namespace, create=True)

    _commit_import_records(store, to_import, clean_strategy, stats)

    if file_work_items:
        w_stats = store.import_work_items(file_work_items, file_work_events, merge_strategy=clean_strategy)
        stats["imported_work_items"] = w_stats["imported_items"]
        stats["skipped_work_items"] = w_stats["skipped_items"]
        stats["imported_work_events"] = w_stats["imported_events"]
        stats["errors"] += w_stats["errors"]

    if hasattr(sys.stderr, "isatty") and sys.stderr.isatty() and not os.environ.get("MESH_QUIET"):
        from . import ui

        _summary = f"{stats['skipped']} skipped (already present)  {ui.sym('dot')}  {stats['errors']} errors"
        if stats["errors"]:
            ui.warn(f"Imported {ui.plural(stats['imported'], 'memory', 'memories')} with problems", _summary)
        else:
            ui.success(f"Imported {ui.plural(stats['imported'], 'memory', 'memories')}", _summary, stream=sys.stderr)
    return stats
