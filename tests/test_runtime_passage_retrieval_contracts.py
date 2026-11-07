"""Tiny synthetic contracts for the opt-in runtime passage evaluator; no model loads."""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCHMARKS))
import production_retrieval as production
import replay_retrieval as replay
import retrieval_ci as ci

MEMORY_ID, INCARNATION, GENERATION = "1" * 32, "2" * 32, "3" * 32
MODEL = {"revision": "fake-revision", "onnx_sha256": "a" * 64, "tokenizer_sha256": "b" * 64}
RUNTIME = {"mesh/example.py": "c" * 64}


def record(identifier=MEMORY_ID, document="doc"):
    return {"acceptance": {"memory_id": identifier}, "corpus_id": "complete", "document_id": document,
            "current_revision": 7, "current_incarnation": INCARNATION, "store_generation": GENERATION,
            "stored_characters": 24, "stored_sha256": "d" * 64, "private": False, "quarantined": False,
            "neural_indexed": True, "body_changed": False, "faithful_public": True}


def candidate(identifier=MEMORY_ID, passage=False):
    return {"id": identifier, "revision": 7, "incarnation": INCARNATION, "store_generation": GENERATION,
            "embedding_model": production.MODEL_TAG, "faithful_document_ids": ["doc"],
            "local_only": False, "quarantined": False, "selected_passage_ordinal": 0 if passage else None,
            "supporting_spans": [{"memory_id": identifier, "revision": 7, "incarnation": INCARNATION,
                                  "store_generation": GENERATION, "start": 0, "end": 12}]}


def jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


class Resources:
    def sample(self):
        return 1024

    def summary(self):
        return {"wall_seconds": 0.01, "process_cpu_seconds": 0.005, "sampled_owned_process_rss_bytes_max": 1024}


def manual_runtime(records, *, progress=True):
    identities = production.runtime_parent_identities(records)

    class Direct:
        def _encode_raw_batches(self, texts, **_kwargs):
            return texts

    class Store:
        db_path = Path("FAKE_TEMP/mesh-home/mesh_fixture.db")
        store_generation = GENERATION

        def __init__(self):
            self.todo, self.jobs, self.limits = set(identities), {}, []

        def backfill_embedding_jobs(self, limit, *, target_model, target_representation):
            self.limits.append((limit, target_model, target_representation))
            missing = sorted(self.todo - self.jobs.keys())[:limit]
            for identifier in missing:
                self.jobs[identifier] = {"memory_id": identifier, "revision": 7, "incarnation": INCARNATION,
                                         "store_generation": GENERATION, "target_model": target_model,
                                         "target_representation": target_representation, "attempts": 0}
            return len(missing)

        def embedding_job_stats(self):
            return {"pending_count": len(self.jobs)}

        def list_embedding_jobs(self, *, limit):
            return list(self.jobs.values())[:limit]

    class Mesh:
        namespace = "fixture"

        def __init__(self):
            self.store, self.waits, self.hints = Store(), [], []

        def wait_for_indexed(self, identifiers, **kwargs):
            self.waits.append((identifiers, kwargs))
            return {"status": "indexed", "indexed": identifiers}

    mesh, model = Mesh(), Direct()

    def hint(root, namespace, identifier, revision, tag, attempts, **kwargs):
        return types.SimpleNamespace(root=root, namespace=namespace, memory_id=identifier,
                                     revision=revision, target_model=tag, attempts=attempts, **kwargs)

    def process(hints):
        mesh.hints.extend(hints)
        if progress:
            model._encode_raw_batches(["synthetic passage"])
            identifier = hints[0].memory_id
            mesh.store.jobs.pop(identifier)
            mesh.store.todo.remove(identifier)

    module = types.SimpleNamespace(_DirectOnnxEmbedding=Direct, _get_model=lambda: (model, "onnx"),
                                   _backend_to_model_tag=lambda *_args: production.MODEL_TAG,
                                   PASSAGE_FORMAT_VERSION=production.PASSAGE_REPRESENTATION,
                                   _ReembedHint=hint, _process_reembed_batch=process,
                                   _encode_texts_with_tag=lambda _texts: pytest.fail("Primary inference"),
                                   _embed_query_with_tag=lambda _query: ([0.0] * 384, production.MODEL_TAG))
    return mesh, model, module


def paired_fixture(root):
    source, fresh = root / "guarded", root / "fresh-default"
    source.mkdir()
    fresh.mkdir()
    records, queries = [record()], [{"id": "query-1", "query": "synthetic query"}]
    jsonl(source / "ingestion-fidelity.jsonl", records)
    jsonl(fresh / "ingestion-fidelity.jsonl", records)
    row = {"query_id": "query-1", "corpus_id": "complete", "candidate_cap": None,
           "query_embedding_model": production.MODEL_TAG, "final_sdk_top10": [candidate()]}
    jsonl(fresh / "results-default.jsonl", [row])
    common = {"status": "complete", "all_queries_accounted": True, "dataset": "prospective", "query_target": 1,
              "dataset_manifest_sha256": "e" * 64, "protocol_sha256": "f" * 64, "runtime_files": RUNTIME,
              "model_revision": MODEL["revision"], "query_model_space_tag": production.MODEL_TAG,
              "model_file_sha256": {key: MODEL[key] for key in ("onnx_sha256", "tokenizer_sha256")},
              "ingestion": {"complete": {"accepted": 1}}, "passage_index_enabled": False}
    source_manifest = dict(common, output_sha256={"ingestion-fidelity.jsonl": replay.digest_file(source / "ingestion-fidelity.jsonl")})
    production.write_json(source / "run-manifest.json", source_manifest)
    fresh_manifest = dict(common, source_run_manifest_sha256=replay.digest_file(source / "run-manifest.json"),
                          labels_in_ranking_inputs=False, saved_query_vectors_used=False, document_inference_invoked=False,
                          query_encoding_mode="auto (unset default)", output_sha256={
                              name: replay.digest_file(fresh / name) for name in ("ingestion-fidelity.jsonl", "results-default.jsonl")},
                          default_query_backend_observations={"normal_sdk_query_encoding_calls": 1,
                              "all_query_model_tags_match_indexed_model": True, "all_query_dimensions_384": True,
                              "document_inference_attempts": 0})
    production.write_json(fresh / "run-manifest.json", fresh_manifest)
    return source, fresh, source_manifest, fresh_manifest, queries, records


@pytest.mark.parametrize("enabled", [False, True])
def test_opt_in_is_set_after_install_model_clears_inherited_flags(enabled):
    with tempfile.TemporaryDirectory(prefix="mesh-frozen-quality-contract-") as temporary, mock.patch.dict(os.environ):
        root = Path(temporary)
        source, fresh, source_manifest, fresh_manifest, queries, records = paired_fixture(root)
        home, data, models = root / "mesh-home", root / "data", root / "models"
        for directory in (home, data, models):
            directory.mkdir()
        jsonl(data / "queries-selected.jsonl", queries)
        fixture = {"query_count": 1, "corpus_count": 1, "files": {"queries-selected.jsonl": replay.digest_file(data / "queries-selected.jsonl")}}
        production.write_json(data / "manifest.json", fixture)
        protocol = {"model": dict(MODEL, name="sentence-transformers/all-MiniLM-L6-v2"), "candidate_caps": [64, 96], "fusion": "calibrated"}
        for name, key in (("model.onnx", "onnx_sha256"), ("tokenizer.json", "tokenizer_sha256")):
            (models / name).write_bytes(b"synthetic local fixture")
            protocol["model"][key] = replay.digest_file(models / name)
        production.write_json(root / "protocol.json", protocol)
        source_manifest.update(embedding_model="minilm", temporary_mesh_home=str(home),
                               dataset_manifest_sha256=replay.digest_file(data / "manifest.json"),
                               protocol_sha256=replay.digest_file(root / "protocol.json"),
                               model_file_sha256={key: protocol["model"][key] for key in ("onnx_sha256", "tokenizer_sha256")})
        jsonl(source / "query-vectors.jsonl", [{"query_id": "query-1", "query": "synthetic query", "corpus_id": "complete",
                                              "embedding_model": production.MODEL_TAG, "namespace": "fixture"}])
        source_manifest["output_sha256"]["query-vectors.jsonl"] = replay.digest_file(source / "query-vectors.jsonl")
        production.write_json(source / "run-manifest.json", source_manifest)
        fresh_manifest.update({key: source_manifest[key] for key in (
            "dataset_manifest_sha256", "protocol_sha256", "model_file_sha256")})
        fresh_manifest["source_run_manifest_sha256"] = replay.digest_file(source / "run-manifest.json")
        production.write_json(fresh / "run-manifest.json", fresh_manifest)
        os.environ["MESH_PASSAGE_INDEX"] = "1"

        def at_import(*_args):
            assert os.environ.get("MESH_PASSAGE_INDEX") == ("1" if enabled else None)
            assert os.environ["MESH_BG_ONNX"] == "0"
            assert "MESH_QUERY_MODE" not in os.environ
            raise RuntimeError("Synthetic import boundary")

        args = argparse.Namespace(command="query-only", dataset="prospective", repository=root,
            dataset_directory=data, protocol=root / "protocol.json", model_directory=models, dependency_directory=None,
            source_run=source, source_home=home, source_namespace="fixture", output=root / "output",
            retain_guarded_home=False, runtime_passages=enabled, fresh_default_run=fresh if enabled else None)
        with mock.patch.object(production, "import_runtime", side_effect=at_import), mock.patch.object(
                production, "RuntimePassageResources", Resources), pytest.raises(RuntimeError, match="Synthetic import boundary"):
            production.run_retrieval(args)
        result = json.loads((args.output / "run-manifest.json").read_text(encoding="utf-8"))
        assert result["passage_index_enabled"] is enabled
        assert result["status"] == "failed"


@pytest.mark.parametrize("schema", ["6", 6])
def test_schema_six_fake_runtime_completes_worker_and_controller_projection(schema):
    with tempfile.TemporaryDirectory(prefix="mesh-frozen-quality-contract-") as temporary, mock.patch.dict(os.environ):
        root = Path(temporary)
        source, fresh, source_manifest, fresh_manifest, queries, records = paired_fixture(root)
        home, data, models = root / "mesh-home", root / "data", root / "models"
        for directory in (home, data, models):
            directory.mkdir()
        content = "synthetic parent content"
        records[0].update(stored_characters=len(content), stored_sha256=production.digest_text(content))
        for directory in (source, fresh):
            jsonl(directory / "ingestion-fidelity.jsonl", records)
        source_manifest["output_sha256"]["ingestion-fidelity.jsonl"] = replay.digest_file(source / "ingestion-fidelity.jsonl")
        fresh_manifest["output_sha256"]["ingestion-fidelity.jsonl"] = source_manifest["output_sha256"]["ingestion-fidelity.jsonl"]
        jsonl(data / "queries-selected.jsonl", queries)
        fixture = {"query_count": 1, "corpus_count": 1, "files": {"queries-selected.jsonl": replay.digest_file(data / "queries-selected.jsonl")}}
        production.write_json(data / "manifest.json", fixture)
        protocol = {"model": dict(MODEL, name="sentence-transformers/all-MiniLM-L6-v2"), "candidate_caps": [64, 96], "fusion": "calibrated"}
        for name, key in (("model.onnx", "onnx_sha256"), ("tokenizer.json", "tokenizer_sha256")):
            (models / name).write_bytes(b"synthetic fixture without a native model")
            protocol["model"][key] = replay.digest_file(models / name)
        production.write_json(root / "protocol.json", protocol)
        source_manifest.update(embedding_model="minilm", temporary_mesh_home=str(home), schema_version=schema,
            dataset_manifest_sha256=replay.digest_file(data / "manifest.json"), protocol_sha256=replay.digest_file(root / "protocol.json"),
            model_file_sha256={key: protocol["model"][key] for key in ("onnx_sha256", "tokenizer_sha256")})
        jsonl(source / "query-vectors.jsonl", [{"query_id": "query-1", "query": "synthetic query", "corpus_id": "complete",
                                              "embedding_model": production.MODEL_TAG, "namespace": "fixture"}])
        source_manifest["output_sha256"]["query-vectors.jsonl"] = replay.digest_file(source / "query-vectors.jsonl")
        production.write_json(source / "run-manifest.json", source_manifest)
        fresh_manifest.update({key: source_manifest[key] for key in (
            "dataset_manifest_sha256", "protocol_sha256", "model_file_sha256", "schema_version")})
        fresh_manifest["source_run_manifest_sha256"] = replay.digest_file(source / "run-manifest.json")
        production.write_json(fresh / "run-manifest.json", fresh_manifest)
        database = home / "mesh_fixture.db"
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE memories(id TEXT,namespace TEXT,content TEXT,revision INTEGER,local_only INTEGER,quarantined INTEGER,embedding_model TEXT,embedding BLOB,incarnation TEXT)")
            connection.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)",
                (MEMORY_ID, "fixture", content, 7, 0, 0, production.MODEL_TAG, b"synthetic primary", INCARNATION))
        original_database_hash = replay.digest_file(database)
        mesh, _model, module = manual_runtime(records)

        @contextlib.contextmanager
        def read_connect():
            with contextlib.closing(sqlite3.connect(mesh.store.db_path)) as connection:
                connection.row_factory = sqlite3.Row
                yield connection

        mesh.store._read_connect = read_connect

        def recall(query, **kwargs):
            assert kwargs == {"n": 10, "include_private": False, "include_quarantined": False}
            assert not mesh.store.jobs and not mesh.store.todo
            module._embed_query_with_tag(query)
            return [candidate(passage=True)]

        mesh.recall = recall
        module.Mesh = lambda **kwargs: mesh
        store_module = types.SimpleNamespace(SCHEMA_VERSION=schema, sqlite3=sqlite3,
                                            validate_namespace=lambda value: value, get_vector_cache_stats=lambda: {})

        def import_runtime(owned, *_args):
            mesh.store.db_path = owned / "mesh-home/mesh_fixture.db"
            return RUNTIME, None, module, store_module

        args = argparse.Namespace(command="query-only", dataset="prospective", repository=root, dataset_directory=data,
            protocol=root / "protocol.json", model_directory=models, dependency_directory=None, source_run=source,
            source_home=home, source_namespace="fixture", output=root / "runtime-passages", retain_guarded_home=False,
            runtime_passages=True, fresh_default_run=fresh)
        with mock.patch.object(production, "import_runtime", side_effect=import_runtime), \
                mock.patch.object(production, "verify_sqlite", return_value={"fts5": True, "threadsafe": True}), \
                mock.patch.object(production, "close_namespace") as closed, \
                mock.patch.object(production, "RuntimePassageResources", Resources), \
                mock.patch.object(production.importlib.metadata, "version", return_value="synthetic"):
            production.run_retrieval(args)
        closed.assert_called_once()
        manifest = json.loads((args.output / "run-manifest.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "complete" and type(manifest["schema_version"]) is int and manifest["schema_version"] == 6
        assert replay.digest_file(database) == original_database_hash
        assert any(wait[1].get("target_representation") == production.PASSAGE_REPRESENTATION for wait in mesh.waits)
        # Supply the one-row offline score only after the complete ranking export.
        fields = {cohort: metrics(1.0, 1) for cohort in (
            "unconditional", "any_gold_survived", "all_gold_survived", "retained_gold_denominator")}
        score = {"run_manifest_sha256": replay.digest_file(args.output / "run-manifest.json"), "query_count": 1,
            "cells": {"results-default": {"final_sdk_top10": fields, "original_sdk_top10": fields}},
            "gold_survival": {"answered_queries": 1}, "ingestion": source_manifest["ingestion"]}
        production.write_json(args.output / "score-summary.json", score)
        _manifest, projected = ci.condition_evidence(args.output, fixture, RUNTIME, protocol["model"],
            replay.digest_file(root / "protocol.json"), scored=True, runtime_passages=True)
        assert type(projected["schema_version"]) is int and projected["schema_version"] == 6
        assert projected["runtime_passage_readiness"]["complete"]["representation_bound_indexed_memories"] == 1
        assert projected["original_parent_identity_model_and_supporting_spans_verified"] is True
        assert projected["runtime_passage_query_resources"]["query_count"] == 1


@pytest.mark.parametrize("schema", [True, False, 6.0, None, "06", "6 ", " 6", "+6", "6.0", "5", "7", 5, 7, [], {}])
def test_runtime_schema_validation_rejects_other_versions_and_noncanonical_values(tmp_path, schema):
    with pytest.raises(ValueError, match="canonical schema-6"):
        production.validate_runtime_passage_schema(schema)
    with pytest.raises(ValueError, match="canonical schema-6"):
        ci.runtime_passage_evidence(tmp_path, {"schema_version": schema}, [])


def test_manual_drain_preserves_full_original_identity_and_exact_representation(monkeypatch):
    records = [record(), record("4" * 32)]
    mesh, model, module = manual_runtime(records)
    original_encoder = module._encode_texts_with_tag
    monkeypatch.setenv("MESH_PASSAGE_INDEX", "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    readiness = {}
    with mock.patch.object(production, "verify_guarded_readiness") as verified:
        production.index_runtime_passages(mesh, records, module, readiness, Resources())
    assert verified.call_count == 1
    assert readiness["representation_bound_indexed_memories"] == 2
    assert readiness["completed_batches"] == 2
    assert readiness["pending_embedding_jobs"] == 0
    assert module._encode_texts_with_tag is original_encoder
    assert "_encode_raw_batches" not in vars(model)
    assert all(limit == (32, production.MODEL_TAG, production.PASSAGE_REPRESENTATION) for limit in mesh.store.limits)
    identifiers, expected = mesh.waits[0]
    assert expected["expected_revisions"] == dict.fromkeys(identifiers, 7)
    assert expected["expected_incarnations"] == dict.fromkeys(identifiers, INCARNATION)
    assert expected["expected_store_generation"] == GENERATION
    assert expected["target_representation"] == production.PASSAGE_REPRESENTATION
    assert expected["target_model"] == production.MODEL_TAG
    assert all(hint.incarnation == INCARNATION and hint.store_generation == GENERATION
               and hint.target_representation == production.PASSAGE_REPRESENTATION for hint in mesh.hints)


def test_manual_drain_stops_after_three_no_progress_batches(monkeypatch):
    mesh, model, module = manual_runtime([record()], progress=False)
    monkeypatch.setenv("MESH_PASSAGE_INDEX", "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    readiness = {}
    with pytest.raises(RuntimeError, match="no progress"):
        production.index_runtime_passages(mesh, [record()], module, readiness, Resources())
    assert readiness["completed_batches"] == 3
    assert readiness["status"] == "failed"
    assert readiness["resource_diagnostics"]["sampled_owned_process_rss_bytes_max"] > 0
    assert "_encode_raw_batches" not in vars(model)
    assert mesh.waits == []


@pytest.mark.parametrize("missing", ["current_revision", "current_incarnation", "store_generation", "stored_characters"])
def test_original_identity_cannot_fall_back_to_current_store(missing):
    source = record()
    source.pop(missing)
    with pytest.raises(ValueError, match="explicit original parent identity"):
        production.runtime_parent_identities([source])


@pytest.mark.parametrize("changed", ["revision", "incarnation", "store_generation", "embedding_model", "span_identity", "span_bounds"])
def test_runtime_export_rejects_stale_parent_model_and_supporting_spans(changed):
    result = candidate(passage=True)
    if changed == "span_identity":
        result["supporting_spans"][0]["incarnation"] = "5" * 32
    elif changed == "span_bounds":
        result["supporting_spans"][0]["end"] = 25
    else:
        result[changed] = 8 if changed == "revision" else "5" * 32
    with pytest.raises(ValueError):
        production.validate_runtime_parent_results([result], production.runtime_parent_identities([record()]))


@pytest.mark.parametrize("tamper", ["passage_flag", "parent_identity", "rank_hash", "query_identity"])
def test_fresh_default_pair_rejects_changed_flags_identity_hash_or_queries(tamper):
    with tempfile.TemporaryDirectory(prefix="mesh-frozen-quality-contract-") as temporary:
        root = Path(temporary)
        source, fresh, source_manifest, fresh_manifest, queries, records = paired_fixture(root)
        if tamper == "passage_flag":
            fresh_manifest["passage_index_enabled"] = True
        else:
            rows = list(replay.read_jsonl(fresh / "results-default.jsonl"))
            if tamper == "parent_identity":
                rows[0]["final_sdk_top10"][0]["incarnation"] = "5" * 32
            elif tamper == "query_identity":
                rows[0]["query_id"] = "other-query"
            else:
                rows[0]["candidate_cap"] = 96
            jsonl(fresh / "results-default.jsonl", rows)
            if tamper != "rank_hash":
                fresh_manifest["output_sha256"]["results-default.jsonl"] = replay.digest_file(fresh / "results-default.jsonl")
        production.write_json(fresh / "run-manifest.json", fresh_manifest)
        with pytest.raises(ValueError):
            production.load_fresh_default_pair(fresh, root, source, source_manifest, queries, records)


def test_guarded_query_only_backup_preserves_database_and_original_identity(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "copy"
    source.mkdir()
    (destination / "mesh-home").mkdir(parents=True)
    database = source / "mesh_fixture.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE memories(id TEXT,revision INTEGER,incarnation TEXT,store_generation TEXT,embedding_model TEXT)")
        connection.execute("INSERT INTO memories VALUES(?,?,?,?,?)", (MEMORY_ID, 7, INCARNATION, GENERATION, production.MODEL_TAG))
    before = production.guarded_database_fingerprint(source, "fixture")
    module = types.SimpleNamespace(validate_namespace=lambda value: value, sqlite3=sqlite3)
    production.copy_guarded_corpus(tmp_path, source, "fixture", destination, [record()], module)
    assert production.guarded_database_fingerprint(source, "fixture") == before
    with sqlite3.connect(destination / "mesh-home/mesh_fixture.db") as connection:
        assert connection.execute("SELECT * FROM memories").fetchone() == (MEMORY_ID, 7, INCARNATION, GENERATION, production.MODEL_TAG)


def test_actual_runtime_queries_use_ordinary_fresh_sdk_and_unchanged_default_pair():
    mesh, _model, module = manual_runtime([record()])
    calls = []

    def recall(query, **kwargs):
        calls.append(kwargs)
        module._embed_query_with_tag(query)
        row = candidate(passage=True)
        row["content"] = "FAKE_BODY_NOT_EXPORTED"
        row["supporting_spans"][0]["text"] = "FAKE_BODY_NOT_EXPORTED"
        return [row]

    mesh.recall = recall
    output, measured, metadata = io.StringIO(), io.StringIO(), {}
    original = {"query-1": [candidate()]}
    production.fresh_queries(mesh, [{"id": "query-1", "query": "synthetic query"}], [record()], {96: output}, metadata,
                             module, original_sdk_top10=original, resource_stream=measured, resources=Resources())
    row = json.loads(output.getvalue())
    assert calls == [{"n": 10, "include_private": False, "include_quarantined": False}]
    assert row["original_sdk_top10"] == original["query-1"]
    assert "FAKE_BODY_NOT_EXPORTED" not in output.getvalue()
    assert "query-1" not in measured.getvalue()
    assert metadata["runtime_passage_query_backend_observations"]["document_inference_attempts"] == 0
    assert json.loads(measured.getvalue())["query_ordinal"] == 1


def metrics(value=1.0, n=20):
    return {name: {"mean": value, "n": n, "ci95": [value, value]}
            for name in (*ci.C4_PAIRED_METRICS, "document_recall_at_10")}


def quality_pair():
    values = metrics()
    cell = {"final_sdk_top10": {"unconditional": copy.deepcopy(values)},
            "original_sdk_top10": {"unconditional": copy.deepcopy(values)},
            "candidate_minus_original_sdk": {name: {"mean": 0.0, "n": 20, "ci95": [0.0, 0.0]} for name in ci.C4_PAIRED_METRICS},
            "candidate_hit_at_1_regressions": {"answered_queries": 20, "baseline_hits": 20, "candidate_hits": 20,
                                               "lost_baseline_hits": 0, "gained_hits": 0}}
    return {"metrics": {"results-default": cell}, "gold_survival": {"answered_queries": 20}}, {
        "metrics": {"results-default": {"final_sdk_top10": {"unconditional": values}}}, "gold_survival": {"answered_queries": 20}}


def test_runtime_quality_gates_reject_public_metric_loss_and_pair_corruption():
    candidate_condition, baseline = quality_pair()
    assert all(gate["passed"] for gate in ci.runtime_passage_quality_gates("scifact", candidate_condition, baseline))
    cell = candidate_condition["metrics"]["results-default"]
    cell["final_sdk_top10"]["unconditional"]["ndcg_at_10"] = {"mean": 0.98, "n": 20, "ci95": [0.98, 0.98]}
    cell["candidate_minus_original_sdk"]["ndcg_at_10"] = {"mean": -0.02, "n": 20, "ci95": [-0.02, -0.02]}
    assert any(not gate["passed"] and "nonregression" in gate["name"]
               for gate in ci.runtime_passage_quality_gates("scifact", candidate_condition, baseline))
    cell["original_sdk_top10"]["unconditional"]["mrr_at_10"]["mean"] = 0.5
    assert any(not gate["passed"] and "paired_accounting" in gate["name"]
               for gate in ci.runtime_passage_quality_gates("scifact", candidate_condition, baseline))


@pytest.mark.parametrize("value", [None, {}, {"wall_seconds": 0.1, "process_cpu_seconds": 0.1, "sampled_owned_process_rss_bytes_max": 0},
                                   {"wall_seconds": float("nan"), "process_cpu_seconds": 0.1, "sampled_owned_process_rss_bytes_max": 1}])
def test_missing_or_invalid_runtime_resource_accounting_fails(value):
    with pytest.raises(ci.GateError):
        ci.runtime_resource_projection(value)


def test_runtime_phases_are_optional_and_keep_child_and_c4_conditions_separate():
    default = ci.initial_summary("prospective")
    requested = ci.initial_summary("prospective", c4=True, runtime_passages=True)
    assert [phase["name"] for phase in default["phases"]] == list(ci.PHASES)
    insertion = ci.PHASES.index("score-fresh-default") + 1
    assert [phase["name"] for phase in requested["phases"]] == [
        *ci.PHASES[:insertion], *ci.RUNTIME_PASSAGE_PHASES, *ci.PHASES[insertion:], *ci.C4_PHASES]
    assert requested["default_changed"] is False
    workflow = (BENCHMARKS.parent / ".github/workflows/retrieval-quality.yml").read_text(encoding="utf-8")
    assert workflow.count("run_runtime_passages:") == 2
    assert workflow.count('if ($env:RETRIEVAL_RUN_RUNTIME_PASSAGES -eq "true")') == 2


@pytest.mark.parametrize("failed_phase", ["passages", "c4-freeze"])
def test_scored_runtime_evidence_and_failed_gates_survive_later_experiment_failure(failed_phase):
    with tempfile.TemporaryDirectory(prefix="mesh-retrieval-ci-contract-") as temporary, mock.patch.dict(os.environ):
        repository = Path(temporary)
        benchmark = repository / "benchmarks"
        benchmark.mkdir()
        for name in (*ci.HARNESS_FILES, "retrieval_c4.py", "retrieval_c4_model.py", "provision_retrieval_c4.py"):
            (benchmark / name).write_text("# synthetic harness\n", encoding="utf-8")
        root = repository / ".work/run"
        (root / "data/prospective").mkdir(parents=True)
        (root / "data/prospective/manifest.json").write_text("{}\n", encoding="utf-8")
        (root / "protocol.json").write_text("{}\n", encoding="utf-8")
        phases, limits = [], []

        def supervise(command, **kwargs):
            script = Path(command[1]).name
            if script == "production_retrieval.py":
                phase = "score-" + Path(command[3]).name if command[2] == "score" else Path(command[command.index("--output") + 1]).name
            elif script == "retrieval_c4.py":
                phase = "c4-" + command[2]
            else:
                phase = {"provision_production_model.py": "provision", "prepare_retrieval_ci.py": "prepare",
                         "replay_retrieval.py": "replay", "passage_retrieval.py": "passages"}[script]
            phases.append(phase)
            limits.append((kwargs["rss_bytes"], kwargs["cpu_seconds"]))
            return {"status": "failed" if phase == failed_phase else "passed", "returncode": 1 if phase == failed_phase else 0,
                    "wall_seconds": 0.01, "sampled_tree_cpu_seconds": 0.0, "sampled_tree_peak_rss_bytes": 1024}

        def load(_directory, name):
            return {"query_count": 1, "corpus_count": 1} if name == "manifest.json" else {
                "status": "failed", "failure_type": "TypeError", "failure_details": {
                    "exception_type": "TypeError", "source_frames": [{"source": "passage_retrieval.py", "line": 1}]}}

        runtime_gates = [{"name": "runtime_passages_resource_accounting", "passed": True},
                         {"name": "runtime_passages_quality", "passed": False}]
        args = argparse.Namespace(dataset="prospective", model_directory=repository / ".work/models",
            qualification_directory=root, floor_qualification_directory=root, wall_seconds=600, rss_mib=64, cpu_seconds=600,
            runtime_passages=True, reranker_experiment="c4" if failed_phase == "c4-freeze" else "off")
        with mock.patch.object(ci, "hashes", return_value={}), mock.patch.object(ci, "harness_hashes", return_value={}), \
                mock.patch.object(ci, "qualify", return_value={"same_runtime_sources": True}), \
                mock.patch.object(ci, "supervise", side_effect=supervise), mock.patch.object(ci, "load_json", side_effect=load), \
                mock.patch.object(ci, "condition_evidence", side_effect=lambda *_args, **_kwargs: ({}, {"status": "complete"})), \
                mock.patch.object(ci, "runtime_passage_pairing"), \
                mock.patch.object(ci, "quality_gates", return_value=[{"name": "legacy_quality", "passed": False}]), \
                mock.patch.object(ci, "runtime_passage_quality_gates", side_effect=lambda *_args: copy.deepcopy(runtime_gates)):
            assert ci.run(args, repository, root) == 1
        summary = json.loads((root / "job-summary.json").read_text(encoding="utf-8"))
        assert phases.index("score-runtime-passages") == phases.index("score-fresh-default") + 2
        assert phases.index("score-runtime-passages") < phases.index("passages")
        assert "runtime-passages" in summary["conditions"]
        assert summary["optional_runtime_passage_index"] == "complete"
        assert all(gate in summary["quality_gates"] for gate in runtime_gates)
        assert summary["status"] == "failed"
        assert all(limit == (64 * 1024**2, 600) for limit in limits)
        if failed_phase == "passages":
            assert summary["failed_worker_diagnostics"]["exception_type"] == "TypeError"


def scored_condition(directory, records, results, *, child=False, missing_model=False):
    directory.mkdir()
    jsonl(directory / "ingestion-fidelity.jsonl", records)
    cell = "results-C64" if child else "results-default"
    jsonl(directory / (cell + ".jsonl"), [{"query_id": "query-1", "final_sdk_top10": results}])
    manifest = {"status": "complete", "all_queries_accounted": True, "query_target": 1,
        "labels_in_ranking_inputs": False, "runtime_files": RUNTIME,
        "model_revision": MODEL["revision"], "query_model_space_tag": production.MODEL_TAG,
        "model_file_sha256": {key: MODEL[key] for key in ("onnx_sha256", "tokenizer_sha256")},
        "protocol_sha256": "e" * 64, "dataset_manifest_sha256": "f" * 64,
        "output_sha256": {name: replay.digest_file(directory / name) for name in ("ingestion-fidelity.jsonl", cell + ".jsonl")}}
    if child:
        manifest.update(passage_ingestion={"complete": {"submitted_passages": 1}}, fixed_parameters={"requested_parent_budget": 96})
    if missing_model:
        manifest.pop("model_file_sha256")
    production.write_json(directory / "run-manifest.json", manifest)
    fields = {cohort: metrics(0.0, 1) for cohort in ("unconditional", "any_gold_survived", "all_gold_survived", "retained_gold_denominator")}
    score = {"query_count": 1, "run_manifest_sha256": replay.digest_file(directory / "run-manifest.json"),
             "cells": {cell: {"final_sdk_top10": fields}}, "gold_survival": {"answered_queries": 1},
             "ingestion": {"complete": {"accepted": len(records)}}}
    production.write_json(directory / "score-summary.json", score)
    return manifest, score


def test_public_unfaithful_parent_keeps_rank_and_receives_zero_gold_credit(tmp_path):
    source, returned = record(), candidate()
    source.update(faithful_public=False, body_changed=True)
    returned["faithful_document_ids"] = []
    directory = tmp_path / "guarded"
    scored_condition(directory, [source], [returned])
    _manifest, evidence = ci.condition_evidence(directory, {"query_count": 1, "corpus_count": 1}, RUNTIME, MODEL, "e" * 64, scored=True)
    assert evidence["unfaithful_public_exported_candidates"] == 1
    assert production.candidate_document(returned) is None
    assert production.query_metrics([returned], {"doc": 1})["hit_at_1"] == 0
    production.validate_runtime_parent_results([returned], production.runtime_parent_identities([source]))


@pytest.mark.parametrize("tamper", ["missing_ids", "invented_ids", "private_source", "quarantined_source", "unknown_parent", "duplicate_identity"])
def test_public_fidelity_projection_never_waives_source_privacy_or_identity(tmp_path, tamper):
    source, returned = record(), candidate()
    source.update(faithful_public=False, body_changed=True)
    returned["faithful_document_ids"] = []
    records = [source]
    if tamper == "missing_ids":
        returned.pop("faithful_document_ids")
    elif tamper == "invented_ids":
        returned["faithful_document_ids"] = ["doc"]
    elif tamper == "unknown_parent":
        returned["id"] = "8" * 32
    elif tamper == "duplicate_identity":
        duplicate = dict(source, current_incarnation="9" * 32)
        records.append(duplicate)
    else:
        source["private" if tamper == "private_source" else "quarantined"] = True
    directory = tmp_path / "guarded"
    scored_condition(directory, records, [returned])
    with pytest.raises(ci.GateError):
        ci.condition_evidence(directory, {"query_count": 1, "corpus_count": len(records)}, RUNTIME, MODEL, "e" * 64, scored=True)


@pytest.mark.parametrize("tamper", [None, "source_hash", "runtime", "model_pin", "source_export"])
def test_legacy_child_model_pins_require_exact_verified_guarded_source(tmp_path, tamper):
    guarded, child = tmp_path / "guarded", tmp_path / "passages"
    source_manifest, _ = scored_condition(guarded, [record()], [candidate()])
    manifest, score = scored_condition(child, [record()], [candidate()], child=True, missing_model=True)
    manifest.update(source_run_manifest_sha256=replay.digest_file(guarded / "run-manifest.json"),
                    source_output_sha256=source_manifest["output_sha256"])
    if tamper == "source_hash":
        manifest["source_run_manifest_sha256"] = "9" * 64
    elif tamper == "runtime":
        source_manifest["runtime_files"] = {"mesh/example.py": "9" * 64}
    elif tamper == "model_pin":
        source_manifest["model_file_sha256"]["onnx_sha256"] = "9" * 64
    elif tamper == "source_export":
        (guarded / "ingestion-fidelity.jsonl").write_text("{}\n", encoding="utf-8")
    if tamper in {"runtime", "model_pin"}:
        production.write_json(guarded / "run-manifest.json", source_manifest)
        manifest["source_run_manifest_sha256"] = replay.digest_file(guarded / "run-manifest.json")
    production.write_json(child / "run-manifest.json", manifest)
    score["run_manifest_sha256"] = replay.digest_file(child / "run-manifest.json")
    production.write_json(child / "score-summary.json", score)
    if tamper:
        with pytest.raises(ValueError):
            ci.condition_evidence(child, {"query_count": 1, "corpus_count": 1}, RUNTIME, MODEL, "e" * 64, scored=True, guarded_source=guarded)
    else:
        _manifest, evidence = ci.condition_evidence(child, {"query_count": 1, "corpus_count": 1}, RUNTIME, MODEL, "e" * 64, scored=True, guarded_source=guarded)
        assert evidence["model_file_sha256"] == source_manifest["model_file_sha256"]
        assert evidence["model_file_provenance"] == "hash_verified_guarded_source"
