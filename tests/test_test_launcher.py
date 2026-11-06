"""The fast launcher keeps isolation and coverage selection intact."""
from __future__ import annotations

import json

import pytest

from scripts import run_tests


def test_launcher_isolates_before_plugins_without_changing_embedding_or_property_profile(tmp_path):
    inherited = {"HOME": "/real-home", "MESH_HOME": "/real-mesh", "PYTEST_PLUGINS": "unsafe_plugin", "PYTEST_ADDOPTS": "-n auto", "OPENAI_API_KEY": "fake", "HYPOTHESIS_PROFILE": "ci", "MESH_EMBEDDING_BACKEND": "onnx"}
    environment = run_tests.isolated_environment(tmp_path, inherited)
    assert environment["HOME"] == str(tmp_path / "home") and environment["MESH_HOME"] == str(tmp_path / "mesh")
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1" and "PYTEST_PLUGINS" not in environment
    assert "PYTEST_ADDOPTS" not in environment
    assert "OPENAI_API_KEY" not in environment and environment["MESH_AUTO_APPROVE"] == "0"
    assert environment["HYPOTHESIS_PROFILE"] == "ci" and environment["MESH_EMBEDDING_BACKEND"] == "onnx"
    assert inherited["HOME"] == "/real-home" and inherited["OPENAI_API_KEY"] == "fake"


def test_parallel_launcher_retains_every_selection_and_coverage_argument():
    arguments = ["tests", "-q", "--cov=mesh", "--cov-report=xml", "--cov-fail-under=60", "--hypothesis-profile=ci"]
    command = run_tests.pytest_command(2, arguments)
    assert command[-len(arguments):] == arguments
    assert command[command.index("-n") + 1] == "2"
    assert command[command.index("--dist") + 1] == "loadfile"
    assert "-p" in command and "pytest_cov.plugin" in command and "_hypothesis_pytestplugin" in command


@pytest.mark.parametrize("workers", [0, 5, 16])
def test_unbounded_worker_count_is_rejected(workers):
    with pytest.raises(ValueError):
        run_tests.pytest_command(workers, [])


def test_real_launcher_blocks_inherited_plugin_and_reports_exit(tmp_path, monkeypatch):
    plugin = tmp_path / "unsafe_plugin.py"
    plugin.write_text("raise RuntimeError('should never be loaded')\n", encoding="utf-8")
    test = tmp_path / "test_isolation_probe.py"
    test.write_text(
        "import os\nfrom pathlib import Path\n"
        "def test_isolation():\n"
        "    assert Path(os.environ['MESH_HOME']).name == 'mesh'\n"
        "    assert Path(os.environ['HOME']).name == 'home'\n"
        "    assert os.environ['MESH_NO_MODEL_DOWNLOAD'] == '1'\n"
        "    assert os.environ['MESH_AUTO_APPROVE'] == '0'\n"
        "    assert 'OPENAI_API_KEY' not in os.environ\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTEST_PLUGINS", "unsafe_plugin")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    report = tmp_path / "report.json"
    assert run_tests.run_tests(1, [str(test), "-q", "-o", "addopts="], report) == 0
    metrics = json.loads(report.read_text(encoding="utf-8"))
    assert metrics["exit_code"] == 0 and metrics["workers"] == 1 and metrics["wall_seconds"] > 0
    assert isinstance(metrics["resource_sampling_available"], bool)


@pytest.mark.parametrize("argument", ["-n16", "-n", "--numprocesses=auto", "--dist=each"])
def test_pytest_flags_cannot_override_the_bounded_pool(argument):
    with pytest.raises(ValueError):
        run_tests.pytest_command(2, [argument])
