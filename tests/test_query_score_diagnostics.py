"""Tiny fixed-export score observations; no runtime model, corpus or Mesh store."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from tests.test_runtime_reranker_diagnostics import ci, production, write_case
from tests.test_runtime_reranker_ci import fixture as native_fixture
from retrieval_c4 import C4Failure


def scored_case(tmp_path, *, legacy_field=None, empty_no_answer=False):
    run, data, manifest, rows, _records, fixture = write_case(tmp_path, native=False)
    for row in rows:
        for field in ("original_sdk_top10", "final_sdk_top10"):
            for candidate in row[field]:
                candidate["score_components"] = production.query_score_components({
                    **candidate, "semantic_similarity": 0.72, "lexical_score": 0.43, "bm25_score": 1.8,
                    "ranking_mode": "calibrated", "retrieval_mode": "cached", "match_reason": "exact+semantic",
                })
    if legacy_field is not None:
        for row in rows:
            for candidate in row[legacy_field]:
                candidate.pop("score_components")
    if empty_no_answer:
        rows[-1]["original_sdk_top10"] = rows[-1]["final_sdk_top10"] = []
    manifest["query_score_components_version"] = 1
    path = run / "results-default.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    manifest["output_sha256"][path.name] = production.digest_file(path)
    production.write_json(run / "run-manifest.json", manifest)
    production.score_run(SimpleNamespace(run_directory=run, dataset_directory=data))
    score = json.loads((run / "score-summary.json").read_text(encoding="utf-8"))
    return run, data, manifest, rows, fixture, score


def test_fixed_export_decomposition_is_deterministic_bound_and_content_free(tmp_path, capsys):
    run, data, manifest, rows, fixture, score = scored_case(tmp_path)
    fixed = {name: (run / name).read_bytes() for name in ("run-manifest.json", "results-default.jsonl", "ingestion-fidelity.jsonl")}
    shown = ci.query_score_diagnostic_evidence(run, manifest, score, fixture, rows, dataset_directory=data)
    production.score_run(SimpleNamespace(run_directory=run, dataset_directory=data))
    assert json.loads((run / "score-summary.json").read_text(encoding="utf-8")) == score
    assert {name: (run / name).read_bytes() for name in fixed} == fixed
    assert shown["body_tag_contributions"] == "unavailable_runtime_union_score"
    assert shown["queries"][0]["rankings"]["final_sdk_top10"]["candidates"][0]["scores"]["semantic_similarity"] == 0.72
    assert shown["queries"][3]["answered"] is False
    output = json.dumps(shown) + capsys.readouterr().out
    for query in ("FAKE_QUERY_SECRET_VALUE", "document-", "diagnostic-query-", "alternate-gold", rows[0]["final_sdk_top10"][0]["id"]):
        assert query not in output


@pytest.mark.parametrize("key,value", [
    ("semantic_similarity", "FAKE_SECRET_BODY"), ("lexical_score", True), ("rank_score", float("nan")),
    ("similarity", float("inf")), ("bm25_score", -1), ("ranking_mode", "FAKE_PRIVATE_PATH"),
    ("match_reason", {"content": "FAKE_BODY"}), ("lexical_source_scope", "FAKE_BODY"),
])
def test_projection_rejects_non_numeric_or_unregistered_components(key, value):
    components = production.query_score_components({"similarity": 0.5, "rank_score": 0.5})
    components[key] = value
    with pytest.raises(ValueError):
        production.project_query_score_components(components)


def test_unknown_components_and_missing_measurements_are_distinct():
    components = production.query_score_components({"similarity": 0.5, "rank_score": 0.5})
    assert components["semantic_similarity"] is components["lexical_score"] is components["ranking_mode"] is None
    assert components["lexical_source_scope"] == "body_and_tags_not_separated"
    components["memory_body"] = "FAKE_SECRET"
    with pytest.raises(ValueError):
        production.project_query_score_components(components)


def test_forged_diagnostic_numbers_and_fixed_export_tampering_fail_closed(tmp_path):
    run, data, manifest, rows, fixture, score = scored_case(tmp_path)
    forged = copy.deepcopy(score)
    forged["query_score_diagnostics"]["results-default"]["queries"][0]["rankings"]["final_sdk_top10"]["candidates"][0]["scores"]["semantic_similarity"] += 0.001
    with pytest.raises(ci.GateError):
        ci.query_score_diagnostic_evidence(run, manifest, forged, fixture, rows, dataset_directory=data)
    forged = copy.deepcopy(score)
    forged["query_score_diagnostics"]["results-default"]["queries"][0]["rankings"]["content"] = "FAKE_BODY"
    with pytest.raises(ci.GateError):
        ci.query_score_diagnostic_evidence(run, manifest, forged, fixture, rows, dataset_directory=data)
    with (run / "results-default.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(" ")
    with pytest.raises(ci.GateError):
        ci.query_score_diagnostic_evidence(run, manifest, score, fixture, rows, dataset_directory=data)


def test_old_scored_exports_retain_absent_diagnostics(tmp_path):
    run, data, manifest, rows, _records, fixture = write_case(tmp_path, native=False)
    production.score_run(SimpleNamespace(run_directory=run, dataset_directory=data))
    score = json.loads((run / "score-summary.json").read_text(encoding="utf-8"))
    assert "query_score_diagnostics" not in score
    assert ci.query_score_diagnostic_evidence(run, manifest, score, fixture, rows) is None


def component_native_fixture(phase):
    observer, query, original, candidate = native_fixture(phase)
    for row in (*original, *candidate):
        row.update(semantic_similarity=0.72, lexical_score=0.43, bm25_score=1.8,
                   ranking_mode="calibrated", retrieval_mode="cached", match_reason="exact+semantic")
    observer.frozen[query["id"]]["final_sdk_top10"] = production.decorate_results(original, observer.by_memory, score_components=True)
    return observer, query, original, candidate


@pytest.mark.parametrize("phase", ["baseline", "c4"])
def test_new_score_components_preserve_native_baseline_prefix_and_tail(phase):
    observer, query, original, candidate = component_native_fixture(phase)
    actual = original if phase == "baseline" else candidate
    exported = observer.validate(query, actual)
    assert [row["id"] for row in exported] == [row["id"] for row in actual]
    assert [row["id"] for row in exported[4:]] == [row["id"] for row in original[4:]]
    assert all(shown["score_components"] == production.query_score_components(row) for shown, row in zip(exported, actual))
    assert exported[0]["score_components"]["semantic_similarity"] == 0.72


def test_native_observer_rejects_component_drift_in_actual_sdk_results():
    observer, query, _original, candidate = component_native_fixture("c4")
    candidate[0]["semantic_similarity"] = 0.73
    with pytest.raises(C4Failure, match="ParentFullIdentityChanged"):
        observer.validate(query, candidate)


@pytest.fixture(scope="module")
def verified_score_fixture(tmp_path_factory):
    return scored_case(tmp_path_factory.mktemp("verified-score"))


def test_consistent_forged_gold_membership_fails_verified_label_reconstruction(verified_score_fixture):
    run, data, manifest, rows, fixture, score = verified_score_fixture
    forged = copy.deepcopy(score)
    query = forged["query_score_diagnostics"]["results-default"]["queries"][0]
    query["answered"] = False
    for ranking in query["rankings"].values():
        ranking["first_primary_gold_rank"] = None
        for candidate in ranking["candidates"]:
            candidate["primary_id_is_gold"] = candidate["any_faithful_id_is_gold"] = False
    with pytest.raises(ci.GateError):
        ci.query_score_diagnostic_evidence(run, manifest, forged, fixture, rows, dataset_directory=data)


@pytest.mark.parametrize("key,value", [
    ("semantic_similarity", 0.71), ("lexical_score", 0.42), ("similarity", 0.89), ("rank_score", 0.99),
    ("bm25_score", 1.9), ("ranking_mode", "rrf"), ("retrieval_mode", "streaming"),
    ("match_reason", "lexical+semantic"), ("lexical_source_scope", "body_and_tags_not_separated"),
])
def test_each_numeric_and_mode_component_is_bound_to_actual_exports(verified_score_fixture, key, value):
    run, data, manifest, rows, fixture, score = verified_score_fixture
    forged = copy.deepcopy(score)
    forged["query_score_diagnostics"]["results-default"]["queries"][0]["rankings"]["final_sdk_top10"]["candidates"][0]["scores"][key] = value
    with pytest.raises(ci.GateError):
        ci.query_score_diagnostic_evidence(run, manifest, forged, fixture, rows, dataset_directory=data)


@pytest.mark.parametrize("key", ["labels_sha256", "results_sha256", "embedding_model_sha256", "dataset_manifest_sha256", "protocol_sha256"])
def test_diagnostic_provenance_hashes_cannot_be_rebound(verified_score_fixture, key):
    run, data, manifest, rows, fixture, score = verified_score_fixture
    forged = copy.deepcopy(score)
    forged["query_score_diagnostics"]["results-default"]["provenance"][key] = "0" * 64
    with pytest.raises(ci.GateError):
        ci.query_score_diagnostic_evidence(run, manifest, forged, fixture, rows, dataset_directory=data)


def test_new_run_with_old_comparator_preserves_absent_components_and_bytes(tmp_path):
    run, data, manifest, rows, fixture, score = scored_case(tmp_path, legacy_field="original_sdk_top10")
    fixed = (run / "results-default.jsonl").read_bytes()
    shown = ci.query_score_diagnostic_evidence(run, manifest, score, fixture, rows, dataset_directory=data)
    original = shown["queries"][0]["rankings"]["original_sdk_top10"]["candidates"][0]["scores"]
    final = shown["queries"][0]["rankings"]["final_sdk_top10"]["candidates"][0]["scores"]
    assert original["semantic_similarity"] is original["lexical_score"] is original["ranking_mode"] is None
    assert original["similarity"] == rows[0]["original_sdk_top10"][0]["similarity"]
    assert final["semantic_similarity"] == 0.72
    assert (run / "results-default.jsonl").read_bytes() == fixed
    assert all("score_components" not in candidate for row in rows for candidate in row["original_sdk_top10"])


def test_empty_no_answer_rankings_remain_empty_and_accounted(tmp_path):
    run, data, manifest, rows, fixture, score = scored_case(tmp_path, empty_no_answer=True)
    shown = ci.query_score_diagnostic_evidence(run, manifest, score, fixture, rows, dataset_directory=data)
    query = shown["queries"][-1]
    assert query["answered"] is False
    assert all(ranking == {"first_primary_gold_rank": None, "candidates": []} for ranking in query["rankings"].values())
