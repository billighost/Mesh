"""Benchmark failures must remain visible instead of looking like fast successful calls."""
from __future__ import annotations

import concurrent.futures
import os
from collections import Counter
from types import SimpleNamespace

import pytest

from benchmark_all_surfaces import BenchmarkRecorder
from benchmarks.concurrency_stress import _WriteTally, _collect_futures, _interleaved, _write_phase
from benchmarks.isolation import isolated_benchmark_home


def test_all_future_results_are_observed_even_after_exception():
    observed = []
    class Future(concurrent.futures.Future):
        def result(self, *args, **kwargs):
            observed.append(self)
            return super().result(*args, **kwargs)
    futures = [Future(), Future(), Future()]
    futures[0].set_exception(RuntimeError("synthetic worker failure"))
    futures[1].set_result("second")
    futures[2].set_result("third")
    results, errors = _collect_futures(futures)
    assert set(results) == {"second", "third"}
    assert errors == Counter({"RuntimeError": 1}) and set(observed) == set(futures)


def test_committed_deduplicated_and_failed_writes_are_distinct():
    tally = _WriteTally(attempted=3)
    tally.record({"status": "stored", "memory_id": "one", "disposition": "inserted", "deduplicated": False})
    tally.record({"status": "stored", "memory_id": "one", "disposition": "deduplicated", "deduplicated": True})
    tally.record({"status": "rejected", "disposition": "rejected"})
    report = _write_phase(SimpleNamespace(count=lambda: 11), 3, 10, [tally], Counter(), 1)
    assert report["submitted"] == 3 and report["committed_operations"] == 2
    assert report["inserted"] == 1 and report["deduplicated"] == 1 and report["failed"] == 1
    assert report["verified_new_records"] == 1 and report["count_integrity_ok"] is True
    assert report["status"] == "FAILED"


def test_worker_failure_and_unattempted_writes_cannot_pass():
    tally = _WriteTally(attempted=1, inserted=1)
    report = _write_phase(SimpleNamespace(count=lambda: 1), 2, 0, [tally], Counter({"ValueError": 1}), 1)
    assert report["unattempted"] == 1 and report["worker_errors"] == {"ValueError": 1}
    assert report["status"] == "FAILED"


def test_interleaved_reader_failure_is_reported(monkeypatch):
    from mesh import memory
    coordinator = SimpleNamespace(count=lambda: 0)
    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def recall(self, *args, **kwargs):
            raise RuntimeError("synthetic reader failure")
        def learn(self, *args, **kwargs):
            return {"status": "rejected"}
    monkeypatch.setattr(memory, "Mesh", Client)
    report = _interleaved(coordinator, "synthetic", 1, 1, 0.05)
    assert report["reads"]["failed"] == 1 and report["status"] == "FAILED"


@pytest.mark.parametrize("failure", [RuntimeError("synthetic"), {"status": "rejected"}, SimpleNamespace(isError=True), SimpleNamespace(status_code=403)])
def test_failed_latency_samples_are_excluded_and_never_marked_pass(failure):
    recorder = BenchmarkRecorder()
    def operation(index):
        if isinstance(failure, Exception):
            raise failure
        return failure
    result = recorder.measure("mcp_handler_inproc", "synthetic failure", operation, iterations=3, warmup=1)
    assert result["error_count"] == 3 and result["warmup_error_count"] == 1
    assert result["successful_iterations"] == 0
    assert result["meets_5ms_p50"] is False and result["meets_5ms_p95"] is False
    assert result["measurement_boundary"] == "in_process_handler"


def test_full_benchmark_home_is_temporary_and_restored_after_failure(tmp_path, monkeypatch):
    from mesh import audit, namespaces, store
    monkeypatch.setattr(audit, "flush_audit_log", lambda: None)
    monkeypatch.setattr(store, "flush_all_telemetry", lambda: None)
    monkeypatch.setattr(namespaces, "_evict_namespace_caches", lambda *args: None)
    monkeypatch.setenv("MESH_HOME", str(tmp_path / "original-mesh"))
    monkeypatch.setenv("HOME", str(tmp_path / "original-home"))
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    original = dict(os.environ)
    benchmark_root = None
    with pytest.raises(RuntimeError):
        with isolated_benchmark_home() as (root, models):
            benchmark_root = root
            assert os.environ["MESH_HOME"] == str(root / "mesh")
            assert os.environ["HOME"] == str(root / "home")
            assert os.environ["CODEX_HOME"] == str(root / "home" / ".codex")
            assert os.environ["MESH_AUTO_APPROVE"] == "0" and "OPENAI_API_KEY" not in os.environ
            assert models is False
            raise RuntimeError("synthetic failure")
    assert dict(os.environ) == original and benchmark_root is not None and not benchmark_root.exists()
