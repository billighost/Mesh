"""Small post-export attribution and content-free artifact regressions."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
import production_retrieval as production
import retrieval_ci as ci
import runtime_rerank_c4 as runtime


def fixture(profile="compact-96"):
    records, before = [], []
    for index in range(5):
        identifier = f"{index + 1:032x}"
        span = {"memory_id": identifier, "revision": 1, "incarnation": "a" * 32,
                "store_generation": "b" * 32, "start": 10, "end": 70}
        row = {"id": identifier, "revision": 1, "incarnation": "a" * 32,
               "store_generation": "b" * 32, "embedding_model": production.MODEL_TAG,
               "local_only": False, "quarantined": False, "rank_score": 1.0 - index / 10,
               "similarity": 0.9 - index / 10, "faithful_document_ids": [f"document-{index}"],
               "selected_passage_ordinal": 0, "supporting_spans": [span]}
        before.append(row)
        records.append({"acceptance": {"memory_id": identifier}, "document_id": f"document-{index}",
                        "current_revision": 1, "current_incarnation": "a" * 32, "store_generation": "b" * 32,
                        "stored_characters": 80, "neural_indexed": True, "private": False,
                        "quarantined": False, "faithful_public": True})
    records.append(dict(records[0], document_id="alternate-gold"))
    before[0]["faithful_document_ids"].append("alternate-gold")
    after = copy.deepcopy(before)
    for index, row in enumerate(after):
        if index >= 4:
            row["rerank_status"] = "outside_parent_cap"
            continue
        account = {"source": "exact_selected_source_span", "start": 10, "end": 70,
                   "query_tokens": 4, "kept_query_tokens": 4, "removed_query_characters": 0,
                   "passage_tokens": 12, "kept_passage_tokens": 12, "removed_passage_characters": 0,
                   "pair_tokens": 32}
        if profile != "full-256":
            account.update(source="verbatim_selected_source_excerpt", start=15, end=55,
                           passage_tokens=100, kept_passage_tokens=32, removed_passage_characters=20,
                           pair_tokens=64, profile=profile)
        row.update(rerank_status="applied", rerank_original_rank=index + 1,
                   rerank_score=(2.0 if index == 2 else -float(index)), rerank_input=account)
    after[:4] = sorted(after[:4], key=lambda row: (-row["rerank_score"], row["rerank_original_rank"]))
    queries = [{"id": f"diagnostic-query-{index}", "query": "What is the fake fixture fact?", "corpus": "complete"}
               for index in range(4)]
    labels = {"qrels": {queries[0]["id"]: {"document-2": 1}, queries[1]["id"]: {"document-4": 1},
                        queries[2]["id"]: {"alternate-gold": 1}}, "unanswered_query_ids": [queries[3]["id"]]}
    rows = [{"query_id": query["id"], "corpus_id": "complete", "candidate_cap": None,
             "query_embedding_model": production.MODEL_TAG, "original_sdk_top10": copy.deepcopy(before),
             "final_sdk_top10": copy.deepcopy(after)} for query in queries]
    manifest = {"status": "complete", "dataset": "longmemeval", "query_target": len(queries),
                "all_queries_accounted": True, "labels_in_ranking_inputs": False, "resource_qualified": True,
                "runtime_files": {"mesh/local_rerank.py": "c" * 64}, "harness_sha256": "d" * 64,
                "protocol_sha256": "e" * 64, "runtime_c4_protocol_sha256": "f" * 64,
                "dataset_manifest_sha256": "1" * 64, "source_run_manifest_sha256": "2" * 64,
                "runtime_passage_run_manifest_sha256": "3" * 64, "runtime_passage_results_sha256": "4" * 64,
                "model_file_sha256": {"onnx_sha256": "5" * 64, "tokenizer_sha256": "6" * 64},
                "output_sha256": {"results-default.jsonl": "7" * 64, "ingestion-fidelity.jsonl": "8" * 64},
                "runtime_reranker": {"configuration": "c4-minilm-runtime-recall", "phase": "c4", "profile": profile,
                    "model": runtime.MODEL, "policy": runtime.POLICY, "effective_limits": runtime.effective_limits(profile),
                    "parent_identity_scores_spans_and_tail_verified": True, "top_one_matches_top_ten": True,
                    "labels_in_ranking_inputs": False}}
    provenance = production.reranker_diagnostic_provenance(manifest, "9" * 64, "a" * 64, "b" * 64)
    return manifest, rows, queries, records, labels, provenance


@pytest.mark.parametrize("profile", runtime.INPUT_PROFILES)
def test_diagnostics_distinguish_prefix_misses_aliases_and_no_answer(profile):
    args = fixture(profile)
    result = production.build_reranker_diagnostics(*args)
    first, tail, alias, unanswered = result["queries"]
    assert (first["baseline_first_primary_gold_rank"], first["final_first_primary_gold_rank"]) == (3, 1)
    assert (tail["baseline_first_primary_gold_rank"], tail["final_first_primary_gold_rank"]) == (5, 5)
    assert not any(row["primary_id_is_gold"] for row in tail["head"])
    assert alias["baseline_first_primary_gold_rank"] is alias["final_first_primary_gold_rank"] is None
    assert alias["head"][0]["primary_id_is_gold"] is False
    assert alias["head"][0]["any_faithful_id_is_gold"] is True
    assert unanswered["answered"] is False and unanswered["final_first_primary_gold_rank"] is None
    assert result["answer_span_containment"] == "unavailable_document_qrels_only"
    assert all(row["selected_span_contains_annotated_evidence"] is None and row["excerpt_contains_annotated_evidence"] is None
               for query in result["queries"] for row in query["head"])


def test_diagnostic_does_not_export_source_text_or_identifiers_or_change_inputs():
    args = fixture()
    manifest, rows, queries, records, labels, provenance = args
    queries[0]["query"] = "FAKE_QUERY_SECRET_VALUE"
    rows[0]["final_sdk_top10"][0]["unknown_source_text"] = "FAKE_MEMORY_SECRET_VALUE"
    # An unknown field cannot replace original parent identity; use a discarded
    # query-export field to exercise the projection without corrupting the pair.
    rows[0]["final_sdk_top10"][0].pop("unknown_source_text")
    rows[0]["unknown_source_text"] = "FAKE_MEMORY_SECRET_VALUE"
    saved = copy.deepcopy(args)
    output = json.dumps(production.build_reranker_diagnostics(*args))
    assert args == saved
    for value in ("FAKE_QUERY_SECRET_VALUE", "FAKE_MEMORY_SECRET_VALUE", "document-", "alternate-gold", "diagnostic-query-", records[0]["acceptance"]["memory_id"]):
        assert value not in output
    assert provenance["labels_sha256"] in output


@pytest.mark.parametrize("mutate", [
    lambda args: args[4]["qrels"].update({args[2][0]["id"]: []}),
    lambda args: args[4]["qrels"][args[2][0]["id"]].update({"document-2": True}),
    lambda args: args[4]["unanswered_query_ids"].append(args[2][0]["id"]),
    lambda args: args[3][0].update(document_id={"body": "FAKE_SECRET"}),
    lambda args: args[1][0]["final_sdk_top10"][0].update(faithful_document_ids=["wrong-alias"]),
    lambda args: args[1][0]["final_sdk_top10"][0].update(local_only=True),
    lambda args: args[1][0]["final_sdk_top10"][0].update(rerank_status="model_not_configured"),
    lambda args: args[1][0]["final_sdk_top10"][0]["rerank_input"].update(start=False),
    lambda args: args[1][0]["final_sdk_top10"][0]["rerank_input"].update(pair_tokens=97),
    lambda args: args[1][0]["final_sdk_top10"][4].update(rerank_score=1.0),
    lambda args: args[1][0].update(corpus_id="wrong-corpus"),
])
def test_corrupt_labels_aliases_privacy_and_input_geometry_fail_closed(mutate):
    args = fixture()
    mutate(args)
    with pytest.raises(ValueError):
        production.build_reranker_diagnostics(*args)


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(content="FAKE_SECRET_VALUE"),
    lambda value: value["provenance"].update(personal_path="FAKE_PRIVATE_PATH"),
    lambda value: value["queries"][0].update(query="FAKE_QUERY_BODY"),
    lambda value: value["queries"][0]["head"][0]["supporting_span"].update(text="FAKE_SPAN_BODY"),
    lambda value: value["queries"][0]["head"][0].update(rerank_score=float("nan")),
    lambda value: value["queries"][0]["head"][0].update(final_rank=False),
    lambda value: value["queries"][0]["head"][0]["rerank_input"].update(source="FAKE_SECRET_VALUE"),
    lambda value: value["queries"][0]["head"][0].update(excerpt_contains_annotated_evidence=True),
    lambda value: value["queries"][0].update(query_ref="a" * 64),
])
def test_public_projection_rejects_free_text_nan_forged_geometry_and_query_refs(mutate):
    args = fixture()
    value = production.build_reranker_diagnostics(*args)
    references = {query["query_ref"] for query in value["queries"]}
    mutate(value)
    with pytest.raises(ValueError):
        production.project_reranker_diagnostics(value, args[-1], len(args[2]), expected_query_refs=references)


def write_case(tmp_path, *, native=True):
    manifest, rows, queries, records, labels, _provenance = fixture()
    run, data = tmp_path / "run", tmp_path / "data"
    run.mkdir()
    data.mkdir()
    production.write_json(data / "labels.json", labels)
    for path, values in ((data / "queries-selected.jsonl", queries), (run / "results-default.jsonl", rows),
                         (run / "ingestion-fidelity.jsonl", [dict(record, corpus_id="complete") for record in records])):
        path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")
    fixture_manifest = {"query_count": len(queries), "corpus_count": len(records),
                        "files": {name: production.digest_file(data / name) for name in ("labels.json", "queries-selected.jsonl")}}
    production.write_json(data / "manifest.json", fixture_manifest)
    manifest.update(dataset_manifest_sha256=production.digest_file(data / "manifest.json"),
                    harness_sha256=production.digest_file(Path(production.__file__)),
                    baseline_provenance="Synthetic post-export fixture", ingestion={"complete": {"accepted": len(records)}},
                    output_sha256={name: production.digest_file(run / name) for name in ("results-default.jsonl", "ingestion-fidelity.jsonl")})
    if not native:
        manifest.pop("runtime_reranker")
    production.write_json(run / "run-manifest.json", manifest)
    return run, data, manifest, rows, records, fixture_manifest


def test_scoring_binds_deterministic_diagnostic_bytes_and_preserves_rank_manifest(tmp_path):
    run, data, manifest, rows, records, prepared = write_case(tmp_path)
    fixed = {name: (run / name).read_bytes() for name in ("run-manifest.json", "results-default.jsonl", "ingestion-fidelity.jsonl")}
    production.score_run(SimpleNamespace(run_directory=run, dataset_directory=data))
    score = json.loads((run / "score-summary.json").read_text(encoding="utf-8"))
    diagnostic = (run / production.RERANK_DIAGNOSTICS_FILE).read_bytes()
    assert score["reranker_diagnostics"]["sha256"] == hashlib.sha256(diagnostic).hexdigest()
    projected = ci.reranker_diagnostic_evidence(run, manifest, score, prepared, rows, records)
    assert projected["diagnostic"]["queries"][0]["final_first_primary_gold_rank"] == 1
    production.score_run(SimpleNamespace(run_directory=run, dataset_directory=data))
    assert (run / production.RERANK_DIAGNOSTICS_FILE).read_bytes() == diagnostic
    assert {name: (run / name).read_bytes() for name in fixed} == fixed


@pytest.mark.parametrize("member", ["results-default.jsonl", "ingestion-fidelity.jsonl"])
def test_tampered_fixed_export_prevents_diagnostic_creation(tmp_path, member):
    run, data, _manifest, _rows, _records, _prepared = write_case(tmp_path)
    with (run / member).open("a", encoding="utf-8") as output:
        output.write(" ")
    with pytest.raises(ValueError, match="export changed"):
        production.score_run(SimpleNamespace(run_directory=run, dataset_directory=data))
    assert not (run / production.RERANK_DIAGNOSTICS_FILE).exists()


def test_diag_tampering_and_rebound_numeric_input_forgery_fail_closed(tmp_path):
    run, data, manifest, rows, records, prepared = write_case(tmp_path)
    production.score_run(SimpleNamespace(run_directory=run, dataset_directory=data))
    score = json.loads((run / "score-summary.json").read_text(encoding="utf-8"))
    path = run / production.RERANK_DIAGNOSTICS_FILE
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises(ci.GateError):
        ci.reranker_diagnostic_evidence(run, manifest, score, prepared, rows, records)
    altered = json.loads(original)
    for row in altered["queries"][0]["head"]:
        row["rerank_score"] += 1.0
    production.write_json(path, altered)
    score["reranker_diagnostics"]["sha256"] = production.digest_file(path)
    with pytest.raises(ci.GateError):
        ci.reranker_diagnostic_evidence(run, manifest, score, prepared, rows, records)


def test_non_c4_scoring_and_older_native_scores_keep_diagnostics_absent(tmp_path):
    run, data, manifest, rows, records, prepared = write_case(tmp_path, native=False)
    production.score_run(SimpleNamespace(run_directory=run, dataset_directory=data))
    score = json.loads((run / "score-summary.json").read_text(encoding="utf-8"))
    assert "reranker_diagnostics" not in score
    assert not (run / production.RERANK_DIAGNOSTICS_FILE).exists()
    assert ci.reranker_diagnostic_evidence(run, manifest, score, prepared, rows, records) is None
