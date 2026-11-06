"""Session hooks respect profiles, privacy, output budgets, and failure boundaries."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mesh import hooks


@pytest.mark.parametrize("output_format", ["markdown", "antigravity"])
@pytest.mark.parametrize("budget", [64, 128, 2000])
def test_brief_budget_includes_framing_and_serialization(output_format, budget):
    brief = {"brief_markdown": 'Fact "quoted"\\pointer\n' * 3000}
    output = hooks.render_brief(brief, budget, output_format)
    assert len(output) + 1 <= budget * 4
    if output_format == "antigravity":
        parsed = json.loads(output)
        assert isinstance(parsed["injectSteps"], list)
        context = parsed["injectSteps"][0]["ephemeralMessage"] if parsed["injectSteps"] else ""
    else:
        context = output
    if context:
        assert "UNTRUSTED MESH REFERENCE DATA" in context
        assert "use scoped recall" in context


def test_hook_passes_profile_and_public_fact_limit_without_changing_root(tmp_path, monkeypatch, capsys):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}", encoding="utf-8")
    calls = []
    profile = object()
    class Client:
        def __init__(self, value):
            assert value is profile
        def call(self, name, arguments):
            calls.append((name, arguments))
            return {"brief_markdown": "Current fact [id-1]: use a temporary test home.", "untrusted_reference_data": True}
    def from_file(path):
        assert path == profile_path
        return profile
    monkeypatch.setitem(sys.modules, "mesh.client", SimpleNamespace(ConnectionProfile=SimpleNamespace(from_file=from_file), MeshClient=Client))
    original_root = os.environ["MESH_HOME"]
    result = hooks.main(["brief", "--no-private", "--profile", str(profile_path), "--max-tokens", "2000"])
    assert result == 0 and os.environ["MESH_HOME"] == original_root
    assert calls[0][0] == "mesh_project_brief"
    assert calls[0][1]["max_facts"] == 20 and calls[0][1]["include_private"] is False
    assert 0 < calls[0][1]["max_tokens"] < 2000
    assert "[id-1]" in capsys.readouterr().out


def test_policy_rejection_does_not_replay_or_inject_error_values(monkeypatch, capsys):
    calls = []
    class Client:
        def __init__(self, profile):
            pass
        def call(self, name, arguments):
            calls.append(name)
            return {"status": "rejected", "error": "PRIVATE_PAYLOAD_DO_NOT_INJECT", "brief_markdown": "private"}
    monkeypatch.setitem(sys.modules, "mesh.client", SimpleNamespace(ConnectionProfile=SimpleNamespace(from_file=lambda path: object()), MeshClient=Client))
    result = hooks.main(["brief", "--no-private", "--profile", "profile.json", "--format", "antigravity"])
    captured = capsys.readouterr()
    assert result == 1 and calls == ["mesh_project_brief"]
    assert json.loads(captured.out) == {"injectSteps": []}
    assert "PRIVATE_PAYLOAD" not in captured.err


@pytest.mark.parametrize("data", ["invalid!", "e30=", hooks.encode_profile_pointer({"profile": "x", "token": "bad"})])
def test_invalid_profile_data_is_rejected(data):
    with pytest.raises(ValueError):
        hooks._decode_profile_pointer(data)


def test_discovery_respects_nearest_repository_boundary(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    work = inner / "src"
    work.mkdir(parents=True)
    (inner / ".git").mkdir()
    outside_profile = outer / ".mesh" / "connection.json"
    outside_profile.parent.mkdir()
    outside_profile.write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        hooks.discover_project_profile(work, "claude-code")
    inside_profile = inner / ".mesh" / "connections" / "claude-code.json"
    inside_profile.parent.mkdir(parents=True)
    inside_profile.write_text("{}", encoding="utf-8")
    assert hooks.discover_project_profile(work, "claude-code") == inside_profile


def test_pointer_encodes_shell_characters_as_data():
    pointer = {"profile": '/project $literal/`value`/"quotes"/connection.json'}
    encoded = hooks.encode_profile_pointer(pointer)
    assert all(character.isascii() and (character.isalnum() or character in "-_=+") for character in encoded)
    assert hooks._decode_profile_pointer(encoded) == pointer
