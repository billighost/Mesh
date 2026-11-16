"""Deterministic fake-only contracts for optional storage instrumentation."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from benchmarks import production_bench as bench
from benchmarks import production_ledger as ledger
from benchmarks import production_runner as runner
from benchmarks import storage_profile as profile


class Clock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        self.value += 1000
        return self.value


def configured() -> dict:
    return {
        "source_manifest": {"mesh/fake.py": "a" * 64},
        "benchmark_source_manifest": {"benchmarks/fake.py": "b" * 64},
        "storage_profile_fixture_sha256": "c" * 64,
        "backend": "neural", "surface": "http", "group_commit": 1,
        "storage_profile_mode": "scalar", "storage_profile_agents": 16,
        "storage_profile_input_facts": 10000,
    }


def instrumented(**options) -> profile.StorageProfiler:
    return profile.StorageProfiler(True, source_binding=profile.binding(configured()),
                                   wall_clock=Clock(), cpu_clock=Clock(), ready_clock=lambda: 2.0, **options)


def fake_modules():
    calls = []
    sentinel = object()

    class Connection:
        in_transaction = True

        def commit(self):
            calls.append("commit")
            return sentinel

    class Pooled(Connection):
        pass

    class Audit(Connection):
        pass

    class Checkout:
        def __enter__(self):
            calls.append("checkout")
            return sentinel

    class Reader(Checkout):
        pass

    class MeshStore:
        def _connect(self):
            calls.append("connect")
            return sentinel

        def publish_embedding(self, *args, **kwargs):
            calls.append(("publish", args, kwargs))
            return sentinel

    class Mesh:
        def _prepare_learn(self, *args, **kwargs):
            calls.append(("prepare", args, kwargs))
            return sentinel

        def _learning_candidates(self, *args, **kwargs):
            calls.append(("candidates", args, kwargs))
            return sentinel

    class Encoder:
        def _encode_raw_batches(self, *args, **kwargs):
            calls.append(("model", args, kwargs))
            return sentinel

    class Group:
        limits = SimpleNamespace(coalesce_seconds=.002)

        def _execute_batch(self, tickets):
            calls.append(("batch", tickets))
            return sentinel

    class Namespace:
        def __init__(self, suppress=False):
            self.suppress = suppress

        def __enter__(self):
            calls.append("enter")
            return sentinel

        def __exit__(self, kind, value, traceback):
            calls.append(("exit", kind, value, traceback))
            return self.suppress

    def namespace(suppress=False):
        return Namespace(suppress)

    def passthrough(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    store = SimpleNamespace(_PooledConnection=Pooled, _ConnectionCheckout=Checkout, _ReaderCheckout=Reader,
                            MeshStore=MeshStore, _begin_write=passthrough,
                            _acquire_os_file_lock=passthrough, namespace_write_lock=namespace)
    memory = SimpleNamespace(Mesh=Mesh, _DirectOnnxEmbedding=Encoder, namespace_write_lock=namespace)
    modules = {"store": store, "memory": memory, "sqlite": SimpleNamespace(Connection=Connection),
               "audit": SimpleNamespace(_ManagedAuditConnection=Audit, deliver_mutation_events=passthrough),
               "group": SimpleNamespace(GroupCommitCoordinator=Group)}
    return modules, calls, sentinel


def row(profiler, stage, phase="startup"):
    return profiler.snapshot()["phases"][phase]["stages"][stage]


def visit_all_phases(profiler):
    for phase in profile.PHASES:
        profiler.phase(phase)
    return profiler


def diagnostic_plan():
    cell = {**runner.cell("diagnostic", "neural", "scalar", 16, "http", 1, 10000), "storage_profile": True}
    plan = {"runtime_sources": configured()["source_manifest"],
            "harness_sources": configured()["benchmark_source_manifest"], "fixture_hashes": {"10000": "c" * 64}}
    return cell, plan


def test_disabled_mode_never_patches_registers_or_reads_clocks():
    def forbidden():
        raise AssertionError("disabled profiler read a clock")

    profiler = profile.StorageProfiler(False, wall_clock=forbidden, cpu_clock=forbidden)
    modules, calls, sentinel = fake_modules()
    original = modules["sqlite"].Connection.commit
    profiler.install(modules)
    assert profiler.call("prepare", lambda value: value, sentinel) is sentinel
    assert modules["sqlite"].Connection.commit is original
    assert profiler.buffers == [] and calls == []
    assert profiler.close() == profile.unobserved(False)


def test_hooks_preserve_original_calls_identity_roles_and_restore():
    modules, calls, sentinel = fake_modules()
    original = modules["sqlite"].Connection.commit
    profiler = instrumented().install(modules)
    store, audit = modules["store"], modules["audit"]
    assert store._PooledConnection().commit() is sentinel
    assert audit._ManagedAuditConnection().commit() is sentinel
    inactive = store._PooledConnection()
    inactive.in_transaction = False
    assert inactive.commit() is sentinel
    assert store._ConnectionCheckout().__enter__() is sentinel
    assert store._ReaderCheckout().__enter__() is sentinel
    assert store.MeshStore()._connect() is sentinel
    assert row(profiler, "store_commit")["count"] == 1
    assert row(profiler, "audit_commit")["count"] == 1
    assert row(profiler, "writer_checkout")["count"] == 1
    assert calls.count("commit") == 3 and calls.count("checkout") == 2
    result = profiler.close()
    assert modules["sqlite"].Connection.commit is original
    assert result["inflight_spans"] == 0


def test_exception_cancellation_arguments_and_secret_inputs_are_not_exported():
    profiler = instrumented()
    sentinel = object()
    calls = []

    def operation(*args, **kwargs):
        calls.append((args, kwargs))
        raise KeyboardInterrupt("PRIVATE_EXCEPTION_BODY")

    with pytest.raises(KeyboardInterrupt):
        profiler.wrap("prepare", operation)(sentinel, body="PRIVATE_MEMORY_BODY")
    assert calls == [((sentinel,), {"body": "PRIVATE_MEMORY_BODY"})]
    result = profiler.close()
    observed = result["phases"]["startup"]["stages"]["prepare"]
    assert observed["count"] == observed["failed_count"] == 1
    assert result["inflight_spans"] == 0
    serialized = json.dumps(result)
    assert "PRIVATE_" not in serialized and "KeyboardInterrupt" not in serialized


def test_nested_namespace_counts_acquisition_once_per_call_and_only_outer_hold():
    modules, calls, sentinel = fake_modules()
    profiler = instrumented().install(modules)
    outer = modules["store"].namespace_write_lock
    nested = modules["memory"].namespace_write_lock
    with outer() as first:
        with nested(suppress=True) as second:
            assert first is second is sentinel
            raise ValueError("PRIVATE_BODY")
    assert calls.count("enter") == 2
    assert row(profiler, "namespace_acquire")["count"] == 2
    assert row(profiler, "namespace_hold")["count"] == 1
    assert len([call for call in calls if isinstance(call, tuple) and call[0] == "exit"]) == 2
    assert profiler.close()["inflight_spans"] == 0


def test_write_begin_distinguishes_savepoints_and_ready_wait_keeps_ticket_identity():
    modules, calls, sentinel = fake_modules()
    profiler = instrumented().install(modules)
    connection = modules["store"]._PooledConnection()
    assert modules["store"]._begin_write(connection, "FAKE_NAMESPACE") is sentinel
    connection.in_transaction = False
    assert modules["store"]._begin_write(connection, "FAKE_NAMESPACE") is sentinel
    tickets = [SimpleNamespace(_ready_at=1.0), SimpleNamespace(_ready_at=1.5)]
    assert modules["group"].GroupCommitCoordinator()._execute_batch(tickets) is sentinel
    assert calls[-1] == ("batch", tickets)
    assert row(profiler, "savepoint_begin")["count"] == row(profiler, "write_begin")["count"] == 1
    assert row(profiler, "group_ready_wait")["wall_sum_ns"] == 1500000000
    assert profiler.close()["configured_coalesce_ns"] == 2000000


def test_thread_churn_and_inflight_accounting_stay_bounded():
    profiler = instrumented(max_threads=2)
    started = profiler.started()
    assert profiler.snapshot()["inflight_spans"] == 1
    profiler.finished("prepare", started, False)
    for _ in range(8):
        profiler.local = threading.local()
        assert profiler.call("prepare", lambda: 7) == 7
    result = profiler.close()
    assert len(profiler.buffers) == 2 and result["registered_threads"] == 2
    assert result["dropped_spans"] == 7 and result["inflight_spans"] == 0
    assert len(json.dumps(result)) < profile.MAX_SIDECAR_BYTES


def test_partial_recovery_checks_bindings_and_filters_unknown_fields(tmp_path):
    profiler = instrumented()
    profiler.call("prepare", lambda: None)
    value = profiler.close()
    value["private_body"] = "NEVER_EXPORT"
    value["phases"]["startup"]["stages"]["prepare"]["private_sql"] = "NEVER_EXPORT"
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    expected = profile.binding(configured())
    recovered = profile.recover(path, expected, interrupted=True)
    assert recovered["state"] == recovered["phases"]["startup"]["state"] == "partial"
    assert "NEVER_EXPORT" not in json.dumps(recovered)
    forged = {**expected, "runtime_sha256": "d" * 64}
    with pytest.raises(profile.ProfileError, match="SourceOrCellMismatch"):
        profile.recover(path, forged)
    absent = profile.recover(tmp_path / "not-created.json", expected)
    assert absent["state"] == "unobserved" and absent["phases"] == {}


def test_invalid_catalog_histogram_and_unobserved_rows_fail_closed():
    profiler = instrumented()
    profiler.call("prepare", lambda: None)
    value = profiler.close()
    expected = profile.binding(configured())
    forged = copy.deepcopy(value)
    forged["phases"]["startup"]["stages"]["PRIVATE_STAGE"] = {}
    with pytest.raises(profile.ProfileError, match="StageCatalog"):
        profile.sanitize(forged, expected)
    forged = copy.deepcopy(value)
    forged["phases"]["startup"]["stages"]["prepare"]["wall_bins"][0] = True
    with pytest.raises(profile.ProfileError, match="Histogram"):
        profile.sanitize(forged, expected)
    forged = copy.deepcopy(value)
    forged["phases"]["verification"]["stages"] = {"prepare": {"count": 0}}
    with pytest.raises(profile.ProfileError, match="UnobservedProfile"):
        profile.sanitize(forged, expected)


def test_http_clients_do_not_install_runtime_instrumentation(monkeypatch):
    monkeypatch.setattr(profile.StorageProfiler, "install", lambda *args: pytest.fail("thin HTTP client installed runtime hooks"))
    assert bench._storage_profiler({"storage_profile": True, "surface": "http", "action": "ingest"}) is None
    assert bench._storage_profiler({"storage_profile": False, "surface": "receipt-sdk", "action": "ingest"}) is None


def test_default_off_controller_and_ledger_keep_diagnostics_separate():
    cell = runner.cell("scale", "neural", "scalar", 16, "http", 1, 10000)
    plan = {"python": "FAKE_PYTHON", "model": {"directory": "FAKE_MODEL"},
            "runtime_sources": configured()["source_manifest"], "harness_sources": configured()["benchmark_source_manifest"],
            "fixture_hashes": {"10000": "c" * 64}}
    assert "--storage-profile" not in runner.command(plan, cell, Path("FAKE_OUTPUT"))
    assert ledger.profile_result(None, cell, plan) == profile.unobserved(False)
    diagnostic = {**cell, "storage_profile": True}
    assert "--storage-profile" in runner.command(plan, diagnostic, Path("FAKE_OUTPUT"))
    profiler = visit_all_phases(instrumented())
    observed = {"version": profile.VERSION, "state": "complete", "observations": [profiler.close()]}
    projected = ledger.profile_result(observed, diagnostic, plan)
    assert projected["state"] == "complete"
    assert ledger.profile_result(profile.unobserved(True), diagnostic, plan)["state"] == "unobserved"
    with pytest.raises(ledger.LedgerError, match="Unrequested"):
        ledger.profile_result(observed, cell, plan)


def test_failed_and_missing_sidecars_retain_rejections_without_replaying(tmp_path):
    profiler = instrumented()
    raw = profiler.close()
    raw["binding"]["runtime_sha256"] = "d" * 64
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(raw), encoding="utf-8")
    first = {**configured(), "action": "serve", "storage_profile": True, "storage_profile_output": str(forged)}
    absent = {**first, "storage_profile_output": str(tmp_path / "absent.json")}
    before = forged.read_bytes()
    projected = bench.profile_bundle([first, absent], interrupted=True)
    assert projected["state"] == "unobserved" and projected["recovery_rejected_observations"] == 1
    assert all(item["state"] == "unobserved" and item["phases"] == {} for item in projected["observations"])
    assert forged.read_bytes() == before
    assert not Path(absent["storage_profile_output"]).exists()


def test_restoration_does_not_overwrite_another_owner_and_reports_loss():
    modules, _, _ = fake_modules()
    profiler = instrumented().install(modules)
    replacement = lambda instance: None
    modules["store"].MeshStore._connect = replacement
    result = profiler.close()
    assert modules["store"].MeshStore._connect is replacement
    assert result["restore_failures"] == 1
    assert result["inflight_spans"] == 0
    assert result["state"] == "partial"


def test_close_during_original_call_preserves_return_and_latches_partial(tmp_path):
    profiler = visit_all_phases(instrumented(output=tmp_path / "profile.json"))
    sentinel, reports, calls = object(), [], []

    def original():
        calls.append("original")
        reports.append(profiler.close())
        return sentinel

    assert profiler.call("prepare", original) is sentinel and calls == ["original"]
    assert reports[0]["state"] == "partial" and reports[0]["inflight_spans"] == 1
    assert profiler.snapshot(True)["state"] == profiler.close()["state"] == "partial"
    recovered = profile.recover(profiler.output, profile.binding(configured()))
    assert recovered["state"] == "partial" and recovered["inflight_spans"] == 1


@pytest.mark.parametrize("loss", ("dropped_spans", "checkpoint_failures", "restore_failures"))
def test_close_reports_any_loss_as_partial(loss, tmp_path):
    profiler = visit_all_phases(instrumented(output=tmp_path / "profile.json"))
    setattr(profiler, loss, 1)
    result = profiler.close()
    assert result["state"] == "partial" and result[loss] == 1
    assert all(phase["state"] == "partial" for phase in result["phases"].values())
    assert profile.recover(profiler.output, profile.binding(configured()))["state"] == "partial"


def test_restore_setter_failure_cannot_mask_original_cancellation():
    class Owner:
        deny = False
        method = object()

        def __setattr__(self, name, value):
            if self.deny and name == "method":
                raise RuntimeError("PRIVATE_RESTORE_FAILURE")
            object.__setattr__(self, name, value)

    profiler = visit_all_phases(instrumented())
    owner = Owner()
    replacement = object()
    profiler.patch(owner, "method", replacement)
    owner.deny = True
    marker = KeyboardInterrupt("PRIVATE_CANCEL")
    reports = []

    def original():
        raise marker

    with pytest.raises(KeyboardInterrupt) as captured:
        try:
            profiler.call("prepare", original)
        finally:
            reports.append(profiler.close(False))
    assert captured.value is marker and owner.method is replacement
    assert reports[0]["state"] == "partial" and reports[0]["restore_failures"] == 1
    assert "PRIVATE_" not in json.dumps(reports)


@pytest.mark.parametrize("join_raises", (False, True))
def test_unsettled_checkpoint_owner_never_competes_with_final_publication(tmp_path, monkeypatch, join_raises):
    profiler = visit_all_phases(instrumented(output=tmp_path / "profile.json"))
    profiler.publish(False)
    before = profiler.output.read_bytes()
    late_publish = profiler.publish

    class PendingCheckpoint:
        def join(self, timeout):
            assert timeout == 5
            if join_raises:
                raise RuntimeError("PRIVATE_JOIN_FAILURE")

        def is_alive(self):
            return True

    profiler.checkpoint_thread = PendingCheckpoint()
    monkeypatch.setattr(profiler, "publish", lambda *_: pytest.fail("final publication raced checkpoint owner"))
    result = profiler.close()
    assert result["state"] == "partial" and result["checkpoint_failures"] == 1
    assert profiler.output.read_bytes() == before
    late_publish(False)
    recovered = profile.recover(profiler.output, profile.binding(configured()))
    assert recovered["state"] == "partial" and recovered["checkpoint_failures"] == 1


def test_publication_lock_never_blocks_original_recording(tmp_path):
    profiler = visit_all_phases(instrumented(output=tmp_path / "profile.json"))
    profiler.publish(False)
    before = profiler.output.read_bytes()
    assert profiler.publish_lock.acquire(blocking=False)
    try:
        assert profiler.call("prepare", lambda: 7) == 7
        profiler.publish(False)
        assert profiler.checkpoint_failures == 1 and profiler.output.read_bytes() == before
    finally:
        profiler.publish_lock.release()
    assert profiler.close()["state"] == "partial"


def test_recursive_malformed_sidecar_remains_diagnostic_only(tmp_path, monkeypatch):
    path = tmp_path / "recursive.json"
    raw = b"[" * 2000 + b"]" * 2000
    path.write_bytes(raw)
    with pytest.raises(profile.ProfileError):
        profile.recover(path, profile.binding(configured()))
    config = {**configured(), "action": "serve", "storage_profile": True, "storage_profile_output": str(path)}
    def recursive_decoder(_):
        raise RecursionError("PRIVATE_DECODER_FAILURE")

    # Python versions differ in their JSON nesting limit; inject its failure so
    # the diagnostic-only exception boundary is deterministic on every version.
    with monkeypatch.context() as patch:
        patch.setattr(profile.json, "loads", recursive_decoder)
        with pytest.raises(profile.ProfileError, match="InvalidJson"):
            profile.recover(path, profile.binding(configured()))
        result = bench.profile_bundle([config], interrupted=True)
    assert result["state"] == "unobserved" and result["recovery_rejected_observations"] == 1
    assert path.read_bytes() == raw
    cell, plan = diagnostic_plan()
    assert ledger.profile_result(result, cell, plan)["recovery_rejected_observations"] == 1


@pytest.mark.parametrize("loss", profile.LOSS_COUNTERS)
def test_complete_sidecar_and_controller_reject_loss(loss):
    value = visit_all_phases(instrumented()).close()
    value[loss] = 1
    with pytest.raises(profile.ProfileError, match="CompletionInvalid"):
        profile.sanitize(value, profile.binding(configured()))
    cell, _ = diagnostic_plan()
    projection = {"validation_passed": True, "storage_profile": {
        "version": profile.VERSION, "state": "complete", "observations": [value], "recovery_rejected_observations": 0,
    }}
    assert not runner.storage_diagnostic_complete(cell, projection)


@pytest.mark.parametrize("missing_phase_state", ("partial", "unobserved"))
def test_complete_sidecar_requires_every_phase(missing_phase_state):
    value = visit_all_phases(instrumented()).close()
    value["phases"]["verification"]["state"] = missing_phase_state
    if missing_phase_state == "unobserved":
        value["phases"]["verification"]["stages"] = {}
    with pytest.raises(profile.ProfileError, match="CompletionInvalid"):
        profile.sanitize(value, profile.binding(configured()))


@pytest.mark.parametrize("cardinality", (0, 2))
def test_complete_http_bundle_requires_one_server(cardinality):
    cell, plan = diagnostic_plan()
    value = visit_all_phases(instrumented()).close()
    bundle = {"version": profile.VERSION, "state": "complete", "observations": [value] * cardinality}
    with pytest.raises(ledger.LedgerError, match="CardinalityInvalid|CompletionInvalid"):
        ledger.profile_result(bundle, cell, plan)
    assert not runner.storage_diagnostic_complete(cell, {"validation_passed": True, "storage_profile": bundle})


def test_controller_and_ledger_require_lossless_complete_http_evidence():
    cell, plan = diagnostic_plan()
    value = visit_all_phases(instrumented()).close()
    bundle = {"version": profile.VERSION, "state": "complete", "observations": [value], "recovery_rejected_observations": 0}
    projected = ledger.profile_result(bundle, cell, plan)
    assert runner.storage_diagnostic_complete(cell, {"validation_passed": True, "storage_profile": projected})
    rejected = {**bundle, "recovery_rejected_observations": 1}
    with pytest.raises(ledger.LedgerError, match="CompletionInvalid"):
        ledger.profile_result(rejected, cell, plan)
    assert not runner.storage_diagnostic_complete(cell, {"validation_passed": True, "storage_profile": rejected})
    assert not runner.storage_diagnostic_complete(cell, {"validation_passed": False, "storage_profile": projected})
    interrupted = profile.sanitize(value, profile.binding(configured()), interrupted=True)
    assert interrupted["state"] == "partial" and all(row["state"] == "partial" for row in interrupted["phases"].values())


def test_sdk_and_incomplete_phase_shutdown_remain_partial():
    sdk_profiler = visit_all_phases(instrumented())
    sdk_report = {"confirmed_acknowledged_facts": 7}
    bench._profile_finish(sdk_profiler, sdk_report)
    assert sdk_report["confirmed_acknowledged_facts"] == 7 and sdk_report["storage_profile"]["state"] == "partial"
    server_report = {}
    bench._profile_finish(visit_all_phases(instrumented()), server_report, shutdown_observed=True)
    assert server_report["storage_profile"]["state"] == "complete"
    incomplete_report = {}
    bench._profile_finish(instrumented(), incomplete_report, shutdown_observed=True)
    assert incomplete_report["storage_profile"]["state"] == "partial"
    assert incomplete_report["storage_profile"]["phases"]["verification"]["state"] == "unobserved"
