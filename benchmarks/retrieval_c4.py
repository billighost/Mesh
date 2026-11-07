"""Default-off, GitHub Actions C4 evaluation; no provider or automatic model download.

Freeze configuration, provision separately, then run matched no-CE/C4 passage
rebuilds in fresh subprocesses. Labels are consumed only by the existing scorer
after a complete, resource-qualified candidate export exists.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gc
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from retrieval_c4_model import LIMITS, MODEL, POLICY, RESOURCE_MAXIMA

IMPLEMENTATIONS = ("retrieval_c4.py", "retrieval_c4_model.py", "provision_retrieval_c4.py",
                   "passage_retrieval.py", "production_retrieval.py", "replay_retrieval.py")
C4_FAILURE_CODES = frozenset({
    "AcceptedParentBodyOrIdentityChanged", "AmbiguousSourceIdentity", "BaselineUnexpectedlyLoadedCE",
    "C4OutputCandidateCapOrTailChanged", "C4PairCapExceeded", "CandidatePrivacyFlagSet",
    "DuplicateImmutableQuery", "EmptySupportingPassage", "FrozenC4ProtocolMismatch", "FrozenProtocolAlreadyExists",
    "HelperUnavailable", "ImmutableCellQueriesChanged", "ImmutableParentOrderOrSpanChanged",
    "ImmutableQueryAccountingChanged", "IncompleteC4Export", "IncompleteMeasuredQueries",
    "IncompleteOrMismatchedPassageComparator", "InferenceCounterAccountingChanged", "InferenceCounterCapExceeded",
    "InsufficientResourceSamples", "Interrupted", "InvalidProcessTreePeak", "InvalidRelevanceLogits",
    "InvalidResourceSamples", "InvalidScoredPairAccounting", "LinkedHelper", "LinkedRepositoryAncestor",
    "MeasuredSubprocessFailed", "MissingProcessTreeSamples", "OutputAlreadyExists", "PairTokenCapExceeded",
    "ParentCapExceeded", "ParentFullIdentityChanged", "ParentIdentifierChanged", "ParentNamespaceChanged",
    "ParentPrivacyChanged", "PassageSourceVectorsOrModelChanged", "PhaseWallDeadlineExceeded",
    "PinnedFixtureHashMismatch", "PinnedFixtureMissing", "ProcessTreeRSSLimitExceeded", "RemoteActionsRequired",
    "RepositoryTraversal", "ResourceGateRejected", "RuntimeSourceHashMismatch", "SelectedPassageExceedsTokenLimit",
    "SourceChangedDuringInference", "SourceChangedDuringWarmupInference", "SourceNamespaceChanged",
    "SupportingSpanBytesChanged", "SupportingSpanCountChanged", "SupportingSpanIdentityChanged",
    "TokenizerPrefixBoundFailed", "UnauthorizedParent", "UnexpectedHelperImport", "UnexpectedModelInputOrProvider",
    "UnexpectedModelOutputCount", "UnexpectedPairSpecialTokens", "UnexpectedSessionReloadCount",
    "UnknownSupportingSource", "UnpairedResourceSamples",
    "RuntimeRerankerConfigurationChanged", "RuntimeRerankerFallback", "RuntimeRerankerInputAccountingChanged",
    "RuntimeRerankerParentSetChanged", "RuntimeRerankerQueryEncodingChanged", "RuntimeRerankerTopOneChanged",
})
C4_FAILURE_TYPES = frozenset({"C4Failure", "C4Unavailable", "C4Aborted"})


class C4Failure(RuntimeError):
    """Content-free failure codes are the only exception details exported."""

    def __init__(self, code, *, evidence=None):
        super().__init__(code)
        self.evidence = evidence


class C4Unavailable(C4Failure):
    pass


class C4Aborted(C4Failure):
    pass


def failure_projection(manifest, stage="c4", *, include_child=True):
    from production_retrieval import WORKER_FAILURE_SOURCES, WORKER_FAILURE_TYPES

    status = manifest.get("status")
    if type(status) is not str or status not in {"failed", "aborted", "unavailable"}:
        return {"stage": stage, "exception_type": "other", "source_frames": []}
    details = manifest.get("failure_details", {})
    if not isinstance(details, dict):
        details = {}
    code = details.get("failure_code", manifest.get("c4_failure_code", manifest.get("failure_type")))
    code = code if type(code) is str and code in C4_FAILURE_CODES else None
    name = details.get("exception_type", manifest.get("failure_type"))
    if type(name) is not str or name not in WORKER_FAILURE_TYPES | C4_FAILURE_TYPES:
        name = {"aborted": "C4Aborted", "unavailable": "C4Unavailable"}.get(status, "C4Failure") if code else "other"
    frames = details.get("source_frames", [])
    selected = []
    if isinstance(frames, list):
        for frame in frames[-4:]:
            if (isinstance(frame, dict) and type(frame.get("source")) is str and frame["source"] in WORKER_FAILURE_SOURCES
                    and type(frame.get("line")) is int and frame["line"] > 0):
                selected.append({"source": frame["source"], "line": frame["line"]})
    result = {"stage": stage, "exception_type": name, "source_frames": selected}
    if code:
        result["failure_code"] = code
    measured_phase = details.get("measured_phase", manifest.get("measured_phase"))
    if type(measured_phase) is str and measured_phase in {"baseline", "c4"}:
        result["measured_phase"] = measured_phase
    evidence = details.get("failure_evidence", manifest.get("failure_evidence", {}))
    if not isinstance(evidence, dict):
        evidence = {}
    projected = {}
    for key in ("process_tree_peak_rss_bytes", "process_tree_sample_count", "rss_limit_bytes", "child_returncode"):
        value = evidence.get(key)
        if type(value) is int and (key == "child_returncode" or value >= 0):
            projected[key] = value
    wall = evidence.get("phase_wall_seconds")
    if type(wall) in {int, float} and (type(wall) is int or math.isfinite(wall)) and wall >= 0:
        projected["phase_wall_seconds"] = wall
    if projected:
        result["failure_evidence"] = projected
    if include_child:
        child = details.get("child_failure_diagnostics", evidence.get("child_failure_diagnostics"))
        if isinstance(child, dict):
            result["child_failure_diagnostics"] = failure_projection(
                {"status": "failed", "failure_details": child}, stage, include_child=False)
        elif type(evidence.get("child_failure_type")) is str:
            result["child_failure_diagnostics"] = failure_projection(
                {"status": "failed", "failure_type": evidence["child_failure_type"]}, stage, include_child=False)
    return result


def failure_details(exc, *, measured_phase=None):
    from production_retrieval import worker_failure_details

    details = worker_failure_details(exc, "c4")
    if type(exc).__name__ in C4_FAILURE_TYPES:
        details["exception_type"] = type(exc).__name__
    if isinstance(exc, C4Failure) and len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in C4_FAILURE_CODES:
        details["failure_code"] = exc.args[0]
    return failure_projection({"status": "failed", "failure_details": details,
                               "failure_evidence": exc.evidence if isinstance(exc, C4Failure) else {},
                               "measured_phase": measured_phase})


def checked_helpers(repository):
    # Validate every ancestor before importing even a stdlib-only helper.
    original = Path(repository).absolute()
    if ".." in Path(repository).parts:
        raise C4Failure("RepositoryTraversal")
    for part in reversed((original, *original.parents)):
        metadata = part.lstat()
        if part.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise C4Failure("LinkedRepositoryAncestor")
    directory = original / "benchmarks"
    for part in (directory, directory / "replay_retrieval.py"):
        metadata = part.lstat()
        if part.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise C4Failure("LinkedHelper")
    path = directory / "replay_retrieval.py"
    previous = sys.modules.get("replay_retrieval")
    if previous is not None:
        if Path(previous.__file__).absolute() != path:
            raise C4Failure("UnexpectedHelperImport")
        return previous
    specification = importlib.util.spec_from_file_location("replay_retrieval", path)
    if specification is None or specification.loader is None:
        raise C4Failure("HelperUnavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    module.checked_path(directory, must_exist=True, directory=True)
    return module


def implementation_hashes(repository, helpers):
    directory = helpers.checked_path(Path(repository) / "benchmarks", must_exist=True, directory=True)
    return {name: helpers.digest_file(helpers.safe_member(directory, name)) for name in IMPLEMENTATIONS}


def verify_fixture(directory, helpers):
    for kind in ("model", "tokenizer"):
        try:
            path = helpers.safe_member(directory, MODEL[kind + "_file"])
        except FileNotFoundError as exc:
            raise C4Unavailable("PinnedFixtureMissing") from exc
        if path.stat().st_size != MODEL[kind + "_bytes"] or helpers.digest_file(path) != MODEL[kind + "_sha256"]:
            raise C4Unavailable("PinnedFixtureHashMismatch")


def nearest_rank(values, fraction):
    if not values or any(isinstance(value, bool) or not isinstance(value, (int, float))
                         or not math.isfinite(value) for value in values):
        raise C4Failure("InvalidResourceSamples")
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def resource_gates(baseline, candidate, baseline_peak, candidate_peak):
    if any(type(value) is not int or value <= 0 for value in (baseline_peak, candidate_peak)):
        raise C4Failure("InvalidProcessTreePeak")
    for row in (*baseline, *candidate):
        if (row.get("candidate_cap") not in (64, 96) or type(row.get("resource_pass", 0)) is not int
                or row.get("resource_pass", 0) < 0
                or any(isinstance(row.get(name), bool) or not isinstance(row.get(name), (int, float))
                       or not math.isfinite(row[name]) or row[name] < 0 for name in ("wall_ms", "process_cpu_ms"))):
            raise C4Failure("InvalidResourceSamples")
    def by_key(rows):
        return {(row["query_id"], row["candidate_cap"], row.get("resource_pass", 0)): row for row in rows}
    original, final = by_key(baseline), by_key(candidate)
    if (len(original) != len(baseline) or len(final) != len(candidate) or set(original) != set(final)
            or {key[1] for key in original} != {64, 96}):
        raise C4Failure("UnpairedResourceSamples")
    differences = {cap: [final[key]["wall_ms"] - original[key]["wall_ms"] for key in original if key[1] == cap]
                   for cap in (64, 96)}
    cpu = {cap: [final[key]["process_cpu_ms"] - original[key]["process_cpu_ms"] for key in original if key[1] == cap]
           for cap in (64, 96)}
    observed = {"total_rss_mib": candidate_peak / 1024**2,
                "delta_rss_mib": (candidate_peak - baseline_peak) / 1024**2,
                "delta_p95_ms": max(nearest_rank(values, 0.95) for values in differences.values()),
                "delta_p99_ms": max(nearest_rank(values, 0.99) for values in differences.values()),
                "delta_cpu_ms": max(sum(values) / len(values) for values in cpu.values())}
    gates = {name: {"observed": observed[name], "maximum": maximum} for name, maximum in RESOURCE_MAXIMA.items()}
    enough = all(sum(row["candidate_cap"] == cap for row in candidate) >= LIMITS["minimum_resource_samples_per_cell"]
                 for cap in (64, 96))
    qualified = enough and all(math.isfinite(value["observed"]) and value["observed"] <= value["maximum"]
                              for value in gates.values())
    return gates, qualified, enough


def candidate_identity(row):
    # Child IDs are newly generated on a rebuild; parent/ordinal/source-span
    # identities, hybrid values and original ordering must still be identical.
    fields = ("id", "revision", "incarnation", "store_generation", "embedding_model", "match_source",
              "selected_passage_ordinal", "faithful_document_ids", "rank_score", "similarity",
              "semantic_similarity", "lexical_score", "bm25_score", "retrieval_mode", "candidate_count",
              "local_only", "quarantined", "confidence", "effective_confidence", "stale", "memory_type", "namespace")
    spans = tuple((span.get("memory_id"), span.get("revision"), span.get("incarnation"),
                   span.get("store_generation"), span.get("start"), span.get("end"), span.get("span_content_sha256"))
                  for span in row.get("supporting_spans", []))
    return tuple(row.get(field) for field in fields), spans


def assert_same_parents(actual, frozen):
    if (len(actual) != len(frozen) or len({row["id"] for row in actual}) != len(actual)
            or [candidate_identity(row) for row in actual] != [candidate_identity(row) for row in frozen]):
        raise C4Failure("ImmutableParentOrderOrSpanChanged")


def bounded_prefix(text, tokenizer, maximum):
    encoded = tokenizer.encode(text, add_special_tokens=False)
    count = len(encoded.ids)
    if count <= maximum:
        return text, count, count
    end = encoded.offsets[maximum - 1][1]
    prefix = text[:end]
    kept = len(tokenizer.encode(prefix, add_special_tokens=False).ids)
    if not prefix.strip() or kept > maximum:
        raise C4Failure("TokenizerPrefixBoundFailed")
    return prefix, count, kept


def verified_inputs(candidates, parent_mesh, records, namespace, passage_module):
    if parent_mesh.namespace != namespace:
        raise C4Failure("SourceNamespaceChanged")
    inputs = []
    for row in candidates:
        if row.get("local_only") or row.get("quarantined"):
            raise C4Failure("CandidatePrivacyFlagSet")
        record = records.get(row["id"])
        if record is None or not record.get("faithful_public"):
            raise C4Failure("UnauthorizedParent")
        current = parent_mesh.get(row["id"], include_private=False)
        if current is None or current.get("local_only") or current.get("quarantined"):
            raise C4Failure("ParentPrivacyChanged")
        if current.get("id") != row["id"] or record["acceptance"]["memory_id"] != row["id"]:
            raise C4Failure("ParentIdentifierChanged")
        if current.get("namespace", namespace) != namespace or row.get("namespace", namespace) != namespace:
            raise C4Failure("ParentNamespaceChanged")
        try:
            identity = passage_module.parent_identity(current, record)
        except ValueError as exc:
            raise C4Failure("AcceptedParentBodyOrIdentityChanged") from exc
        if any(row.get(key) != value for key, value in identity.items()):
            raise C4Failure("ParentFullIdentityChanged")
        body = current["content"]
        if row.get("match_source") == "derived_passage":
            spans = row.get("supporting_spans", [])
            if len(spans) != 1:
                raise C4Failure("SupportingSpanCountChanged")
            span = spans[0]
            for key, value in (("memory_id", row["id"]), ("revision", identity["revision"]),
                               ("incarnation", identity["incarnation"]), ("store_generation", identity["store_generation"])):
                if span.get(key) != value:
                    raise C4Failure("SupportingSpanIdentityChanged")
            start, end = span.get("start"), span.get("end")
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body)
                    or passage_module.content_hash(body[start:end].strip()) != span.get("span_content_sha256")):
                raise C4Failure("SupportingSpanBytesChanged")
            text, mode = body[start:end], "exact_selected_source_span"
        elif row.get("match_source") == "original_parent":
            text, mode = body, "accepted_original_body"
        else:
            raise C4Failure("UnknownSupportingSource")
        inputs.append((text, mode))
    return inputs


class FrozenCrossEncoder:
    def __init__(self, directory, helpers, counters):
        verify_fixture(directory, helpers)
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        self.counters = counters
        self.tokenizer = Tokenizer.from_file(str(helpers.safe_member(directory, MODEL["tokenizer_file"])))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.pair_tokenizer = Tokenizer.from_file(str(helpers.safe_member(directory, MODEL["tokenizer_file"])))
        self.pair_tokenizer.no_truncation()
        self.pair_tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        if self.pair_tokenizer.num_special_tokens_to_add(is_pair=True) != 3:
            raise C4Failure("UnexpectedPairSpecialTokens")
        options = ort.SessionOptions()
        options.intra_op_num_threads = LIMITS["intra_op_threads"]
        options.inter_op_num_threads = LIMITS["inter_op_threads"]
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.log_severity_level = 3
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.session = ort.InferenceSession(str(helpers.safe_member(directory, MODEL["model_file"])),
                                           sess_options=options, providers=[LIMITS["provider"]])
        entries = self.session.get_inputs()
        outputs = self.session.get_outputs()
        self.names = {entry.name for entry in entries}
        if (not {"input_ids", "attention_mask"}.issubset(self.names)
                or not self.names.issubset({"input_ids", "attention_mask", "token_type_ids"})
                or any(entry.type != "tensor(int64)" for entry in entries)
                or len(outputs) != 1 or outputs[0].type != "tensor(float)"
                or self.session.get_providers() != [LIMITS["provider"]]):
            raise C4Failure("UnexpectedModelInputOrProvider")
        verify_fixture(directory, helpers)

    def score(self, query, inputs):
        if len(inputs) > LIMITS["parent_cap"]:
            raise C4Failure("C4PairCapExceeded")
        if not inputs:
            return [], {"scored_pairs": 0, "inference_batches": 0, "input_accounting": []}
        query_text, query_tokens, kept_query = bounded_prefix(query, self.tokenizer, LIMITS["maximum_query_tokens"])
        pairs, accounting = [], []
        for text, mode in inputs:
            passage, original_tokens, kept_tokens = bounded_prefix(text, self.tokenizer, LIMITS["maximum_passage_tokens"])
            if mode == "exact_selected_source_span" and original_tokens != kept_tokens:
                raise C4Failure("SelectedPassageExceedsTokenLimit")
            if not passage.strip():
                raise C4Failure("EmptySupportingPassage")
            pairs.append((query_text, passage))
            accounting.append({"source": mode, "query_tokens": query_tokens, "kept_query_tokens": kept_query,
                               "removed_query_characters": len(query) - len(query_text),
                               "passage_tokens": original_tokens, "kept_passage_tokens": kept_tokens,
                               "removed_passage_characters": len(text) - len(passage),
                               "input_sha256": hashlib.sha256(passage.encode("utf-8")).hexdigest()})
        encoded = self.pair_tokenizer.encode_batch(pairs)
        if any(len(item.ids) > LIMITS["maximum_pair_tokens"] for item in encoded):
            raise C4Failure("PairTokenCapExceeded")
        for item, account in zip(encoded, accounting):
            account["pair_tokens"] = len(item.ids)
        arrays = {"input_ids": self.np.asarray([item.ids for item in encoded], dtype=self.np.int64),
                  "attention_mask": self.np.asarray([item.attention_mask for item in encoded], dtype=self.np.int64),
                  "token_type_ids": self.np.asarray([item.type_ids for item in encoded], dtype=self.np.int64)}
        self.counters["ce_inference_attempted_batches"] += 1
        self.counters["ce_inference_attempted_pairs"] += len(inputs)
        outputs = self.session.run(None, {name: arrays[name] for name in self.names})
        self.counters["ce_inference_returned_batches"] += 1
        self.counters["ce_inference_returned_pairs"] += len(inputs)
        if len(outputs) != 1:
            raise C4Failure("UnexpectedModelOutputCount")
        logits = self.np.asarray(outputs[0])
        if logits.shape == (len(inputs), 1):
            logits = logits[:, 0]
        if logits.shape != (len(inputs),) or not self.np.isfinite(logits).all():
            raise C4Failure("InvalidRelevanceLogits")
        return [float(value) for value in logits], {"scored_pairs": len(inputs), "inference_batches": 1,
                                                   "input_accounting": accounting}

    def close(self):
        self.session = None
        self.tokenizer = None
        self.pair_tokenizer = None
        gc.collect()


def reorder_c4(candidates, scores):
    count = min(LIMITS["parent_cap"], len(candidates))
    if len(scores) != count or any(not math.isfinite(score) for score in scores):
        raise C4Failure("InvalidScoredPairAccounting")
    head = [dict(row, ce_score=float(score), ce_original_rank=index + 1, ce_configuration="c4-minilm-exact-passage")
            for index, (row, score) in enumerate(zip(candidates[:count], scores))]
    head.sort(key=lambda row: (-row["ce_score"], row["ce_original_rank"], row["id"]))
    return head + candidates[count:]


class PassageObserver:
    def __init__(self, args, helpers, passage_module):
        self.args, self.helpers, self.passage = args, helpers, passage_module
        self.frozen, self.measurements, self.counts = {}, [], Counter()
        for cap in (64, 96):
            rows = list(helpers.read_jsonl(helpers.safe_member(args.passage_run, f"results-C{cap}.jsonl")))
            self.frozen[cap] = {row["query_id"]: row for row in rows}
            if len(rows) != len(self.frozen[cap]):
                raise C4Failure("DuplicateImmutableQuery")
        if not self.frozen[64] or set(self.frozen[64]) != set(self.frozen[96]):
            raise C4Failure("ImmutableCellQueriesChanged")
        self.resource_passes = max(1, math.ceil(LIMITS["minimum_resource_samples_per_cell"] / len(self.frozen[64])))
        self.encoder = None

    def checked(self, query, cap, result):
        frozen = self.frozen[cap].get(query["query_id"])
        if (frozen is None or result["corpus_id"] != frozen["corpus_id"] or result["query_id"] != query["query_id"]
                or result["candidate_cap"] != cap or frozen["candidate_cap"] != cap):
            raise C4Failure("ImmutableQueryAccountingChanged")
        for field in ("raw_hybrid_top64", "final_sdk_top10"):
            assert_same_parents(result[field], frozen[field])
        budget = result["candidate_budget"]
        if (budget["parent_cap_violation"] or budget["parent_materialization_peak_count"] > budget["parent_candidate_cap"]
                or len(result["raw_hybrid_top64"]) > 64 or len(result["final_sdk_top10"]) > 10):
            raise C4Failure("ParentCapExceeded")
        return frozen

    def before_namespace(self, queries, retrieve, parent_mesh, source_records):
        self.parent_mesh, self.records = parent_mesh, {}
        for record in source_records:
            if record.get("faithful_public"):
                identifier = record["acceptance"]["memory_id"]
                previous = self.records.setdefault(identifier, record)
                if (previous["stored_sha256"] != record["stored_sha256"]
                        or previous["current_revision"] != record["current_revision"]
                        or any(previous["acceptance"].get(key) != record["acceptance"].get(key)
                               for key in ("memory_id", "incarnation", "store_generation"))):
                    raise C4Failure("AmbiguousSourceIdentity")
        warmups = queries[:LIMITS["warmup_queries_per_namespace"]]
        warm_candidates = []
        for query in warmups:
            for cap in (64, 96):
                result = retrieve(query, cap)
                self.checked(query, cap, result)
                verified_inputs(result["raw_hybrid_top64"], parent_mesh, self.records, query["namespace"], self.passage)
                if cap == 64:
                    warm_candidates.append((query, result["raw_hybrid_top64"]))
                self.counts["baseline_warmup_queries"] += 1
        if self.args.phase == "c4" and self.encoder is None:
            start = time.perf_counter()
            self.encoder = FrozenCrossEncoder(self.args.model_directory, self.helpers, self.counts)
            self.counts["session_load_count"] += 1
            self.counts["model_cold_load_ms"] = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            for query, candidates in warm_candidates:
                inputs = verified_inputs(candidates, parent_mesh, self.records, query["namespace"], self.passage)
                _, scoring = self.encoder.score(query["query"], inputs[:LIMITS["parent_cap"]])
                self.counts["warmup_scored_pairs"] += scoring["scored_pairs"]
                self.counts["warmup_inference_batches"] += scoring["inference_batches"]
                if verified_inputs(candidates, parent_mesh, self.records, query["namespace"], self.passage) != inputs:
                    raise C4Failure("SourceChangedDuringWarmupInference")
            self.counts["model_cold_warmup_ms"] = (time.perf_counter() - start) * 1000

    def evaluate(self, query, cap, retrieve, *, warmup=False, resource_pass=0):
        wall, cpu = time.perf_counter(), time.process_time()
        result = retrieve(query, cap)
        frozen = self.checked(query, cap, result)
        candidates = result["raw_hybrid_top64"]
        inputs = verified_inputs(candidates, self.parent_mesh, self.records, query["namespace"], self.passage)
        scoring = {"scored_pairs": 0, "inference_batches": 0, "input_accounting": []}
        prefix = "warmup_" if warmup else "measured_"
        if self.encoder is not None:
            scores, scoring = self.encoder.score(query["query"], inputs[:LIMITS["parent_cap"]])
            self.counts[prefix + "scored_pairs"] += scoring["scored_pairs"]
            self.counts[prefix + "inference_batches"] += scoring["inference_batches"]
            ranked = reorder_c4(candidates, scores)
            if (len(ranked) != len(candidates) or {row["id"] for row in ranked} != {row["id"] for row in candidates}
                    or ranked[LIMITS["parent_cap"]:] != candidates[LIMITS["parent_cap"]:]):
                raise C4Failure("C4OutputCandidateCapOrTailChanged")
            result.update(raw_hybrid_top64=ranked, final_sdk_top10=ranked[:10],
                          original_sdk_top10=frozen["final_sdk_top10"], ce_accounting=scoring)
        after = verified_inputs(candidates, self.parent_mesh, self.records, query["namespace"], self.passage)
        if inputs != after:
            raise C4Failure("SourceChangedDuringInference")
        measurement = {"query_id": query["query_id"], "candidate_cap": cap, "wall_ms": (time.perf_counter() - wall) * 1000,
                       "process_cpu_ms": (time.process_time() - cpu) * 1000,
                       "resource_pass": resource_pass, "quality_export": resource_pass == 0 and not warmup,
                       "pre_inference_authorized_parents": len(inputs), "post_inference_authorized_parents": len(after),
                       **scoring}
        if not warmup:
            self.measurements.append(measurement)
        return result

    def query(self, query, cap, retrieve):
        return self.evaluate(query, cap, retrieve)

    def after_namespace(self, queries, retrieve):
        for resource_pass in range(1, self.resource_passes):
            for query in queries:
                for cap in (64, 96):
                    self.evaluate(query, cap, retrieve, resource_pass=resource_pass)

    def finish(self, manifest, output):
        expected = {(query_id, cap, resource_pass) for cap, rows in self.frozen.items() for query_id in rows
                    for resource_pass in range(self.resource_passes)}
        actual = {(row["query_id"], row["candidate_cap"], row["resource_pass"]) for row in self.measurements}
        if expected != actual or len(actual) != len(self.measurements):
            raise C4Failure("IncompleteMeasuredQueries")
        if self.args.phase == "c4" and self.counts["session_load_count"] != 1:
            raise C4Failure("UnexpectedSessionReloadCount")
        if self.args.phase == "baseline" and self.counts["session_load_count"]:
            raise C4Failure("BaselineUnexpectedlyLoadedCE")
        if any(row["scored_pairs"] > LIMITS["parent_cap"] or row["inference_batches"] > 1 for row in self.measurements):
            raise C4Failure("InferenceCounterCapExceeded")
        if self.args.phase == "c4" and (
                self.counts["ce_inference_attempted_pairs"] != self.counts["warmup_scored_pairs"] + self.counts["measured_scored_pairs"]
                or self.counts["ce_inference_attempted_batches"] != self.counts["warmup_inference_batches"] + self.counts["measured_inference_batches"]):
            raise C4Failure("InferenceCounterAccountingChanged")
        with (output / "query-resources.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
            for row in self.measurements:
                stream.write(json.dumps(row, allow_nan=False) + "\n")
        manifest.update(c4_phase=self.args.phase, c4_inference=dict(self.counts), labels_in_ranking_inputs=False,
                        resource_passes=self.resource_passes,
                        quality_export_query_cells=sum(row["quality_export"] for row in self.measurements))

    def close(self):
        if self.encoder is not None:
            self.encoder.close()


def verify_protocol(args, helpers):
    protocol = json.loads(helpers.checked_path(args.protocol, must_exist=True, directory=False).read_text(encoding="utf-8"))
    if (protocol.get("version") != 1 or protocol.get("model") != MODEL or protocol.get("limits") != LIMITS
            or protocol.get("policy") != POLICY or protocol.get("resource_maxima") != RESOURCE_MAXIMA
            or protocol.get("base_protocol_sha256") != helpers.digest_file(args.base_protocol)
            or protocol.get("implementation_sha256") != implementation_hashes(args.repository, helpers)):
        raise C4Failure("FrozenC4ProtocolMismatch")
    return protocol


def measure(args, helpers):
    output = helpers.isolated_artifact_path(args.output, args.repository)
    existed, observer = output.exists(), None
    try:
        verify_protocol(args, helpers)
        for name in ("source_run", "passage_run", "dataset_directory", "embedding_model_directory"):
            setattr(args, name, helpers.isolated_artifact_path(getattr(args, name), args.repository, must_exist=True))
        args.model_directory = helpers.isolated_artifact_path(args.model_directory, args.repository)
        from passage_retrieval import main as passage_main
        import passage_retrieval

        observer = PassageObserver(args, helpers, passage_retrieval)
        argv = ["--repository", str(args.repository), "--source-run", str(args.source_run),
                "--dataset-directory", str(args.dataset_directory), "--protocol", str(args.base_protocol),
                "--model-directory", str(args.embedding_model_directory), "--helper-directory", str(Path(args.repository) / "benchmarks"),
                "--parent-budget", "96", "--output", str(args.output)]
        if args.dependency_directory:
            argv += ["--dependency-directory", str(args.dependency_directory)]
        passage_main(argv, experiment=observer)
    except BaseException as exc:
        # Preserve setup failures too; never overwrite a pre-existing output.
        if not existed:
            output.mkdir(parents=True, exist_ok=True)
            manifest_path = helpers.safe_member(output, "run-manifest.json", must_exist=False)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
            details = failure_details(exc)
            manifest.update(status="unavailable" if isinstance(exc, C4Unavailable) else "aborted" if isinstance(exc, (C4Aborted, KeyboardInterrupt)) else "failed",
                            failure_type=details.get("failure_code", details["exception_type"]), failure_details=details,
                            c4_failure_code=details.get("failure_code", details["exception_type"]))
            if observer is not None:
                manifest.update(c4_inference=dict(observer.counts), completed_measured_queries=len(observer.measurements),
                                completed_quality_query_cells=sum(row["quality_export"] for row in observer.measurements))
            helpers.write_json(manifest_path, manifest)
        raise


def terminate_tree(process, psutil, observed_children=()):
    try:
        descendants = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []
    descendants = list({child.pid: child for child in [*observed_children, *descendants]}.values())
    for child in reversed(descendants):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    psutil.wait_procs(descendants, timeout=5)


def supervised_measure(command, repository, rss_limit, phase_output, helpers):
    import psutil

    wrapper = psutil.Process()
    peak, samples, started, observed_children = 0, 0, time.monotonic(), {}
    environment = dict(os.environ, TOKENIZERS_PARALLELISM="false", OMP_NUM_THREADS="1",
                       OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1")
    process = subprocess.Popen(command, cwd=repository, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while process.poll() is None:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
                observed_children.update({child.pid: child for child in children})
                current = wrapper.memory_info().rss + sum(child.memory_info().rss for child in [psutil.Process(process.pid), *children])
            except psutil.NoSuchProcess:
                time.sleep(0.01)
                continue
            peak, samples = max(peak, current), samples + 1
            if current > rss_limit:
                raise C4Aborted("ProcessTreeRSSLimitExceeded", evidence={"process_tree_peak_rss_bytes": peak,
                    "process_tree_sample_count": samples, "rss_limit_bytes": rss_limit})
            if time.monotonic() - started > 1800:
                raise C4Aborted("PhaseWallDeadlineExceeded", evidence={"phase_wall_seconds": time.monotonic() - started,
                    "process_tree_peak_rss_bytes": peak, "process_tree_sample_count": samples})
            time.sleep(0.01)
        if process.returncode != 0:
            manifest_path = helpers.safe_member(phase_output, "run-manifest.json", must_exist=False)
            failure = {}
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                failure = failure_projection(manifest)
            raise C4Failure("MeasuredSubprocessFailed", evidence={"child_failure_diagnostics": failure,
                "child_returncode": process.returncode, "process_tree_peak_rss_bytes": peak,
                "process_tree_sample_count": samples})
        if not samples:
            raise C4Failure("MissingProcessTreeSamples")
        return {"process_tree_peak_rss_bytes": peak, "process_tree_sample_count": samples,
                "phase_wall_seconds": time.monotonic() - started}
    finally:
        terminate_tree(process, psutil, observed_children.values())


def run(args, helpers):
    output = helpers.isolated_artifact_path(args.output, args.repository)
    if output.exists():
        raise C4Failure("OutputAlreadyExists")
    output.mkdir(parents=True)
    metadata = {"status": "running", "configuration": "c4-minilm-exact-passage", "optional_default_enabled": False,
                "reranker_model": MODEL, "reranker_limits": LIMITS, "reranker_policy": POLICY,
                "all_queries_accounted": False, "resource_qualified": False, "paired_original_condition": "no_reranker_passages",
                "resource_gates": {name: {"observed": None, "maximum": maximum} for name, maximum in RESOURCE_MAXIMA.items()},
                "unscored_queries": None, "labels_in_ranking_inputs": False, "failure_type": None}
    helpers.write_json(output / "run-manifest.json", metadata)
    owned, active_phase = None, None
    try:
        verify_protocol(args, helpers)
        for name in ("source_run", "passage_run", "dataset_directory", "embedding_model_directory"):
            setattr(args, name, helpers.isolated_artifact_path(getattr(args, name), args.repository, must_exist=True))
        source_path = helpers.safe_member(args.source_run, "run-manifest.json")
        passage_path = helpers.safe_member(args.passage_run, "run-manifest.json")
        source = json.loads(source_path.read_text(encoding="utf-8"))
        passage = json.loads(passage_path.read_text(encoding="utf-8"))
        metadata.update(source_run_manifest_sha256=helpers.digest_file(source_path),
                        passage_run_manifest_sha256=helpers.digest_file(passage_path),
                        passage_output_sha256=passage["output_sha256"],
                        c4_protocol_sha256=helpers.digest_file(args.protocol), query_target=passage["query_target"],
                        unscored_queries=passage["query_target"] * 2)
        if (source["status"] != "complete" or passage["status"] != "complete" or not passage.get("all_queries_accounted")
                or passage["source_run_manifest_sha256"] != helpers.digest_file(source_path)
                or passage["protocol_sha256"] != helpers.digest_file(args.base_protocol)
                or passage["runtime_files"] != source["runtime_files"]
                or source["dataset_manifest_sha256"] != passage["dataset_manifest_sha256"]
                or passage["cache_condition_bytes"] != 256 * 1024**2
                or any(row["parent_cap_violation"] for row in passage["candidate_budget_observations"])):
            raise C4Failure("IncompleteOrMismatchedPassageComparator")
        for relative, expected in source["runtime_files"].items():
            if helpers.digest_file(helpers.safe_member(args.repository, relative)) != expected:
                raise C4Failure("RuntimeSourceHashMismatch")
        helpers.verify_exports(args.source_run, source)
        helpers.verify_exports(args.passage_run, passage)
        if (source["output_sha256"].get("query-vectors.jsonl") != passage["output_sha256"].get("query-vectors.jsonl")
                or source["output_sha256"].get("ingestion-fidelity.jsonl") != passage["output_sha256"].get("ingestion-fidelity.jsonl")
                or source["query_target"] != passage["query_target"]
                or source["embedding_model"] != passage["embedding_model"]
                or source["model_revision"] != passage["model_revision"]
                or source["query_model_space_tag"] != passage["query_model_space_tag"]):
            raise C4Failure("PassageSourceVectorsOrModelChanged")
        args.model_directory = helpers.isolated_artifact_path(args.model_directory, args.repository)
        verify_fixture(args.model_directory, helpers)
        owned = tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-c4-")
        temporary = Path(owned.name).resolve(strict=True)
        helpers.configure_isolation(temporary)
        base = [sys.executable, str(helpers.safe_member(Path(args.repository) / "benchmarks", "retrieval_c4.py")), "measure",
                "--repository", str(args.repository), "--source-run", str(args.source_run), "--passage-run", str(args.passage_run),
                "--dataset-directory", str(args.dataset_directory), "--base-protocol", str(args.base_protocol), "--protocol", str(args.protocol),
                "--model-directory", str(args.model_directory), "--embedding-model-directory", str(args.embedding_model_directory)]
        if args.dependency_directory:
            base += ["--dependency-directory", str(args.dependency_directory)]
        phases = {}
        for phase in ("baseline", "c4"):
            phase_output = temporary / phase
            limit = RESOURCE_MAXIMA["total_rss_mib"] * 1024**2
            if phase == "c4":
                limit = min(limit, phases["baseline"]["process_tree_peak_rss_bytes"] + RESOURCE_MAXIMA["delta_rss_mib"] * 1024**2)
            active_phase = phase
            phases[phase] = supervised_measure(base + ["--phase", phase, "--output", str(phase_output)],
                args.repository, limit, phase_output, helpers)
            active_phase = None
            phases[phase]["run_manifest_sha256"] = helpers.digest_file(helpers.safe_member(phase_output, "run-manifest.json"))
            metadata["matched_resource_phases"] = phases
            helpers.write_json(output / "run-manifest.json", metadata)
        baseline_output, c4_output = temporary / "baseline", temporary / "c4"
        baseline_samples = list(helpers.read_jsonl(helpers.safe_member(baseline_output, "query-resources.jsonl")))
        c4_samples = list(helpers.read_jsonl(helpers.safe_member(c4_output, "query-resources.jsonl")))
        gates, qualified, enough = resource_gates(baseline_samples, c4_samples,
            phases["baseline"]["process_tree_peak_rss_bytes"], phases["c4"]["process_tree_peak_rss_bytes"])
        metadata["warm_resource_summary"] = {
            phase: {str(cap): {"samples": sum(row["candidate_cap"] == cap for row in samples),
                "wall_p50_ms": nearest_rank([row["wall_ms"] for row in samples if row["candidate_cap"] == cap], 0.50),
                "wall_p95_ms": nearest_rank([row["wall_ms"] for row in samples if row["candidate_cap"] == cap], 0.95),
                "wall_p99_ms": nearest_rank([row["wall_ms"] for row in samples if row["candidate_cap"] == cap], 0.99),
                "mean_process_cpu_ms": sum(row["process_cpu_ms"] for row in samples if row["candidate_cap"] == cap)
                    / sum(row["candidate_cap"] == cap for row in samples)} for cap in (64, 96)}
            for phase, samples in (("baseline", baseline_samples), ("c4", c4_samples))}
        final = json.loads(helpers.safe_member(c4_output, "run-manifest.json").read_text(encoding="utf-8"))
        helpers.verify_exports(c4_output, final)
        if final["status"] != "complete" or final["runtime_files"] != source["runtime_files"]:
            raise C4Failure("IncompleteC4Export")
        base_protocol = json.loads(args.base_protocol.read_text(encoding="utf-8"))
        metadata.update(final, status="running", resource_gates=gates, resource_qualified=qualified,
                        resource_sample_count_sufficient=enough, unscored_queries=0,
                        paired_original_condition="no_reranker_passages", optional_default_enabled=False,
                        model_file_sha256={key: base_protocol["model"][key] for key in ("onnx_sha256", "tokenizer_sha256")})
        import shutil

        for relative in final["output_sha256"]:
            shutil.copyfile(helpers.safe_member(c4_output, relative), helpers.safe_member(output, relative, must_exist=False))
        metadata["baseline_query_resource_sha256"] = helpers.digest_file(helpers.safe_member(baseline_output, "query-resources.jsonl"))
        helpers.verify_exports(args.source_run, source)
        helpers.verify_exports(args.passage_run, passage)
        verify_protocol(args, helpers)
        verify_fixture(args.model_directory, helpers)
        if not qualified:
            raise C4Failure("InsufficientResourceSamples" if not enough else "ResourceGateRejected")
        metadata["status"] = "complete"
        metadata["quality_scoring_status"] = "exports_fixed_ready_for_separate_scorer"
    except BaseException as exc:
        details = failure_details(exc, measured_phase=active_phase)
        metadata.update(status="unavailable" if isinstance(exc, C4Unavailable) else "aborted" if isinstance(exc, (C4Aborted, KeyboardInterrupt)) else "failed",
                        failure_type=details.get("failure_code", details["exception_type"]), failure_details=details,
                        all_queries_accounted=False)
        metadata["quality_scoring_status"] = "not_run_optional_configuration_failed"
        if "failure_evidence" in details:
            metadata["failure_evidence"] = details["failure_evidence"]
        raise
    finally:
        if owned is not None:
            root = Path(owned.name)
            metadata["owned_cleanup_files"] = sorted(item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file())
            owned.cleanup()
        helpers.write_json(output / "run-manifest.json", metadata)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="Freeze implementation, fixture, token, candidate and resource policy before C4 execution")
    freeze.add_argument("--repository", type=Path, required=True)
    freeze.add_argument("--base-protocol", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    for name in ("run", "measure"):
        command = commands.add_parser(name, help="Run matched remote-only C4 evaluation" if name == "run" else argparse.SUPPRESS)
        for option in ("repository", "source-run", "passage-run", "dataset-directory", "base-protocol", "protocol",
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
                                   "resource_maxima": RESOURCE_MAXIMA, "base_protocol_sha256": helpers.digest_file(args.base_protocol),
                                   "implementation_sha256": implementation_hashes(args.repository, helpers)})
    elif args.command == "measure":
        measure(args, helpers)
    else:
        run(args, helpers)
    return 0


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise C4Aborted("Interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        raise SystemExit(main())
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        print(json.dumps({"status": "unavailable" if isinstance(exc, C4Unavailable) else "aborted" if isinstance(exc, (C4Aborted, KeyboardInterrupt)) else "failed",
                          "failure_type": str(exc) if isinstance(exc, C4Failure) else type(exc).__name__}), flush=True)
        raise SystemExit(2 if isinstance(exc, C4Unavailable) else 130 if isinstance(exc, (C4Aborted, KeyboardInterrupt)) else 1) from None
