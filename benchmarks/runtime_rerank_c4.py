"""Remote-only qualification of the optional reranker through actual Mesh.recall.

The ordinary runtime-passage export fixes the comparator before matched native
subprocesses run. Ranking never reads relevance labels; the normal scorer runs
only after all identities, native calls, and resource limits are verified.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

import production_retrieval as production
from retrieval_c4 import (C4Aborted, C4Failure, C4Unavailable, checked_helpers, failure_details,
                          nearest_rank, supervised_measure, verify_fixture)
from retrieval_c4_model import LIMITS, MODEL, RESOURCE_MAXIMA

IMPLEMENTATIONS = ("runtime_rerank_c4.py", "production_retrieval.py", "replay_retrieval.py",
                   "retrieval_c4.py", "retrieval_c4_model.py", "provision_retrieval_c4.py")
POLICY = {
    "entrypoint": "Mesh.recall(query, n=10, include_private=False, include_quarantined=False)",
    "representation": production.PASSAGE_REPRESENTATION,
    "comparator": "Hash-verified original-parent runtime-passage export, reranker disabled.",
    "candidates": "First four eligible parents before n trim; preserve base scores, identities, spans and tail.",
    "query_encoding": "Fresh default neural query encoding; exactly one encoding per native call.",
    "resource_samples": "One default configuration; max(1, ceil(100/query count)) fixed matched passes.",
    "resources": "Same C4 RAM, latency and CPU maxima; include cold verified-byte model loading in tree RSS.",
    "top_one": "One separately recorded native n=1 call per query must match its native n=10 first result.",
    "labels_in_ranking_inputs": False,
    "production_default_enabled": False,
}
RERANK_FIELDS = ("rerank_status", "rerank_score", "rerank_original_rank", "rerank_input")


def implementation_hashes(repository, helpers):
    return {name: helpers.digest_file(helpers.safe_member(Path(repository) / "benchmarks", name))
            for name in IMPLEMENTATIONS}


def verify_protocol(args, helpers):
    protocol = json.loads(helpers.checked_path(args.protocol, must_exist=True, directory=False).read_text(encoding="utf-8"))
    if (protocol != {"version": 1, "model": MODEL, "limits": LIMITS, "policy": POLICY,
                     "resource_maxima": RESOURCE_MAXIMA,
                     "base_protocol_sha256": helpers.digest_file(args.base_protocol),
                     "implementation_sha256": implementation_hashes(args.repository, helpers)}):
        raise C4Failure("FrozenC4ProtocolMismatch")


def resource_gates(baseline, candidate, baseline_peak, candidate_peak):
    if any(type(value) is not int or value <= 0 for value in (baseline_peak, candidate_peak)):
        raise C4Failure("InvalidProcessTreePeak")
    for row in (*baseline, *candidate):
        if (type(row.get("query_id")) is not str or not row["query_id"]
                or type(row.get("resource_pass")) is not int or row["resource_pass"] < 0
                or any(type(row.get(key)) not in {int, float} or not math.isfinite(row[key]) or row[key] < 0
                       for key in ("wall_ms", "process_cpu_ms"))):
            raise C4Failure("InvalidResourceSamples")
    original = {(row["query_id"], row["resource_pass"]): row for row in baseline}
    final = {(row["query_id"], row["resource_pass"]): row for row in candidate}
    if not original or len(original) != len(baseline) or len(final) != len(candidate) or set(original) != set(final):
        raise C4Failure("UnpairedResourceSamples")
    differences = [final[key]["wall_ms"] - original[key]["wall_ms"] for key in original]
    cpu = [final[key]["process_cpu_ms"] - original[key]["process_cpu_ms"] for key in original]
    observed = {"total_rss_mib": candidate_peak / 1024**2,
                "delta_rss_mib": (candidate_peak - baseline_peak) / 1024**2,
                "delta_p95_ms": nearest_rank(differences, 0.95),
                "delta_p99_ms": nearest_rank(differences, 0.99), "delta_cpu_ms": sum(cpu) / len(cpu)}
    gates = {name: {"observed": observed[name], "maximum": maximum} for name, maximum in RESOURCE_MAXIMA.items()}
    enough = len(candidate) >= LIMITS["minimum_resource_samples_per_cell"]
    qualified = enough and all(math.isfinite(row["observed"]) and row["observed"] <= row["maximum"]
                              for row in gates.values())
    return gates, qualified, enough


class RuntimeObserver:
    def __init__(self, args, helpers):
        self.args, self.helpers = args, helpers
        path = helpers.safe_member(args.runtime_passage_run, "results-default.jsonl")
        rows = list(helpers.read_jsonl(path))
        self.frozen = {row["query_id"]: row for row in rows}
        if not rows or len(rows) != len(self.frozen):
            raise C4Failure("DuplicateImmutableQuery")
        self.resource_passes = max(1, math.ceil(LIMITS["minimum_resource_samples_per_cell"] / len(rows)))
        self.counts, self.measurements, self.quality_heads = Counter(), [], {}
        self.module = None
        self.original_init = self.original_score = None

    def configure(self):
        # install_model clears inherited settings; opt in only after its isolation.
        os.environ["MESH_RERANK_ENABLED"] = "1" if self.args.phase == "c4" else "0"
        if self.args.phase == "c4":
            os.environ["MESH_RERANK_MODEL_DIR"] = str(self.args.model_directory)

    def attach_runtime(self, memory_module):
        if self.args.phase != "c4" or self.module is not None:
            return
        module = importlib.import_module("mesh.local_rerank")
        if (Path(module.__file__).parent != Path(memory_module.__file__).parent
                or module.MODEL != {key: value for key, value in MODEL.items() if key != "tokenizer_verified"}
                or (module.PARENT_CAP, module.QUERY_TOKENS, module.PASSAGE_TOKENS, module.PAIR_TOKENS)
                != (LIMITS["parent_cap"], LIMITS["maximum_query_tokens"], LIMITS["maximum_passage_tokens"], LIMITS["maximum_pair_tokens"])):
            raise C4Failure("RuntimeRerankerConfigurationChanged")
        self.module = module
        self.original_init, self.original_score = module._LocalCrossEncoder.__init__, module._LocalCrossEncoder.score

        def counted_init(engine, *args, **kwargs):
            started = time.perf_counter()
            self.counts["session_load_attempts"] += 1
            result = self.original_init(engine, *args, **kwargs)
            self.counts["session_load_count"] += 1
            self.counts["cold_load_ms"] += (time.perf_counter() - started) * 1000
            return result

        def counted_score(engine, arrays, count):
            self.counts["inference_attempted_batches"] += 1
            self.counts["inference_attempted_pairs"] += count
            scores = self.original_score(engine, arrays, count)
            self.counts["inference_returned_batches"] += 1
            self.counts["inference_returned_pairs"] += count
            return scores

        module._LocalCrossEncoder.__init__, module._LocalCrossEncoder.score = counted_init, counted_score

    def validate(self, query, results, *, n=10):
        frozen = self.frozen.get(query["id"])
        if frozen is None or frozen["corpus_id"] != query.get("corpus", "complete"):
            raise C4Failure("ImmutableQueryAccountingChanged")
        expected = frozen["final_sdk_top10"]
        exported = production.decorate_results(results, self.by_memory)
        production.validate_runtime_parent_results(exported, self.identities)
        if len(exported) != min(n, len(expected)) or len({row["id"] for row in exported}) != len(exported):
            raise C4Failure("RuntimeRerankerParentSetChanged")
        by_id = {row["id"]: row for row in expected}
        for row in exported:
            if row != by_id.get(row["id"]):
                raise C4Failure("ParentFullIdentityChanged")
        if self.args.phase == "baseline":
            if exported != expected[:n] or any(any(key in row for key in RERANK_FIELDS) for row in results):
                raise C4Failure("BaselineUnexpectedlyLoadedCE")
        else:
            cap = min(LIMITS["parent_cap"], len(expected))
            if n == 10 and ({row["id"] for row in exported[:cap]} != {row["id"] for row in expected[:cap]}
                            or exported[cap:] != expected[cap:]):
                raise C4Failure("C4OutputCandidateCapOrTailChanged")
            original_ranks = {row["id"]: rank for rank, row in enumerate(expected, 1)}
            for row, projection in zip(results, exported):
                rank = original_ranks[row["id"]]
                status = row.get("rerank_status")
                if cap < 2:
                    if status != "insufficient_candidates":
                        raise C4Failure("RuntimeRerankerFallback")
                elif rank > cap:
                    if status != "outside_parent_cap" or any(key in row for key in RERANK_FIELDS[1:]):
                        raise C4Failure("C4OutputCandidateCapOrTailChanged")
                else:
                    if (status != "applied" or type(row.get("rerank_score")) not in {int, float}
                            or not math.isfinite(row["rerank_score"]) or type(row.get("rerank_original_rank")) is not int
                            or row["rerank_original_rank"] != rank):
                        raise C4Failure("RuntimeRerankerFallback")
                    self.validate_input(query, row)
                projection.update({key: row[key] for key in RERANK_FIELDS if key in row})
            applied = [row for row in results if row.get("rerank_status") == "applied"]
            if applied != sorted(applied, key=lambda row: (-row["rerank_score"], row["rerank_original_rank"])):
                raise C4Failure("InvalidRelevanceLogits")
        return exported

    def validate_input(self, query, row):
        body = row.get("content")
        records = self.by_memory[row["id"]]
        if (not isinstance(body, str) or not records
                or any(hashlib.sha256(body.encode("utf-8")).hexdigest() != record.get("stored_sha256") for record in records)):
            raise C4Failure("AcceptedParentBodyOrIdentityChanged")
        account = row.get("rerank_input")
        integer_keys = ("start", "end", "query_tokens", "kept_query_tokens", "removed_query_characters",
                        "passage_tokens", "kept_passage_tokens", "removed_passage_characters", "pair_tokens")
        if (not isinstance(account, dict) or set(account) != {"source", *integer_keys}
                or any(type(account.get(key)) is not int or account[key] < 0 for key in integer_keys)
                or not 0 <= account["start"] < account["end"] <= len(body)
                or not account["kept_query_tokens"] <= min(account["query_tokens"], LIMITS["maximum_query_tokens"])
                or not 0 < account["kept_passage_tokens"] <= min(account["passage_tokens"], LIMITS["maximum_passage_tokens"])
                or not 3 <= account["pair_tokens"] <= LIMITS["maximum_pair_tokens"]
                or account["removed_query_characters"] > len(query["query"])):
            raise C4Failure("RuntimeRerankerInputAccountingChanged")
        if row.get("selected_passage_ordinal") is not None:
            spans = row.get("supporting_spans", [])
            if (len(spans) != 1 or account["source"] != "exact_selected_source_span"
                    or (account["start"], account["end"]) != (spans[0].get("start"), spans[0].get("end"))
                    or spans[0].get("text") != body[account["start"]:account["end"]]
                    or account["removed_passage_characters"] != 0 or account["passage_tokens"] != account["kept_passage_tokens"]):
                raise C4Failure("SupportingSpanBytesChanged")
        elif (account["source"] != "accepted_original_body" or account["start"] != 0
              or account["removed_passage_characters"] != len(body) - account["end"]):
            raise C4Failure("RuntimeRerankerInputAccountingChanged")

    def native_call(self, mesh, query, memory_module, *, n=10, category="warmup"):
        original = memory_module._embed_query_with_tag
        document_encoder, worker = memory_module._encode_texts_with_tag, memory_module._process_reembed_batch
        calls = []

        def counted_query(text):
            vector, tag = original(text)
            calls.append((tag, len(vector)))
            return vector, tag

        def forbid_document_inference(*_args, **_kwargs):
            self.counts["document_inference_attempts"] += 1
            raise C4Failure("RuntimeRerankerQueryEncodingChanged")

        memory_module._embed_query_with_tag = counted_query
        memory_module._encode_texts_with_tag = memory_module._process_reembed_batch = forbid_document_inference
        started, cpu = time.perf_counter(), time.process_time()
        try:
            results = mesh.recall(query["query"], n=n, include_private=False, include_quarantined=False)
        finally:
            memory_module._embed_query_with_tag = original
            memory_module._encode_texts_with_tag, memory_module._process_reembed_batch = document_encoder, worker
        wall_ms, cpu_ms = (time.perf_counter() - started) * 1000, (time.process_time() - cpu) * 1000
        if calls != [(production.MODEL_TAG, 384)]:
            raise C4Failure("RuntimeRerankerQueryEncodingChanged")
        exported = self.validate(query, results, n=n)
        self.counts[category + "_native_calls"] += 1
        return results, exported, wall_ms, cpu_ms

    def before_namespace(self, mesh, queries, records, memory_module):
        self.identities = production.runtime_parent_identities(records)
        self.by_memory = defaultdict(list)
        for record in records:
            if record["acceptance"].get("memory_id"):
                self.by_memory[record["acceptance"]["memory_id"]].append(record)
        self.attach_runtime(memory_module)
        for query in queries[:LIMITS["warmup_queries_per_namespace"]]:
            self.native_call(mesh, query, memory_module)

    def sample(self, query, wall_ms, cpu_ms, resource_pass):
        self.measurements.append({"query_id": query["id"], "resource_pass": resource_pass,
                                  "wall_ms": wall_ms, "process_cpu_ms": cpu_ms,
                                  "quality_export": resource_pass == 0})

    def observe(self, query, results, payload, wall_ms, cpu_ms):
        payload["final_sdk_top10"] = self.validate(query, results)
        payload["original_sdk_top10"] = self.frozen[query["id"]]["final_sdk_top10"]
        self.quality_heads[query["id"]] = [row["id"] for row in results[:1]]
        self.counts["quality_native_calls"] += 1
        self.sample(query, wall_ms, cpu_ms, 0)

    def after_namespace(self, mesh, queries, records, memory_module):
        for query in queries:
            results, _exported, _wall, _cpu = self.native_call(mesh, query, memory_module, n=1, category="top_one")
            if [row["id"] for row in results] != self.quality_heads[query["id"]]:
                raise C4Failure("RuntimeRerankerTopOneChanged")
        for resource_pass in range(1, self.resource_passes):
            for query in queries:
                _results, _exported, wall, cpu = self.native_call(mesh, query, memory_module, category="resource_repeat")
                self.sample(query, wall, cpu, resource_pass)

    def finish(self, manifest, output):
        expected = {(query_id, resource_pass) for query_id in self.frozen for resource_pass in range(self.resource_passes)}
        actual = {(row["query_id"], row["resource_pass"]) for row in self.measurements}
        if (expected != actual or len(actual) != len(self.measurements)
                or self.counts["quality_native_calls"] != len(self.frozen)
                or self.counts["top_one_native_calls"] != len(self.frozen)):
            raise C4Failure("IncompleteMeasuredQueries")
        if self.args.phase == "c4" and (self.counts["session_load_count"] != 1
                or self.counts["session_load_attempts"] != 1
                or self.counts["inference_attempted_batches"] != self.counts["inference_returned_batches"]
                or self.counts["inference_attempted_pairs"] != self.counts["inference_returned_pairs"]):
            raise C4Failure("InferenceCounterAccountingChanged")
        with (output / "runtime-reranker-resources.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
            for row in self.measurements:
                production.append_jsonl(stream, row)
        manifest["runtime_reranker"] = {"configuration": "c4-minilm-runtime-recall", "phase": self.args.phase,
                                       "policy": POLICY, "model": MODEL, "limits": LIMITS,
                                       "resource_passes": self.resource_passes, "native_counts": dict(self.counts),
                                       "parent_identity_scores_spans_and_tail_verified": True,
                                       "top_one_matches_top_ten": True, "labels_in_ranking_inputs": False}

    def close(self):
        if self.module is not None:
            self.module._LocalCrossEncoder.__init__, self.module._LocalCrossEncoder.score = self.original_init, self.original_score
            self.module.unload_reranker()


def measure(args, helpers):
    verify_protocol(args, helpers)
    observer = RuntimeObserver(args, helpers)
    source = next(helpers.read_jsonl(helpers.safe_member(args.source_run, "query-vectors.jsonl")))
    options = argparse.Namespace(command="query-only", dataset=args.dataset, repository=args.repository,
        dataset_directory=args.dataset_directory, protocol=args.base_protocol, model_directory=args.embedding_model_directory,
        dependency_directory=args.dependency_directory, output=args.output, retain_guarded_home=False,
        source_run=args.source_run, source_home=None, source_namespace=source["namespace"] if args.dataset != "longmemeval" else None,
        runtime_passages=True, fresh_default_run=args.fresh_default_run)
    try:
        production.run_retrieval(options, experiment=observer)
    except BaseException as exc:
        manifest_path = helpers.safe_member(args.output, "run-manifest.json", must_exist=False)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            details = failure_details(exc, measured_phase=args.phase)
            manifest.update(failure_details=details, failure_type=details.get("failure_code", details["exception_type"]),
                            runtime_reranker_native_counts=dict(observer.counts))
            helpers.write_json(manifest_path, manifest)
        raise


def run(args, helpers):
    output = helpers.isolated_artifact_path(args.output, args.repository)
    if output.exists():
        raise C4Failure("OutputAlreadyExists")
    output.mkdir(parents=True)
    metadata = {"status": "running", "configuration": "c4-minilm-runtime-recall", "optional_default_enabled": False,
                "reranker_model": MODEL, "reranker_limits": LIMITS, "reranker_policy": POLICY,
                "all_queries_accounted": False, "resource_qualified": False, "paired_original_condition": "runtime_passages",
                "resource_gates": {key: {"observed": None, "maximum": maximum} for key, maximum in RESOURCE_MAXIMA.items()},
                "labels_in_ranking_inputs": False, "failure_type": None}
    owned, active_phase = None, None
    helpers.write_json(output / "run-manifest.json", metadata)
    try:
        verify_protocol(args, helpers)
        for name in ("source_run", "runtime_passage_run", "fresh_default_run", "dataset_directory", "embedding_model_directory", "model_directory"):
            setattr(args, name, helpers.isolated_artifact_path(getattr(args, name), args.repository, must_exist=True))
        source_path = helpers.safe_member(args.source_run, "run-manifest.json")
        comparator_path = helpers.safe_member(args.runtime_passage_run, "run-manifest.json")
        source, comparator = (json.loads(path.read_text(encoding="utf-8")) for path in (source_path, comparator_path))
        if (source.get("status") != "complete" or comparator.get("status") != "complete"
                or comparator.get("all_queries_accounted") is not True or comparator.get("passage_index_enabled") is not True
                or comparator.get("source_run_manifest_sha256") != helpers.digest_file(source_path)
                or comparator.get("runtime_files") != source.get("runtime_files")
                or comparator.get("dataset_manifest_sha256") != source.get("dataset_manifest_sha256")
                or comparator.get("protocol_sha256") != helpers.digest_file(args.base_protocol)
                or comparator.get("query_target") != source.get("query_target")
                or comparator.get("runtime_passage_index", {}).get("representation") != production.PASSAGE_REPRESENTATION):
            raise C4Failure("IncompleteOrMismatchedPassageComparator")
        for relative, expected in source["runtime_files"].items():
            if helpers.digest_file(helpers.safe_member(args.repository, relative)) != expected:
                raise C4Failure("RuntimeSourceHashMismatch")
        helpers.verify_exports(args.source_run, source)
        helpers.verify_exports(args.runtime_passage_run, comparator)
        verify_fixture(args.model_directory, helpers)
        metadata.update(runtime_passage_run_manifest_sha256=helpers.digest_file(comparator_path),
                        runtime_passage_results_sha256=comparator["output_sha256"]["results-default.jsonl"],
                        runtime_c4_protocol_sha256=helpers.digest_file(args.protocol))
        owned = tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-runtime-c4-")
        temporary = Path(owned.name).resolve(strict=True)
        helpers.configure_isolation(temporary)
        base = [sys.executable, str(helpers.safe_member(args.repository, "benchmarks/runtime_rerank_c4.py")), "measure", args.dataset]
        for name in ("repository", "source_run", "runtime_passage_run", "fresh_default_run", "dataset_directory", "base_protocol", "protocol",
                     "model_directory", "embedding_model_directory"):
            base += ["--" + name.replace("_", "-"), str(getattr(args, name))]
        if args.dependency_directory:
            base += ["--dependency-directory", str(args.dependency_directory)]
        phases = {}
        for phase in ("baseline", "c4"):
            active_phase = phase
            ceiling = RESOURCE_MAXIMA["total_rss_mib"] * 1024**2
            if phase == "c4":
                ceiling = min(ceiling, phases["baseline"]["process_tree_peak_rss_bytes"] + RESOURCE_MAXIMA["delta_rss_mib"] * 1024**2)
            phase_output = temporary / phase
            phases[phase] = supervised_measure(base + ["--phase", phase, "--output", str(phase_output)], args.repository, ceiling, phase_output, helpers)
            active_phase = None
            metadata["matched_resource_phases"] = phases
            helpers.write_json(output / "run-manifest.json", metadata)
        samples = {phase: list(helpers.read_jsonl(helpers.safe_member(temporary / phase, "runtime-reranker-resources.jsonl")))
                   for phase in ("baseline", "c4")}
        gates, qualified, enough = resource_gates(samples["baseline"], samples["c4"],
            phases["baseline"]["process_tree_peak_rss_bytes"], phases["c4"]["process_tree_peak_rss_bytes"])
        final = json.loads(helpers.safe_member(temporary / "c4", "run-manifest.json").read_text(encoding="utf-8"))
        helpers.verify_exports(temporary / "c4", final)
        if final.get("status") != "complete" or final.get("runtime_files") != source["runtime_files"]:
            raise C4Failure("IncompleteC4Export")
        metadata.update(final, status="running", resource_gates=gates, resource_qualified=qualified,
                        resource_sample_count_sufficient=enough, unscored_queries=0)
        for relative in final["output_sha256"]:
            shutil.copyfile(helpers.safe_member(temporary / "c4", relative), helpers.safe_member(output, relative, must_exist=False))
        metadata["baseline_query_resource_sha256"] = helpers.digest_file(helpers.safe_member(temporary / "baseline", "runtime-reranker-resources.jsonl"))
        metadata["warm_resource_summary"] = {
            phase: {"samples": len(rows), "wall_p50_ms": nearest_rank([row["wall_ms"] for row in rows], 0.50),
                    "wall_p95_ms": nearest_rank([row["wall_ms"] for row in rows], 0.95),
                    "wall_p99_ms": nearest_rank([row["wall_ms"] for row in rows], 0.99),
                    "mean_process_cpu_ms": sum(row["process_cpu_ms"] for row in rows) / len(rows)}
            for phase, rows in samples.items()}
        helpers.verify_exports(args.source_run, source)
        helpers.verify_exports(args.runtime_passage_run, comparator)
        verify_protocol(args, helpers)
        verify_fixture(args.model_directory, helpers)
        if not qualified:
            raise C4Failure("InsufficientResourceSamples" if not enough else "ResourceGateRejected")
        metadata.update(status="complete", quality_scoring_status="exports_fixed_ready_for_separate_scorer")
    except BaseException as exc:
        details = failure_details(exc, measured_phase=active_phase)
        metadata.update(status="unavailable" if isinstance(exc, C4Unavailable) else "aborted" if isinstance(exc, (C4Aborted, KeyboardInterrupt)) else "failed",
                        failure_type=details.get("failure_code", details["exception_type"]), failure_details=details,
                        quality_scoring_status="not_run_optional_configuration_failed")
        raise
    finally:
        if owned is not None:
            root = Path(owned.name)
            metadata["owned_cleanup_files"] = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
            owned.cleanup()
        helpers.write_json(output / "run-manifest.json", metadata)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="Freeze actual runtime-C4 qualification before native execution")
    for name in ("repository", "base-protocol", "output"):
        freeze.add_argument("--" + name, type=Path, required=True)
    for name in ("run", "measure"):
        command = commands.add_parser(name, help="Qualify actual Mesh.recall on CI" if name == "run" else argparse.SUPPRESS)
        command.add_argument("dataset", choices=("nfcorpus", "scifact", "prospective", "longtext-prospective", "longmemeval"))
        for option in ("repository", "source-run", "runtime-passage-run", "fresh-default-run", "dataset-directory", "base-protocol", "protocol",
                       "model-directory", "embedding-model-directory", "output"):
            command.add_argument("--" + option, type=Path, required=True)
        command.add_argument("--dependency-directory", type=Path)
        if name == "measure":
            command.add_argument("--phase", choices=("baseline", "c4"), required=True)
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise C4Unavailable("RemoteActionsRequired")
    helpers = checked_helpers(args.repository)
    args.repository = helpers.checked_path(args.repository, must_exist=True, directory=True)
    args.base_protocol = helpers.checked_path(args.base_protocol, must_exist=True, directory=False)
    if args.command == "freeze":
        target = helpers.isolated_artifact_path(args.output, args.repository)
        if target.exists():
            raise C4Failure("FrozenProtocolAlreadyExists")
        target.parent.mkdir(parents=True, exist_ok=True)
        helpers.write_json(target, {"version": 1, "model": MODEL, "limits": LIMITS, "policy": POLICY,
                                   "resource_maxima": RESOURCE_MAXIMA,
                                   "base_protocol_sha256": helpers.digest_file(args.base_protocol),
                                   "implementation_sha256": implementation_hashes(args.repository, helpers)})
    elif args.command == "measure":
        measure(args, helpers)
    else:
        run(args, helpers)


if __name__ == "__main__":
    main()
