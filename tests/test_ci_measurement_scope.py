"""A measurement shortcut must not conceal runtime changes in a multi-commit push."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from scripts import check_ci_measurement_scope as scope


@pytest.mark.parametrize("message,expected", [
    ("fix retrieval", "full"),
    ("fix retrieval\n\n[retrieval-only]", "retrieval"),
    ("fix retrieval [RETRIEVAL-ONLY]", "retrieval"),
    ("publish evidence\n\n[qualification-only]", "qualification"),
])
def test_explicit_scope_selection(message, expected):
    assert scope.requested_scope(message) == expected


def test_conflicting_scopes_are_rejected():
    with pytest.raises(ValueError, match="Conflicting"):
        scope.requested_scope("[retrieval-only]\n[qualification-only]")


@pytest.mark.parametrize("path", [
    "mesh/store.py", "mesh/memory.py", "mesh/skills/mesh-context-layer/SKILL.md",
    "pyproject.toml", "benchmarks/production_bench.py", "scripts/run_tests.py",
    "tests/test_production_storage.py", "AGENTS.md",
])
@pytest.mark.parametrize("mode", ["retrieval", "qualification"])
def test_measurement_scope_rejects_runtime_and_measured_harness_changes(path, mode):
    with pytest.raises(ValueError, match="unchanged runtime"):
        scope.validate_changed_paths(mode, ["docs/CI_VALIDATION.md", path])


def test_documentation_scope_rejects_retrieval_changes():
    with pytest.raises(ValueError, match="unchanged runtime"):
        scope.validate_changed_paths("qualification", ["benchmarks/production_retrieval.py"])
    scope.validate_changed_paths("retrieval", ["benchmarks/production_retrieval.py", "docs/CI_VALIDATION.md"])
    scope.validate_changed_paths("qualification", ["benchmarks/results/example.json", "docs/CI_VALIDATION.md"])


@pytest.mark.parametrize("path", ["../README.md", "docs/../README.md", "/docs/example.md", "docs\\example.md", "docs//example.md"])
def test_unsafe_changed_paths_are_rejected(path):
    with pytest.raises(ValueError, match="Invalid changed path"):
        scope.validate_changed_paths("retrieval", [path])


def test_diff_uses_entire_push_before_accepting_last_commit_scope(tmp_path, monkeypatch):
    before, head = "a" * 40, "b" * 40
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": before, "after": head, "head_commit": {"message": "fix [retrieval-only]"}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_SHA", head)
    calls = []

    def git(*arguments):
        calls.append(arguments)
        return head + "\n" if arguments[0] == "rev-parse" else "mesh/store.py\0benchmarks/production_retrieval.py\0"

    monkeypatch.setattr(scope, "git", git)
    with pytest.raises(ValueError, match="unchanged runtime"):
        scope.main()
    assert calls[-1] == ("diff", "--no-renames", "--name-only", "-z", before, head)


def test_real_runtime_rename_cannot_hide_in_an_allowed_fixture_path(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("Git is required for a real rename regression")
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(scope, "ROOT", tmp_path)
    configuration = ["git", "-c", "user.name=Mesh Tests", "-c", "user.email=mesh@example.invalid",
                     "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", f"core.hooksPath={tmp_path / 'no-hooks'}"]

    def git(*arguments):
        return subprocess.run([*configuration, *arguments], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()

    template = tmp_path / "empty-template"
    template.mkdir()
    git("init", "--template=" + str(template))
    source = tmp_path / "mesh" / "example.py"
    source.parent.mkdir()
    source.write_text("# synthetic runtime source\n", encoding="utf-8")
    git("add", "mesh/example.py")
    git("commit", "-m", "initial synthetic source")
    before = git("rev-parse", "HEAD")
    destination = tmp_path / "benchmarks" / "retrieval_fixtures" / "example.py"
    destination.parent.mkdir(parents=True)
    assert list(source.parent.iterdir()) == [source]
    assert source.resolve().is_relative_to(tmp_path.resolve()) and destination.parent.resolve().is_relative_to(tmp_path.resolve())
    source.rename(destination)
    git("add", "-A")
    git("commit", "-m", "move synthetic source [retrieval-only]")
    head = git("rev-parse", "HEAD")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": before, "after": head, "head_commit": {"message": "[retrieval-only]"}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_SHA", head)
    with pytest.raises(ValueError, match="unchanged runtime"):
        scope.main()


def test_manual_measurements_use_workflow_inputs(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.setattr(scope, "git", lambda *_args: pytest.fail("Manual inputs must not read a push base"))
    assert scope.main() == 0
    assert json.loads(capsys.readouterr().out)["measurement_scope"] == "manual_inputs"
