"""Actions-only, pinned E5 shadow comparison through the ordinary Mesh SDK.

The production loader and defaults are unchanged. Static preparation is separate
from native measurement; relevance labels are opened only by the existing scorer.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import contextlib
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

import production_retrieval as production
import replay_retrieval as helpers
from retrieval_c4 import C4Failure, supervised_measure
from runtime_rerank_c4 import resource_gates

REVISION = "02af79985278377e65c724a76275707cb0333c70"
MODEL = {
    "repository": "Xenova/e5-small-v2", "revision": REVISION,
    "upstream_repository": "intfloat/e5-small-v2",
    "upstream_card_revision": "ffb93f3bd4047442299a41ebb6fa998a38507c52",
    "license": "MIT", "language": "en", "dimension": 384,
    "files": {
        "model.onnx": {"remote": "onnx/model_quantized.onnx", "bytes": 34014367,
                       "sha256": "7d9092cb25f2bd1c023b7e8d2aa459044a02030ac880e5a59fdaf27af69f1ded"},
        "tokenizer.json": {"remote": "tokenizer.json", "bytes": 711396,
                           "sha256": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"},
    },
}
PROFILE = {
    "version": "e5-bounded256-window-mean-v1", "model": MODEL,
    "roles": {"query": "query: ", "document": "passage: "},
    "normalization": "SDK whitespace collapse, no extra case or lexical rewriting",
    "maximum_sequence_tokens": 256, "content_window_tokens": 224, "window_stride_tokens": 192,
    "windowing": "unprefixed tokenizer offsets; prefix every full-coverage window; reject truncation",
    "pooling": "attention-masked mean token output, L2 each window, mean windows then L2",
    "provider": "CPUExecutionProvider", "threads": "same DirectOnnx settings as the default model",
    "sdk": "Fresh Mesh.recall n=10, no precomputed vectors, ordinary access recording on the quality pass; separate n=1 parity",
    "resources": "normal-cache and cache-cold strata; max(1,ceil(100/query_target)) complete matched passes each",
    "model_swap_limits": {"total_rss_mib": 768, "delta_rss_mib": 128, "delta_p95_ms": 50, "delta_p99_ms": 100, "delta_cpu_ms": 50},
}


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


PROFILE_SHA256 = identity(PROFILE)
MODEL_TAG = "onnx-e5-small-v2-q8-" + PROFILE_SHA256
STRATA = ("normal-cache", "cache-cold")
IMPLEMENTATIONS = ("embedding_shadow.py", "retrieval_ci.py", "production_retrieval.py",
                   "replay_retrieval.py", "retrieval_c4.py", "runtime_rerank_c4.py", "retrieval_c4_model.py")
FAILURE_CODES = frozenset({
    "ShadowActionsRequired", "ShadowPinnedFixtureMismatch", "ShadowBaselineFixtureMismatch",
    "ShadowTokenizerLostContent", "ShadowWindowCoverageMismatch", "ShadowUnexpectedModelContract",
    "ShadowPrefixWouldTruncate", "ShadowRetargetIdentityMismatch", "ShadowJobTargetMismatch",
    "ShadowIndexingNoProgress", "ShadowOriginalParentSetChanged", "ShadowRevisionModelReadinessMismatch",
    "ShadowPendingJobs", "ShadowQuerySpaceMismatch", "ShadowQueryAttemptedDocumentInference",
    "ShadowSDKEncodingAccountingMismatch", "ShadowFreshNativeEncodingMissing", "ShadowNativeEncodingIncomplete",
    "ShadowFirstResultParityMismatch", "ShadowPairedCorpusMismatch", "ShadowRuntimeSourceMismatch",
    "ShadowImportedRuntimeMismatch", "ShadowQueryAccountingMismatch", "ShadowSourceOrFixtureMismatch",
    "ShadowFrozenSnapshotChanged", "ShadowCurrentDefaultRanksChanged", "ShadowImplementationChanged",
    "ShadowResourceGateRejected", "ShadowUnqualifiedScoringForbidden", "MeasuredSubprocessFailed",
    "ProcessTreeRSSLimitExceeded", "PhaseWallDeadlineExceeded",
})


def verify_fixture(directory):
    for name, expected in MODEL["files"].items():
        path = helpers.safe_member(directory, name)
        if path.stat().st_size != expected["bytes"] or helpers.digest_file(path) != expected["sha256"]:
            raise C4Failure("ShadowPinnedFixtureMismatch")


def verify_baseline_fixture(directory):
    from provision_production_model import FILES
    for name, (_remote, size, digest) in FILES.items():
        path = helpers.safe_member(directory, name)
        if path.stat().st_size != size or helpers.digest_file(path) != digest:
            raise C4Failure("ShadowBaselineFixtureMismatch")


def provision(directory, repository):
    # Native files can only be provisioned in Actions, never on the local machine.
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise C4Failure("ShadowActionsRequired")
    from prepare_retrieval_ci import verified_download
    directory = helpers.isolated_artifact_path(directory, repository)
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in MODEL["files"].items():
        verified_download(f"https://huggingface.co/{MODEL['repository']}/resolve/{REVISION}/{expected['remote']}",
                          helpers.safe_member(directory, name, must_exist=False), expected["sha256"],
                          maximum_bytes=expected["bytes"])
    verify_fixture(directory)


def token_windows(text, tokenizer):
    encoding = tokenizer.encode(text, add_special_tokens=False)
    offsets = encoding.offsets
    if not offsets:
        if text.strip():
            raise C4Failure("ShadowTokenizerLostContent")
        return [text]
    spans = []
    for start in range(0, len(offsets), PROFILE["window_stride_tokens"]):
        end = min(len(offsets), start + PROFILE["content_window_tokens"])
        spans.append((offsets[start][0], offsets[end - 1][1]))
        if end == len(offsets):
            break
    covered_to = 0
    for left, right in spans:
        if not 0 <= left < right <= len(text) or any(not char.isspace() for char in text[covered_to:left]):
            raise C4Failure("ShadowWindowCoverageMismatch")
        covered_to = max(covered_to, right)
    if any(not char.isspace() for char in text[covered_to:]):
        raise C4Failure("ShadowWindowCoverageMismatch")
    return [text[left:right] for left, right in spans]


class ObservedSession:
    def __init__(self, session):
        self.session, self.counts = session, Counter()

    def __getattr__(self, name):
        return getattr(self.session, name)

    def run(self, outputs, inputs):
        self.counts["native_attempted_batches"] += 1
        self.counts["native_attempted_windows"] += len(inputs["input_ids"])
        result = self.session.run(outputs, inputs)
        self.counts["native_returned_batches"] += 1
        self.counts["native_returned_windows"] += len(inputs["input_ids"])
        return result


class E5ShadowEmbedding:
    """Verified benchmark adapter; no production model discovery or selection."""
    model_tag = MODEL_TAG
    model_revision = REVISION
    embedding_preprocessing_identity = PROFILE_SHA256

    def __init__(self, directory, memory):
        verify_fixture(directory)
        self.model_path = helpers.safe_member(directory, "model.onnx")
        self.tokenizer_path = helpers.safe_member(directory, "tokenizer.json")
        self.engine = memory._DirectOnnxEmbedding(self.model_path, self.tokenizer_path)
        self.engine.session = ObservedSession(self.engine.session)
        entries, outputs = self.engine.session.get_inputs(), self.engine.session.get_outputs()
        if (not {"input_ids", "attention_mask"}.issubset(self.engine.input_names)
                or not self.engine.input_names.issubset({"input_ids", "attention_mask", "token_type_ids"})
                or any(entry.type != "tensor(int64)" for entry in entries)
                or len(outputs) != 1 or outputs[0].type != "tensor(float)"
                or len(outputs[0].shape) != 3 or outputs[0].shape[-1] != 384
                or self.engine.session.get_providers() != [PROFILE["provider"]]):
            raise C4Failure("ShadowUnexpectedModelContract")
        self.memory, self.coverage = memory, Counter()
        verify_fixture(directory)

    @property
    def session(self):
        return self.engine.session

    def encode_for_role(self, texts, *, role, batch_size):
        import numpy as np
        if role not in PROFILE["roles"]:
            raise ValueError("Embedding role must be document or query")
        if not texts:
            return np.empty((0, 384), dtype=np.float32)
        windows, ranges = [], []
        for text in texts:
            pieces = token_windows(text, self.engine.window_tokenizer)
            start = len(windows)
            for piece in pieces:
                prefixed = PROFILE["roles"][role] + piece
                tokens = self.engine.window_tokenizer.encode(prefixed).ids
                if len(tokens) > PROFILE["maximum_sequence_tokens"]:
                    raise C4Failure("ShadowPrefixWouldTruncate")
                windows.append(prefixed)
                self.coverage["validated_windows"] += 1
                self.coverage["maximum_sequence_tokens"] = max(self.coverage["maximum_sequence_tokens"], len(tokens))
            ranges.append((start, len(windows)))
            self.coverage[role + "_full_coverage_inputs"] += 1
        vectors = self.engine._encode_raw_batches(windows, batch_size=batch_size)
        return np.vstack([self.memory._normalize_rows(vectors[left:right].mean(axis=0)) for left, right in ranges])


def implementation_hashes(repository):
    return {"benchmarks/" + name: helpers.digest_file(helpers.safe_member(repository, "benchmarks/" + name))
            for name in IMPLEMENTATIONS}


def clear_query_caches(memory):
    with memory._EMBED_CACHE_LOCK:
        memory._EMBED_CACHE.clear()
        memory._EMBED_CACHE_BYTES = 0
    with memory._NORM_QUERY_CACHE_LOCK:
        memory._NORM_QUERY_CACHE.clear()
        memory._NORM_QUERY_CACHE_BYTES = 0


def original_parent_identities(records, expected_tag):
    identities = production.runtime_parent_identities(records)
    return {identifier: dict(item, embedding_model=expected_tag) for identifier, item in identities.items()}


def validate_returned_parents(rows, records, expected_tag):
    production.validate_runtime_parent_results(rows, original_parent_identities(records, expected_tag), allow_passages=False)


def grouped_parent_identities(records, expected_tag):
    groups = defaultdict(list)
    for record in records:
        groups[record["corpus_id"]].append(record)
    return {corpus: original_parent_identities(group, expected_tag) for corpus, group in groups.items()}


def failed_native_phase(exc, elapsed_seconds, ceiling):
    # Exception text, filenames and arbitrary nested diagnostics are never copied.
    result = {"status": "failed",
              "phase_wall_seconds": elapsed_seconds if type(elapsed_seconds) in {int, float} and math.isfinite(elapsed_seconds) and elapsed_seconds >= 0 else None,
              "rss_limit_bytes": ceiling if type(ceiling) is int and ceiling >= 0 else None}
    evidence = getattr(exc, "evidence", None)
    if isinstance(evidence, dict):
        for name in ("process_tree_peak_rss_bytes", "process_tree_sample_count", "rss_limit_bytes", "child_returncode"):
            value = evidence.get(name)
            if type(value) is int and (value >= 0 or name == "child_returncode"):
                result[name] = value
        value = evidence.get("phase_wall_seconds")
        if type(value) in {int, float} and math.isfinite(value) and value >= 0:
            result["phase_wall_seconds"] = value
    if isinstance(exc, C4Failure) and exc.args and isinstance(exc.args[0], str) and exc.args[0] in FAILURE_CODES:
        result["failure_code"] = exc.args[0]
    return result


def reembed(mesh, records, memory):
    indexed = []
    for identifier in sorted({row["acceptance"]["memory_id"] for row in records if row["acceptance"].get("memory_id")}):
        row = mesh.get(identifier, include_private=True)
        if row is None or not mesh.store.enqueue_embedding_job(
                identifier, expected_revision=row["revision"], expected_incarnation=row["incarnation"],
                expected_store_generation=mesh.store.store_generation, target_model=MODEL_TAG, target_representation=""):
            raise C4Failure("ShadowRetargetIdentityMismatch")
        indexed.append({"id": identifier, "revision": row["revision"], "incarnation": row["incarnation"],
                        "store_generation": mesh.store.store_generation, "source_embedding_model": row["embedding_model"],
                        "target_model": MODEL_TAG, "content_sha256": production.digest_text(row["content"]),
                        "local_only": row["local_only"], "quarantined": row["quarantined"]})
    no_progress = 0
    while (before := mesh.store.embedding_job_stats()["pending_count"]):
        jobs = mesh.store.list_embedding_jobs(limit=32)
        if not jobs or any(job["target_model"] != MODEL_TAG or job["target_representation"] for job in jobs):
            raise C4Failure("ShadowJobTargetMismatch")
        hints = [memory._ReembedHint(str(mesh.store.db_path.parent), mesh.namespace, job["memory_id"], job["revision"],
                                    job["target_model"], job["attempts"], incarnation=job["incarnation"],
                                    store_generation=job["store_generation"]) for job in jobs]
        memory._process_reembed_batch(hints)
        after = mesh.store.embedding_job_stats()["pending_count"]
        no_progress = no_progress + 1 if after >= before else 0
        if no_progress >= 3:
            raise C4Failure("ShadowIndexingNoProgress")
    validate_index(mesh, indexed, MODEL_TAG)
    return indexed


def validate_index(mesh, indexed, expected_model):
    with mesh.store._read_connect() as connection:
        rows = {row["id"]: row for row in connection.execute("SELECT * FROM memories WHERE namespace=?", (mesh.namespace,))}
        if set(rows) != {item["id"] for item in indexed} or connection.execute("SELECT count(*) FROM memory_passages").fetchone()[0]:
            raise C4Failure("ShadowOriginalParentSetChanged")
    for item in indexed:
        row = rows[item["id"]]
        if (row["embedding_model"] != expected_model or row["embedding"] is None
                or any(row[key] != item[key] for key in ("revision", "incarnation", "local_only", "quarantined"))
                or row["passage_format_version"] or row["passage_count"]
                or mesh.store.store_generation != item["store_generation"]
                or production.digest_text(row["content"]) != item["content_sha256"]):
            raise C4Failure("ShadowRevisionModelReadinessMismatch")
    for offset in range(0, len(indexed), 128):
        batch = indexed[offset:offset + 128]
        readiness = mesh.wait_for_indexed([item["id"] for item in batch], timeout=0, include_private=True,
                                         expected_revisions={item["id"]: item["revision"] for item in batch},
                                         expected_incarnations={item["id"]: item["incarnation"] for item in batch},
                                         expected_store_generation=mesh.store.store_generation, target_model=expected_model)
        if readiness["status"] != "indexed" or len(readiness["indexed"]) != len(batch):
            raise C4Failure("ShadowRevisionModelReadinessMismatch")
    if mesh.store.embedding_job_stats()["pending_count"]:
        raise C4Failure("ShadowPendingJobs")


def measure_queries(mesh, queries, records, memory, model, output, metadata, baseline):
    by_memory = defaultdict(list)
    for row in records:
        if row["acceptance"].get("memory_id"):
            by_memory[row["acceptance"]["memory_id"]].append(row)
    passes = max(1, math.ceil(100 / metadata["query_target"]))
    original_query = memory._embed_query_with_tag
    query_requests = []

    def observe_query(query):
        vector, tag = original_query(query)
        if tag != metadata["query_model_space_tag"] or len(vector) != 384:
            raise C4Failure("ShadowQuerySpaceMismatch")
        query_requests.append(production.digest_text(query))
        return vector, tag

    def forbid_documents(*_args, **_kwargs):
        raise C4Failure("ShadowQueryAttemptedDocumentInference")

    with contextlib.ExitStack() as stack:
        old_documents, old_worker = memory._encode_texts_with_tag, memory._process_reembed_batch
        memory._embed_query_with_tag, memory._encode_texts_with_tag = observe_query, forbid_documents
        memory._process_reembed_batch = forbid_documents
        stack.callback(setattr, memory, "_embed_query_with_tag", original_query)
        stack.callback(setattr, memory, "_encode_texts_with_tag", old_documents)
        stack.callback(setattr, memory, "_process_reembed_batch", old_worker)
        rank_stream = stack.enter_context((output / "results-default.jsonl").open("a", encoding="utf-8", newline="\n"))
        sample_stream = stack.enter_context((output / "shadow-resources.jsonl").open("a", encoding="utf-8", newline="\n"))
        for stratum in STRATA:
            clear_query_caches(memory)
            for resource_pass in range(passes):
                for query in queries:
                    if stratum == "cache-cold":
                        clear_query_caches(memory)
                    before_requests, before_native = len(query_requests), dict(model.session.counts)
                    started, cpu = time.perf_counter(), time.process_time()
                    results = mesh.recall(query["query"], n=10, include_private=False, include_quarantined=False,
                                          record_access=stratum == STRATA[0] and resource_pass == 0)
                    wall_ms, cpu_ms = (time.perf_counter() - started) * 1000, (time.process_time() - cpu) * 1000
                    if len(query_requests) != before_requests + 1 or query_requests[-1] != production.digest_text(query["query"]):
                        raise C4Failure("ShadowSDKEncodingAccountingMismatch")
                    metadata["sdk_query_encoding_requests"] += 1
                    delta = {key: value - before_native.get(key, 0) for key, value in model.session.counts.items()}
                    if stratum == "cache-cold" and delta.get("native_returned_batches", 0) < 1:
                        raise C4Failure("ShadowFreshNativeEncodingMissing")
                    if delta.get("native_attempted_batches", 0) != delta.get("native_returned_batches", 0):
                        raise C4Failure("ShadowNativeEncodingIncomplete")
                    production.append_jsonl(sample_stream, {"query_id": query["id"], "resource_pass": resource_pass,
                                                           "stratum": stratum, "wall_ms": wall_ms, "process_cpu_ms": cpu_ms,
                                                           "native": delta, "query_vector_cache_hit": not bool(delta.get("native_returned_batches")),
                                                           "sdk_query_encoding_requests": 1})
                    if stratum == STRATA[0] and resource_pass == 0:
                        exported = production.decorate_results(results, by_memory, score_components=True)
                        one = mesh.recall(query["query"], n=1, include_private=False, include_quarantined=False, record_access=False)
                        metadata["sdk_query_encoding_requests"] += 1
                        if production.decorate_results(one, by_memory, score_components=True) != exported[:1] or len(query_requests) != before_requests + 2:
                            raise C4Failure("ShadowFirstResultParityMismatch")
                        payload = {"query_id": query["id"], "corpus_id": query.get("corpus", "complete"), "candidate_cap": None,
                                   "query_embedding_model": metadata["query_model_space_tag"], "final_sdk_top10": exported}
                        if baseline is not None:
                            before = baseline[query["id"]]
                            if before["corpus_id"] != payload["corpus_id"]:
                                raise C4Failure("ShadowPairedCorpusMismatch")
                            payload["original_sdk_top10"] = before["final_sdk_top10"]
                        production.append_jsonl(rank_stream, payload)
                        metadata["completed_queries"] += 1
                    metadata["native_counts"] = dict(model.session.counts)
                    helpers.write_json(output / "run-manifest.json", metadata)


def measure(args):
    output = helpers.isolated_artifact_path(args.output, args.repository)
    output.mkdir(parents=True, exist_ok=False)
    source = json.loads(helpers.safe_member(args.source_run, "run-manifest.json").read_text(encoding="utf-8"))
    metadata = {"status": "running", "dataset": args.dataset, "query_target": source["query_target"],
                "dataset_manifest_sha256": source["dataset_manifest_sha256"], "runtime_files": source["runtime_files"],
                "source_run_manifest_sha256": helpers.digest_file(helpers.safe_member(args.source_run, "run-manifest.json")),
                "profile_sha256": PROFILE_SHA256, "profile": PROFILE, "phase": args.phase,
                "query_model_space_tag": MODEL_TAG if args.phase == "e5" else production.MODEL_TAG,
                "labels_in_ranking_inputs": False, "default_changed": False, "passage_index_enabled": False,
                "baseline_provenance": "Same-source actual SDK model-swap comparison on identical original parents.",
                "ingestion": source["ingestion"], "completed_queries": 0, "sdk_query_encoding_requests": 0,
                "all_queries_accounted": False, "output_sha256": {}, "indexing": {}}
    helpers.write_json(output / "run-manifest.json", metadata)
    try:
        with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-e5-native-") as owned:
            root = Path(owned).resolve(strict=True)
            helpers.configure_isolation(root)
            os.environ.update(MESH_FORCE_HASH_EMBED="0", MESH_SYNC_ONNX="1", MESH_QUERY_MODE="neural",
                              MESH_REQUIRE_MODEL="1", MESH_PASSAGE_INDEX="0", MESH_RERANK_MODEL_DIR="")
            implementation = root / "implementation"
            if helpers.freeze_runtime(args.repository, implementation) != source["runtime_files"]:
                raise C4Failure("ShadowRuntimeSourceMismatch")
            sys.path.insert(0, str(implementation))
            import mesh.audit as audit
            import mesh.memory as memory
            import mesh.store as store
            if Path(memory.__file__).resolve().parent != implementation / "mesh" or store.DEFAULT_MAX_VECTOR_CACHE_BYTES != 256 * 1024**2:
                raise C4Failure("ShadowImportedRuntimeMismatch")
            memory._trigger_background_bootstrap = lambda: None
            if args.phase == "baseline":
                verify_baseline_fixture(args.embedding_model_directory)
            model = (E5ShadowEmbedding(args.model_directory, memory) if args.phase == "e5"
                     else memory._DirectOnnxEmbedding(helpers.safe_member(args.embedding_model_directory, "model.onnx"),
                                                      helpers.safe_member(args.embedding_model_directory, "tokenizer.json")))
            if args.phase == "baseline":
                model.session = ObservedSession(model.session)
            memory._model, memory._model_backend = model, "onnx"
            metadata["dependencies"] = {name: importlib.metadata.version(name) for name in ("numpy", "onnxruntime", "tokenizers", "apsw")}
            records = list(helpers.read_jsonl(helpers.safe_member(args.source_run, "ingestion-fidelity.jsonl")))
            shutil.copyfile(helpers.safe_member(args.source_run, "ingestion-fidelity.jsonl"), output / "ingestion-fidelity.jsonl")
            queries = list(helpers.read_jsonl(helpers.safe_member(args.dataset_directory, "queries-selected.jsonl")))
            baseline = ({row["query_id"]: row for row in helpers.read_jsonl(helpers.safe_member(args.baseline_run, "results-default.jsonl"))}
                        if args.phase == "e5" else None)
            groups = [(query["corpus"], [query]) for query in queries] if args.dataset == "longmemeval" else [("complete", queries)]
            for corpus_id, group in groups:
                namespace = args.namespaces[corpus_id]
                selected = [row for row in records if row["corpus_id"] == corpus_id]
                production.copy_guarded_corpus(args.source_run, args.snapshot_home, namespace, root, selected, store)
                mesh = memory.Mesh(namespace=namespace, agent_id="offline-e5-shadow")
                try:
                    metadata["sqlite"] = production.verify_sqlite(mesh, store)
                    started, cpu = time.perf_counter(), time.process_time()
                    if args.phase == "e5":
                        indexed = reembed(mesh, selected, memory)
                    else:
                        production.verify_guarded_readiness(mesh, selected, memory)
                        indexed = [{"id": row["acceptance"]["memory_id"], "revision": row["current_revision"],
                                    "incarnation": row["current_incarnation"], "store_generation": row["store_generation"],
                                    "local_only": row["private"], "quarantined": row["quarantined"],
                                    "content_sha256": row["stored_sha256"]}
                                   for row in {row["acceptance"]["memory_id"]: row for row in selected if row["acceptance"].get("memory_id")}.values()]
                        validate_index(mesh, indexed, production.MODEL_TAG)
                    metadata["indexing"][corpus_id] = {"parents": len(indexed), "wall_seconds": time.perf_counter() - started,
                                                       "process_cpu_seconds": time.process_time() - cpu, "pending_jobs": 0}
                    with (output / "shadow-indexing.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
                        for item in indexed:
                            production.append_jsonl(stream, dict(item, corpus_id=corpus_id))
                    measure_queries(mesh, group, selected, memory, model, output, metadata, baseline)
                    validate_index(mesh, indexed, metadata["query_model_space_tag"])
                finally:
                    production.close_namespace(mesh, audit, memory, store)
            metadata["native_counts"] = dict(model.session.counts)
            metadata["coverage_counts"] = dict(model.coverage) if args.phase == "e5" else None
            metadata["all_queries_accounted"] = metadata["completed_queries"] == metadata["query_target"]
            if not metadata["all_queries_accounted"]:
                raise C4Failure("ShadowQueryAccountingMismatch")
            metadata["status"] = "complete"
    except BaseException as exc:
        metadata.update(status="failed", failure_type=type(exc).__name__)
        if isinstance(exc, C4Failure) and exc.args and exc.args[0] in FAILURE_CODES:
            metadata["failure_code"] = exc.args[0]
        raise
    finally:
        metadata["output_sha256"] = {path.name: helpers.digest_file(path) for path in output.glob("*.jsonl")}
        helpers.write_json(output / "run-manifest.json", metadata)


def freeze_shadow_snapshots(source_home, snapshot, namespaces, sqlite3):
    fingerprints = {}
    for corpus_id, namespace in namespaces.items():
        database = helpers.safe_member(source_home, "mesh_" + namespace + ".db")
        frozen_database = helpers.safe_member(snapshot, database.name, must_exist=False)
        if frozen_database.exists():
            raise C4Failure("ShadowFrozenSnapshotChanged")
        source_db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(str(frozen_database))
        try:
            source_db.backup(target)
            # Finish the owned immutable copy before hashing. Read-only opens
            # of a WAL-mode backup can otherwise create an empty WAL file.
            if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise C4Failure("ShadowFrozenSnapshotChanged")
        finally:
            target.close()
            source_db.close()
        fingerprint = production.guarded_database_fingerprint(snapshot, namespace)
        if fingerprint["wal_sha256"] is not None:
            raise C4Failure("ShadowFrozenSnapshotChanged")
        fingerprints[corpus_id] = fingerprint
    return fingerprints


def run(args):
    """Outer retrieval_ci supplies cumulative wall/CPU supervision and qualification."""
    from retrieval_ci import hashes, qualify
    from retrieval_c4_model import RESOURCE_MAXIMA
    output = helpers.isolated_artifact_path(args.output, args.repository)
    output.mkdir(parents=True, exist_ok=False)
    metadata = {"status": "running", "profile": PROFILE, "profile_sha256": PROFILE_SHA256,
                "labels_in_ranking_inputs": False, "default_changed": False, "resource_qualified": False,
                "all_queries_accounted": False, "quality_scoring_status": "not_run", "phases": {}, "resource_gates": {}}
    helpers.write_json(output / "run-manifest.json", metadata)
    try:
        metadata["qualification"] = qualify(args.repository, args.qualification_directory, args.floor_qualification_directory)
        verify_fixture(args.model_directory)
        source = json.loads(helpers.safe_member(args.source_run, "run-manifest.json").read_text(encoding="utf-8"))
        fresh = json.loads(helpers.safe_member(args.fresh_default_run, "run-manifest.json").read_text(encoding="utf-8"))
        fixture = production.verify_manifest(helpers.safe_member(args.dataset_directory, "manifest.json"))
        if (source["status"] != "complete" or fresh["status"] != "complete" or source["runtime_files"] != hashes(args.repository)
                or fresh["runtime_files"] != source["runtime_files"] or source["dataset_manifest_sha256"] != helpers.digest_file(helpers.safe_member(args.dataset_directory, "manifest.json"))
                or source["dataset"] != args.dataset or fixture["query_count"] != source["query_target"]
                or fresh["source_run_manifest_sha256"] != helpers.digest_file(helpers.safe_member(args.source_run, "run-manifest.json"))):
            raise C4Failure("ShadowSourceOrFixtureMismatch")
        helpers.verify_exports(args.source_run, source)
        helpers.verify_exports(args.fresh_default_run, fresh)
        metadata.update(runtime_files=source["runtime_files"], implementation_sha256=implementation_hashes(args.repository),
                        dataset_manifest_sha256=source["dataset_manifest_sha256"], query_target=source["query_target"],
                        source_run_manifest_sha256=helpers.digest_file(helpers.safe_member(args.source_run, "run-manifest.json")))
        verify_baseline_fixture(args.embedding_model_directory)
        records = list(helpers.read_jsonl(helpers.safe_member(args.source_run, "query-vectors.jsonl")))
        namespaces = {row["corpus_id"]: row["namespace"] for row in records}
        source_home = helpers.isolated_source_home(source["temporary_mesh_home"])
        with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-e5-pair-") as owned:
            root = Path(owned).resolve(strict=True)
            snapshot = root / "snapshot-home"
            snapshot.mkdir()
            # Snapshot both phases from the same identity before either SDK phase.
            from mesh.sqlite_runtime import sqlite3
            fingerprints = freeze_shadow_snapshots(source_home, snapshot, namespaces, sqlite3)
            metadata["snapshot_fingerprints"] = fingerprints
            base = [sys.executable, str(helpers.safe_member(args.repository, "benchmarks/embedding_shadow.py")), "measure", args.dataset]
            for name in ("repository", "source_run", "fresh_default_run", "dataset_directory", "model_directory", "embedding_model_directory"):
                base += ["--" + name.replace("_", "-"), str(getattr(args, name))]
            base += ["--snapshot-home", str(snapshot), "--namespaces", json.dumps(namespaces, sort_keys=True)]
            phases = {}
            fidelity_records = list(helpers.read_jsonl(helpers.safe_member(args.source_run, "ingestion-fidelity.jsonl")))
            for phase in ("baseline", "e5"):
                phase_output = output / phase
                ceiling = RESOURCE_MAXIMA["total_rss_mib"] * 1024**2
                if phase == "e5":
                    ceiling = min(ceiling, phases["baseline"]["process_tree_peak_rss_bytes"] + RESOURCE_MAXIMA["delta_rss_mib"] * 1024**2)
                command = base + ["--phase", phase, "--output", str(phase_output)]
                if phase == "e5":
                    command += ["--baseline-run", str(output / "baseline")]
                metadata["active_phase"] = phase
                started = time.monotonic()
                try:
                    phases[phase] = dict(supervised_measure(command, args.repository, ceiling, phase_output, helpers), status="passed")
                except BaseException as exc:
                    phases[phase] = failed_native_phase(exc, time.monotonic() - started, ceiling)
                    raise
                finally:
                    metadata["phases"] = phases
                    helpers.write_json(output / "run-manifest.json", metadata)
                expected_tag = MODEL_TAG if phase == "e5" else production.MODEL_TAG
                phase_identities = grouped_parent_identities(fidelity_records, expected_tag)
                for row in helpers.read_jsonl(helpers.safe_member(phase_output, "results-default.jsonl")):
                    production.validate_runtime_parent_results(row["final_sdk_top10"], phase_identities[row["corpus_id"]], allow_passages=False)
                metadata["active_phase"] = None
                if any(production.guarded_database_fingerprint(snapshot, namespace) != fingerprints[corpus_id]
                       for corpus_id, namespace in namespaces.items()):
                    raise C4Failure("ShadowFrozenSnapshotChanged")
            baseline_rows = list(helpers.read_jsonl(output / "baseline/results-default.jsonl"))
            fresh_rows = list(helpers.read_jsonl(helpers.safe_member(args.fresh_default_run, "results-default.jsonl")))
            if baseline_rows != fresh_rows:
                raise C4Failure("ShadowCurrentDefaultRanksChanged")
            qualified = True
            for stratum in STRATA:
                samples = {phase: [row for row in helpers.read_jsonl(output / phase / "shadow-resources.jsonl") if row["stratum"] == stratum]
                           for phase in phases}
                gates, passed, enough = resource_gates(samples["baseline"], samples["e5"],
                    phases["baseline"]["process_tree_peak_rss_bytes"], phases["e5"]["process_tree_peak_rss_bytes"])
                metadata["resource_gates"][stratum] = gates
                qualified = qualified and passed and enough
            if implementation_hashes(args.repository) != metadata["implementation_sha256"]:
                raise C4Failure("ShadowImplementationChanged")
            metadata.update(resource_qualified=qualified, all_queries_accounted=True)
            if not qualified:
                raise C4Failure("ShadowResourceGateRejected")
            metadata.update(status="complete", quality_scoring_status="exports_fixed_ready_for_separate_scorer")
    except BaseException as exc:
        metadata.update(status="failed", failure_type=type(exc).__name__)
        if isinstance(exc, C4Failure) and exc.args and exc.args[0] in FAILURE_CODES:
            metadata["failure_code"] = exc.args[0]
        raise
    finally:
        helpers.write_json(output / "run-manifest.json", metadata)


def evidence(directory, fixture, runtime_files, source_run, repository):
    """Validate native evidence before exposing content-free aggregate results."""
    import retrieval_ci as ci
    main = ci.load_json(directory, "run-manifest.json")
    source = ci.load_json(source_run, "run-manifest.json")
    fidelity_digest = source["output_sha256"]["ingestion-fidelity.jsonl"]
    if (main.get("status") != "complete" or main.get("resource_qualified") is not True
            or main.get("all_queries_accounted") is not True or main.get("profile") != PROFILE
            or main.get("profile_sha256") != PROFILE_SHA256 or main.get("runtime_files") != runtime_files
            or main.get("implementation_sha256") != implementation_hashes(repository)
            or main.get("source_run_manifest_sha256") != helpers.digest_file(helpers.safe_member(source_run, "run-manifest.json"))
            or main.get("query_target") != fixture["query_count"]):
        raise C4Failure("ShadowEvidenceProvenanceMismatch")
    indexed_source = {}
    source_records = list(helpers.read_jsonl(helpers.safe_member(source_run, "ingestion-fidelity.jsonl")))
    for row in source_records:
        identifier = row["acceptance"].get("memory_id")
        if identifier:
            indexed_source[(row["corpus_id"], identifier)] = {
                "revision": row["current_revision"], "incarnation": row["current_incarnation"],
                "store_generation": row["store_generation"], "content_sha256": row["stored_sha256"],
                "local_only": row["private"], "quarantined": row["quarantined"]}
    projections, ranks, samples = {}, {}, {}
    for phase, expected_tag in (("baseline", production.MODEL_TAG), ("e5", MODEL_TAG)):
        folder = directory / phase
        manifest, score = ci.load_json(folder, "run-manifest.json"), ci.load_json(folder, "score-summary.json")
        helpers.verify_exports(folder, manifest)
        if (manifest.get("status") != "complete" or manifest.get("all_queries_accounted") is not True
                or manifest.get("runtime_files") != runtime_files or manifest.get("profile_sha256") != PROFILE_SHA256
                or manifest.get("query_model_space_tag") != expected_tag or manifest.get("query_target") != fixture["query_count"]
                or manifest["output_sha256"].get("ingestion-fidelity.jsonl") != fidelity_digest
                or manifest.get("dataset_manifest_sha256") != main["dataset_manifest_sha256"]
                or score.get("run_manifest_sha256") != helpers.digest_file(helpers.safe_member(folder, "run-manifest.json"))
                or score.get("query_count") != fixture["query_count"]
                or score.get("labels_sha256") != (fixture["files"]["labels.json"].get("sha256")
                                                   if isinstance(fixture["files"]["labels.json"], dict) else fixture["files"]["labels.json"])
                or score.get("scorer_sha256") != helpers.digest_file(Path(production.__file__))):
            raise C4Failure("ShadowNativePhaseProvenanceMismatch")
        indexing_rows = list(helpers.read_jsonl(helpers.safe_member(folder, "shadow-indexing.jsonl")))
        indexing = {(row["corpus_id"], row["id"]): row for row in indexing_rows}
        if len(indexing) != len(indexing_rows) or set(indexing) != set(indexed_source):
            raise C4Failure("ShadowFullParentIndexingAccountingMismatch")
        for key, expected in indexed_source.items():
            row = indexing[key]
            if any(row.get(field) != value for field, value in expected.items()) or phase == "e5" and (
                    row.get("source_embedding_model") != production.MODEL_TAG or row.get("target_model") != MODEL_TAG):
                raise C4Failure("ShadowIndexIdentityChanged")
        rank_rows = list(helpers.read_jsonl(helpers.safe_member(folder, "results-default.jsonl")))
        phase_identities = grouped_parent_identities(source_records, expected_tag)
        ranks[phase] = {row["query_id"]: row for row in rank_rows}
        if len(ranks[phase]) != len(rank_rows) or len(rank_rows) != fixture["query_count"]:
            raise C4Failure("ShadowFullQueryAccountingMismatch")
        for query in rank_rows:
            if query.get("candidate_cap") is not None or query.get("query_embedding_model") != expected_tag:
                raise C4Failure("ShadowQueryModelChanged")
            production.validate_runtime_parent_results(query["final_sdk_top10"], phase_identities[query["corpus_id"]], allow_passages=False)
            for returned in query["final_sdk_top10"]:
                expected = indexed_source.get((query["corpus_id"], returned["id"]))
                if (expected is None or expected["local_only"] or expected["quarantined"]
                        or returned.get("embedding_model") != expected_tag
                        or any(returned.get(key) != expected[key] for key in ("revision", "incarnation", "store_generation"))):
                    raise C4Failure("ShadowReturnedParentIdentityMismatch")
        native_rows = list(helpers.read_jsonl(helpers.safe_member(folder, "shadow-resources.jsonl")))
        passes = max(1, math.ceil(100 / fixture["query_count"]))
        expected_samples = {(query_id, resource_pass, stratum) for query_id in ranks[phase]
                            for resource_pass in range(passes) for stratum in STRATA}
        sample_map = {(row["query_id"], row["resource_pass"], row["stratum"]): row for row in native_rows}
        if len(sample_map) != len(native_rows) or set(sample_map) != expected_samples:
            raise C4Failure("ShadowMatchedResourceAccountingMismatch")
        for row in native_rows:
            native = row.get("native")
            if (not isinstance(native, dict) or any(type(value) is not int or value < 0 for value in native.values())
                    or row.get("sdk_query_encoding_requests") != 1
                    or row.get("query_vector_cache_hit") is not (native.get("native_returned_batches", 0) == 0)
                    or native.get("native_attempted_batches", 0) != native.get("native_returned_batches", 0)
                    or native.get("native_attempted_windows", 0) != native.get("native_returned_windows", 0)
                    or row["stratum"] == "cache-cold" and native.get("native_returned_batches", 0) == 0):
                raise C4Failure("ShadowNativeCacheAccountingMismatch")
        if manifest.get("sdk_query_encoding_requests") != len(native_rows) + fixture["query_count"]:
            raise C4Failure("ShadowSDKTotalAccountingMismatch")
        samples[phase] = native_rows
        projections[phase] = {"run_manifest_sha256": helpers.digest_file(helpers.safe_member(folder, "run-manifest.json")),
                              "score_summary_sha256": helpers.digest_file(helpers.safe_member(folder, "score-summary.json")),
                              "labels_sha256": score["labels_sha256"], "scorer_sha256": score["scorer_sha256"],
                              "output_sha256": manifest["output_sha256"], "metrics": ci.metric_projection(score["cells"]),
                              "gold_survival": ci.counters(score["gold_survival"]), "indexing": manifest["indexing"],
                              "model_space_tag": expected_tag, "native_counts": manifest["native_counts"],
                              "coverage_counts": manifest["coverage_counts"], "sdk_query_encoding_requests": manifest["sdk_query_encoding_requests"]}
    if set(ranks["baseline"]) != set(ranks["e5"]):
        raise C4Failure("ShadowPairedQuerySetChanged")
    for identifier, candidate in ranks["e5"].items():
        before = ranks["baseline"][identifier]
        if candidate.get("original_sdk_top10") != before["final_sdk_top10"] or candidate["corpus_id"] != before["corpus_id"]:
            raise C4Failure("ShadowOriginalRanksChanged")
    gates = []
    for stratum in STRATA:
        observed, qualified, enough = resource_gates(
            [row for row in samples["baseline"] if row["stratum"] == stratum],
            [row for row in samples["e5"] if row["stratum"] == stratum],
            main["phases"]["baseline"]["process_tree_peak_rss_bytes"], main["phases"]["e5"]["process_tree_peak_rss_bytes"])
        if observed != main["resource_gates"].get(stratum) or not qualified or not enough:
            raise C4Failure("ShadowResourceGatesChanged")
        gates.extend(ci.c4_resource_gates({"resource_gates": observed}, prefix="e5_shadow_" + stratum))
    gates.extend(ci.c4_quality_gates(fixture["name"], projections["e5"], projections["baseline"],
                                   cells_required=("results-default",), prefix="e5_shadow"))
    if fixture["name"] in ci.QUALITY_TARGETS:
        observed = projections["e5"]["metrics"]["results-default"]["final_sdk_top10"]["unconditional"]["hit_at_1"]["mean"]
        gates.append({"name": "e5_shadow_exact_evidence_hit_at_1", "passed": observed >= ci.QUALITY_TARGETS[fixture["name"]],
                      "observed": observed, "minimum": ci.QUALITY_TARGETS[fixture["name"]]})
    return {"profile": PROFILE, "profile_sha256": PROFILE_SHA256, "run_manifest_sha256": helpers.digest_file(directory / "run-manifest.json"),
            "phases": projections, "resources": main["phases"], "resource_gates": main["resource_gates"],
            "snapshot_fingerprints": main["snapshot_fingerprints"], "original_fidelity_preserved": True,
            "full_parent_and_query_identity_verified": True, "model_swap_costs": True}, gates


def failure_projection(directory):
    result = {"quality_scoring_status": "not_run_failed_or_interrupted", "profile_sha256": PROFILE_SHA256, "phases": {}}
    if not directory.exists():
        result["phases"] = {phase: {"status": "not_run", "completed_queries": 0} for phase in ("baseline", "e5")}
        return result
    for phase in ("baseline", "e5"):
        path = helpers.safe_member(directory, phase + "/run-manifest.json", must_exist=False)
        if not path.exists():
            result["phases"][phase] = {"status": "not_run", "completed_queries": 0}
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        status = manifest.get("status")
        selected = {"status": status if isinstance(status, str) and status in {"running", "complete", "failed"} else "unknown"}
        code = manifest.get("failure_code")
        if isinstance(code, str) and code in FAILURE_CODES:
            selected["failure_code"] = code
        for name in ("completed_queries", "query_target", "sdk_query_encoding_requests"):
            value = manifest.get(name)
            if type(value) is int and value >= 0:
                selected[name] = value
        native_counts = manifest.get("native_counts", {})
        if not isinstance(native_counts, dict):
            native_counts = {}
        selected["native_counts"] = {key: value for key, value in native_counts.items()
                                     if key in {"native_attempted_batches", "native_attempted_windows", "native_returned_batches", "native_returned_windows"}
                                     and type(value) is int and value >= 0}
        result["phases"][phase] = selected
    path = helpers.safe_member(directory, "run-manifest.json", must_exist=False)
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        if isinstance(manifest.get("active_phase"), str) and manifest["active_phase"] in {"baseline", "e5"}:
            result["active_phase"] = manifest["active_phase"]
        recorded_phases = manifest.get("phases", {})
        if not isinstance(recorded_phases, dict):
            recorded_phases = {}
        for phase, recorded in recorded_phases.items():
            if phase in {"baseline", "e5"} and isinstance(recorded, dict):
                measurement = failed_native_phase(
                    C4Failure(recorded.get("failure_code"), evidence=recorded), recorded.get("phase_wall_seconds", 0),
                    recorded.get("rss_limit_bytes", 0))
                status = recorded.get("status")
                measurement["status"] = status if isinstance(status, str) and status in {"passed", "failed"} else "unknown"
                result["phases"][phase]["native_measurement"] = measurement
                if measurement["status"] == "failed":
                    result["phases"][phase]["failed_native_measurement"] = measurement
        if isinstance(manifest.get("failure_code"), str) and manifest["failure_code"] in FAILURE_CODES:
            result["failure_code"] = manifest["failure_code"]
        result["resource_gates"] = {}
        resource_reports = manifest.get("resource_gates", {})
        if not isinstance(resource_reports, dict):
            resource_reports = {}
        for stratum in STRATA:
            result["resource_gates"][stratum] = {}
            stratum_reports = resource_reports.get(stratum, {})
            if not isinstance(stratum_reports, dict):
                stratum_reports = {}
            for name, maximum in PROFILE["model_swap_limits"].items():
                row = stratum_reports.get(name, {})
                if not isinstance(row, dict):
                    row = {}
                value = row.get("observed")
                result["resource_gates"][stratum][name] = {
                    "maximum": maximum,
                    "observed": value if type(value) in {int, float} and math.isfinite(value) else None}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("provision", "verify", "run", "measure", "score"))
    parser.add_argument("dataset", nargs="?", choices=("nfcorpus", "scifact", "prospective", "longtext-prospective", "longmemeval"))
    parser.add_argument("--repository", type=Path, default=Path("."))
    for name in ("output", "model_directory", "embedding_model_directory", "source_run", "fresh_default_run", "dataset_directory", "snapshot_home", "baseline_run", "qualification_directory", "floor_qualification_directory"):
        parser.add_argument("--" + name.replace("_", "-"), type=Path)
    parser.add_argument("--phase", choices=("baseline", "e5"))
    parser.add_argument("--namespaces", type=json.loads)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise C4Failure("ShadowActionsRequired")
    args.repository = helpers.checked_path(args.repository, must_exist=True, directory=True)
    if args.command == "provision":
        provision(args.model_directory, args.repository)
    elif args.command == "verify":
        verify_fixture(helpers.isolated_artifact_path(args.model_directory, args.repository, must_exist=True))
    elif args.command == "run":
        run(args)
    elif args.command == "measure":
        measure(args)
    else:
        manifest = json.loads(helpers.safe_member(args.output, "run-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("status") != "complete" or manifest.get("resource_qualified") is not True:
            raise C4Failure("ShadowUnqualifiedScoringForbidden")
        for phase in ("baseline", "e5"):
            production.score_run(argparse.Namespace(run_directory=args.output / phase, dataset_directory=args.dataset_directory))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_class": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
