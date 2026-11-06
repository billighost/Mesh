"""Thin authenticated benchmark clients and read-only native verification.

HTTP clients must not import Mesh, load an embedding model, start a daemon or
replay a mutation. Public writes and retrieval use the reviewed MeshClient;
only telemetry and independent integrity checks use the helpers below.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import math
import os
import re
import secrets
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

SURFACES = ("bare-sdk", "receipt-sdk", "http")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MODEL_MODULES = ("mesh.memory", "onnxruntime", "torch", "fastembed", "sentence_transformers")


def reject_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError("Benchmark paths must not traverse links or reparse points.")


def _json_object(raw: bytes) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError("HTTP telemetry contains a nonfinite JSON constant.")
    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("HTTP telemetry contains a nonfinite JSON number.")
        return number
    def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("HTTP telemetry contains duplicate JSON fields.")
            result[key] = value
        return result
    result = json.loads(raw, parse_constant=reject_constant, parse_float=finite_float,
                        object_pairs_hook=unique_fields)
    if not isinstance(result, dict):
        raise ValueError("HTTP telemetry must be a JSON object.")
    return result


def authenticated_stats(profile: Any, *, timeout: float = 10.0) -> dict[str, Any]:
    """Verify the configured store's HMAC identity before reading real /stats."""
    from mesh.client import ConnectionProfile, ProfileError, store_identity

    if not isinstance(profile, ConnectionProfile) or profile.endpoint is None or profile.token_file is None:
        raise ProfileError("Telemetry requires an explicit reviewed HTTP connection profile.")
    if not 0 < timeout <= 120:
        raise ValueError("Invalid telemetry timeout.")
    token_path = profile.token_file
    reject_links(token_path)
    with token_path.open("rb") as handle:
        regular = stat.S_ISREG(os.fstat(handle.fileno()).st_mode)
        raw_token = handle.read(1025)
    if not regular or len(raw_token) > 1024:
        raise ProfileError("Invalid telemetry token pointer target.")
    token = raw_token.decode("utf-8").strip()
    if not token or len(token) > 512 or re.search(r"[\x00-\x20\x7f]", token):
        raise ProfileError("Invalid telemetry token pointer target.")
    parsed = urlsplit(profile.endpoint)
    kind = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = kind(parsed.hostname, parsed.port, timeout=timeout)
    try:
        nonce = secrets.token_hex(24)
        connection.request("GET", "/identity?nonce=" + nonce)
        response = connection.getresponse()
        identity_raw = response.read(16385)
        if response.status != 200 or len(identity_raw) > 16384:
            raise ProfileError("Telemetry store identity verification failed.")
        identity = _json_object(identity_raw)
        expected_store = store_identity(profile.mesh_home)
        signature = hmac.new(token.encode(), (nonce + "|" + expected_store).encode(), hashlib.sha256).hexdigest()
        if identity.get("store_id") != expected_store or not hmac.compare_digest(
            str(identity.get("scope_hmac", "")), signature
        ):
            raise ProfileError("Telemetry endpoint did not prove the configured store identity.")
        connection.request("GET", "/stats", headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if response.status != 200 or len(raw) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("Authenticated HTTP telemetry was unavailable or exceeded its byte limit.")
        return _json_object(raw)
    finally:
        connection.close()


def checked_group_stats(stats: Any, enabled: bool) -> dict[str, Any]:
    group = stats.get("group_commit") if isinstance(stats, dict) else None
    if not isinstance(group, dict) or type(group.get("enabled")) is not bool:
        raise RuntimeError("HTTP server did not expose actual group-commit status.")
    if group["enabled"] != enabled:
        raise RuntimeError("HTTP server group-commit status differs from the requested flag.")
    for key in ("committed_batches", "executed_commands"):
        if type(group.get(key)) is not int or group[key] < 0:
            raise RuntimeError("HTTP server did not expose valid group-commit execution counters.")
    if not enabled and (group["committed_batches"] or group["executed_commands"]):
        raise RuntimeError("Disabled group commit reported grouped execution.")
    return dict(group)


def group_execution(before: Any, after: Any, enabled: bool, expected_commands: int) -> dict[str, Any]:
    baseline = checked_group_stats({"group_commit": before}, enabled)
    final = checked_group_stats({"group_commit": after}, enabled)
    batches = final["committed_batches"] - baseline["committed_batches"]
    commands = final["executed_commands"] - baseline["executed_commands"]
    valid = (
        batches >= 0 and commands >= 0 and batches <= commands
        and (not enabled or commands == expected_commands and (batches > 0 or expected_commands == 0))
    )
    return {
        "enabled": enabled, "baseline": baseline, "after_ingestion": final,
        "ingest_committed_batches": batches, "ingest_executed_commands": commands,
        "expected_successful_ingest_commands": expected_commands, "execution_verified": valid,
        "multiple_commands_per_commit_observed": valid and enabled and commands > batches,
        "commands_per_commit": commands / batches if valid and batches else None,
        "boundary": "authenticated_stats_after_seed_to_after_measured_write_acknowledgements",
    }


def thin_client_metadata() -> dict[str, Any]:
    loaded = [name for name in _MODEL_MODULES if name in sys.modules]
    if loaded:
        raise RuntimeError("An HTTP benchmark client imported the memory/model runtime.")
    return {"client_loads_memory_or_model": False, "checked_model_modules": list(_MODEL_MODULES)}


class ReadonlyStore:
    """Observe the server's database using the same patched SQLite library."""
    def __init__(self, mesh_home: Path, namespace: str):
        from mesh.audit import validate_namespace

        self.namespace = validate_namespace(namespace)
        self.db_path = mesh_home / ("mesh_" + self.namespace + ".db")
        reject_links(self.db_path)
        if not self.db_path.is_file():
            raise FileNotFoundError("The benchmark server database does not exist.")

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        from mesh.sqlite_runtime import sqlite3

        reject_links(self.db_path)
        connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True,
                                     isolation_level=None, timeout=0.25)
        try:
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    def wait_for_indexed(self, ids: list[str], *, timeout: float, expected_revisions: dict[str, int],
                         target_model: str, include_private: bool = False) -> dict[str, Any]:
        if include_private or not 0 <= timeout <= 60 or len(ids) > 128:
            raise ValueError("Invalid read-only neural verification bounds.")
        if any(not isinstance(item, str) or not _OPERATION_ID.fullmatch(item) for item in ids):
            raise ValueError("Invalid neural verification ID.")
        if set(ids) != set(expected_revisions) or any(type(value) is not int or value < 1
                                                    for value in expected_revisions.values()):
            raise ValueError("Invalid neural verification revisions.")
        deadline = time.monotonic() + timeout
        indexed: set[str] = set()
        revisions: dict[str, int] = {}
        models: dict[str, str] = {}
        while ids:
            indexed.clear()
            revisions.clear()
            models.clear()
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT id,revision,embedding_model,local_only,quarantined FROM memories "
                    "WHERE namespace=? AND id IN (" + ",".join("?" for _ in ids) + ")",
                    (self.namespace, *ids),
                )
                for memory_id, revision, model, private, quarantined in rows:
                    revisions[memory_id], models[memory_id] = revision, model
                    if revision == expected_revisions[memory_id] and model == target_model and not private and not quarantined:
                        indexed.add(memory_id)
            if indexed == set(ids) or time.monotonic() >= deadline:
                break
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return {
            "status": "indexed" if indexed == set(ids) else "not_ready", "indexed": sorted(indexed),
            "pending": sorted(set(ids) - indexed), "revisions": revisions, "embedding_models": models,
            "boundary": "read_only_patched_sqlite_expected_revision_and_model_confirmation",
        }


class ReceiptSurface:
    """Expose one fixture through SDK or authenticated /mcp without fallback."""
    def __init__(self, profile: Any, *, transport: str, backend: str, timeout: float,
                 managed_mesh: Any = None, readonly_store: ReadonlyStore | None = None):
        from mesh.client import MeshClient

        if transport not in ("sdk", "http") or backend not in ("hash", "neural"):
            raise ValueError("Invalid receipt benchmark transport or query mode.")
        if transport == "http" and managed_mesh is not None:
            raise ValueError("HTTP fixture clients cannot own a Mesh instance.")
        self.client = MeshClient(profile, transport=transport, timeout=timeout)
        self.transport, self.query_mode = transport, backend
        self.managed_mesh = managed_mesh
        self.store = readonly_store if readonly_store is not None else getattr(managed_mesh, "store", None)

    def write(self, items: list[dict[str, Any]], *, mode: str, operation_id: str) -> tuple[list[Any], list[str]]:
        if mode not in ("scalar", "atomic") or not _OPERATION_ID.fullmatch(operation_id):
            raise ValueError("Invalid fixture mutation identity.")
        name = "mesh_learn_many" if mode == "atomic" else "mesh_learn"
        arguments = ({"items": items, "atomic": True, "deduplicate": False}
                     if mode == "atomic" else {**items[0], "deduplicate": False})
        reply = self.client.call(name, arguments, operation_id=operation_id)
        receipt = reply.get("receipt") if isinstance(reply, dict) else None
        errors = []
        if (
            not isinstance(reply, dict) or reply.get("operation_id") != operation_id or reply.get("replayed") is not False
            or not isinstance(receipt, dict) or receipt.get("operation_id") != operation_id
        ):
            errors.append("InvalidFreshOperationReceiptEnvelope")
        results = reply.get("results") if mode == "atomic" and isinstance(reply, dict) else [reply]
        return results, errors

    def get(self, memory_id: str, *, include_private: bool = False) -> dict[str, Any] | None:
        reply = self.client.call("mesh_inspect", {"memory_id": memory_id, "include_private": include_private,
                                                 "include_quarantined": False})
        memories = reply.get("memories")
        if not isinstance(memories, list) or len(memories) > 1 or any(not isinstance(row, dict) for row in memories):
            raise ValueError("Exact inspection did not return zero or one memory.")
        return memories[0] if memories else None

    def recall(self, query: str, **options: Any) -> list[dict[str, Any]]:
        reply = self.client.call("mesh_recall", {"query": query, **options, "include_other_possible": False})
        rows = reply.get("results")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Public tool recall did not return its results list.")
        return rows

    def operation_receipt(self, operation_id: str) -> dict[str, Any]:
        reply = self.client.call("mesh_operation_get", {"operation_id": operation_id})
        receipt = reply.get("receipt")
        if reply.get("status") != "recorded" or not isinstance(receipt, dict) or receipt.get("operation_id") != operation_id:
            raise ValueError("The public operation lookup did not confirm the stable receipt.")
        return receipt

    def wait_for_indexed(self, ids: list[str], **options: Any) -> dict[str, Any]:
        target = self.managed_mesh if self.managed_mesh is not None else self.store
        if target is None:
            raise RuntimeError("Readiness verification requires an explicit native observer.")
        return target.wait_for_indexed(ids, **options)

    def group_stats(self, enabled: bool) -> dict[str, Any]:
        if self.transport != "http":
            raise ValueError("Group-commit execution is measured only on HTTP.")
        return checked_group_stats(authenticated_stats(self.client.profile, timeout=self.client.timeout), enabled)

    def close(self) -> None:
        if self.managed_mesh is not None:
            self.managed_mesh.close()
