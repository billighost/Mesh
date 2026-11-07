"""Recoverable local namespace publication with one active authoritative database."""
from __future__ import annotations

import contextvars
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .audit import reject_symlink, secure_file_permissions, validate_namespace
from .sqlite_runtime import sqlite3

_RECOVERING = contextvars.ContextVar("mesh_namespace_rename_recovery", default=False)
_LOGGER = logging.getLogger(__name__)
_PREFIX = ".mesh-rename-"


def _phase_hook(phase: str, manifest: dict[str, Any]) -> None:
    """An internal checkpoint for deterministic process-death tests."""


def _sync_file(path: Path) -> None:
    reject_symlink(path)
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        _sync_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _sync_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)
    if sys.platform == "darwin":
        import fcntl

        fcntl.fcntl(descriptor, getattr(fcntl, "F_FULLFSYNC", 51))


def _sync_directory(root: Path) -> None:
    from .namespaces import _sync_namespace_directory

    _sync_namespace_directory(root)


def _durable_replace(source: Path, target: Path, *, replace: bool = False) -> None:
    reject_symlink(source)
    reject_symlink(target)
    if not replace and target.exists():
        raise FileExistsError("Namespace publication target already exists.")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        # Write-through publication complements FULL commits on Windows, where
        # Python cannot fsync a directory handle as it can on POSIX.
        if not move(str(source), str(target), 0x8 | (0x1 if replace else 0)):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)
    _sync_directory(target.parent)


def _manifest_path(root: Path, operation_id: str) -> Path:
    return root / f"{_PREFIX}{operation_id}.json"


def _paths(root: Path, manifest: dict[str, Any]) -> dict[str, Path]:
    operation_id = manifest["operation_id"]
    old, new = manifest["old"], manifest["new"]
    result = {
        "old": root / f"mesh_{old}.db", "new": root / f"mesh_{new}.db",
        "old_audit": root / f"audit_{old}.db", "new_audit": root / f"audit_{new}.db",
        "stage": root / f"{_PREFIX}{operation_id}.stage.db",
        "stage_audit": root / f"{_PREFIX}{operation_id}.stage-audit.db",
        "retired": root / f"{_PREFIX}{operation_id}.retired.db",
        "retired_audit": root / f"{_PREFIX}{operation_id}.retired-audit.db",
    }
    for path in result.values():
        reject_symlink(path)
        if path.resolve().parent != root:
            raise ValueError("Namespace maintenance path escapes its data root.")
    return result


def _write_manifest(root: Path, manifest: dict[str, Any], phase: str) -> None:
    manifest["phase"] = phase
    path = _manifest_path(root, manifest["operation_id"])
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, prefix=_PREFIX,
                                     suffix=".manifest-tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(manifest, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        _sync_descriptor(stream.fileno())
    try:
        secure_file_permissions(temporary)
        _durable_replace(temporary, path, replace=True)
    finally:
        temporary.unlink(missing_ok=True)


def _read_manifest(root: Path, path: Path, *, namespace: str | None = None) -> dict[str, Any] | None:
    from .store import _validate_incarnation

    reject_symlink(path)
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 4096:
        raise ValueError("Invalid namespace maintenance manifest.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("Unsupported namespace maintenance manifest.")
    if namespace is not None and namespace not in (manifest.get("old"), manifest.get("new")):
        return None
    operation_id = _validate_incarnation(manifest.get("operation_id"), "operation_id")
    if path != _manifest_path(root, operation_id):
        raise ValueError("Namespace maintenance manifest identity mismatch.")
    old, new = validate_namespace(manifest.get("old")), validate_namespace(manifest.get("new"))
    if old == new:
        raise ValueError("Namespace maintenance names must differ.")
    for key in ("source_generation", "destination_generation"):
        _validate_incarnation(manifest.get(key), key)
    if type(manifest.get("count")) is not int or manifest["count"] < 0 or type(manifest.get("had_audit")) is not bool:
        raise ValueError("Namespace maintenance accounting is malformed.")
    if manifest.get("phase") not in {"prepared", "staged", "source_hidden", "published", "activated", "rolled_back"}:
        raise ValueError("Namespace maintenance phase is invalid.")
    _paths(root, manifest)
    return manifest


def _metadata(path: Path) -> tuple[str | None, str | None, str | None]:
    reject_symlink(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        rows = dict(connection.execute("SELECT key,value FROM mesh_meta WHERE key IN ('namespace_owner','store_generation','namespace_rename_pending')"))
        return rows.get("namespace_owner"), rows.get("store_generation"), rows.get("namespace_rename_pending")
    finally:
        connection.close()


def _verify_database(path: Path, owner: str, generation: str, pending: str | None, count: int | None) -> None:
    if _metadata(path) != (owner, generation, pending):
        raise RuntimeError("Namespace maintenance database identity changed; no files were discarded.")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Namespace maintenance database integrity check failed.")
        if count is not None and connection.execute("SELECT COUNT(*) FROM memories WHERE namespace=?", (owner,)).fetchone()[0] != count:
            raise RuntimeError("Namespace maintenance memory count changed.")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("memories", "memory_passages", "queries", "contradictions", "work_items", "work_events", "embedding_jobs", "mutation_outbox", "operation_receipts"):
            if table in tables and connection.execute(f"SELECT 1 FROM {table} WHERE namespace!=? LIMIT 1", (owner,)).fetchone():
                raise RuntimeError("Namespace maintenance database has unexpected namespace rows.")
        if "memory_passages" in tables:
            from .passage_search import verify_passage_rows

            verify_passage_rows(connection)
    finally:
        connection.close()


def _set_pending(path: Path, operation_id: str | None, *, generation: str | None = None) -> None:
    from .namespaces import _checkpoint_truncate_or_raise

    connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if operation_id is None:
            connection.execute("DELETE FROM mesh_meta WHERE key='namespace_rename_pending'")
        else:
            connection.execute("INSERT OR REPLACE INTO mesh_meta(key,value) VALUES('namespace_rename_pending',?)", (operation_id,))
        if generation is not None:
            connection.execute("UPDATE mesh_meta SET value=? WHERE key='store_generation'", (generation,))
        connection.commit()
        _checkpoint_truncate_or_raise(connection, path)
    finally:
        connection.close()
    _sync_file(path)


def _stage(root: Path, manifest: dict[str, Any], paths: dict[str, Path]) -> None:
    from .namespaces import _checkpoint_truncate_or_raise, _rebind_namespace_database

    shutil.copy2(paths["old"], paths["stage"])
    secure_file_permissions(paths["stage"])
    _sync_file(paths["stage"])
    connection = sqlite3.connect(paths["stage"], timeout=5.0)
    try:
        _rebind_namespace_database(connection, manifest["old"], manifest["new"], new_generation=manifest["destination_generation"])
        _checkpoint_truncate_or_raise(connection, paths["stage"])
    finally:
        connection.close()
    _set_pending(paths["stage"], manifest["operation_id"], generation=manifest["destination_generation"])
    _verify_database(paths["stage"], manifest["new"], manifest["destination_generation"], manifest["operation_id"], manifest["count"])
    if manifest["had_audit"]:
        shutil.copy2(paths["old_audit"], paths["stage_audit"])
        secure_file_permissions(paths["stage_audit"])
        _sync_file(paths["stage_audit"])
        connection = sqlite3.connect(paths["stage_audit"], timeout=5.0)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            connection.execute("BEGIN IMMEDIATE")
            for table in ("audit_log", "audit_events"):
                if table in tables:
                    connection.execute(f"UPDATE {table} SET namespace=? WHERE namespace=?", (manifest["new"], manifest["old"]))
            connection.commit()
            _checkpoint_truncate_or_raise(connection, paths["stage_audit"])
        finally:
            connection.close()
        _sync_file(paths["stage_audit"])
        status = paths["stage_audit"].stat()
        manifest["stage_audit_identity"] = [status.st_dev, status.st_ino]
    _write_manifest(root, manifest, "staged")
    _phase_hook("staged", manifest)


def _verify_audit(path: Path, manifest: dict[str, Any], *, staged: bool = True) -> None:
    reject_symlink(path)
    status = path.stat()
    identity_key = "stage_audit_identity" if staged else "source_audit_identity"
    owner = manifest["new"] if staged else manifest["old"]
    if [status.st_dev, status.st_ino] != manifest.get(identity_key):
        raise RuntimeError("Namespace maintenance audit identity changed; no files were discarded.")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Namespace maintenance audit integrity check failed.")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("audit_log", "audit_events"):
            if table in tables and connection.execute(f"SELECT 1 FROM {table} WHERE namespace!=? LIMIT 1", (owner,)).fetchone():
                raise RuntimeError("Namespace maintenance audit contains unexpected namespace rows.")
    finally:
        connection.close()


def _cleanup(root: Path, manifest: dict[str, Any], paths: dict[str, Path]) -> None:
    from .namespaces import _remove_sqlite_files

    try:
        if paths["new"].exists() and _metadata(paths["new"]) == (manifest["new"], manifest["destination_generation"], None):
            _verify_database(paths["new"], manifest["new"], manifest["destination_generation"], None, None)
        elif paths["old"].exists() and _metadata(paths["old"]) == (manifest["old"], manifest["source_generation"], None):
            _verify_database(paths["old"], manifest["old"], manifest["source_generation"], None, None)
        else:
            raise RuntimeError("No validated active namespace is available; cleanup was refused.")
        for key in ("retired", "retired_audit", "stage", "stage_audit"):
            path = paths[key]
            if path.exists():
                if key == "retired" and _metadata(path) != (manifest["old"], manifest["source_generation"], manifest["operation_id"]):
                    raise RuntimeError("Retired namespace identity changed; cleanup was refused.")
                if key == "stage" and _metadata(path) not in (
                    (manifest["new"], manifest["destination_generation"], manifest["operation_id"]),
                    (manifest["old"], manifest["source_generation"], manifest["operation_id"]),
                ):
                    raise RuntimeError("Staged namespace identity changed; cleanup was refused.")
                if key in {"retired_audit", "stage_audit"}:
                    _verify_audit(path, manifest, staged=key == "stage_audit")
                _remove_sqlite_files(path, strict=True)
        _manifest_path(root, manifest["operation_id"]).unlink(missing_ok=True)
        _sync_directory(root)
    except Exception:
        # Publication has already committed, or the original was restored.
        # A cleanup failure must not turn that known outcome into an error.
        _LOGGER.warning("Namespace maintenance completed; temporary-file cleanup remains pending.")


def _publish(root: Path, manifest: dict[str, Any], paths: dict[str, Path]) -> None:
    old, new, operation_id = manifest["old"], manifest["new"], manifest["operation_id"]
    if paths["new"].exists():
        metadata = _metadata(paths["new"])
        if metadata == (new, manifest["destination_generation"], None):
            # New writes may have followed activation before temporary cleanup.
            if manifest["had_audit"]:
                _verify_audit(paths["new_audit"], manifest)
            _cleanup(root, manifest, paths)
            return
        _verify_database(paths["new"], new, manifest["destination_generation"], operation_id, manifest["count"])
    else:
        _verify_database(paths["stage"], new, manifest["destination_generation"], operation_id, manifest["count"])
    if paths["old"].exists():
        _verify_database(paths["old"], old, manifest["source_generation"], operation_id, manifest["count"])
        _durable_replace(paths["old"], paths["retired"])
    elif not paths["retired"].exists():
        raise RuntimeError("Namespace maintenance source is missing; recovery was refused.")
    _verify_database(paths["retired"], old, manifest["source_generation"], operation_id, manifest["count"])
    if manifest["had_audit"] and paths["old_audit"].exists():
        _verify_audit(paths["old_audit"], manifest, staged=False)
        _durable_replace(paths["old_audit"], paths["retired_audit"])
    _write_manifest(root, manifest, "source_hidden")
    _phase_hook("source_hidden", manifest)
    if not paths["new"].exists():
        _durable_replace(paths["stage"], paths["new"])
    if manifest["had_audit"]:
        if not paths["new_audit"].exists():
            _verify_audit(paths["stage_audit"], manifest)
            _durable_replace(paths["stage_audit"], paths["new_audit"])
        _verify_audit(paths["new_audit"], manifest)
    _write_manifest(root, manifest, "published")
    _phase_hook("published", manifest)
    _set_pending(paths["new"], None)
    _write_manifest(root, manifest, "activated")
    _phase_hook("activated", manifest)
    _cleanup(root, manifest, paths)


def _rollback(root: Path, manifest: dict[str, Any], paths: dict[str, Path]) -> None:
    if paths["old"].exists() and _metadata(paths["old"]) == (manifest["old"], manifest["source_generation"], None):
        _write_manifest(root, manifest, "rolled_back")
        _cleanup(root, manifest, paths)
        return
    if paths["new"].exists():
        if _metadata(paths["new"]) == (manifest["new"], manifest["destination_generation"], None):
            _cleanup(root, manifest, paths)
            return
        _verify_database(paths["new"], manifest["new"], manifest["destination_generation"], manifest["operation_id"], manifest["count"])
        _durable_replace(paths["new"], paths["stage"])
    if paths["new_audit"].exists():
        _verify_audit(paths["new_audit"], manifest)
        _durable_replace(paths["new_audit"], paths["stage_audit"])
    if not paths["old"].exists() and paths["retired"].exists():
        _verify_database(paths["retired"], manifest["old"], manifest["source_generation"], manifest["operation_id"], manifest["count"])
        _durable_replace(paths["retired"], paths["old"])
    if manifest["had_audit"] and not paths["old_audit"].exists() and paths["retired_audit"].exists():
        _verify_audit(paths["retired_audit"], manifest, staged=False)
        _durable_replace(paths["retired_audit"], paths["old_audit"])
    _verify_database(paths["old"], manifest["old"], manifest["source_generation"], manifest["operation_id"], manifest["count"])
    _set_pending(paths["old"], None)
    _write_manifest(root, manifest, "rolled_back")
    _cleanup(root, manifest, paths)


def recover_namespace_renames(root: Path, namespace: str | None = None) -> list[dict[str, Any]]:
    """Resume only validated journals; normal opens never publish an unverified copy."""
    from .namespaces import _evict_namespace_caches
    from .store import namespace_write_lock

    if _RECOVERING.get():
        return []
    recovered = []
    token = _RECOVERING.set(True)
    try:
        for path in root.glob(f"{_PREFIX}*.json"):
            manifest = _read_manifest(root, path, namespace=namespace)
            if manifest is None:
                continue
            with ExitStack() as stack:
                for name in sorted({manifest["old"], manifest["new"]}):
                    stack.enter_context(namespace_write_lock(name, timeout=30.0, data_root=root))
                if not path.exists():
                    continue
                manifest = _read_manifest(root, path)
                if manifest is None:
                    raise RuntimeError("Namespace maintenance manifest disappeared.")
                paths = _paths(root, manifest)
                for name, key in ((manifest["old"], "old"), (manifest["new"], "new")):
                    _evict_namespace_caches(name, paths[key])
                restored = paths["old"].exists() and not paths["new"].exists() and _metadata(paths["old"]) == (manifest["old"], manifest["source_generation"], None)
                if restored:
                    # A crash may follow clearing the original's marker but
                    # precede persisting the rollback phase.
                    _write_manifest(root, manifest, "rolled_back")
                    _cleanup(root, manifest, paths)
                elif manifest["phase"] in {"prepared", "rolled_back"}:
                    if manifest["phase"] == "rolled_back":
                        _verify_database(paths["old"], manifest["old"], manifest["source_generation"], None, None)
                        _cleanup(root, manifest, paths)
                    else:
                        _rollback(root, manifest, paths)
                else:
                    _publish(root, manifest, paths)
                    recovered.append(manifest)
    finally:
        _RECOVERING.reset(token)
    return recovered


def rename_namespace(old: str, new: str, root: Path) -> int:
    from .namespaces import _checkpoint_truncate_or_raise, _evict_namespace_caches, _reject_namespace_collision
    from .store import MeshStore, NamespaceNotFoundError, namespace_write_lock

    recovered = recover_namespace_renames(root)
    for completed in recovered:
        if (completed["old"], completed["new"]) == (old, new) and not (root / f"mesh_{old}.db").exists():
            return completed["count"]
    manifest: dict[str, Any] | None = None
    token = _RECOVERING.set(True)
    try:
        with ExitStack() as stack:
            for name in sorted({old, new}):
                stack.enter_context(namespace_write_lock(name, timeout=30.0, data_root=root))
            source = root / f"mesh_{old}.db"
            if not source.exists():
                raise NamespaceNotFoundError(f"Namespace '{old}' does not exist")
            _reject_namespace_collision(source, old, "rename")
            original = MeshStore(old, create=False)
            manifest = {"version": 1, "operation_id": uuid.uuid4().hex, "old": old, "new": new,
                        "source_generation": original.store_generation, "destination_generation": uuid.uuid4().hex,
                        "count": original.count(), "had_audit": (root / f"audit_{old}.db").exists(), "phase": "prepared"}
            paths = _paths(root, manifest)
            if manifest["had_audit"]:
                status = paths["old_audit"].stat()
                manifest["source_audit_identity"] = [status.st_dev, status.st_ino]
            if paths["new"].exists() or paths["new_audit"].exists():
                raise ValueError(f"Namespace '{new}' already exists. Delete it first.")
            for name, key in ((old, "old"), (new, "new")):
                _evict_namespace_caches(name, paths[key])
            for key in ("old", "old_audit"):
                if paths[key].exists():
                    connection = sqlite3.connect(paths[key].as_uri() + "?mode=rw", uri=True)
                    try:
                        _checkpoint_truncate_or_raise(connection, paths[key])
                    finally:
                        connection.close()
            _write_manifest(root, manifest, "prepared")
            _set_pending(paths["old"], manifest["operation_id"])
            _phase_hook("prepared", manifest)
            try:
                _stage(root, manifest, paths)
                _publish(root, manifest, paths)
            except Exception:
                committed = paths["new"].exists() and _metadata(paths["new"]) == (new, manifest["destination_generation"], None)
                if committed:
                    _cleanup(root, manifest, paths)
                else:
                    _rollback(root, manifest, paths)
                    raise
            return manifest["count"]
    finally:
        _RECOVERING.reset(token)
