"""Pure controller regressions: no benchmark process, model or provider is started."""
from __future__ import annotations

import importlib.util
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

_RUNNER = Path(__file__).resolve().parents[1] / "benchmarks/production_runner.py"
sys.path.insert(0, str(_RUNNER.parent))
try:
    _SPEC = importlib.util.spec_from_file_location("mesh_production_runner_regressions", _RUNNER)
    controller = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(controller)
    _SPEED_SPEC = importlib.util.spec_from_file_location("mesh_production_speed_regressions", _RUNNER.with_name("production_test_speed.py"))
    speed_metadata = importlib.util.module_from_spec(_SPEED_SPEC)
    _SPEED_SPEC.loader.exec_module(speed_metadata)
finally:
    sys.path.pop(0)


@pytest.fixture
def isolated_plan(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    run_root = repository / ".work/run"
    run_root.mkdir(parents=True)
    monkeypatch.setattr(controller, "REPO", repository)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    items = [controller.cell("primary", "hash", "scalar", count, "receipt-sdk", 0, 512) for count in (1, 4)]
    plan = {"run_root": str(run_root), "cells": items}
    controller.write_json(run_root / "plan.json", plan)
    controller.write_json(run_root / "seal.json", {"plan_sha256": controller.sha_file(run_root / "plan.json")})
    state = {"cells": {item["id"]: {"status": "planned", "reason": "none"} for item in items}}
    controller.write_json(run_root / "state.json", state)
    monkeypatch.setattr(controller, "load_plan", lambda _: plan)
    monkeypatch.setattr(controller, "unchanged", lambda _: True)
    monkeypatch.setattr(controller, "execute", lambda *_: pytest.fail("A pure controller test dispatched a worker"))
    options = SimpleNamespace(plan=run_root / "plan.json", phase="primary", quiet_window_open=True, cell_id=None)
    return plan, state, options


def test_dispatching_state_never_resubmits(isolated_plan):
    plan, state, options = isolated_plan
    state["cells"][plan["cells"][0]["id"]]["status"] = "dispatching"
    controller.write_json(Path(plan["run_root"]) / "state.json", state)
    with pytest.raises(controller.ControlError, match="UnresolvedDispatchRequiresManualAccountingReview"):
        controller.step(options)


def test_source_mismatch_refuses_before_dispatch(isolated_plan, monkeypatch):
    _, _, options = isolated_plan
    monkeypatch.setattr(controller, "unchanged", lambda _: False)
    with pytest.raises(controller.ControlError, match="SourceOrRuntimeChanged"):
        controller.step(options)


def test_final_source_drift_cannot_pass_after_pending_cells_are_deferred(isolated_plan, monkeypatch):
    plan, state, options = isolated_plan
    state["cells"][plan["cells"][0]["id"]].update(status="completed", reason="none")
    controller.write_json(Path(plan["run_root"]) / "state.json", state)
    monkeypatch.setattr(controller, "unchanged", lambda _: False)
    monkeypatch.setattr(controller, "snapshot", lambda _: {"status": "complete"})

    controller.finalize(options)
    final_state = controller.read_json(Path(plan["run_root"]) / "state.json")
    assert final_state["cells"][plan["cells"][1]["id"]] == {
        "status": "deferred", "reason": "operator_stop",
    }
    with pytest.raises(controller.ControlError, match="SourceOrRuntimeChanged"):
        controller.check(options)


@pytest.mark.parametrize("failure,expected", [("integrity", "integrity_failure"),
                                               ("receipt", "receipt_failure"),
                                               ("cleanup", "cleanup_failure")])
def test_hard_failures_stop_all_pending_cells(isolated_plan, failure, expected):
    plan, state, _ = isolated_plan
    execution = {"all_owned_processes_stopped": True, "source_unchanged_after": True}
    result = {
        "checks": {"policy_observed": True, "policy_matches_registered": True,
                   "verification_observed": True, "integrity_ok": True,
                   "accounting_observed": True, "accounting_consistent": True,
                   "attempt_accounting_complete": True, "model_manifest_observed": True,
                   "model_bytes_match_registered": True},
        "writes": {"protocol_error_count": 0, "unacknowledged_receipt_count": 0,
                   "unknown_outcome_rows": 0, "unacknowledged_committed_row_count": 0},
        "verification": {"receipt_status": "confirmed"},
    }
    if failure == "integrity":
        result["checks"]["integrity_ok"] = False
    elif failure == "receipt":
        result["verification"]["receipt_status"] = "failed"
    else:
        execution["all_owned_processes_stopped"] = False
    reason = controller.hard_failure(result, execution)
    assert reason == expected
    controller.record_dependent_stops(plan, state, plan["cells"][0], reason)
    assert all(entry == {"status": "deferred", "reason": expected} for entry in state["cells"].values())
    assert controller.read_json(Path(plan["run_root"]) / "STOP")["reason"] == expected


def test_neural_sdk_projection_refuses_unsafe_agent_count():
    mib = 1024 * 1024
    plan = {"limits": {"minimum_available_ram_bytes": 256 * mib, "max_tree_rss_bytes": 1024 * mib,
                       "sdk_projection_multiplier": 1.25, "sdk_cache_margin_bytes": 256 * mib}}
    reference = {
        "cell": {"backend": "neural", "surface": "receipt-sdk", "agents": 4},
        "headline_eligible": True, "checks": {"resource_budget_exceeded": False},
        "execution": {"sustained_paging_observed": False},
        "resources": {"roles": {"write_clients": {"peak_rss_bytes": 400 * mib},
                                "coordinator": {"peak_rss_bytes": 128 * mib}}},
    }
    item = controller.cell("primary", "neural", "scalar", 16, "receipt-sdk", 0, 512)
    assert controller.resource_gate(plan, item, [reference], 8192 * mib) == "resource"


def test_public_projection_excludes_bodies_paths_and_unknown_fields():
    ledger = controller.ledger
    item = controller.cell("pilot", "hash", "scalar", 1, "receipt-sdk", 0, 128)
    runtime_sources = {"mesh/version.py": "b" * 64, "pyproject.toml": "c" * 64}
    harness_sources = {"benchmarks/production_bench.py": "d" * 64, "benchmarks/production_transport.py": "e" * 64}
    plan = {"runtime": {"python_version": "3.14.0"}, "fixture_hashes": {"128": "a" * 64},
            "runtime_sources": runtime_sources, "harness_sources": harness_sources,
            "model": {"hashes": {}}, "anchor_labels": {"cedar": "anchor_01"}}
    private = {"content": "PRIVATE_UNSAFE_BODY", "query": "PRIVATE_UNSAFE_QUERY",
               "config": "C:/Users/PRIVATE_USER/config.json", "token": "YOUR_TOKEN_HERE",
               "log": "PRIVATE_UNSAFE_LOG"}
    report = {
        "report_version": 2, "fixture_version": 1, "total_input_rows": 128, "anchor_rows": 12,
        "fixture_sha256": "a" * 64, "python_version": "3.14.0", "coverage_instrumented": False,
        "runtime_source_manifest": runtime_sources, "benchmark_source_manifest": harness_sources,
        "unknown_private_object": private,
        "cases": [{"agents": 1, "backend": "hash", "mode": "scalar", "surface": "receipt-sdk",
                   "group_commit_requested": 0, "status": "failed", "model_manifest": None,
                   "profile_policy": ledger.POLICY, "writes": {"submitted_rows": 128},
                   "error_stage": "C:/Users/PRIVATE_USER/error.json", "unknown_private_object": private,
                   "worker_metadata": [{"sqlite_version": "3.54.1", "schema_version": "5",
                                        "synchronous": "FULL", "journal_mode": "wal", "query_model_tag": "hash-384"}]}],
    }
    execution = {"input_report_sha256": "f" * 64, "exit_code": 1, "source_unchanged_after": True,
                 "all_owned_processes_stopped": True, "resource_budget_exceeded": False}
    projected = ledger.result_projection(report, item, plan, execution)
    serialized = json.dumps(projected)
    assert all(value not in serialized for value in private.values())
    assert "unknown_private_object" not in serialized
    assert "C:/Users" not in serialized
    assert projected["native_runtime"]["sqlite_versions"] == ["3.54.1"]
    assert projected["checks"]["wal_full_verified"] is True
    assert projected["headline_eligible"] is False


def test_no_completed_baseline_cannot_be_reported_as_success(isolated_plan, monkeypatch):
    plan, state, options = isolated_plan
    for entry in state["cells"].values():
        entry.update(status="deferred", reason="resource")
    controller.write_json(Path(plan["run_root"]) / "state.json", state)
    with pytest.raises(controller.ControlError, match="NoCompletedPrimaryBaseline"):
        controller.check(options)
    state["cells"][plan["cells"][0]["id"]].update(status="completed", reason="none")
    controller.write_json(Path(plan["run_root"]) / "state.json", state)
    monkeypatch.setattr(controller, "snapshot", lambda _: {"status": "complete"})
    result = controller.check(options)
    assert result["completed_primary_cells"] == 1
    assert result["deferred_primary_cells"] == 1
    assert result["status"] == "measured_primary_success_with_visible_deferrals"


@pytest.mark.parametrize("scale_status,reason", [("failed", "receipt_failure"), ("failed", "cleanup_failure"), ("dispatching", "none")])
def test_executed_scale_failure_cannot_leave_the_matrix_green(isolated_plan, monkeypatch, scale_status, reason):
    plan, state, options = isolated_plan
    for entry in state["cells"].values():
        entry.update(status="completed", reason="none")
    scale = controller.cell("scale", "hash", "scalar", 16, "http", 0, 10000)
    plan["cells"].append(scale)
    state["cells"][scale["id"]] = {"status": scale_status, "reason": reason}
    controller.write_json(Path(plan["run_root"]) / "state.json", state)
    monkeypatch.setattr(controller, "snapshot", lambda _: {"status": "complete"})
    with pytest.raises(controller.ControlError, match="ExecutedScaleFailureRequiresReview"):
        controller.check(options)
    state["cells"][scale["id"]] = {"status": "deferred", "reason": "resource"}
    controller.write_json(Path(plan["run_root"]) / "state.json", state)
    assert controller.check(options)["completed_primary_cells"] == len(plan["cells"]) - 1


def test_speed_identity_distinguishes_changed_skipped_testcases(tmp_path):
    first = tmp_path / "first.xml"
    second = tmp_path / "second.xml"
    first.write_text('<testsuite><testcase classname="example" name="a"><skipped type="pytest.skip"/></testcase>'
                     '<testcase classname="example" name="b"/></testsuite>', encoding="utf-8")
    second.write_text('<testsuite><testcase classname="example" name="a"/>'
                      '<testcase classname="example" name="b"><skipped type="pytest.skip"/></testcase></testsuite>', encoding="utf-8")
    one, two = speed_metadata.suite_identity(first), speed_metadata.suite_identity(second)
    assert one["selection_sha256"] == two["selection_sha256"]
    assert one["skipped"] == two["skipped"] == 1
    assert one["skipped_selection_sha256"] != two["skipped_selection_sha256"]


def test_speed_failure_identifiers_exclude_parameters_and_xml_bodies(tmp_path):
    report = tmp_path / "failed.xml"
    report.write_text(
        '<testsuite><testcase classname="tests.test_audit" name="test_redaction[FAKE_SECRET_VALUE]">'
        '<failure>FAKE_SECRET_VALUE C:/Users/EXAMPLE</failure></testcase>'
        '<testcase classname="C:/Users/EXAMPLE" name="test_error"><error>FAKE_SECRET_VALUE</error></testcase>'
        '<testcase classname="tests.test_audit" name="FAKE_SECRET_VALUE"><failure/></testcase></testsuite>',
        encoding="utf-8",
    )
    selection = speed_metadata.suite_identity(report)
    assert selection["failed_test_identifiers"] == ["tests.test_audit::test_redaction"]
    assert selection["unidentified_failed_cases"] == 2
    assert (selection["failures"], selection["errors"]) == (2, 1)
    speed_metadata.runner.ledger.assert_safe_output(selection)
    assert "FAKE_SECRET_VALUE" not in json.dumps(selection)
    assert "C:/Users" not in json.dumps(selection)


def _matching_speed_observations():
    observation = {"status": "completed", "resource_sampling_available": True, "wall_seconds": 10.0,
                   "selection": {"tests": 1100, "selection_sha256": "a" * 64,
                                 "skipped_selection_sha256": "b" * 64, "errors": 0, "failures": 0}}
    return [copy.deepcopy(observation), copy.deepcopy(observation)]


def test_speed_ratio_requires_complete_matching_metadata():
    assert speed_metadata.comparison_metadata_eligible(_matching_speed_observations(), source_unchanged=True,
                                                        runtime_unchanged=True)


@pytest.mark.parametrize("mismatch", ["skips", "errors", "failures", "runtime"])
def test_speed_ratio_refuses_skip_failure_or_runtime_mismatch(mismatch):
    observations = _matching_speed_observations()
    if mismatch == "skips":
        observations[1]["selection"]["skipped_selection_sha256"] = "c" * 64
    elif mismatch in {"errors", "failures"}:
        observations[1]["selection"][mismatch] = 1
    assert not speed_metadata.comparison_metadata_eligible(observations, source_unchanged=True,
                                                            runtime_unchanged=mismatch != "runtime")


@pytest.mark.parametrize("changed_wrapper", ["launcher", "controller"])
def test_speed_ratio_rejects_changed_execution_code(tmp_path, monkeypatch, changed_wrapper):
    launcher = tmp_path / "scripts/run_tests.py"
    launcher.parent.mkdir()
    launcher.write_text("# original isolated launcher\n", encoding="utf-8")
    speed_controller = tmp_path / "speed.py"
    speed_controller.write_text("# original speed controller\n", encoding="utf-8")
    monkeypatch.setattr(speed_metadata.runner, "REPO", tmp_path)
    monkeypatch.setattr(speed_metadata, "__file__", str(speed_controller))
    qualified = speed_metadata.execution_sources()
    selected = launcher if changed_wrapper == "launcher" else speed_controller
    selected.write_text("# changed execution settings\n", encoding="utf-8")
    assert not speed_metadata.comparison_metadata_eligible(
        _matching_speed_observations(),
        source_unchanged=speed_metadata.execution_sources() == qualified,
        runtime_unchanged=True,
    )


@pytest.mark.parametrize("drift,expected", [("source", "SourceOrRuntimeChanged"), ("seal", "FinalSourceQualificationSealRequired")])
def test_final_matrix_check_rejects_drift_after_earlier_success(isolated_plan, monkeypatch, drift, expected):
    plan, state, options = isolated_plan
    state["cells"][plan["cells"][0]["id"]] = {"status": "completed", "reason": "none"}
    state["cells"][plan["cells"][1]["id"]] = {"status": "deferred", "reason": "operator_stop"}
    controller.write_json(Path(plan["run_root"]) / "state.json", state)
    if drift == "source":
        monkeypatch.setattr(controller, "unchanged", lambda _: False)
    else:
        controller.write_json(Path(plan["run_root"]) / "seal.json", {"plan_sha256": "0" * 64})
    with pytest.raises(controller.ControlError, match=expected):
        controller.check(options)
