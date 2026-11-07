"""One fixed-order hosted-runner full-suite observation, with no coverage instrumentation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import production_runner as runner

SEED = 20261106


def execution_sources() -> dict[str, str]:
    return {
        "launcher_sha256": runner.sha_file(runner.REPO / "scripts/run_tests.py"),
        "controller_sha256": runner.sha_file(Path(__file__)),
    }


def bounded_run(command: list[str], environment: dict[str, str]) -> tuple[int, bool]:
    import psutil

    process = subprocess.Popen(command, cwd=runner.REPO, env=environment,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    observed = {process.pid: psutil.Process(process.pid).create_time()}
    deadline = time.monotonic() + 3000
    timed_out = False
    try:
        while process.poll() is None:
            try:
                for child in psutil.Process(process.pid).children(recursive=True):
                    observed[child.pid] = child.create_time()
            except psutil.Error:
                pass
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.25)
    finally:
        stopped = runner.stop_owned(psutil, observed)
        process.wait(timeout=10)
    return (124 if timed_out else process.returncode), stopped


def suite_identity(path: Path) -> dict:
    cases = list(ET.parse(path).iter("testcase"))
    identities = sorted((case.get("classname", ""), case.get("name", "")) for case in cases)
    skipped = sorted((case.get("classname", ""), case.get("name", ""),
                      case.find("skipped").get("type", "skipped"))
                     for case in cases if case.find("skipped") is not None)
    failed = [case for case in cases if case.find("failure") is not None or case.find("error") is not None]
    failure_ids = []
    for case in failed:
        # Parameter IDs and XML failure bodies may contain synthetic secret values.
        module = case.get("classname", "")
        name = case.get("name", "").split("[", 1)[0]
        identifier = module + "::" + name
        if (re.fullmatch(r"tests(?:\.[A-Za-z_][A-Za-z_0-9]*)+", module)
                and re.fullmatch(r"test_[A-Za-z_0-9]+", name) and len(identifier) <= 256):
            failure_ids.append(identifier)
    return {
        "tests": len(cases), "selection_sha256": hashlib.sha256(json.dumps(identities).encode()).hexdigest(),
        "skipped_selection_sha256": hashlib.sha256(json.dumps(skipped).encode()).hexdigest(),
        "failures": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
        "failed_test_identifiers": sorted(set(failure_ids)),
        "unidentified_failed_cases": len(failed) - len(failure_ids),
    }


def matching_suite_selections(observations: list[dict]) -> bool:
    if len(observations) != 2:
        return False
    first, second = (item.get("selection", {}) for item in observations)
    return (first.get("tests", 0) >= 1000
            and first.get("tests") == second.get("tests")
            and bool(first.get("selection_sha256"))
            and first.get("selection_sha256") == second.get("selection_sha256")
            and bool(first.get("skipped_selection_sha256"))
            and first.get("skipped_selection_sha256") == second.get("skipped_selection_sha256"))


def comparison_metadata_eligible(observations: list[dict], *, source_unchanged: bool,
                                 runtime_unchanged: bool) -> bool:
    if not source_unchanged or not runtime_unchanged or not matching_suite_selections(observations):
        return False
    for item in observations:
        selection = item.get("selection", {})
        wall = runner.ledger.number(item.get("wall_seconds"))
        if (item.get("status") != "completed" or item.get("resource_sampling_available") is not True
                or selection.get("errors") != 0 or selection.get("failures") != 0
                or wall is None or wall <= 0):
            return False
    return True


def main() -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise runner.ControlError("CiOnlyFullSuiteSpeedCommand")
    if any(name in os.environ for name in ("COVERAGE_PROCESS_START", "COV_CORE_SOURCE")):
        raise runner.ControlError("InstrumentedTestSpeedRejected")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualified-source", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    options = parser.parse_args()
    output = runner.private_path(options.output_directory)
    output.mkdir(parents=True, exist_ok=True)
    qualified = runner.read_json(runner.private_path(options.qualified_source))
    before, harness, runtime = runner.manifest(), runner.harness_manifest(), runner.runtime()
    wrappers = execution_sources()
    if (qualified.get("runtime_sources") != before or qualified.get("harness_sources") != harness
            or qualified.get("runtime", {}).get("python_version") != runtime["python_version"]
            or qualified.get("runtime", {}).get("packages") != runtime["packages"]
            or qualified.get("test_execution_sources") != wrappers):
        raise runner.ControlError("QualifiedTestSpeedSourceOrRuntimeMismatch")
    current_xml = output / "current.xml"
    common = ["-q", "--tb=short", "-ra", "--hypothesis-profile=ci", "--hypothesis-show-statistics",
              f"--hypothesis-seed={SEED}", "--junitxml=" + current_xml.relative_to(runner.REPO).as_posix()]
    observations = []
    for workers in (1, 2):
        if (runner.manifest() != before or runner.harness_manifest() != harness
                or runner.runtime() != runtime or execution_sources() != wrappers):
            raise runner.ControlError("TestSpeedSourceOrRuntimeDrift")
        raw = output / f"raw-{workers}.json"
        environment = runner.base_environment(output / f"launcher-{workers}")
        environment["HYPOTHESIS_PROFILE"] = "ci"
        command = [sys.executable, str(runner.REPO / "scripts/run_tests.py"), "--workers", str(workers),
                   "--report", str(raw), "--", *common]
        if current_xml.exists():
            archived = output / f"suite-before-{workers}.xml"
            runner.private_path(archived)
            print(json.dumps({"listed_junit_before_archive": current_xml.name, "archive": archived.name}))
            os.replace(current_xml, archived)
        code, stopped = bounded_run(command, environment)
        source = runner.read_json(raw) if raw.exists() else {}
        selection = suite_identity(current_xml) if current_xml.exists() else {}
        observation = {
            "workers": workers, "exit_code": code,
            "status": "completed" if code == 0 and stopped else "failed",
            "all_observed_owned_processes_stopped": stopped,
            "raw_report_sha256": runner.sha_file(raw) if raw.exists() else None,
            "wall_seconds": runner.ledger.number(source.get("wall_seconds")),
            "sampled_peak_process_tree_rss_bytes": runner.ledger.number(source.get("sampled_peak_process_tree_rss_bytes")),
            "sampled_process_tree_cpu_seconds": runner.ledger.number(source.get("sampled_process_tree_cpu_seconds")),
            "resource_sample_count": runner.ledger.number(source.get("resource_sample_count")),
            "resource_sample_interval_seconds": runner.ledger.number(source.get("resource_sampling_interval_seconds")),
            "resource_sampling_available": source.get("resource_sampling_available") is True,
            "selection": selection,
        }
        runner.ledger.assert_safe_output(observation)
        runner.write_json(output / f"observation-{workers}.json", observation)
        observations.append(observation)
        runner.write_json(output / "checkpoint.json", {"observations": observations})
    same_selection = matching_suite_selections(observations)
    source_unchanged = (runner.manifest() == before and runner.harness_manifest() == harness
                        and execution_sources() == wrappers)
    runtime_unchanged = runner.runtime() == runtime
    ready = comparison_metadata_eligible(observations, source_unchanged=source_unchanged,
                                         runtime_unchanged=runtime_unchanged)
    denominator = observations[1]["wall_seconds"]
    ratio = observations[0]["wall_seconds"] / denominator if ready and denominator else None
    summary = {
        "schema_version": 1, "status": "completed" if ready else "failed",
        "runtime": runtime, "ordered_workers": [1, 2], "repetitions_per_setting": 1,
        "hypothesis_profile": "ci", "hypothesis_seed": SEED, "coverage_instrumented": False,
        "same_full_suite_selection_verified": same_selection,
        "source_unchanged_after": source_unchanged, "runtime_unchanged_after": runtime_unchanged,
        "fresh_home_and_hypothesis_directories_per_invocation": True,
        "observations": observations, "observed_wall_ratio_1_over_2": ratio,
        "limitation_codes": ["one_hosted_runner_fixed_order_observation", "no_generalized_speedup_claim",
                             "os_and_file_cache_order_effects", "summed_rss_can_double_count_shared_pages",
                             "sampling_can_miss_short_lived_cpu"],
    }
    runner.ledger.assert_safe_output(summary)
    summary["runtime_source_manifest"] = runner.ledger.source_manifest(before)
    summary["harness_source_manifest"] = runner.ledger.source_manifest(harness)
    summary["test_speed_source_sha256"] = wrappers["controller_sha256"]
    summary["test_launcher_source_sha256"] = wrappers["launcher_sha256"]
    runner.write_json(output / "comparison.json", summary)
    print(json.dumps({"status": summary["status"], "same_full_suite_selection_verified": same_selection,
                      "observations": 2, "repetitions_per_setting": 1}))
    return 0 if ready else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except runner.ControlError as exc:
        print(json.dumps({"status": "control_error", "code": str(exc)}), flush=True)
        raise SystemExit(2) from None
