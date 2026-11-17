"""Resolve local project identity without conflating linked worktrees or parents."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mesh.namespace_resolution import resolve_namespace


@pytest.fixture(autouse=True)
def clear_namespace(monkeypatch):
    for name in ("MESH_NAMESPACE", "MESH_LOCKED_NAMESPACE", "MESH_LOCK_NAMESPACE"):
        monkeypatch.delenv(name, raising=False)


def origin(directory: Path, name: str = "acme-api") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config").write_text(
        f'[remote "origin"]\nurl = git@example.invalid:company/{name}.git\n', encoding="utf-8"
    )


def test_normal_repository_and_nested_directory(tmp_path):
    project = tmp_path / "checkout"
    origin(project / ".git")
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    assert resolve_namespace(workspace=nested) == "acme-api"


@pytest.mark.parametrize("relative_gitdir", [False, True])
@pytest.mark.parametrize("relative_common", [False, True])
def test_linked_worktree_uses_common_origin(tmp_path, relative_gitdir, relative_common):
    common = tmp_path / "repository" / ".git"
    origin(common)
    metadata = common / "worktrees" / "branch"
    metadata.mkdir(parents=True)
    checkout = tmp_path / "feature-worktree"
    checkout.mkdir()
    gitdir = os.path.relpath(metadata, checkout) if relative_gitdir else str(metadata)
    common_value = os.path.relpath(common, metadata) if relative_common else str(common)
    (checkout / ".git").write_text("gitdir: " + gitdir + "\n", encoding="utf-8")
    (metadata / "commondir").write_text(common_value + "\n", encoding="utf-8")
    assert resolve_namespace(workspace=checkout) == "acme-api"


def test_submodule_gitfile_uses_its_own_origin(tmp_path):
    project = tmp_path / "submodule"
    project.mkdir()
    metadata = tmp_path / "metadata"
    origin(metadata, "submodule-library")
    (project / ".git").write_text("gitdir: ../metadata\n", encoding="utf-8")
    assert resolve_namespace(workspace=project) == "submodule-library"


def test_explicit_environment_and_marker_take_precedence(tmp_path, monkeypatch):
    origin(tmp_path / ".git")
    marker = tmp_path / ".mesh" / "namespace"
    marker.parent.mkdir()
    marker.write_text("Project.V2\n", encoding="utf-8")
    assert resolve_namespace(workspace=tmp_path) == "Project.V2"
    monkeypatch.setenv("MESH_NAMESPACE", "configured")
    assert resolve_namespace(workspace=tmp_path) == "configured"
    assert resolve_namespace("explicit", workspace=tmp_path) == "explicit"


def test_nearest_git_boundary_prevents_outer_namespace(tmp_path):
    origin(tmp_path / ".git", "outer-project")
    marker = tmp_path / ".mesh" / "namespace"
    marker.parent.mkdir()
    marker.write_text("outer-marker", encoding="utf-8")
    inner = tmp_path / "inner-project"
    (inner / ".git").mkdir(parents=True)
    assert resolve_namespace(workspace=inner) == "inner-project"


@pytest.mark.parametrize("value", ["", "not a gitdir", "gitdir: \n", "gitdir: ../one\n../two", "gitdir: " + "x" * 4096])
def test_malformed_gitfiles_fail_without_guessing(tmp_path, value):
    (tmp_path / ".git").write_text(value, encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_namespace(workspace=tmp_path)


def test_oversized_config_is_rejected(tmp_path):
    origin(tmp_path / ".git")
    (tmp_path / ".git" / "config").write_text("x" * 1048577, encoding="utf-8")
    with pytest.raises(ValueError, match="too large"):
        resolve_namespace(workspace=tmp_path)


def test_linked_config_is_rejected(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    config = tmp_path / "config"
    config.write_text('[remote "origin"]\nurl = https://example.invalid/other.git\n')
    try:
        (git_dir / "config").symlink_to(config)
    except OSError:
        pytest.skip("This account cannot create symlinks")
    with pytest.raises(ValueError):
        resolve_namespace(workspace=tmp_path)
