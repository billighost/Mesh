"""
scripts/build_release.py — Production release builder, validation, and SBOM/provenance generator.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import zipfile

# Ensure repository root is in sys.path for standalone execution
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def clean_dist(dist_dir: pathlib.Path) -> None:
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    print(f"[1/6] Cleaned distribution directory: {dist_dir}")


def build_artifacts(repo_root: pathlib.Path) -> None:
    print("[2/6] Building sdist and wheel with build backend...")
    subprocess.check_call([sys.executable, "-m", "build", "--no-isolation"], cwd=repo_root)


def validate_twine(dist_dir: pathlib.Path) -> None:
    print("[3/6] Running twine check --strict on built distributions...")
    dist_files = [str(p) for p in dist_dir.iterdir() if p.suffix in (".whl", ".gz")]
    subprocess.check_call([sys.executable, "-m", "twine", "check", "--strict", *dist_files])
    print("      Twine check PASSED.")


def validate_archive_contents(dist_dir: pathlib.Path) -> None:
    print("[4/6] Inspecting distribution contents for leaked files...")
    sdist_files = list(dist_dir.glob("*.tar.gz"))
    wheel_files = list(dist_dir.glob("*.whl"))

    if not sdist_files or not wheel_files:
        raise RuntimeError("Missing sdist or wheel in dist/")

    # Check sdist
    with tarfile.open(sdist_files[0]) as t:
        sdist_members = t.getnames()

    # Check wheel
    with zipfile.ZipFile(wheel_files[0]) as z:
        wheel_members = z.namelist()

    banned_extensions = (".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".mp3", ".wav")
    leaks = []
    for member in sdist_members + wheel_members:
        lower = member.lower()
        if any(lower.endswith(ext) for ext in banned_extensions) or "marketing" in lower:
            leaks.append(member)

    if leaks:
        raise RuntimeError(f"Distribution packaging leak detected! Found banned files: {leaks}")

    print(f"      Sdist contains {len(sdist_members)} files (0 leaks).")
    print(f"      Wheel contains {len(wheel_members)} files (0 leaks).")


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

    # Git metadata
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True).strip()
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, text=True).strip()
    except Exception:
        commit = os.environ.get("GITHUB_SHA", "unknown")
        branch = os.environ.get("GITHUB_REF_NAME", "unknown")

    from mesh.version import VERSION

    provenance = {
        "schema_version": "1.0",
        "subject": "mesh-context-layer",
        "version": VERSION,
        "build_timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_repository": "https://github.com/billighost/Mesh",
        "commit": commit,
        "branch": branch,
        "build_backend": "hatchling.build",
        "builder": "pypa/build",
        "artifacts": artifacts,
        "runtime_dependencies": [
            "numpy>=1.24.0",
            "onnxruntime>=1.17.0",
            "tokenizers>=0.15.0",
            "fastapi>=0.104.0",
            "uvicorn>=0.24.0",
            "cryptography>=41.0.0",
            "keyring>=24.0.0",
        ],
    }
    (dist_dir / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "component": {
                "name": "mesh-context-layer",
                "version": VERSION,
                "type": "library",
                "purl": f"pkg:pypi/mesh-context-layer@{VERSION}",
                "licenses": [{"license": {"id": "MIT"}}],
            },
        },
        "components": [
            {"name": "numpy", "version": ">=1.24.0", "purl": "pkg:pypi/numpy"},
            {"name": "onnxruntime", "version": ">=1.17.0", "purl": "pkg:pypi/onnxruntime"},
            {"name": "tokenizers", "version": ">=0.15.0", "purl": "pkg:pypi/tokenizers"},
            {"name": "fastapi", "version": ">=0.104.0", "purl": "pkg:pypi/fastapi"},
            {"name": "uvicorn", "version": ">=0.24.0", "purl": "pkg:pypi/uvicorn"},
            {"name": "cryptography", "version": ">=41.0.0", "purl": "pkg:pypi/cryptography"},
            {"name": "keyring", "version": ">=24.0.0", "purl": "pkg:pypi/keyring"},
        ],
    }
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
