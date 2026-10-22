"""
tests/test_doctor_scope_precedence.py — `mesh doctor` project-vs-user MCP scope
reporting (optional item 2 of the Run C re-test).

Clients that support project scope load the project-scope MCP config for the
current directory ahead of the user-scope one. Doctor now reports that
precedence instead of leaving a shadowed user config invisible. The checks are
strictly read-only: no config file is created, modified or removed.

Everything runs against a temp HOME/MESH_HOME and a temp project directory; the
daemon probe is pointed at a free localhost port so a real Mesh daemon on the
machine is never contacted.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

import mesh.cli as cli


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def doctor_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    mesh_home = home / ".mesh"
    project = tmp_path / "project"
    mesh_home.mkdir(parents=True)
    project.mkdir(parents=True)

    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_NO_MODEL_DOWNLOAD", "1")
    monkeypatch.setenv("MESH_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv("MESH_HTTP_PORT", str(_free_port()))
    monkeypatch.chdir(project)
    return home, project


def _write_mcp(path: Path, command: str | None = sys.executable, with_mesh: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {"mcpServers": {}}
    if with_mesh:
        payload["mcpServers"]["mesh"] = {"command": command, "args": []}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_project_config_precedence_is_reported(doctor_env, capsys):
    home, project = doctor_env
    _write_mcp(home / ".claude" / "mcp.json")
    _write_mcp(project / ".claude" / "mcp.json")

    cli.doctor_cmd()
    out = capsys.readouterr().out

    assert "Claude Code scope" in out
    assert "project takes precedence over user" in out


def test_project_only_config_is_reported_as_active(doctor_env, capsys):
    _home, project = doctor_env
    _write_mcp(project / ".claude" / "mcp.json")

    cli.doctor_cmd()
    out = capsys.readouterr().out

    assert "project config active" in out
    assert "takes precedence over user" not in out


def test_user_only_config_does_not_claim_project_precedence(doctor_env, capsys):
    home, _project = doctor_env
    _write_mcp(home / ".claude" / "mcp.json")

    cli.doctor_cmd()
    out = capsys.readouterr().out

    assert "takes precedence over user" not in out
    assert "project config active" not in out
    # The existing user-scope registration row still appears.
    assert "Claude Code MCP" in out


def test_project_config_without_mesh_entry_is_ignored(doctor_env, capsys):
    home, project = doctor_env
    _write_mcp(home / ".claude" / "mcp.json")
    _write_mcp(project / ".claude" / "mcp.json", with_mesh=False)

    cli.doctor_cmd()
    out = capsys.readouterr().out

    assert "takes precedence over user" not in out
    assert "project config active" not in out


def test_unreadable_project_config_is_ignored(doctor_env, capsys):
    home, project = doctor_env
    _write_mcp(home / ".claude" / "mcp.json")
    broken = project / ".claude" / "mcp.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not valid json", encoding="utf-8")

    cli.doctor_cmd()
    out = capsys.readouterr().out

    assert "takes precedence over user" not in out
    assert "project config active" not in out


def test_doctor_does_not_modify_scope_configs(doctor_env, capsys):
    home, project = doctor_env
    user_cfg = home / ".claude" / "mcp.json"
    project_cfg = project / ".claude" / "mcp.json"
    _write_mcp(user_cfg)
    _write_mcp(project_cfg)
    before = {p: p.read_bytes() for p in (user_cfg, project_cfg)}

    cli.doctor_cmd()
    capsys.readouterr()

    assert {p: p.read_bytes() for p in (user_cfg, project_cfg)} == before
