"""
tests/test_installer.py — Comprehensive tests for mesh install, mesh uninstall,
and agent adapters across isolated temporary environments.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mesh.installer import (
    ADAPTER_MAP,
    SUPPORTED_AGENTS,
    apply_installation,
    get_rules_block_content,
    plan_installation,
    remove_hooks_file_entry,
    remove_mcp_config_entry,
    remove_rules_file_block,
    resolve_mesh_mcp_command,
    uninstall_mesh_agent,
    update_hooks_file,
    update_mcp_config_file,
    update_rules_file,
)
from mesh.version import VERSION


@pytest.fixture
def isolated_env(monkeypatch):
    """
    Ensure all tests run strictly against an isolated temporary environment.
    Sets HOME, USERPROFILE, APPDATA, XDG_CONFIG_HOME, and MESH_HOME to temp.
    """
    temp_dir = tempfile.mkdtemp(prefix="mesh_test_installer_")
    t_path = Path(temp_dir)
    home_dir = t_path / "home"
    appdata_dir = t_path / "appdata"
    xdg_dir = t_path / "xdg"
    mesh_home = t_path / "mesh_home"

    home_dir.mkdir(parents=True)
    appdata_dir.mkdir(parents=True)
    xdg_dir.mkdir(parents=True)
    mesh_home.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.setenv("APPDATA", str(appdata_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_dir))
    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")

    # Patch Path.home() so it always resolves to home_dir
    monkeypatch.setattr(Path, "home", lambda: home_dir)

    yield {
        "root": t_path,
        "home": home_dir,
        "appdata": appdata_dir,
        "xdg": xdg_dir,
        "mesh_home": mesh_home,
    }

    shutil.rmtree(temp_dir, ignore_errors=True)


def test_resolve_mcp_command():
    """Verify that resolve_mesh_mcp_command produces a valid executable and arguments (Issue A-002)."""
    cmd, args = resolve_mesh_mcp_command()
    assert cmd is not None
    assert len(cmd) > 0
    assert isinstance(args, list)
    assert args != ["mcp"], "resolve_mesh_mcp_command must not generate invalid ['mcp'] argument"
    if cmd.endswith("mesh") or cmd.endswith("mesh.exe"):
        assert args == ["server"], f"Expected ['server'] for mesh executable, got {args}"
    else:
        assert args == ["-m", "mesh.mcp_server"], f"Expected ['-m', 'mesh.mcp_server'] for python fallback, got {args}"


def test_cli_mesh_mcp_subcommand_alias(isolated_env):
    """Verify 'mesh mcp' is supported as a transparent alias for 'mesh server' (Issue A-002)."""
    from mesh.cli import _get_command_mapping
    mapping = _get_command_mapping()
    assert "mcp" in mapping, "'mcp' must be registered in CLI command handlers as alias to server"
    assert mapping["mcp"] is mapping["server"]


def test_rules_block_content():
    """Verify rules block content is <= 12 lines and wrapped in version markers."""
    block = get_rules_block_content(VERSION)
    assert f"<!-- mesh:begin v={VERSION} -->" in block
    assert "<!-- mesh:end -->" in block
    assert "mesh_project_brief" in block
    assert "mesh_task_complete" in block
    assert len(block.strip().splitlines()) <= 15


def test_update_mcp_config_preserves_other_servers(isolated_env):
    """Verify adding mesh server preserves existing servers and top-level keys."""
    cfg_file = isolated_env["home"] / "test_mcp.json"
    initial_data = {
        "version": "1.0",
        "mcpServers": {
            "sqlite": {"command": "sqlite-mcp", "args": ["--db", "test.db"]},
            "github": {"command": "github-mcp", "args": []},
        },
    }
    cfg_file.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")

    changed, diff, bak = update_mcp_config_file(
        cfg_file,
        command="/usr/bin/mesh",
        args=["mcp"],
        env={"MESH_NAMESPACE": "test_proj"},
    )
    assert changed is True
    assert bak is not None
    assert bak.exists()

    # Read back and verify
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["version"] == "1.0"
    assert "sqlite" in data["mcpServers"]
    assert "github" in data["mcpServers"]
    assert "mesh" in data["mcpServers"]
    assert data["mcpServers"]["mesh"]["command"] == "/usr/bin/mesh"
    assert data["mcpServers"]["mesh"]["env"]["MESH_NAMESPACE"] == "test_proj"

    # Idempotent re-run
    changed2, diff2, bak2 = update_mcp_config_file(
        cfg_file,
        command="/usr/bin/mesh",
        args=["mcp"],
        env={"MESH_NAMESPACE": "test_proj"},
    )
    assert changed2 is False
    assert bak2 is None


def test_remove_mcp_config_entry(isolated_env):
    """Verify removing mesh entry preserves other servers."""
    cfg_file = isolated_env["home"] / "test_mcp.json"
    initial_data = {
        "mcpServers": {
            "other": {"command": "other-cmd"},
            "mesh": {"command": "mesh", "args": ["mcp"]},
        }
    }
    cfg_file.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")

    changed, diff, bak = remove_mcp_config_entry(cfg_file)
    assert changed is True
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert "other" in data["mcpServers"]
    assert "mesh" not in data["mcpServers"]


def test_rules_block_insertion_and_idempotence(isolated_env):
    """Verify rules block is appended, preserves existing text, and updates idempotently."""
    rules_file = isolated_env["home"] / "CLAUDE.md"
    initial_text = "# Project Guidelines\n- Rule 1: Always write tests.\n"
    rules_file.write_text(initial_text, encoding="utf-8")

    # First insertion
    changed, diff, bak = update_rules_file(rules_file)
    assert changed is True
    assert bak is not None
    assert bak.exists()

    content = rules_file.read_text(encoding="utf-8")
    assert "# Project Guidelines" in content
    assert "Rule 1: Always write tests." in content
    assert f"<!-- mesh:begin v={VERSION} -->" in content
    assert "<!-- mesh:end -->" in content

    # Second insertion (idempotent)
    changed2, diff2, bak2 = update_rules_file(rules_file)
    assert changed2 is False

    # Remove rules block
    changed3, diff3, bak3 = remove_rules_file_block(rules_file)
    assert changed3 is True
    cleaned_content = rules_file.read_text(encoding="utf-8")
    assert cleaned_content.strip() == initial_text.strip()
    assert "<!-- mesh:begin" not in cleaned_content


def test_dry_run_changes_nothing(isolated_env):
    """Verify dry-run returns diffs and plan without writing any files to disk."""
    proj_dir = isolated_env["home"] / "my_project"
    proj_dir.mkdir()

    plan = plan_installation(
        agents=["claude-code", "cursor"],
        scope="project",
        project_dir=proj_dir,
    )
    assert len(plan) == 2

    # Execute with dry_run=True
    results = apply_installation(plan, dry_run=True)
    assert len(results) == 2

    # Verify no files were created
    assert not (proj_dir / ".claude" / "mcp.json").exists()
    assert not (proj_dir / "CLAUDE.md").exists()
    assert not (proj_dir / ".cursor" / "mcp.json").exists()
    assert not (proj_dir / ".cursorrules").exists()


def test_all_agent_adapters_user_scope(isolated_env):
    """Test full install and uninstall cycle for all supported agents in user scope."""
    plan = plan_installation(scope="user")
    assert len(plan) == len(SUPPORTED_AGENTS)

    # Apply install
    results = apply_installation(plan, dry_run=False)
    assert len(results) == len(SUPPORTED_AGENTS)

    # Verify each agent's config or skill or rules was created
    for r in results:
        agent_id = r["agent"]
        adapter = ADAPTER_MAP[agent_id]
        mcp_path = adapter.get_mcp_config_path("user")
        if mcp_path and r["mcp"]:
            assert mcp_path.exists(), f"MCP config for {agent_id} not created at {mcp_path}"
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
            assert "mesh" in data["mcpServers"]

        rules_path = adapter.get_rules_path("user")
        if rules_path and r["rules"]:
            assert rules_path.exists(), f"Rules file for {agent_id} not created at {rules_path}"
            assert "<!-- mesh:begin" in rules_path.read_text(encoding="utf-8")

        skill_root = adapter.get_skill_root_path("user")
        if skill_root and r["skill"]:
            skill_dir = skill_root / "mesh-context-layer"
            assert skill_dir.is_dir(), f"Skill directory not created for {agent_id} at {skill_dir}"
            assert (skill_dir / "SKILL.md").is_file()

    # Re-run install to verify idempotence
    re_results = apply_installation(plan, dry_run=False)
    for r in re_results:
        if r.get("mcp") and not r["mcp"].get("error"):
            assert r["mcp"]["changed"] is False
        if r.get("rules") and not r["rules"].get("error"):
            assert r["rules"]["changed"] is False

    # Uninstall all
    un_results = uninstall_mesh_agent(scope="user", dry_run=False)
    for r in un_results:
        agent_id = r["agent"]
        adapter = ADAPTER_MAP[agent_id]
        mcp_path = adapter.get_mcp_config_path("user")
        if mcp_path and mcp_path.exists():
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
            assert "mesh" not in data.get("mcpServers", {})


def test_project_scope_installation_with_dir(isolated_env):
    """Test project-scope installation with explicit directory and namespace."""
    proj_dir = isolated_env["home"] / "workspace" / "custom_project"
    proj_dir.mkdir(parents=True)

    plan = plan_installation(
        agents=["claude-code", "antigravity", "cursor"],
        scope="project",
        project_dir=proj_dir,
        namespace="custom_ns",
    )
    results = apply_installation(plan, dry_run=False)
    assert len(results) == 3

    # Check Claude Code project config
    claude_mcp = proj_dir / ".claude" / "mcp.json"
    assert claude_mcp.exists()
    c_data = json.loads(claude_mcp.read_text(encoding="utf-8"))
    assert c_data["mcpServers"]["mesh"]["env"]["MESH_NAMESPACE"] == "custom_ns"

    claude_rules = proj_dir / "CLAUDE.md"
    assert claude_rules.exists()
    assert "<!-- mesh:begin" in claude_rules.read_text(encoding="utf-8")

    # Check Cursor project config
    cursor_mcp = proj_dir / ".cursor" / "mcp.json"
    assert cursor_mcp.exists()
    cursor_rules = proj_dir / ".cursorrules"
    assert cursor_rules.exists()


def test_malformed_json_handling(isolated_env):
    """Verify that malformed JSON config is caught and reported cleanly."""
    bad_cfg = isolated_env["home"] / "bad_mcp.json"
    bad_cfg.write_text("{ this is not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        update_mcp_config_file(bad_cfg, "/bin/mesh", ["server"])


def test_project_scope_binds_mesh_home_and_venv(isolated_env, monkeypatch):
    """
    Issue A-003 / B-001 regression:
    Project-scope installation must bind MESH_HOME in env and prioritize virtualenv binary.
    """
    proj_dir = isolated_env["home"] / "my_project"
    proj_dir.mkdir(parents=True)
    venv_dir = proj_dir / ".venv"
    if os.name == "nt":
        scripts_dir = venv_dir / "Scripts"
        scripts_dir.mkdir(parents=True)
        fake_mesh = scripts_dir / "mesh.exe"
    else:
        scripts_dir = venv_dir / "bin"
        scripts_dir.mkdir(parents=True)
        fake_mesh = scripts_dir / "mesh"
    fake_mesh.write_text("binary", encoding="utf-8")

    # 1. Plan with project scope and explicit namespace
    plan = plan_installation(
        agents=["antigravity"],
        scope="project",
        project_dir=proj_dir,
        namespace="my_ns",
    )
    results = apply_installation(plan, dry_run=False)
    assert len(results) == 1

    cfg_file = proj_dir / ".agents" / "mcp_config.json"
    assert cfg_file.exists()
    cfg_data = json.loads(cfg_file.read_text(encoding="utf-8"))
    mesh_entry = cfg_data["mcpServers"]["mesh"]

    # Verify MESH_HOME is populated in env
    assert "env" in mesh_entry and mesh_entry["env"] is not None
    assert "MESH_HOME" in mesh_entry["env"], "MESH_HOME must be set in project-scoped MCP env"
    expected_mesh_home = str((proj_dir / ".mesh-home").resolve())
    assert mesh_entry["env"]["MESH_HOME"] == expected_mesh_home
    assert mesh_entry["env"]["MESH_NAMESPACE"] == "my_ns"

    # Verify command resolves to project venv binary
    assert str(fake_mesh.resolve()) == mesh_entry["command"]
    assert mesh_entry["args"] == ["server"]


def test_install_apply_with_json_writes_files(isolated_env, monkeypatch, capsys):
    """
    Issue A-001 / B-002 regression:
    mesh install --apply --json must execute modifications and create files, not stay in plan mode.
    """
    from mesh.cli import install_cmd

    proj_dir = isolated_env["home"] / "json_apply_proj"
    proj_dir.mkdir(parents=True)

    test_args = [
        "mesh",
        "--scope",
        "project",
        "--agents",
        "antigravity",
        "--apply",
        "--json",
        "--dir",
        str(proj_dir),
    ]
    monkeypatch.setattr(sys, "argv", test_args)

    install_cmd()
    captured = capsys.readouterr()
    assert captured.out.strip(), "Expected JSON output on stdout"
    data = json.loads(captured.out)
    assert data["status"] == "ok"
    assert data.get("mode") == "applied", f"Expected mode 'applied', got {data.get('mode')}"

    # Verify files were actually written to disk
    mcp_file = proj_dir / ".agents" / "mcp_config.json"
    assert mcp_file.is_file(), f"Expected {mcp_file} to exist on disk after --apply --json"
    rules_file = proj_dir / "GEMINI.md"
    assert rules_file.is_file(), f"Expected {rules_file} to exist on disk after --apply --json"


def test_issue_t001_safe_replace_dir_retry(tmp_path):
    """
    Issue T-001 regression:
    _safe_replace_dir retries when Windows raises PermissionError (Access is denied)
    due to transient filesystem indexing or antivirus locks.
    """
    from mesh.skill_installer import _safe_replace_dir

    src_dir = tmp_path / "src_skill"
    src_dir.mkdir()
    (src_dir / "SKILL.md").write_text("# Skill", encoding="utf-8")

    dst_dir = tmp_path / "dst_skill"

    call_count = 0
    orig_replace = os.replace

    def mock_replace(s, d):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise PermissionError(5, "Access is denied: transient lock simulation")
        return orig_replace(s, d)

    with patch("os.replace", side_effect=mock_replace):
        _safe_replace_dir(src_dir, dst_dir, max_retries=5, backoff=0.01)

    assert call_count == 3
    assert dst_dir.is_dir()
    assert (dst_dir / "SKILL.md").read_text(encoding="utf-8") == "# Skill"


def test_issue_t003_hooks_claude_code(isolated_env):
    """
    Issue T-003 regression & Hooks feature:
    ClaudeCodeAdapter supports SessionStart hook registration in settings.json.
    Verifies plan, apply, idempotence, and clean uninstall.
    """
    adapter = ADAPTER_MAP["claude-code"]
    assert adapter.supports_hooks() is True

    hooks_file = isolated_env["home"] / ".claude" / "settings.json"
    # Seed with existing settings to verify other settings are preserved
    initial_settings = {
        "permissions": {"allow": ["Read", "LS"]},
        "env": {"FOO": "BAR"},
    }
    hooks_file.parent.mkdir(parents=True, exist_ok=True)
    hooks_file.write_text(json.dumps(initial_settings, indent=2), encoding="utf-8")

    # 1. Update hooks file
    changed, diff, bak = update_hooks_file(adapter, hooks_file, "mesh brief")
    assert changed is True
    assert bak is not None and bak.exists()

    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["Read", "LS"]}
    assert data["env"] == {"FOO": "BAR"}
    assert "hooks" in data
    assert "SessionStart" in data["hooks"]
    assert len(data["hooks"]["SessionStart"]) == 1
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "mesh brief"

    # 2. Idempotent re-run
    changed2, diff2, bak2 = update_hooks_file(adapter, hooks_file, "mesh brief")
    assert changed2 is False

    # 3. Uninstall hooks
    changed3, diff3, bak3 = remove_hooks_file_entry(adapter, hooks_file)
    assert changed3 is True
    data3 = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert "hooks" not in data3
    assert data3["permissions"] == {"allow": ["Read", "LS"]}
    assert data3["env"] == {"FOO": "BAR"}


def test_issue_t003_hooks_antigravity(isolated_env):
    """
    Issue T-003 regression & Hooks feature:
    AntigravityAdapter supports SessionStart hook registration in hooks.json.
    Verifies plan, apply, idempotence, and clean uninstall.
    """
    adapter = ADAPTER_MAP["antigravity"]
    assert adapter.supports_hooks() is True

    proj_dir = isolated_env["home"] / "ag_proj"
    proj_dir.mkdir(parents=True)
    hooks_file = proj_dir / ".agents" / "hooks.json"

    # 1. Plan with hooks=True
    plan = plan_installation(
        agents=["antigravity"],
        scope="project",
        project_dir=proj_dir,
        hooks=True,
    )
    ag_plan = plan[0]
    assert ag_plan["hooks"] is not None
    assert ag_plan["hooks"]["changed"] is True
    assert "brief" in ag_plan["hooks"]["command"]

    # 2. Apply plan
    results = apply_installation(plan)
    assert results[0]["hooks"]["status"] == "updated"
    assert hooks_file.is_file()

    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert "mesh-context-brief" in data
    assert "SessionStart" in data["mesh-context-brief"]
    cmd = data["mesh-context-brief"]["SessionStart"][0]["hooks"][0]["command"]
    assert "brief" in cmd

    # 3. Uninstall
    un_results = uninstall_mesh_agent(
        agents=["antigravity"],
        scope="project",
        project_dir=proj_dir,
    )
    assert un_results[0]["hooks"]["status"] == "removed"
    assert not hooks_file.exists()


def test_issue_t003_cli_hooks_flag(isolated_env, monkeypatch, capsys):
    """Verify mesh install --hooks registers hooks across supporting agents."""
    from mesh.cli import install_cmd

    proj_dir = isolated_env["home"] / "cli_hooks_proj"
    proj_dir.mkdir(parents=True)

    test_args = [
        "mesh",
        "--scope",
        "project",
        "--agents",
        "claude-code",
        "--hooks",
        "--apply",
        "--json",
        "--dir",
        str(proj_dir),
    ]
    monkeypatch.setattr(sys, "argv", test_args)

    install_cmd()
    captured = capsys.readouterr()
    res = json.loads(captured.out)
    assert res["status"] == "ok"
    assert res["results"][0]["hooks"]["status"] == "updated"

    settings_file = proj_dir / ".claude" / "settings.json"
    assert settings_file.is_file()
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "hooks" in data
    assert "SessionStart" in data["hooks"]


def test_interactive_install_hooks_confirmation(isolated_env, monkeypatch):
    """Verify interactive mesh install prompts to install hooks and honors confirmation."""
    import sys
    from mesh.cli import install_cmd
    import mesh.ui as ui

    proj_dir = isolated_env["home"] / "interactive_hooks_proj"
    proj_dir.mkdir(parents=True)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "argv", ["mesh", "--scope", "project", "--agents", "claude-code", "--dir", str(proj_dir)])

    # Scenario 1: User says Yes to apply, and Yes to hooks
    confirmations = [True, True]
    monkeypatch.setattr(ui, "confirm", lambda question, **kwargs: confirmations.pop(0))

    install_cmd()

    settings_file = proj_dir / ".claude" / "settings.json"
    assert settings_file.is_file()
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "hooks" in data
    assert "SessionStart" in data["hooks"]

    # Scenario 2: Second project, User says Yes to apply, but No to hooks
    proj_dir2 = isolated_env["home"] / "interactive_no_hooks_proj"
    proj_dir2.mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["mesh", "--scope", "project", "--agents", "claude-code", "--dir", str(proj_dir2)])

    confirmations = [True, False]
    monkeypatch.setattr(ui, "confirm", lambda question, **kwargs: confirmations.pop(0))

    install_cmd()

    settings_file2 = proj_dir2 / ".claude" / "settings.json"
    assert not settings_file2.exists()

