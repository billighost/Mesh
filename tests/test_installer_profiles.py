"""Installer profiles preserve server restrictions and do not interpolate shell input."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mesh import installer
from mesh.hooks import _decode_profile_pointer

try:
    import tomllib
except ImportError:
    import tomli as tomllib


@pytest.fixture
def project(tmp_path, monkeypatch):
    path = tmp_path / "Project $literal `data`"
    path.mkdir()
    monkeypatch.delenv("MESH_NAMESPACE", raising=False)
    monkeypatch.delenv("MESH_AGENT_ID", raising=False)
    for name in ("MESH_LOCKED_NAMESPACE", "MESH_LOCK_NAMESPACE", "MESH_AGENT_SCOPES", "MESH_READ_ONLY", "MESH_ENFORCE_AGENT_OWNERSHIP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(installer.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""))
    return path


@pytest.mark.parametrize("namespace", ["../other", "Acme;touch", "x$(echo)", "x\nnext", "", "a" * 65])
@pytest.mark.parametrize("hooks", [False, True])
def test_invalid_namespace_is_rejected_before_planning(project, namespace, hooks):
    with pytest.raises(ValueError):
        installer.plan_installation(agents=["claude-code"], scope="project", project_dir=project, namespace=namespace, hooks=hooks)
    assert list(project.iterdir()) == []


@pytest.mark.parametrize("scope", ["projects", "", "global"])
def test_invalid_scope_is_rejected(project, scope):
    with pytest.raises(ValueError):
        installer.plan_installation(scope=scope, project_dir=project)
    with pytest.raises(ValueError):
        installer.uninstall_mesh_agent(scope=scope, project_dir=project)


@pytest.mark.parametrize("config_format", ["json", "opencode", "toml"])
def test_upgrading_mcp_preserves_and_cannot_weaken_restrictions(tmp_path, config_format):
    environment = {
        "MESH_NAMESPACE": "Acme.v2", "MESH_LOCKED_NAMESPACE": "Acme.v2",
        "MESH_READ_ONLY": "1", "MESH_ENFORCE_AGENT_OWNERSHIP": "1",
        "MESH_MCP_INCLUDE_PRIVATE": "0", "CUSTOM_SETTING": "kept",
    }
    if config_format == "toml":
        path = tmp_path / "config.toml"
        text = '# Keep my config\nmodel = "test-model"\n[mcp_servers."mesh"] # quoted header\ncommand = "old"\nstartup_timeout_sec = 17\n[mcp_servers.mesh.env]\n'
        text += "".join(f"{json.dumps(key)} = {json.dumps(value)}\n" for key, value in environment.items())
        path.write_text(text, encoding="utf-8")
    else:
        path = tmp_path / ("opencode.json" if config_format == "opencode" else "mcp.json")
        key, env_key = ("mcp", "environment") if config_format == "opencode" else ("mcpServers", "env")
        path.write_text(json.dumps({key: {"mesh": {"command": "old", "timeout": 17, env_key: environment}}, "other": "kept"}), encoding="utf-8")
    original = path.read_bytes()
    changed, _, backup = installer.update_mcp_config_file(
        path, "new-mesh", ["server"], env={"MESH_HOME": str(tmp_path / "data"), "MESH_READ_ONLY": "0", "MESH_MCP_INCLUDE_PRIVATE": "1"},
    )
    assert changed and backup is not None and backup.read_bytes() == original
    if config_format == "toml":
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        entry = parsed["mcp_servers"]["mesh"]
        assert parsed["model"] == "test-model" and entry["startup_timeout_sec"] == 17
        updated_env = entry["env"]
    else:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        entry = parsed[key]["mesh"]
        assert parsed["other"] == "kept" and entry["timeout"] == 17
        updated_env = entry[env_key]
    assert all(updated_env[key] == value for key, value in environment.items())
    assert updated_env["MESH_HOME"] == str(tmp_path / "data")
    assert installer.update_mcp_config_file(path, "new-mesh", ["server"])[0] is False


def test_project_hook_uses_profile_pointer_and_dry_run_changes_nothing(project):
    plan = installer.plan_installation(agents=["claude-code"], scope="project", project_dir=project, namespace="Acme.v2", hooks=True)
    item = plan[0]
    assert not item["mcp"].get("error")
    assert len(item["profiles"]) == 2
    command = item["hooks"]["command"]
    assert command.startswith("mesh-hook brief --no-private --max-tokens 2000 --profile-data ")
    assert str(project) not in command and "Acme.v2" not in command
    encoded = command.split("--profile-data ", 1)[1]
    pointer = _decode_profile_pointer(encoded)
    assert pointer == {"profile": str(project / ".mesh" / "connections" / "claude-code.json")}
    assert item["profiles"][0]["document"]["namespace"] == "Acme.v2"
    assert item["profiles"][0]["document"]["mesh_home"] == str(Path("..") / ".." / ".mesh-home")
    installer.apply_installation(plan, dry_run=True)
    assert list(project.iterdir()) == []


def test_namespace_marker_spelling_is_preserved(project):
    marker = project / ".mesh" / "namespace"
    marker.parent.mkdir()
    marker.write_text("Acme_API.v2\n", encoding="utf-8")
    plan = installer.plan_installation(agents=["cursor"], scope="project", project_dir=project)
    assert plan[0]["mcp"]["env"]["MESH_NAMESPACE"] == "Acme_API.v2"
    assert plan[0]["profiles"][0]["document"]["namespace"] == "Acme_API.v2"
    assert marker.read_text(encoding="utf-8") == "Acme_API.v2\n"


def test_existing_scoped_policy_is_carried_into_profile(project):
    config = project / ".mcp.json"
    env = {
        "MESH_NAMESPACE": "Acme.v2", "MESH_AGENT_ID": "reviewer",
        "MESH_AGENT_SCOPES": json.dumps({"reviewer": {"namespaces": ["Acme.v2"], "mode": "ro"}}),
        "MESH_ENFORCE_AGENT_OWNERSHIP": "1", "MESH_BLOCK_PRIVATE_OVERRIDE": "1",
    }
    config.write_text(json.dumps({"mcpServers": {"mesh": {"command": "old", "env": env}}}), encoding="utf-8")
    item = installer.plan_installation(agents=["claude-code"], scope="project", project_dir=project, hooks=True)[0]
    document = item["profiles"][0]["document"]
    assert document["agent_id"] == "reviewer"
    assert document["policy"] == {"read_only": True, "enforce_agent_ownership": True, "include_private": False, "allow_namespace_override": False}
    assert item["mcp"]["env"]["MESH_AGENT_SCOPES"] == env["MESH_AGENT_SCOPES"]


@pytest.mark.parametrize("lock", ["MESH_LOCKED_NAMESPACE", "MESH_LOCK_NAMESPACE"])
def test_locked_namespace_mismatch_stops_profile_and_hook(project, lock):
    config = project / ".mcp.json"
    original = json.dumps({"mcpServers": {"mesh": {"command": "old", "env": {"MESH_NAMESPACE": "Original", lock: "Original"}}}})
    config.write_text(original, encoding="utf-8")
    item = installer.plan_installation(agents=["claude-code"], scope="project", project_dir=project, namespace="Other", hooks=True)[0]
    assert item["mcp"].get("error") and item["hooks"].get("error")
    assert item["profiles"] == []
    assert config.read_text(encoding="utf-8") == original
    result = installer.apply_installation([item], dry_run=True)[0]
    assert result["mcp"].get("error") and result["hooks"].get("error")


def test_install_is_idempotent_and_uninstall_preserves_profiles(project):
    kwargs = {"agents": ["claude-code"], "scope": "project", "project_dir": project, "namespace": "Acme.v2", "hooks": True}
    result = installer.apply_installation(installer.plan_installation(**kwargs))[0]
    assert all(profile["status"] == "updated" for profile in result["profiles"])
    profile_path = project / ".mesh" / "connections" / "claude-code.json"
    shared_path = project / ".mesh" / "connection.json"
    original = profile_path.read_bytes(), shared_path.read_bytes()
    second = installer.apply_installation(installer.plan_installation(**kwargs))[0]
    assert all(not profile["changed"] for profile in second["profiles"])
    installer.uninstall_mesh_agent(agents=["claude-code"], scope="project", project_dir=project)
    assert not (project / ".mcp.json").exists()
    assert not (project / ".claude" / "settings.json").exists()
    assert (profile_path.read_bytes(), shared_path.read_bytes()) == original


def test_stricter_reviewed_profile_is_preserved_with_backup(project):
    kwargs = {"agents": ["cursor"], "scope": "project", "project_dir": project, "namespace": "Acme.v2"}
    installer.apply_installation(installer.plan_installation(**kwargs))
    path = project / ".mesh" / "connections" / "cursor.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["policy"]["read_only"] = True
    document["policy"]["enforce_agent_ownership"] = True
    path.write_text(json.dumps(document), encoding="utf-8")
    previous = path.read_bytes()
    result = installer.apply_installation(installer.plan_installation(**kwargs))[0]
    profile = result["profiles"][0]
    assert Path(profile["backup"]).read_bytes() == previous
    policy = json.loads(path.read_text(encoding="utf-8"))["policy"]
    assert policy["read_only"] and policy["enforce_agent_ownership"]


@pytest.mark.parametrize("operation", ["mcp", "rules", "hooks"])
def test_linked_config_parent_is_rejected_without_writes(tmp_path, monkeypatch, operation):
    linked = tmp_path / "linked-parent"
    from mesh import skill_installer
    monkeypatch.setattr(skill_installer, "_is_symlink_or_junction", lambda path: path == linked)
    path = linked / "settings.json"
    with pytest.raises(ValueError, match="linked configuration"):
        if operation == "mcp":
            installer.update_mcp_config_file(path, "mesh", ["server"])
        elif operation == "rules":
            installer.update_rules_file(path)
        else:
            installer.update_hooks_file(installer.ADAPTER_MAP["claude-code"], path, "mesh brief")
    assert not linked.exists()


def test_stale_reviewed_plan_cannot_overwrite_new_policy(project):
    kwargs = {"agents": ["claude-code"], "scope": "project", "project_dir": project, "namespace": "Acme.v2", "hooks": True}
    plan = installer.plan_installation(**kwargs)
    config = project / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": {"mesh": {"command": "old", "env": {"MESH_READ_ONLY": "1"}}}}), encoding="utf-8")
    previous = config.read_bytes()
    result = installer.apply_installation(plan)[0]
    assert result["status"] == "stopped (configuration changed)"
    assert config.read_bytes() == previous
    assert not (project / ".mesh").exists() and not (project / ".claude").exists()


def test_backup_keeps_original_crlf_bytes(tmp_path):
    path = tmp_path / "settings.json"
    original = b'{\r\n  "other": "keep"\r\n}\r\n'
    path.write_bytes(original)
    _, _, backup = installer.update_mcp_config_file(path, "mesh", ["server"])
    assert backup is not None and backup.read_bytes() == original


def test_rules_preserve_unrelated_whitespace_and_crlf(tmp_path):
    path = tmp_path / "AGENTS.md"
    original = b'  Project instructions  \r\n\r\n'
    path.write_bytes(original)
    installer.update_rules_file(path)
    assert path.read_bytes().startswith(original)
    installer.remove_rules_file_block(path)
    assert path.read_bytes().startswith(original)


def test_missing_packaged_rules_does_not_install_stale_protocol(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "_RULES_BLOCK_FILE", tmp_path / "missing.md")
    with pytest.raises(FileNotFoundError):
        installer.get_rules_block_content()


@pytest.mark.parametrize("original", [b"Instructions", b"Instructions\n", b"Instructions\n\n", b"Instructions\r\n\r\n", b"  Instructions  \n\n\n"])
def test_rules_upgrade_and_uninstall_restore_exact_user_bytes(tmp_path, original):
    path = tmp_path / "AGENTS.md"
    path.write_bytes(original)
    installer.update_rules_file(path)
    installed = path.read_bytes()
    installer.update_rules_file(path)
    assert path.read_bytes() == installed
    # A user's subsequent instructions outside the block must also survive.
    suffix = b"\nMore user instructions\r\n"
    path.write_bytes(installed + suffix)
    installer.remove_rules_file_block(path)
    assert path.read_bytes() == original + suffix
