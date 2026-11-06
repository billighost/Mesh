"""Installation must preserve unrelated hooks and reject ambiguous profiles."""
from __future__ import annotations

import json

import pytest

from mesh import installer


def profile_data(root):
    return {"version": 1, "namespace": "example", "mesh_home": str(root), "agent_id": "test-agent",
            "policy": {"include_private": False, "read_only": False, "enforce_agent_ownership": False, "allow_namespace_override": False},
            "producer_scope": "local-user"}


@pytest.mark.parametrize("invalid", ["duplicate_policy", "duplicate_version", "bool_version", "missing_policy", "unsafe_endpoint"])
def test_installer_rejects_profiles_the_strict_reader_would_reject_without_writing(tmp_path, invalid):
    root = tmp_path / "data"
    root.mkdir()
    path = tmp_path / "connection.json"
    data = profile_data(root)
    if invalid == "bool_version":
        data["version"] = True
    elif invalid == "missing_policy":
        data.pop("policy")
    elif invalid == "unsafe_endpoint":
        data.update({"endpoint": "https://example.invalid", "token_file": "token-pointer"})
    raw = json.dumps(data)
    if invalid == "duplicate_policy":
        raw = raw.replace('"read_only": false', '"read_only": true, "read_only": false', 1)
    elif invalid == "duplicate_version":
        raw = raw.replace('"version": 1', '"version": 2, "version": 1', 1)
    path.write_text(raw, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        installer._connection_profile_plan(path, "example", root, "test-agent", profile_data(root)["policy"])
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.bak*")) == []


@pytest.mark.parametrize("command", [
    "echo mesh brief metrics", "my-mesh-brief-monitor --once", "python explain_mesh_brief.py",
    "notify --message 'mesh brief ready'", "echo 'mesh-hook brief'",
])
def test_hook_update_and_uninstall_preserve_commands_that_only_mention_mesh(tmp_path, command):
    path = tmp_path / "settings.json"
    original = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command, "timeout": 9}]}]}}
    path.write_text(json.dumps(original), encoding="utf-8")
    adapter = installer.ClaudeCodeAdapter()
    new_command = "mesh-hook brief --no-private --profile-data FAKE_POINTER"
    changed, _, _ = installer.update_hooks_file(adapter, path, new_command)
    assert changed is True
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["hooks"]["SessionStart"][0] == original["hooks"]["SessionStart"][0]
    assert len(after["hooks"]["SessionStart"]) == 2
    installer.remove_hooks_file_entry(adapter, path)
    assert json.loads(path.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("command", [
    "mesh brief --no-private", "mesh-hook brief --no-private", '"/opt/Mesh Tools/mesh-hook" brief --no-private',
    '"C:\\Mesh Tools\\mesh-hook.exe" brief --no-private', "python -m mesh.hooks brief --no-private",
])
def test_installer_still_upgrades_and_removes_its_legacy_or_quoted_hooks(tmp_path, command):
    path = tmp_path / "settings.json"
    original = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    path.write_text(json.dumps(original), encoding="utf-8")
    adapter = installer.ClaudeCodeAdapter()
    replacement = "mesh-hook brief --no-private --profile-data FAKE_POINTER"
    installer.update_hooks_file(adapter, path, replacement)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert len(after["hooks"]["SessionStart"]) == 1
    assert after["hooks"]["SessionStart"][0]["hooks"][0]["command"] == replacement
    installer.remove_hooks_file_entry(adapter, path)
    assert not path.exists()
