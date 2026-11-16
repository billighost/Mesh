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


def verify_publication_source(repo_root: pathlib.Path, version: str) -> dict:
    """Require tagged source and completed correctness/storage proof from that exact commit."""
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
    requirements = {
        "ci.yml": (),
        "production-performance.yml": (
            "Same-commit qualification (primary)", "Same-commit qualification (floor)",
            "Serial production matrix (manual only)", "Full-suite test speed (manual only, fixed order)",
        ),
    }
    proof = {}
    for workflow, required_jobs in requirements.items():
        payload = github_read(f"actions/workflows/{workflow}/runs?head_sha={commit}&status=completed&per_page=100")
        candidates = [run for run in payload.get("workflow_runs", [])
                      if run.get("head_sha") == commit and run.get("status") == "completed"]
        accepted = None
        for run in candidates:
            if not required_jobs:
                if run.get("conclusion") == "success":
                    accepted = run
                    break
                continue
            jobs = github_read(f"actions/runs/{run['id']}/jobs?per_page=100").get("jobs", [])
            by_name = {job.get("name"): job for job in jobs}
            if all(by_name.get(name, {}).get("status") == "completed"
                   and by_name[name].get("conclusion") == "success" for name in required_jobs):
                accepted = run
                break
        if accepted is None:
            raise ValueError("Missing completed same-source qualification: " + workflow)
        proof[workflow] = accepted["id"]
    result = {"status": "passed", "commit": commit, "tag": "v" + version, "qualification_runs": proof,
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
    print("     After same-source qualification, Actions exchanges an OIDC token with PyPI")
    print("     and securely publish the wheel and sdist!")
    print("=" * 68)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checks-only", action="store_true", help="Validate source metadata without building packages.")
    parser.add_argument("--verify-publication", action="store_true",
                        help="Require the exact version tag, clean source and completed same-source CI/storage qualification.")
    options = parser.parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    print("=================================================================")
    print("    Mesh Context Layer — Release Rehearsal & Pre-flight Check   ")
    print("=================================================================")
    version = check_version_sync(repo_root)
    check_changelog(repo_root, version)
    check_git_status(repo_root, require_clean=options.verify_publication)
    if options.verify_publication:
        verify_publication_source(repo_root, version)
    if not options.checks_only:
        run_build_pipeline(repo_root)
    print_trusted_publishing_guide(version)
    print("Pre-flight release check PASSED.")


if __name__ == "__main__":
    main()
