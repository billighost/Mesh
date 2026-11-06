"""Allowlisted numerical projections; never copy whole benchmark reports."""
from __future__ import annotations

import math
import re
from typing import Any

HEX = re.compile(r"^[a-f0-9]{64}$")
SAFE_TAG = re.compile(r"^[A-Za-z0-9_.:@+#-]{1,160}$")
LATENCY = ("successful_samples", "p50_ms", "p95_ms", "p99_ms", "mean_ms")
WRITE_COUNTS = (
    "submitted_rows", "attempted_rows", "acknowledged_rows", "inserted_acknowledgements",
    "deduplicated_acknowledgements", "unique_inserted_ids", "failed_acknowledgements",
    "unobserved_rows", "unattempted_rows", "attempted_operation_ids",
    "acknowledged_operation_ids", "unknown_outcome_rows",
)
DISCREPANCIES = frozenset({
    "unacknowledged_database_id", "content_mismatch", "revision_or_visibility_mismatch",
    "source_actor_mismatch", "missing_acknowledged_id", "row_count_mismatch",
    "database_integrity", "exact_id_not_visible", "exact_id_lookup_failed",
})
ERROR_CODES = frozenset({
    "ValueError", "TypeError", "KeyError", "RuntimeError", "OSError", "TimeoutError",
    "Timeout", "MissingOrInvalidResult", "UnexpectedProcessExit", "UnknownMutationOutcome",
    "MissingAcknowledgedOperationIdentity", "ReceiptProducerScopeMismatch",
    "InvalidNativeReceipt", "MissingNativeAcknowledgedReceipt", "UnexpectedNativeReceipt",
    "NativeReceiptAcknowledgementMismatch", "UnconfirmedWriteAcknowledgement",
    "OperationAcknowledgementError", "ProfileError", "TransportUnavailable",
    "StorePolicyRejected", "GroupAdmissionRejected", "CommitOutcomeUnknown",
    "OperationFailed", "CloseFailed", "RuntimeSourceChanged", "RuntimeSourceUnverifiable",
    "HttpClientImportedModelRuntime", "SeedWorkerFailed", "WorkerExitedBeforeBarrier",
    "HttpServerExitedBeforeBarrier", "ReadinessBarrierTimedOut", "ServerShutdownFailed",
    "ResourceLimitExceeded",
})
STAGES = frozenset({
    "verify_model_copy", "verify_runtime_source", "import_runtime",
    "construct_http_client", "construct_mesh", "runtime_readiness",
    "authenticated_http_server_startup", "seed_anchors", "worker_startup_barrier",
    "collect_writes", "verify_actual_group_commit_execution", "verify_committed_ids_and_retrieval",
    "seed", "ingest", "verify", "serve", "telemetry", "shutdown",
    "wrapper_collection",
})
MODEL_TAGS = frozenset({"hash-384", "onnx-minilm-l6-v2", "needs-reembed", "unknown", "None"})
ENGINE_BACKENDS = frozenset({"onnx", "hash-fallback", "onnx-warming", "unloaded", "fastembed", "sentence-transformers"})
PHASES = frozenset({"startup", "ingestion", "verification", "shutdown"})
ROLES = frozenset({
    "coordinator", "write_clients", "http_server", "verification_clients", "other_descendants",
})
POLICY = {
    "include_private": False, "read_only": False, "enforce_agent_ownership": True,
    "allow_namespace_override": False,
}


class LedgerError(ValueError):
    """Content-free report contract failure."""


def number(value: Any) -> int | float | None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return value


def fields(value: Any, names: tuple[str, ...]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {name: number(source.get(name)) for name in names}


def list_count(value: Any) -> int | None:
    return len(value) if isinstance(value, list) else None


def latency(value: Any) -> dict[str, Any]:
    return fields(value, LATENCY)


def counters(value: Any, allowed: frozenset[str] = ERROR_CODES) -> dict[str, int]:
    result: dict[str, int] = {}
    if not isinstance(value, dict):
        return result
    for key, count in value.items():
        if type(count) is not int or count < 0:
            continue
        safe_key = key if key in allowed else "other"
        result[safe_key] = result.get(safe_key, 0) + count
    return result


def safe_tag(value: Any) -> str | None:
    if not isinstance(value, str) or not SAFE_TAG.fullmatch(value):
        return None
    if re.match(r"^[A-Za-z]:", value):
        return None
    return value


def model_tag(value: Any) -> str | None:
    return value if value in MODEL_TAGS else None


def native_metadata(values: list[Any]) -> dict[str, Any]:
    records = [item for item in values if isinstance(item, dict)]
    versions = sorted({item["sqlite_version"] for item in records
                       if isinstance(item.get("sqlite_version"), str)
                       and re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?", item["sqlite_version"])})
    schemas = sorted({int(str(item["schema_version"])) for item in records
                      if re.fullmatch(r"\d{1,3}", str(item.get("schema_version", "")))})
    return {
        "observed_workers": len(records), "sqlite_versions": versions, "schema_versions": schemas,
        "wal_full_verified": bool(records) and all(
            item.get("synchronous") == "FULL" and str(item.get("journal_mode", "")).lower() == "wal"
            for item in records
        ),
        "model_tags": sorted({model_tag(item.get("query_model_tag")) for item in records
                              if model_tag(item.get("query_model_tag")) is not None}),
        "engine_backends": sorted({item["backend"] for item in records if item.get("backend") in ENGINE_BACKENDS}),
    }


def source_manifest(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise LedgerError("MissingRelativeSourceManifest")
    result = {}
    for key, digest in value.items():
        if (
            not isinstance(key, str) or "\\" in key or ":" in key or key.startswith("/")
            or any(part in {"", ".", ".."} for part in key.split("/"))
            or not (
                key == "pyproject.toml"
                or key.startswith("mesh/") and key.endswith(".py")
                or key in {"benchmarks/production_bench.py", "benchmarks/production_transport.py"}
            )
            or not isinstance(digest, str) or not HEX.fullmatch(digest)
        ):
            raise LedgerError("UnsafeRelativeSourceManifest")
        result[key] = digest
    return result


def query_result(value: Any, anchor_labels: dict[str, str]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    status = source.get("status")
    result = {
        "status": status if status in {"completed", "failed", "unavailable"} else "unobserved",
        **fields(source, ("attempted_queries", "recall_at_1", "recall_at_5")),
        "latency": latency(source),
        "first_pass": latency(source.get("first_pass")),
        "repeated_passes": latency(source.get("repeated_passes")),
        "error_counts": counters(source.get("errors")),
        "top_1_misses_by_anchor": {},
    }
    misses = source.get("top_1_misses_by_anchor", {})
    if isinstance(misses, dict):
        for name, count in misses.items():
            if name in anchor_labels and type(count) is int and count >= 0:
                result["top_1_misses_by_anchor"][anchor_labels[name]] = count
    return result


def resource_result(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = {
        "sampling_available": source.get("sampling_available") is True,
        **fields(source, (
            "sample_interval_seconds", "sample_count", "sampled_peak_process_tree_rss_bytes",
            "sampled_process_tree_cpu_seconds", "sampled_peak_process_count",
        )),
        "phases": {}, "roles": {},
    }
    for kind, allowed, names in (
        ("phases", PHASES, ("peak_rss_bytes", "cpu_seconds", "samples")),
        ("roles", ROLES, ("peak_rss_bytes", "cpu_seconds")),
    ):
        values = source.get(kind, {})
        if isinstance(values, dict):
            result[kind] = {name: fields(item, names) for name, item in values.items() if name in allowed}
    return result


def model_manifest(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"model.onnx", "tokenizer.json"}:
        raise LedgerError("InvalidModelManifest")
    if any(not isinstance(item, str) or not HEX.fullmatch(item) for item in value.values()):
        raise LedgerError("InvalidModelDigest")
    return dict(value)


def result_projection(report: dict[str, Any], cell: dict[str, Any], plan: dict[str, Any],
                      execution: dict[str, Any]) -> dict[str, Any]:
    """Project known numerical fields; arbitrary bodies/messages never reach output."""
    if (
        report.get("report_version") != 2 or report.get("fixture_version") != 1
        or report.get("total_input_rows") != cell["input_facts"]
        or report.get("anchor_rows") != 12
        or report.get("fixture_sha256") != plan["fixture_hashes"][str(cell["input_facts"])]
        or report.get("python_version") != plan["runtime"]["python_version"]
        or report.get("coverage_instrumented") is not False
    ):
        raise LedgerError("ReportHeaderMismatch")
    if source_manifest(report.get("runtime_source_manifest")) != plan["runtime_sources"]:
        raise LedgerError("RuntimeManifestMismatch")
    if source_manifest(report.get("benchmark_source_manifest")) != plan["harness_sources"]:
        raise LedgerError("HarnessManifestMismatch")
    cases = report.get("cases")
    if not isinstance(cases, list) or len(cases) != 1 or not isinstance(cases[0], dict):
        raise LedgerError("MultipleOrMissingCase")
    case = cases[0]
    for key, target in (
        ("agents", cell["agents"]), ("backend", cell["backend"]), ("mode", cell["mode"]),
        ("surface", cell["surface"]), ("group_commit_requested", cell["group_commit"]),
    ):
        if case.get(key) != target:
            raise LedgerError("CaseDimensionsMismatch")
    status = case.get("status")
    if status not in {"completed", "failed", "unavailable"}:
        raise LedgerError("CaseStatusMismatch")
    writes = case.get("writes", {})
    verified = case.get("readiness_and_retrieval", {})
    writes = writes if isinstance(writes, dict) else {}
    verified = verified if isinstance(verified, dict) else {}
    dense = verified.get("dense_readiness", {})
    dense = dense if isinstance(dense, dict) else {}
    receipts = verified.get("operation_receipts", {})
    receipts = receipts if isinstance(receipts, dict) else {}
    exact = verified.get("exact_lookup", {})
    lexical = verified.get("lexical_anchor_readiness", {})
    lexical = lexical if isinstance(lexical, dict) else {}
    expected_rows = cell["input_facts"] + 12
    resources = resource_result(case.get("resources"))
    published_models = model_manifest(case.get("model_manifest"))
    models_match = (
        published_models == plan["model"]["hashes"] if cell["backend"] == "neural"
        else published_models is None
    )
    metadata = case.get("worker_metadata", [])
    metadata = metadata if isinstance(metadata, list) else []
    native = native_metadata(metadata)
    thin_http = (
        bool(metadata) and all(
            isinstance(item, dict) and item.get("client_loads_memory_or_model") is False for item in metadata
        ) if cell["surface"] == "http" else None
    )
    group = case.get("group_commit_execution", {})
    group = group if isinstance(group, dict) else {}
    group_verified = (
        group.get("execution_verified") is True and group.get("enabled") is bool(cell["group_commit"])
        if cell["surface"] == "http" else True
    )
    policy_matches = case.get("profile_policy") == POLICY
    neural_ready = (
        dense.get("status") == "indexed"
        and dense.get("expected_rows") == expected_rows
        and dense.get("verified_expected_revisions") == expected_rows
        and model_tag(dense.get("target_model")) is not None
        and dense.get("target_model") != "hash-384"
        and dense.get("embedding_models_after_wait") == {dense.get("target_model"): expected_rows}
    ) if cell["backend"] == "neural" else None
    qualified = (
        status == "completed" and execution.get("exit_code") == 0
        and execution.get("source_unchanged_after") is True
        and execution.get("all_owned_processes_stopped") is True
        and execution.get("resource_budget_exceeded") is False
        and writes.get("accounting_consistent") is True
        and writes.get("attempt_accounting_complete") is True
        and writes.get("acknowledged_rows") == cell["input_facts"]
        and writes.get("unknown_outcome_rows") == 0
        and verified.get("integrity_ok") is True
        and verified.get("verified_rows") == expected_rows
        and verified.get("exact_ids_visible") == expected_rows
        and lexical.get("status") == "ready"
        and receipts.get("status") == "confirmed"
        and resources["sampling_available"] and bool(resources["sample_count"])
        and group_verified and policy_matches and models_match
        and native["wal_full_verified"] and native["observed_workers"] == cell["agents"]
        and (thin_http is True if cell["surface"] == "http" else True)
        and (neural_ready is True if cell["backend"] == "neural" else True)
    )
    retrieval = verified.get("retrieval", {})
    retrieval = retrieval if isinstance(retrieval, dict) else {}
    dense_models = dense.get("embedding_models_after_wait", {})
    dense_models = dense_models if isinstance(dense_models, dict) else {}
    error_types: dict[str, int] = {}
    error_stages: dict[str, int] = {}
    worker_failures = case.get("worker_failures", [])
    worker_failures = worker_failures if isinstance(worker_failures, list) else []
    for item in [case, *worker_failures, verified]:
        if not isinstance(item, dict):
            continue
        for name in ("error_type", "worker_failure", "close_error_type", "source_verification_error_type"):
            code = item.get(name)
            if isinstance(code, str):
                error_types[code] = error_types.get(code, 0) + 1
        stage = item.get("error_stage")
        if isinstance(stage, str):
            error_stages[stage] = error_stages.get(stage, 0) + 1
    result = {
        "schema_version": 1,
        "cell": {name: cell[name] for name in (
            "id", "phase", "repetition", "backend", "mode", "surface", "group_commit",
            "agents", "input_facts", "anchors", "batch_size", "query_repetitions",
        )},
        "harness_status": status, "headline_eligible": bool(qualified),
        "collection_kind": ("interrupted_dispatch_recovery" if report.get("collection_kind")
                            == "interrupted_dispatch_recovery" else "harness"),
        "input_report_sha256": execution["input_report_sha256"],
        "checks": {
            "source_unchanged_after": execution.get("source_unchanged_after") is True,
            "all_owned_processes_stopped": execution.get("all_owned_processes_stopped") is True,
            "resource_budget_exceeded": execution.get("resource_budget_exceeded") is True,
            "accounting_consistent": writes.get("accounting_consistent") is True,
            "attempt_accounting_complete": writes.get("attempt_accounting_complete") is True,
            "accounting_observed": type(writes.get("accounting_consistent")) is bool,
            "verification_observed": type(verified.get("verified_rows")) is int,
            "policy_observed": "profile_policy" in case,
            "integrity_ok": verified.get("integrity_ok") is True,
            "policy_matches_registered": policy_matches,
            "model_bytes_match_registered": models_match,
            "model_manifest_observed": "model_manifest" in case,
            "thin_http_clients_verified": thin_http,
            "group_execution_verified": group_verified,
            "neural_expected_revisions_verified": neural_ready,
            "wal_full_verified": native["wal_full_verified"],
        },
        "native_runtime": native,
        "writes": {
            **fields(writes, WRITE_COUNTS),
            "protocol_error_count": len(writes.get("protocol_errors", [])),
            "unacknowledged_committed_row_count": list_count(verified.get("unacknowledged_committed_rows")),
            "unacknowledged_receipt_count": list_count(receipts.get("unacknowledged_receipt_ids")),
            "verified_committed_input_rows": number(verified.get("verified_committed_input_rows")),
        },
        "timing": {
            **fields(case, (
                "startup_seconds", "durable_acceptance_wall_seconds", "write_wall_seconds",
                "scenario_total_wall_seconds", "acknowledged_rows_per_second",
                "verified_committed_input_rows_per_second", "write_workers_process_cpu_seconds",
                "ingest_workers_process_cpu_seconds",
            )),
            "write_call_latency": latency(case.get("write_call_latency")),
        },
        "verification": {
            "verified_rows": number(verified.get("verified_rows")),
            "exact_ids_visible": number(verified.get("exact_ids_visible")),
            "discrepancy_counts": counters(verified.get("discrepancies"), DISCREPANCIES),
            "exact_lookup": latency(exact),
            "lexical_anchor_ready": lexical.get("status") == "ready",
            "receipt_status": receipts.get("status") if receipts.get("status") in {"confirmed", "failed", "unavailable"} else "unobserved",
            "receipt_counts": fields(receipts, (
                "expected_operation_ids", "native_receipts", "publicly_confirmed_receipts",
            )),
            "receipt_error_counts": counters(receipts.get("errors")),
            "receipt_lookup_latency": latency(receipts.get("public_receipt_lookup")),
            "dense": {
                "status": dense.get("status") if dense.get("status") in {"indexed", "not_ready", "unavailable"} else "unobserved",
                "target_model_tag": model_tag(dense.get("target_model")),
                **fields(dense, (
                    "verified_expected_revisions", "expected_rows", "wait_wall_seconds", "wait_budget_seconds",
                    "verified_since_write_barrier_seconds", "fully_indexed_input_rows_per_second",
                )),
                "model_counts": {model_tag(key): count for key, count in dense_models.items()
                                 if model_tag(key) is not None and type(count) is int and count >= 0},
            },
            "retrieval": {name: query_result(retrieval.get(name), plan["anchor_labels"]) for name in (
                "keyword_queries", "lexical_hash_hybrid", "neural_hybrid",
            )},
        },
        "group": {
            **fields(group, (
                "ingest_committed_batches", "ingest_executed_commands",
                "expected_successful_ingest_commands", "commands_per_commit",
            )),
            "multiple_commands_per_commit_observed": group.get("multiple_commands_per_commit_observed") is True,
        },
        "resources": resources,
        "failures": {"worker_failure_count": len(worker_failures),
                     "error_type_counts": counters(error_types), "error_stage_counts": counters(error_stages, STAGES)},
        "footprint": fields(case.get("footprint"), (
            "database_bytes", "wal_bytes", "model_bytes", "other_bytes", "total_bytes",
        )),
        "execution": {
            "exit_code": execution.get("exit_code"),
            "wrapper_wall_seconds": number(execution.get("wrapper_wall_seconds")),
            "wrapper_observed_peak_tree_rss_bytes": number(execution.get("observed_peak_tree_rss_bytes")),
            "wrapper_observed_min_available_ram_bytes": number(execution.get("min_available_ram_bytes")),
            "paging_diagnostic_available": execution.get("paging_diagnostic_available") is True,
            "paging_sample_count": number(execution.get("paging_sample_count")),
            "sampled_peak_pages_output_per_second": number(execution.get("sampled_peak_pages_output_per_second")),
            "sustained_paging_observed": execution.get("sustained_paging_observed") is True,
        },
    }
    assert_safe_output(result)
    return result


def assert_safe_output(value: Any) -> None:
    """Defend the public projection against accidental future arbitrary-string copies."""
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise LedgerError("NonFinitePublicNumber")
        return
    if isinstance(value, str):
        if (
            "\\" in value or "/" in value or re.match(r"^[A-Za-z]:", value)
            or any(char.isspace() for char in value) or len(value) > 256
        ):
            raise LedgerError("UnsafePublicString")
        return
    if isinstance(value, list):
        for item in value:
            assert_safe_output(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LedgerError("NonStringPublicKey")
            assert_safe_output(item)
        return
    raise LedgerError("UnexpectedPublicType")
