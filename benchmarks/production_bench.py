"""Offline process benchmarks for authenticated writes, retrieval readiness and recovery.

Example: python benchmarks/production_bench.py --agents 1 4 8 16 --memories 512 --output production.json
Neural runs require an explicit directory containing verified model.onnx/tokenizer.json.
Timings describe this fixture and interface; they are not competitor or power-loss claims.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import platform
import re
import socket
import stat
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts.run_tests import isolated_environment
from benchmarks.production_transport import (
    SURFACES,
    ReadonlyStore,
    ReceiptSurface,
    group_execution,
    thin_client_metadata,
)

NAMESPACE = "production-bench"
FIXTURE_VERSION = 1
_ANCHORS = (
    ("cedar", "The Cedar billing relay allows 850 milliseconds for its downstream payment response.",
     "Cedar billing relay payment response timeout", "How long may Cedar wait for the payment service?"),
    ("kestrel", "The Kestrel export pipeline retries a failed delivery after 40 seconds.",
     "Kestrel export retry delay", "When does Kestrel try sending a failed export again?"),
    ("crane", "The Crane inventory service retains warehouse reconciliation records for 45 days.",
     "Crane warehouse reconciliation retention", "How long does Crane keep inventory reconciliation history?"),
    ("maple", "Maple database migrations run with the command python tools/apply_schema.py.",
     "Maple migration apply_schema.py command", "How do developers apply a Maple database schema change?"),
    ("otter", "The Otter queue sends failed invoices to its invoice-quarantine destination.",
     "Otter failed invoice queue destination", "Where does Otter put invoices it cannot deliver?"),
    ("badger", "Badger deployment health requires both the status endpoint and a successful write probe.",
     "Badger deployment health write probe", "What must pass before a Badger deployment is healthy?"),
    ("finch", "Finch report downloads are limited to 24 megabytes per request.",
     "Finch report download size limit", "What is the largest report a Finch request can download?"),
    ("larch", "The Larch scheduler uses UTC for all daily settlement cutoffs.",
     "Larch daily settlement cutoff timezone", "Which time zone determines Larch settlement deadlines?"),
    ("puma", "Puma support escalation belongs to the on-call database maintainer.",
     "Puma support escalation owner", "Who should receive an escalated Puma support incident?"),
    ("heron", "Heron event replay starts at the last confirmed checkpoint rather than the latest timestamp.",
     "Heron event replay checkpoint", "Where should Heron resume processing after an interruption?"),
    ("aspen", "Aspen keeps integration fixtures in tests/fixtures/integration.",
     "Aspen integration fixtures directory", "Where are the reusable Aspen integration test examples?"),
    ("marten", "Marten release approval requires the migration rollback rehearsal to succeed.",
     "Marten release approval rollback rehearsal", "Which recovery check must pass before Marten ships?"),
)
_TEMPLATES = (
    "Release record {i} tracks public warehouse adapter item {i}; its reconciliation window is {v} days.",
    "Catalog record {i} documents report fragment {i}; its approved transfer ceiling is {v} megabytes.",
    "Delivery record {i} records export segment {i}; its retry interval is {v} seconds.",
    "Maintenance record {i} covers scheduler partition {i}; its settlement checkpoint is sequence {v}.",
    "Integration record {i} describes public fixture package {i}; its validation sample contains {v} rows.",
    "Deployment record {i} documents application shard {i}; its health observation window is {v} minutes.",
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_REPORT_BYTES = 64 * 1024 * 1024
_MAX_JOURNAL_BYTES = 128 * 1024 * 1024


def stable_operation_id(config: dict[str, Any], indices: list[int]) -> str:
    """Keep retry identity deterministic for a fixture, actor and call grouping."""
    request = {"fixture_version": FIXTURE_VERSION, "namespace": NAMESPACE,
               "action": config["action"], "actor": config["actor"],
               "mode": config["mode"], "indices": indices}
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return "bench-" + digest


def source_manifest(repo: Path) -> dict[str, str]:
    """Bind each worker to the same local runtime without printing personal paths."""
    files = sorted((repo / "mesh").rglob("*.py"))
    if not files:
        raise ValueError("No Mesh Python sources were found in the selected checkout.")
    result = {}
    for path in [*files, repo / "pyproject.toml"]:
        _reject_links(path)
        with path.open("rb") as handle:
            raw = handle.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Runtime source exceeds the source verification byte limit.")
        result[path.relative_to(repo).as_posix()] = hashlib.sha256(raw).hexdigest()
    return result


def benchmark_source_manifest() -> dict[str, str]:
    return {"benchmarks/" + name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("production_bench.py", "production_transport.py")}


def _sources_unchanged(config: dict[str, Any]) -> bool:
    return (source_manifest(Path(config["repo"])) == config["source_manifest"]
            and ("benchmark_source_manifest" not in config
                 or benchmark_source_manifest() == config["benchmark_source_manifest"]))


def _reject_links(path: Path) -> None:
    """Include Windows reparse points on Python versions without is_junction."""
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


def _read_json(path: Path, maximum: int = _MAX_REPORT_BYTES) -> dict[str, Any]:
    _reject_links(path)
    with path.open("rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Expected a regular benchmark JSON file.")
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("Benchmark JSON exceeds its byte limit.")
    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Benchmark JSON contains a nonfinite number.")
        return parsed
    def reject_constant(value: str) -> None:
        raise ValueError("Benchmark JSON contains a nonfinite constant.")
    value = json.loads(raw, parse_float=finite_float, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a benchmark JSON object.")
    return value


def write_json(path: Path, value: Any) -> None:
    """Publish only complete result files, even if a worker dies."""
    _reject_links(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def latency_summary(samples: list[float]) -> dict[str, Any]:
    values = sorted(samples)
    def percentile(fraction: float) -> float | None:
        if not values:
            return None
        position = (len(values) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 4)
    return {
        "successful_samples": len(values), "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95), "p99_ms": percentile(0.99),
        "mean_ms": round(statistics.mean(values), 4) if values else None,
    }


def fixture_record(index: int) -> dict[str, Any]:
    if index < 0:
        anchor = _ANCHORS[-index - 1]
        content = anchor[1]
        tag = "benchmark-anchor-" + anchor[0]
    else:
        content = _TEMPLATES[index % len(_TEMPLATES)].format(i=index, v=11 + index * 7)
        tag = "benchmark-record-" + str(index)
    return {
        "content": content, "memory_type": "fact", "confidence": 1.0,
        "tags": ["production-benchmark", tag], "local_only": False, "trust_tier": "agent",
    }


def fixture_digest(count: int) -> str:
    payload = [fixture_record(index) for index in range(-len(_ANCHORS), count)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def acknowledgement(result: Any, index: int) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("error") or result.get("status") not in {
        "stored", "stored_with_conflict",
    }:
        raise ValueError("Unconfirmed write acknowledgement.")
    memory_id = result.get("memory_id")
    revision = result.get("revision")
    if not isinstance(memory_id, str) or not _ID_RE.fullmatch(memory_id):
        raise ValueError("Write acknowledgement has no valid ID.")
    if type(revision) is not int or revision < 1:
        raise ValueError("Write acknowledgement has no committed revision.")
    deduplicated = result.get("deduplicated")
    if type(deduplicated) is not bool:
        raise ValueError("Invalid acknowledgement disposition.")
    if "disposition" in result and result["disposition"] != ("deduplicated" if deduplicated else "inserted"):
        raise ValueError("Write acknowledgement has contradictory dispositions.")
    return {
        "fixture_index": index, "memory_id": memory_id, "revision": revision,
        "deduplicated": deduplicated, "indexing_state": result.get("indexing_state"),
    }


def write_accounting(reports: list[dict[str, Any]], submitted: int) -> dict[str, Any]:
    """Keep attempted rows, acknowledged rows, and inserted IDs distinct."""
    entries = [entry for report in reports for entry in report.get("acknowledgements", [])]
    failures = [failure for report in reports for failure in report.get("failures", [])]
    attempted = sum(report.get("attempted", 0) for report in reports)
    inserted = [entry for entry in entries if not entry["deduplicated"]]
    unique_ids = {entry["memory_id"] for entry in inserted}
    unique_indices = {entry["fixture_index"] for entry in entries}
    failure_indices = {entry["fixture_index"] for entry in failures}
    complete = all(report.get("attempt_accounting_complete", True) for report in reports)
    protocol_errors = [error for report in reports for error in report.get("protocol_errors", [])]
    operation_ids = {row["operation_id"] for row in [*entries, *failures] if row.get("operation_id")}
    acknowledged_operations = {row["operation_id"] for row in entries if row.get("operation_id")}
    consistent = (
        attempted <= submitted and len(entries) + len(failures) == attempted
        and len(unique_ids) == len(inserted) and len(unique_indices) == len(entries)
        and len(failure_indices) == len(failures) and not (failure_indices & unique_indices)
        and complete and not protocol_errors
    )
    return {
        "submitted_rows": submitted, "attempted_rows": attempted,
        "acknowledged_rows": len(entries), "inserted_acknowledgements": len(inserted),
        "deduplicated_acknowledgements": len(entries) - len(inserted),
        "unique_inserted_ids": len(unique_ids), "failed_acknowledgements": len(failures),
        "unobserved_rows": max(0, submitted - attempted),
        "unattempted_rows": max(0, submitted - attempted) if complete else None,
        "attempt_accounting_complete": complete, "accounting_consistent": consistent,
        "attempted_operation_ids": len(operation_ids), "acknowledged_operation_ids": len(acknowledged_operations),
        "unknown_outcome_rows": sum(bool(entry["outcome_may_be_committed"]) for entry in failures),
        "protocol_errors": protocol_errors,
        "failures": failures, "acknowledgements": entries,
    }


def _empty_write_report() -> dict[str, Any]:
    return {
        "attempted": 0, "acknowledgements": [], "failures": [], "latencies_ms": [],
        "protocol_errors": [], "attempt_accounting_complete": True,
    }


def _row_failure(index: int, error: str, *, committed: bool = True,
                 latency: float | None = None) -> dict[str, Any]:
    return {
        "fixture_index": index, "error_type": error,
        "outcome_may_be_committed": committed, "call_latency_ms": latency,
    }


def _journal_event(handle: Any, event: dict[str, Any]) -> None:
    handle.write(json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n")
    # Two flushed markers per call expose a killed worker's last known
    # attempt without adding a competing fsync to every scalar storage write.
    handle.flush()


def recover_progress(config: dict[str, Any]) -> dict[str, Any]:
    """Recover known acknowledgements and unresolved attempts after process death."""
    report = _empty_write_report()
    path = Path(config["journal"])
    if not path.exists():
        return report
    _reject_links(path)
    assigned = set(config.get("indices", []))
    pending: dict[int, list[int]] = {}
    operation_ids: dict[int, str | None] = {}
    seen: set[int] = set()
    consumed = 0
    try:
        with path.open("rb") as handle:
            while raw := handle.readline(1024 * 1024 + 1):
                consumed += len(raw)
                if consumed > _MAX_JOURNAL_BYTES or len(raw) > 1024 * 1024 or not raw.endswith(b"\n"):
                    raise ValueError("Incomplete or oversized progress journal.")
                event = json.loads(raw)
                if not isinstance(event, dict) or type(event.get("sequence")) is not int:
                    raise ValueError("Invalid progress journal event.")
                sequence = event["sequence"]
                if event.get("kind") == "attempt":
                    indices = event.get("indices")
                    if (
                        sequence != len(pending) or not isinstance(indices, list) or not indices
                        or any(type(index) is not int or index not in assigned or index in seen for index in indices)
                        or len(indices) != len(set(indices))
                    ):
                        raise ValueError("Invalid progress attempt.")
                    pending[sequence] = indices
                    operation_id = event.get("operation_id")
                    if config.get("surface", "bare-sdk") != "bare-sdk":
                        if operation_id != stable_operation_id(config, indices):
                            raise ValueError("Progress attempt has a different stable operation identity.")
                    elif operation_id is not None:
                        raise ValueError("Bare SDK progress cannot claim a durable operation identity.")
                    operation_ids[sequence] = operation_id
                    seen.update(indices)
                    report["attempted"] += len(indices)
                elif event.get("kind") == "outcome":
                    selected = pending.get(sequence)
                    entries, failures = event.get("acknowledgements"), event.get("failures")
                    if not selected or not isinstance(entries, list) or not isinstance(failures, list):
                        raise ValueError("Invalid progress outcome.")
                    for entry in entries:
                        _validate_entry(entry)
                    for failure in failures:
                        _validate_failure(failure)
                    if any(row.get("operation_id") != operation_ids[sequence] for row in [*entries, *failures]):
                        raise ValueError("Progress outcome has a different operation identity.")
                    observed = [row["fixture_index"] for row in [*entries, *failures]]
                    if len(observed) != len(selected) or set(observed) != set(selected):
                        raise ValueError("Progress outcome does not cover its attempt.")
                    protocol_errors = event.get("protocol_errors", [])
                    if not isinstance(protocol_errors, list) or any(not isinstance(value, str) for value in protocol_errors):
                        raise ValueError("Invalid progress protocol errors.")
                    latency = event.get("latency_ms")
                    if latency is not None:
                        if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
                            raise ValueError("Invalid progress latency.")
                    report["acknowledgements"].extend(entries)
                    report["failures"].extend(failures)
                    report["protocol_errors"].extend(protocol_errors)
                    if latency is not None:
                        report["latencies_ms"].append(latency)
                    pending[sequence] = []
                elif event.get("kind") == "complete":
                    completed = event.get("ingest_completed_ns")
                    cpu = event.get("ingest_process_cpu_seconds")
                    if (
                        sequence != len(pending) or seen != assigned or any(pending.values())
                        or type(completed) is not int or completed <= 0
                        or type(cpu) not in (int, float) or not math.isfinite(cpu) or cpu < 0
                        or "ingest_completed_ns" in report
                    ):
                        raise ValueError("Invalid progress completion marker.")
                    report["ingest_completed_ns"] = completed
                    report["ingest_process_cpu_seconds"] = cpu
                else:
                    raise ValueError("Unknown progress journal event.")
    except (ValueError, OSError, TypeError, KeyError, RecursionError):
        report["attempt_accounting_complete"] = False
        report["protocol_errors"].append("IncompleteProgressJournal")
    for sequence, indices in pending.items():
        for index in indices:
            failure = _row_failure(index, "WorkerTerminatedBeforeAcknowledgement")
            if operation_ids.get(sequence) is not None:
                failure["operation_id"] = operation_ids[sequence]
            report["failures"].append(failure)
    report["recovered_from_progress_journal"] = True
    return report


def _validate_entry(entry: Any) -> None:
    if (
        not isinstance(entry, dict) or type(entry.get("fixture_index")) is not int
        or not isinstance(entry.get("memory_id"), str) or not _ID_RE.fullmatch(entry["memory_id"])
        or type(entry.get("revision")) is not int or entry["revision"] < 1
        or type(entry.get("deduplicated")) is not bool
    ):
        raise ValueError("Invalid reported write acknowledgement.")
    _validate_row_identity(entry)


def _validate_failure(failure: Any) -> None:
    if (
        not isinstance(failure, dict) or type(failure.get("fixture_index")) is not int
        or not isinstance(failure.get("error_type"), str)
        or type(failure.get("outcome_may_be_committed")) is not bool
    ):
        raise ValueError("Invalid reported write failure.")
    _validate_row_identity(failure)


def _validate_row_identity(row: dict[str, Any]) -> None:
    if "operation_id" in row and (not isinstance(row["operation_id"], str) or not _ID_RE.fullmatch(row["operation_id"])):
        raise ValueError("Invalid reported stable operation identity.")
    if "agent_id" in row and (not isinstance(row["agent_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", row["agent_id"])):
        raise ValueError("Invalid reported operation actor.")
    if "tool_name" in row and row["tool_name"] not in {"mesh_learn", "mesh_learn_many"}:
        raise ValueError("Invalid reported mutation tool.")


def _validate_write_report(report: dict[str, Any], config: dict[str, Any]) -> None:
    if type(report.get("attempted")) is not int or not 0 <= report["attempted"] <= len(config["indices"]):
        raise ValueError("Invalid reported attempt count.")
    assigned = set(config["indices"])
    identities: dict[int, str] = {}
    if config.get("surface", "bare-sdk") != "bare-sdk":
        size = config["batch_size"] if config["mode"] == "atomic" else 1
        for offset in range(0, len(config["indices"]), size):
            selected = config["indices"][offset:offset + size]
            identities.update(dict.fromkeys(selected, stable_operation_id(config, selected)))
    for key, validator in (("acknowledgements", _validate_entry), ("failures", _validate_failure)):
        rows = report.get(key)
        if not isinstance(rows, list):
            raise ValueError("Invalid reported row collection.")
        for row in rows:
            validator(row)
            if row["fixture_index"] not in assigned:
                raise ValueError("Reported row was not assigned to this worker.")
            if identities and row.get("operation_id") != identities[row["fixture_index"]]:
                raise ValueError("Reported row has a different stable operation identity.")
    for key in ("latencies_ms", "protocol_errors"):
        if not isinstance(report.get(key), list):
            raise ValueError("Invalid reported measurement collection.")
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in report["latencies_ms"]):
        raise ValueError("Invalid reported latency sample.")
    if any(not isinstance(value, str) for value in report["protocol_errors"]):
        raise ValueError("Invalid reported protocol error.")
    if type(report.get("attempt_accounting_complete")) is not bool:
        raise ValueError("Invalid reported attempt completeness.")


class ProcessTreeSampler:
    """Sample summed RSS and CPU for the coordinator and only its descendants."""
    def __init__(self) -> None:
        try:
            import psutil
        except ImportError:
            self.library = None
            self.process = None
        else:
            self.library = psutil
            self.process = psutil.Process()
        self.stop_event = threading.Event()
        self.started_epoch = time.time()
        self.cpu: dict[tuple[int, float], float] = {}
        self.baselines: dict[tuple[int, float], float] = {}
        self.lock = threading.Lock()
        self.phase = "startup"
        self.roles: dict[int, str] = {os.getpid(): "coordinator"}
        self.phase_stats: dict[str, dict[str, Any]] = {}
        self.role_stats: dict[str, dict[str, Any]] = {}
        self.peak_rss = self.peak_processes = self.samples = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _sample(self) -> None:
        if self.process is None:
            return
        try:
            processes = [self.process, *self.process.children(recursive=True)]
        except self.library.Error:
            return
        rss = observed = 0
        observations = []
        for process in processes:
            try:
                with process.oneshot():
                    created = process.create_time()
                    key = (process.pid, created)
                    cpu = process.cpu_times()
                    total = cpu.user + cpu.system
                    observations.append((key, total, process.memory_info().rss))
            except self.library.Error:
                continue
        with self.lock:
            phase = self.phase_stats.setdefault(self.phase, {"peak_rss_bytes": 0, "cpu_seconds": 0.0, "samples": 0})
            role_rss: Counter[str] = Counter()
            for key, total, memory in observations:
                baseline = self.baselines.setdefault(key, total if key[1] < self.started_epoch else 0.0)
                previous = self.cpu.get(key, 0.0)
                self.cpu[key] = max(previous, total - baseline)
                delta = self.cpu[key] - previous
                role = self.roles.get(key[0], "other_descendants")
                role_stats = self.role_stats.setdefault(role, {"peak_rss_bytes": 0, "cpu_seconds": 0.0})
                role_stats["cpu_seconds"] += delta
                phase["cpu_seconds"] += delta
                rss += memory
                role_rss[role] += memory
                observed += 1
            for role, memory in role_rss.items():
                self.role_stats[role]["peak_rss_bytes"] = max(self.role_stats[role]["peak_rss_bytes"], memory)
            phase["peak_rss_bytes"] = max(phase["peak_rss_bytes"], rss)
            phase["samples"] += 1
            self.peak_rss = max(self.peak_rss, rss)
            self.peak_processes = max(self.peak_processes, observed)
            self.samples += 1

    def register(self, pid: int, role: str) -> None:
        with self.lock:
            self.roles[pid] = role

    def mark_phase(self, phase: str) -> None:
        self._sample()
        with self.lock:
            self.phase = phase

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self._sample()
            self.stop_event.wait(0.1)

    def start(self) -> None:
        self._sample()
        self.thread.start()

    def stop(self) -> dict[str, Any]:
        self._sample()
        self.stop_event.set()
        self.thread.join(timeout=2)
        return {
            "sampling_available": self.process is not None, "sample_interval_seconds": 0.1,
            "sample_count": self.samples,
            "sampled_peak_process_tree_rss_bytes": self.peak_rss if self.samples else None,
            "sampled_process_tree_cpu_seconds": round(sum(self.cpu.values()), 4) if self.samples else None,
            "sampled_peak_process_count": self.peak_processes,
            "phases": self.phase_stats if self.samples else {},
            "roles": self.role_stats if self.samples else {},
            "boundary": "entire_scenario_coordinator_and_descendants",
            "notes": "Summed RSS can count shared pages repeatedly; sampling can miss short-lived children and phase boundaries.",
        }


def worker_environment(root: Path, home: Path, backend: str, repo: Path,
                       group_commit: int = 0) -> dict[str, str]:
    # Benchmarks must not inherit the operator's Mesh policy, encryption key,
    # daemon endpoint or provider configuration. The explicit fixture is public.
    inherited = {key: value for key, value in os.environ.items() if not key.upper().startswith("MESH_")}
    environment = isolated_environment(home, inherited)
    environment.update({
        "MESH_HOME": str(root / "mesh"), "MESH_DIR": str(root / "mesh"),
        "MESH_NAMESPACE": NAMESPACE, "MESH_LOCKED_NAMESPACE": NAMESPACE,
        "MESH_LOCK_NAMESPACE": NAMESPACE, "MESH_NO_DAEMON_PROXY": "1",
        "MESH_ENABLE_AUTO_SYNC": "0", "MESH_MCP_INCLUDE_PRIVATE": "0",
        "MESH_READ_ONLY": "0", "MESH_ENFORCE_AGENT_OWNERSHIP": "1",
        "MESH_FORCE_HASH_EMBED": "1" if backend == "hash" else "0",
        "MESH_REQUIRE_MODEL": "0" if backend == "hash" else "1",
        "MESH_QUERY_MODE": backend, "MESH_FORCE_FASTEMBED": "0",
        "MESH_FORCE_PYTORCH": "0", "MESH_VAULT_SECRETS": "0", "MESH_NON_INTERACTIVE": "1",
        "MESH_QUIET": "1", "MESH_SYNC_ONNX": "0", "MESH_BG_ONNX": "1",
        "MESH_GROUP_COMMIT": str(group_commit),
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "MESH_AGENT_SCOPES": json.dumps({"*": {"namespaces": [NAMESPACE], "mode": "rw"}}),
    })
    environment.pop("MESH_AUTHENTICATED_AGENT_ID", None)
    environment.pop("MESH_UNIX_SOCKET", None)
    environment["PYTHONPATH"] = str(repo) + (
        os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
    )
    return environment


def _metadata(mesh: Any, backend: str) -> dict[str, Any]:
    from mesh import __version__
    from mesh.memory import _embed_query_with_tag, get_engine_status
    from mesh.sqlite_runtime import is_patched_sqlite
    with mesh.store._connect() as connection:
        version = str(connection.execute("SELECT sqlite_version()").fetchone()[0])
        synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    if synchronous != 2 or journal.lower() != "wal" or not is_patched_sqlite(version):
        raise RuntimeError("Benchmark requires the patched FULL SQLite WAL writer connection.")
    _, tag = _embed_query_with_tag("benchmark model readiness")
    engine = get_engine_status()
    if backend == "neural" and (tag == "hash-384" or not engine.get("model_loaded")):
        raise RuntimeError("Verified neural backend was not ready.")
    return {
        "mesh_version": __version__,
        "sqlite_version": version, "synchronous": "FULL", "journal_mode": journal,
        "schema_version": mesh.store.get_schema_version(),
        "query_model_tag": tag, "model_loaded": bool(engine.get("model_loaded")),
        "backend": engine.get("backend"), "embedding_degraded": bool(engine.get("embedding_degraded")),
    }


def _validate_config(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    if not isinstance(data.get("root"), str) or not Path(data["root"]).is_absolute():
        raise ValueError("Worker root must be an absolute temporary path.")
    unresolved = Path(data["root"])
    _reject_links(unresolved)
    root = unresolved.resolve()
    if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("mesh-production-bench-"):
        raise ValueError("Worker root must be a coordinator-created temporary benchmark directory.")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Worker configuration is outside the benchmark directory.")
    marker = _read_json(root / "benchmark-only.json", 4096)
    if not isinstance(data.get("token"), str) or not re.fullmatch(r"[a-f0-9]{32}", data["token"]):
        raise ValueError("Invalid benchmark marker token.")
    if marker != {"token": data["token"], "fixture_version": FIXTURE_VERSION}:
        raise ValueError("Worker benchmark marker did not match.")
    if data["action"] not in {"seed", "ingest", "verify", "serve", "telemetry",
                             "crash-before", "crash-after", "verify-crash"}:
        raise ValueError("Unsupported worker action.")
    if data["backend"] not in {"hash", "neural"}:
        raise ValueError("Unsupported benchmark backend.")
    data.setdefault("surface", "bare-sdk")
    data.setdefault("group_commit", 0)
    if (
        data["surface"] not in SURFACES or type(data["group_commit"]) is not int or data["group_commit"] not in (0, 1)
        or data["surface"] != "http" and data["group_commit"] != 0
        or data["action"] in {"serve", "telemetry"} and data["surface"] != "http"
        or data["action"] in {"crash-before", "crash-after", "verify-crash"} and data["surface"] != "bare-sdk"
    ):
        raise ValueError("Invalid benchmark surface or group-commit binding.")
    number = re.fullmatch(r"config-(\d+)\.json", path.name)
    if number is None or path.resolve().parent != root:
        raise ValueError("Unexpected worker configuration location.")
    for key, name in {
        "result": f"result-{number[1]}.json", "ready": f"ready-{number[1]}.json",
        "journal": f"journal-{number[1]}.jsonl", "go": "go.json",
    }.items():
        target = Path(data[key])
        _reject_links(target)
        if not target.is_absolute() or target != root / name:
            raise ValueError("Worker outputs must stay inside their new temporary benchmark root.")
    if "stop" in data or data["action"] == "serve":
        target = Path(data.get("stop", ""))
        _reject_links(target)
        if not target.is_absolute() or target != root / f"stop-{number[1]}.json":
            raise ValueError("Server stop marker must stay inside its temporary benchmark root.")
    if (
        data.get("mode") not in {"scalar", "atomic"}
        or type(data.get("batch_size")) is not int or not 1 <= data["batch_size"] <= 128
        or type(data.get("timeout")) not in (float, int)
        or not math.isfinite(data["timeout"]) or not 0 < data["timeout"] <= 3600
        or not isinstance(data.get("actor"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", data["actor"])
    ):
        raise ValueError("Invalid worker operation bounds or actor.")
    if data["action"] in {"seed", "ingest"} and (
        not isinstance(data.get("indices"), list)
        or len(data["indices"]) > 100000
        or any(type(index) is not int or not -len(_ANCHORS) <= index < 100000 for index in data["indices"])
        or len(data["indices"]) != len(set(data["indices"]))
    ):
        raise ValueError("Invalid worker fixture indices.")
    return data


def _ingest(mesh: Any, config: dict[str, Any]) -> dict[str, Any]:
    indices = config["indices"]
    report = _empty_write_report()
    cpu_started = time.process_time()
    size = config["batch_size"] if config["mode"] == "atomic" else 1
    with Path(config["journal"]).open("x", encoding="utf-8") as journal:
        for sequence, start in enumerate(range(0, len(indices), size)):
            selected = indices[start:start + size]
            operation_id = (stable_operation_id(config, selected)
                            if config.get("surface", "bare-sdk") != "bare-sdk" else None)
            _journal_event(journal, {"kind": "attempt", "sequence": sequence, "indices": selected,
                                     "operation_id": operation_id})
            report["attempted"] += len(selected)
            before = time.perf_counter_ns()
            entries, failures, protocol_errors = [], [], []
            try:
                items = [fixture_record(index) for index in selected]
                if operation_id is not None:
                    results, envelope_errors = mesh.write(items, mode=config["mode"], operation_id=operation_id)
                    protocol_errors.extend(envelope_errors)
                elif config["mode"] == "atomic":
                    results = mesh.learn_many(items, atomic=True, deduplicate=False)
                else:
                    results = [mesh.learn(**items[0], deduplicate=False)]
                if not isinstance(results, list):
                    results = []
                    protocol_errors.append("UnexpectedBatchReplyType")
                if len(results) != len(selected):
                    protocol_errors.append("UnexpectedBatchReplyLength")
                for position, index in enumerate(selected):
                    raw = results[position] if position < len(results) else None
                    try:
                        entries.append(acknowledgement(raw, index))
                    except ValueError:
                        rejected = isinstance(raw, dict) and raw.get("status") == "rejected"
                        failures.append(_row_failure(index, "RejectedWrite" if rejected else "InvalidWriteAcknowledgement",
                                                     committed=not rejected))
            except Exception as exc:
                # An exception can follow a commit. Record the stable identity
                # when available; no benchmark surface automatically replays.
                may_be_committed = True
                if operation_id is not None:
                    from mesh.client import ProfileError, TransportUnavailable
                    may_be_committed = not isinstance(exc, (ProfileError, TransportUnavailable))
                failures.extend(_row_failure(index, type(exc).__name__, committed=may_be_committed) for index in selected)
            elapsed = (time.perf_counter_ns() - before) / 1e6
            for row in [*entries, *failures]:
                if operation_id is not None:
                    row["operation_id"] = operation_id
                    row["tool_name"] = "mesh_learn_many" if config["mode"] == "atomic" else "mesh_learn"
                if config.get("actor") is not None:
                    row["agent_id"] = config["actor"]
            for failure in failures:
                failure["call_latency_ms"] = round(elapsed, 4)
            latency = elapsed if not failures and not protocol_errors else None
            _journal_event(journal, {
                "kind": "outcome", "sequence": sequence, "acknowledgements": entries,
                "failures": failures, "protocol_errors": protocol_errors, "latency_ms": latency,
            })
            report["acknowledgements"].extend(entries)
            report["failures"].extend(failures)
            report["protocol_errors"].extend(protocol_errors)
            if latency is not None:
                report["latencies_ms"].append(latency)
        report["ingest_completed_ns"] = time.perf_counter_ns()
        report["ingest_process_cpu_seconds"] = round(time.process_time() - cpu_started, 6)
        _journal_event(journal, {
            "kind": "complete", "sequence": math.ceil(len(indices) / size),
            "ingest_completed_ns": report["ingest_completed_ns"],
            "ingest_process_cpu_seconds": report["ingest_process_cpu_seconds"],
        })
    return report


def _query_suite(mesh: Any, config: dict[str, Any], targets: dict[int, str], mode: str,
                 position: int) -> dict[str, Any]:
    surface = config.get("surface", "bare-sdk")
    if getattr(mesh, "query_mode", mode) != mode:
        return {"status": "unavailable", "reason": "This transport's query mode is fixed by its reviewed runtime profile.",
                "actual_query_encoding_mode": mesh.query_mode}
    previous = os.environ.get("MESH_QUERY_MODE")
    os.environ["MESH_QUERY_MODE"] = mode
    latencies: list[float] = []
    first_pass: list[float] = []
    repeated_passes: list[float] = []
    failures: Counter[str] = Counter()
    misses: Counter[str] = Counter()
    at_one = at_five = attempted = 0
    try:
        for repetition in range(config["query_repetitions"]):
            for number, anchor in enumerate(_ANCHORS):
                attempted += 1
                before = time.perf_counter_ns()
                try:
                    results = mesh.recall(anchor[position], n=5, include_private=False, include_quarantined=False)
                    if not isinstance(results, list) or any(not isinstance(row, dict) for row in results):
                        raise ValueError("Invalid public recall result.")
                    ids = [row.get("id", row.get("memory_id")) for row in results]
                    target = targets[-number - 1]
                    at_one += bool(ids and ids[0] == target)
                    at_five += target in ids[:5]
                    if not ids or ids[0] != target:
                        misses[anchor[0]] += 1
                    latency = (time.perf_counter_ns() - before) / 1e6
                    latencies.append(latency)
                    (first_pass if repetition == 0 else repeated_passes).append(latency)
                except Exception as exc:
                    failures[type(exc).__name__] += 1
    finally:
        if previous is None:
            os.environ.pop("MESH_QUERY_MODE", None)
        else:
            os.environ["MESH_QUERY_MODE"] = previous
    return {
        "status": "completed" if not failures else "failed", "attempted_queries": attempted,
        "query_encoding_mode": mode, "recall_at_1": at_one / attempted if attempted else None,
        "recall_at_5": at_five / attempted if attempted else None,
        "errors": dict(failures), "top_1_misses_by_anchor": dict(misses), **latency_summary(latencies),
        "first_pass": latency_summary(first_pass), "repeated_passes": latency_summary(repeated_passes),
        "boundary": ("guarded_sdk_hybrid_recall_including_query_encoding_and_default_telemetry" if surface == "bare-sdk"
                     else "receipt_sdk_public_tool_recall_with_context_and_default_telemetry" if surface == "receipt-sdk"
                     else "authenticated_identity_and_http_mcp_recall_with_context_and_default_telemetry"),
        "fixture_scope": "twelve_handwritten_anchors_with_entity_names_and_varied_topic_distractors",
    }


def _dense_readiness(mesh: Any, config: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    if config["backend"] != "neural":
        return {"status": "unavailable", "reason": "No verified local neural model was configured."}
    target_model = config["metadata"]["query_model_tag"]
    started = time.perf_counter()
    deadline = time.monotonic() + min(60.0, config["timeout"])
    indexed: set[str] = set()
    outcomes: Counter[str] = Counter()
    for offset in range(0, len(entries), 128):
        chunk = entries[offset:offset + 128]
        expected = {entry["memory_id"]: entry["revision"] for entry in chunk}
        remaining = max(0.0, min(60.0, deadline - time.monotonic()))
        result = mesh.wait_for_indexed(list(expected), timeout=remaining, expected_revisions=expected,
                                       include_private=False, target_model=target_model)
        outcomes[str(result.get("status"))] += 1
        for memory_id in result.get("indexed", []):
            if (result.get("revisions", {}).get(memory_id) == expected.get(memory_id)
                    and result.get("embedding_models", {}).get(memory_id) == target_model):
                indexed.add(memory_id)
        if result.get("status") != "indexed":
            break
    elapsed = time.perf_counter() - started
    expected_ids = {entry["memory_id"] for entry in entries}
    after_barrier = (time.perf_counter_ns() - config["write_started_ns"]) / 1e9
    with mesh.store._connect() as connection:
        models = dict(Counter(str(row[0]) for row in connection.execute(
            "SELECT embedding_model FROM memories WHERE namespace=?", (NAMESPACE,),
        )))
    ready = indexed == expected_ids and bool(expected_ids) and set(models) == {target_model}
    return {
        "status": "indexed" if ready else "not_ready", "target_model": target_model,
        "verified_expected_revisions": len(indexed), "expected_rows": len(expected_ids),
        "wait_wall_seconds": round(elapsed, 6), "wait_budget_seconds": min(60.0, config["timeout"]),
        "verified_since_write_barrier_seconds": round(after_barrier, 6),
        "fully_indexed_input_rows_per_second": config["submitted"] / after_barrier if ready else None,
        "batch_outcomes": dict(outcomes), "embedding_models_after_wait": models,
        "boundary": "write_barrier_to_fresh_process_confirmation_including_close_startup_exact_and_native_fts_checks",
    }


def _receipt_rows(result: Any) -> list[tuple[str, int, bool]]:
    if not isinstance(result, dict):
        raise ValueError("Invalid content-free receipt result.")
    rows = result.get("results", [result])
    if not isinstance(rows, list) or not rows:
        raise ValueError("Invalid content-free receipt rows.")
    verified = [acknowledgement(row, number) for number, row in enumerate(rows)]
    return [(row["memory_id"], row["revision"], row["deduplicated"]) for row in verified]


def _verify_receipts(mesh: Any, config: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    if config.get("surface", "bare-sdk") == "bare-sdk":
        return {"status": "unavailable", "reason": "Bare guarded SDK writes have no operation receipts."}
    expected: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        operation_id = entry.get("operation_id")
        if not isinstance(operation_id, str) or not _ID_RE.fullmatch(operation_id):
            return {"status": "failed", "errors": {"MissingAcknowledgedOperationIdentity": 1}}
        expected.setdefault(operation_id, []).append(entry)
    failures: Counter[str] = Counter()
    native: dict[str, dict[str, Any]] = {}
    with mesh.store._connect() as connection:
        for operation_id, scope, operation, digest, result_json in connection.execute(
            "SELECT operation_id,producer_scope,operation,parameter_digest,result_json "
            "FROM operation_receipts WHERE namespace=?", (NAMESPACE,),
        ):
            if scope != "local-user":
                failures["ReceiptProducerScopeMismatch"] += 1
                continue
            try:
                if not isinstance(result_json, str) or len(result_json.encode()) > 1024 * 1024:
                    raise ValueError("Invalid bounded receipt JSON.")
                if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("Invalid receipt request digest.")
                native[operation_id] = {"operation": operation, "rows": _receipt_rows(json.loads(result_json))}
            except (TypeError, ValueError):
                failures["InvalidNativeReceipt"] += 1
    failures["MissingNativeAcknowledgedReceipt"] += len(set(expected) - set(native))
    unacknowledged = sorted(set(native) - set(expected))
    unknown_operations = {row.get("operation_id") for row in config.get("failed_rows", [])}
    failures["UnexpectedNativeReceipt"] += len(set(unacknowledged) - unknown_operations)
    latencies: list[float] = []
    confirmed = 0
    for operation_id, rows in expected.items():
        expected_rows = sorted((row["memory_id"], row["revision"], row["deduplicated"]) for row in rows)
        expected_tools = {row.get("tool_name") for row in rows}
        recorded = native.get(operation_id)
        if recorded is None or set(expected_tools) != {recorded["operation"]} or sorted(recorded["rows"]) != expected_rows:
            failures["NativeReceiptAcknowledgementMismatch"] += 1
        before = time.perf_counter_ns()
        try:
            receipt = mesh.operation_receipt(operation_id)
            if receipt.get("operation") not in expected_tools or sorted(_receipt_rows(receipt.get("result"))) != expected_rows:
                raise ValueError("Public operation receipt differs from the acknowledgement.")
            confirmed += 1
            latencies.append((time.perf_counter_ns() - before) / 1e6)
        except Exception as exc:
            failures[type(exc).__name__] += 1
    return {"status": "confirmed" if not any(failures.values()) else "failed", "errors": dict(failures),
            "expected_operation_ids": len(expected), "native_receipts": len(native),
            "publicly_confirmed_receipts": confirmed, "unacknowledged_receipt_ids": unacknowledged,
            "public_receipt_lookup": latency_summary(latencies), "producer_scope": "local-user"}


def _lexical_index_readiness(mesh: Any, targets: dict[int, str]) -> dict[str, Any]:
    missing = []
    with mesh.store._connect() as connection:
        for number, anchor in enumerate(_ANCHORS):
            ids = {row[0] for row in connection.execute(
                "SELECT memories_fts.id FROM memories_fts JOIN memories m ON m.id=memories_fts.id "
                "WHERE memories_fts MATCH ? AND m.namespace=? AND m.local_only=0 AND m.quarantined=0",
                (anchor[0], NAMESPACE),
            )}
            if targets.get(-number - 1) not in ids:
                missing.append(anchor[0])
    return {"status": "ready" if not missing else "incomplete", "scope": "twelve_anchor_rows",
            "missing_anchor_ids": missing, "boundary": "native_read_only_public_fts_visibility"}


def _verify(mesh: Any, config: dict[str, Any]) -> dict[str, Any]:
    entries = config["acknowledgements"]
    all_entries = config["seed_acknowledgements"] + entries
    expected = {entry["memory_id"]: entry for entry in all_entries}
    discrepancies: Counter[str] = Counter()
    models: Counter[str] = Counter()
    actual: set[str] = set()
    expected_content = {fixture_record(index)["content"]: index
                        for index in range(-len(_ANCHORS), config["submitted"])}
    committed_inputs: list[dict[str, Any]] = []
    unacknowledged: list[dict[str, Any]] = []
    with mesh.store._connect() as connection:
        for row in connection.execute(
            "SELECT id,content,revision,embedding_model,local_only,quarantined,source_agent FROM memories WHERE namespace=?",
            (NAMESPACE,),
        ):
            memory_id, content, revision, model, private, quarantined, actor = row
            actual.add(memory_id)
            models[str(model)] += 1
            index = expected_content.get(content)
            if index is not None and index >= 0:
                committed_inputs.append({"fixture_index": index, "memory_id": memory_id, "revision": revision})
            record = expected.get(memory_id)
            if record is None:
                discrepancies["unacknowledged_database_id"] += 1
                unacknowledged.append({"fixture_index": index, "memory_id": memory_id, "revision": revision})
                continue
            if content != fixture_record(record["fixture_index"])["content"]:
                discrepancies["content_mismatch"] += 1
            if revision != record["revision"] or private or quarantined:
                discrepancies["revision_or_visibility_mismatch"] += 1
            if record.get("agent_id") is not None and actor != record["agent_id"]:
                discrepancies["source_actor_mismatch"] += 1
        quick_check = [row[0] for row in connection.execute("PRAGMA quick_check")]
        foreign_key_errors = len(list(connection.execute("PRAGMA foreign_key_check")))
    discrepancies["missing_acknowledged_id"] += len(set(expected) - actual)
    if len(actual) != config["submitted"] + len(config["seed_acknowledgements"]):
        discrepancies["row_count_mismatch"] += 1
    if quick_check != ["ok"] or foreign_key_errors:
        discrepancies["database_integrity"] += 1

    exact_latencies: list[float] = []
    exact_failures: Counter[str] = Counter()
    exact_visible = 0
    for entry in all_entries:
        before = time.perf_counter_ns()
        try:
            result = mesh.get(entry["memory_id"], include_private=False)
            if (
                isinstance(result, dict) and result.get("id") == entry["memory_id"]
                and result.get("content") == fixture_record(entry["fixture_index"])["content"]
                and result.get("revision") == entry["revision"]
                and not result.get("local_only") and not result.get("quarantined")
            ):
                exact_visible += 1
                exact_latencies.append((time.perf_counter_ns() - before) / 1e6)
            else:
                discrepancies["exact_id_not_visible"] += 1
        except Exception as exc:
            exact_failures[type(exc).__name__] += 1
            discrepancies["exact_id_lookup_failed"] += 1
    targets = {entry["fixture_index"]: entry["memory_id"] for entry in config["seed_acknowledgements"]}
    lexical_index = _lexical_index_readiness(mesh, targets)
    dense = _dense_readiness(mesh, config, all_entries)
    keyword_mode = getattr(mesh, "query_mode", "hash")
    keyword = _query_suite(mesh, config, targets, keyword_mode, 2)
    lexical = (keyword if keyword_mode == "hash" else
               {"status": "unavailable", "reason": "The runtime profile uses neural query encoding for HTTP/SDK tool parity."})
    neural = (_query_suite(mesh, config, targets, "neural", 3) if dense["status"] == "indexed" else
              {"status": "unavailable", "reason": "Expected revisions were not verified in the neural model space."})
    receipts = _verify_receipts(mesh, config, all_entries)
    surface = config.get("surface", "bare-sdk")
    return {
        "integrity_ok": not any(discrepancies.values()), "discrepancies": dict(discrepancies),
        "verified_rows": len(actual), "exact_ids_visible": exact_visible,
        "verified_committed_input_rows": len(committed_inputs), "committed_input_ids": committed_inputs,
        "unacknowledged_committed_rows": unacknowledged,
        "embedding_models_before_wait": dict(models),
        "exact_lookup": {**latency_summary(exact_latencies), "errors": dict(exact_failures),
                         "boundary": "guarded_sdk_get_by_exact_id" if surface == "bare-sdk" else
                         "reviewed_sdk_inspect_by_exact_id" if surface == "receipt-sdk" else
                         "authenticated_identity_and_http_mcp_inspect_by_exact_id", "verified_visible_rows": exact_visible},
        "lexical_anchor_readiness": lexical_index,
        "keyword_anchor_search": {"status": "ready" if keyword["recall_at_5"] == 1.0 else "incomplete",
                                  "query_encoding_mode": keyword_mode},
        "operation_receipts": receipts,
        "dense_readiness": dense, "retrieval": {"keyword_queries": keyword, "lexical_hash_hybrid": lexical,
                                                "neural_hybrid": neural},
        "quick_check": quick_check, "foreign_key_errors": foreign_key_errors,
    }


def _crash(mesh: Any, config: dict[str, Any]) -> None:
    from mesh.client import ConnectionProfile, MeshClient
    root = Path(config["root"])
    profile = ConnectionProfile(
        NAMESPACE, root / "mesh", agent_id=config["actor"], producer_scope="local-user",
    )
    client = MeshClient(profile, transport="sdk")
    operation_id = config["action"]
    arguments = {"content": fixture_record(-1)["content"], "tags": ["production-crash-probe"]}
    staged: dict[str, Any] = {"operation_id": operation_id, "metadata": config["runtime_metadata"]}

    def die_after_commit() -> None:
        staged["commit_boundary"] = "after_outer_commit_before_post_commit_callbacks"
        write_json(Path(config["result"]), staged)
        os._exit(72)

    with mesh.store.write_transaction():
        if config["action"] == "crash-after":
            # The real commit runs unchanged. Register the fault first so audit
            # delivery cannot race the assertion that its outbox was durable.
            mesh.store.after_commit(die_after_commit)
        result = client.call("mesh_learn", arguments, operation_id)
        staged["memory_id"] = result["memory_id"]
        staged["revision"] = result["revision"]
        if config["action"] == "crash-before":
            staged["commit_boundary"] = "inside_outer_transaction_before_commit"
            write_json(Path(config["result"]), staged)
            os._exit(71)


def _durable_audit_counts(mesh_home: Path) -> tuple[int, int]:
    from mesh.sqlite_runtime import sqlite3

    path = mesh_home / f"audit_{NAMESPACE}.db"
    _reject_links(path)
    if not path.is_file():
        return 0, 0
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'").fetchone():
            return 0, 0
        counts = connection.execute(
            "SELECT count(*),count(DISTINCT event_id) FROM audit_events WHERE event_id IS NOT NULL"
        ).fetchone()
    finally:
        connection.close()
    return int(counts[0]), int(counts[1])


def _verify_crash(mesh: Any, config: dict[str, Any]) -> dict[str, Any]:
    from mesh.audit import deliver_mutation_events
    from mesh.client import ConnectionProfile, MeshClient
    root = Path(config["root"])
    staged = config["staged"]
    result = mesh.get(staged["memory_id"])
    client = MeshClient(ConnectionProfile(
        NAMESPACE, root / "mesh", agent_id=config["actor"], producer_scope="local-user",
    ), transport="sdk")
    receipt = client.call("mesh_operation_get", {"operation_id": staged["operation_id"]})
    after = config["after_commit"]
    with mesh.store._connect() as connection:
        counts = {}
        for table in ("memories", "operation_receipts", "mutation_outbox"):
            counts[table] = int(connection.execute("SELECT count(*) FROM " + table).fetchone()[0])
        quick_check = [row[0] for row in connection.execute("PRAGMA quick_check")]
        foreign_key_errors = len(list(connection.execute("PRAGMA foreign_key_check")))
    audit_before = _durable_audit_counts(root / "mesh")
    # Recovery can deliver an event between these independent database reads.
    # Either durable location proves acceptance; their sum is not a snapshot.
    durable_event_visible = counts["mutation_outbox"] > 0 or audit_before[0] > 0
    good = (
        bool(result) == after and (receipt.get("status") == "recorded") == after
        and counts["memories"] == int(after) and counts["operation_receipts"] == int(after)
        and durable_event_visible == after and quick_check == ["ok"] and not foreign_key_errors
    )
    if after and result and result.get("content") != fixture_record(-1)["content"]:
        good = False
    if after:
        recorded = receipt.get("receipt", {}).get("result", {})
        good = good and recorded.get("memory_id") == staged["memory_id"] and recorded.get("revision") == staged["revision"]
    arguments = {"content": fixture_record(-1)["content"], "tags": ["production-crash-probe"]}
    replay = client.call("mesh_learn", arguments, staged["operation_id"])
    with mesh.store._connect() as connection:
        replay_counts = {table: int(connection.execute("SELECT count(*) FROM " + table).fetchone()[0])
                         for table in ("memories", "operation_receipts")}
    replay_ok = (
        replay.get("replayed") is after and replay_counts == {"memories": 1, "operation_receipts": 1}
        and (not after or replay.get("memory_id") == staged["memory_id"])
    )
    delivered = deliver_mutation_events(mesh.store, limit=32)
    delivered_again = deliver_mutation_events(mesh.store, limit=32)
    with mesh.store._connect() as connection:
        remaining_events = int(connection.execute("SELECT count(*) FROM mutation_outbox").fetchone()[0])
    durable_audit = _durable_audit_counts(root / "mesh")
    audit_ok = remaining_events == 0 and durable_audit[0] == durable_audit[1] == 1 and delivered_again == 0
    return {"integrity_ok": bool(good and replay_ok and audit_ok), "counts_before_replay": counts,
            "audit_before_replay": {"durable_audit_events": audit_before[0],
                                     "unique_durable_audit_event_ids": audit_before[1]},
            "receipt_status_before_replay": receipt.get("status"), "memory_visible_before_replay": bool(result),
            "counts_after_replay": replay_counts, "replay_confirmed": replay_ok,
            "quick_check": quick_check, "foreign_key_errors": foreign_key_errors,
            "audit_delivery_after_restart": {
                "integrity_ok": audit_ok, "delivered_by_probe": delivered,
                "remaining_outbox_events": remaining_events, "durable_audit_events": durable_audit[0],
                "unique_durable_audit_event_ids": durable_audit[1], "second_delivery_count": delivered_again,
            }}


def _receipt_surface(config: dict[str, Any], mesh: Any = None) -> ReceiptSurface:
    from mesh.client import ConnectionPolicy, ConnectionProfile

    http = config["surface"] == "http"
    mesh_home = Path(config["root"]) / "mesh"
    profile = ConnectionProfile(
        namespace=NAMESPACE, mesh_home=mesh_home, agent_id=config["actor"], producer_scope="local-user",
        policy=ConnectionPolicy(include_private=False, enforce_agent_ownership=True),
        endpoint=config.get("endpoint") if http else None,
        token_file=mesh_home / "auth_token" if http else None,
    )
    if http and profile.endpoint is None:
        raise ValueError("HTTP benchmark clients require the coordinator's existing server endpoint.")
    readonly = ReadonlyStore(mesh_home, NAMESPACE) if http and config["action"] == "verify" else None
    return ReceiptSurface(profile, transport="http" if http else "sdk", backend=config["backend"],
                          timeout=min(120.0, config["timeout"]), managed_mesh=mesh, readonly_store=readonly)


def _close_cached_runtime(http: Any = None) -> None:
    """Close explicit cached instances only after the benchmark server stops."""
    if http is not None:
        http.shutdown_http_runtime()
        return
    from mesh import operations
    from mesh.execution import close_executor

    close_executor(wait=True)
    with operations._MESH_LOCK:
        meshes = list(operations._MESHES.values())
        operations._MESHES.clear()
    seen: set[int] = set()
    for mesh in meshes:
        if id(mesh) not in seen:
            mesh.close()
            seen.add(id(mesh))


def _serve(config: dict[str, Any]) -> int:
    """Run an actual authenticated server without the interactive CLI startup."""
    report: dict[str, Any] = {}
    metadata = None
    http = server = listener = watcher = None
    finished = threading.Event()
    watcher_errors: list[str] = []
    stage = "verify_runtime_source"
    started = time.perf_counter()
    try:
        repo = Path(config["repo"])
        if not _sources_unchanged(config):
            raise RuntimeError("Selected Mesh runtime changed before the server started.")
        sys.path.insert(0, str(repo))
        stage = "import_http_runtime"
        import uvicorn
        from mesh import http_server as http
        from mesh.daemon import get_or_create_auth_token

        stage = "server_runtime_readiness"
        get_or_create_auth_token()
        mesh = http.get_mesh(namespace=NAMESPACE, agent_id=config["actor"], create=True)
        metadata = _metadata(mesh, config["backend"])
        http._DAEMON_READY_EVENT.set()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        endpoint = "http://127.0.0.1:" + str(listener.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(http.app, log_level="warning", access_log=False,
                                              lifespan="on", timeout_graceful_shutdown=5))
        stage = "server_startup"
        def monitor() -> None:
            published = False
            deadline = time.monotonic() + config["timeout"]
            try:
                while not finished.wait(0.01):
                    if server.started and not published:
                        write_json(Path(config["ready"]), {"endpoint": endpoint, "metadata": metadata,
                                                            "group_commit_requested": config["group_commit"]})
                        published = True
                    if Path(config["stop"]).is_file():
                        if _read_json(Path(config["stop"]), 4096) != {"stop": True}:
                            raise ValueError("Invalid benchmark server stop marker.")
                        server.should_exit = True
                        report["stop_marker_observed"] = True
                        return
                    if not published and time.monotonic() >= deadline:
                        raise TimeoutError("Benchmark HTTP server did not become ready.")
            except Exception as exc:
                watcher_errors.append(type(exc).__name__)
                server.should_exit = True
        watcher = threading.Thread(target=monitor, name="benchmark-http-monitor", daemon=True)
        watcher.start()
        server.run(sockets=[listener])
        if not server.started or watcher_errors or not report.get("stop_marker_observed"):
            raise RuntimeError("Benchmark HTTP server readiness or monitoring failed.")
        report["graceful_shutdown"] = True
    except Exception as exc:
        report.update(worker_failure="ServerFailed", error_type=type(exc).__name__, error_stage=stage)
    finally:
        finished.set()
        if watcher is not None:
            watcher.join(timeout=2)
        if listener is not None:
            listener.close()
        if http is not None:
            http._DAEMON_READY_EVENT.clear()
            try:
                _close_cached_runtime(http)
            except Exception as exc:
                report.setdefault("worker_failure", "ServerCloseFailed")
                report["close_error_type"] = type(exc).__name__
        try:
            if not _sources_unchanged(config):
                report.setdefault("worker_failure", "RuntimeSourceChanged")
        except Exception as exc:
            report.setdefault("worker_failure", "RuntimeSourceUnverifiable")
            report["source_verification_error_type"] = type(exc).__name__
        report.update(metadata=metadata, process_cpu_seconds=round(time.process_time(), 6),
                      server_lifecycle_wall_seconds=round(time.perf_counter() - started, 6))
        if watcher_errors:
            report["monitor_error_types"] = watcher_errors
        write_json(Path(config["result"]), report)
    return int(bool(report.get("worker_failure")))


def worker(config_path: Path) -> int:
    config = _validate_config(config_path)
    if config["action"] == "serve":
        return _serve(config)
    mesh = None
    metadata = None
    report = _empty_write_report()
    stage = "verify_runtime_source"
    try:
        repo = Path(config["repo"])
        if not _sources_unchanged(config):
            raise RuntimeError("Selected Mesh runtime changed before the worker started.")
        stage = "import_runtime"
        sys.path.insert(0, str(repo))
        if config["surface"] == "http":
            stage = "construct_http_client"
            mesh = _receipt_surface(config)
            stage = "runtime_readiness"
            from mesh.sqlite_runtime import is_patched_sqlite
            metadata = config.get("server_metadata")
            if (
                not isinstance(metadata, dict) or metadata.get("synchronous") != "FULL"
                or str(metadata.get("journal_mode", "")).lower() != "wal"
                or not is_patched_sqlite(metadata.get("sqlite_version", ""))
                or not isinstance(metadata.get("query_model_tag"), str)
                or config["backend"] == "hash" and metadata["query_model_tag"] != "hash-384"
                or config["backend"] == "neural" and (
                    metadata["query_model_tag"] == "hash-384" or metadata.get("model_loaded") is not True)
            ):
                raise RuntimeError("HTTP server did not verify the required writer and model runtime.")
            group = mesh.group_stats(bool(config["group_commit"]))
            metadata = {**metadata, **thin_client_metadata(), "group_commit": group,
                        "runtime_location": "authenticated_server"}
        else:
            from mesh.memory import Mesh
            stage = "construct_mesh"
            if config["surface"] == "receipt-sdk":
                from mesh.operations import _get_mesh
                managed = _get_mesh(namespace=NAMESPACE, agent_id=config["actor"], create=config["action"] != "verify")
                mesh = _receipt_surface(config, managed)
                metadata_target = managed
            else:
                mesh = Mesh(namespace=NAMESPACE, agent_id=config["actor"],
                            create=config["action"] not in {"verify", "verify-crash"}, trusted_local=False)
                metadata_target = mesh
            stage = "runtime_readiness"
            metadata = _metadata(metadata_target, config["backend"])
        config["runtime_metadata"] = metadata
        stage = config["action"]
        if config["action"].startswith("crash-"):
            _crash(mesh, config)
            raise RuntimeError("Crash probe did not exit.")
        if config["action"] == "telemetry":
            report = {"group_commit": metadata["group_commit"]}
        elif config["action"] == "verify-crash":
            report = _verify_crash(mesh, config)
        elif config["action"] == "verify":
            report = _verify(mesh, config)
        else:
            if config["action"] == "ingest":
                write_json(Path(config["ready"]), metadata)
                deadline = time.monotonic() + config["timeout"]
                while not Path(config["go"]).exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("Coordinator barrier was not released.")
                    time.sleep(0.005)
            report = _ingest(mesh, config)
    except Exception as exc:
        if config["action"] in {"seed", "ingest"}:
            report = recover_progress(config)
        report.update({"worker_failure": "OperationFailed", "error_type": type(exc).__name__, "error_stage": stage})
    finally:
        if mesh is not None:
            try:
                if config["surface"] == "receipt-sdk":
                    _close_cached_runtime()
                else:
                    mesh.close()
            except Exception as exc:
                report.setdefault("worker_failure", "CloseFailed")
                report["close_error_type"] = type(exc).__name__
        if config["surface"] == "http":
            try:
                checked = thin_client_metadata()
                if isinstance(metadata, dict):
                    metadata.update(checked)
            except RuntimeError:
                report.setdefault("worker_failure", "HttpClientImportedModelRuntime")
                if isinstance(metadata, dict):
                    metadata["client_loads_memory_or_model"] = True
        try:
            if not _sources_unchanged(config):
                report.setdefault("worker_failure", "RuntimeSourceChanged")
        except Exception as exc:
            report.setdefault("worker_failure", "RuntimeSourceUnverifiable")
            report["source_verification_error_type"] = type(exc).__name__
        report["metadata"] = metadata
        report["surface"] = config["surface"]
        report["group_commit_requested"] = config["group_commit"]
        report["process_cpu_seconds"] = round(time.process_time(), 6)
        write_json(Path(config["result"]), report)
    return int(bool(report.get("worker_failure")))


def _allowed_model_hashes(repo: Path) -> dict[str, set[str]]:
    tree = ast.parse((repo / "mesh" / "memory.py").read_text(encoding="utf-8"))
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for name in node.targets:
                if isinstance(name, ast.Name) and name.id in {"_ALLOWED_MODEL_SHA256", "_ALLOWED_TOKENIZER_SHA256"}:
                    result[name.id] = set(ast.literal_eval(node.value))
    if len(result) != 2:
        raise ValueError("Cannot verify the current repository model allowlists.")
    return result


def copy_verified_model(source: Path | None, destination: Path, repo: Path) -> dict[str, str] | None:
    if source is None:
        return None
    directory = source.expanduser().absolute()
    if not (directory / "model.onnx").is_file():
        directory /= "all-MiniLM-L6-v2-onnx"
    allowed = _allowed_model_hashes(repo)
    manifest: dict[str, str] = {}
    destination /= "models/all-MiniLM-L6-v2-onnx"
    _reject_links(destination)
    destination.mkdir(parents=True)
    for name, key, maximum in [
        ("model.onnx", "_ALLOWED_MODEL_SHA256", 200 * 1024 * 1024),
        ("tokenizer.json", "_ALLOWED_TOKENIZER_SHA256", 16 * 1024 * 1024),
    ]:
        path = directory / name
        _reject_links(path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError("Model artifact exceeds the verification byte budget.")
        digest = hashlib.sha256()
        copied = destination / (name + "." + uuid.uuid4().hex + ".tmp")
        copied_bytes = 0
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle, copied.open("xb") as output:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("Model source changed during verification.")
            for chunk in iter(lambda: handle.read(65536), b""):
                copied_bytes += len(chunk)
                if copied_bytes > maximum:
                    raise ValueError("Model artifact exceeds the verification byte budget.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
            after = os.fstat(handle.fileno())
            if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Model source changed during verification.")
        if digest.hexdigest() not in allowed[key]:
            raise ValueError("Model artifact checksum is not in the repository allowlist.")
        _reject_links(path)
        manifest[name] = digest.hexdigest()
        os.replace(copied, destination / name)
    return manifest


def footprint(root: Path) -> dict[str, int]:
    totals: Counter[str] = Counter()
    for path in root.rglob("*"):
        if path.is_file():
            size = path.stat().st_size
            group = "model_bytes" if "models" in path.parts else "wal_bytes" if path.name.endswith("-wal") else "other_bytes"
            if path.suffix == ".db":
                group = "database_bytes"
            totals[group] += size
            totals["total_bytes"] += size
    return dict(totals)


class Coordinator:
    def __init__(self, root: Path, repo: Path, backend: str, timeout: float,
                 expected_source: dict[str, str] | None = None, *, surface: str = "bare-sdk",
                 group_commit: int = 0, sampler: ProcessTreeSampler | None = None):
        self.root, self.repo, self.backend, self.timeout = root, repo, backend, timeout
        self.token = uuid.uuid4().hex
        self.processes: list[subprocess.Popen] = []
        self.number = 0
        self.surface, self.group_commit, self.sampler = surface, group_commit, sampler
        self.server: tuple[subprocess.Popen, dict[str, Any]] | None = None
        self.server_connection: dict[str, Any] = {}
        self.server_shutdown: dict[str, Any] | None = None
        self.source_manifest = source_manifest(repo) if expected_source is None else expected_source
        self.benchmark_source_manifest = benchmark_source_manifest()
        write_json(root / "benchmark-only.json", {"token": self.token, "fixture_version": FIXTURE_VERSION})

    def launch(self, action: str, **options: Any) -> tuple[subprocess.Popen, dict[str, Any]]:
        number = self.number
        self.number += 1
        config = {
            "root": str(self.root), "repo": str(self.repo), "token": self.token,
            "source_manifest": self.source_manifest,
            "benchmark_source_manifest": self.benchmark_source_manifest,
            "action": action, "backend": self.backend, "actor": "benchmark-" + str(number),
            "timeout": self.timeout, "mode": "scalar", "batch_size": 32,
            "surface": self.surface, "group_commit": self.group_commit, **self.server_connection, **options,
            "result": str(self.root / ("result-" + str(number) + ".json")),
            "ready": str(self.root / ("ready-" + str(number) + ".json")),
            "journal": str(self.root / ("journal-" + str(number) + ".jsonl")),
            "go": str(self.root / "go.json"),
            "stop": str(self.root / ("stop-" + str(number) + ".json")),
        }
        path = self.root / ("config-" + str(number) + ".json")
        write_json(path, config)
        environment = worker_environment(self.root, self.root / ("home-" + str(number)), self.backend, self.repo,
                                         config["group_commit"])
        with (self.root / ("log-" + str(number) + ".txt")).open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker-config", str(path)],
                cwd=self.repo, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        self.processes.append(process)
        if self.sampler is not None:
            role = "http_server" if action == "serve" else "write_clients" if action in {"seed", "ingest"} else "verification_clients"
            self.sampler.register(process.pid, role)
        return process, config

    def wait_ready(self, pending: list[tuple[subprocess.Popen, dict[str, Any]]]) -> None:
        deadline = time.monotonic() + self.timeout
        while not all(Path(config["ready"]).is_file() for _, config in pending):
            if any(process.poll() is not None for process, _ in pending):
                raise RuntimeError("WorkerExitedBeforeBarrier")
            if self.server is not None and self.server[0].poll() is not None:
                raise RuntimeError("HttpServerExitedBeforeBarrier")
            if time.monotonic() >= deadline:
                raise TimeoutError("ReadinessBarrierTimedOut")
            time.sleep(0.01)

    def start_http_server(self) -> dict[str, Any]:
        if self.surface != "http" or self.server is not None:
            raise ValueError("This coordinator cannot start another HTTP server.")
        self.server = self.launch("serve", actor="benchmark-server")
        self.wait_ready([self.server])
        ready = _read_json(Path(self.server[1]["ready"]), 65536)
        if not isinstance(ready.get("endpoint"), str) or not isinstance(ready.get("metadata"), dict):
            raise ValueError("Invalid HTTP server readiness publication.")
        self.server_connection = {"endpoint": ready["endpoint"], "server_metadata": ready["metadata"]}
        return ready

    def telemetry(self) -> dict[str, Any]:
        process, config = self.launch("telemetry", actor="benchmark-telemetry")
        report = self.collect(process, config)
        if report.get("worker_failure") or not isinstance(report.get("group_commit"), dict):
            raise RuntimeError("AuthenticatedHttpTelemetryFailed")
        return report["group_commit"]

    def collect(self, process: subprocess.Popen, config: dict[str, Any], expected_exit: int = 0,
                deadline: float | None = None) -> dict[str, Any]:
        timeout = self.timeout if deadline is None else max(0.0, min(self.timeout, deadline - time.monotonic()))
        timed_out = False
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait(timeout=5)
            timed_out = True
        path = Path(config["result"])
        try:
            report = _read_json(path)
            if config["action"] in {"seed", "ingest"}:
                _validate_write_report(report, config)
        except (ValueError, OSError, TypeError, KeyError, RecursionError):
            report = recover_progress(config)
            report["worker_failure"] = "MissingOrInvalidResult"
        if timed_out:
            report["worker_failure"] = "Timeout"
        elif code != expected_exit:
            # A valid result can precede a teardown failure. Its confirmed
            # acknowledgements still count, alongside the process failure.
            report.setdefault("worker_failure", "UnexpectedProcessExit")
        report["exit_code"] = code
        return report

    def stop(self) -> None:
        for process in self.processes:
            if process.poll() is None and (self.server is None or process is not self.server[0]):
                process.kill()
                process.wait(timeout=5)
        if self.server is not None:
            process, config = self.server
            try:
                if process.poll() is None:
                    write_json(Path(config["stop"]), {"stop": True})
                self.server_shutdown = self.collect(process, config, deadline=time.monotonic() + 15)
            except Exception as exc:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                self.server_shutdown = {"worker_failure": "ServerShutdownFailed", "error_type": type(exc).__name__}
            self.server = None


def run_case(options: argparse.Namespace, agents: int, mode: str, backend: str,
             surface: str = "bare-sdk", group_commit: int = 0) -> dict[str, Any]:
    if backend == "neural" and options.model_dir is None:
        return {"agents": agents, "mode": mode, "backend": backend, "surface": surface,
                "group_commit_requested": group_commit, "status": "unavailable",
                "reason": "An explicit verified local model directory was not provided."}
    with tempfile.TemporaryDirectory(prefix="mesh-production-bench-") as temporary:
        root = Path(temporary)
        sampler = ProcessTreeSampler()
        coordinator = Coordinator(root, options.repo_root, backend, options.timeout, getattr(options, "source_manifest", None),
                                  surface=surface, group_commit=group_commit, sampler=sampler)
        started = time.perf_counter()
        sampler.start()
        report: dict[str, Any] = {"agents": agents, "mode": mode, "backend": backend, "surface": surface,
                                  "group_commit_requested": group_commit,
                                  "runtime_source_manifest": coordinator.source_manifest}
        pending: list[tuple[subprocess.Popen, dict[str, Any]]] = []
        workers: list[dict[str, Any]] = []
        stage = "verify_model_copy"
        try:
            manifest = copy_verified_model(options.model_dir if backend == "neural" else None,
                                           root / "mesh", options.repo_root)
            if surface == "http":
                stage = "authenticated_http_server_startup"
                report["server_readiness"] = coordinator.start_http_server()
            stage = "seed_anchors"
            process, config = coordinator.launch("seed", actor="benchmark-seed", indices=list(range(-len(_ANCHORS), 0)))
            seed = coordinator.collect(process, config)
            report["seed_worker"] = seed
            if seed.get("worker_failure") or seed.get("failures") or seed.get("protocol_errors"):
                raise RuntimeError("SeedWorkerFailed")
            group_before = coordinator.telemetry() if surface == "http" else None
            stage = "worker_startup_barrier"
            pending = [coordinator.launch(
                "ingest", indices=list(range(index, options.memories, agents)),
                actor="benchmark-agent-" + str(index), mode=mode, batch_size=options.batch_size,
            ) for index in range(agents)]
            coordinator.wait_ready(pending)
            startup = time.perf_counter() - started
            sampler.mark_phase("ingestion")
            work_started = time.perf_counter()
            write_started_ns = time.perf_counter_ns()
            write_json(root / "go.json", {"go": True})
            stage = "collect_writes"
            deadline = time.monotonic() + options.timeout
            workers = [coordinator.collect(process, config, deadline=deadline) for process, config in pending]
            elapsed = time.perf_counter() - work_started
            completed = [worker.get("ingest_completed_ns") for worker in workers]
            acceptance_wall = ((max(completed) - write_started_ns) / 1e9 if completed
                               and all(type(value) is int and value >= write_started_ns for value in completed) else None)
            accounting = write_accounting(workers, options.memories)
            report["writes"] = accounting
            report["worker_failures"] = [worker for worker in workers if worker.get("worker_failure")]
            metadata = [worker.get("metadata") for worker in workers if worker.get("metadata")]
            sampler.mark_phase("verification")
            if surface == "http":
                stage = "verify_actual_group_commit_execution"
                group_after = coordinator.telemetry()
                report["group_commit_execution"] = group_execution(group_before, group_after, bool(group_commit),
                                                                   accounting["acknowledged_operation_ids"])
            stage = "verify_committed_ids_and_retrieval"
            verification_process, verification_config = coordinator.launch(
                "verify", actor="benchmark-verifier", acknowledgements=accounting["acknowledgements"],
                failed_rows=accounting["failures"],
                seed_acknowledgements=seed["acknowledgements"], submitted=options.memories,
                metadata=seed["metadata"], query_repetitions=options.query_repetitions,
                write_started_ns=write_started_ns,
            )
            verification = coordinator.collect(verification_process, verification_config)
            worker_failures = [worker for worker in workers if worker.get("worker_failure")]
            good = (
                not worker_failures and accounting["accounting_consistent"]
                and not accounting["failed_acknowledgements"] and not accounting["unattempted_rows"]
                and accounting["acknowledged_rows"] == options.memories
                and verification.get("integrity_ok") is True
                and not verification.get("worker_failure")
                and verification.get("lexical_anchor_readiness", {}).get("status") == "ready"
                and verification.get("retrieval", {}).get("keyword_queries", {}).get("status") == "completed"
                and (surface == "bare-sdk" or verification.get("operation_receipts", {}).get("status") == "confirmed")
                and (surface != "http" or report["group_commit_execution"]["execution_verified"])
                and (backend != "neural" or verification.get("dense_readiness", {}).get("status") == "indexed")
                and (backend != "neural" or verification.get("retrieval", {}).get("neural_hybrid", {}).get("status") == "completed")
            )
            report.update({
                "status": "completed" if good else "failed", "model_manifest": manifest,
                "startup_seconds": round(startup, 6), "write_wall_seconds": round(elapsed, 6),
                "write_boundary": "released_barrier_to_last_worker_exit_including_close",
                "durable_acceptance_wall_seconds": round(acceptance_wall, 6) if acceptance_wall is not None else None,
                "acceptance_boundary": "released_barrier_to_last_worker_finished_ingest_excluding_close",
                "mutation_surface": {"bare-sdk": "guarded_mesh_sdk", "receipt-sdk": "reviewed_meshclient_sdk_with_receipts",
                                     "http": "authenticated_meshclient_http_mcp_with_receipts"}[surface],
                "deduplicate": False, "receipt_producer_scope": "local-user" if surface != "bare-sdk" else None,
                "profile_policy": {"include_private": False, "read_only": False, "enforce_agent_ownership": True,
                                   "allow_namespace_override": False},
                "document_embedding_mode": "asynchronous_acceptance_then_separate_dense_readiness",
                "atomic_batch_size": options.batch_size if mode == "atomic" else 1,
                "acknowledged_rows_per_second": accounting["acknowledged_rows"] / acceptance_wall if acceptance_wall else None,
                "verified_committed_input_rows_per_second": (
                    verification["verified_committed_input_rows"] / acceptance_wall
                    if "verified_committed_input_rows" in verification and acceptance_wall else None
                ),
                "writes": accounting, "worker_failures": worker_failures,
                "write_call_latency": latency_summary([
                    value for worker in workers for value in worker.get("latencies_ms", [])
                ]),
                "worker_metadata": metadata, "readiness_and_retrieval": verification,
                "write_workers_process_cpu_seconds": sum(worker.get("process_cpu_seconds", 0.0) for worker in workers),
                "ingest_workers_process_cpu_seconds": sum(worker.get("ingest_process_cpu_seconds", 0.0) for worker in workers),
            })
        except Exception as exc:
            report.update({"status": "failed", "error_type": type(exc).__name__,
                           "error_stage": stage})
        finally:
            sampler.mark_phase("shutdown")
            coordinator.stop()
            if coordinator.server_shutdown is not None:
                report["http_server_shutdown"] = coordinator.server_shutdown
                if coordinator.server_shutdown.get("worker_failure"):
                    report["status"] = "failed"
            if pending and not workers:
                workers = [coordinator.collect(process, config) for process, config in pending]
                report["writes"] = write_accounting(workers, options.memories)
                report["worker_failures"] = [worker for worker in workers if worker.get("worker_failure")]
            elif workers:
                report.setdefault("writes", write_accounting(workers, options.memories))
            report["resources"] = sampler.stop()
            report["scenario_total_wall_seconds"] = round(time.perf_counter() - started, 6)
            report["footprint"] = footprint(root / "mesh") if (root / "mesh").exists() else {}
            # List only this validated newly created directory before automatic cleanup.
            report["cleanup_inventory"] = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            if root.resolve().parent != Path(tempfile.gettempdir()).resolve():
                raise RuntimeError("Unexpected temporary cleanup target.")
        return report


def crash_probes(options: argparse.Namespace) -> list[dict[str, Any]]:
    probes = []
    for action, code in [("crash-before", 71), ("crash-after", 72)]:
        with tempfile.TemporaryDirectory(prefix="mesh-production-bench-") as temporary:
            root = Path(temporary)
            coordinator = Coordinator(root, options.repo_root, "hash", options.timeout, getattr(options, "source_manifest", None))
            probe: dict[str, Any] = {"phase": action, "boundary": "process_death_and_fresh_process_restart"}
            stage = "crash_at_commit_boundary"
            try:
                process, config = coordinator.launch(action, actor="benchmark-crash")
                staged = coordinator.collect(process, config, expected_exit=code)
                probe["crashed_worker"] = staged
                probe["process_exit_code"] = staged.get("exit_code")
                if staged.get("worker_failure"):
                    probe.update({"status": "failed", "worker_failure": staged["worker_failure"]})
                    continue
                stage = "fresh_process_verify_and_replay"
                verifier, verification_config = coordinator.launch(
                    "verify-crash", actor="benchmark-crash", staged=staged, after_commit=code == 72,
                )
                result = coordinator.collect(verifier, verification_config)
                probe.update({"status": "completed" if result.get("integrity_ok") else "failed", **result})
            except Exception as exc:
                probe.update({"status": "failed", "error_type": type(exc).__name__, "error_stage": stage})
            finally:
                coordinator.stop()
                inventory = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
                probe["cleanup_inventory"] = inventory
                if root.resolve().parent != Path(tempfile.gettempdir()).resolve():
                    raise RuntimeError("Unexpected temporary cleanup target.")
                probes.append(probe)
    return probes


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--agents", nargs="+", type=int, choices=[1, 4, 8, 16], default=[1, 4, 8, 16])
    result.add_argument("--surfaces", nargs="+", choices=SURFACES, default=["receipt-sdk", "http"],
                        help="Reviewed receipt SDK/HTTP paths; bare-sdk is a separate diagnostic.")
    result.add_argument("--group-commit", nargs="+", type=int, choices=[0, 1], default=[0, 1],
                        help="MESH_GROUP_COMMIT off/on for HTTP scenarios only.")
    result.add_argument("--modes", nargs="+", choices=["scalar", "atomic"], default=["scalar", "atomic"])
    result.add_argument("--backends", nargs="+", choices=["hash", "neural"], default=["hash"])
    result.add_argument("--memories", type=int, default=512, help="Same total input rows in every scenario.")
    result.add_argument("--batch-size", type=int, default=32, help="Atomic batch rows (1–128).")
    result.add_argument("--query-repetitions", type=int, default=5)
    result.add_argument("--timeout", type=float, default=300, help="Bounded process/barrier deadline.")
    result.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="Explicit source checkout; never a data home.")
    result.add_argument("--model-dir", type=Path, help="Explicit verified model artifacts; no downloads.")
    result.add_argument("--output", type=Path, default=Path("production-bench.json"))
    result.add_argument("--skip-crash-probes", action="store_true")
    result.add_argument("--worker-config", type=Path, help=argparse.SUPPRESS)
    return result


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    options = argument_parser.parse_args(argv)
    if options.worker_config is not None:
        try:
            return worker(options.worker_config)
        except Exception as exc:
            print("Benchmark worker failed: " + type(exc).__name__, file=sys.stderr, flush=True)
            return 1
    if not 1 <= options.memories <= 100000 or not 1 <= options.batch_size <= 128:
        argument_parser.error("Use 1–100000 total memories and 1–128 rows per atomic batch.")
    if not 1 <= options.query_repetitions <= 100 or not 0 < options.timeout <= 3600:
        argument_parser.error("Use 1–100 query repetitions and a timeout of at most 3600 seconds.")
    options.repo_root = options.repo_root.resolve()
    if not (options.repo_root / "mesh" / "memory.py").is_file():
        argument_parser.error("--repo-root must name a Mesh source checkout.")
    options.source_manifest = source_manifest(options.repo_root)
    cases = []
    for backend in options.backends:
        for mode in options.modes:
            for agents in options.agents:
                for surface in options.surfaces:
                    for group_commit in options.group_commit if surface == "http" else [0]:
                        print(f"Benchmark {surface}/{backend}/{mode}/group={group_commit}: "
                              f"{agents} agents, {options.memories} rows", flush=True)
                        cases.append(run_case(options, agents, mode, backend, surface, group_commit))
    probes = [] if options.skip_crash_probes else crash_probes(options)
    report = {
        "report_version": 2, "fixture_version": FIXTURE_VERSION,
        "fixture_sha256": fixture_digest(options.memories),
        "total_input_rows": options.memories, "anchor_rows": len(_ANCHORS),
        "python_version": sys.version.split()[0],
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                     "logical_cpus": os.cpu_count()},
        "runtime_source_manifest": options.source_manifest,
        "benchmark_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "benchmark_source_manifest": benchmark_source_manifest(),
        "coverage_instrumented": any(name in os.environ for name in ("COVERAGE_PROCESS_START", "COV_CORE_SOURCE")),
        "cases": cases, "crash_probes": probes,
        "measurement_notes": [
            "Readiness, hash hybrid and verified neural hybrid measurements are reported separately.",
            "Successful write-call latency excludes failed acknowledgements; all failures remain in accounting.",
            "Input row count and fixture are held constant across agent counts and ingestion modes.",
            "Two flushed non-fsynced progress records per call are included in acceptance time; its final marker is published afterward.",
            "Per-call latencies exclude progress publication; successful atomic calls have batch latency, not per-row latency.",
            "Repeated retrieval passes can use production query/vector caches; the first pass is reported separately.",
            "Neural acceptance and verified indexing throughput are separate; hash hybrid is not dense retrieval.",
            "Receipt SDK and HTTP share fixture actors, policy and stable operation IDs; bare SDK has a separate boundary.",
            "HTTP clients load no Mesh/model runtime; one real authenticated server owns model and document indexing.",
            "HTTP query encoding follows the fixed server profile; neural HTTP never claims hash-encoded hybrid recall.",
            "HTTP group-commit results require authenticated execution counters; flag presence alone is insufficient.",
            "This synthetic fixture is not a held-out competitor comparison or a physical power-loss test.",
        ],
    }
    options.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(options.output, report)
    print("Benchmark report: " + str(options.output), flush=True)
    return int(any(case["status"] == "failed" for case in cases) or any(probe["status"] == "failed" for probe in probes))


if __name__ == "__main__":
    raise SystemExit(main())

