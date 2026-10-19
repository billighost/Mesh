"""
scripts/release_check.py — Pre-release validation and PyPI Trusted Publishing setup guide.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys


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
        print(f"      WARNING: CHANGELOG.md does not yet have an explicit section for v{version}.")
    else:
        print(f"      CHANGELOG.md contains release notes for v{version}.")


def check_git_status(repo_root: pathlib.Path) -> None:
    print("[3/5] Checking git status and branch...")
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, text=True
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repo_root, text=True
        ).strip()

        print(f"      Current branch: {branch}")
        if status:
            print("      Note: Working tree has uncommitted modifications.")
        else:
            print("      Working tree is clean.")
    except Exception as exc:
        print(f"      Could not query git: {exc}")


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
    print()
    print("     GitHub Actions will exchange a short-lived OIDC token with PyPI")
    print("     and securely publish the wheel and sdist!")
    print("=" * 68)


def main() -> None:
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    print("=================================================================")
    print("    Mesh Context Layer — Release Rehearsal & Pre-flight Check   ")
    print("=================================================================")
    version = check_version_sync(repo_root)
    check_changelog(repo_root, version)
    check_git_status(repo_root)
    run_build_pipeline(repo_root)
    print_trusted_publishing_guide(version)
    print("Pre-flight release check PASSED.")


if __name__ == "__main__":
    main()
