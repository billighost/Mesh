"""Transport-independent operation names and retry identity rules."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
from typing import Any

MUTATING_TOOLS = frozenset(
    {
        "mesh_learn",
        "mesh_learn_many",
        "mesh_update",
        "mesh_approve",
        "mesh_forget",
        "mesh_resolve_contradiction",
        "mesh_sync",
        "mesh_task_create",
        "mesh_task_update",
        "mesh_task_complete",
    }
)
TOOL_NAMES = MUTATING_TOOLS | {
    "mesh_recall",
    "mesh_inspect",
    "mesh_context",
    "mesh_patterns",
    "mesh_contradictions",
    "mesh_project_brief",
    "mesh_task_list",
    "mesh_task_get",
    "mesh_log",
    "mesh_operation_get",
}
_OP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
OPERATION_RECEIPT_VERSION = 1


class UnknownOutcome(RuntimeError):
    """A dispatched mutation did not produce a confirmed acknowledgement."""

    def __init__(self, operation_id: str | None):
        self.operation_id = operation_id
        super().__init__(
            "The mutation outcome is unknown. Reconcile or retry the same operation ID against the same profile."
        )


def receipt_capability_proof(token: str, nonce: str, store_id: str, version: int = OPERATION_RECEIPT_VERSION) -> str:
    message = f"{nonce}|{store_id}|operation-receipts:{version}".encode("utf-8")
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def supports_operation_receipts(identity: Any, token: str, nonce: str, store_id: str) -> bool:
    if not isinstance(identity, dict) or identity.get("store_id") != store_id:
        return False
    version = identity.get("operation_receipt_version")
    proof = identity.get("receipt_hmac")
    return (
        type(version) is int and version == OPERATION_RECEIPT_VERSION
        and isinstance(proof, str) and bool(re.fullmatch(r"[a-f0-9]{64}", proof))
        and hmac.compare_digest(proof, receipt_capability_proof(token, nonce, store_id, version))
    )


def _receipt_matches(receipt: Any, actual: Any, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(receipt, dict):
        if not isinstance(actual, dict):
            return False
        identity_fields = {"status", "memory_id", "item_id", "id", "short_id", "revision", "incarnation", "store_generation"}
        if not (identity_fields & actual.keys()) <= receipt.keys():
            return False
        return all(key in actual and _receipt_matches(value, actual[key], depth + 1) for key, value in receipt.items())
    if isinstance(receipt, list):
        return isinstance(actual, list) and len(receipt) == len(actual) and all(
            _receipt_matches(value, other, depth + 1) for value, other in zip(receipt, actual)
        )
    return (
        (not isinstance(receipt, float) or math.isfinite(receipt))
        and type(receipt) is type(actual) and receipt == actual
    )


def validate_mutation_acknowledgement(
    result: Any, operation_id: str, namespace: str, *, operation: str, producer_scope: str,
) -> dict[str, Any]:
    """Accept confirmed receipts or explicit terminal rejections, never bare success."""
    validate_operation_id(operation_id)
    if not isinstance(result, dict):
        raise UnknownOutcome(operation_id)
    status = result.get("status")
    if not isinstance(status, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", status):
        raise UnknownOutcome(operation_id)
    if result.get("operation_id", operation_id) != operation_id or result.get("namespace", namespace) != namespace:
        raise UnknownOutcome(operation_id)
    receipt = result.get("receipt")
    if receipt is None and status in {"error", "rejected", "busy", "unavailable"}:
        return {**result, "operation_id": operation_id, "namespace": namespace}
    if (
        result.get("operation_id") != operation_id or result.get("namespace") != namespace
        or type(result.get("replayed")) is not bool or not isinstance(receipt, dict)
        or receipt.get("operation_id") != operation_id
        or not isinstance(receipt.get("created_at"), str) or not 1 <= len(receipt["created_at"]) <= 64
        or not isinstance(receipt.get("result"), (dict, list))
        or not receipt["result"]
        or receipt.get("operation", operation) != operation
        or receipt.get("namespace", namespace) != namespace
        or receipt.get("producer_scope", producer_scope) != producer_scope
    ):
        raise UnknownOutcome(operation_id)
    actual = result.get("result") if isinstance(receipt["result"], list) else result
    if not _receipt_matches(receipt["result"], actual):
        raise UnknownOutcome(operation_id)
    return result


def validate_operation_id(value: str) -> str:
    if not isinstance(value, str) or not _OP_ID.fullmatch(value):
        raise ValueError("Invalid operation ID.")
    return value


def parameter_digest(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def producer_scope() -> str:
    # Shared-token agent labels do not establish separate authenticated principals.
    principal = (os.environ.get("MESH_AUTHENTICATED_AGENT_ID") or "").strip()
    return validate_producer_scope("principal:" + principal if principal else "local-user")


def validate_producer_scope(value: str) -> str:
    if value == "local-user":
        return value
    if not isinstance(value, str) or not value.startswith("principal:"):
        raise ValueError("Producer scope must be local-user or a reviewed authenticated principal.")
    principal = value[len("principal:"):]
    if not principal or len(principal) > 64 or principal != principal.strip() or any(ord(c) < 32 or ord(c) == 127 for c in principal):
        raise ValueError("Invalid authenticated producer scope.")
    return value
