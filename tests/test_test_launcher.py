"""The fast launcher keeps isolation and coverage selection intact."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import textwrap

import pytest

from scripts import run_tests


def test_launcher_isolates_before_plugins_without_changing_embedding_or_property_profile(tmp_path):
    inherited = {
        "HOME": "/real-home", "MESH_HOME": "/real-mesh", "TMPDIR": "/old-temp-alias",
        "TMP": "/old-temp-alias", "TEMP": "/old-temp-alias",
        "PYTEST_PLUGINS": "unsafe_plugin", "PYTEST_ADDOPTS": "-n auto", "OPENAI_API_KEY": "fake",
        "HYPOTHESIS_PROFILE": "ci", "MESH_EMBEDDING_BACKEND": "onnx",
    }
    environment = run_tests.isolated_environment(tmp_path, inherited)
    root = tmp_path.resolve()
    assert environment["HOME"] == str(root / "home") and environment["MESH_HOME"] == str(root / "mesh")
    assert environment["MESH_TEST_RUN_ROOT"] == str(root)
    assert all(environment[name] == str(root / "tmp") for name in ("TMPDIR", "TMP", "TEMP"))
    assert (root / "tmp").is_dir()
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1" and "PYTEST_PLUGINS" not in environment
    assert "PYTEST_ADDOPTS" not in environment
    assert "OPENAI_API_KEY" not in environment and environment["MESH_AUTO_APPROVE"] == "0"
    assert environment["HYPOTHESIS_PROFILE"] == "ci" and environment["MESH_EMBEDDING_BACKEND"] == "onnx"
    assert inherited["HOME"] == "/real-home" and inherited["OPENAI_API_KEY"] == "fake"


def test_launcher_discards_inherited_mesh_configuration_and_credentials(tmp_path):
    discarded = {
        "MESH_NAMESPACE": "external-project", "MESH_AGENT_ID": "external-agent",
        "MESH_READ_ONLY": "1", "MESH_LOCKED_NAMESPACE": "external-project",
        "MESH_AGENT_SCOPES": "{}", "MESH_CONNECTION_PROFILE": "/external-profile.json",
        "MESH_TOKEN_FILE": "/external-token", "MESH_AUTH_TOKEN": "FAKE_EXTERNAL_TOKEN",
        "MESH_ENCRYPT_AT_REST": "1", "MESH_ENCRYPTION_KEY": "FAKE_EXTERNAL_KEY",
        "MESH_PASSPHRASE": "FAKE_EXTERNAL_PASSPHRASE", "MESH_AI_PROVIDER": "cloud",
        "MESH_AI_API_KEY": "FAKE_PROVIDER_KEY", "MESH_LOG_QUERY_TEXT": "1",
        "ANTHROPIC_AUTH_TOKEN": "FAKE_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN": "FAKE_OAUTH_TOKEN",
    }
    requested = {"MESH_FORCE_HASH_EMBED": "1", "MESH_BG_ONNX": "0", "MESH_TEST_WORKERS": "1"}
    environment = run_tests.isolated_environment(tmp_path, {**discarded, **requested})
    assert not discarded.keys() & environment.keys()
    assert all(environment[name] == value for name, value in requested.items())


def test_fresh_owned_temp_root_uses_canonical_paths_under_an_os_alias(tmp_path):
    canonical_parent = tmp_path / "canonical-parent"
    canonical_parent.mkdir()
    alias = tmp_path / "temporary-parent-alias"
    try:
        alias.symlink_to(canonical_parent, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this platform.")
    fresh_root = Path(tempfile.mkdtemp(prefix="fresh-owned-", dir=alias))
    environment = run_tests.isolated_environment(fresh_root, {})
    canonical_root = fresh_root.resolve()
    assert environment["MESH_TEST_RUN_ROOT"] == str(canonical_root)
    for name in ("HOME", "MESH_HOME", "TMPDIR", "TMP", "TEMP"):
        path = Path(environment[name])
        assert path.is_relative_to(canonical_root) and path == path.resolve()


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
    (tmp_path / "conftest.py").write_text(
        (run_tests.REPO_ROOT / "tests/conftest.py").read_text(encoding="utf-8"), encoding="utf-8",
    )
    test.write_text(
        textwrap.dedent("""
            import json
            import os
            from pathlib import Path
            import subprocess
            import sys
            import tempfile

            LEAKED_KEYS = ('MESH_PASSPHRASE', 'MESH_ENCRYPTION_KEY', 'MESH_READ_ONLY', 'MESH_NAMESPACE')
            RECORD = Path(__file__).with_name('case-paths.json')

            def test_isolation_and_direct_mutation():
                assert Path(os.environ['MESH_HOME']).name == 'mesh'
                assert Path(os.environ['HOME']).name == 'home'
                assert os.environ['MESH_NO_MODEL_DOWNLOAD'] == '1'
                assert os.environ['MESH_AUTO_APPROVE'] == '0'
                assert 'OPENAI_API_KEY' not in os.environ
                assert all(name not in os.environ for name in LEAKED_KEYS)
                previous = {name: os.environ[name] for name in ('HOME', 'MESH_HOME', 'TMPDIR')}
                RECORD.write_text(json.dumps(previous), encoding='utf-8')
                (Path(previous['HOME']) / '.claude.json').write_text('{}', encoding='utf-8')
                (Path(previous['MESH_HOME']) / '.encryption_enabled').write_text('1', encoding='utf-8')
                for name in LEAKED_KEYS:
                    os.environ[name] = 'FAKE_DIRECT_TEST_MUTATION'
                os.environ['MESH_FORCE_HASH_EMBED'] = '0'
                os.environ['HYPOTHESIS_PROFILE'] = 'ci'
                os.environ['TMPDIR'] = previous['HOME']
                tempfile.tempdir = previous['HOME']

            def test_next_case_has_fresh_configuration_and_nested_temporary_roots():
                previous = json.loads(RECORD.read_text(encoding='utf-8'))
                assert all(os.environ[name] != previous[name] for name in previous)
                assert not (Path(os.environ['HOME']) / '.claude.json').exists()
                assert not (Path(os.environ['MESH_HOME']) / '.encryption_enabled').exists()
                assert all(name not in os.environ for name in LEAKED_KEYS)
                assert os.environ['MESH_FORCE_HASH_EMBED'] == '1'
                assert os.environ['MESH_BG_ONNX'] == '0'
                assert os.environ['HYPOTHESIS_PROFILE'] == 'default'
                temporary = Path(os.environ['TMPDIR'])
                assert all(os.environ[name] == str(temporary) for name in ('TMPDIR', 'TMP', 'TEMP'))
                assert temporary == temporary.resolve()
                assert tempfile.gettempdir() == str(temporary)
                assert Path(tempfile.mkdtemp()).parent == temporary
                child = subprocess.check_output(
                    [sys.executable, '-c', 'import tempfile; print(tempfile.mkdtemp())'], text=True,
                ).strip()
                assert Path(child).parent == temporary and Path(child) == Path(child).resolve()
        """),
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTEST_PLUGINS", "unsafe_plugin")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setenv("MESH_PASSPHRASE", "FAKE_INHERITED_PASSPHRASE")
    monkeypatch.setenv("MESH_READ_ONLY", "1")
    monkeypatch.setenv("MESH_NAMESPACE", "external-project")
    monkeypatch.setenv("MESH_FORCE_HASH_EMBED", "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    monkeypatch.setenv("HYPOTHESIS_PROFILE", "default")
    report = tmp_path / "report.json"
    assert run_tests.run_tests(1, [str(test), f"--confcutdir={tmp_path}", "-q", "-o", "addopts="], report) == 0
    metrics = json.loads(report.read_text(encoding="utf-8"))
    assert metrics["exit_code"] == 0 and metrics["workers"] == 1 and metrics["wall_seconds"] > 0
    assert isinstance(metrics["resource_sampling_available"], bool)


@pytest.mark.parametrize("argument", ["-n16", "-n", "--numprocesses=auto", "--dist=each"])
def test_pytest_flags_cannot_override_the_bounded_pool(argument):
    with pytest.raises(ValueError):
        run_tests.pytest_command(2, [argument])
