"""Model-free release source fencing and authoritative package metadata checks."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from scripts import build_release, release_check


COMMIT = "a" * 40
REQUIRED_JOBS = (
    "Same-commit qualification (primary)", "Same-commit qualification (floor)",
    "Serial production matrix (manual only)", "Full-suite test speed (manual only, fixed order)",
)


def fake_package(tmp_path: Path, version: str = "0.9.0") -> Path:
    (tmp_path / "mesh").mkdir()
    (tmp_path / "mesh/version.py").write_text('VERSION = "0.9.0"\n', encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    metadata = (f"Metadata-Version: 2.4\nName: mesh-context-layer\nVersion: {version}\n"
                "Requires-Dist: apsw<4,>=3.51.3.0\n"
                'Requires-Dist: keyring>=24; extra == "secrets"\n\n')
    with zipfile.ZipFile(dist / f"mesh_context_layer-{version}-py3-none-any.whl", "w") as package:
        package.writestr(f"mesh_context_layer-{version}.dist-info/METADATA", metadata)
    (dist / f"mesh_context_layer-{version}.tar.gz").write_bytes(b"fake-archive-bytes")
    return dist


def git_metadata(arguments, **_kwargs):
    return COMMIT + "\n" if arguments[-1] == "HEAD" else "release-fixture\n"


def publication_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v0.9.0")
    monkeypatch.setenv("GITHUB_SHA", COMMIT)
    monkeypatch.setenv("GITHUB_REPOSITORY", "billighost/Mesh")
    monkeypatch.setattr(release_check.subprocess, "check_output", lambda *_args, **_kwargs: COMMIT + "\n")


def qualification_reader(path: str, jobs=None):
    if path.startswith("actions/workflows/ci.yml/"):
        return {"workflow_runs": [{"id": 101, "head_sha": COMMIT, "status": "completed", "conclusion": "success"}]}
    if path.startswith("actions/workflows/production-performance.yml/"):
        return {"workflow_runs": [{"id": 102, "head_sha": COMMIT, "status": "completed", "conclusion": "failure"}]}
    assert path == "actions/runs/102/jobs?per_page=100"
    return {"jobs": jobs or [
        *({"name": name, "status": "completed", "conclusion": "success"} for name in REQUIRED_JOBS),
        {"name": "Same-source retrieval quality / Guarded retrieval (fixture, compact-80)",
         "status": "completed", "conclusion": "failure"},
    ]}


def test_release_provenance_uses_built_requirements_including_apsw_and_marked_extras(tmp_path, monkeypatch):
    dist = fake_package(tmp_path)
    monkeypatch.setattr(build_release.subprocess, "check_output", git_metadata)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    build_release.generate_checksums_and_provenance(tmp_path, dist)
    provenance = json.loads((dist / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["commit"] == COMMIT
    assert provenance["declared_requirements"] == ["apsw<4,>=3.51.3.0", 'keyring>=24; extra == "secrets"']
    assert len(provenance["artifacts"]) == 2
    sbom = json.loads((dist / "sbom.json").read_text(encoding="utf-8"))
    assert {component["name"] for component in sbom["components"]} == {"apsw", "keyring"}
    assert all("version" not in component for component in sbom["components"])
    assert all(component["properties"][0]["value"] in provenance["declared_requirements"]
               for component in sbom["components"])


def test_release_provenance_rejects_a_wheel_from_another_version(tmp_path, monkeypatch):
    dist = fake_package(tmp_path, version="0.8.3")
    monkeypatch.setattr(build_release.subprocess, "check_output", git_metadata)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    with pytest.raises(ValueError, match="wheel version"):
        build_release.generate_checksums_and_provenance(tmp_path, dist)
    assert not (dist / "provenance.json").exists()


def test_release_provenance_rejects_a_different_requested_commit(tmp_path, monkeypatch):
    dist = fake_package(tmp_path)
    monkeypatch.setattr(build_release.subprocess, "check_output", git_metadata)
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    with pytest.raises(ValueError, match="requested GitHub source"):
        build_release.generate_checksums_and_provenance(tmp_path, dist)


def test_release_build_does_not_retry_a_failed_build_with_weaker_isolation(tmp_path, monkeypatch):
    calls = []

    def fail(arguments, **_kwargs):
        calls.append(arguments)
        raise subprocess.CalledProcessError(1, arguments)

    monkeypatch.setattr(build_release.subprocess, "check_call", fail)
    with pytest.raises(subprocess.CalledProcessError):
        build_release.build_artifacts(tmp_path)
    assert len(calls) == 1
    assert "--no-isolation" not in calls[0]


def test_existing_distributions_are_listed_and_preserved(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build_release, "REPO_ROOT", tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    old = dist / "existing.whl"
    old.write_bytes(b"preserve-me")
    with pytest.raises(FileExistsError, match="empty dist"):
        build_release.clean_dist(dist)
    assert old.read_bytes() == b"preserve-me"
    assert "existing.whl" in capsys.readouterr().out


def test_publication_requires_the_version_tag_before_reading_remote_evidence(tmp_path, monkeypatch):
    publication_environment(monkeypatch)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setattr(release_check, "github_read", lambda _path: pytest.fail("Unfenced source read"))
    with pytest.raises(ValueError, match="exact version tag"):
        release_check.verify_publication_source(tmp_path, "0.9.0")


def test_publication_accepts_required_source_checks_and_retains_optional_failure(tmp_path, monkeypatch):
    publication_environment(monkeypatch)
    monkeypatch.setattr(release_check, "github_read", qualification_reader)
    proof = release_check.verify_publication_source(tmp_path, "0.9.0")
    assert proof["qualification_runs"] == {"ci.yml": 101, "production-performance.yml": 102}
    assert "disabled" in proof["optional_retrieval_policy"]


@pytest.mark.parametrize("missing", REQUIRED_JOBS)
def test_publication_rejects_missing_required_qualification(tmp_path, monkeypatch, missing):
    publication_environment(monkeypatch)
    jobs = [{"name": name, "status": "completed", "conclusion": "success"}
            for name in REQUIRED_JOBS if name != missing]
    monkeypatch.setattr(release_check, "github_read", lambda path: qualification_reader(path, jobs))
    with pytest.raises(ValueError, match="same-source qualification"):
        release_check.verify_publication_source(tmp_path, "0.9.0")


def test_publication_rejects_successful_ci_for_another_commit(tmp_path, monkeypatch):
    publication_environment(monkeypatch)
    monkeypatch.setattr(release_check, "github_read", lambda _path: {
        "workflow_runs": [{"id": 101, "head_sha": "b" * 40, "status": "completed", "conclusion": "success"}]})
    with pytest.raises(ValueError, match="ci.yml"):
        release_check.verify_publication_source(tmp_path, "0.9.0")
