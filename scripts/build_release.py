"""
scripts/build_release.py — Production release builder, validation, and SBOM/provenance generator.
"""
from __future__ import annotations

import datetime
from email.parser import Parser
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tarfile
import zipfile

# Ensure repository root is in sys.path for standalone execution
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def clean_dist(dist_dir: pathlib.Path) -> None:
    expected = REPO_ROOT / "dist"
    if dist_dir.is_symlink() or dist_dir.resolve() != expected.resolve():
        raise ValueError("Release distributions require the repository dist directory.")
    if dist_dir.exists():
        entries = sorted(path.name for path in dist_dir.iterdir())
        print("Existing distribution entries: " + json.dumps(entries))
        if entries:
            raise FileExistsError("Use an empty dist directory for a release build.")
    dist_dir.mkdir(parents=True, exist_ok=True)
    print("[1/6] Verified an empty distribution directory.")


def build_artifacts(repo_root: pathlib.Path) -> None:
    print("[2/6] Building sdist and wheel with build backend...")
    subprocess.check_call([sys.executable, "-m", "build"], cwd=repo_root)


def validate_twine(dist_dir: pathlib.Path) -> None:
    print("[3/6] Running twine check --strict on built distributions...")
    dist_files = [str(p) for p in dist_dir.iterdir() if p.suffix in (".whl", ".gz")]
    subprocess.check_call([sys.executable, "-m", "twine", "check", "--strict", *dist_files])
    print("      Twine check PASSED.")


def validate_archive_contents(dist_dir: pathlib.Path) -> None:
    print("[4/6] Inspecting distribution contents for leaked files...")
    sdist_files = list(dist_dir.glob("*.tar.gz"))
    wheel_files = list(dist_dir.glob("*.whl"))

    if len(sdist_files) != 1 or len(wheel_files) != 1:
        raise RuntimeError("Release builds require exactly one sdist and one wheel.")

    # Check sdist
    with tarfile.open(sdist_files[0]) as t:
        sdist_members = t.getnames()

    # Check wheel
    with zipfile.ZipFile(wheel_files[0]) as z:
        wheel_members = z.namelist()

    banned_extensions = (
        ".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".mp3", ".wav",
        ".dll", ".so", ".dylib", ".pyd", ".exe", ".onnx", ".safetensors",
    )
    leaks = []
    for member in sdist_members + wheel_members:
        lower = member.lower()
        if (any(lower.endswith(ext) for ext in banned_extensions)
                or any(part in {".env", ".mesh", ".work", ".codex", "node_modules", "marketing"}
                       for part in pathlib.PurePosixPath(member).parts)):
            leaks.append(member)

    if leaks:
        raise RuntimeError(f"Distribution packaging leak detected! Found banned files: {leaks}")

    print(f"      Sdist contains {len(sdist_members)} files (0 leaks).")
    print(f"      Wheel contains {len(wheel_members)} files (0 leaks).")


def distribution_metadata(dist_dir: pathlib.Path) -> tuple[str, str, list[str]]:
    wheels = sorted(dist_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Release metadata requires exactly one wheel.")
    with zipfile.ZipFile(wheels[0]) as package:
        metadata_names = [name for name in package.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("Wheel metadata must have one package identity.")
        metadata = Parser().parsestr(package.read(metadata_names[0]).decode("utf-8"))
    name, version = metadata["Name"], metadata["Version"]
    if name != "mesh-context-layer" or not version:
        raise ValueError("Unexpected release package identity.")
    return name, version, sorted(metadata.get_all("Requires-Dist", []))


def generate_checksums_and_provenance(repo_root: pathlib.Path, dist_dir: pathlib.Path) -> None:
    print("[5/6] Generating SHA-256 checksums, provenance, and SBOM...")
    files = sorted([f for f in dist_dir.iterdir() if f.suffix in (".whl", ".gz")])

    sha256_lines = []
    artifacts = []

    for f in files:
        data = f.read_bytes()
        h = hashlib.sha256(data).hexdigest()
        sha256_lines.append(f"{h}  {f.name}")
        artifacts.append({
            "name": f.name,
            "sha256": h,
            "size_bytes": len(data),
            "content_type": "application/x-wheel+zip" if f.suffix == ".whl" else "application/gzip",
        })

    (dist_dir / "SHA256SUMS.txt").write_text("\n".join(sha256_lines) + "\n", encoding="utf-8")

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True).strip()
    branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Release provenance requires a full Git commit.")
    expected_commit = os.environ.get("GITHUB_SHA")
    if expected_commit and expected_commit != commit:
        raise ValueError("Release build does not match the requested GitHub source.")
    name, version, requirements = distribution_metadata(dist_dir)
    source_metadata = (repo_root / "mesh/version.py").read_text(encoding="utf-8")
    version_match = re.search(r'^VERSION\s*=\s*"([^"\n]+)"', source_metadata, re.M)
    if version_match is None or version_match.group(1) != version:
        raise ValueError("Built wheel version does not match the source version.")

    provenance = {
        "schema_version": "1.0",
        "subject": name,
        "version": version,
        "build_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_repository": "https://github.com/billighost/Mesh",
        "commit": commit,
        "branch": branch,
        "build_backend": "hatchling.build",
        "builder": "pypa/build",
        "artifacts": artifacts,
        "declared_requirements": requirements,
        "dependency_provenance": "Requires-Dist from the built wheel; includes optional extras and environment markers.",
    }
    (dist_dir / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "component": {
                "name": name,
                "version": version,
                "type": "library",
                "purl": f"pkg:pypi/mesh-context-layer@{version}",
                "licenses": [{"license": {"id": "MIT"}}],
            },
        },
        "components": [],
    }
    for requirement in requirements:
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
        if match is None:
            raise ValueError("Invalid built-wheel dependency requirement.")
        dependency = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        sbom["components"].append({
            "type": "library", "name": dependency, "purl": f"pkg:pypi/{dependency}",
            "properties": [{"name": "mesh:declared-requirement", "value": requirement}],
        })
    (dist_dir / "sbom.json").write_text(json.dumps(sbom, indent=2), encoding="utf-8")
    print("      Generated SHA256SUMS.txt, provenance.json, and sbom.json.")


def main() -> None:
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    dist_dir = repo_root / "dist"

    print("=================================================================")
    print("  Mesh Context Layer — Production Release Build & Verification  ")
    print("=================================================================")
    clean_dist(dist_dir)
    build_artifacts(repo_root)
    validate_twine(dist_dir)
    validate_archive_contents(dist_dir)
    generate_checksums_and_provenance(repo_root, dist_dir)
    print("[6/6] Build and verification finished successfully!")
    print("=================================================================")


if __name__ == "__main__":
    main()
