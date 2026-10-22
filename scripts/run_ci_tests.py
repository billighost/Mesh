"""
scripts/run_ci_tests.py — CI Pytest runner with live streaming, GitHub annotations, and step summary.
"""
from __future__ import annotations

import os
import subprocess
import sys


def main() -> None:
    cmd = [sys.executable, "-m", "pytest", "-v", "--tb=short", "-ra"]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    lines: list[str] = []
    if proc.stdout:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.append(line)
    proc.wait()

    failures: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("FAILED ") or stripped.startswith("ERROR "):
            failures.append(stripped)

    # Emit GitHub workflow error annotations
    for f in failures:
        # e.g. FAILED tests/test_perf_smoke.py::test_core_operations_latency_budget - ...
        parts = f.split(" ", 2)
        test_id = parts[1] if len(parts) > 1 else f
        print(f"::error title=Test Failure::{test_id}")

    # Write GitHub Step Summary
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        os_name = os.environ.get("RUNNER_OS", sys.platform)
        py_ver = sys.version.split()[0]
        try:
            with open(summary_path, "a", encoding="utf-8") as s:
                s.write(f"### Pytest: {os_name} (Python {py_ver})\n\n")
                if proc.returncode == 0:
                    s.write(":white_check_mark: **All tests passed!**\n\n")
                else:
                    s.write(f":x: **Failed with exit code {proc.returncode}**\n\n")
                    if failures:
                        s.write("#### Failed Tests:\n")
                        for f in failures:
                            s.write(f"- `{f}`\n")
                        s.write("\n")
                    s.write("<details><summary>Failure Output (last 150 lines)</summary>\n\n```\n")
                    s.write("".join(lines[-150:]))
                    s.write("\n```\n</details>\n\n")
        except Exception as exc:
            print(f"Warning: could not write GITHUB_STEP_SUMMARY: {exc}", file=sys.stderr)

    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
