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
    monkeypatch.setattr(release_check.subprocess, "check_output",
                        lambda args, **_kwargs: "" if "status" in args else COMMIT + "\n")


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
    with pytest.raises(ValueError, match="same-source qualification|source-bound measurement"):
        release_check.verify_publication_source(tmp_path, "0.9.0")


def test_publication_rejects_successful_ci_for_another_commit(tmp_path, monkeypatch):
    publication_environment(monkeypatch)
    monkeypatch.setattr(release_check, "github_read", lambda _path: {
        "workflow_runs": [{"id": 101, "head_sha": "b" * 40, "status": "completed", "conclusion": "success"}]})
    with pytest.raises(ValueError, match="ci.yml"):
        release_check.verify_publication_source(tmp_path, "0.9.0")


def fixture_git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output([
        "git", "-c", "user.name=Release Fixture", "-c", "user.email=release@example.invalid",
        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + str(repo / "disabled-hooks"),
        *arguments,
    ], cwd=repo, text=True).strip()


def source_fixture(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    fixture_git(repo, "init", "--quiet")
    for name in (*release_check.STORAGE_REQUIRED_PATHS, "tests/test_fixture.py", "scripts/release_check.py",
                 "mesh/skills/fixture.md", "benchmarks/retrieval_fixtures/fixture/labels.json"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture-source\n")
    return repo, fixture_commit(repo)


def fixture_commit(repo: Path) -> str:
    fixture_git(repo, "add", "--", ".")
    fixture_git(repo, "commit", "--quiet", "-m", "Fixture source")
    return fixture_git(repo, "rev-parse", "HEAD")


def test_docs_only_commit_reuses_measurements_with_explicit_ancestor_and_scope(tmp_path):
    repo, measured = source_fixture(tmp_path)
    (repo / "README.md").write_bytes(b"Final release notes.\n")
    current = fixture_commit(repo)
    for measurement in release_check.MEASUREMENT_JOBS:
        proof = release_check.measurement_source_proof(repo, current, measured, measurement)
        assert proof["measured_commit"] == measured
        assert proof["release_commit"] == current
        assert proof["same_commit"] is False
        assert "mesh/store.py" in proof["verified_paths"]
        assert "README.md" not in proof["verified_paths"]
        assert ("tests/test_fixture.py" in proof["verified_paths"]) == (measurement == "test_speed")


@pytest.mark.parametrize("path,storage_rejected", [
    ("mesh/store.py", True), ("benchmarks/production_bench.py", True), ("pyproject.toml", True),
    ("scripts/run_tests.py", True), ("scripts/validate_production_package.py", True),
    (".github/workflows/production-performance.yml", True),
    (".github/workflows/retrieval-quality.yml", True),
    ("benchmarks/retrieval_fixtures/fixture/labels.json", True),
    ("tests/test_fixture.py", False), ("scripts/release_check.py", False),
    ("mesh/skills/fixture.md", False),
])
def test_one_byte_source_changes_reject_reuse_at_their_measurement_boundary(tmp_path, path, storage_rejected):
    repo, measured = source_fixture(tmp_path)
    target = repo / path
    target.write_bytes(target.read_bytes() + b"x")
    current = fixture_commit(repo)
    if storage_rejected:
        with pytest.raises(ValueError, match="source bytes"):
            release_check.measurement_source_proof(repo, current, measured, "storage")
    else:
        assert release_check.measurement_source_proof(repo, current, measured, "storage")["same_commit"] is False
    with pytest.raises(ValueError, match="source bytes"):
        release_check.measurement_source_proof(repo, current, measured, "test_speed")


@pytest.mark.parametrize("action", ["add", "delete"])
def test_changed_measurement_path_sets_reject_reuse(tmp_path, action):
    repo, measured = source_fixture(tmp_path)
    if action == "add":
        (repo / "benchmarks/new_harness.py").write_bytes(b"new source\n")
    else:
        target = repo / "benchmarks/production_bench.py"
        assert target.is_file()
        assert target in list(target.parent.iterdir())
        # The path is fixed inside the newly created fixture repository.
        target.unlink()
    current = fixture_commit(repo)
    with pytest.raises(ValueError, match="sources are missing|source bytes"):
        release_check.measurement_source_proof(repo, current, measured, "storage")


def test_measurement_source_requires_an_ancestor_and_full_commit_ids(tmp_path):
    repo, before = source_fixture(tmp_path)
    (repo / "README.md").write_bytes(b"A descendant.\n")
    after = fixture_commit(repo)
    with pytest.raises(ValueError, match="release ancestor"):
        release_check.measurement_source_proof(repo, before, after, "storage")
    with pytest.raises(ValueError, match="full Git commits"):
        release_check.measurement_source_proof(repo, after, before[:8], "storage")
    with pytest.raises(ValueError, match="Unknown release measurement"):
        release_check.measurement_source_proof(repo, after, before, "unknown")


def test_measurement_source_rejects_links_before_accepting_reuse(tmp_path, monkeypatch):
    repo, measured = source_fixture(tmp_path)
    real = release_check.subprocess.check_output

    def linked_tree(arguments, **kwargs):
        output = real(arguments, **kwargs)
        if "ls-tree" in arguments:
            output = output.replace(b"100644 blob", b"120000 blob", 1)
        return output

    monkeypatch.setattr(release_check.subprocess, "check_output", linked_tree)
    with pytest.raises(ValueError, match="Unsafe measurement source"):
        release_check.measurement_source_proof(repo, measured, measured, "storage")


def test_publication_reuses_docs_only_ancestor_but_requires_current_correctness(tmp_path, monkeypatch):
    repo, measured = source_fixture(tmp_path)
    (repo / "README.md").write_bytes(b"Final release documentation.\n")
    current = fixture_commit(repo)
    fixture_git(repo, "tag", "v0.9.0")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v0.9.0")
    monkeypatch.setenv("GITHUB_SHA", current)
    monkeypatch.setenv("GITHUB_REPOSITORY", "billighost/Mesh")

    def read(path):
        if path.startswith("actions/workflows/ci.yml/"):
            return {"workflow_runs": [{"id": 101, "head_sha": current, "status": "completed", "conclusion": "success"}]}
        if path.startswith("actions/workflows/production-performance.yml/runs?head_sha="):
            return {"workflow_runs": [{"id": 102, "head_sha": current, "status": "completed", "conclusion": "failure"}]}
        if path == "actions/workflows/production-performance.yml/runs?status=completed&per_page=100":
            return {"workflow_runs": [{"id": 103, "head_sha": measured, "status": "completed", "conclusion": "success"}]}
        if path == "actions/runs/102/jobs?per_page=100":
            return {"jobs": [{"name": name, "status": "completed", "conclusion": "success"}
                             for name in release_check.QUALIFICATION_JOBS]}
        assert path == "actions/runs/103/jobs?per_page=100"
        return {"jobs": [{"name": name, "status": "completed", "conclusion": "success"}
                         for name in release_check.MEASUREMENT_JOBS.values()]}

    monkeypatch.setattr(release_check, "github_read", read)
    proof = release_check.verify_publication_source(repo, "0.9.0")
    assert proof["qualification_runs"]["production-performance.yml"] == 102
    assert all(value["run_id"] == 103 and value["measured_commit"] == measured
               and value["same_commit"] is False for value in proof["measurement_runs"].values())


def test_untracked_source_prevents_publication(tmp_path, monkeypatch):
    repo, current = source_fixture(tmp_path)
    fixture_git(repo, "tag", "v0.9.0")
    (repo / "benchmarks/untracked_comparison.py").write_bytes(b"unqualified source\n")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v0.9.0")
    monkeypatch.setenv("GITHUB_SHA", current)
    monkeypatch.setenv("GITHUB_REPOSITORY", "billighost/Mesh")
    monkeypatch.setattr(release_check, "github_read", lambda _path: pytest.fail("Dirty source must reject first"))
    with pytest.raises(ValueError, match="clean source checkout"):
        release_check.verify_publication_source(repo, "0.9.0")
