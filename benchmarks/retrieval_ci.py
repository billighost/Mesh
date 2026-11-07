"""Supervise isolated retrieval CI and always preserve a content-free job artifact."""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from prepare_retrieval_ci import BOUNDED_DATASETS, DATASETS, MODEL
from production_retrieval import (MODEL_TAG, PASSAGE_BATCH_LIMIT, PASSAGE_REPRESENTATION,
                                  WORKER_FAILURE_SOURCES, WORKER_FAILURE_TYPES,
                                  runtime_parent_identities, validate_runtime_parent_results, validate_runtime_passage_schema)
from replay_retrieval import (FIELDS, checked_path, configure_isolation, digest_file,
                              isolated_artifact_path, read_jsonl, safe_member, verify_exports)

DATASET_NAMES = (*DATASETS, *BOUNDED_DATASETS)
CONDITIONS = ("guarded", "replay", "fresh-default", "passages")
PHASES = ("provision", "prepare", "guarded", "score-guarded", "replay", "fresh-default", "score-fresh-default",
          "passages", "score-passages")
C4_PHASES = ("c4-freeze", "c4-provision", "c4", "score-c4")
RUNTIME_PASSAGE_PHASES = ("runtime-passages", "score-runtime-passages")
RUNTIME_C4_PHASES = ("runtime-c4-freeze", "runtime-c4-provision", "runtime-c4", "score-runtime-c4")
HARNESS_FILES = ("production_retrieval.py", "replay_retrieval.py", "prepare_retrieval_ci.py",
                 "retrieval_ci.py", "passage_retrieval.py", "provision_production_model.py",
                 "production_runner.py", "production_ledger.py", "runtime_rerank_c4.py")
QUALITY_TARGETS = {"prospective": 0.95, "longtext-prospective": 5 / 6, "longmemeval": 5 / 6}
C4_RESOURCE_LIMITS = {"total_rss_mib": 768, "delta_rss_mib": 128, "delta_p95_ms": 50, "delta_p99_ms": 100, "delta_cpu_ms": 50}
C4_PAIRED_METRICS = ("hit_at_1", "hit_at_5", "hit_at_10", "mrr_at_10", "ndcg_at_10")
C4_HIT_COUNT_FIELDS = ("answered_queries", "baseline_hits", "candidate_hits", "lost_baseline_hits", "gained_hits")
FAILURE_TYPES = frozenset({"GateError", "ValueError", "TypeError", "KeyError", "AttributeError", "AssertionError",
                           "OSError", "FileNotFoundError", "PermissionError", "RuntimeError", "ImportError",
                           "ModuleNotFoundError", "TimeoutExpired", "KeyboardInterrupt", "SystemExit"})
COUNTER_KEYS = {
    "accepted", "rejected", "missing", "private", "quarantined", "body_changed", "revision_advanced", "neural_indexed",
    "faithful_public", "input_boundary_trimmed", "submitted", "unique_accepted_memories", "pending_embedding_jobs",
    "answered_queries", "any_gold_survived_queries", "all_gold_survived_queries", "gold_documents", "retained_gold_documents",
    "tests", "skipped", "failures", "errors", "real_recovery_case_passed", "submitted_passages", "source_submissions",
    "excluded_nonpublic_or_unfaithful_sources", "unchanged_short_parent_representations", "public_parent_memories",
    "planned_passages", "split_uncovered_nonspace_characters", "maximum_passage_tokens", "faithful_public_passages",
    "rejected_passages", "excluded_private_or_quarantined_passages", "body_or_identity_changed_passages",
    "parents_with_full_nonspace_coverage", "parents_with_partial_passage_coverage", "guarded_uncovered_nonspace_characters",
    "unique_public_child_memories", "identity_bound_indexed_child_memories",
    "eligible_original_parent_memories", "representation_bound_indexed_memories", "batch_limit", "maximum_batches",
    "completed_batches", "scheduled_jobs", "passage_raw_batch_encoding_calls", "passage_encoded_rows",
    "primary_document_inference_attempts",
}


class GateError(ValueError):
    pass


def failure_details(exc, stage):
    # Exception arguments and absolute filenames can contain paths or memory bodies.
    known_sources = {*HARNESS_FILES, "retrieval_c4.py", "retrieval_c4_model.py", "provision_retrieval_c4.py"}
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        name = Path(trace.tb_frame.f_code.co_filename).name
        if name in known_sources:
            frames.append({"source": name, "line": trace.tb_lineno})
        trace = trace.tb_next
    name = type(exc).__name__
    return {"stage": stage, "exception_type": name if name in FAILURE_TYPES else "other", "source_frames": frames[-4:]}


def worker_failure_projection(manifest, stage):
    if stage in {"c4", "runtime-c4"}:
        from retrieval_c4 import failure_projection

        return failure_projection(manifest, stage)
    details = manifest.get("failure_details", {}) if manifest.get("status") == "failed" else {}
    if not isinstance(details, dict):
        details = {}
    name = details.get("exception_type", manifest.get("failure_type"))
    frames = details.get("source_frames", [])
    selected = []
    if isinstance(frames, list):
        for frame in frames[-4:]:
            if (isinstance(frame, dict) and frame.get("source") in WORKER_FAILURE_SOURCES
                    and type(frame.get("line")) is int and frame["line"] > 0):
                selected.append({"source": frame["source"], "line": frame["line"]})
    return {"stage": stage, "exception_type": name if isinstance(name, str) and name in WORKER_FAILURE_TYPES else "other",
            "source_frames": selected}


def hashes(repository):
    return {path.relative_to(repository).as_posix(): digest_file(checked_path(path, root=repository, must_exist=True))
            for path in sorted((repository / "mesh").rglob("*.py"))}


def harness_hashes(repository):
    return {"benchmarks/" + name: digest_file(safe_member(repository, "benchmarks/" + name)) for name in HARNESS_FILES}


def load_json(root, name):
    return json.loads(safe_member(root, name).read_text(encoding="utf-8"))


def save(root, summary):
    # Rebuild summaries from fixed fields; never copy exception text or logs.
    path = safe_member(root, "job-summary.json", must_exist=False)
    pending = safe_member(root, "job-summary.json.pending", must_exist=False)
    with pending.open("w", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(summary, allow_nan=False, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    print(json.dumps({"artifact_temporary_file": pending.name, "destination": path.name}), flush=True)
    os.replace(pending, path)


def initial_summary(dataset, c4=False, runtime_passages=False):
    phases = list(PHASES)
    if runtime_passages:
        insertion = phases.index("score-fresh-default") + 1
        phases[insertion:insertion] = RUNTIME_PASSAGE_PHASES
        if c4:
            insertion = phases.index("score-runtime-passages") + 1
            phases[insertion:insertion] = RUNTIME_C4_PHASES
    phases.extend(C4_PHASES if c4 else ())
    return {"artifact_version": 1, "status": "not_started", "dataset": dataset,
            "conditions": {}, "quality_gates": [],
            "phases": [{"name": phase, "status": "not_run"} for phase in phases],
            "labels_in_ranking_inputs": False, "default_changed": False,
            "claims": {"full_longmemeval": False, "competitor_superiority": False, "quiet_performance": False},
            "optional_c4_cross_encoder": "requested_separate_experiment" if c4 else "not_run_separate_manual_experiment",
            "optional_runtime_passage_index": "requested_separate_experiment" if runtime_passages else "not_run_separate_manual_experiment",
            "optional_runtime_c4": "requested_actual_mesh_recall" if c4 and runtime_passages else "not_run_separate_manual_experiment"}


class TreeUsage:
    """Sum monotonic per-process CPU observations, including exited descendants."""
    def __init__(self, psutil, pid):
        self.psutil, self.parent = psutil, psutil.Process(pid)
        self.seen, self.processes = {}, {}

    def sample(self):
        rss = 0
        try:
            processes = [self.parent, *self.parent.children(recursive=True)]
        except self.psutil.NoSuchProcess:
            processes = list(self.processes.values())
        for process in processes:
            try:
                identity = (process.pid, process.create_time())
                cpu = process.cpu_times()
                rss += process.memory_info().rss
                self.seen[identity] = max(self.seen.get(identity, 0.0), cpu.user + cpu.system)
                self.processes[identity] = process
            except (self.psutil.NoSuchProcess, self.psutil.ZombieProcess):
                continue
        return rss, sum(self.seen.values())


def stop_tree(process, usage):
    targets = list(usage.processes.values()) if usage else []
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for target in reversed(targets):
        try:
            target.terminate()
        except (usage.psutil.NoSuchProcess, usage.psutil.ZombieProcess):
            pass
    if usage:
        _done, alive = usage.psutil.wait_procs(targets, timeout=2)
        for target in alive:
            try:
                target.kill()
            except (usage.psutil.NoSuchProcess, usage.psutil.ZombieProcess):
                pass
        usage.psutil.wait_procs(alive, timeout=2)
    if os.name == "nt" and process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        raise GateError("OwnedProcessDidNotStop") from None


def supervise(argv, *, repository, environment, deadline, rss_bytes, cpu_seconds,
              psutil=None, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep, stopper=stop_tree):
    if psutil is None:
        import psutil
    started = clock()
    if started >= deadline or cpu_seconds <= 0:
        return {"status": "failed", "reason": "wall_cap" if started >= deadline else "cpu_cap",
                "wall_seconds": 0.0, "sampled_tree_cpu_seconds": 0.0, "sampled_tree_peak_rss_bytes": 0}
    process, usage = None, None
    result = {"status": "running", "wall_seconds": 0.0,
              "sampled_tree_cpu_seconds": 0.0, "sampled_tree_peak_rss_bytes": 0}
    try:
        options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NO_WINDOW}
        process = popen(argv, cwd=repository, env=environment, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, **options)
        usage = TreeUsage(psutil, process.pid)
        while True:
            rss, cpu = usage.sample()
            result["sampled_tree_peak_rss_bytes"] = max(result["sampled_tree_peak_rss_bytes"], rss)
            result["sampled_tree_cpu_seconds"] = max(result["sampled_tree_cpu_seconds"], cpu)
            result["wall_seconds"] = max(0.0, clock() - started)
            bound = ("wall_cap" if clock() >= deadline else "rss_cap" if rss > rss_bytes
                     else "cpu_cap" if cpu > cpu_seconds else None)
            if bound:
                result.update(status="failed", reason=bound)
                break
            code = process.poll()
            if code is not None:
                result.update(status="passed" if code == 0 else "failed", returncode=int(code))
                break
            sleep(0.2)
    except BaseException:
        result.update(status="failed", reason="supervisor_error")
        raise
    finally:
        if process is not None:
            stopper(process, usage)
    return result


def numeric(value):
    if value is None or type(value) is bool:
        return value
    if type(value) not in {int, float} or not math.isfinite(value):
        raise GateError("UnsafeNumericArtifact")
    return value


def counters(value):
    if not isinstance(value, dict):
        raise GateError("InvalidCounterArtifact")
    result = {}
    for key, child in value.items():
        if not isinstance(key, str) or not (key in COUNTER_KEYS or key == "complete" or re.fullmatch(r"corpus-[0-9]{3}\.jsonl", key)):
            raise GateError("UnsafeCounterName")
        result[key] = counters(child) if isinstance(child, dict) else numeric(child)
    return result


def interval_projection(interval):
    if not isinstance(interval, dict) or type(interval.get("n")) is not int or interval["n"] < 0:
        raise GateError("InvalidMetricInterval")
    mean, bounds, count = interval.get("mean"), interval.get("ci95"), interval["n"]
    if count == 0:
        if mean is not None or bounds is not None:
            raise GateError("InvalidEmptyMetricInterval")
        return {"mean": None, "n": 0, "ci95": None}
    if (type(mean) not in {int, float} or not isinstance(bounds, list) or len(bounds) != 2
            or any(type(bound) not in {int, float} for bound in bounds)):
        raise GateError("InvalidMetricInterval")
    projected = {"mean": numeric(mean), "n": count, "ci95": [numeric(bound) for bound in bounds]}
    if projected["ci95"][0] > projected["ci95"][1]:
        raise GateError("InvalidMetricInterval")
    return projected


def metric_projection(cells):
    result = {}
    for cell, values in cells.items():
        if cell not in {"results-C64", "results-C96", "results-default"}:
            raise GateError("UnknownRankCell")
        selected = {}
        for field in ("final_sdk_top10", "raw_hybrid_top64", "dense_public_top64", "original_sdk_top10"):
            if field not in values:
                continue
            selected[field] = {}
            for cohort in ("unconditional", "any_gold_survived", "all_gold_survived", "retained_gold_denominator"):
                metrics = values[field][cohort]
                selected[field][cohort] = {}
                for name, interval in metrics.items():
                    if not re.fullmatch(r"(?:hit|document_recall|mrr|ndcg)_at_(?:1|5|10|64)", name):
                        raise GateError("UnknownMetric")
                    selected[field][cohort][name] = interval_projection(interval)
        if "gold_session_hits" in values:
            selected["gold_session_hits"] = metric_projection({cell: values["gold_session_hits"]})[cell]
        if "candidate_minus_original_sdk" in values:
            selected["candidate_minus_original_sdk"] = {
                metric: interval_projection(interval)
                for metric, interval in values["candidate_minus_original_sdk"].items() if metric in C4_PAIRED_METRICS}
        if "candidate_hit_at_1_regressions" in values:
            selected["candidate_hit_at_1_regressions"] = {
                key: numeric(value) for key, value in values["candidate_hit_at_1_regressions"].items() if key in C4_HIT_COUNT_FIELDS}
        result[cell] = selected
    return result


def qualify(repository, primary, floor):
    # The shared runner checks real crash-recovery cases and the complete suite.
    import production_runner as qualification
    current = hashes(repository) | {"pyproject.toml": digest_file(safe_member(repository, "pyproject.toml"))}
    primary = isolated_artifact_path(primary, repository, must_exist=True)
    floor = isolated_artifact_path(floor, repository, must_exist=True)
    source = load_json(primary, "source-runtime.json")
    floor_source = load_json(floor, "source-runtime.json")
    if source["runtime_sources"] != current or floor_source["runtime_sources"] != current:
        raise GateError("QualificationSourceMismatch")
    for relative, expected in source["harness_sources"].items():
        if digest_file(safe_member(repository, relative)) != expected:
            raise GateError("QualificationHarnessMismatch")
    runtime = qualification.runtime()
    if (source["runtime"]["python_version"] != platform.python_version()
            or source["runtime"]["packages"] != runtime["packages"]
            or source["runtime"]["mesh_version"] != runtime["mesh_version"]):
        raise GateError("QualificationDependencyMismatch")
    primary_tests = qualification.qualified_junit(safe_member(primary, "full.xml"), require_recovery=True)
    floor_tests = qualification.qualified_junit(safe_member(floor, "full.xml"))
    coverage = float(ET.parse(safe_member(primary, "coverage.xml")).getroot().get("line-rate", "nan"))
    if not math.isfinite(coverage) or coverage < 0.60 or load_json(primary, "package-final-validation.json")["status"] != "passed":
        raise GateError("QualificationCoverageOrPackageFailure")
    def test_projection(tests, directory):
        return counters({key: value for key, value in tests.items() if key != "sha256"}) | {"sha256": digest_file(safe_member(directory, "full.xml"))}

    return {"primary": test_projection(primary_tests, primary), "floor": test_projection(floor_tests, floor), "coverage": coverage,
            "source_runtime_sha256": digest_file(safe_member(primary, "source-runtime.json")),
            "constraints_sha256": digest_file(safe_member(primary, "constraints.txt")),
            "same_runtime_sources": True, "same_primary_python_and_packages": True}


def condition_evidence(directory, fixture, runtime_files, model, protocol_hash, *, scored, runtime_passages=False,
                       guarded_source=None):
    manifest = load_json(directory, "run-manifest.json")
    if manifest["status"] != "complete" or not manifest["all_queries_accounted"] or manifest["query_target"] != fixture["query_count"]:
        raise GateError("IncompleteCondition")
    if manifest["labels_in_ranking_inputs"] is not False or manifest["runtime_files"] != runtime_files:
        raise GateError("ConditionPrivacyOrRuntimeMismatch")
    verify_exports(directory, manifest)
    if any(not re.fullmatch(r"(?:results-(?:C64|C96|default)|ingestion-fidelity|query-vectors|passage-ingestion-fidelity|parent-passage-coverage|query-resources|runtime-reranker-resources)\.jsonl", name)
           for name in manifest["output_sha256"]):
        raise GateError("UnknownPublicExportName")
    result = {"status": "complete", "query_count": manifest["query_target"],
              "run_manifest_sha256": digest_file(safe_member(directory, "run-manifest.json")),
              "runtime_files": runtime_files, "output_sha256": manifest["output_sha256"]}
    if not scored:
        comparisons = manifest["rank_comparisons"]
        result["exact_rank_continuity"] = set(comparisons) == {"64", "96"} and all(
            comparisons[cap][field]["identical_ranked_queries"] == fixture["query_count"] for cap in ("64", "96") for field in FIELDS)
        return manifest, result
    model_files = manifest.get("model_file_sha256")
    if model_files is None and "passage_ingestion" in manifest and guarded_source is not None:
        # Older child-passage exports identify their guarded source instead of
        # repeating its local model hashes. Derive only from that verified source.
        guarded_source = checked_path(guarded_source, must_exist=True, directory=True)
        guarded = load_json(guarded_source, "run-manifest.json")
        verify_exports(guarded_source, guarded)
        guarded_hash = digest_file(safe_member(guarded_source, "run-manifest.json"))
        if (guarded.get("status") != "complete" or guarded.get("all_queries_accounted") is not True
                or manifest.get("source_run_manifest_sha256") != guarded_hash
                or manifest.get("source_output_sha256") != guarded["output_sha256"]
                or any(guarded.get(key) != manifest.get(key) for key in (
                    "runtime_files", "model_revision", "query_model_space_tag", "protocol_sha256", "dataset_manifest_sha256",
                ))):
            raise GateError("PassageGuardedModelProvenanceMismatch")
        model_files = guarded.get("model_file_sha256")
        result.update(model_file_provenance="hash_verified_guarded_source", paired_guarded_model_manifest_sha256=guarded_hash)
    if (manifest["model_revision"] != model["revision"] or manifest["query_model_space_tag"] != "onnx-minilm-l6-v2"
            or model_files != {key: model[key] for key in ("onnx_sha256", "tokenizer_sha256")}
            or manifest["protocol_sha256"] != protocol_hash):
        raise GateError("ConditionModelOrProtocolMismatch")
    score = load_json(directory, "score-summary.json")
    if score["run_manifest_sha256"] != result["run_manifest_sha256"] or score["query_count"] != fixture["query_count"]:
        raise GateError("ScoreProvenanceMismatch")
    records = list(read_jsonl(safe_member(directory, "ingestion-fidelity.jsonl")))
    if len(records) != fixture["corpus_count"]:
        raise GateError("IncompleteIngestionFidelity")
    faithful, public_parents, parent_states = {}, set(), {}
    nonpublic_parents = set()
    identities = runtime_parent_identities(records) if runtime_passages else None
    for record in records:
        identifier = record["acceptance"].get("memory_id")
        if identifier and (not record.get("current_incarnation") or not record.get("store_generation") or not record["current_revision"]):
            raise GateError("IncompleteSourceIdentity")
        if identifier:
            state = tuple(record.get(key) for key in (
                "current_revision", "current_incarnation", "store_generation", "stored_sha256",
                "private", "quarantined", "neural_indexed",
            ))
            if parent_states.setdefault(identifier, state) != state:
                raise GateError("InconsistentDuplicateSourceIdentity")
            if record["private"] or record["quarantined"]:
                nonpublic_parents.add(identifier)
            else:
                public_parents.add(identifier)
        if record["faithful_public"]:
            if record["private"] or record["quarantined"] or record["body_changed"]:
                raise GateError("InvalidPublicFidelity")
            faithful.setdefault(identifier, []).append(record["document_id"])
    unfaithful_public_candidates = 0
    for relative in manifest["output_sha256"]:
        if not relative.startswith("results-"):
            continue
        rows = list(read_jsonl(safe_member(directory, relative)))
        if len(rows) != fixture["query_count"] or len({row["query_id"] for row in rows}) != len(rows):
            raise GateError("IncompleteRanks")
        for row in rows:
            for field in (*FIELDS, "original_sdk_top10"):
                if field not in row:
                    continue
                candidates = row[field]
                if runtime_passages and field in {"final_sdk_top10", "original_sdk_top10"}:
                    validate_runtime_parent_results(candidates, identities, allow_passages=field == "final_sdk_top10")
                limit = 24 if field == "authorized_sdk_rerank_top24" else 10 if field.endswith("top10") else 64
                if len(candidates) > limit or len({candidate["id"] for candidate in candidates}) != len(candidates):
                    raise GateError("UnboundedOrDuplicateParents")
                for candidate in candidates:
                    expected_ids = faithful.get(candidate["id"], [])
                    actual_ids = candidate.get("faithful_document_ids")
                    if (candidate.get("local_only") or candidate.get("quarantined")
                            or candidate["id"] not in public_parents or candidate["id"] in nonpublic_parents
                            or not isinstance(actual_ids, list) or actual_ids != expected_ids):
                        raise GateError("UnfaithfulOrPrivateExport")
                    # Guarded public text can differ from its original source.
                    # Empty faithful IDs keep it visible without relevance credit.
                    unfaithful_public_candidates += int(not expected_ids)
    result.update(metrics=metric_projection(score["cells"]), gold_survival=counters(score["gold_survival"]),
                  ingestion=counters(score["ingestion"]), model_revision=model["revision"], model_file_sha256=model_files,
                  protocol_sha256=protocol_hash, dataset_manifest_sha256=manifest["dataset_manifest_sha256"],
                  score_summary_sha256=digest_file(safe_member(directory, "score-summary.json")))
    result["unfaithful_public_exported_candidates"] = unfaithful_public_candidates
    if "passage_ingestion" in manifest:
        result["passage_ingestion"] = counters(manifest["passage_ingestion"])
        # The passage harness records the requested budget with its fixed parameters.
        parameters = manifest.get("fixed_parameters", {})
        if not isinstance(parameters, dict) or type(parameters.get("requested_parent_budget")) is not int:
            raise GateError("UndeclaredParentBudget")
        if parameters["requested_parent_budget"] != 96:
            raise GateError("UndeclaredParentBudget")
        result["parent_budget"] = 96
        result["parent_caps"] = [64, 96]
        result["privacy_and_canonical_source_ids_verified"] = True
    if runtime_passages:
        result.update(runtime_passage_evidence(directory, manifest, records))
    return manifest, result


def runtime_resource_projection(value):
    keys = {"wall_seconds", "process_cpu_seconds", "sampled_owned_process_rss_bytes_max"}
    if (not isinstance(value, dict) or set(value) != keys
            or any(type(value[key]) not in {int, float} or not math.isfinite(value[key]) or value[key] < 0 for key in keys)
            or value["sampled_owned_process_rss_bytes_max"] <= 0):
        raise GateError("RuntimePassageResourceAccountingMissing")
    return {key: numeric(value[key]) for key in sorted(keys)}


def runtime_passage_evidence(directory, manifest, records):
    schema_version = validate_runtime_passage_schema(manifest.get("schema_version"))
    configuration = manifest.get("runtime_passage_index", {})
    if (manifest.get("passage_index_enabled") is not True
            or configuration.get("representation") != PASSAGE_REPRESENTATION
            or configuration.get("backend") != "direct-onnx" or configuration.get("manual_worker") is not True
            or manifest.get("guarded_source_immutable") is not True):
        raise GateError("RuntimePassageConditionOrSourceMismatch")
    expected = {}
    for record in records:
        expected.setdefault(record["corpus_id"], set())
        if record["acceptance"].get("memory_id"):
            expected[record["corpus_id"]].add(record["acceptance"]["memory_id"])
    readiness, fingerprints = manifest.get("runtime_passage_readiness", {}), manifest.get("guarded_source_database_fingerprints", {})
    if set(readiness) != set(expected) or set(fingerprints) != set(expected):
        raise GateError("RuntimePassageCorpusAccountingMismatch")
    projected = {}
    for corpus, identifiers in expected.items():
        item, count = readiness[corpus], len(identifiers)
        required = {"eligible_original_parent_memories": count, "representation_bound_indexed_memories": count,
                    "pending_embedding_jobs": 0, "primary_document_inference_attempts": 0,
                    "batch_limit": PASSAGE_BATCH_LIMIT, "maximum_batches": max(3, count * 3)}
        integer_fields = (*required, "completed_batches", "scheduled_jobs", "passage_raw_batch_encoding_calls", "passage_encoded_rows")
        if (item.get("status") != "indexed" or item.get("target_model") != MODEL_TAG
                or item.get("target_representation") != PASSAGE_REPRESENTATION
                or any(type(item.get(key)) is not int or item[key] < 0 for key in integer_fields)
                or any(item.get(key) != value for key, value in required.items())
                or item["completed_batches"] > item["maximum_batches"] or item["scheduled_jobs"] > count):
            raise GateError("RuntimePassageOriginalParentReadinessFailed")
        fingerprint = fingerprints[corpus]
        if (set(fingerprint) != {"database_sha256", "wal_sha256"}
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for key, value in fingerprint.items() if key != "wal_sha256" or value is not None)):
            raise GateError("RuntimePassageSourceFingerprintMissing")
        projected[corpus] = counters({key: item[key] for key in integer_fields}) | {
            "target_representation": PASSAGE_REPRESENTATION, "target_model": MODEL_TAG,
            "resource_diagnostics": runtime_resource_projection(item.get("resource_diagnostics"))}
    observations = manifest.get("runtime_passage_query_backend_observations", {})
    if (observations.get("normal_sdk_query_encoding_calls") != manifest["query_target"]
            or observations.get("all_query_model_tags_match_indexed_model") is not True
            or observations.get("all_query_dimensions_384") is not True or observations.get("document_inference_attempts") != 0):
        raise GateError("RuntimePassageFreshQueryInferenceAccountingFailed")
    if "query-resources.jsonl" not in manifest["output_sha256"]:
        raise GateError("RuntimePassageQueryResourcesMissing")
    resources = list(read_jsonl(safe_member(directory, "query-resources.jsonl")))
    if len(resources) != manifest["query_target"]:
        raise GateError("RuntimePassageQueryResourcesIncomplete")
    for ordinal, row in enumerate(resources, 1):
        if (set(row) != {"query_ordinal", "elapsed_ms", "process_cpu_ms", "sampled_owned_process_rss_bytes"}
                or type(row["query_ordinal"]) is not int or row["query_ordinal"] != ordinal
                or any(type(row[key]) not in {int, float} or not math.isfinite(row[key]) or row[key] < 0
                       for key in ("elapsed_ms", "process_cpu_ms", "sampled_owned_process_rss_bytes"))
                or row["sampled_owned_process_rss_bytes"] <= 0):
            raise GateError("RuntimePassageQueryResourcesInvalid")
    return {"schema_version": schema_version,
            "runtime_passage_index": {"representation": PASSAGE_REPRESENTATION, "backend": "direct-onnx", "manual_worker": True},
            "runtime_passage_readiness": projected, "guarded_source_immutable": True,
            "guarded_source_database_fingerprints": fingerprints,
            "runtime_passage_resource_diagnostics": runtime_resource_projection(manifest.get("runtime_passage_resource_diagnostics")),
            "runtime_passage_query_resources": {"query_count": len(resources),
                "elapsed_ms_total": sum(row["elapsed_ms"] for row in resources),
                "elapsed_ms_max": max(row["elapsed_ms"] for row in resources),
                "process_cpu_ms_total": sum(row["process_cpu_ms"] for row in resources),
                "sampled_owned_process_rss_bytes_max": max(row["sampled_owned_process_rss_bytes"] for row in resources)},
            "original_parent_identity_model_and_supporting_spans_verified": True}


def runtime_passage_pairing(root, manifests, conditions):
    experiment, baseline = manifests["runtime-passages"], manifests["fresh-default"]
    pairing = experiment["runtime_passage_index"]
    if (baseline.get("passage_index_enabled") is not False
            or pairing.get("fresh_default_run_manifest_sha256") != conditions["fresh-default"]["run_manifest_sha256"]
            or pairing.get("fresh_default_results_sha256") != baseline["output_sha256"]["results-default.jsonl"]
            or experiment["source_fidelity_sha256"] != baseline["output_sha256"]["ingestion-fidelity.jsonl"]):
        raise GateError("RuntimePassageFreshDefaultPairMismatch")
    original = {row["query_id"]: row for row in read_jsonl(safe_member(root / "fresh-default", "results-default.jsonl"))}
    candidate = list(read_jsonl(safe_member(root / "runtime-passages", "results-default.jsonl")))
    if len(candidate) != len(original) or {row["query_id"] for row in candidate} != set(original):
        raise GateError("RuntimePassagePairedQueryAccountingMismatch")
    for row in candidate:
        matching = original[row["query_id"]]
        if (row.get("original_sdk_top10") != matching["final_sdk_top10"] or row["corpus_id"] != matching["corpus_id"]
                or row.get("candidate_cap") is not None or row["query_embedding_model"] != MODEL_TAG):
            raise GateError("RuntimePassageOriginalRanksChanged")
    conditions["runtime-passages"]["paired_fresh_default_run_manifest_sha256"] = pairing["fresh_default_run_manifest_sha256"]
    conditions["runtime-passages"]["paired_fresh_default_results_sha256"] = pairing["fresh_default_results_sha256"]
    conditions["runtime-passages"]["original_fresh_default_ranks_verified"] = True


def runtime_passage_quality_gates(dataset, condition, baseline):
    cells, matching = condition["metrics"], baseline["metrics"]
    answered = condition["gold_survival"]["answered_queries"]
    if (set(cells) != {"results-default"} or set(matching) != {"results-default"}
            or type(answered) is not int or answered <= 0 or baseline["gold_survival"]["answered_queries"] != answered):
        raise GateError("RuntimePassageScoredPairIncomplete")
    cell = cells["results-default"]
    candidate, original = cell["final_sdk_top10"]["unconditional"], cell["original_sdk_top10"]["unconditional"]
    fresh = matching["results-default"]["final_sdk_top10"]["unconditional"]
    gates = [{"name": "runtime_passages_original_parent_readiness_and_resource_accounting", "passed": True}]
    for metric in (*C4_PAIRED_METRICS, "document_recall_at_10"):
        items = (candidate[metric], original[metric], fresh[metric])
        valid = all(item["n"] == answered and type(item["mean"]) in {int, float}
                    and math.isfinite(item["mean"]) and 0 <= item["mean"] <= 1 for item in items)
        valid = valid and math.isclose(original[metric]["mean"], fresh[metric]["mean"], rel_tol=0, abs_tol=1e-12)
        if metric in C4_PAIRED_METRICS:
            paired = cell["candidate_minus_original_sdk"][metric]
            valid = (valid and paired["n"] == answered and type(paired["mean"]) in {int, float}
                     and math.isfinite(paired["mean"]) and math.isclose(
                         paired["mean"], candidate[metric]["mean"] - original[metric]["mean"], rel_tol=0, abs_tol=1e-12))
        gates.append({"name": "runtime_passages_paired_accounting_" + metric, "passed": valid})
    counts = cell["candidate_hit_at_1_regressions"]
    balanced = all(type(counts.get(key)) is int and 0 <= counts[key] <= answered for key in C4_HIT_COUNT_FIELDS)
    if balanced:
        lost, gained = counts["lost_baseline_hits"], counts["gained_hits"]
        balanced = (counts["answered_queries"] == answered and lost + gained <= answered
                    and lost <= counts["baseline_hits"] and gained <= answered - counts["baseline_hits"]
                    and counts["candidate_hits"] == counts["baseline_hits"] - lost + gained
                    and math.isclose(original["hit_at_1"]["mean"], counts["baseline_hits"] / answered, rel_tol=0, abs_tol=1e-12)
                    and math.isclose(candidate["hit_at_1"]["mean"], counts["candidate_hits"] / answered, rel_tol=0, abs_tol=1e-12))
    gates.append({"name": "runtime_passages_paired_hit_at_1_count_accounting", "passed": balanced})
    if dataset in QUALITY_TARGETS:
        target, observed = QUALITY_TARGETS[dataset], candidate["hit_at_1"]["mean"]
        gates.extend([{"name": "runtime_passages_exact_evidence_hit_at_1", "passed": observed >= target,
                       "observed": observed, "minimum": target},
                      {"name": "runtime_passages_no_new_exact_evidence_hit_at_1_regressions",
                       "passed": balanced and counts["lost_baseline_hits"] == 0, "maximum_regressions": 0,
                       "observed_regressions": counts.get("lost_baseline_hits")}])
    else:
        for metric in ("ndcg_at_10", "document_recall_at_10"):
            delta = candidate[metric]["mean"] - original[metric]["mean"]
            gates.append({"name": "runtime_passages_paired_nonregression_" + metric,
                          "passed": delta >= -0.01 - 1e-12, "observed_delta": delta, "minimum_delta": -0.01})
    return gates


def quality_gates(dataset, conditions):
    gates = [{"name": "saved_vector_exact_rank_continuity", "passed": conditions["replay"]["exact_rank_continuity"]}]
    for cell, metrics in conditions["passages"]["metrics"].items():
        candidate = metrics["final_sdk_top10"]["unconditional"]
        original = metrics["original_sdk_top10"]["unconditional"]
        if dataset in QUALITY_TARGETS:
            target = QUALITY_TARGETS[dataset]
            gates.append({"name": "passage_exact_evidence_hit_at_1_" + cell,
                          "passed": candidate["hit_at_1"]["mean"] >= target,
                          "observed": candidate["hit_at_1"]["mean"], "minimum": target})
        else:
            for metric in ("ndcg_at_10", "document_recall_at_10"):
                difference = candidate[metric]["mean"] - original[metric]["mean"]
                gates.append({"name": "paired_passage_nonregression_" + cell + "_" + metric,
                              "passed": difference >= -0.01 - 1e-12, "observed_delta": difference, "minimum_delta": -0.01})
    if dataset in QUALITY_TARGETS:
        observed = conditions["fresh-default"]["metrics"]["results-default"]["final_sdk_top10"]["unconditional"]["hit_at_1"]["mean"]
        gates.append({"name": "fresh_default_exact_evidence_hit_at_1", "passed": observed >= QUALITY_TARGETS[dataset],
                      "observed": observed, "minimum": QUALITY_TARGETS[dataset]})
    return gates


def c4_resource_gates(manifest, *, prefix="c4"):
    """Missing measurements remain visibly unqualified; reported booleans cannot waive limits."""
    reported = manifest.get("resource_gates", {})
    result = []
    for name, maximum in C4_RESOURCE_LIMITS.items():
        entry = reported.get(name, {})
        observed = numeric(entry.get("observed"))
        qualified = (type(observed) in {int, float} and entry.get("maximum") == maximum
                     and observed <= maximum and (name != "total_rss_mib" or observed > 0))
        result.append({"name": prefix + "_" + name, "passed": qualified, "observed": observed, "maximum": maximum})
    return result


def c4_quality_gates(dataset, condition, matching_passages, *, cells_required=("results-C64", "results-C96"), prefix="c4"):
    """C4's zero-loss policy uses every answered query and its exact passage baseline."""
    def mapping(value):
        return value if isinstance(value, dict) else {}

    def finite(value, *, signed=False):
        if type(value) not in {int, float} or not math.isfinite(value):
            return None
        return value if (-1 if signed else 0) <= value <= 1 else None

    def interval(container, metric, expected, *, signed=False):
        item = mapping(mapping(container).get(metric))
        mean = finite(item.get("mean"), signed=signed)
        bounds = item.get("ci95")
        valid_bounds = isinstance(bounds, list) and len(bounds) == 2 and all(finite(bound, signed=signed) is not None for bound in bounds)
        valid = (mean is not None and type(item.get("n")) is int and item["n"] == expected
                 and valid_bounds and bounds[0] <= bounds[1])
        return mean, valid

    condition, matching_passages = mapping(condition), mapping(matching_passages)
    cells, matching = mapping(condition.get("metrics")), mapping(matching_passages.get("metrics"))
    answered = mapping(condition.get("gold_survival")).get("answered_queries")
    matching_answered = mapping(matching_passages.get("gold_survival")).get("answered_queries")
    denominator_valid = type(answered) is int and answered > 0 and type(matching_answered) is int and matching_answered == answered
    complete = set(cells) == set(cells_required) and set(matching) == set(cells_required)
    gates = [{"name": prefix + "_paired_cells_and_answered_denominator", "passed": complete and denominator_valid}]
    for cell in cells_required:
        report = mapping(cells.get(cell))
        original = mapping(mapping(report.get("original_sdk_top10")).get("unconditional"))
        candidate = mapping(mapping(report.get("final_sdk_top10")).get("unconditional"))
        baseline = mapping(mapping(mapping(matching.get(cell)).get("final_sdk_top10")).get("unconditional"))
        paired = mapping(report.get("candidate_minus_original_sdk"))
        for metric in C4_PAIRED_METRICS:
            difference, paired_valid = interval(paired, metric, answered, signed=True)
            before, original_valid = interval(original, metric, answered)
            after, candidate_valid = interval(candidate, metric, answered)
            matched, baseline_valid = interval(baseline, metric, answered)
            provenance = (denominator_valid and paired_valid and original_valid and candidate_valid and baseline_valid
                          and math.isclose(before, matched, rel_tol=0, abs_tol=1e-12)
                          and math.isclose(difference, after - before, rel_tol=0, abs_tol=1e-12))
            gates.append({"name": prefix + "_paired_zero_loss_" + cell + "_" + metric,
                          "passed": provenance and difference >= 0, "observed_delta": difference, "minimum_delta": 0,
                          "paired_accounting_verified": provenance})
        counts = mapping(report.get("candidate_hit_at_1_regressions"))
        balanced = denominator_valid and all(type(counts.get(key)) is int and 0 <= counts[key] <= answered for key in C4_HIT_COUNT_FIELDS)
        if balanced:
            lost, gained = counts["lost_baseline_hits"], counts["gained_hits"]
            before, before_valid = interval(original, "hit_at_1", answered)
            after, after_valid = interval(candidate, "hit_at_1", answered)
            balanced = (counts["answered_queries"] == answered and lost + gained <= answered
                        and lost <= counts["baseline_hits"] and gained <= answered - counts["baseline_hits"]
                        and counts["candidate_hits"] == counts["baseline_hits"] - lost + gained
                        and before_valid and after_valid
                        and math.isclose(before, counts["baseline_hits"] / answered, rel_tol=0, abs_tol=1e-12)
                        and math.isclose(after, counts["candidate_hits"] / answered, rel_tol=0, abs_tol=1e-12))
        gates.append({"name": prefix + "_paired_hit_at_1_count_accounting_" + cell, "passed": balanced})
        if dataset in QUALITY_TARGETS:
            lost = counts.get("lost_baseline_hits")
            gates.append({"name": prefix + "_no_new_exact_evidence_hit_at_1_regressions_" + cell,
                          "passed": balanced and lost == 0,
                          "observed_regressions": lost if type(lost) is int else None, "maximum_regressions": 0})
    return gates


def runtime_c4_evidence(directory, manifest):
    from retrieval_c4_model import LIMITS, MODEL
    from runtime_rerank_c4 import POLICY

    configuration = manifest.get("runtime_reranker", {})
    counts = configuration.get("native_counts", {})
    target = manifest["query_target"]
    passes = max(1, math.ceil(LIMITS["minimum_resource_samples_per_cell"] / target)) if type(target) is int and target > 0 else None
    if (configuration.get("configuration") != "c4-minilm-runtime-recall" or configuration.get("phase") != "c4"
            or configuration.get("policy") != POLICY or configuration.get("model") != MODEL
            or configuration.get("limits") != LIMITS or type(configuration.get("resource_passes")) is not int
            or configuration.get("resource_passes") != passes
            or configuration.get("parent_identity_scores_spans_and_tail_verified") is not True
            or configuration.get("top_one_matches_top_ten") is not True
            or configuration.get("labels_in_ranking_inputs") is not False or not isinstance(counts, dict)):
        raise GateError("RuntimeC4NativeConfigurationMismatch")
    integer_keys = ("quality_native_calls", "top_one_native_calls", "resource_repeat_native_calls", "warmup_native_calls",
                    "session_load_attempts", "session_load_count", "inference_attempted_batches", "inference_attempted_pairs",
                    "inference_returned_batches", "inference_returned_pairs", "document_inference_attempts")
    if (set(counts) - {*integer_keys, "cold_load_ms"}
            or any(type(counts.get(key, 0)) is not int or counts.get(key, 0) < 0 for key in integer_keys)
            or counts.get("quality_native_calls") != target or counts.get("top_one_native_calls") != target
            or counts.get("resource_repeat_native_calls", 0) != target * (passes - 1)
            or not 0 < counts.get("warmup_native_calls", 0) <= target
            or counts.get("session_load_count") != 1 or counts.get("session_load_attempts") != 1
            or counts.get("document_inference_attempts", 0) != 0
            or type(counts.get("cold_load_ms")) not in {int, float} or not math.isfinite(counts["cold_load_ms"]) or counts["cold_load_ms"] < 0):
        raise GateError("RuntimeC4NativeCallAccountingMismatch")
    calls = sum(counts.get(key, 0) for key in integer_keys[:4])
    batches, pairs = counts.get("inference_returned_batches", 0), counts.get("inference_returned_pairs", 0)
    if (not 0 < batches <= calls or not batches * 2 <= pairs <= batches * LIMITS["parent_cap"]
            or counts.get("inference_attempted_batches") != batches or counts.get("inference_attempted_pairs") != pairs):
        raise GateError("RuntimeC4NativeInferenceAccountingMismatch")
    samples = list(read_jsonl(safe_member(directory, "runtime-reranker-resources.jsonl")))
    expected_queries = {row["query_id"] for row in read_jsonl(safe_member(directory, "results-default.jsonl"))}
    expected = {(query_id, resource_pass) for query_id in expected_queries for resource_pass in range(passes)}
    actual = set()
    for row in samples:
        if (set(row) != {"query_id", "resource_pass", "wall_ms", "process_cpu_ms", "quality_export"}
                or type(row.get("query_id")) is not str or type(row.get("resource_pass")) is not int
                or type(row.get("quality_export")) is not bool or row["quality_export"] != (row["resource_pass"] == 0)
                or any(type(row.get(key)) not in {int, float} or not math.isfinite(row[key]) or row[key] < 0
                       for key in ("wall_ms", "process_cpu_ms"))):
            raise GateError("RuntimeC4ResourceSamplesInvalid")
        actual.add((row["query_id"], row["resource_pass"]))
    if (len(expected_queries) != target or len(actual) != len(samples) or actual != expected
            or len(samples) < LIMITS["minimum_resource_samples_per_cell"]):
        raise GateError("RuntimeC4ResourceSamplesIncomplete")
    return {"configuration": "c4-minilm-runtime-recall", "model": MODEL, "limits": LIMITS,
            "resource_passes": passes, "resource_sample_count": len(samples),
            "native_counts": {key: numeric(value) for key, value in counts.items()},
            "parent_identity_scores_spans_and_tail_verified": True, "top_one_matches_top_ten": True}


def runtime_c4_pairing(root, manifests, conditions):
    experiment, baseline = manifests["runtime-c4"], manifests["runtime-passages"]
    if (experiment.get("paired_original_condition") != "runtime_passages"
            or experiment.get("runtime_passage_run_manifest_sha256") != conditions["runtime-passages"]["run_manifest_sha256"]
            or experiment.get("runtime_passage_results_sha256") != baseline["output_sha256"]["results-default.jsonl"]
            or experiment.get("source_run_manifest_sha256") != conditions["guarded"]["run_manifest_sha256"]):
        raise GateError("RuntimeC4PairSourceMismatch")
    original = list(read_jsonl(safe_member(root / "runtime-passages", "results-default.jsonl")))
    candidate = list(read_jsonl(safe_member(root / "runtime-c4", "results-default.jsonl")))
    if len(original) != len(candidate):
        raise GateError("RuntimeC4PairQueryCountMismatch")
    for before, after in zip(original, candidate):
        if (any(before.get(key) != after.get(key) for key in ("query_id", "corpus_id", "candidate_cap", "query_embedding_model"))
                or after.get("original_sdk_top10") != before["final_sdk_top10"]):
            raise GateError("RuntimeC4PairRankMismatch")
    conditions["runtime-c4"]["runtime_reranker"] = runtime_c4_evidence(root / "runtime-c4", experiment)
    conditions["runtime-c4"]["original_runtime_passage_ranks_verified"] = True


def runtime_c4_quality_gates(dataset, condition, baseline):
    gates = c4_quality_gates(dataset, condition, baseline, cells_required=("results-default",), prefix="runtime_c4")
    if dataset in QUALITY_TARGETS:
        observed = condition.get("metrics", {}).get("results-default", {}).get("final_sdk_top10", {}).get("unconditional", {}).get("hit_at_1", {}).get("mean")
        valid = type(observed) in {int, float} and math.isfinite(observed)
        gates.append({"name": "runtime_c4_exact_evidence_hit_at_1", "passed": valid and observed >= QUALITY_TARGETS[dataset],
                      "observed": observed if valid else None, "minimum": QUALITY_TARGETS[dataset]})
    return gates


def run(args, repository, root):
    c4 = getattr(args, "reranker_experiment", "off") == "c4"
    runtime_passages = getattr(args, "runtime_passages", False)
    summary = initial_summary(args.dataset, c4, runtime_passages)
    summary["status"] = "running"
    save(root, summary)
    stage = "qualification"
    try:
        runtime_files, harness = hashes(repository), harness_hashes(repository)
        summary.update(runtime_files=runtime_files, harness_files=harness)
        summary["qualification"] = qualify(repository, args.qualification_directory, args.floor_qualification_directory)
        summary["resource_limits"] = {"whole_run_wall_seconds": args.wall_seconds, "sampled_tree_rss_bytes": args.rss_mib * 1024**2,
                                      "sampled_accumulated_tree_cpu_seconds": args.cpu_seconds, "sample_period_seconds": 0.2}
        stage = "commands"
        models = isolated_artifact_path(args.model_directory, repository)
        data = root / "data" / args.dataset
        protocol = root / "protocol.json"
        python = sys.executable
        production = str(safe_member(repository, "benchmarks/production_retrieval.py"))
        common = ["--repository", str(repository), "--dataset-directory", str(data), "--protocol", str(protocol), "--model-directory", str(models)]
        commands = {
            "provision": [python, str(safe_member(repository, "benchmarks/provision_production_model.py")), "--directory", str(models)],
            "prepare": [python, str(safe_member(repository, "benchmarks/prepare_retrieval_ci.py")), args.dataset,
                        "--repository", str(repository), "--output-root", str(root), "--model-directory", str(models)],
            "guarded": [python, production, "regression", args.dataset, *common, "--retain-guarded-home", "--output", str(root / "guarded")],
            "replay": [python, str(safe_member(repository, "benchmarks/replay_retrieval.py")), str(root / "guarded"),
                       "--repository", str(repository), "--output", str(root / "replay")],
            "fresh-default": [python, production, "query-only", args.dataset, *common, "--source-run", str(root / "guarded"),
                              "--source-namespace", "bounded-" + args.dataset + "-000", "--output", str(root / "fresh-default")],
            "passages": [python, str(safe_member(repository, "benchmarks/passage_retrieval.py")), *common,
                         "--source-run", str(root / "guarded"), "--parent-budget", "96", "--output", str(root / "passages")],
        }
        commands.update({"score-" + condition: [python, production, "score", str(root / condition), "--dataset-directory", str(data)]
                         for condition in ("guarded", "fresh-default", "passages")})
        if runtime_passages:
            commands.update({
                "runtime-passages": [python, production, "query-only", args.dataset, *common,
                                     "--source-run", str(root / "guarded"), "--source-namespace", "bounded-" + args.dataset + "-000",
                                     "--runtime-passages", "--fresh-default-run", str(root / "fresh-default"), "--output", str(root / "runtime-passages")],
                "score-runtime-passages": [python, production, "score", str(root / "runtime-passages"), "--dataset-directory", str(data)],
            })
        if c4:
            reranker = str(repository / "benchmarks/retrieval_c4.py")
            c4_models = models.parent / "c4-minilm-ce"
            c4_protocol = root / "c4-protocol.json"
            commands.update({
                "c4-freeze": [python, reranker, "freeze", "--repository", str(repository), "--base-protocol", str(protocol), "--output", str(c4_protocol)],
                "c4-provision": [python, str(repository / "benchmarks/provision_retrieval_c4.py"), "--repository", str(repository), "--directory", str(c4_models)],
                "c4": [python, reranker, "run", "--repository", str(repository), "--source-run", str(root / "guarded"),
                       "--passage-run", str(root / "passages"), "--dataset-directory", str(data), "--base-protocol", str(protocol),
                       "--protocol", str(c4_protocol), "--model-directory", str(c4_models), "--embedding-model-directory", str(models), "--output", str(root / "c4")],
                "score-c4": [python, production, "score", str(root / "c4"), "--dataset-directory", str(data)],
            })
            if runtime_passages:
                runtime_reranker = str(repository / "benchmarks/runtime_rerank_c4.py")
                runtime_protocol = root / "runtime-c4-protocol.json"
                commands.update({
                    "runtime-c4-freeze": [python, runtime_reranker, "freeze", "--repository", str(repository),
                                          "--base-protocol", str(protocol), "--output", str(runtime_protocol)],
                    "runtime-c4-provision": [python, str(repository / "benchmarks/provision_retrieval_c4.py"),
                                             "--repository", str(repository), "--directory", str(c4_models)],
                    "runtime-c4": [python, runtime_reranker, "run", args.dataset, "--repository", str(repository),
                                   "--source-run", str(root / "guarded"), "--runtime-passage-run", str(root / "runtime-passages"),
                                   "--fresh-default-run", str(root / "fresh-default"), "--dataset-directory", str(data),
                                   "--base-protocol", str(protocol), "--protocol", str(runtime_protocol),
                                   "--model-directory", str(c4_models), "--embedding-model-directory", str(models),
                                   "--output", str(root / "runtime-c4")],
                    "score-runtime-c4": [python, production, "score", str(root / "runtime-c4"), "--dataset-directory", str(data)],
                })
        deadline, used_cpu = time.monotonic() + args.wall_seconds, 0.0
        manifests = {}
        stage = "isolation"
        with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-supervisor-") as owned:
            # Only this factory-owned path may resolve system temp aliases.
            supervisor_root = Path(owned).resolve(strict=True)
            configure_isolation(supervisor_root)
            environment = dict(os.environ)
            environment.update(TMPDIR=str(supervisor_root), TMP=str(supervisor_root), TEMP=str(supervisor_root), TOKENIZERS_PARALLELISM="false",
                               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1")
            for phase in summary["phases"]:
                stage = phase["name"]
                phase["status"] = "running"
                save(root, summary)
                if phase["name"] in C4_PHASES:
                    for relative in ("benchmarks/retrieval_c4.py", "benchmarks/retrieval_c4_model.py", "benchmarks/provision_retrieval_c4.py"):
                        actual = digest_file(safe_member(repository, relative))
                        previous = summary.setdefault("optional_harness_files", {}).setdefault(relative, actual)
                        if actual != previous:
                            raise GateError("OptionalSourceChanged")
                observed = supervise(commands[phase["name"]], repository=repository, environment=environment, deadline=deadline,
                                     rss_bytes=args.rss_mib * 1024**2, cpu_seconds=args.cpu_seconds - used_cpu)
                phase.update(observed)
                used_cpu += observed["sampled_tree_cpu_seconds"]
                save(root, summary)
                if observed["status"] != "passed":
                    if phase["name"] in {"guarded", "fresh-default", "passages", "runtime-passages", "c4", "runtime-c4"}:
                        try:
                            partial = load_json(root / phase["name"], "run-manifest.json")
                            summary["failed_worker_diagnostics"] = worker_failure_projection(partial, phase["name"])
                        except (ValueError, OSError, KeyError, TypeError):
                            pass
                    if phase["name"] in RUNTIME_PASSAGE_PHASES:
                        summary["optional_runtime_passage_index"] = "failed_or_unavailable"
                        try:
                            partial = load_json(root / "runtime-passages", "run-manifest.json")
                            summary["runtime_passage_failed_resource_diagnostics"] = runtime_resource_projection(
                                partial.get("runtime_passage_resource_diagnostics"))
                        except (ValueError, OSError, KeyError, TypeError):
                            pass
                    if phase["name"] in C4_PHASES:
                        summary["optional_c4_cross_encoder"] = "failed_or_unavailable"
                        try:
                            partial = load_json(root / "c4", "run-manifest.json")
                            summary["quality_gates"].extend(c4_resource_gates(partial))
                        except (ValueError, OSError, KeyError, TypeError):
                            pass
                    if phase["name"] in RUNTIME_C4_PHASES:
                        summary["optional_runtime_c4"] = "failed_or_unavailable"
                        try:
                            partial = load_json(root / "runtime-c4", "run-manifest.json")
                            summary["quality_gates"].extend(c4_resource_gates(partial, prefix="runtime_c4"))
                        except (ValueError, OSError, KeyError, TypeError):
                            pass
                    raise GateError("PhaseFailed")
                condition = "replay" if phase["name"] == "replay" else phase["name"][6:] if phase["name"].startswith("score-") else None
                if condition:
                    fixture = load_json(data, "manifest.json")
                    protocol_hash = digest_file(safe_member(root, "protocol.json"))
                    manifests[condition], summary["conditions"][condition] = condition_evidence(
                        root / condition, fixture, runtime_files, MODEL, protocol_hash, scored=condition != "replay",
                        runtime_passages=condition in {"runtime-passages", "runtime-c4"},
                        guarded_source=root / "guarded" if condition == "passages" else None)
                    if condition == "passages":
                        summary["quality_gates"] = quality_gates(args.dataset, summary["conditions"])
                        if "runtime-passages" in summary["conditions"]:
                            summary["quality_gates"].extend(runtime_passage_quality_gates(
                                args.dataset, summary["conditions"]["runtime-passages"], summary["conditions"]["fresh-default"]))
                        if "runtime-c4" in summary["conditions"]:
                            summary["quality_gates"].extend(c4_resource_gates(manifests["runtime-c4"], prefix="runtime_c4"))
                            summary["quality_gates"].extend(runtime_c4_quality_gates(
                                args.dataset, summary["conditions"]["runtime-c4"], summary["conditions"]["runtime-passages"]))
                    if condition == "runtime-passages":
                        runtime_passage_pairing(root, manifests, summary["conditions"])
                        summary["quality_gates"].extend(runtime_passage_quality_gates(
                            args.dataset, summary["conditions"][condition], summary["conditions"]["fresh-default"]))
                        summary["optional_runtime_passage_index"] = "complete"
                    if condition == "runtime-c4":
                        runtime_c4_pairing(root, manifests, summary["conditions"])
                        summary["optional_runtime_c4"] = "complete"
                        summary["quality_gates"].extend(c4_resource_gates(manifests[condition], prefix="runtime_c4"))
                        summary["quality_gates"].extend(runtime_c4_quality_gates(
                            args.dataset, summary["conditions"][condition], summary["conditions"]["runtime-passages"]))
                    save(root, summary)
            stage = "paired_validation"
            fixture = load_json(data, "manifest.json")
            baseline_hash = summary["conditions"]["guarded"]["run_manifest_sha256"]
            fixture_hash = digest_file(safe_member(data, "manifest.json"))
            for condition in ("fresh-default", "passages", *(("runtime-passages",) if runtime_passages else ())):
                if (manifests[condition]["source_run_manifest_sha256"] != baseline_hash
                        or manifests[condition]["dataset_manifest_sha256"] != fixture_hash):
                    raise GateError("PairedConditionSourceMismatch")
            if (manifests["replay"]["source_run_manifest_sha256"] != baseline_hash
                    or manifests["replay"]["source_runtime_files"] != runtime_files):
                raise GateError("ReplaySourceMismatch")
            observations = manifests["fresh-default"]["default_query_backend_observations"]
            if (observations["normal_sdk_query_encoding_calls"] != fixture["query_count"]
                    or not observations["all_query_model_tags_match_indexed_model"] or not observations["all_query_dimensions_384"]
                    or observations["document_inference_attempts"] != 0):
                raise GateError("FreshQueryInferenceAccountingFailed")
            if hashes(repository) != runtime_files or harness_hashes(repository) != harness:
                raise GateError("SourceChangedDuringRun")
            summary["quality_gates"] = quality_gates(args.dataset, summary["conditions"])
            if runtime_passages:
                runtime_passage_pairing(root, manifests, summary["conditions"])
                summary["optional_runtime_passage_index"] = "complete"
                summary["quality_gates"].extend(runtime_passage_quality_gates(
                    args.dataset, summary["conditions"]["runtime-passages"], summary["conditions"]["fresh-default"]))
                if c4:
                    runtime_c4_pairing(root, manifests, summary["conditions"])
                    summary["optional_runtime_c4"] = "complete"
                    summary["quality_gates"].extend(c4_resource_gates(manifests["runtime-c4"], prefix="runtime_c4"))
                    summary["quality_gates"].extend(runtime_c4_quality_gates(
                        args.dataset, summary["conditions"]["runtime-c4"], summary["conditions"]["runtime-passages"]))
            if c4:
                experiment = manifests["c4"]
                if (experiment.get("paired_original_condition") != "no_reranker_passages"
                        or experiment["source_run_manifest_sha256"] != baseline_hash
                        or experiment["passage_run_manifest_sha256"] != summary["conditions"]["passages"]["run_manifest_sha256"]):
                    raise GateError("OptionalPairedSourceMismatch")
                summary["optional_c4_cross_encoder"] = "complete"
                summary["quality_gates"].extend(c4_resource_gates(experiment))
                summary["quality_gates"].extend(c4_quality_gates(
                    args.dataset, summary["conditions"]["c4"], summary["conditions"]["passages"]))
            fresh = summary["conditions"]["fresh-default"]["metrics"]["results-default"]["final_sdk_top10"]["unconditional"]
            baseline = summary["conditions"]["guarded"]["metrics"]["results-C96"]["final_sdk_top10"]["unconditional"]
            selected_metrics = ("hit_at_1", "hit_at_10", "document_recall_at_10", "ndcg_at_10")
            summary["quality_differences"] = {
                "fresh_default_minus_guarded_C96": {metric: fresh[metric]["mean"] - baseline[metric]["mean"] for metric in selected_metrics},
                "passage_minus_original_sdk": {
                    cell: {metric: values["final_sdk_top10"]["unconditional"][metric]["mean"] - values["original_sdk_top10"]["unconditional"][metric]["mean"]
                           for metric in selected_metrics}
                    for cell, values in summary["conditions"]["passages"]["metrics"].items()},
            }
            if runtime_passages:
                summary["quality_differences"]["runtime_passages_minus_fresh_default"] = {
                    metric: summary["conditions"]["runtime-passages"]["metrics"]["results-default"]["final_sdk_top10"]["unconditional"][metric]["mean"] - fresh[metric]["mean"]
                    for metric in selected_metrics}
            summary["source_and_query_identity_verified"] = True
            summary["fresh_default_rank_parity_required"] = False
            summary["status"] = "complete" if all(gate["passed"] for gate in summary["quality_gates"]) else "failed"
            if summary["status"] == "failed":
                summary["failure_reason"] = "quality_gate"
    except BaseException as exc:
        summary.update(status="failed", failure_reason="qualification_or_input" if not summary.get("qualification")
                       else "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "phase_or_contract",
                       failure_details=failure_details(exc, stage))
        for phase in summary["phases"]:
            if phase["status"] == "running":
                phase.update(status="failed", reason="supervisor_or_contract")
    finally:
        save(root, summary)
    return 0 if summary["status"] == "complete" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "run", "finalize"))
    parser.add_argument("dataset", choices=DATASET_NAMES)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--model-directory", type=Path)
    parser.add_argument("--qualification-directory", type=Path)
    parser.add_argument("--floor-qualification-directory", type=Path)
    parser.add_argument("--wall-seconds", type=int, default=4200)
    parser.add_argument("--rss-mib", type=int, default=4096)
    parser.add_argument("--cpu-seconds", type=int, default=3600)
    parser.add_argument("--reranker-experiment", choices=("off", "c4"), default="off", help="Separate optional C4 experiment; requires its runner/provisioner and matching resource qualification")
    parser.add_argument("--runtime-passages", action="store_true", help="Separate optional actual schema-6 passage-index evaluation on original parents, paired with fresh-default under the existing whole-run caps")
    args = parser.parse_args()
    repository = checked_path(args.repository, must_exist=True, directory=True)
    root = isolated_artifact_path(args.output_root, repository)
    root.mkdir(parents=True, exist_ok=True)
    if args.command == "run":
        if (os.environ.get("GITHUB_ACTIONS") != "true" or not args.model_directory or not args.qualification_directory
                or not args.floor_qualification_directory or not 1 <= args.wall_seconds <= 4500
                or not 64 <= args.rss_mib <= 6144 or not 1 <= args.cpu_seconds <= 12000):
            summary = initial_summary(args.dataset, args.reranker_experiment == "c4", args.runtime_passages)
            summary.update(status="failed", failure_reason="ci_arguments_required")
            save(root, summary)
            return 1
        return run(args, repository, root)
    if args.command == "init":
        if (root / "job-summary.json").exists():
            raise GateError("RefusingExistingArtifact")
        save(root, initial_summary(args.dataset, args.reranker_experiment == "c4", args.runtime_passages))
    else:
        current = load_json(root, "job-summary.json")
        if current["status"] in {"running", "not_started"}:
            # Preserve the recorded phases after a workflow cancellation or setup failure.
            current.update(status="failed", failure_reason="workflow_interrupted")
            save(root, current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
