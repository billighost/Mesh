"""Small model-free query/cache telemetry and stopped-worker artifact contracts."""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
import production_retrieval as production
import retrieval_telemetry as telemetry
import runtime_rerank_c4 as runtime


def cache():
    return dict(zip(telemetry.CACHE_FIELDS, (256 * 1024**2, 1024, 1, 1, 0)))


def row(ordinal=1, *, modern=True):
    value = {"query_ordinal": ordinal, "elapsed_ms": 2.0, "process_cpu_ms": 1.0,
             "sampled_owned_process_rss_bytes": 1024}
    if modern:
        value.update(telemetry.query_observation([{"retrieval_mode": "cached"}], cache()))
    return value


def expected(target=2):
    return {"runtime_files": {"mesh/store.py": "a" * 64},
            "model_file_sha256": {"onnx_sha256": "b" * 64, "tokenizer_sha256": "c" * 64},
            "dataset_manifest_sha256": "d" * 64, "protocol_sha256": "e" * 64,
            "source_run_manifest_sha256": "f" * 64, "query_target": target}


def stopped_export(path, rows, *, target=2, tail=b""):
    path.mkdir(exist_ok=True)
    source = expected(target)
    source.update(status="running", passage_index_enabled=True, labels_in_ranking_inputs=False,
                  query_telemetry_version=1, cache_observations={"PRIVATE_FIXTURE_PATH": cache()})
    telemetry.checkpoint_query_provenance(path / "run-manifest.json", source)
    (path / "query-resources.jsonl").write_bytes(b"".join(json.dumps(item).encode() + b"\n" for item in rows) + tail)
    return expected(target)


def test_completed_and_old_query_resources_keep_numeric_resource_contract():
    old, absent = telemetry.query_resources_projection([row(modern=False)], 1, complete=True)
    modern, observations = telemetry.query_resources_projection([row()], 1, complete=True, version=1)
    assert absent is None and modern == old
    assert observations["query_mode_counts"] == {"cached": 1, "streaming": 0, "unreported": 0, "mixed": 0, "empty": 0}
    assert observations["cache_stats_last"] == cache()


def test_modes_are_reported_without_exporting_unknown_mode_text():
    observation = telemetry.query_observation([{"retrieval_mode": "cached"}, {"retrieval_mode": "streaming"},
                                               {"retrieval_mode": "PRIVATE_MODE_TEXT"}], cache())
    resources, result = telemetry.query_resources_projection([row() | observation,
        row(2) | telemetry.query_observation([], cache())], 2, complete=True, version=1)
    assert resources["query_count"] == 2
    assert result["query_mode_counts"]["mixed"] == result["query_mode_counts"]["empty"] == 1
    assert "PRIVATE_MODE_TEXT" not in json.dumps(result)


@pytest.mark.parametrize("change", [
    lambda item: item.update(query_ordinal=True),
    lambda item: item.update(process_cpu_ms=float("nan")),
    lambda item: item.update(sampled_owned_process_rss_bytes=0),
    lambda item: item.update(memory_body="PRIVATE_BODY"),
    lambda item: item["result_mode_counts"].update(cached=11),
    lambda item: item["result_mode_counts"].update(streaming=-1),
    lambda item: item["cache_stats"].update(charged_bytes=True),
    lambda item: item["cache_stats"].update(path="PRIVATE_PATH"),
])
def test_invalid_or_content_bearing_rows_are_rejected(change):
    item = row()
    change(item)
    with pytest.raises(ValueError):
        telemetry.query_resources_projection([item], 1, complete=True, version=1)


def test_query_resource_counts_and_finite_totals_are_required():
    with pytest.raises(ValueError):
        telemetry.query_resources_projection([row()], 2, complete=True, version=1)
    with pytest.raises(ValueError):
        telemetry.query_resources_projection([row(), row()], 2, complete=True, version=1)
    with pytest.raises(ValueError):
        telemetry.query_resources_projection([row() | {"elapsed_ms": 1e308}, row(2) | {"elapsed_ms": 1e308}],
                                             2, complete=True, version=1)


def test_interrupted_progress_is_unscored_and_ignores_only_unterminated_tail(tmp_path):
    source = stopped_export(tmp_path, [row()], tail=b'{"query_ordinal": 2')
    result = telemetry.progress_from_run(tmp_path, source)
    assert result["status"] == "unscored_progress" and result["query_resources"]["query_count"] == 1
    assert result["unterminated_tail_ignored"] is True and result["complete_query_resource_export"] is False
    assert "PRIVATE_FIXTURE_PATH" not in (tmp_path / "run-manifest.json").read_text()
    assert "PRIVATE_FIXTURE_PATH" not in json.dumps(result)
    (tmp_path / "query-resources.jsonl").write_bytes(b'{"invalid":}\n')
    with pytest.raises(ValueError):
        telemetry.progress_from_run(tmp_path, source)


def test_real_stopped_worker_retains_only_flushed_numeric_progress(tmp_path):
    source = stopped_export(tmp_path, [])
    program = "import json,os,sys; p=json.load(sys.stdin); f=open(p['path'],'ab'); f.write(p['row'].encode()+b'\\n'); f.flush(); f.write(b'{'); f.flush(); os._exit(19)"
    result = subprocess.run([sys.executable, "-c", program], input=json.dumps({
        "path": str(tmp_path / "query-resources.jsonl"), "row": json.dumps(row())}), text=True, capture_output=True, timeout=10)
    assert result.returncode == 19
    progress = telemetry.progress_from_run(tmp_path, source)
    assert progress["query_resources"]["query_count"] == 1 and progress["unterminated_tail_ignored"] is True


@pytest.mark.parametrize("parent_manifest", [None, b'{"status":', b'[]'])
def test_independent_role_progress_survives_missing_or_truncated_native_parent(tmp_path, parent_manifest):
    directory = tmp_path / "native"
    directory.mkdir()
    roles = directory / "query-progress"
    roles.mkdir()
    source = stopped_export(roles / "baseline", [row()])
    stopped_export(roles / "c4", [row()], tail=b'{"unfinished":')
    if parent_manifest is not None:
        (directory / "run-manifest.json").write_bytes(parent_manifest)
    progress = telemetry.matched_progress_after_stop(directory, source)
    assert set(progress) == {"baseline", "c4"}
    assert all(value["status"] == "unscored_progress" for value in progress.values())
    assert progress["c4"]["query_resources"]["query_count"] == 1


@pytest.mark.parametrize("key", ["runtime_files", "model_file_sha256", "dataset_manifest_sha256",
                                 "protocol_sha256", "source_run_manifest_sha256", "query_target"])
def test_progress_requires_expected_source_model_and_protocol(tmp_path, key):
    source = stopped_export(tmp_path, [row()])
    path = tmp_path / "run-manifest.json"
    manifest = json.loads(path.read_text())
    manifest[key] = {} if isinstance(manifest[key], dict) else 3 if key == "query_target" else "0" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        telemetry.progress_from_run(tmp_path, source)


@pytest.mark.parametrize("change", [
    lambda value: value.update(body="PRIVATE_BODY"),
    lambda value: value["query_resources"].update(query_count=True),
    lambda value: value["query_resources"].update(elapsed_ms_total=float("inf")),
    lambda value: value["query_telemetry"]["query_mode_counts"].update(cached=2),
    lambda value: value["query_telemetry"].update(cache_stats_last=None),
    lambda value: value["cache_observations"][0].update(body="PRIVATE_BODY"),
])
def test_public_progress_rejects_free_text_and_forged_counts(tmp_path, change):
    source = stopped_export(tmp_path, [row()])
    value = copy.deepcopy(telemetry.progress_from_run(tmp_path, source))
    change(value)
    with pytest.raises(ValueError):
        telemetry.project_progress(value, source)


def test_fresh_query_producer_copies_numeric_rows_and_restores_hooks(monkeypatch):
    module = SimpleNamespace(_embed_query_with_tag=lambda _text: ([0.0] * 384, production.MODEL_TAG),
                             _encode_texts_with_tag=lambda *_args: None, _process_reembed_batch=lambda *_args: None)
    original = module._embed_query_with_tag

    class Mesh:
        def recall(self, text, **kwargs):
            assert kwargs == {"n": 10, "include_private": False, "include_quarantined": False}
            module._embed_query_with_tag(text)
            return [{"retrieval_mode": "streaming"}]

    monkeypatch.setattr(production, "runtime_parent_identities", lambda _records: {})
    monkeypatch.setattr(production, "validate_runtime_parent_results", lambda *_args: None)
    def decorate(_results, _by_memory, *, score_components):
        assert score_components is True
        return []

    monkeypatch.setattr(production, "decorate_results", decorate)
    resource_stream, stable_stream, results_stream = io.StringIO(), io.StringIO(), io.StringIO()
    production.fresh_queries(Mesh(), [{"id": "PRIVATE_QUERY_ID", "query": "PRIVATE_QUERY_TEXT"}], [],
        {96: results_stream}, {}, module, original_sdk_top10={"PRIVATE_QUERY_ID": []},
        resource_stream=resource_stream, progress_resource_stream=stable_stream,
        resources=SimpleNamespace(sample=lambda: 1024), cache_stats=cache)
    assert resource_stream.getvalue() == stable_stream.getvalue() and "PRIVATE_QUERY" not in stable_stream.getvalue()
    assert json.loads(stable_stream.getvalue())["result_mode_counts"]["streaming"] == 1
    assert module._embed_query_with_tag is original


@pytest.mark.parametrize("phase", ["baseline", "c4"])
def test_native_measure_forwards_only_its_fixed_progress_role(tmp_path, monkeypatch, phase):
    source = tmp_path / "source"
    source.mkdir()
    (source / "query-vectors.jsonl").write_text(json.dumps({"namespace": "fixture"}) + "\n")
    args = SimpleNamespace(dataset="prospective", repository=tmp_path, dataset_directory=tmp_path,
        base_protocol=tmp_path / "base.json", embedding_model_directory=tmp_path, dependency_directory=None,
        output=tmp_path / "output", source_run=source, runtime_passage_run=tmp_path / "passages",
        fresh_default_run=tmp_path / "fresh", query_progress_root=tmp_path / "persistent", phase=phase)
    calls = []
    monkeypatch.setattr(runtime, "verify_protocol", lambda *_args: None)
    monkeypatch.setattr(runtime, "RuntimeObserver", lambda *_args: object())
    monkeypatch.setattr(production, "run_retrieval", lambda _options, **kwargs: calls.append(kwargs))
    runtime.measure(args, production)
    assert calls[0]["query_progress_directory"] == args.query_progress_root / phase
