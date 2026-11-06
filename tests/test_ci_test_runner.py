"""CI keeps failure evidence while bounding the retained log and isolating pytest."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from scripts import run_ci_tests


@pytest.mark.parametrize("exit_code", [0, 1])
def test_ci_runner_preserves_early_failure_and_emits_bounded_summary(tmp_path, monkeypatch, capsys, exit_code):
    summary = tmp_path / "summary.md"
    monkeypatch.setattr(run_ci_tests, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("MESH_TEST_WORKERS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "FAKE_PROVIDER_KEY")
    monkeypatch.setenv("HYPOTHESIS_PROFILE", "ci")
    observed = {}

    class Process:
        pid = 1
        stdout = io.StringIO("FAILED tests/test_probe.py::test_failure\n" + "".join(f"output {i}\n" for i in range(350)))

        def wait(self):
            return exit_code

    def launch(command, **kwargs):
        observed.update(command=command, **kwargs)
        return Process()

    class Sampler:
        def __init__(self, pid):
            pass

        def start(self):
            pass

        def stop(self):
            return {"resource_sampling_available": False}

    monkeypatch.setattr(run_ci_tests.subprocess, "Popen", launch)
    monkeypatch.setattr(run_ci_tests, "_ResourceSampler", Sampler)
    with pytest.raises(SystemExit) as result:
        run_ci_tests.main()
    assert result.value.code == exit_code
    assert "::error title=Test Failure::tests/test_probe.py::test_failure" in capsys.readouterr().out
    assert "--cov-fail-under=60" in observed["command"]
    assert "--hypothesis-profile=ci" in observed["command"]
    assert "-n" not in observed["command"]
    assert observed["env"]["MESH_AUTO_APPROVE"] == "0"
    assert observed["env"]["MESH_NO_MODEL_DOWNLOAD"] == "1"
    assert "OPENAI_API_KEY" not in observed["env"]
    assert Path(observed["env"]["MESH_HOME"]).name == "mesh"
    report = json.loads((tmp_path / ".work/ci/test-run.json").read_text(encoding="utf-8"))
    assert report["exit_code"] == exit_code and report["workers"] == 1
    if exit_code:
        text = summary.read_text(encoding="utf-8")
        assert "test_failure" in text
        assert "output 349" in text and "output 199" not in text


def test_ci_runner_stops_its_process_on_stream_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(run_ci_tests, "REPO_ROOT", tmp_path)
    stopped = []

    class BrokenStream:
        def __iter__(self):
            raise OSError("fixture stream failure")

        def close(self):
            pass

    class Process:
        pid = 1
        stdout = BrokenStream()

    class Sampler:
        def __init__(self, pid):
            pass

        def start(self):
            pass

        def stop(self):
            return {}

    process = Process()
    monkeypatch.setattr(run_ci_tests.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(run_ci_tests, "_ResourceSampler", Sampler)
    monkeypatch.setattr(run_ci_tests, "_stop_process_tree", stopped.append)
    with pytest.raises(OSError, match="fixture stream failure"):
        run_ci_tests.main()
    assert stopped == [process]
