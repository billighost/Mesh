"""One-case production benchmark dispatch, with private checkpoints and safe exports.

Preparation/sealing/export import no Mesh/model runtime and start no child process.
Execution requires the reviewed-source seal and an explicit --quiet-window-open.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import stat
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import production_ledger as ledger
except ImportError:
    import production_ledger as ledger

MIB = 1024 * 1024
MAX_JSON = 64 * MIB
TERMINAL = {"completed", "failed", "deferred", "unavailable", "accounting_recovered"}
STATUS_REASONS = {
    "none", "budget", "resource", "readiness", "pilot_failed", "consecutive_lane_failure",
    "source_drift", "operator_stop", "report_contract", "child_failure", "unresolved_dispatch",
    "receipt_failure", "integrity_failure", "policy_failure", "accounting_failure", "cleanup_failure",
    "unavailable_model",
    "not_requested",
}
EXPECTED_MODEL_HASHES = {
    "model.onnx": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
    "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
}
SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent


class ControlError(RuntimeError):
    """A content-free control failure."""


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def no_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            value = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or (
            getattr(value, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ControlError("LinkedPathRejected")


def private_path(path: Path) -> Path:
    no_links(path.absolute())
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(REPO / ".work")
    except ValueError as exc:
        raise ControlError("PrivateArtifactOutsideWork") from exc
    if not relative.parts:
        raise ControlError("WorkRootIsNotARunDirectory")
    return resolved


def sha_file(path: Path, maximum: int = MAX_JSON) -> str:
    no_links(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise ControlError("InvalidBoundedRegularFile")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise ControlError("FileChangedBeforeHash")
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise ControlError("FileChangedWhileHashing")
    return digest.hexdigest()


def read_json(path: Path, maximum: int = MAX_JSON) -> dict[str, Any]:
    no_links(path)
    with path.open("rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ControlError("NotRegularJson")
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise ControlError("JsonTooLarge")
    def finite(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ControlError("NonFiniteJson")
        return result
    def reject_constant(value: str) -> None:
        raise ControlError("NonFiniteJson")
    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value = {}
        for key, item in items:
            if key in value:
                raise ControlError("DuplicateJsonKey")
            value[key] = item
        return value
    result = json.loads(data, parse_float=finite, parse_constant=reject_constant, object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ControlError("JsonObjectRequired")
    return result


def write_json(path: Path, value: Any) -> None:
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def assignments(path: Path) -> dict[str, Any]:
    result = {}
    for item in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    try:
                        result[target.id] = ast.literal_eval(item.value)
                    except (ValueError, TypeError):
                        pass
    return result


def manifest() -> dict[str, str]:
    result = {p.relative_to(REPO).as_posix(): sha_file(p, 4 * MIB)
              for p in sorted((REPO / "mesh").rglob("*.py"))}
    result["pyproject.toml"] = sha_file(REPO / "pyproject.toml", 4 * MIB)
    return ledger.source_manifest(result)


def harness_manifest() -> dict[str, str]:
    return {f"benchmarks/{name}": sha_file(REPO / "benchmarks" / name, 4 * MIB)
            for name in ("production_bench.py", "production_transport.py")}


def controller_manifest() -> dict[str, str]:
    return {name: sha_file(SCRIPT_DIR / name, 4 * MIB) for name in ("production_runner.py", "production_ledger.py")}


def runtime() -> dict[str, Any]:
    versions = {}
    for name in ("apsw", "numpy", "onnxruntime", "mcp", "psutil", "uvicorn", "starlette", "tokenizers"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "python_version": platform.python_version(), "packages": versions,
        "system": platform.system(), "release": platform.release(),
        "machine": platform.machine(), "logical_cpus": os.cpu_count(),
        "mesh_version": assignments(REPO / "mesh/version.py")["VERSION"],
    }


def model_fixture(directory: Path) -> dict[str, Any]:
    directory = private_path(directory)
    if not (directory / "model.onnx").is_file():
        directory = directory / "all-MiniLM-L6-v2-onnx"
    no_links(directory)
    allowed = assignments(REPO / "mesh/memory.py")
    hashes = {}
    sizes = {}
    for name, key, maximum in (
        ("model.onnx", "_ALLOWED_MODEL_SHA256", 200 * MIB),
        ("tokenizer.json", "_ALLOWED_TOKENIZER_SHA256", 16 * MIB),
    ):
        path = directory / name
        hashes[name] = sha_file(path, maximum)
        sizes[name] = path.stat().st_size
        if hashes[name] not in allowed.get(key, set()):
            raise ControlError("ModelDigestNotAllowlisted")
    return {"directory": str(directory), "hashes": hashes, "sizes": sizes}


def fixture_data() -> tuple[dict[str, str], dict[str, str]]:
    values = assignments(REPO / "benchmarks/production_bench.py")
    anchors = values["_ANCHORS"]
    templates = values["_TEMPLATES"]
    if len(anchors) != 12:
        raise ControlError("UnexpectedAnchorCount")
    labels = {value[0]: f"anchor_{index + 1:02}" for index, value in enumerate(anchors)}
    digests = {}
    for count in (128, 512, 10000):
        records = []
        for index in range(-len(anchors), count):
            if index < 0:
                anchor = anchors[-index - 1]
                content, tag = anchor[1], "benchmark-anchor-" + anchor[0]
            else:
                content = templates[index % len(templates)].format(i=index, v=11 + index * 7)
                tag = "benchmark-record-" + str(index)
            records.append({
                "content": content, "memory_type": "fact", "confidence": 1.0,
                "tags": ["production-benchmark", tag], "local_only": False, "trust_tier": "agent",
            })
        digests[str(count)] = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    return digests, labels


def cell(phase: str, backend: str, mode: str, agents: int, surface: str,
         group: int, count: int) -> dict[str, Any]:
    if surface != "http" and group:
        raise ControlError("NonHttpGroupFlag")
    repetition = 0 if phase == "pilot" else 1
    return {
        "id": f"{phase}-r{repetition}-{backend}-{mode}-{agents}a-{surface}-g{group}-{count}",
        "phase": phase, "repetition": repetition, "backend": backend, "mode": mode,
        "agents": agents, "surface": surface, "group_commit": group, "input_facts": count,
        "anchors": 12, "batch_size": 8 if mode == "atomic" else 1, "query_repetitions": 5,
        "process_timeout_seconds": 600 if phase == "scale" else 300,
        "outer_timeout_seconds": 1800 if phase == "scale" else 1200,
    }


def prepare(options: argparse.Namespace) -> dict[str, Any]:
    run_root = private_path(options.run_root)
    if run_root.exists() and any(run_root.iterdir()):
        raise ControlError("RunRootAlreadyContainsEvidence")
    runtime_before, harness_before = manifest(), harness_manifest()
    try:
        model = model_fixture(options.model_dir)
        if model["hashes"] != EXPECTED_MODEL_HASHES:
            raise ControlError("UnexpectedPinnedModelFixture")
        model["available"] = True
    except (ControlError, OSError):
        if not options.allow_unavailable_model:
            raise
        model = {"directory": str(private_path(options.model_dir)), "hashes": {}, "sizes": {}, "available": False}
    fixture_hashes, labels = fixture_data()
    pilots = [cell("pilot", "hash", "scalar", 16, "http", 1, 128)]
    pilots += [cell("pilot", "neural", "scalar", n, "receipt-sdk", 0, 128) for n in (1, 4)]
    pilots += [cell("pilot", "neural", "scalar", 16, "http", g, 128) for g in (0, 1)]
    primary = [
        cell("primary", backend, mode, n, surface, g, 512)
        for backend in ("hash", "neural") for mode in ("scalar", "atomic") for n in (1, 4, 8, 16)
        for surface in ("receipt-sdk", "http") for g in ((0, 1) if surface == "http" else (0,))
    ]
    scale = [cell("scale", backend, "scalar", 16, "http", g, 10000)
             for backend in ("hash", "neural") for g in (0, 1)]
    if len(primary) != 48 or len({item["id"] for item in pilots + primary + scale}) != 57:
        raise ControlError("MatrixRegistrationMismatch")
    if manifest() != runtime_before or harness_manifest() != harness_before:
        raise ControlError("SourcesChangedDuringPreparation")
    plan = {
        "schema_version": 1, "status": "prepared_not_executed", "created_utc": utc(),
        "run_root": str(run_root), "repo_root": str(REPO), "python": str(Path(sys.executable).resolve()),
        "runtime": runtime(), "runtime_sources": runtime_before, "harness_sources": harness_before,
        "controller_sources": controller_manifest(), "model": model,
        "fixture_hashes": fixture_hashes, "anchor_labels": labels, "cells": pilots + primary + scale,
        "primary_rounds": 1, "optional_repeats_registered": False,
        "limits": {
            "max_tree_rss_bytes": options.max_rss_mib * MIB,
            "minimum_available_ram_bytes": options.free_ram_mib * MIB,
            "critical_available_ram_bytes": 512 * MIB,
            "sdk_projection_multiplier": 1.25, "sdk_cache_margin_bytes": 256 * MIB,
            "dispatch_budget_seconds": {"pilot_primary": 5400, "scale": 1200},
            "cleanup_reserve_seconds": 60, "dense_readiness_wait_seconds": 60,
            "rss_excess_stop_seconds": 1.0, "pressure_stop_seconds": 10.0,
            "paging_output_threshold_pages_per_second": 256.0,
            "wrapper_sample_interval_seconds": 0.1,
        },
    }
    import psutil
    plan["hardware"] = {"logical_cpus": psutil.cpu_count(), "total_ram_bytes": psutil.virtual_memory().total}
    run_root.mkdir(parents=True, exist_ok=True)
    write_json(run_root / "plan.json", plan)
    write_json(run_root / "state.json", {
        "schema_version": 1, "started_utc": {}, "cells": {
            item["id"]: ({"status": "unavailable", "reason": "unavailable_model"}
                         if item["backend"] == "neural" and not model["available"]
                         else {"status": "planned", "reason": "none"}) for item in plan["cells"]
        },
    })
    snapshot(plan)
    return {"status": plan["status"], "plan": str(run_root / "plan.json"),
            "primary_cells": 48, "pilot_cells": 5, "scale_cells": 4,
            "verified_model_hashes": model["hashes"], "sealed": False}


def load_plan(path: Path) -> dict[str, Any]:
    path = private_path(path)
    plan = read_json(path)
    run_root = private_path(Path(plan["run_root"]))
    if path != run_root / "plan.json" or plan.get("repo_root") != str(REPO):
        raise ControlError("PlanBindingMismatch")
    if plan.get("primary_rounds") != 1 or plan.get("optional_repeats_registered") is not False:
        raise ControlError("UnsupportedRoundRegistration")
    if sum(item["phase"] == "primary" for item in plan["cells"]) != 48:
        raise ControlError("IncompletePrimaryRegistration")
    return plan


def unchanged(plan: dict[str, Any]) -> bool:
    return (
        manifest() == plan["runtime_sources"] and harness_manifest() == plan["harness_sources"]
        and controller_manifest() == plan["controller_sources"] and runtime() == plan["runtime"]
        and str(Path(sys.executable).resolve()) == plan["python"]
    )


def qualified_junit(path: Path, require_recovery: bool = False) -> dict[str, Any]:
    path = private_path(path)
    if path.stat().st_size > 4 * MIB:
        raise ControlError("QualificationXmlTooLarge")
    tree = ET.parse(path)
    cases = list(tree.iter("testcase"))
    if len(cases) < 1000:
        raise ControlError("FullSuiteEvidenceRequired")
    if any(case.find("failure") is not None or case.find("error") is not None for case in cases):
        raise ControlError("QualificationSuiteFailed")
    recovery = [
        case for case in cases if case.get("name")
        == "test_process_death_before_and_after_commit_then_fresh_process_replay"
        and case.find("skipped") is None
    ]
    if require_recovery and not recovery:
        raise ControlError("SameSourceRealRecoveryEvidenceRequired")
    return {"sha256": sha_file(path), "tests": len(cases),
            "skipped": sum(case.find("skipped") is not None for case in cases),
            "failures": 0, "errors": 0, "real_recovery_case_passed": bool(recovery)}


def seal(options: argparse.Namespace) -> dict[str, Any]:
    plan = load_plan(options.plan)
    if not options.root_confirms_other_checks_and_source_binding:
        raise ControlError("RootQualificationConfirmationRequired")
    if not unchanged(plan):
        raise ControlError("PrepareFreshPlanForFinalSource")
    if plan["model"]["available"]:
        model = model_fixture(Path(plan["model"]["directory"]))
        if model["hashes"] != plan["model"]["hashes"]:
            raise ControlError("ModelChangedAfterPreparation")
    if options.ci_qualification is not None:
        qualified = read_json(private_path(options.ci_qualification))
        if (qualified.get("runtime_sources") != plan["runtime_sources"]
                or qualified.get("harness_sources") != plan["harness_sources"]
                or qualified.get("runtime", {}).get("python_version") != plan["runtime"]["python_version"]
                or qualified.get("runtime", {}).get("packages") != plan["runtime"]["packages"]):
            raise ControlError("CiQualificationSourceOrNativeRuntimeMismatch")
    primary = qualified_junit(options.primary_junit, require_recovery=True)
    minimum = qualified_junit(options.minimum_junit)
    coverage = private_path(options.coverage_xml)
    if coverage.stat().st_size > 8 * MIB:
        raise ControlError("CoverageXmlTooLarge")
    rate = float(ET.parse(coverage).getroot().get("line-rate", "nan"))
    if not math.isfinite(rate) or rate < 0.60:
        raise ControlError("CoverageBelowRequiredGate")
    packaging_path = private_path(options.packaging_report)
    packaging = read_json(packaging_path)
    if packaging.get("status") != "passed":
        raise ControlError("PassingPackagingReportRequired")
    result = {
        "schema_version": 1, "status": "sealed_waiting_for_quiet_window", "sealed_utc": utc(),
        "plan_sha256": sha_file(Path(plan["run_root"]) / "plan.json"),
        "primary": primary, "minimum": minimum,
        "coverage": {"sha256": sha_file(coverage), "line_rate": rate},
        "packaging": {"sha256": sha_file(packaging_path), "status": "passed"},
        "ruff_docs_skill_passed_root_confirmed": True,
        "same_source_as_full_checks_root_confirmed": True,
        "real_recovery_qualification": "same_source_full_primary_suite",
    }
    write_json(Path(plan["run_root"]) / "seal.json", result)
    return {"status": result["status"], "plan_sha256": result["plan_sha256"]}


def base_environment(private: Path) -> dict[str, str]:
    preserved = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "LC_CTYPE",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in preserved}
    for name, leaf in {
        "HOME": "home", "USERPROFILE": "home", "APPDATA": "appdata",
        "LOCALAPPDATA": "localappdata", "XDG_CONFIG_HOME": "xdg", "XDG_CACHE_HOME": "cache",
        "TEMP": "tmp", "TMP": "tmp", "TMPDIR": "tmp", "MESH_HOME": "mesh", "MESH_DIR": "mesh",
    }.items():
        directory = private / leaf
        directory.mkdir(parents=True, exist_ok=True)
        env[name] = str(directory)
    for name in ("MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_NO_IMPLICIT_CLOUD",
                 "MESH_NO_DAEMON_PROXY", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        env[name] = "1"
    for name in ("MESH_AUTO_APPROVE", "MESH_AUTO_START_DAEMON", "MESH_ENABLE_AUTO_SYNC",
                 "MESH_ALLOW_CLOUD_CAPTURE", "MESH_ALLOW_CLOUD_SYNC", "MESH_ALLOW_LOG_SYNC"):
        env[name] = "0"
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[name] = "1"
    env.update(PYTHONPATH=str(REPO), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return env


def command(plan: dict[str, Any], item: dict[str, Any], output: Path) -> list[str]:
    args = [
        plan["python"], str(REPO / "benchmarks/production_bench.py"), "--repo-root", str(REPO),
        "--agents", str(item["agents"]), "--memories", str(item["input_facts"]),
        "--surfaces", item["surface"], "--group-commit", str(item["group_commit"]),
        "--modes", item["mode"], "--backends", item["backend"], "--batch-size", "8",
        "--query-repetitions", str(item["query_repetitions"]),
        "--timeout", str(item["process_timeout_seconds"]), "--skip-crash-probes",
        "--output", str(output),
    ]
    if item["backend"] == "neural":
        args += ["--model-dir", plan["model"]["directory"]]
    return args


def prior_results(plan: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(plan["run_root"]) / "numeric"
    return [read_json(path) for path in sorted(root.glob("*.json")) if path.name != "ledger.json"]


def hard_failure(result: dict[str, Any], execution: dict[str, Any]) -> str | None:
    checks, writes, verified = result["checks"], result["writes"], result["verification"]
    if execution.get("all_owned_processes_stopped") is not True:
        return "cleanup_failure"
    if execution.get("source_unchanged_after") is not True:
        return "source_drift"
    if checks["policy_observed"] and not checks["policy_matches_registered"]:
        return "policy_failure"
    if checks["verification_observed"] and not checks["integrity_ok"]:
        return "integrity_failure"
    if (verified["receipt_status"] == "failed" or writes["protocol_error_count"]
            or writes["unacknowledged_receipt_count"] or writes.get("unknown_outcome_rows")):
        return "receipt_failure"
    if (writes["unacknowledged_committed_row_count"] or checks["accounting_observed"]
            and (not checks["accounting_consistent"] or not checks["attempt_accounting_complete"])):
        return "accounting_failure"
    if checks["model_manifest_observed"] and not checks["model_bytes_match_registered"]:
        return "source_drift"
    return None


def record_dependent_stops(plan: dict[str, Any], state: dict[str, Any], item: dict[str, Any],
                           reason: str | None) -> None:
    if reason is not None:
        for planned in state["cells"].values():
            if planned["status"] == "planned":
                planned.update(status="deferred", reason=reason)
        write_json(Path(plan["run_root"]) / "STOP", {"reason": reason, "cell_id": item["id"]})
        return
    lane = tuple(item[name] for name in ("backend", "surface", "mode"))
    completed = [state["cells"][prior["id"]] for prior in plan["cells"]
                 if tuple(prior[name] for name in ("backend", "surface", "mode")) == lane
                 and state["cells"][prior["id"]]["status"] in {"completed", "failed", "unavailable"}]
    if len(completed) >= 2 and all(
        entry["status"] == "failed" and entry["reason"] in {"child_failure", "readiness", "unresolved_dispatch"}
        for entry in completed[-2:]
    ):
        for other in plan["cells"]:
            entry = state["cells"][other["id"]]
            if tuple(other[name] for name in ("backend", "surface", "mode")) == lane and entry["status"] == "planned":
                entry.update(status="deferred", reason="consecutive_lane_failure")


def forecast(item: dict[str, Any], results: list[dict[str, Any]]) -> float:
    candidates = [result for result in results if result.get("headline_eligible") is True
                  and result["cell"]["backend"] == item["backend"]
                  and result["cell"]["surface"] == item["surface"]]
    preferred_phase = "primary" if item["phase"] == "scale" else "pilot"
    preferred = [result for result in candidates if result["cell"]["phase"] == preferred_phase]
    candidates = preferred or candidates
    if not candidates:
        return 600.0 if item["phase"] == "scale" else 120.0
    reference = min(candidates, key=lambda row: (
        abs(row["cell"]["agents"] - item["agents"]),
        row["cell"]["mode"] != item["mode"],
        row["cell"]["group_commit"] != item["group_commit"],
        abs(row["cell"]["input_facts"] - item["input_facts"]),
    ))
    timing = reference["timing"]
    total = float(timing.get("scenario_total_wall_seconds") or 120)
    startup = float(timing.get("startup_seconds") or 0)
    row_ratio = item["input_facts"] / reference["cell"]["input_facts"]
    agent_ratio = max(1.0, item["agents"] / reference["cell"]["agents"])
    # Exact and receipt verification also scale with rows, not just ingestion.
    factor = 2.0 if item["phase"] == "scale" else 1.5
    return max(30.0, factor * (startup * agent_ratio + max(0.0, total - startup) * row_ratio))


def resource_gate(plan: dict[str, Any], item: dict[str, Any], results: list[dict[str, Any]],
                  available: int) -> str | None:
    limits = plan["limits"]
    if available < limits["minimum_available_ram_bytes"]:
        return "resource"
    if item["backend"] == "neural" and item["surface"] == "receipt-sdk" and item["agents"] > 1:
        if any(
            row["cell"]["backend"] == "neural" and row["cell"]["surface"] == "receipt-sdk"
            and row["cell"]["agents"] <= item["agents"]
            and (row["checks"].get("resource_budget_exceeded") is True
                 or row["execution"].get("sustained_paging_observed") is True)
            for row in results
        ):
            return "resource"
        references = [row for row in results if row["cell"]["backend"] == "neural"
                      and row["cell"]["surface"] == "receipt-sdk"
                      and row["cell"]["agents"] < item["agents"]
                      and row.get("headline_eligible") is True]
        if not references:
            return "pilot_failed"
        reference = max(references, key=lambda row: row["cell"]["agents"])
        roles = reference["resources"]["roles"]
        writers = (roles.get("write_clients") or {}).get("peak_rss_bytes")
        if not writers:
            return "resource"
        overhead = sum(float(roles.get(role, {}).get("peak_rss_bytes") or 0)
                       for role in ledger.ROLES - {"write_clients"})
        projected = (
            overhead + writers / reference["cell"]["agents"] * item["agents"]
            * limits["sdk_projection_multiplier"] + limits["sdk_cache_margin_bytes"]
        )
        if projected > min(limits["max_tree_rss_bytes"], available - limits["minimum_available_ram_bytes"]):
            return "resource"
    if item["phase"] == "scale" and item["backend"] == "neural":
        references = [row for row in results if row["cell"]["phase"] == "primary"
                      and row["cell"]["backend"] == "neural" and row["cell"]["surface"] == "http"
                      and row["cell"]["agents"] == 16 and row.get("headline_eligible") is True]
        if not references:
            return "readiness"
        lag = max(float(row["verification"]["dense"].get("wait_wall_seconds") or 0) for row in references)
        if lag * (10000 / 512) * 2 > limits["dense_readiness_wait_seconds"]:
            return "readiness"
    return None


class PagingMonitor:
    """Observe Windows host page output; never confuse model reads with swapping."""

    def __init__(self) -> None:
        self.available = False
        self.query = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class FormattedUnion(ctypes.Union):
                _fields_ = [("doubleValue", ctypes.c_double), ("largeValue", ctypes.c_longlong)]

            class FormattedValue(ctypes.Structure):
                _fields_ = [("CStatus", wintypes.DWORD), ("value", FormattedUnion)]

            self.ctypes, self.value_type = ctypes, FormattedValue
            self.library = ctypes.WinDLL("pdh", use_last_error=True)
            self.library.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            self.library.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            self.library.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            self.library.PdhGetFormattedCounterValue.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(FormattedValue)]
            self.library.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            query, counter = ctypes.c_void_p(), ctypes.c_void_p()
            if self.library.PdhOpenQueryW(None, 0, ctypes.byref(query)):
                return
            self.query = query
            if self.library.PdhAddEnglishCounterW(query, r"\Memory\Pages Output/sec", 0, ctypes.byref(counter)):
                self.close()
                return
            self.counter = counter
            self.available = self.library.PdhCollectQueryData(query) == 0
        except (AttributeError, OSError):
            self.close()

    def sample(self) -> float | None:
        if not self.available:
            return None
        value = self.value_type()
        if (self.library.PdhCollectQueryData(self.query)
                or self.library.PdhGetFormattedCounterValue(self.counter, 0x200, None, self.ctypes.byref(value))
                or value.CStatus not in (0, 1)):
            return None
        result = float(value.value.doubleValue)
        return result if math.isfinite(result) and result >= 0 else None

    def close(self) -> None:
        if self.query is not None:
            self.library.PdhCloseQuery(self.query)
            self.query = None


def alive(psutil: Any, pid: int, created: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.create_time() == created and process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def stop_owned(psutil: Any, observed: dict[int, float]) -> bool:
    targets = []
    for pid, created in observed.items():
        if alive(psutil, pid, created):
            targets.append(psutil.Process(pid))
    for process in reversed(targets):
        try:
            if process.create_time() == observed[process.pid]:
                process.terminate()
        except psutil.Error:
            pass
    _, waiting = psutil.wait_procs(targets, timeout=3)
    for process in waiting:
        try:
            if process.create_time() == observed[process.pid]:
                process.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(waiting, timeout=3)
    return not any(alive(psutil, pid, created) for pid, created in observed.items())


def execute(plan: dict[str, Any], item: dict[str, Any], output: Path, private: Path) -> dict[str, Any]:
    import psutil

    observed: dict[int, float] = {}
    peak = 0
    minimum_available = int(psutil.virtual_memory().available)
    started = time.monotonic()
    interrupted = False
    stop_reason = "none"
    child = None
    paging = PagingMonitor()
    paging_peak = 0.0
    paging_samples = 0
    sustained_paging = False
    execution_path = private / "execution.json"
    private.mkdir(parents=True, exist_ok=True)
    with (private / "stdout.log").open("xb") as stdout, (private / "stderr.log").open("xb") as stderr:
        try:
            child = subprocess.Popen(
                command(plan, item, output), cwd=REPO, env=base_environment(private),
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            process = psutil.Process(child.pid)
            observed[child.pid] = process.create_time()
            write_json(execution_path, {"status": "dispatching", "pid": child.pid,
                                        "created": observed[child.pid], "started_utc": utc()})
            critical_samples = 0
            rss_excess_started = pressure_started = None
            while child.poll() is None:
                rss = 0
                try:
                    processes = [process, *process.children(recursive=True)]
                except psutil.Error:
                    processes = []
                for current in processes:
                    try:
                        observed[current.pid] = current.create_time()
                        rss += current.memory_info().rss
                    except psutil.Error:
                        continue
                peak = max(peak, rss)
                available = int(psutil.virtual_memory().available)
                minimum_available = min(minimum_available, available)
                now = time.monotonic()
                page_output = paging.sample()
                if page_output is not None:
                    paging_samples += 1
                    paging_peak = max(paging_peak, page_output)
                pressure = (page_output is not None
                            and page_output > plan["limits"]["paging_output_threshold_pages_per_second"]
                            and available < plan["limits"]["minimum_available_ram_bytes"])
                pressure_started = (pressure_started or now) if pressure else None
                rss_excess_started = (rss_excess_started or now) if rss > plan["limits"]["max_tree_rss_bytes"] else None
                critical_samples = critical_samples + 1 if available < plan["limits"]["critical_available_ram_bytes"] else 0
                if critical_samples >= 3:
                    stop_reason = "resource"
                    break
                if pressure_started is not None and now - pressure_started >= plan["limits"]["pressure_stop_seconds"]:
                    sustained_paging, stop_reason = True, "resource"
                    break
                if rss_excess_started is not None and now - rss_excess_started >= plan["limits"]["rss_excess_stop_seconds"]:
                    stop_reason = "resource"
                    break
                if time.monotonic() - started > item["outer_timeout_seconds"]:
                    stop_reason = "child_failure"
                    break
                time.sleep(plan["limits"]["wrapper_sample_interval_seconds"])
        except KeyboardInterrupt:
            interrupted, stop_reason = True, "operator_stop"
        finally:
            if child is not None and child.poll() is None:
                stop_owned(psutil, observed)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            all_stopped = stop_owned(psutil, observed)
            paging.close()
    result = {
        "status": "finished", "exit_code": child.returncode if child is not None else None,
        "wrapper_wall_seconds": round(time.monotonic() - started, 6),
        "observed_peak_tree_rss_bytes": peak, "min_available_ram_bytes": minimum_available,
        "resource_budget_exceeded": peak > plan["limits"]["max_tree_rss_bytes"],
        "all_owned_processes_stopped": all_stopped, "stop_reason": stop_reason,
        "source_unchanged_after": unchanged(plan), "interrupted": interrupted,
        "paging_diagnostic_available": paging.available,
        "paging_sample_count": paging_samples,
        "sampled_peak_pages_output_per_second": round(paging_peak, 6) if paging_samples else None,
        "sustained_paging_observed": sustained_paging,
        "observed_pids": observed,
    }
    write_json(execution_path, result)
    return result


def interrupted_report(plan: dict[str, Any], item: dict[str, Any], private: Path,
                       execution: dict[str, Any]) -> dict[str, Any]:
    """Recover flushed observations without restarting workers or replaying writes."""
    reports, metadata, recovery_errors = [], [], []
    progress_observed = False
    if execution.get("source_unchanged_after") is True:
        sys.path.insert(0, str(REPO))
        from benchmarks import production_bench as bench

        if Path(bench.__file__).resolve() != REPO / "benchmarks/production_bench.py":
            raise ControlError("RecoveryHarnessBindingMismatch")
        roots = sorted((private / "tmp").glob("mesh-production-bench-*"))
        for evidence_root in roots:
            private_path(evidence_root)
            for path in sorted(evidence_root.glob("config-*.json")):
                try:
                    config = read_json(path, MIB)
                    if config.get("action") != "ingest":
                        continue
                    if (config.get("repo") != str(REPO) or config.get("root") != str(evidence_root)
                            or config.get("source_manifest") != plan["runtime_sources"]
                            or config.get("benchmark_source_manifest") != plan["harness_sources"]
                            or config.get("backend") != item["backend"] or config.get("mode") != item["mode"]
                            or config.get("surface") != item["surface"]
                            or config.get("group_commit") != item["group_commit"]):
                        raise ControlError("PrivateProgressBindingMismatch")
                    for name in ("journal", "result", "ready"):
                        if private_path(Path(config[name])).parent != evidence_root:
                            raise ControlError("PrivateProgressPathMismatch")
                    original = Path(config["result"])
                    if original.exists():
                        recovered = read_json(original)
                        bench._validate_write_report(recovered, config)
                    else:
                        recovered = bench.recover_progress(config)
                        recovered["worker_failure"] = "MissingOrInvalidResult"
                    bench._validate_write_report(recovered, config)
                    reports.append(recovered)
                    progress_observed = True
                    ready = Path(config["ready"])
                    observed_metadata = recovered.get("metadata")
                    if not isinstance(observed_metadata, dict) and ready.exists():
                        observed_metadata = read_json(ready, MIB)
                    if isinstance(observed_metadata, dict):
                        metadata.append(observed_metadata)
                except (OSError, ValueError, TypeError, KeyError, ControlError) as exc:
                    recovery_errors.append(type(exc).__name__)
        accounting = bench.write_accounting(reports, item["input_facts"])
    else:
        accounting = {"submitted_rows": item["input_facts"]}
    if not progress_observed or recovery_errors:
        # Missing private evidence cannot establish that unobserved writes never ran.
        accounting.update(attempted_rows=None, unattempted_rows=None, unknown_outcome_rows=None,
                          attempt_accounting_complete=False, accounting_consistent=False)
    return {
        "report_version": 2, "fixture_version": 1,
        "fixture_sha256": plan["fixture_hashes"][str(item["input_facts"])],
        "total_input_rows": item["input_facts"], "anchor_rows": 12,
        "python_version": plan["runtime"]["python_version"], "coverage_instrumented": False,
        "runtime_source_manifest": plan["runtime_sources"], "benchmark_source_manifest": plan["harness_sources"],
        "collection_kind": "interrupted_dispatch_recovery", "original_harness_report_published": False,
        "progress_recovery_error_classes": recovery_errors,
        "cases": [{
            "agents": item["agents"], "backend": item["backend"], "mode": item["mode"],
            "surface": item["surface"], "group_commit_requested": item["group_commit"], "status": "failed",
            "error_type": "ResourceLimitExceeded" if execution["stop_reason"] == "resource" else "TimeoutError",
            "error_stage": "wrapper_collection", "writes": accounting,
            "worker_failures": [report for report in reports if report.get("worker_failure")],
            "recovered_worker_observations": reports, "worker_metadata": metadata,
            "scenario_total_wall_seconds": execution["wrapper_wall_seconds"],
        }],
    }


def snapshot(plan: dict[str, Any], destination: Path | None = None) -> dict[str, Any]:
    root = Path(plan["run_root"])
    state = read_json(root / "state.json")
    records = []
    counts = {"planned": 0, "dispatching": 0, **{key: 0 for key in TERMINAL}}
    for item in plan["cells"]:
        entry = state["cells"][item["id"]]
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        record = {
            "cell": {name: item[name] for name in (
                "id", "phase", "repetition", "backend", "mode", "surface", "group_commit",
                "agents", "input_facts", "anchors", "batch_size", "query_repetitions",
            )},
            "status": entry["status"], "reason": entry["reason"],
        }
        numeric = root / "numeric" / (item["id"] + ".json")
        if numeric.exists():
            record["result"] = read_json(numeric)
        ledger.assert_safe_output(record)
        write_json(root / "public/cells" / (item["id"] + ".json"), record)
        records.append(record)
    header = {
        "schema_version": 1, "report_version": 2, "fixture_version": 1,
        "created_utc": plan["created_utc"], "checkpoint_utc": utc(),
        "status": "complete" if all(
            state["cells"][item["id"]]["status"] in TERMINAL for item in plan["cells"]
            if item["phase"] == "primary"
        ) else "in_progress",
        "runtime": plan["runtime"], "model_hashes": plan["model"]["hashes"],
        "expected_model_hashes": EXPECTED_MODEL_HASHES,
        "model_fixture_available": plan["model"]["available"],
        "hardware": plan["hardware"], "declared_resource_limits": plan["limits"],
        "model_sizes": plan["model"]["sizes"], "fixture_hashes": plan["fixture_hashes"],
        "primary_rounds": 1, "primary_planned_cells": 48, "pilot_planned_cells": 5,
        "scale_planned_cells": 4, "status_counts": counts, "cells": records,
        "boundary_codes": {
            "acceptance": "barrier_to_last_ingestion_completion_excluding_close",
            "fully_indexed": "barrier_to_fresh_process_revision_and_model_confirmation",
            "atomic_latency": "batch_call_percentile",
            "resources": "sampled_summed_rss_may_double_count_shared_pages",
            "retrieval_scope": "twelve_functional_anchors_not_competitor_superiority",
        },
        "controller_hashes": plan["controller_sources"],
    }
    ledger.assert_safe_output(header)
    header["runtime_source_manifest"] = ledger.source_manifest(plan["runtime_sources"])
    header["harness_source_manifest"] = ledger.source_manifest(plan["harness_sources"])
    for label, value in (("runtime_source_sha256", header["runtime_source_manifest"]),
                         ("harness_source_sha256", header["harness_source_manifest"])):
        header[label] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    seal_path = root / "seal.json"
    if seal_path.exists():
        qualification = read_json(seal_path)
        header["qualification"] = {name: qualification[name] for name in (
            "primary", "minimum", "coverage", "packaging", "real_recovery_qualification",
        )}
        ledger.assert_safe_output(header["qualification"])
    if destination is None:
        destination = root / "numeric" / "ledger.json"
    else:
        no_links(destination.absolute())
        destination = destination.resolve()
        try:
            destination.relative_to(REPO / "benchmarks/results")
        except ValueError as exc:
            raise ControlError("TrackedExportOutsideBenchmarkResults") from exc
        if header["status"] != "complete" or not seal_path.exists():
            raise ControlError("IncompleteRunCannotPublishTrackedOutput")
        if destination.suffix != ".json":
            raise ControlError("TrackedExportMustBeJson")
    write_json(destination, header)
    return {"status": header["status"], "ledger": str(destination),
            "status_counts": counts, "primary_planned_cells": 48}


def step(options: argparse.Namespace) -> dict[str, Any]:
    import psutil

    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ControlError("CiOnlyMatrixDispatch")
    if not options.quiet_window_open:
        raise ControlError("ExplicitRootQuietWindowRequired")
    plan = load_plan(options.plan)
    root = Path(plan["run_root"])
    seal_path = root / "seal.json"
    if not seal_path.exists() or read_json(seal_path).get("plan_sha256") != sha_file(root / "plan.json"):
        raise ControlError("FinalSourceQualificationSealRequired")
    if not unchanged(plan):
        raise ControlError("SourceOrRuntimeChanged")
    if any(name in os.environ for name in ("COVERAGE_PROCESS_START", "COV_CORE_SOURCE")):
        raise ControlError("InstrumentedBenchmarkRejected")
    state = read_json(root / "state.json")
    if any(entry["status"] == "dispatching" for entry in state["cells"].values()):
        raise ControlError("UnresolvedDispatchRequiresManualAccountingReview")
    selected = None
    if options.cell_id is not None:
        selected = next((item for item in plan["cells"] if item["id"] == options.cell_id), None)
        if selected is None or selected["phase"] != options.phase:
            raise ControlError("UnregisteredOrWrongPhaseCell")
        existing = state["cells"][selected["id"]]
        if existing["status"] != "planned":
            return {"cell_id": selected["id"], "status": existing["status"],
                    "reason": existing["reason"], **snapshot(plan)}
    if (root / "STOP").exists():
        raise ControlError("OperatorStopMarkerPresent")
    pending = [item for item in plan["cells"] if item["phase"] == options.phase
               and state["cells"][item["id"]]["status"] == "planned"]
    if selected is not None:
        pending = [selected, *[item for item in pending if item["id"] != selected["id"]]]
    if not pending:
        return {"status": "phase_complete", "phase": options.phase, **snapshot(plan)}
    item = pending[0]
    if options.phase != "pilot" and any(
        entry["status"] == "planned" for name, entry in state["cells"].items() if name.startswith("pilot-")
    ):
        raise ControlError("FinishSeparatePilotsBeforePrimaryOrScale")
    if options.phase == "scale" and any(
        entry["status"] == "planned" for name, entry in state["cells"].items() if name.startswith("primary-")
    ):
        raise ControlError("FinishPrimaryRegistrationBeforeScale")
    bucket = "scale" if options.phase == "scale" else "pilot_primary"
    started = state["started_utc"].setdefault(bucket, utc())
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds()
    results = prior_results(plan)
    remaining = plan["limits"]["dispatch_budget_seconds"][bucket] - elapsed
    reason = resource_gate(plan, item, results, int(psutil.virtual_memory().available))
    needed = forecast(item, results) + plan["limits"]["cleanup_reserve_seconds"]
    if item["surface"] == "http" and item["group_commit"] == 0 and options.phase != "pilot":
        counterpart = next((value for value in pending if value["backend"] == item["backend"]
                            and value["mode"] == item["mode"] and value["agents"] == item["agents"]
                            and value["group_commit"] == 1), None)
        if counterpart is not None:
            needed += forecast(counterpart, results) + plan["limits"]["cleanup_reserve_seconds"]
    if remaining < needed:
        reason = "budget"
    if reason is not None:
        state["cells"][item["id"]] = {"status": "deferred", "reason": reason}
        write_json(root / "state.json", state)
        snapshot(plan)
        return {"status": "deferred", "reason": reason, "cell_id": item["id"]}
    entry = {"status": "dispatching", "reason": "none", "started_utc": utc(),
             "forecast_seconds": round(needed, 3)}
    state["cells"][item["id"]] = entry
    write_json(root / "state.json", state)
    snapshot(plan)
    output = root / "raw" / (item["id"] + ".json")
    private = root / "private" / item["id"]
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ControlError("ExistingOutputMustNeverBeOverwrittenByReplay")
    print(json.dumps({"status": "dispatching", "cell_id": item["id"]}), flush=True)
    execution = execute(plan, item, output, private)
    if not output.exists():
        write_json(output, interrupted_report(plan, item, private, execution))
    entry["execution"] = execution
    entry["finished_utc"] = utc()
    entry["status"] = "failed"
    entry["reason"] = execution["stop_reason"] if execution["stop_reason"] in STATUS_REASONS else "child_failure"
    stopping_reason = ("cleanup_failure" if execution.get("all_owned_processes_stopped") is not True else
                       "source_drift" if execution.get("source_unchanged_after") is not True else None)
    if output.exists():
        try:
            execution["input_report_sha256"] = sha_file(output)
            projection = ledger.result_projection(read_json(output), item, plan, execution)
            write_json(root / "numeric" / (item["id"] + ".json"), projection)
            entry["status"] = "completed" if projection["headline_eligible"] else (
                "unavailable" if projection["harness_status"] == "unavailable" else "failed"
            )
            entry["reason"] = "none" if entry["status"] == "completed" else "child_failure"
            if entry["status"] == "failed" and (execution["resource_budget_exceeded"]
                    or execution.get("sustained_paging_observed") or execution["stop_reason"] == "resource"):
                entry["reason"] = "resource"
            stopping_reason = hard_failure(projection, execution) or stopping_reason
            if stopping_reason is not None:
                entry["reason"] = stopping_reason
        except (ledger.LedgerError, ValueError, TypeError, KeyError, OSError):
            entry["reason"] = "report_contract"
            stopping_reason = "source_drift" if execution.get("source_unchanged_after") is not True else "report_contract"
    else:
        entry["reason"] = "unresolved_dispatch"
        entry["private_progress_inventory"] = sorted(
            str(path.relative_to(private)) for path in private.rglob("journal-*.jsonl")
        )
    state["cells"][item["id"]] = entry
    record_dependent_stops(plan, state, item, stopping_reason)
    write_json(root / "state.json", state)
    snapshot(plan)
    return {"status": entry["status"], "reason": entry["reason"], "cell_id": item["id"],
            "raw_report_saved": output.exists(), "all_owned_processes_stopped": execution["all_owned_processes_stopped"]}


def defer(options: argparse.Namespace) -> dict[str, Any]:
    plan = load_plan(options.plan)
    root = Path(plan["run_root"])
    state = read_json(root / "state.json")
    changed = 0
    for item in plan["cells"]:
        if item["phase"] == options.phase and state["cells"][item["id"]]["status"] == "planned":
            state["cells"][item["id"]] = {"status": "deferred", "reason": options.reason}
            changed += 1
    write_json(root / "state.json", state)
    return {"deferred_cells": changed, **snapshot(plan)}


def finalize(options: argparse.Namespace) -> dict[str, Any]:
    import psutil

    plan = load_plan(options.plan)
    root = Path(plan["run_root"])
    state = read_json(root / "state.json")
    for item in plan["cells"]:
        entry = state["cells"][item["id"]]
        if entry["status"] == "planned":
            entry.update(status="deferred", reason="operator_stop")
        elif entry["status"] == "dispatching":
            private = root / "private" / item["id"]
            execution_path = private / "execution.json"
            saved = read_json(execution_path) if execution_path.exists() else {}
            observed = {int(pid): created for pid, created in saved.get("observed_pids", {}).items()}
            if "pid" in saved and "created" in saved:
                observed[int(saved["pid"])] = saved["created"]
            for pid, created in list(observed.items()):
                if alive(psutil, pid, created):
                    try:
                        for process in psutil.Process(pid).children(recursive=True):
                            observed[process.pid] = process.create_time()
                    except psutil.Error:
                        pass
            stopped = stop_owned(psutil, observed)
            execution = {
                "exit_code": None, "source_unchanged_after": unchanged(plan),
                "all_owned_processes_stopped": stopped, "resource_budget_exceeded": False,
                "stop_reason": "operator_stop", "wrapper_wall_seconds": None,
                "observed_peak_tree_rss_bytes": None, "min_available_ram_bytes": None,
                "observed_pids": observed,
            }
            output = root / "raw" / (item["id"] + ".json")
            if not output.exists():
                write_json(output, interrupted_report(plan, item, private, execution))
            execution["input_report_sha256"] = sha_file(output)
            projection = ledger.result_projection(read_json(output), item, plan, execution)
            write_json(root / "numeric" / (item["id"] + ".json"), projection)
            entry.update(status="failed", reason="unresolved_dispatch", execution=execution)
    write_json(root / "state.json", state)
    return {"status": "finalized_without_replay", **snapshot(plan)}


def check(options: argparse.Namespace) -> dict[str, Any]:
    plan = load_plan(options.plan)
    root = Path(plan["run_root"])
    seal_path = root / "seal.json"
    if (not seal_path.exists()
            or read_json(seal_path).get("plan_sha256") != sha_file(root / "plan.json")):
        raise ControlError("FinalSourceQualificationSealRequired")
    if not unchanged(plan):
        raise ControlError("SourceOrRuntimeChanged")
    state = read_json(Path(plan["run_root"]) / "state.json")
    primary = [state["cells"][item["id"]] for item in plan["cells"] if item["phase"] == "primary"]
    scale = [state["cells"][item["id"]] for item in plan["cells"] if item["phase"] == "scale"]
    if any(entry["status"] in {"failed", "dispatching"} for entry in scale):
        raise ControlError("ExecutedScaleFailureRequiresReview")
    completed = sum(entry["status"] == "completed" for entry in primary)
    deferred = sum(entry["status"] == "deferred" for entry in primary)
    if not completed:
        raise ControlError("NoCompletedPrimaryBaseline")
    if any(entry["status"] in {"failed", "unavailable", "planned", "dispatching"} for entry in primary):
        raise ControlError("PrimaryFailureOrUnavailabilityRequiresReview")
    summary = snapshot(plan)
    return {**summary, "status": "measured_primary_success_with_visible_deferrals",
            "completed_primary_cells": completed, "deferred_primary_cells": deferred}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    prep = actions.add_parser("prepare")
    prep.add_argument("--run-root", type=Path, required=True)
    prep.add_argument("--model-dir", type=Path, required=True)
    prep.add_argument("--max-rss-mib", type=int, default=4096)
    prep.add_argument("--free-ram-mib", type=int, default=2048)
    prep.add_argument("--allow-unavailable-model", action="store_true")
    sealing = actions.add_parser("seal")
    sealing.add_argument("--plan", type=Path, required=True)
    sealing.add_argument("--primary-junit", type=Path, required=True)
    sealing.add_argument("--minimum-junit", type=Path, required=True)
    sealing.add_argument("--coverage-xml", type=Path, required=True)
    sealing.add_argument("--packaging-report", type=Path, required=True)
    sealing.add_argument("--root-confirms-other-checks-and-source-binding", action="store_true")
    sealing.add_argument("--ci-qualification", type=Path)
    dispatch = actions.add_parser("step")
    dispatch.add_argument("--plan", type=Path, required=True)
    dispatch.add_argument("--phase", choices=("pilot", "primary", "scale"), required=True)
    dispatch.add_argument("--quiet-window-open", action="store_true")
    dispatch.add_argument("--cell-id")
    export = actions.add_parser("snapshot")
    export.add_argument("--plan", type=Path, required=True)
    export.add_argument("--destination", type=Path)
    deferred = actions.add_parser("defer")
    deferred.add_argument("--plan", type=Path, required=True)
    deferred.add_argument("--phase", choices=("pilot", "primary", "scale"), required=True)
    deferred.add_argument("--reason", choices=sorted(STATUS_REASONS - {"none"}), required=True)
    finishing = actions.add_parser("finalize")
    finishing.add_argument("--plan", type=Path, required=True)
    acceptance = actions.add_parser("check")
    acceptance.add_argument("--plan", type=Path, required=True)
    options = parser.parse_args()
    if options.action == "prepare":
        if not 512 <= options.max_rss_mib <= 16384 or not 512 <= options.free_ram_mib <= 16384:
            raise ControlError("ResourceBudgetOutsideBounds")
        result = prepare(options)
    elif options.action == "seal":
        result = seal(options)
    elif options.action == "step":
        result = step(options)
    elif options.action == "snapshot":
        result = snapshot(load_plan(options.plan), options.destination)
    elif options.action == "finalize":
        result = finalize(options)
    elif options.action == "check":
        result = check(options)
    else:
        result = defer(options)
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ControlError as exc:
        print(json.dumps({"status": "control_error", "code": str(exc)}), flush=True)
        raise SystemExit(2) from None
