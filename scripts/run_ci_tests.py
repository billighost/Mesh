"""
scripts/run_ci_tests.py — CI Pytest runner with live streaming, GitHub annotations, and step summary.
"""
from __future__ import annotations

import os
import subprocess
import sys
import json
import tempfile
import time
from collections import deque
from pathlib import Path

if __package__:
    from .run_tests import REPO_ROOT, _ResourceSampler, _stop_process_tree, isolated_environment, pytest_command
else:
    from run_tests import REPO_ROOT, _ResourceSampler, _stop_process_tree, isolated_environment, pytest_command


def main() -> None:
    evidence = REPO_ROOT / ".work" / "ci"
    evidence.mkdir(parents=True, exist_ok=True)
    arguments = [
        "-v", "--tb=short", "-ra", "--cov=mesh", "--cov-fail-under=60", "--cov-report=term-missing",
        f"--cov-report=xml:{evidence / 'coverage.xml'}", f"--junitxml={evidence / 'tests.xml'}",
    ]
    if os.environ.get("HYPOTHESIS_PROFILE"):
        arguments.extend([f"--hypothesis-profile={os.environ['HYPOTHESIS_PROFILE']}", "--hypothesis-show-statistics"])
    workers = int(os.environ.get("MESH_TEST_WORKERS", "2"))
    cmd = pytest_command(workers, arguments)
    lines: deque[str] = deque(maxlen=150)
    failures: list[str] = []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="mesh-ci-test-run-", ignore_cleanup_errors=True) as temporary:
        environment = isolated_environment(Path(temporary))
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=environment,
        )
        sampler = _ResourceSampler(proc.pid)
        sampler.start()
        try:
            if proc.stdout:
                for line in proc.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    lines.append(line)
                    stripped = line.strip()
                    if stripped.startswith(("FAILED ", "ERROR ")):
                        failures.append(stripped)
            return_code = proc.wait()
        except KeyboardInterrupt:
            _stop_process_tree(proc)
            return_code = 130
        except BaseException:
            _stop_process_tree(proc)
            raise
        finally:
            resources = sampler.stop()
            if proc.stdout:
                proc.stdout.close()

    report = {
        "workers": workers,
        "distribution": "loadfile" if workers > 1 else "sequential",
        "exit_code": return_code,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "python_version": sys.version.split()[0],
        **resources,
    }
    (evidence / "test-run.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    # Emit GitHub workflow error annotations
    for f in failures:
        # e.g. FAILED tests/test_perf_smoke.py::test_core_operations_latency_budget - ...
        parts = f.split(" ", 2)
        test_id = parts[1] if len(parts) > 1 else f
        escaped = test_id.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Test Failure::{escaped}")

    # Write GitHub Step Summary
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        os_name = os.environ.get("RUNNER_OS", sys.platform)
        py_ver = sys.version.split()[0]
        try:
            with open(summary_path, "a", encoding="utf-8") as s:
                s.write(f"### Pytest: {os_name} (Python {py_ver})\n\n")
                if return_code == 0:
                    s.write(":white_check_mark: **All tests passed!**\n\n")
                else:
                    s.write(f":x: **Failed with exit code {return_code}**\n\n")
                    if failures:
                        s.write("#### Failed Tests:\n")
                        for f in failures:
                            s.write(f"- `{f}`\n")
                        s.write("\n")
                    s.write("<details><summary>Failure Output (last 150 lines)</summary>\n\n```\n")
                    s.write("".join(lines))
                    s.write("\n```\n</details>\n\n")
        except Exception as exc:
            print(f"Warning: could not write GITHUB_STEP_SUMMARY: {exc}", file=sys.stderr)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
