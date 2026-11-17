"""Production measurements must preserve failures, verified artifacts and write counts."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import production_bench as bench


def _raw_ack(memory_id="memory-one", **overrides):
    return {"status": "stored", "memory_id": memory_id, "revision": 1, "deduplicated": False, **overrides}


def _report(entries, failures=()):
    result = bench._empty_write_report()
    result.update(attempted=len(entries) + len(failures), acknowledgements=entries, failures=list(failures))
    return result


def _config(root, **overrides):
    config = {
        "root": str(root), "repo": str(bench.REPO_ROOT), "token": "f" * 32,
        "source_manifest": bench.source_manifest(bench.REPO_ROOT),
        "action": "ingest", "backend": "hash", "actor": "benchmark-test",
        "timeout": 15, "mode": "scalar", "batch_size": 2, "indices": [0, 1, 2],
        "result": str(root / "result-0.json"), "ready": str(root / "ready-0.json"),
        "journal": str(root / "journal-0.jsonl"), "go": str(root / "go.json"), **overrides,
    }
    bench.write_json(root / "benchmark-only.json", {"token": config["token"], "fixture_version": bench.FIXTURE_VERSION})
    bench.write_json(root / "config-0.json", config)
    return config


@pytest.fixture
def benchmark_root():
    temporary = tempfile.TemporaryDirectory(prefix="mesh-production-bench-")
    root = Path(temporary.name)
    try:
        yield root
    finally:
        # Inventory this new root before its bounded automatic cleanup.
        inventory = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
        assert root.resolve().parent == Path(tempfile.gettempdir()).resolve()
        assert root.name.startswith("mesh-production-bench-")
        assert all(not Path(name).is_absolute() for name in inventory)
        temporary.cleanup()


@pytest.mark.parametrize("changes", [
    {"status": "ok"}, {"error": "failure"}, {"memory_id": ""}, {"memory_id": "../outside"},
    {"revision": True}, {"revision": 0}, {"deduplicated": "false"}, {"deduplicated": None},
    {"disposition": "deduplicated"},
])
def test_unconfirmed_replies_never_count_as_committed(changes):
    with pytest.raises(ValueError):
        bench.acknowledgement(_raw_ack(**changes), 0)


def test_attempted_inserted_deduplicated_unknown_and_unattempted_are_separate():
    inserted = bench.acknowledgement(_raw_ack(), 0)
    duplicate = bench.acknowledgement(_raw_ack(deduplicated=True), 1)
    uncertain = bench._row_failure(2, "UnknownOutcome")
    report = bench.write_accounting([_report([inserted, duplicate], [uncertain])], 4)
    assert report["attempted_rows"] == 3 and report["acknowledged_rows"] == 2
    assert report["unique_inserted_ids"] == 1 and report["deduplicated_acknowledgements"] == 1
    assert report["failed_acknowledgements"] == 1 and report["unknown_outcome_rows"] == 1
    assert report["unattempted_rows"] == 1 and report["accounting_consistent"] is True


@pytest.mark.parametrize("second", [
    bench.acknowledgement(_raw_ack(), 1),
    bench.acknowledgement(_raw_ack("memory-two"), 0),
])
def test_duplicate_inserted_ids_or_fixture_indices_cannot_pass(second):
    first = bench.acknowledgement(_raw_ack(), 0)
    assert bench.write_accounting([_report([first, second])], 2)["accounting_consistent"] is False


@pytest.mark.parametrize("reply", [
    [_raw_ack()],
    [_raw_ack(), {"status": "rejected"}],
])
def test_bad_atomic_reply_preserves_each_confirmed_sibling_and_never_replays(tmp_path, reply):
    calls = []
    def learn_many(items, **options):
        calls.append((items, options))
        return reply
    config = {"indices": [0, 1], "mode": "atomic", "batch_size": 2, "journal": str(tmp_path / "journal.jsonl")}
    report = bench._ingest(SimpleNamespace(learn_many=learn_many), config)
    assert len(calls) == 1 and calls[0][1] == {"atomic": True, "deduplicate": False}
    assert report["attempted"] == 2 and len(report["acknowledgements"]) == len(report["failures"]) == 1
    assert report["acknowledgements"][0]["memory_id"] == "memory-one"
    assert report["latencies_ms"] == []
    recovered = bench.recover_progress(config)
    assert recovered["acknowledgements"] == report["acknowledgements"]
    assert recovered["failures"] == report["failures"]
    assert recovered["failures"][0]["outcome_may_be_committed"] is (len(reply) == 1)


def test_extra_atomic_results_are_protocol_failure_even_when_mapped_rows_are_confirmed(tmp_path):
    mesh = SimpleNamespace(learn_many=lambda *args, **kwargs: [_raw_ack(), _raw_ack("memory-two"), _raw_ack("extra")])
    config = {"indices": [0, 1], "mode": "atomic", "batch_size": 2, "journal": str(tmp_path / "journal.jsonl")}
    report = bench._ingest(mesh, config)
    assert len(report["acknowledgements"]) == 2 and report["latencies_ms"] == []
    assert bench.write_accounting([report], 2)["accounting_consistent"] is False


def test_real_worker_death_preserves_prior_ack_and_last_unresolved_attempt(benchmark_root):
    config = _config(benchmark_root)
    script = benchmark_root / "death_worker.py"
    script.write_text(
        "import json, os, sys\n"
        "from types import SimpleNamespace\n"
        "from benchmarks.production_bench import _ingest\n"
        "calls = 0\n"
        "def learn(**kwargs):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    if calls == 2:\n"
        "        os._exit(19)\n"
        "    return {'status': 'stored', 'memory_id': 'first', 'revision': 1, 'deduplicated': False}\n"
        "config = json.loads(open(sys.argv[1], encoding='utf-8').read())\n"
        "_ingest(SimpleNamespace(learn=learn), config)\n",
        encoding="utf-8",
    )
    environment = bench.worker_environment(benchmark_root, benchmark_root / "home", "hash", bench.REPO_ROOT)
    process = subprocess.Popen([sys.executable, str(script), str(benchmark_root / "config-0.json")],
                               cwd=bench.REPO_ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    coordinator = bench.Coordinator(benchmark_root, bench.REPO_ROOT, "hash", 15)
    coordinator.processes.append(process)
    try:
        recovered = coordinator.collect(process, config)
    finally:
        coordinator.stop()
    assert recovered["exit_code"] == 19 and recovered["worker_failure"] == "MissingOrInvalidResult"
    report = bench.write_accounting([recovered], 3)
    assert report["attempted_rows"] == 2 and report["acknowledged_rows"] == 1
    assert report["acknowledgements"][0]["memory_id"] == "first"
    assert report["unknown_outcome_rows"] == 1 and report["unattempted_rows"] == 1
    assert process.poll() is not None


def test_truncated_journal_does_not_label_unobserved_inputs_as_unattempted(tmp_path):
    journal = tmp_path / "progress.jsonl"
    journal.write_text('{"kind":"attempt","sequence":0,"indices":[0]}\n{"kind":"attempt"', encoding="utf-8")
    recovered = bench.recover_progress({"journal": str(journal), "indices": [0, 1, 2]})
    report = bench.write_accounting([recovered], 3)
    assert report["attempted_rows"] == 1 and report["unknown_outcome_rows"] == 1
    assert report["unattempted_rows"] is None and report["unobserved_rows"] == 2
    assert report["accounting_consistent"] is False


def test_exit_error_after_result_publication_does_not_erase_acknowledgements(benchmark_root):
    config = _config(benchmark_root, indices=[0])
    report = _report([bench.acknowledgement(_raw_ack(), 0)])
    bench.write_json(Path(config["result"]), report)
    process = SimpleNamespace(wait=lambda timeout: 31)
    observed = bench.Coordinator(benchmark_root, bench.REPO_ROOT, "hash", 15).collect(process, config)
    assert observed["exit_code"] == 31 and observed["worker_failure"] == "UnexpectedProcessExit"
    assert observed["attempted"] == 1 and observed["acknowledgements"] == report["acknowledgements"]


def test_timeout_still_collects_already_published_confirmed_results(benchmark_root):
    config = _config(benchmark_root, indices=[0])
    bench.write_json(Path(config["result"]), _report([bench.acknowledgement(_raw_ack(), 0)]))
    calls = []
    def wait(timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("synthetic", timeout)
        return -9
    killed = []
    process = SimpleNamespace(wait=wait, kill=lambda: killed.append(True))
    report = bench.Coordinator(benchmark_root, bench.REPO_ROOT, "hash", 15).collect(process, config)
    assert killed == [True] and report["worker_failure"] == "Timeout"
    assert len(report["acknowledgements"]) == 1 and report["exit_code"] == -9


def test_worker_config_cannot_redirect_results_outside_its_temporary_root(benchmark_root, tmp_path):
    outside = tmp_path / "outside.json"
    _config(benchmark_root, result=str(outside))
    with pytest.raises(ValueError, match="outputs must stay"):
        bench._validate_config(benchmark_root / "config-0.json")
    assert not outside.exists()


def test_worker_constructor_failure_is_structured_and_closes_nothing(benchmark_root, monkeypatch):
    from mesh import memory
    _config(benchmark_root)
    def fail(**kwargs):
        raise RuntimeError("Safe synthetic bootstrap failure")
    monkeypatch.setattr(memory, "Mesh", fail)
    assert bench.worker(benchmark_root / "config-0.json") == 1
    report = bench._read_json(benchmark_root / "result-0.json")
    assert report["error_stage"] == "construct_mesh" and report["error_type"] == "RuntimeError"
    assert report["attempted"] == 0 and report["acknowledgements"] == []


def test_changed_runtime_source_is_rejected_before_mesh_construction(benchmark_root, monkeypatch):
    from mesh import memory
    manifest = bench.source_manifest(bench.REPO_ROOT)
    manifest["mesh/memory.py"] = "0" * 64
    _config(benchmark_root, source_manifest=manifest)
    constructed = []
    monkeypatch.setattr(memory, "Mesh", lambda **kwargs: constructed.append(True))
    assert bench.worker(benchmark_root / "config-0.json") == 1
    report = bench._read_json(benchmark_root / "result-0.json")
    assert report["error_stage"] == "verify_runtime_source" and not constructed
    assert report["attempted"] == 0


def test_readiness_failure_is_structured_and_the_constructed_mesh_is_closed(benchmark_root, monkeypatch):
    from mesh import memory
    _config(benchmark_root)
    closed = []
    mesh = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(memory, "Mesh", lambda **kwargs: mesh)
    def fail(*args):
        raise RuntimeError("Expected FULL runtime is unavailable")
    monkeypatch.setattr(bench, "_metadata", fail)
    assert bench.worker(benchmark_root / "config-0.json") == 1
    report = bench._read_json(benchmark_root / "result-0.json")
    assert report["error_stage"] == "runtime_readiness" and closed == [True]


def test_close_failure_preserves_the_completed_worker_write_report(benchmark_root, monkeypatch):
    from mesh import memory
    config = _config(benchmark_root, action="seed", indices=[0])
    def close():
        raise RuntimeError("Synthetic teardown failure")
    mesh = SimpleNamespace(learn=lambda **kwargs: _raw_ack(), close=close)
    monkeypatch.setattr(memory, "Mesh", lambda **kwargs: mesh)
    monkeypatch.setattr(bench, "_metadata", lambda *args: {"synchronous": "FULL"})
    assert bench.worker(benchmark_root / "config-0.json") == 1
    report = bench._read_json(Path(config["result"]))
    assert report["worker_failure"] == "CloseFailed" and report["close_error_type"] == "RuntimeError"
    assert report["attempted"] == 1 and report["acknowledgements"][0]["memory_id"] == "memory-one"


def _model_fixture(tmp_path):
    source, destination, repo = (tmp_path / name for name in ("source", "destination", "checkout"))
    source.mkdir()
    (repo / "mesh").mkdir(parents=True)
    original, alternate, tokenizer = b"approved-first-model", b"approved-second-model", b"approved-tokenizer"
    model_hashes = {hashlib.sha256(value).hexdigest() for value in (original, alternate)}
    tokenizer_hash = hashlib.sha256(tokenizer).hexdigest()
    (repo / "mesh" / "memory.py").write_text(
        f"_ALLOWED_MODEL_SHA256 = {model_hashes!r}\n_ALLOWED_TOKENIZER_SHA256 = {{{tokenizer_hash!r}}}\n",
        encoding="utf-8",
    )
    (source / "model.onnx").write_bytes(original)
    (source / "tokenizer.json").write_bytes(tokenizer)
    return source, destination, repo, original, alternate


def test_model_manifest_describes_copied_bytes_when_approved_source_changes(tmp_path, monkeypatch):
    source, destination, repo, original, alternate = _model_fixture(tmp_path)
    replace = os.replace
    def change_source(temporary, target):
        if Path(target).name == "model.onnx":
            (source / "model.onnx").write_bytes(alternate)
        replace(temporary, target)
    monkeypatch.setattr(bench.os, "replace", change_source)
    manifest = bench.copy_verified_model(source, destination, repo)
    copied = destination / "models" / "all-MiniLM-L6-v2-onnx" / "model.onnx"
    assert copied.read_bytes() == original and (source / "model.onnx").read_bytes() == alternate
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == manifest["model.onnx"]


def test_unknown_model_checksum_is_rejected_without_publishing_a_model(tmp_path):
    source, destination, repo, _, _ = _model_fixture(tmp_path)
    (source / "model.onnx").write_bytes(b"unapproved-artifact")
    with pytest.raises(ValueError, match="checksum"):
        bench.copy_verified_model(source, destination, repo)
    assert not (destination / "models" / "all-MiniLM-L6-v2-onnx" / "model.onnx").exists()


def test_windows_reparse_points_are_rejected_without_path_is_junction(tmp_path, monkeypatch):
    target = tmp_path / "junction"
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="reparse points"):
        bench._reject_links(target / "model.onnx")


def test_worker_environment_discards_real_mesh_bindings_and_cloud_configuration(benchmark_root, monkeypatch):
    monkeypatch.setenv("MESH_HOME", "/fake-original")
    monkeypatch.setenv("MESH_ENCRYPTION_KEY", "fake-key")
    monkeypatch.setenv("MESH_SERVER_URL", "https://fake.invalid")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-provider-token")
    environment = bench.worker_environment(benchmark_root, benchmark_root / "agent-home", "hash", bench.REPO_ROOT)
    assert environment["MESH_HOME"] == str(benchmark_root / "mesh")
    assert "MESH_ENCRYPTION_KEY" not in environment and "MESH_SERVER_URL" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["MESH_NO_AUTO_DAEMON"] == environment["MESH_NO_MODEL_DOWNLOAD"] == "1"
    assert environment["MESH_ENABLE_AUTO_SYNC"] == environment["MESH_AUTO_APPROVE"] == "0"
    assert environment["HF_HUB_OFFLINE"] == environment["TRANSFORMERS_OFFLINE"] == "1"
    assert json.loads(environment["MESH_AGENT_SCOPES"])["*"]["namespaces"] == [bench.NAMESPACE]


def test_hash_mode_does_not_report_neural_readiness_or_fully_indexed_throughput():
    report = bench._dense_readiness(None, {"backend": "hash"}, [])
    assert report["status"] == "unavailable" and "fully_indexed_input_rows_per_second" not in report


@pytest.mark.parametrize("revision,model", [(2, "onnx-minilm-l6-v2"), (1, "hash-384")])
def test_dense_throughput_requires_matching_expected_revision_and_model(revision, model):
    calls = []
    target = "onnx-minilm-l6-v2"
    def wait(ids, **kwargs):
        calls.append((ids, kwargs))
        return {"status": "indexed", "indexed": ids, "revisions": {"memory-one": revision},
                "embedding_models": {"memory-one": model}}
    mesh = SimpleNamespace(wait_for_indexed=wait,
                           store=SimpleNamespace(_connect=lambda: nullcontext(
                               SimpleNamespace(execute=lambda *args: [(target,)]))))
    config = {"backend": "neural", "metadata": {"query_model_tag": target}, "timeout": 5,
              "submitted": 1, "write_started_ns": time.perf_counter_ns()}
    report = bench._dense_readiness(mesh, config, [bench.acknowledgement(_raw_ack(), 0)])
    assert calls[0][1]["expected_revisions"] == {"memory-one": 1} and calls[0][1]["target_model"] == target
    assert report["status"] == "not_ready" and report["fully_indexed_input_rows_per_second"] is None


@pytest.mark.parametrize("version,synchronous,journal,backend,tag", [
    ("3.40.1", 2, "wal", "hash", "hash-384"),
    ("3.52.0", 1, "wal", "hash", "hash-384"),
    ("3.52.0", 2, "delete", "hash", "hash-384"),
    ("3.52.0", 2, "wal", "neural", "hash-384"),
])
def test_unsafe_sqlite_or_unready_neural_runtime_cannot_produce_measurements(
        version, synchronous, journal, backend, tag, monkeypatch):
    from mesh import memory
    monkeypatch.setitem(sys.modules, "mesh.sqlite_runtime", SimpleNamespace(is_patched_sqlite=lambda value: value == "3.52.0"))
    monkeypatch.setattr(memory, "_embed_query_with_tag", lambda value: ([0.0], tag))
    monkeypatch.setattr(memory, "get_engine_status", lambda: {"model_loaded": tag != "hash-384"})
    rows = {"SELECT sqlite_version()": version, "PRAGMA synchronous": synchronous, "PRAGMA journal_mode": journal}
    connection = SimpleNamespace(execute=lambda sql: SimpleNamespace(fetchone=lambda: [rows[sql]]))
    mesh = SimpleNamespace(store=SimpleNamespace(_connect=lambda: nullcontext(connection)))
    with pytest.raises(RuntimeError):
        bench._metadata(mesh, backend)


@pytest.mark.parametrize("number", ["NaN", "Infinity", "1e9999"])
def test_nonfinite_worker_json_is_rejected_before_report_publication(tmp_path, number):
    report = tmp_path / "result.json"
    report.write_text('{"latency":' + number + '}', encoding="utf-8")
    with pytest.raises(ValueError, match="nonfinite"):
        bench._read_json(report)
