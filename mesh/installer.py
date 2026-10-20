"""
mesh/installer.py — Multi-Agent MCP Server, Skill, Rules, and Hooks Installer.

Configures Mesh for AI coding agents:
  1. MCP Server registration (resolving absolute binary path or python -m mesh.mcp_server)
  2. Agent Skill installation (SKILL.md and references/ with version stamping)
  3. Always-on Rules insertion (short <= 12 lines between idempotent markers)
  4. Non-interactive safety gates (--dry-run, --json, --apply, timestamped .bak backups)
  5. Reversible uninstall restoring original bytes
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .version import VERSION

logger = logging.getLogger(__name__)

_RULES_MARKER_REGEX = re.compile(r"<!-- mesh:begin(?:\s+v=([^\s>]+))?\s*-->.*?<!-- mesh:end -->\n?", re.DOTALL)
_RULES_BLOCK_FILE = Path(__file__).parent / "skills" / "mesh-context-layer" / "rules_block.md"
_SKILL_NAME = "mesh-context-layer"
_SKILLS_DIR = Path(__file__).parent / "skills"
_VERSION_MARKER_FILE = ".mesh-skill-version"


def resolve_mesh_mcp_command(project_dir: Optional[Path] = None) -> tuple[str, list[str]]:
    """
    Resolve the command to run the Mesh MCP server in the agent's environment:
      1. Virtual environment binary if in project_dir/.venv or active sys.prefix != sys.base_prefix
      2. Absolute path to 'mesh' or 'mesh.exe' via shutil.which -> with ['server'] argument
      3. Fallback to sys.executable with ['-m', 'mesh.mcp_server']
    """
    # 1. Check project_dir/.venv or project_dir/venv
    if project_dir:
        cand_dirs = [
            project_dir / ".venv" / ("Scripts" if os.name == "nt" else "bin"),
            project_dir / "venv" / ("Scripts" if os.name == "nt" else "bin"),
        ]
        for cdir in cand_dirs:
            mesh_bin = cdir / ("mesh.exe" if os.name == "nt" else "mesh")
            if mesh_bin.is_file():
                return str(mesh_bin.resolve()), ["server"]
            py_bin = cdir / ("python.exe" if os.name == "nt" else "python")
            if py_bin.is_file():
                return str(py_bin.resolve()), ["-m", "mesh.mcp_server"]

    # 2. Check active virtual environment
    if sys.prefix != sys.base_prefix:
        venv_scripts = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
        mesh_bin = venv_scripts / ("mesh.exe" if os.name == "nt" else "mesh")
        if mesh_bin.is_file():
            return str(mesh_bin.resolve()), ["server"]
        return str(Path(sys.executable).resolve()), ["-m", "mesh.mcp_server"]

    # 3. Global PATH search
    cmd = shutil.which("mesh")
    if cmd:
        return str(Path(cmd).resolve()), ["server"]
    return str(Path(sys.executable).resolve()), ["-m", "mesh.mcp_server"]


def get_rules_block_content(version: str = VERSION) -> str:
    """Return the always-on rules block wrapped in idempotent markers."""
    if _RULES_BLOCK_FILE.is_file():
        body = _RULES_BLOCK_FILE.read_text(encoding="utf-8").strip()
    else:
        body = (
            "# Mesh Memory & Task Protocol (Always-On)\n"
            "1. SESSION START: Call `mesh_project_brief(max_tokens=500)` immediately to load open tasks and facts.\n"
            "2. RECALL FIRST: Before asking the user or assuming conventions/ports, query `mesh_recall`.\n"
            "3. RECORD IMMEDIATELY: Store durable architectural decisions, ports, and conventions via `mesh_learn`.\n"
            "4. UPDATE, DON'T DUPLICATE: When a known fact changes, locate it via `mesh_recall` and update with `mesh_update`.\n"
            "5. TRACK WORK: Break goals into tasks with `mesh_task_create`; update status (`in_progress`, `blocked`, `done`).\n"
            "6. COMPLETE WITH SUMMARY: When marking work done via `mesh_task_complete`, always provide a summary.\n"
            "7. PROJECT CHANGELOG: When asked what was done or for progress reports, call `mesh_log`.\n"
            "8. PRIVACY & SECRETS: Never store secret values; store only non-sensitive pointers."
        )
    return f"<!-- mesh:begin v={version} -->\n{body}\n<!-- mesh:end -->\n"


def _atomic_write_with_backup(
    target_path: Path,
    new_content: str,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    """
    Write new_content to target_path with a timestamped .bak file.
    Returns (changed, diff_str, backup_path).
    """
    old_content = ""
    if target_path.exists():
        try:
            old_content = target_path.read_text(encoding="utf-8")
        except Exception:
            old_content = ""

    if old_content == new_content:
        return False, "", None

    diff_lines = list(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=str(target_path) + " (current)",
            tofile=str(target_path) + " (proposed)",
        )
    )
    diff_str = "".join(diff_lines)

    if dry_run:
        return True, diff_str, None

    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Optional[Path] = None
    if target_path.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup_path = target_path.with_name(f"{target_path.name}.bak-{ts}")
        shutil.copy2(target_path, backup_path)

    tmp_path = target_path.with_name(f"{target_path.name}.tmp-{os.getpid()}")
    try:
        tmp_path.write_text(new_content, encoding="utf-8")
        os.replace(tmp_path, target_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return True, diff_str, backup_path


class AgentAdapter:
    """Base adapter defining configuration paths and mechanisms for an AI coding agent."""

    def __init__(self, agent_id: str, display_name: str):
        self.agent_id = agent_id
        self.display_name = display_name

    def get_user_home(self) -> Path:
        return Path.home()

    def get_appdata(self) -> Optional[Path]:
        raw = os.environ.get("APPDATA", "").strip() if os.name == "nt" else ""
        return Path(raw) if raw else None

    def get_xdg_config(self) -> Path:
        raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
        return Path(raw) if raw else (self.get_user_home() / ".config")

    def supports_scope(self, scope: str) -> bool:
        return scope in ("user", "project")

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        return None

    def get_skill_root_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        return None

    def get_rules_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        return None

    def supports_hooks(self) -> bool:
        return False


class ClaudeCodeAdapter(AgentAdapter):
    def __init__(self):
        super().__init__("claude-code", "Claude Code")

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".claude" / "mcp.json"
        return self.get_user_home() / ".claude" / "mcp.json"

    def get_skill_root_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".claude" / "skills"
        return self.get_user_home() / ".claude" / "skills"

    def get_rules_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / "CLAUDE.md"
        return self.get_user_home() / ".claude" / "CLAUDE.md"

    def supports_hooks(self) -> bool:
        return True


class ClaudeDesktopAdapter(AgentAdapter):
    def __init__(self):
        super().__init__("claude-desktop", "Claude Desktop")

    def supports_scope(self, scope: str) -> bool:
        return scope == "user"

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if os.name == "nt":
            appdata = self.get_appdata()
            if appdata:
                return appdata / "Claude" / "claude_desktop_config.json"
            return self.get_user_home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
        if sys.platform == "darwin":
            return self.get_user_home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        return self.get_xdg_config() / "Claude" / "claude_desktop_config.json"


class AntigravityAdapter(AgentAdapter):
    def __init__(self):
        super().__init__("antigravity", "Antigravity")

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".agents" / "mcp_config.json"
        return self.get_user_home() / ".gemini" / "antigravity-ide" / "mcp_config.json"

    def get_skill_root_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".agents" / "skills"
        return self.get_user_home() / ".gemini" / "config" / "skills"

    def get_rules_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / "GEMINI.md"
        return self.get_user_home() / ".gemini" / "config" / "rules" / "mesh.md"


class GeminiCliAdapter(AgentAdapter):
    def __init__(self):
        super().__init__("gemini-cli", "Gemini CLI")

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".gemini" / "mcp.json"
        return self.get_user_home() / ".gemini" / "mcp.json"

    def get_skill_root_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".gemini" / "skills"
        return self.get_user_home() / ".gemini" / "skills"

    def get_rules_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / "GEMINI.md"
        return self.get_user_home() / ".gemini" / "GEMINI.md"


class CursorAdapter(AgentAdapter):
    def __init__(self):
        super().__init__("cursor", "Cursor")

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".cursor" / "mcp.json"
        return self.get_user_home() / ".cursor" / "mcp.json"

    def get_skill_root_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".cursor" / "skills"
        return self.get_user_home() / ".cursor" / "skills"

    def get_rules_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".cursorrules"
        return self.get_user_home() / ".cursor" / "rules" / "mesh.mdc"


class WindsurfAdapter(AgentAdapter):
    def __init__(self):
        super().__init__("windsurf", "Windsurf")

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".windsurf" / "mcp_config.json"
        return self.get_user_home() / ".codeium" / "windsurf" / "mcp_config.json"

    def get_skill_root_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".windsurf" / "skills"
        return self.get_user_home() / ".codeium" / "windsurf" / "skills"

    def get_rules_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".windsurfrules"
        return self.get_user_home() / ".codeium" / "windsurf" / "rules" / "mesh.md"


class OpenCodeAdapter(AgentAdapter):
    def __init__(self):
        super().__init__("opencode", "OpenCode")

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / "opencode.json"
        return self.get_xdg_config() / "opencode" / "mcp.json"

    def get_skill_root_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".opencode" / "skills"
        return self.get_xdg_config() / "opencode" / "skills"

    def get_rules_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / "AGENTS.md"
        return self.get_xdg_config() / "opencode" / "AGENTS.md"


class CodexAdapter(AgentAdapter):
    def __init__(self):
        super().__init__("codex", "OpenAI Codex CLI")

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".codex" / "mcp.json"
        return self.get_user_home() / ".codex" / "mcp.json"

    def get_skill_root_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".codex" / "skills"
        return self.get_user_home() / ".codex" / "skills"

    def get_rules_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / "AGENTS.md"
        return self.get_user_home() / ".codex" / "AGENTS.md"


_ALL_ADAPTERS: list[AgentAdapter] = [
    ClaudeCodeAdapter(),
    ClaudeDesktopAdapter(),
    AntigravityAdapter(),
    GeminiCliAdapter(),
    CursorAdapter(),
    WindsurfAdapter(),
    OpenCodeAdapter(),
    CodexAdapter(),
]

ADAPTER_MAP: dict[str, AgentAdapter] = {a.agent_id: a for a in _ALL_ADAPTERS}
SUPPORTED_AGENTS: list[str] = list(ADAPTER_MAP.keys())


def update_mcp_config_file(
    config_path: Path,
    command: str,
    args: list[str],
    env: Optional[dict[str, str]] = None,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    """
    Safely update an MCP config file to register 'mesh' without touching other servers.
    Preserves all existing top-level keys and server entries.
    """
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            content = config_path.read_text(encoding="utf-8")
            if content.strip():
                data = json.loads(content)
        except Exception as exc:
            raise ValueError(f"Existing MCP config at {config_path} contains invalid JSON: {exc}")

    if not isinstance(data, dict):
        raise ValueError(f"Existing MCP config at {config_path} is not a JSON object.")

    mcp_servers = data.setdefault("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        raise ValueError(f"Invalid 'mcpServers' entry in {config_path}: expected JSON object.")

    server_entry: dict[str, Any] = {
        "command": command,
        "args": args,
    }
    if env:
        server_entry["env"] = env

    mcp_servers["mesh"] = server_entry
    new_json = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return _atomic_write_with_backup(config_path, new_json, dry_run=dry_run)


def remove_mcp_config_entry(
    config_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    """Remove the 'mesh' entry from an MCP config file, preserving everything else."""
    if not config_path.exists():
        return False, "", None

    try:
        content = config_path.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception as exc:
        raise ValueError(f"Invalid JSON in {config_path}: {exc}")

    if not isinstance(data, dict):
        return False, "", None

    mcp_servers = data.get("mcpServers")
    if not isinstance(mcp_servers, dict) or "mesh" not in mcp_servers:
        return False, "", None

    del mcp_servers["mesh"]
    new_json = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return _atomic_write_with_backup(config_path, new_json, dry_run=dry_run)


def update_rules_file(
    rules_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    """
    Insert or update the always-on rules block between idempotent markers.
    Never touches text outside the markers.
    """
    rules_block = get_rules_block_content(VERSION)
    old_content = ""
    if rules_path.exists():
        try:
            old_content = rules_path.read_text(encoding="utf-8")
        except Exception as exc:
            raise ValueError(f"Could not read existing rules file {rules_path}: {exc}")

    if _RULES_MARKER_REGEX.search(old_content):
        new_content = _RULES_MARKER_REGEX.sub(rules_block, old_content)
    else:
        prefix = "\n\n" if old_content.strip() else ""
        new_content = old_content.rstrip() + prefix + rules_block

    return _atomic_write_with_backup(rules_path, new_content, dry_run=dry_run)


def remove_rules_file_block(
    rules_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    """Remove the Mesh always-on rules block from a rules file."""
    if not rules_path.exists():
        return False, "", None

    try:
        old_content = rules_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise ValueError(f"Could not read {rules_path}: {exc}")

    if not _RULES_MARKER_REGEX.search(old_content):
        return False, "", None

    new_content = _RULES_MARKER_REGEX.sub("", old_content).strip()
    if new_content:
        new_content += "\n"
        return _atomic_write_with_backup(rules_path, new_content, dry_run=dry_run)
    else:
        # File was only our rules block
        if dry_run:
            return True, f"would delete empty {rules_path}", None
        rules_path.unlink()
        return True, f"deleted {rules_path}", None


def install_agent_skill(
    skill_root: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[bool, str]:
    """Install or update the bundled skill directory."""
    from .skill_installer import install_skill

    msg, changed = install_skill(skill_root, overwrite=True, dry_run=dry_run, force=force)
    return changed, msg


def uninstall_agent_skill(
    skill_root: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Remove installed skill directory."""
    dst = skill_root / _SKILL_NAME
    if not dst.exists():
        return False, f"not present at {dst}"
    if dry_run:
        return True, f"would remove skill at {dst}"
    shutil.rmtree(dst)
    return True, f"removed {dst}"


def plan_installation(
    agents: Optional[list[str]] = None,
    scope: str = "user",
    project_dir: Optional[Path] = None,
    namespace: Optional[str] = None,
    mesh_home: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Build a complete installation plan for selected agents without modifying any files.
    """
    clean_scope = scope.lower().strip()
    target_agents = agents or SUPPORTED_AGENTS
    p_dir = project_dir or Path.cwd()
    cmd, args = resolve_mesh_mcp_command(project_dir=p_dir)

    env_vars: Optional[dict[str, str]] = None
    if clean_scope == "project":
        env_vars = {}
        if namespace:
            env_vars["MESH_NAMESPACE"] = namespace
        target_mesh_home = mesh_home or str((p_dir / ".mesh-home").resolve())
        env_vars["MESH_HOME"] = target_mesh_home

    plan: list[dict[str, Any]] = []

    for agent_id in target_agents:
        adapter = ADAPTER_MAP.get(agent_id.lower().strip())
        if not adapter:
            continue
        if not adapter.supports_scope(clean_scope):
            continue

        agent_plan: dict[str, Any] = {
            "agent": adapter.agent_id,
            "display_name": adapter.display_name,
            "scope": clean_scope,
            "mcp": None,
            "skill": None,
            "rules": None,
        }

        # 1. MCP
        mcp_path = adapter.get_mcp_config_path(clean_scope, p_dir)
        if mcp_path:
            try:
                changed, diff, _ = update_mcp_config_file(mcp_path, cmd, args, env=env_vars, dry_run=True)
                agent_plan["mcp"] = {
                    "path": str(mcp_path),
                    "command": cmd,
                    "args": args,
                    "action": "update" if mcp_path.exists() else "create",
                    "changed": changed,
                    "diff": diff,
                    "env": env_vars,
                }
            except Exception as exc:
                agent_plan["mcp"] = {"path": str(mcp_path), "error": str(exc), "changed": False}

        # 2. Skill
        skill_root = adapter.get_skill_root_path(clean_scope, p_dir)
        if skill_root:
            try:
                changed, msg = install_agent_skill(skill_root, dry_run=True)
                agent_plan["skill"] = {
                    "path": str(skill_root / _SKILL_NAME),
                    "action": "install",
                    "changed": changed,
                    "message": msg,
                }
            except Exception as exc:
                agent_plan["skill"] = {"path": str(skill_root / _SKILL_NAME), "error": str(exc), "changed": False}

        # 3. Rules
        rules_path = adapter.get_rules_path(clean_scope, p_dir)
        if rules_path:
            try:
                changed, diff, _ = update_rules_file(rules_path, dry_run=True)
                agent_plan["rules"] = {
                    "path": str(rules_path),
                    "action": "update" if rules_path.exists() else "create",
                    "changed": changed,
                    "diff": diff,
                }
            except Exception as exc:
                agent_plan["rules"] = {"path": str(rules_path), "error": str(exc), "changed": False}

        plan.append(agent_plan)

    return plan


def apply_installation(
    plan: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    permission_fn: Optional[Callable[[str], bool]] = None,
) -> list[dict[str, Any]]:
    """
    Execute an installation plan, applying backups before any edits.
    """
    default_cmd, default_args = resolve_mesh_mcp_command()
    results: list[dict[str, Any]] = []

    for item in plan:
        agent_id = item["agent"]
        scope = item["scope"]
        res: dict[str, Any] = {
            "agent": agent_id,
            "display_name": item["display_name"],
            "scope": scope,
            "mcp": None,
            "skill": None,
            "rules": None,
        }

        # Consent check if not dry_run and permission_fn provided
        if not dry_run and permission_fn:
            desc = f"Configure Mesh for {item['display_name']} ({scope} scope)"
            if not permission_fn(desc):
                res["status"] = "skipped (user declined)"
                results.append(res)
                continue

        # 1. MCP
        mcp_info = item.get("mcp")
        if mcp_info and not mcp_info.get("error"):
            path = Path(mcp_info["path"])
            cmd = mcp_info.get("command") or default_cmd
            args = mcp_info.get("args") or default_args
            try:
                changed, diff, bak = update_mcp_config_file(
                    path, cmd, args, env=mcp_info.get("env"), dry_run=dry_run
                )
                res["mcp"] = {
                    "path": str(path),
                    "changed": changed,
                    "backup": str(bak) if bak else None,
                    "status": "updated" if changed else "already up to date",
                }
            except Exception as exc:
                res["mcp"] = {"path": str(path), "error": str(exc), "changed": False}

        # 2. Skill
        skill_info = item.get("skill")
        if skill_info and not skill_info.get("error"):
            path = Path(skill_info["path"]).parent
            try:
                changed, msg = install_agent_skill(path, dry_run=dry_run)
                res["skill"] = {
                    "path": str(path / _SKILL_NAME),
                    "changed": changed,
                    "status": msg,
                }
            except Exception as exc:
                res["skill"] = {"path": str(path / _SKILL_NAME), "error": str(exc), "changed": False}

        # 3. Rules
        rules_info = item.get("rules")
        if rules_info and not rules_info.get("error"):
            path = Path(rules_info["path"])
            try:
                changed, diff, bak = update_rules_file(path, dry_run=dry_run)
                res["rules"] = {
                    "path": str(path),
                    "changed": changed,
                    "backup": str(bak) if bak else None,
                    "status": "updated" if changed else "already up to date",
                }
            except Exception as exc:
                res["rules"] = {"path": str(path), "error": str(exc), "changed": False}

        results.append(res)

    return results


def uninstall_mesh_agent(
    agents: Optional[list[str]] = None,
    scope: str = "user",
    project_dir: Optional[Path] = None,
    *,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """
    Remove Mesh MCP configuration, rules blocks, and skills from target agents.
    Restores original files where possible.
    """
    clean_scope = scope.lower().strip()
    target_agents = agents or SUPPORTED_AGENTS
    p_dir = project_dir or Path.cwd()
    results: list[dict[str, Any]] = []

    for agent_id in target_agents:
        adapter = ADAPTER_MAP.get(agent_id.lower().strip())
        if not adapter or not adapter.supports_scope(clean_scope):
            continue

        res: dict[str, Any] = {
            "agent": adapter.agent_id,
            "display_name": adapter.display_name,
            "scope": clean_scope,
            "mcp": None,
            "skill": None,
            "rules": None,
        }

        # 1. MCP
        mcp_path = adapter.get_mcp_config_path(clean_scope, p_dir)
        if mcp_path and mcp_path.exists():
            try:
                changed, diff, bak = remove_mcp_config_entry(mcp_path, dry_run=dry_run)
                res["mcp"] = {
                    "path": str(mcp_path),
                    "changed": changed,
                    "status": "removed" if changed else "not found",
                }
            except Exception as exc:
                res["mcp"] = {"path": str(mcp_path), "error": str(exc), "changed": False}

        # 2. Rules
        rules_path = adapter.get_rules_path(clean_scope, p_dir)
        if rules_path and rules_path.exists():
            try:
                changed, diff, bak = remove_rules_file_block(rules_path, dry_run=dry_run)
                res["rules"] = {
                    "path": str(rules_path),
                    "changed": changed,
                    "status": "removed" if changed else "not found",
                }
            except Exception as exc:
                res["rules"] = {"path": str(rules_path), "error": str(exc), "changed": False}

        # 3. Skill
        skill_root = adapter.get_skill_root_path(clean_scope, p_dir)
        if skill_root:
            try:
                changed, msg = uninstall_agent_skill(skill_root, dry_run=dry_run)
                res["skill"] = {
                    "path": str(skill_root / _SKILL_NAME),
                    "changed": changed,
                    "status": msg,
                }
            except Exception as exc:
                res["skill"] = {"path": str(skill_root / _SKILL_NAME), "error": str(exc), "changed": False}

        results.append(res)

    return results
