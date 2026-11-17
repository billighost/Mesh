"""Record exact qualified package versions and relative source hashes for CI."""
from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path
import re

import production_runner as runner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = runner.private_path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    project_name = "mesh-context-layer"
    lines = set()
    for distribution in importlib.metadata.distributions():
        name, version = distribution.metadata["Name"], distribution.version
        if not name or name.lower().replace("_", "-") == project_name:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", version):
            raise runner.ControlError("UnsafeDependencyConstraint")
        lines.add(name + "==" + version)
    (directory / "constraints.txt").write_text("\n".join(sorted(lines, key=str.lower)) + "\n", encoding="utf-8")
    runner.write_json(directory / "source-runtime.json", {
        "runtime_sources": runner.manifest(), "harness_sources": runner.harness_manifest(),
        "runtime": runner.runtime(),
        "test_execution_sources": {
            "launcher_sha256": runner.sha_file(runner.REPO / "scripts/run_tests.py"),
            "controller_sha256": runner.sha_file(runner.REPO / "benchmarks/production_test_speed.py"),
        },
    })
    print(f"Qualified dependency constraints recorded: {len(lines)} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
