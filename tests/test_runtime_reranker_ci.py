"""Small, model-free contracts for actual runtime-C4 qualification."""
from __future__ import annotations

from collections import defaultdict
import contextlib
import copy
import hashlib
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
import production_retrieval as production
import retrieval_ci as ci
import runtime_rerank_c4 as runtime
from retrieval_c4 import C4Failure


def fixture(phase="c4", count=6, profile="full-256"):
    rows, records = [], []
    for index in range(count):
        identifier, body = f"{index + 1:032x}", f"Original parent fact {index}."
        row = {"id": identifier, "content": body, "revision": 1, "incarnation": "a" * 32,
               "store_generation": "b" * 32, "embedding_model": production.MODEL_TAG,
               "rank_score": 1.0 - index / 10, "similarity": 0.9 - index / 10,
               "local_only": False, "quarantined": False, "selected_passage_ordinal": None,
               "supporting_spans": [{"memory_id": identifier, "revision": 1, "incarnation": "a" * 32,
                                      "store_generation": "b" * 32, "start": 0, "end": len(body), "text": body}]}
        rows.append(row)
        records.append({"acceptance": {"memory_id": identifier}, "document_id": "doc-" + str(index),
                        "current_revision": 1, "current_incarnation": "a" * 32, "store_generation": "b" * 32,
                        "stored_characters": len(body), "stored_sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "private": False, "quarantined": False, "neural_indexed": True, "faithful_public": True})
    by_memory = defaultdict(list)
    for record in records:
        by_memory[record["acceptance"]["memory_id"]].append(record)
    query = {"id": "query-1", "query": "What fact?"}
    observer = runtime.RuntimeObserver.__new__(runtime.RuntimeObserver)
    observer.args = SimpleNamespace(phase=phase, profile=profile)
    observer.by_memory, observer.identities = by_memory, production.runtime_parent_identities(records)
    observer.frozen = {query["id"]: {"query_id": query["id"], "corpus_id": "complete", "candidate_cap": None,
        "query_embedding_model": production.MODEL_TAG, "final_sdk_top10": production.decorate_results(rows, by_memory)}}
    observer.counts, observer.measurements, observer.quality_heads = runtime.Counter(), [], {}
    observer.resource_passes = 1
    observer.module = observer.original_init = observer.original_score = None
    candidate = []
    for index, row in enumerate(rows):
        item = dict(row)
        if index < min(4, count) and count >= 2:
            item.update(rerank_status="applied", rerank_score=float(index), rerank_original_rank=index + 1,
                        rerank_input={"source": "accepted_original_body", "start": 0, "end": len(row["content"]),
                                      "query_tokens": 2, "kept_query_tokens": 2, "removed_query_characters": 0,
                                      "passage_tokens": 4, "kept_passage_tokens": 4, "removed_passage_characters": 0,
                                      "pair_tokens": 9})
            if profile != "full-256":
                item["rerank_input"]["profile"] = profile
        else:
            item["rerank_status"] = "insufficient_candidates" if count < 2 else "outside_parent_cap"
        candidate.append(item)
    candidate[:4] = reversed(candidate[:4])
    return observer, query, rows, candidate


def test_native_candidate_keeps_base_scores_spans_and_tail():
    observer, query, original, candidate = fixture()
    result = observer.validate(query, candidate)
    assert [row["id"] for row in result[:4]] == [row["id"] for row in reversed(original[:4])]
    assert [row["id"] for row in result[4:]] == [row["id"] for row in original[4:]]
    assert result[0]["rank_score"] < result[3]["rank_score"]
    assert result[0]["rerank_score"] > result[3]["rerank_score"]


@pytest.mark.parametrize("key,value", [("revision", 2), ("incarnation", "c" * 32),
                                      ("store_generation", "d" * 32), ("rank_score", 99.0),
                                      ("local_only", True), ("quarantined", True)])
def test_native_identity_and_base_score_changes_fail_closed(key, value):
    observer, query, _original, candidate = fixture()
    candidate[0][key] = value
    with pytest.raises((C4Failure, ValueError, RuntimeError)):
        observer.validate(query, candidate)


def test_native_source_body_and_span_changes_are_not_exported():
    for mutate in (lambda row: row.update(content="Changed source."),
                   lambda row: row["supporting_spans"][0].update(end=1)):
        observer, query, _original, candidate = fixture()
        candidate = copy.deepcopy(candidate)
        mutate(candidate[0])
        with pytest.raises((C4Failure, ValueError)):
            observer.validate(query, candidate)


@pytest.mark.parametrize("key,value", [("kept_query_tokens", 30), ("kept_passage_tokens", 225),
                                      ("pair_tokens", 257), ("start", False), ("removed_query_characters", 999)])
def test_runtime_input_accounting_has_independent_hard_caps(key, value):
    observer, query, _original, candidate = fixture()
    candidate[0]["rerank_input"][key] = value
    with pytest.raises(C4Failure, match="RuntimeRerankerInputAccountingChanged"):
        observer.validate(query, candidate)


def test_runtime_fallback_is_visible_and_unqualified():
    observer, query, _original, candidate = fixture()
    candidate[0]["rerank_status"] = "model_not_configured"
    with pytest.raises(C4Failure, match="RuntimeRerankerFallback"):
        observer.validate(query, candidate)


def test_baseline_rejects_reranking_and_requires_exact_order():
    observer, query, original, candidate = fixture("baseline")
    observer.validate(query, original)
    for rows in (candidate, list(reversed(original))):
        with pytest.raises(C4Failure):
            observer.validate(query, rows)


def test_native_top_one_is_checked_against_native_top_ten():
    observer, query, _original, candidate = fixture()
    observer.quality_heads[query["id"]] = [candidate[0]["id"]]
    module = SimpleNamespace(_embed_query_with_tag=lambda _query: ([0.0] * 384, production.MODEL_TAG),
                             _encode_texts_with_tag=lambda *_args: None, _process_reembed_batch=lambda *_args: None)

    class Mesh:
        def recall(self, text, **kwargs):
            assert kwargs == {"n": 1, "include_private": False, "include_quarantined": False}
            module._embed_query_with_tag(text)
            return [candidate[1]]

    with pytest.raises(C4Failure, match="RuntimeRerankerTopOneChanged"):
        observer.after_namespace(Mesh(), [query], [], module)


def test_native_calls_reject_extra_query_encodings_and_restore_hooks():
    observer, query, _original, candidate = fixture()
    encoder = lambda _query: ([0.0] * 384, production.MODEL_TAG)
    module = SimpleNamespace(_embed_query_with_tag=encoder, _encode_texts_with_tag=lambda *_args: None,
                             _process_reembed_batch=lambda *_args: None)

    class Mesh:
        def recall(self, text, **_kwargs):
            module._embed_query_with_tag(text)
            module._embed_query_with_tag(text)
            return candidate

    with pytest.raises(C4Failure, match="RuntimeRerankerQueryEncodingChanged"):
        observer.native_call(Mesh(), query, module)
    assert module._embed_query_with_tag is encoder


def samples(count=100):
    return [{"query_id": f"query-{index}", "resource_pass": 0, "wall_ms": 10.0, "process_cpu_ms": 4.0}
            for index in range(count)]


def test_runtime_resource_limits_match_the_frozen_c4_maxima():
    baseline = samples()
    candidate = [dict(row, wall_ms=35.0, process_cpu_ms=14.0) for row in baseline]
    gates, qualified, enough = runtime.resource_gates(baseline, candidate, 200 * 1024**2, 243 * 1024**2)
    assert qualified and enough
    assert {key: row["maximum"] for key, row in gates.items()} == runtime.RESOURCE_MAXIMA
    assert gates["delta_p95_ms"]["observed"] == 25.0


@pytest.mark.parametrize("changed", ["missing", "duplicate", "boolean", "nonfinite"])
def test_runtime_resources_require_complete_finite_matching_samples(changed):
    baseline = samples()
    candidate = copy.deepcopy(baseline)
    if changed == "missing":
        candidate.pop()
    elif changed == "duplicate":
        candidate.append(candidate[0])
    elif changed == "boolean":
        candidate[0]["resource_pass"] = False
    else:
        candidate[0]["wall_ms"] = float("nan")
    with pytest.raises(C4Failure):
        runtime.resource_gates(baseline, candidate, 1, 1)


def test_small_or_over_budget_resource_cells_do_not_qualify():
    _gates, qualified, enough = runtime.resource_gates(samples(6), samples(6), 1, 1)
    assert not qualified and not enough
    _gates, qualified, enough = runtime.resource_gates(samples(), samples(), 1, 769 * 1024**2)
    assert not qualified and enough


def test_actual_runtime_c4_runs_before_legacy_child_experiments_only_when_requested():
    phases = [row["name"] for row in ci.initial_summary("prospective", True, True)["phases"]]
    assert phases.index("score-runtime-passages") < phases.index("runtime-c4") < phases.index("passages")
    for c4, passages in ((False, False), (False, True), (True, False)):
        assert "runtime-c4" not in [row["name"] for row in ci.initial_summary("prospective", c4, passages)["phases"]]


def test_runtime_quality_cannot_pass_with_missing_metrics_or_only_a_saved_baseline():
    gates = ci.runtime_c4_quality_gates("prospective", {}, {})
    assert gates and not any(row["passed"] for row in gates)


def test_runtime_quality_requires_absolute_target_and_verified_zero_loss():
    count = 16

    def intervals(mean):
        return {key: {"mean": mean, "n": count, "ci95": [mean, mean]} for key in ci.C4_PAIRED_METRICS}

    before, after = 13 / count, 1.0
    original, candidate = intervals(before), intervals(after)
    condition = {"gold_survival": {"answered_queries": count}, "metrics": {"results-default": {
        "original_sdk_top10": {"unconditional": original}, "final_sdk_top10": {"unconditional": candidate},
        "candidate_minus_original_sdk": intervals(after - before), "candidate_hit_at_1_regressions": {
            "answered_queries": count, "baseline_hits": 13, "candidate_hits": 16, "lost_baseline_hits": 0, "gained_hits": 3}}}}
    baseline = {"gold_survival": {"answered_queries": count}, "metrics": {"results-default": {
        "final_sdk_top10": {"unconditional": original}}}}
    assert all(row["passed"] for row in ci.runtime_c4_quality_gates("prospective", condition, baseline))
    broken = copy.deepcopy(condition)
    broken["metrics"]["results-default"]["candidate_minus_original_sdk"]["hit_at_1"]["n"] = 0
    assert not all(row["passed"] for row in ci.runtime_c4_quality_gates("prospective", broken, baseline))


def test_runtime_evidence_requires_all_native_calls_and_resource_repetitions(tmp_path):
    target, passes = 16, 7
    counts = {"quality_native_calls": target, "top_one_native_calls": target, "resource_repeat_native_calls": target * (passes - 1),
              "warmup_native_calls": 5, "session_load_attempts": 1, "session_load_count": 1, "cold_load_ms": 10.0,
              "inference_attempted_batches": 133, "inference_returned_batches": 133,
              "inference_attempted_pairs": 532, "inference_returned_pairs": 532}
    manifest = {"query_target": target, "runtime_reranker": {"configuration": "c4-minilm-runtime-recall", "phase": "c4",
                "policy": runtime.POLICY, "model": runtime.MODEL, "limits": runtime.LIMITS, "resource_passes": passes,
                "native_counts": counts, "parent_identity_scores_spans_and_tail_verified": True,
                "top_one_matches_top_ten": True, "labels_in_ranking_inputs": False}}
    (tmp_path / "results-default.jsonl").write_text("".join(json.dumps({"query_id": f"q{index}"}) + "\n" for index in range(target)))
    resource_rows = [{"query_id": f"q{index}", "resource_pass": repeat, "wall_ms": 1.0, "process_cpu_ms": 1.0,
                      "quality_export": repeat == 0} for index in range(target) for repeat in range(passes)]
    (tmp_path / "runtime-reranker-resources.jsonl").write_text("".join(json.dumps(row) + "\n" for row in resource_rows))
    assert ci.runtime_c4_evidence(tmp_path, manifest)["resource_sample_count"] == 112
    broken = copy.deepcopy(manifest)
    broken["runtime_reranker"]["native_counts"]["top_one_native_calls"] -= 1
    with pytest.raises(ci.GateError, match="RuntimeC4NativeCallAccountingMismatch"):
        ci.runtime_c4_evidence(tmp_path, broken)
    (tmp_path / "runtime-reranker-resources.jsonl").write_text("".join(json.dumps(row) + "\n" for row in resource_rows[:-1]))
    with pytest.raises(ci.GateError, match="RuntimeC4ResourceSamplesIncomplete"):
        ci.runtime_c4_evidence(tmp_path, manifest)


def scored_runtime_c4_condition(directory, *, mutate_manifest=None, mutate_results=None, profile="full-256"):
    observer, query, original, candidate = fixture(count=2, profile=profile)
    for row in original:
        start, end = 9, len(row["content"]) - 1
        row["selected_passage_ordinal"] = 0
        row["supporting_spans"][0].update(start=start, end=end, text=row["content"][start:end])
    for row in candidate:
        row["selected_passage_ordinal"] = 0
        span = row["supporting_spans"][0]
        row["rerank_input"].update(source="exact_selected_source_span", start=span["start"], end=span["end"])
    observer.frozen[query["id"]]["final_sdk_top10"] = production.decorate_results(original, observer.by_memory)
    result = dict(observer.frozen[query["id"]],
                  original_sdk_top10=production.decorate_results(original, observer.by_memory),
                  final_sdk_top10=observer.validate(query, candidate))
    if mutate_results is not None:
        mutate_results(result)
    records = [dict(record, body_changed=False, corpus_id="complete")
               for group in observer.by_memory.values() for record in group]
    passes = runtime.LIMITS["minimum_resource_samples_per_cell"]
    resources = [{"query_id": query["id"], "resource_pass": repeat, "wall_ms": 1.0, "process_cpu_ms": 1.0,
                  "quality_export": repeat == 0} for repeat in range(passes)]
    exports = {"results-default.jsonl": [result], "ingestion-fidelity.jsonl": records,
               "runtime-reranker-resources.jsonl": resources}
    for name, rows in exports.items():
        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    runtime_files, protocol_hash = {"mesh/store.py": "a" * 64}, "e" * 64
    model = ci.MODEL
    calls = passes + 2
    counts = {"quality_native_calls": 1, "top_one_native_calls": 1, "resource_repeat_native_calls": passes - 1,
              "warmup_native_calls": 1, "session_load_attempts": 1, "session_load_count": 1, "cold_load_ms": 1.0,
              "inference_attempted_batches": calls, "inference_returned_batches": calls,
              "inference_attempted_pairs": 2 * calls, "inference_returned_pairs": 2 * calls,
              "document_inference_attempts": 0}
    manifest = {"status": "complete", "all_queries_accounted": True, "query_target": 1,
                "labels_in_ranking_inputs": False, "runtime_files": runtime_files,
                "model_revision": model["revision"], "query_model_space_tag": production.MODEL_TAG,
                "model_file_sha256": {key: model[key] for key in ("onnx_sha256", "tokenizer_sha256")},
                "protocol_sha256": protocol_hash, "dataset_manifest_sha256": "f" * 64,
                "output_sha256": {name: ci.digest_file(directory / name) for name in exports},
                "paired_original_condition": "runtime_passages", "runtime_reranker": {
                    "configuration": "c4-minilm-runtime-recall", "phase": "c4", "policy": runtime.POLICY,
                    "model": runtime.MODEL, "limits": runtime.LIMITS, "resource_passes": passes,
                    "native_counts": counts, "parent_identity_scores_spans_and_tail_verified": True,
                    "top_one_matches_top_ten": True, "labels_in_ranking_inputs": False}}
    if profile != "full-256":
        manifest["runtime_rerank_profile"] = profile
        manifest["runtime_reranker"].update(profile=profile, effective_limits=runtime.effective_limits(profile))
    if mutate_manifest is not None:
        mutate_manifest(manifest)
    production.write_json(directory / "run-manifest.json", manifest)
    production.write_json(directory / "score-summary.json", {
        "run_manifest_sha256": ci.digest_file(directory / "run-manifest.json"), "query_count": 1,
        "cells": {}, "gold_survival": {"answered_queries": 1}, "ingestion": {"complete": {"accepted": 2}}})
    return {"query_count": 1, "corpus_count": 2}, runtime_files, model, protocol_hash


def test_runtime_c4_condition_accepts_verified_derived_span_comparator(tmp_path, monkeypatch):
    arguments = scored_runtime_c4_condition(tmp_path)
    monkeypatch.setattr(ci, "runtime_passage_evidence", lambda *_args: {})
    manifest, result = ci.condition_evidence(tmp_path, *arguments, scored=True, runtime_passages=True, runtime_c4=True)
    assert manifest["paired_original_condition"] == "runtime_passages"
    assert result["status"] == "complete"
    assert result["unfaithful_public_exported_candidates"] == 0


def test_native_looking_condition_cannot_enable_original_passages_without_c4_opt_in(tmp_path):
    arguments = scored_runtime_c4_condition(tmp_path)
    with pytest.raises(ValueError, match="Paired default export used passage mode"):
        ci.condition_evidence(tmp_path, *arguments, scored=True, runtime_passages=True)


@pytest.mark.parametrize("key,value,error", [
    ("paired_original_condition", "fresh_default", "RuntimeC4ConditionOrPairingMismatch"),
    ("paired_original_condition", None, "RuntimeC4ConditionOrPairingMismatch"),
    ("phase", "baseline", "RuntimeC4NativeConfigurationMismatch"),
    ("configuration", "c4-minilm", "RuntimeC4NativeConfigurationMismatch"),
])
def test_c4_opt_in_rejects_forged_comparator_metadata(tmp_path, key, value, error):
    def mutate(manifest):
        target = manifest if key == "paired_original_condition" else manifest["runtime_reranker"]
        target[key] = value

    arguments = scored_runtime_c4_condition(tmp_path, mutate_manifest=mutate)
    with pytest.raises(ci.GateError, match=error):
        ci.condition_evidence(tmp_path, *arguments, scored=True, runtime_passages=True, runtime_c4=True)


@pytest.mark.parametrize("key,value", [("runtime_c4", 1), ("runtime_c4", "c4"),
                                       ("runtime_passages", False), ("runtime_passages", 1),
                                       ("scored", False), ("scored", 1)])
def test_runtime_c4_condition_requires_explicit_boolean_scored_passage_mode(tmp_path, key, value):
    arguments = scored_runtime_c4_condition(tmp_path)
    options = {"scored": True, "runtime_passages": True, "runtime_c4": True, key: value}
    with pytest.raises(ci.GateError, match="RuntimeC4ConditionOrPairingMismatch"):
        ci.condition_evidence(tmp_path, *arguments, **options)


@pytest.mark.parametrize("key,value", [("revision", 2), ("store_generation", "c" * 32),
                                       ("local_only", True), ("span_end", 1000)])
def test_runtime_c4_original_comparator_keeps_identity_privacy_and_span_bounds_strict(tmp_path, key, value):
    def mutate(row):
        original = row["original_sdk_top10"][0]
        if key == "span_end":
            original["supporting_spans"][0]["end"] = value
        else:
            original[key] = value

    arguments = scored_runtime_c4_condition(tmp_path, mutate_results=mutate)
    with pytest.raises(ValueError, match="Exported parent identity|Supporting span differs"):
        ci.condition_evidence(tmp_path, *arguments, scored=True, runtime_passages=True, runtime_c4=True)


@pytest.fixture(params=("stdlib", "patched"))
def passage_snapshot_fixture(request):
    snapshot_sqlite = sqlite3
    if request.param == "patched":
        from mesh.sqlite_runtime import sqlite3 as snapshot_sqlite

    with tempfile.TemporaryDirectory(prefix="mesh-frozen-quality-snapshot-contract-") as temporary:
        root = Path(temporary).resolve()
        home, run = root / "mesh-home", root / "runtime-passages"
        snapshot_root = root / production.PASSAGE_SNAPSHOT_FOLDER
        for directory in (home, run, snapshot_root / "mesh-home"):
            directory.mkdir(parents=True)
        observer, query, original, _candidate = fixture("baseline", count=2)
        for row in original:
            row["selected_passage_ordinal"] = 0
        observer.frozen[query["id"]]["final_sdk_top10"] = production.decorate_results(original, observer.by_memory)
        records = [dict(record, corpus_id="complete", body_changed=False)
                   for group in observer.by_memory.values() for record in group]
        database = home / "mesh_fixture.db"
        with contextlib.closing(snapshot_sqlite.connect(database)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE memories(id TEXT,namespace TEXT,content TEXT,revision INTEGER,local_only INTEGER,quarantined INTEGER,embedding_model TEXT,embedding BLOB,incarnation TEXT,access_count INTEGER)")
            connection.execute("CREATE TABLE mesh_meta(key TEXT PRIMARY KEY,value TEXT)")
            connection.execute("INSERT INTO mesh_meta VALUES('store_generation',?)", ("b" * 32,))
            connection.execute("CREATE TABLE passages(memory_id TEXT,revision INTEGER,incarnation TEXT,store_generation TEXT,embedding_model TEXT,start INTEGER,end INTEGER,embedding BLOB)")
            for row in original:
                connection.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (row["id"], "fixture", row["content"], row["revision"], 0, 0, production.MODEL_TAG,
                     b"model-free primary", row["incarnation"], 0))
                connection.execute("INSERT INTO passages VALUES(?,?,?,?,?,?,?,?)",
                    (row["id"], row["revision"], row["incarnation"], row["store_generation"], production.MODEL_TAG,
                     0, len(row["content"]), b"model-free passage"))
        store_module = SimpleNamespace(sqlite3=snapshot_sqlite, validate_namespace=lambda value: value)
        store = SimpleNamespace(db_path=database, store_generation="b" * 32,
                                embedding_job_stats=lambda: {"pending_count": 0})

        @contextlib.contextmanager
        def read_connect():
            with contextlib.closing(snapshot_sqlite.connect(store.db_path)) as connection:
                connection.row_factory = snapshot_sqlite.Row
                yield connection

        store._read_connect = read_connect
        waits = []

        def wait(identifiers, **options):
            waits.append(options)
            return {"status": "indexed", "indexed": identifiers}

        mesh = SimpleNamespace(namespace="fixture", store=store, wait_for_indexed=wait)
        item = production.freeze_runtime_passage_snapshot(mesh, records, snapshot_root, store_module)
        assert item["fingerprint"]["wal_sha256"] is None
        snapshot_metadata = {"configuration": production.PASSAGE_SNAPSHOT_CONFIGURATION, "copy_method": "sqlite_backup",
                             "frozen_before_queries": True, "immutable": True, "corpora": {"complete": item}}
        exported = observer.frozen[query["id"]]
        for name, rows in (("ingestion-fidelity.jsonl", records), ("results-default.jsonl", [exported])):
            (run / name).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        source = {"dataset": "prospective", "query_target": 1, "dataset_manifest_sha256": "c" * 64,
                  "protocol_sha256": "d" * 64, "runtime_files": {"mesh/store.py": "e" * 64},
                  "model_revision": ci.MODEL["revision"], "query_model_space_tag": production.MODEL_TAG,
                  "model_file_sha256": {key: ci.MODEL[key] for key in ("onnx_sha256", "tokenizer_sha256")},
                  "output_sha256": {"ingestion-fidelity.jsonl": ci.digest_file(run / "ingestion-fidelity.jsonl")}}
        pairing = {"fresh_default_run_manifest_sha256": "f" * 64, "fresh_default_results_sha256": "1" * 64}
        source_hash = "2" * 64
        manifest = dict(source, status="complete", all_queries_accounted=True, schema_version=6,
                        passage_index_enabled=True, guarded_source_immutable=True,
                        temporary_mesh_home=str(home), runtime_passage_snapshot=snapshot_metadata,
                        source_run_manifest_sha256=source_hash, source_fidelity_sha256=source["output_sha256"]["ingestion-fidelity.jsonl"],
                        runtime_passage_index=pairing,
                        output_sha256={name: ci.digest_file(run / name) for name in ("ingestion-fidelity.jsonl", "results-default.jsonl")})
        production.write_json(run / "run-manifest.json", manifest)
        source_queries = {query["id"]: {"namespace": "fixture", "corpus_id": "complete"}}

        def load(explicit_home=None):
            production.write_json(run / "run-manifest.json", manifest)
            return production.load_runtime_passage_snapshot(run, explicit_home, root, source, source_hash, pairing, records, source_queries)

        class Model:
            def _encode_raw_batches(self, *_args, **_kwargs):
                raise AssertionError("No model inference belongs in snapshot readiness")

        model = Model()
        module = SimpleNamespace(_DirectOnnxEmbedding=Model, _get_model=lambda: (model, "onnx"),
            _backend_to_model_tag=lambda *_args: production.MODEL_TAG, PASSAGE_FORMAT_VERSION=production.PASSAGE_REPRESENTATION,
            _encode_texts_with_tag=lambda *_args: None, _process_reembed_batch=lambda *_args: None)
        resources = SimpleNamespace(summary=lambda: {"sampled_owned_process_rss_bytes_max": 1024})
        yield SimpleNamespace(root=root, home=home, run=run, snapshot_root=snapshot_root, load=load, manifest=manifest,
                              mesh=mesh, records=records, store_module=store_module, module=module, model=model,
                              resources=resources, waits=waits, observer=observer, query=query, original=original)


def test_snapshot_backup_freezes_prequery_access_state_and_preserves_two_copy_parity(passage_snapshot_fixture):
    state = passage_snapshot_fixture
    snapshot = state.load()
    with contextlib.closing(sqlite3.connect(state.mesh.store.db_path)) as connection, connection:
        connection.execute("UPDATE memories SET access_count=access_count+1")
    source_fingerprint = production.guarded_database_fingerprint(state.home, "fixture")
    copied = []
    for phase in ("baseline", "c4"):
        root = state.root / phase
        (root / "mesh-home").mkdir(parents=True)
        production.copy_runtime_passage_snapshot(snapshot, "complete", "fixture", root, state.records, state.store_module)
        with contextlib.closing(sqlite3.connect(root / "mesh-home/mesh_fixture.db")) as connection:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
            copied.append((connection.execute("SELECT * FROM memories ORDER BY id").fetchall(),
                           connection.execute("SELECT * FROM passages ORDER BY memory_id").fetchall(),
                           connection.execute("SELECT value FROM mesh_meta WHERE key='store_generation'").fetchone()))
    assert copied[0] == copied[1]
    assert production.guarded_database_fingerprint(state.home, "fixture") == source_fingerprint
    assert all(row[-1] == 0 for row in copied[0][0])
    production.verify_runtime_passage_snapshot(snapshot)
    templates = {row["id"]: row for row in state.original}
    passages = {row[0]: row for row in copied[0][1]}
    retrieved = []
    for source in copied[0][0]:
        row = dict(templates[source[0]], content=source[2], revision=source[3], embedding_model=source[6],
                   incarnation=source[8], store_generation=copied[0][2][0])
        passage = passages[row["id"]]
        row["supporting_spans"] = [{"memory_id": passage[0], "revision": passage[1], "incarnation": passage[2],
                                     "store_generation": passage[3], "start": passage[5], "end": passage[6],
                                     "text": row["content"][passage[5]:passage[6]]}]
        retrieved.append(row)
    assert state.observer.validate(state.query, retrieved) == state.observer.frozen[state.query["id"]]["final_sdk_top10"]
    with pytest.raises(ValueError, match="overwrite"):
        production.freeze_runtime_passage_snapshot(state.mesh, state.records, state.snapshot_root, state.store_module)


@pytest.mark.parametrize("tamper", ["database", "wal", "generation", "pairing", "query_namespace"])
def test_snapshot_reuse_rejects_hash_identity_and_pair_tampering(passage_snapshot_fixture, tamper):
    state = passage_snapshot_fixture
    home = state.snapshot_root / "mesh-home"
    if tamper == "database":
        with contextlib.closing(sqlite3.connect(home / "mesh_fixture.db")) as connection, connection:
            connection.execute("UPDATE memories SET content='changed'")
    elif tamper == "wal":
        (home / "mesh_fixture.db-wal").write_bytes(b"changed WAL")
    elif tamper == "generation":
        state.manifest["runtime_passage_snapshot"]["corpora"]["complete"]["store_generation"] = "c" * 32
    elif tamper == "pairing":
        state.manifest["runtime_passage_index"] = dict(state.manifest["runtime_passage_index"], fresh_default_results_sha256="0" * 64)
    else:
        state.manifest["runtime_passage_snapshot"]["corpora"]["complete"]["namespace"] = "another-namespace"
    with pytest.raises(ValueError, match="snapshot"):
        state.load()


def test_snapshot_reuse_rejects_ordinary_home_before_reading_its_snapshot(passage_snapshot_fixture, monkeypatch):
    state = passage_snapshot_fixture
    ordinary_home = state.root / ".mesh"
    ordinary_home.mkdir()
    reads = []
    monkeypatch.setattr(production, "guarded_database_fingerprint", lambda *_args: reads.append(True))
    with pytest.raises(ValueError, match="named isolated guarded quality home"):
        state.load(ordinary_home)
    assert reads == []


def test_snapshot_source_mutation_during_backup_is_rejected(passage_snapshot_fixture, monkeypatch):
    state = passage_snapshot_fixture
    snapshot = state.load()
    root = state.root / "copy"
    (root / "mesh-home").mkdir(parents=True)
    original = production.copy_guarded_corpus

    def tamper(*args):
        original(*args)
        with contextlib.closing(sqlite3.connect(snapshot["home"] / "mesh_fixture.db")) as connection, connection:
            connection.execute("UPDATE memories SET access_count=99")

    monkeypatch.setattr(production, "copy_guarded_corpus", tamper)
    with pytest.raises(ValueError, match="while copying"):
        production.copy_runtime_passage_snapshot(snapshot, "complete", "fixture", root, state.records, state.store_module)


def test_snapshot_readiness_is_generation_bound_and_performs_zero_inference(passage_snapshot_fixture, monkeypatch):
    state = passage_snapshot_fixture
    monkeypatch.setenv("MESH_PASSAGE_INDEX", "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    readiness = {}
    production.reuse_runtime_passages(state.mesh, state.records, state.module, readiness, state.resources)
    assert readiness["status"] == "indexed" and readiness["representation_bound_indexed_memories"] == 2
    assert all(readiness[key] == 0 for key in ("scheduled_jobs", "completed_batches", "passage_encoded_rows",
                                              "passage_raw_batch_encoding_calls", "primary_document_inference_attempts"))
    assert any(wait.get("target_representation") == production.PASSAGE_REPRESENTATION for wait in state.waits)
    state.mesh.store.store_generation = "c" * 32
    with pytest.raises(ValueError, match="generation changed"):
        production.reuse_runtime_passages(state.mesh, state.records, state.module, {}, state.resources)


@pytest.mark.parametrize("broken", ["pending", "primary_inference", "passage_inference", "worker_inference"])
def test_snapshot_reuse_does_not_rebuild_a_broken_or_reinferring_checkpoint(passage_snapshot_fixture, monkeypatch, broken):
    state = passage_snapshot_fixture
    monkeypatch.setenv("MESH_PASSAGE_INDEX", "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    encoder, worker = state.module._encode_texts_with_tag, state.module._process_reembed_batch

    def wait(identifiers, **_options):
        if broken == "pending":
            return {"status": "pending", "indexed": []}
        operation = {"primary_inference": state.module._encode_texts_with_tag,
                     "passage_inference": state.model._encode_raw_batches,
                     "worker_inference": state.module._process_reembed_batch}[broken]
        operation(["never encoded"])
        return {"status": "indexed", "indexed": identifiers}

    state.mesh.wait_for_indexed = wait
    readiness = {}
    with pytest.raises((ValueError, RuntimeError), match="readiness failed|inference"):
        production.reuse_runtime_passages(state.mesh, state.records, state.module, readiness, state.resources)
    assert readiness["status"] == "failed"
    assert state.module._encode_texts_with_tag is encoder and state.module._process_reembed_batch is worker
    assert "_encode_raw_batches" not in vars(state.model)


def test_native_measure_explicitly_forwards_snapshot_reuse(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "query-vectors.jsonl").write_text(json.dumps({"namespace": "fixture"}) + "\n", encoding="utf-8")
    args = SimpleNamespace(dataset="prospective", repository=tmp_path, dataset_directory=tmp_path,
        base_protocol=tmp_path / "base.json", embedding_model_directory=tmp_path, dependency_directory=None,
        output=tmp_path / "output", source_run=source, runtime_passage_run=tmp_path / "passages",
        runtime_passage_home=tmp_path / "explicit-snapshot-home", fresh_default_run=tmp_path / "fresh")
    observer, calls = object(), []
    monkeypatch.setattr(runtime, "verify_protocol", lambda *_args: None)
    monkeypatch.setattr(runtime, "RuntimeObserver", lambda *_args: observer)
    monkeypatch.setattr(production, "run_retrieval", lambda _options, **kwargs: calls.append(kwargs))
    runtime.measure(args, production)
    assert calls == [{"experiment": observer, "passage_snapshot": {
        "directory": args.runtime_passage_run, "home": args.runtime_passage_home}}]


def test_native_snapshot_evidence_requires_zero_reinference_and_matched_snapshot(passage_snapshot_fixture, monkeypatch):
    state = passage_snapshot_fixture
    snapshot = state.load()
    monkeypatch.setenv("MESH_PASSAGE_INDEX", "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    readiness = {}
    production.reuse_runtime_passages(state.mesh, state.records, state.module, readiness, state.resources)
    directory = state.root / "native-evidence"
    directory.mkdir()
    scored_runtime_c4_condition(directory)
    manifest = ci.load_json(directory, "run-manifest.json")
    reuse = {"source_run_manifest_sha256": snapshot["run_manifest_sha256"], "snapshot": snapshot["metadata"],
             "source_immutable": True, "readiness_verified_without_inference": True}
    manifest.update(runtime_passage_snapshot_reuse=reuse, document_inference_invoked=False,
                    runtime_passage_readiness={"complete": readiness},
                    runtime_passage_run_manifest_sha256=snapshot["run_manifest_sha256"],
                    matched_snapshot_reuse={"baseline": copy.deepcopy(reuse), "c4": copy.deepcopy(reuse)})
    assert ci.runtime_c4_evidence(directory, manifest)["resource_sample_count"] == 100
    manifest["matched_snapshot_reuse"]["baseline"]["snapshot"]["corpora"]["complete"]["fingerprint"]["database_sha256"] = "0" * 64
    with pytest.raises(ci.GateError, match="MatchedSnapshotReuseMismatch"):
        ci.runtime_c4_evidence(directory, manifest)
    manifest["runtime_passage_readiness"]["complete"]["passage_encoded_rows"] = 1
    with pytest.raises(ValueError, match="performed work"):
        production.runtime_passage_snapshot_reuse_metadata(manifest, state.records)


def compact_fixture(profile, selected):
    observer, query, original, candidate = fixture(profile=profile)
    words = [f"evidence{index}" for index in range(250)]
    words[80] = "Caf\u00e9"
    source = " ".join(words)
    prefix, suffix = "Pr\u00e9face \u03a9. ", " Fin."
    body = prefix + source + suffix
    source_start, source_end = (len(prefix), len(prefix) + len(source)) if selected else (0, len(body))
    for row in original:
        row.update(content=body, selected_passage_ordinal=2 if selected else None)
        row["supporting_spans"][0].update(start=source_start, end=source_end, text=body[source_start:source_end])
        for record in observer.by_memory[row["id"]]:
            record.update(stored_characters=len(body), stored_sha256=production.digest_text(body))
    observer.identities = production.runtime_parent_identities([record for group in observer.by_memory.values() for record in group])
    observer.frozen[query["id"]]["final_sdk_top10"] = production.decorate_results(original, observer.by_memory)
    passage_limit = runtime.INPUT_PROFILES[profile][0]
    start = len(prefix) + len(" ".join(words[:40])) + 1
    excerpt = " ".join(words[40:40 + passage_limit])
    end = start + len(excerpt)
    originals = {row["id"]: row for row in original}
    for row in candidate:
        before = originals[row["id"]]
        row.update(content=before["content"], selected_passage_ordinal=before["selected_passage_ordinal"],
                   supporting_spans=copy.deepcopy(before["supporting_spans"]))
        if row.get("rerank_status") == "applied":
            row["rerank_input"].update(source="verbatim_selected_source_excerpt" if selected else "verbatim_original_body_excerpt",
                start=start, end=end, passage_tokens=len(body[source_start:source_end].split()), kept_passage_tokens=passage_limit,
                removed_passage_characters=source_end - source_start - len(excerpt), pair_tokens=passage_limit + 5)
    return observer, query, original, candidate


@pytest.mark.parametrize("profile", tuple(runtime.INPUT_PROFILES))
@pytest.mark.parametrize("phase", ["baseline", "c4"])
def test_native_configuration_explicitly_pins_profile_and_disables_baseline(tmp_path, monkeypatch, profile, phase):
    observer, _query, _original, _candidate = fixture(phase=phase, profile=profile)
    observer.args.model_directory = tmp_path / "fake-model"
    monkeypatch.setenv("MESH_RERANK_MODEL_DIR", "inherited-model")
    monkeypatch.setenv("MESH_RERANK_ENABLED", "1")
    observer.configure()
    assert runtime.os.environ["MESH_RERANK_PROFILE"] == profile
    assert runtime.os.environ["MESH_RERANK_ENABLED"] == ("1" if phase == "c4" else "0")
    assert runtime.os.environ.get("MESH_RERANK_MODEL_DIR") == (str(observer.args.model_directory) if phase == "c4" else None)


@pytest.mark.parametrize("profile", tuple(runtime.INPUT_PROFILES))
def test_frozen_protocol_binds_profile_and_effective_caps(tmp_path, monkeypatch, profile):
    base, target = tmp_path / "base.json", tmp_path / "profile.json"
    base.write_text("{}", encoding="utf-8")
    args = SimpleNamespace(profile=profile, repository=tmp_path, base_protocol=base, protocol=target)
    monkeypatch.setattr(runtime, "implementation_hashes", lambda *_args: {"runtime_rerank_c4.py": "a" * 64})
    frozen = runtime.frozen_protocol(args, production)
    production.write_json(target, frozen)
    runtime.verify_protocol(args, production)
    assert frozen["profile"] == profile
    assert frozen["limits"] == runtime.LIMITS
    assert (frozen["effective_limits"]["maximum_passage_tokens"], frozen["effective_limits"]["maximum_pair_tokens"]) == runtime.INPUT_PROFILES[profile]
    assert frozen["effective_limits"]["maximum_query_tokens"] == 29
    assert tuple(runtime.INPUT_PROFILES) == ci.RUNTIME_RERANK_PROFILES
    assert frozen["resource_maxima"] == {
        "total_rss_mib": 768, "delta_rss_mib": 128, "delta_p95_ms": 50,
        "delta_p99_ms": 100, "delta_cpu_ms": 50,
    }
    frozen["effective_limits"]["maximum_pair_tokens"] += 1
    production.write_json(target, frozen)
    with pytest.raises(C4Failure, match="FrozenC4ProtocolMismatch"):
        runtime.verify_protocol(args, production)


@pytest.mark.parametrize("changed", ["profile_catalog", "hard_passage_cap"])
def test_native_runtime_catalog_and_full_hard_constants_remain_pinned(monkeypatch, changed):
    import mesh.local_rerank as local

    observer, _query, _original, _candidate = fixture(phase="baseline", profile="compact-96")
    memory_module = SimpleNamespace(__file__=str(Path(local.__file__).with_name("memory.py")))
    observer.attach_runtime(memory_module)
    assert observer.module is None
    if changed == "profile_catalog":
        monkeypatch.setattr(local, "INPUT_PROFILES", dict(local.INPUT_PROFILES, **{"compact-96": (65, 96)}))
    else:
        monkeypatch.setattr(local, "PASSAGE_TOKENS", 64)
    with pytest.raises(C4Failure, match="RuntimeRerankerConfigurationChanged"):
        observer.attach_runtime(memory_module)


@pytest.mark.parametrize("profile", ["compact-128", "compact-96", "compact-80"])
@pytest.mark.parametrize("selected", [False, True])
def test_compact_runtime_accepts_exact_contiguous_subspan_and_keeps_original_evidence(profile, selected):
    observer, query, original, candidate = compact_fixture(profile, selected)
    exported = observer.validate(query, candidate)
    before = {row["id"]: row for row in production.decorate_results(original, observer.by_memory)}
    assert [row["id"] for row in exported[4:]] == [row["id"] for row in original[4:]]
    for row in exported:
        assert row["supporting_spans"] == before[row["id"]]["supporting_spans"]
        assert row["rank_score"] == before[row["id"]]["rank_score"]
        if row.get("rerank_status") == "applied":
            account = row["rerank_input"]
            assert account["profile"] == profile
            assert "Caf\u00e9" in candidate[0]["content"][account["start"]:account["end"]]


@pytest.mark.parametrize("profile", ["compact-128", "compact-96"])
@pytest.mark.parametrize("selected", [False, True])
def test_compact_profile_keeps_exact_full_source_mode_when_source_fits(profile, selected):
    observer, query, original, candidate = fixture(profile=profile)
    if selected:
        for row in (*original, *candidate):
            row["selected_passage_ordinal"] = 0
        for row in candidate:
            if "rerank_input" in row:
                row["rerank_input"]["source"] = "exact_selected_source_span"
        observer.frozen[query["id"]]["final_sdk_top10"] = production.decorate_results(original, observer.by_memory)
    assert observer.validate(query, candidate)[0]["rerank_input"]["profile"] == profile


@pytest.mark.parametrize("changed", ["profile", "start", "end", "removed", "passage_cap", "pair_cap", "mode", "span_bytes", "extra_text"])
def test_compact_runtime_rejects_profile_bounds_provenance_and_cap_changes(changed):
    observer, query, _original, candidate = compact_fixture("compact-96", True)
    row, account = candidate[0], candidate[0]["rerank_input"]
    if changed == "profile":
        account["profile"] = "compact-128"
    elif changed == "start":
        account["start"] = 0
    elif changed == "end":
        account["end"] = len(row["content"])
    elif changed == "removed":
        account["removed_passage_characters"] += 1
    elif changed == "passage_cap":
        account["kept_passage_tokens"] = 65
    elif changed == "pair_cap":
        account["pair_tokens"] = 97
    elif changed == "mode":
        account["source"] = "verbatim_original_body_excerpt"
    elif changed == "span_bytes":
        row["supporting_spans"][0]["text"] = "changed original evidence"
    else:
        account["excerpt_text"] = "must never appear in the export"
    with pytest.raises(C4Failure, match="InputAccountingChanged|SupportingSpanBytesChanged"):
        observer.validate(query, candidate)


@pytest.mark.parametrize("profile", tuple(runtime.INPUT_PROFILES))
def test_native_evidence_accepts_known_profile_and_matching_effective_limits(tmp_path, profile):
    arguments = scored_runtime_c4_condition(tmp_path, profile=profile)
    manifest, _report = ci.condition_evidence(tmp_path, *arguments, scored=True, runtime_c4=False)
    evidence = ci.runtime_c4_evidence(tmp_path, manifest, expected_profile=profile)
    assert evidence["profile"] == profile
    assert evidence["effective_limits"] == runtime.effective_limits(profile)
    with pytest.raises(ci.GateError, match="NativeProfileMismatch"):
        ci.runtime_c4_evidence(tmp_path, manifest, expected_profile="compact-96" if profile != "compact-96" else "full-256")


@pytest.mark.parametrize("changed", ["unknown_profile", "missing_limits", "larger_passage_cap", "larger_pair_cap", "query_cap", "top_level_profile"])
def test_native_evidence_rejects_unpinned_compact_configuration(tmp_path, changed):
    scored_runtime_c4_condition(tmp_path, profile="compact-96")
    manifest = ci.load_json(tmp_path, "run-manifest.json")
    configuration = manifest["runtime_reranker"]
    if changed == "unknown_profile":
        configuration["profile"] = "compact-64"
    elif changed == "missing_limits":
        configuration.pop("effective_limits")
    elif changed == "top_level_profile":
        manifest["runtime_rerank_profile"] = "full-256"
    else:
        key = {"larger_passage_cap": "maximum_passage_tokens", "larger_pair_cap": "maximum_pair_tokens", "query_cap": "maximum_query_tokens"}[changed]
        configuration["effective_limits"][key] += 1
    with pytest.raises(ci.GateError, match="NativeProfileMismatch|NativeConfigurationMismatch"):
        ci.runtime_c4_evidence(tmp_path, manifest)


@pytest.mark.parametrize("command", ["init", "run"])
@pytest.mark.parametrize("profile", ["compact-96", "compact-80"])
def test_ci_setup_failure_artifact_preserves_requested_profile(tmp_path, monkeypatch, command, profile):
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / ".work").mkdir()
    output = repository / ".work" / "profile-output"
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(sys, "argv", ["retrieval_ci.py", command, "prospective", "--repository", str(repository),
        "--output-root", str(output), "--runtime-rerank-profile", profile, "--runtime-passages", "--reranker-experiment", "c4"])
    assert ci.main() == (0 if command == "init" else 1)
    assert ci.load_json(output, "job-summary.json")["runtime_rerank_profile"] == profile


def test_remote_guard_does_not_allow_local_native_execution(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(runtime.C4Unavailable, match="RemoteActionsRequired"):
        runtime.main(["freeze", "--repository", ".", "--base-protocol", "fake-protocol.json", "--output", "fake-output.json"])
