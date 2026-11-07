"""Small, model-free contracts for actual runtime-C4 qualification."""
from __future__ import annotations

from collections import defaultdict
import copy
import hashlib
from pathlib import Path
import json
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
import production_retrieval as production
import retrieval_ci as ci
import runtime_rerank_c4 as runtime
from retrieval_c4 import C4Failure


def fixture(phase="c4", count=6):
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
    observer.args = SimpleNamespace(phase=phase)
    observer.by_memory, observer.identities = by_memory, production.runtime_parent_identities(records)
    observer.frozen = {query["id"]: {"query_id": query["id"], "corpus_id": "complete", "candidate_cap": None,
        "query_embedding_model": production.MODEL_TAG, "final_sdk_top10": production.decorate_results(rows, by_memory)}}
    observer.counts, observer.measurements, observer.quality_heads = runtime.Counter(), [], {}
    observer.resource_passes = 1
    candidate = []
    for index, row in enumerate(rows):
        item = dict(row)
        if index < min(4, count) and count >= 2:
            item.update(rerank_status="applied", rerank_score=float(index), rerank_original_rank=index + 1,
                        rerank_input={"source": "accepted_original_body", "start": 0, "end": len(row["content"]),
                                      "query_tokens": 2, "kept_query_tokens": 2, "removed_query_characters": 0,
                                      "passage_tokens": 4, "kept_passage_tokens": 4, "removed_passage_characters": 0,
                                      "pair_tokens": 9})
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


def test_remote_guard_does_not_allow_local_native_execution(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(runtime.C4Unavailable, match="RemoteActionsRequired"):
        runtime.main(["freeze", "--repository", ".", "--base-protocol", "fake-protocol.json", "--output", "fake-output.json"])
