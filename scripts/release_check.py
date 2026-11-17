"""
scripts/release_check.py — Pre-release validation and PyPI Trusted Publishing setup guide.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request


QUALIFICATION_JOBS = ("Same-commit qualification (primary)", "Same-commit qualification (floor)")
MEASUREMENT_JOBS = {
    "storage": "Serial production matrix (manual only)",
    "test_speed": "Full-suite test speed (manual only, fixed order)",
}
STORAGE_REQUIRED_PATHS = frozenset({
    "pyproject.toml", "MANIFEST.in", "benchmark_all_surfaces.py",
    "mesh/memory.py", "mesh/store.py", "mesh/sqlite_runtime.py",
    "benchmarks/production_bench.py", "benchmarks/production_transport.py",
    "benchmarks/production_runner.py", "benchmarks/production_ledger.py",
    "benchmarks/production_report.py", "benchmarks/production_test_speed.py",
    "benchmarks/freeze_production_dependencies.py", "benchmarks/provision_production_model.py",
    ".github/workflows/production-performance.yml", ".github/workflows/retrieval-quality.yml",
    "scripts/run_tests.py", "scripts/run_ci_tests.py", "scripts/check_ci_measurement_scope.py",
    "scripts/schema_sources.py", "scripts/validate_production_package.py",
})


def check_version_sync(repo_root: pathlib.Path) -> str:
    print("[1/5] Checking version synchronization...")
    pyproject = repo_root / "pyproject.toml"
    pyproject_text = pyproject.read_text(encoding="utf-8")
    m_pyproj = re.search(r'version\s*=\s*"([^"]+)"', pyproject_text)
    if not m_pyproj:
        raise ValueError("Could not find version in pyproject.toml!")
    v_pyproj = m_pyproj.group(1)

    version_py = repo_root / "mesh" / "version.py"
    version_py_text = version_py.read_text(encoding="utf-8")
    m_ver = re.search(r'VERSION\s*=\s*"([^"]+)"', version_py_text)
    if not m_ver:
        raise ValueError("Could not find VERSION in mesh/version.py!")
    v_code = m_ver.group(1)

    if v_pyproj != v_code:
        raise ValueError(
            f"Version mismatch! pyproject.toml has '{v_pyproj}' but mesh/version.py has '{v_code}'."
        )

    print(f"      Version synchronized: v{v_pyproj}")
    return v_pyproj


def check_changelog(repo_root: pathlib.Path, version: str) -> None:
    print("[2/5] Checking CHANGELOG.md for release notes...")
    changelog = repo_root / "CHANGELOG.md"
    if not changelog.exists():
        raise FileNotFoundError("CHANGELOG.md not found in repository root!")
    changelog_text = changelog.read_text(encoding="utf-8")

    # Look for version header, e.g. ## [0.7.2] or ## 0.7.2 or [0.7.2]
    pattern = rf"(?:##\s*\[?v?{re.escape(version)}\]?|\[{re.escape(version)}\])"
    if not re.search(pattern, changelog_text):
        raise ValueError(f"CHANGELOG.md must contain release notes for v{version}.")
    else:
        print(f"      CHANGELOG.md contains release notes for v{version}.")


def check_git_status(repo_root: pathlib.Path, *, require_clean: bool = False) -> None:
    print("[3/5] Checking git status and branch...")
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, text=True
    ).strip()
    status = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repo_root, text=True
    ).strip()
    print(f"      Current branch: {branch}")
    if status:
        if require_clean:
            raise ValueError("Publication requires a clean source checkout.")
        print("      Note: Working tree has uncommitted modifications.")
    else:
        print("      Working tree is clean.")


def github_read(path: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Mesh-release-qualification",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request("https://api.github.com/repos/billighost/Mesh/" + path, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _measurement_path(path: str, measurement: str) -> bool:
    if path in STORAGE_REQUIRED_PATHS:
        return True
    if path.startswith(("mesh/", "benchmarks/")) and path.endswith(".py"):
        return True
    if path.startswith("benchmarks/retrieval_fixtures/") and path.endswith((".json", ".jsonl")):
        return True
    return measurement == "test_speed" and path.startswith(("mesh/", "tests/", "scripts/"))


def _git_sources(repo_root: pathlib.Path, commit: str, measurement: str) -> dict[str, tuple[str, str]]:
    records = subprocess.check_output(["git", "ls-tree", "-r", "-z", "--full-tree", commit], cwd=repo_root)
    result = {}
    for record in records.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, blob = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise ValueError("Invalid Git source-tree record.") from error
        if not _measurement_path(path, measurement):
            continue
        if (path.startswith("/") or "\\" in path or ":" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or any(ord(character) < 32 for character in path)
                or mode not in {"100644", "100755"} or kind != "blob"
                or re.fullmatch(r"[0-9a-f]{40}", blob) is None or path in result):
            raise ValueError("Unsafe measurement source path or Git object.")
        result[path] = (mode, blob)
    if not STORAGE_REQUIRED_PATHS.issubset(result):
        raise ValueError("Required measurement sources are missing.")
    if measurement == "test_speed" and not any(path.startswith("tests/") for path in result):
        raise ValueError("Test-speed source proof requires the complete test tree.")
    return result


def measurement_source_proof(repo_root: pathlib.Path, release_commit: str, measured_commit: str,
                             measurement: str) -> dict:
    """Compare immutable Git blobs and modes; changed, added or removed sources reject reuse."""
    if measurement not in MEASUREMENT_JOBS:
        raise ValueError("Unknown release measurement scope.")
    if any(re.fullmatch(r"[0-9a-f]{40}", commit) is None for commit in (release_commit, measured_commit)):
        raise ValueError("Measurement source proof requires full Git commits.")
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", measured_commit, release_commit],
                              cwd=repo_root, capture_output=True, check=False)
    if ancestor.returncode != 0:
        raise ValueError("Measurement commit is not an available release ancestor.")
    current = _git_sources(repo_root, release_commit, measurement)
    measured = _git_sources(repo_root, measured_commit, measurement)
    if current != measured:
        raise ValueError("Measurement source bytes, file modes or path set changed.")
    return {"measurement": measurement, "release_commit": release_commit, "measured_commit": measured_commit,
            "same_commit": release_commit == measured_commit,
            "comparison": "Exact Git blob identities and file modes; no newline normalization.",
            "verified_paths": sorted(current)}


def _completed_runs(payload: dict, commit: str | None = None) -> list[dict]:
    return [run for run in payload.get("workflow_runs", []) if isinstance(run, dict)
            and type(run.get("id")) is int and run["id"] > 0
            and isinstance(run.get("head_sha"), str) and re.fullmatch(r"[0-9a-f]{40}", run["head_sha"])
            and run.get("status") == "completed" and (commit is None or run["head_sha"] == commit)]


def _successful_jobs(run_id: int) -> set[str]:
    payload = github_read(f"actions/runs/{run_id}/jobs?per_page=100")
    jobs = payload.get("jobs", [])
    if not isinstance(jobs, list) or payload.get("total_count", len(jobs)) != len(jobs):
        raise ValueError("Incomplete qualification job evidence.")
    counts = {}
    passed = set()
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str):
            raise ValueError("Invalid qualification job evidence.")
        name = job["name"]
        counts[name] = counts.get(name, 0) + 1
        if job.get("status") == "completed" and job.get("conclusion") == "success":
            passed.add(name)
    if any(counts.get(name, 0) > 1 for name in (*QUALIFICATION_JOBS, *MEASUREMENT_JOBS.values())):
        raise ValueError("Ambiguous qualification job evidence.")
    return passed


def verify_publication_source(repo_root: pathlib.Path, version: str) -> dict:
    """Require current correctness; reuse ancestral measurements only with exact source proof."""
    check_git_status(repo_root, require_clean=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Publication requires a full Git commit.")
    expected_ref = "refs/tags/v" + version
    if os.environ.get("GITHUB_REF") != expected_ref or os.environ.get("GITHUB_SHA") != commit:
        raise ValueError("Publication must run from the exact version tag and commit.")
    tagged = subprocess.check_output(["git", "rev-parse", expected_ref + "^{}"], cwd=repo_root, text=True).strip()
    if tagged != commit:
        raise ValueError("Version tag does not identify the checked-out source.")
    if os.environ.get("GITHUB_REPOSITORY", "").lower() != "billighost/mesh":
        raise ValueError("Unexpected publication repository.")
    ci = _completed_runs(github_read(
        f"actions/workflows/ci.yml/runs?head_sha={commit}&status=completed&per_page=100"), commit)
    ci_run = next((run for run in ci if run.get("conclusion") == "success"), None)
    if ci_run is None:
        raise ValueError("Missing completed same-source qualification: ci.yml")
    production = _completed_runs(github_read(
        f"actions/workflows/production-performance.yml/runs?head_sha={commit}&status=completed&per_page=100"), commit)
    job_cache = {}

    def passed_jobs(run: dict) -> set[str]:
        if run["id"] not in job_cache:
            job_cache[run["id"]] = _successful_jobs(run["id"])
        return job_cache[run["id"]]

    qualification = next((run for run in production if set(QUALIFICATION_JOBS).issubset(passed_jobs(run))), None)
    if qualification is None:
        raise ValueError("Missing completed same-source qualification: production-performance.yml")
    measurements = {}
    for measurement, job in MEASUREMENT_JOBS.items():
        current = next((run for run in production if job in passed_jobs(run)), None)
        if current is not None:
            measurements[measurement] = {"run_id": current["id"], "measured_commit": commit,
                                         "release_commit": commit, "same_commit": True}
    if len(measurements) < len(MEASUREMENT_JOBS):
        ancestors = _completed_runs(github_read(
            "actions/workflows/production-performance.yml/runs?status=completed&per_page=100"))
        for measurement, job in MEASUREMENT_JOBS.items():
            if measurement in measurements:
                continue
            for run in ancestors:
                if run["head_sha"] == commit or job not in passed_jobs(run):
                    continue
                try:
                    source = measurement_source_proof(repo_root, commit, run["head_sha"], measurement)
                except (ValueError, subprocess.CalledProcessError):
                    continue
                measurements[measurement] = {"run_id": run["id"], **source}
                break
            if measurement not in measurements:
                raise ValueError("Missing completed source-bound measurement: " + measurement)
    proof = {"ci.yml": ci_run["id"], "production-performance.yml": qualification["id"]}
    result = {"status": "passed", "commit": commit, "tag": "v" + version, "qualification_runs": proof,
              "measurement_runs": measurements,
              "optional_retrieval_policy": "Retain experiment failures and keep unqualified optional defaults disabled."}
    print(json.dumps(result))
    return result


def run_build_pipeline(repo_root: pathlib.Path) -> None:
    print("[4/5] Executing release build & validation pipeline...")
    build_script = repo_root / "scripts" / "build_release.py"
    subprocess.check_call([sys.executable, str(build_script)], cwd=repo_root)


def print_trusted_publishing_guide(version: str) -> None:
    print("[5/5] PyPI Trusted Publishing (OIDC) Instructions:")
    print("=" * 68)
    print("  To enable automated, credential-less PyPI publishing with zero API keys:")
    print()
    print("  1. Log into your PyPI account at: https://pypi.org/manage/account/publishing/")
    print("     (Or TestPyPI: https://test.pypi.org/manage/account/publishing/)")
    print()
    print("  2. Under 'Add a publisher', select 'GitHub':")
    print("     - PyPI Project Name: mesh-context-layer")
    print("     - Owner:             billighost")
    print("     - Repository name:   Mesh")
    print("     - Workflow name:     publish.yml")
    print("     - Environment name:  pypi   (or testpypi)")
    print()
    print("  3. Click 'Add publisher'.")
    print()
    print("  4. In your GitHub repository (https://github.com/billighost/Mesh):")
    print("     - Go to Settings -> Environments.")
    print("     - Create an environment named 'pypi'.")
    print("     - (Optional) Add required reviewers for production releases.")
    print()
    print(f"  5. To publish release v{version}:")
    print(f"     git tag -a v{version} -m 'Release v{version}'")
    print(f"     git push origin v{version}")
    print(f"     Create and publish the GitHub release for v{version}.")
    print()
    print("     The published-release event starts the workflow; pushing only the tag does not.")
    print("     Manual publication also requires selecting the exact version tag.")
    print("     Current CI and full qualification must pass on that exact tag commit.")
    print("     Older measurements require an ancestor with identical measured source bytes.")
    print("     After source-bound qualification, Actions exchanges an OIDC token with PyPI")
    print("     and securely publish the wheel and sdist!")
    print("=" * 68)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checks-only", action="store_true", help="Validate source metadata without building packages.")
    parser.add_argument("--verify-publication", action="store_true",
                        help="Require the exact tag, clean source, current CI/full qualification and source-bound measurements.")
    options = parser.parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    print("=================================================================")
    print("    Mesh Context Layer — Release Rehearsal & Pre-flight Check   ")
    print("=================================================================")
    version = check_version_sync(repo_root)
    check_changelog(repo_root, version)
    if options.verify_publication:
        verify_publication_source(repo_root, version)
    else:
        check_git_status(repo_root)
    if not options.checks_only:
        run_build_pipeline(repo_root)
    print_trusted_publishing_guide(version)
    print("Pre-flight release check PASSED.")


if __name__ == "__main__":
    main()
