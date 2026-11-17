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
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

from .version import VERSION

logger = logging.getLogger(__name__)

_RULES_MARKER_REGEX = re.compile(r"<!-- mesh:begin(?:\s+v=([^\s>]+))?\s*-->.*?<!-- mesh:end -->(?:\r?\n)?", re.DOTALL)
_RULES_BLOCK_FILE = Path(__file__).parent / "skills" / "mesh-context-layer" / "rules_block.md"
_SKILL_NAME = "mesh-context-layer"
_SKILLS_DIR = Path(__file__).parent / "skills"
_VERSION_MARKER_FILE = ".mesh-skill-version"


def _reject_linked_config_path(path: Path) -> None:
    """Do not follow links in user configuration paths, including parent junctions."""
    from .skill_installer import _is_symlink_or_junction

    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        if _is_symlink_or_junction(candidate):
            raise ValueError(f"Refusing linked configuration path: {candidate}")


def _config_fingerprint(path: Path) -> str | None:
    _reject_linked_config_path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _merge_mcp_environment(existing: Any, overrides: Optional[dict[str, str]]) -> dict[str, str]:
    """Retain server policy and custom settings while applying explicit new bindings."""
    if existing is None:
        existing = {}
    if not isinstance(existing, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in existing.items()
    ):
        raise ValueError("Mesh MCP environment must be an object with string keys and values.")
    merged = dict(existing)
    merged.update(overrides or {})
    for name in ("MESH_READ_ONLY", "MESH_ENFORCE_AGENT_OWNERSHIP", "MESH_BLOCK_PRIVATE_OVERRIDE"):
        if name in existing and _policy_flag(existing, name):
            merged[name] = existing[name]
    if "MESH_MCP_INCLUDE_PRIVATE" in existing and not _policy_flag(existing, "MESH_MCP_INCLUDE_PRIVATE"):
        merged["MESH_MCP_INCLUDE_PRIVATE"] = existing["MESH_MCP_INCLUDE_PRIVATE"]
    for name in ("MESH_LOCKED_NAMESPACE", "MESH_LOCK_NAMESPACE", "MESH_AGENT_SCOPES"):
        if existing.get(name, "").strip():
            if overrides and name in overrides and overrides[name] != existing[name]:
                raise ValueError(f"Refusing to replace preserved server restriction: {name}.")
            merged[name] = existing[name]
    return merged


def _read_mcp_environment(config_path: Path) -> dict[str, str]:
    """Read the installed Mesh server's environment without executing its command."""
    _reject_linked_config_path(config_path)
    if not config_path.exists():
        return {}
    content = config_path.read_text(encoding="utf-8")
    if not content.strip():
        return {}
    if config_path.suffix == ".toml":
        if tomllib is None:
            raise ValueError("Reading existing TOML safely requires Python 3.11+ or tomli.")
        data = tomllib.loads(content)
        servers = data.get("mcp_servers", {})
        env_key = "env"
    else:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("MCP configuration must be an object.")
        is_opencode = "opencode" in config_path.name
        servers = data.get("mcp" if is_opencode else "mcpServers", {})
        env_key = "environment" if is_opencode else "env"
    if not isinstance(servers, dict):
        raise ValueError("MCP server configuration must be an object.")
    entry = servers.get("mesh", {})
    if not isinstance(entry, dict):
        raise ValueError("Mesh MCP server configuration must be an object.")
    return _merge_mcp_environment(entry.get(env_key), None)


def _policy_flag(environment: dict[str, str], name: str, default: bool = False) -> bool:
    raw = environment.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"Invalid boolean policy setting: {name}.")


def _connection_policy(environment: dict[str, str], namespace: str, agent_id: str) -> dict[str, bool]:
    policy = {
        "include_private": False,
        "read_only": _policy_flag(environment, "MESH_READ_ONLY"),
        "enforce_agent_ownership": _policy_flag(environment, "MESH_ENFORCE_AGENT_OWNERSHIP"),
        "allow_namespace_override": False,
    }
    # The previous lock accepts either a boolean or an explicit namespace name.
    for setting in ("MESH_LOCKED_NAMESPACE", "MESH_LOCK_NAMESPACE"):
        locked = environment.get(setting, "").strip()
        if locked:
            allowed = environment.get("MESH_NAMESPACE", "shared") if locked.lower() in ("1", "true", "yes") else locked
            if allowed != namespace:
                raise ValueError("Installed MCP namespace lock conflicts with the requested project namespace.")
    raw_scopes = environment.get("MESH_AGENT_SCOPES", "").strip()
    if raw_scopes:
        scopes = json.loads(raw_scopes)
        if not isinstance(scopes, dict):
            raise ValueError("MESH_AGENT_SCOPES must be a JSON object.")
        rule = scopes.get(agent_id) or scopes.get("*")
        if rule is not None:
            if not isinstance(rule, dict):
                raise ValueError("Agent scope must be a JSON object.")
            names = rule.get("namespaces")
            if names is not None:
                if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                    raise ValueError("Agent scope namespaces must be strings.")
                if namespace not in [name.strip() for name in names]:
                    raise ValueError("Installed MCP agent scope excludes the requested namespace.")
            if str(rule.get("mode") or "rw").strip().lower() in ("ro", "read", "readonly", "read-only", "read_only"):
                policy["read_only"] = True
    return policy


def _project_namespace(project_dir: Path) -> str:
    """Use persisted spelling first; sanitize only a newly derived repository name."""
    from .audit import validate_namespace

    marker = project_dir / ".mesh" / "namespace"
    _reject_linked_config_path(marker)
    if marker.is_file():
        return validate_namespace(marker.read_text(encoding="utf-8").strip())
    name = project_dir.name
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            remote = result.stdout.strip().rstrip("/")
            name = remote.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            if name.endswith(".git"):
                name = name[:-4]
    except (OSError, subprocess.TimeoutExpired):
        pass
    generated = re.sub(r"[^A-Za-z0-9._-]", "-", name).replace("..", "-").strip("._-")[:64]
    return validate_namespace(generated)


def _connection_profile_plan(
    path: Path, namespace: str, mesh_home: Path, agent_id: str, policy: dict[str, bool],
) -> dict[str, Any]:
    """Prepare a reviewable profile and preserve stricter reviewed restrictions."""
    _reject_linked_config_path(path)
    _reject_linked_config_path(mesh_home)
    try:
        home_reference = os.path.relpath(mesh_home, path.parent)
    except ValueError:
        home_reference = str(mesh_home)
    document: dict[str, Any] = {
        "version": 1, "namespace": namespace, "mesh_home": home_reference,
        "agent_id": agent_id, "policy": dict(policy),
        "producer_scope": "local-user",
    }
    if path.exists():
        from .client import ConnectionProfile, _MAX_PROFILE_BYTES, _pairs_unique

        if path.stat().st_size > _MAX_PROFILE_BYTES:
            raise ValueError("Existing connection profile is too large.")
        previous = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs_unique)
        ConnectionProfile._from_document(previous, path)
        allowed_keys = {"version", "namespace", "mesh_home", "agent_id", "policy", "endpoint", "token_file", "producer_scope"}
        if not isinstance(previous, dict) or set(previous) - allowed_keys or previous.get("version") != 1:
            raise ValueError("Existing connection profile has an unsupported schema.")
        previous_home = previous.get("mesh_home")
        if not isinstance(previous_home, str) or not previous_home:
            raise ValueError("Existing connection profile has an invalid data-root pointer.")
        previous_root = (path.parent / previous_home).absolute()
        _reject_linked_config_path(previous_root)
        if previous.get("namespace") != namespace or previous.get("agent_id") != agent_id or previous_root.resolve() != mesh_home.resolve():
            raise ValueError("Existing connection profile has a different binding; review it before replacing it.")
        previous_policy = previous.get("policy", {})
        if not isinstance(previous_policy, dict) or set(previous_policy) - set(policy) or any(
            not isinstance(value, bool) for value in previous_policy.values()
        ):
            raise ValueError("Existing connection profile has an unsupported policy.")
        document["mesh_home"] = previous_home
        from .operation_contract import validate_producer_scope

        existing_scope = validate_producer_scope(previous.get("producer_scope", "local-user"))
        if existing_scope.startswith("principal:") and existing_scope != "principal:" + agent_id:
            raise ValueError("Existing connection profile has a different authenticated principal binding.")
        document["producer_scope"] = existing_scope
        for key in policy:
            old_value = previous_policy.get(key, False)
            document["policy"][key] = (
                old_value and policy[key] if key in ("include_private", "allow_namespace_override")
                else old_value or policy[key]
            )
        for key in ("endpoint", "token_file"):
            if key in previous:
                if not isinstance(previous[key], str) or not previous[key]:
                    raise ValueError(f"Existing connection profile has an invalid {key} pointer.")
                if key == "token_file":
                    _reject_linked_config_path((path.parent / previous[key]).absolute())
                document[key] = previous[key]
    content = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    changed, diff, _ = _atomic_write_with_backup(path, content, dry_run=True)
    return {
        "path": str(path), "document": document, "content": content,
        "action": "update" if path.exists() else "create", "changed": changed, "diff": diff,
        "baseline_sha256": _config_fingerprint(path),
    }


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
    # A stale embedded fallback silently changes the agent protocol on a broken install.
    body = _RULES_BLOCK_FILE.read_text(encoding="utf-8").strip()
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
    _reject_linked_config_path(target_path)
    old_content = ""
    old_bytes = b""
    if target_path.exists():
        old_bytes = target_path.read_bytes()
        old_content = old_bytes.decode("utf-8")

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
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        backup_fd, backup_name = tempfile.mkstemp(prefix=f"{target_path.name}.bak-{ts}-", dir=target_path.parent)
        backup_path = Path(backup_name)
        with os.fdopen(backup_fd, "wb") as stream:
            stream.write(old_bytes)
            stream.flush()
            os.fsync(stream.fileno())

    fd, temporary_name = tempfile.mkstemp(prefix=f"{target_path.name}.tmp-", dir=target_path.parent)
    tmp_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(new_content)
            stream.flush()
            os.fsync(stream.fileno())
        _reject_linked_config_path(target_path)
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

    def __init__(
        self,
        agent_id: str,
        display_name: str,
        verified: bool = True,
        verification_note: str = "",
    ):
        self.agent_id = agent_id
        self.display_name = display_name
        self.verified = verified
        self.verification_note = verification_note

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

    def get_legacy_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        return None

    def get_skill_root_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        return None

    def get_rules_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        return None

    def supports_hooks(self) -> bool:
        return False

    def get_hooks_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        return None


class ClaudeCodeAdapter(AgentAdapter):
    def __init__(self):
        super().__init__("claude-code", "Claude Code", verified=True)

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        # Claude Code official docs (2026): project scope reads /.mcp.json at repository root,
        # and user scope reads ~/.claude.json.
        if scope == "project" and project_dir:
            return project_dir / ".mcp.json"
        return self.get_user_home() / ".claude.json"

    def get_legacy_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        """Pre-0.8.2 Mesh versions incorrectly wrote .claude/mcp.json."""
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

    def get_hooks_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".claude" / "settings.json"
        return self.get_user_home() / ".claude" / "settings.json"


class ClaudeDesktopAdapter(AgentAdapter):
    def __init__(self):
        super().__init__("claude-desktop", "Claude Desktop", verified=True)

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
        super().__init__("antigravity", "Antigravity", verified=True)

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

    def supports_hooks(self) -> bool:
        return True

    def get_hooks_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".agents" / "hooks.json"
        return self.get_user_home() / ".gemini" / "config" / "hooks.json"


class GeminiCliAdapter(AgentAdapter):
    def __init__(self):
        super().__init__(
            "gemini-cli",
            "Gemini CLI",
            verified=False,
            verification_note="Official documentation specifies ~/.gemini/settings.json or .gemini/settings.json with an 'mcpServers' object.",
        )

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".gemini" / "settings.json"
        return self.get_user_home() / ".gemini" / "settings.json"

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
        super().__init__("cursor", "Cursor", verified=True)

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
        super().__init__(
            "windsurf",
            "Windsurf",
            verified=False,
            verification_note="User-scope verified (~/.codeium/windsurf/mcp_config.json, targeting Windsurf editor; JetBrains plugin uses ~/.codeium/mcp_config.json). Project-level MCP config is not documented by Windsurf, so Mesh does not write one.",
        )

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project":
            return None
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
        super().__init__(
            "opencode",
            "OpenCode",
            verified=False,
            verification_note="Official documentation specifies opencode.json with an 'mcp' table (type: 'local', command: [...]).",
        )

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / "opencode.json"
        return self.get_xdg_config() / "opencode" / "opencode.json"

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
        super().__init__(
            "codex",
            "OpenAI Codex CLI",
            verified=False,
            verification_note="Official documentation specifies ~/.codex/config.toml or .codex/config.toml ([mcp_servers.<name>] TOML tables).",
        )

    def get_mcp_config_path(self, scope: str, project_dir: Optional[Path] = None) -> Optional[Path]:
        if scope == "project" and project_dir:
            return project_dir / ".codex" / "config.toml"
        return self.get_user_home() / ".codex" / "config.toml"

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


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(key)} = {_toml_value(item)}" for key, item in value.items()) + " }"
    raise ValueError(f"Cannot safely preserve TOML setting of type {type(value).__name__}.")


def _format_toml_mcp_section(
    command: str,
    args: list[str],
    env: Optional[dict[str, str]] = None,
    extra_settings: Optional[dict[str, Any]] = None,
) -> str:
    lines = [
        "[mcp_servers.mesh]",
        f"command = {json.dumps(command)}",
        f"args = {json.dumps(args)}",
    ]
    for key, value in (extra_settings or {}).items():
        if key not in ("command", "args", "env"):
            lines.append(f"{json.dumps(key)} = {_toml_value(value)}")
    if env:
        lines.append("[mcp_servers.mesh.env]")
        for k, v in sorted(env.items()):
            lines.append(f"{json.dumps(k)} = {json.dumps(v)}")
    return "\n".join(lines) + "\n"


def _splice_toml_mcp_section(content: str, replacement: Optional[str]) -> str:
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    in_mesh = False
    mesh_inserted = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            header = stripped.lstrip("[").split("]", 1)[0].strip()
            normalized_header = header.replace('"mesh"', "mesh").replace("'mesh'", "mesh")
            if normalized_header == "mcp_servers.mesh" or normalized_header.startswith("mcp_servers.mesh."):
                in_mesh = True
                if replacement and not mesh_inserted:
                    out.append(replacement if replacement.endswith("\n") else replacement + "\n")
                    mesh_inserted = True
                continue
            else:
                in_mesh = False
        if not in_mesh:
            out.append(line)

    if replacement and not mesh_inserted:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        if out and out[-1].strip():
            out.append("\n")
        out.append(replacement if replacement.endswith("\n") else replacement + "\n")

    return "".join(out)


def _update_toml_mcp_config(
    config_path: Path,
    command: str,
    args: list[str],
    env: Optional[dict[str, str]] = None,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    _reject_linked_config_path(config_path)
    old_content = ""
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            old_content = config_path.read_text(encoding="utf-8")
            if old_content.strip():
                if tomllib is None:
                    raise ValueError("Reading existing TOML safely requires Python 3.11+ or tomli.")
                data = tomllib.loads(old_content)
        except Exception as exc:
            raise ValueError(f"Existing MCP config at {config_path} contains invalid TOML: {exc}")

    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict) or not isinstance(servers.get("mesh", {}), dict):
        raise ValueError("Mesh MCP server configuration must be an object.")
    existing_entry = servers.get("mesh", {})
    merged_env = _merge_mcp_environment(existing_entry.get("env"), env)
    replacement = _format_toml_mcp_section(command, args, merged_env, existing_entry)
    new_content = _splice_toml_mcp_section(old_content, replacement)

    if tomllib is not None:
        try:
            tomllib.loads(new_content)
        except Exception as exc:
            raise ValueError(f"Generated invalid TOML for {config_path}: {exc}")

    return _atomic_write_with_backup(config_path, new_content, dry_run=dry_run)


def _remove_toml_mcp_config(
    config_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    if not config_path.exists():
        return False, "", None

    try:
        old_content = config_path.read_text(encoding="utf-8")
        if old_content.strip() and tomllib is not None:
            data = tomllib.loads(old_content)
            if not isinstance(data.get("mcp_servers", {}), dict) or "mesh" not in data.get("mcp_servers", {}):
                return False, "", None
    except Exception as exc:
        raise ValueError(f"Invalid TOML in {config_path}: {exc}")

    new_content = _splice_toml_mcp_section(old_content, None).strip()
    if not new_content:
        if dry_run:
            return True, f"would delete empty {config_path}", None
        config_path.unlink()
        return True, f"deleted empty {config_path}", None

    new_content += "\n"
    if tomllib is not None:
        try:
            tomllib.loads(new_content)
        except Exception as exc:
            raise ValueError(f"Generated invalid TOML during removal in {config_path}: {exc}")

    return _atomic_write_with_backup(config_path, new_content, dry_run=dry_run)


def _update_opencode_json_mcp(
    config_path: Path,
    command: str,
    args: list[str],
    env: Optional[dict[str, str]] = None,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    _reject_linked_config_path(config_path)
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

    mcp = data.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise ValueError(f"Invalid 'mcp' entry in {config_path}: expected JSON object.")

    cmd_list = [command] + list(args)
    existing_entry = mcp.get("mesh", {})
    if not isinstance(existing_entry, dict):
        raise ValueError("Mesh MCP server configuration must be an object.")
    server_entry = dict(existing_entry)
    server_entry.update({"type": "local", "command": cmd_list})
    server_entry.setdefault("enabled", True)
    merged_env = _merge_mcp_environment(existing_entry.get("environment"), env)
    if merged_env:
        server_entry["environment"] = merged_env

    mcp["mesh"] = server_entry
    new_json = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return _atomic_write_with_backup(config_path, new_json, dry_run=dry_run)


def _remove_opencode_json_mcp(
    config_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    if not config_path.exists():
        return False, "", None

    try:
        content = config_path.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception as exc:
        raise ValueError(f"Invalid JSON in {config_path}: {exc}")

    if not isinstance(data, dict):
        return False, "", None

    mcp = data.get("mcp")
    if not isinstance(mcp, dict) or "mesh" not in mcp:
        return False, "", None

    del mcp["mesh"]
    if len(data) == 1 and not mcp:
        if dry_run:
            return True, f"would delete empty {config_path}", None
        config_path.unlink()
        return True, f"deleted empty {config_path}", None

    new_json = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return _atomic_write_with_backup(config_path, new_json, dry_run=dry_run)


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
    Supports standard JSON mcpServers, OpenCode mcp object, and Codex TOML tables.
    """
    _reject_linked_config_path(config_path)
    if config_path.suffix == ".toml":
        return _update_toml_mcp_config(config_path, command, args, env=env, dry_run=dry_run)
    if "opencode" in config_path.name:
        return _update_opencode_json_mcp(config_path, command, args, env=env, dry_run=dry_run)

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

    existing_entry = mcp_servers.get("mesh", {})
    if not isinstance(existing_entry, dict):
        raise ValueError("Mesh MCP server configuration must be an object.")
    server_entry = dict(existing_entry)
    server_entry.update({"command": command, "args": args})
    merged_env = _merge_mcp_environment(existing_entry.get("env"), env)
    if merged_env:
        server_entry["env"] = merged_env

    mcp_servers["mesh"] = server_entry
    new_json = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return _atomic_write_with_backup(config_path, new_json, dry_run=dry_run)


def remove_mcp_config_entry(
    config_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    """Remove the 'mesh' entry from an MCP config file, preserving everything else."""
    _reject_linked_config_path(config_path)
    if not config_path.exists():
        return False, "", None

    if config_path.suffix == ".toml":
        return _remove_toml_mcp_config(config_path, dry_run=dry_run)
    if "opencode" in config_path.name:
        return _remove_opencode_json_mcp(config_path, dry_run=dry_run)

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
    if len(data) == 1 and not mcp_servers:
        # File only contained the Mesh server entry under mcpServers
        if dry_run:
            return True, f"would delete empty {config_path}", None
        config_path.unlink()
        return True, f"deleted empty {config_path}", None

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
    _reject_linked_config_path(rules_path)
    rules_block = get_rules_block_content(VERSION)
    old_content = ""
    if rules_path.exists():
        try:
            old_content = rules_path.read_bytes().decode("utf-8")
        except Exception as exc:
            raise ValueError(f"Could not read existing rules file {rules_path}: {exc}")

    if _RULES_MARKER_REGEX.search(old_content):
        def replace_block(match: re.Match[str]) -> str:
            separator = "<!-- mesh:separator=2 -->" in match.group(0)
            return rules_block.replace("\n", "\n<!-- mesh:separator=2 -->\n", 1) if separator else rules_block
        new_content = _RULES_MARKER_REGEX.sub(replace_block, old_content)
    else:
        prefix = "\n\n" if old_content.strip() else ""
        if prefix:
            # Identify only our added separator so uninstall preserves user bytes.
            rules_block = rules_block.replace("\n", "\n<!-- mesh:separator=2 -->\n", 1)
        new_content = old_content + prefix + rules_block

    return _atomic_write_with_backup(rules_path, new_content, dry_run=dry_run)


def remove_rules_file_block(
    rules_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    """Remove the Mesh always-on rules block from a rules file."""
    _reject_linked_config_path(rules_path)
    if not rules_path.exists():
        return False, "", None

    try:
        old_content = rules_path.read_bytes().decode("utf-8")
    except Exception as exc:
        raise ValueError(f"Could not read {rules_path}: {exc}")

    if not _RULES_MARKER_REGEX.search(old_content):
        return False, "", None

    new_content = old_content
    for match in reversed(list(_RULES_MARKER_REGEX.finditer(old_content))):
        start = match.start()
        if "<!-- mesh:separator=2 -->" in match.group(0) and old_content[max(0, start - 2):start] == "\n\n":
            start -= 2
        new_content = new_content[:start] + new_content[match.end():]
    if new_content.strip():
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
    _reject_linked_config_path(skill_root)
    dst = skill_root / _SKILL_NAME
    from .skill_installer import _is_symlink_or_junction, _remove_path_or_link

    if not dst.exists() and not _is_symlink_or_junction(dst):
        return False, f"not present at {dst}"
    if dry_run:
        return True, f"would remove skill at {dst}"
    _remove_path_or_link(dst)
    return True, f"removed {dst}"


def _is_mesh_brief_command(command: Any) -> bool:
    """Recognize Mesh executables, preserving hooks that merely mention Mesh."""
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return False
    if not words:
        return False
    executable = re.split(r"[\\/]", words[0].strip("\"'"))[-1].lower()
    if executable in {"mesh", "mesh.exe", "mesh-hook", "mesh-hook.exe"}:
        return len(words) > 1 and words[1] == "brief"
    return (
        bool(re.fullmatch(r"python(?:\d+(?:\.\d+)?)?(?:\.exe)?", executable))
        and words[1:4] == ["-m", "mesh.hooks", "brief"]
    )


def update_hooks_file(
    adapter: AgentAdapter,
    hooks_path: Path,
    command: str,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    """
    Safely register Mesh's SessionStart lifecycle hook into the agent's hooks/settings file.
    Preserves all existing hooks and other configuration keys.
    """
    _reject_linked_config_path(hooks_path)
    data: dict[str, Any] = {}
    if hooks_path.exists():
        try:
            content = hooks_path.read_text(encoding="utf-8")
            if content.strip():
                data = json.loads(content)
        except Exception as exc:
            raise ValueError(f"Existing hooks config at {hooks_path} contains invalid JSON: {exc}")

    if not isinstance(data, dict):
        raise ValueError(f"Existing hooks config at {hooks_path} is not a JSON object.")

    if adapter.agent_id == "claude-code":
        hooks_dict = data.setdefault("hooks", {})
        if not isinstance(hooks_dict, dict):
            raise ValueError(f"Invalid 'hooks' entry in {hooks_path}: expected JSON object.")
        session_start = hooks_dict.setdefault("SessionStart", [])
        if not isinstance(session_start, list):
            raise ValueError(f"Invalid 'SessionStart' entry in {hooks_path}: expected JSON array.")

        # Check if already present and matches
        for entry in session_start:
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                for sub_hook in entry.get("hooks", []):
                    if isinstance(sub_hook, dict) and _is_mesh_brief_command(sub_hook.get("command")):
                        if sub_hook.get("command") == command:
                            return False, "", None
                        sub_hook["command"] = command
                        new_json = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
                        return _atomic_write_with_backup(hooks_path, new_json, dry_run=dry_run)

        # Append new hook
        new_hook_entry = {
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 15,
                    "statusMessage": "Loading Mesh context brief...",
                }
            ],
        }
        session_start.append(new_hook_entry)

    elif adapter.agent_id == "antigravity":
        existing_mesh_hook = data.get("mesh-context-brief")
        new_hook_def = {
            "PreInvocation": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 15,
                }
            ]
        }
        if existing_mesh_hook == new_hook_def:
            return False, "", None
        data["mesh-context-brief"] = new_hook_def

    else:
        return False, "", None

    new_json = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return _atomic_write_with_backup(hooks_path, new_json, dry_run=dry_run)


def remove_hooks_file_entry(
    adapter: AgentAdapter,
    hooks_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, str, Optional[Path]]:
    """Remove Mesh's hook entry from the agent's hooks/settings file, preserving other settings."""
    _reject_linked_config_path(hooks_path)
    if not hooks_path.exists():
        return False, "", None

    try:
        content = hooks_path.read_text(encoding="utf-8")
        data = json.loads(content)
    except Exception as exc:
        raise ValueError(f"Invalid JSON in {hooks_path}: {exc}")

    if not isinstance(data, dict):
        return False, "", None

    changed = False
    if adapter.agent_id == "claude-code":
        hooks_dict = data.get("hooks")
        if isinstance(hooks_dict, dict):
            session_start = hooks_dict.get("SessionStart")
            if isinstance(session_start, list):
                new_session_start = []
                for entry in session_start:
                    if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                        entry_hooks = [
                            h
                            for h in entry.get("hooks", [])
                            if not (isinstance(h, dict) and _is_mesh_brief_command(h.get("command")))
                        ]
                        if len(entry_hooks) != len(entry.get("hooks", [])):
                            changed = True
                        if entry_hooks:
                            entry["hooks"] = entry_hooks
                            new_session_start.append(entry)
                    else:
                        new_session_start.append(entry)
                if changed:
                    if new_session_start:
                        hooks_dict["SessionStart"] = new_session_start
                    else:
                        del hooks_dict["SessionStart"]
                    if not hooks_dict:
                        del data["hooks"]

    elif adapter.agent_id == "antigravity":
        if "mesh-context-brief" in data:
            del data["mesh-context-brief"]
            changed = True

    if not changed:
        return False, "", None

    if not data:
        if dry_run:
            return True, f"would delete empty {hooks_path}", None
        hooks_path.unlink()
        return True, f"deleted empty {hooks_path}", None

    new_json = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return _atomic_write_with_backup(hooks_path, new_json, dry_run=dry_run)


_AGENT_ALIAS_MAP: dict[str, str] = {
    "claude": "claude-code",
    "gemini": "gemini-cli",
}


def plan_installation(
    agents: Optional[list[str]] = None,
    scope: str = "user",
    project_dir: Optional[Path] = None,
    namespace: Optional[str] = None,
    mesh_home: Optional[str] = None,
    hooks: bool = False,
) -> list[dict[str, Any]]:
    """
    Build a complete installation plan for selected agents without modifying any files.
    """
    from .audit import get_mesh_dir, validate_namespace
    from .hooks import encode_profile_pointer

    clean_scope = scope.lower().strip()
    if clean_scope not in ("user", "project"):
        raise ValueError("Installation scope must be 'user' or 'project'.")
    if namespace is not None:
        namespace = validate_namespace(namespace)
    if agents:
        target_agents = [_AGENT_ALIAS_MAP.get(a.lower().strip(), a.lower().strip()) for a in agents]
    else:
        target_agents = SUPPORTED_AGENTS

    p_dir = (project_dir or Path.cwd()).absolute()
    _reject_linked_config_path(p_dir)
    cmd, args = resolve_mesh_mcp_command(project_dir=p_dir)

    plan: list[dict[str, Any]] = []
    project_profile_planned = False

    for agent_id in target_agents:
        adapter = ADAPTER_MAP.get(agent_id.lower().strip())
        if not adapter:
            continue
        if not adapter.supports_scope(clean_scope):
            continue

        agent_plan: dict[str, Any] = {
            "agent": adapter.agent_id,
            "display_name": adapter.display_name,
            "verified": adapter.verified,
            "verification_note": adapter.verification_note,
            "scope": clean_scope,
            "mcp": None,
            "skill": None,
            "rules": None,
            "hooks": None,
            "profiles": [],
        }

        # 1. MCP
        mcp_path = adapter.get_mcp_config_path(clean_scope, p_dir)
        legacy_path = adapter.get_legacy_mcp_config_path(clean_scope, p_dir)
        legacy_migrating = False
        if legacy_path and legacy_path.exists():
            try:
                l_content = legacy_path.read_text(encoding="utf-8")
                l_data = json.loads(l_content)
                if isinstance(l_data, dict) and "mesh" in l_data.get("mcpServers", {}):
                    legacy_migrating = True
            except Exception:
                pass

        if mcp_path:
            try:
                existing_env = _read_mcp_environment(mcp_path)
                if legacy_migrating and legacy_path:
                    existing_env = _merge_mcp_environment(_read_mcp_environment(legacy_path), existing_env)
                server_environment = dict(os.environ)
                server_environment.update(existing_env)
                resolved_namespace = namespace or server_environment.get("MESH_NAMESPACE")
                if not resolved_namespace:
                    resolved_namespace = _project_namespace(p_dir) if clean_scope == "project" else "shared"
                resolved_namespace = validate_namespace(resolved_namespace)
                stored_home = existing_env.get("MESH_HOME") or existing_env.get("MESH_DIR")
                if mesh_home is not None:
                    target_mesh_home = Path(mesh_home).expanduser().absolute()
                elif stored_home:
                    target_mesh_home = Path(stored_home).expanduser().absolute()
                elif clean_scope == "project":
                    target_mesh_home = p_dir / ".mesh-home"
                else:
                    target_mesh_home = get_mesh_dir().absolute()
                resolved_agent = server_environment.get("MESH_AGENT_ID") or "mcp-agent"
                policy = _connection_policy(server_environment, resolved_namespace, resolved_agent)
                env_vars = dict(existing_env)
                env_vars.update({"MESH_HOME": str(target_mesh_home), "MESH_NAMESPACE": resolved_namespace})
                # Make inherited restrictions explicit, so upgrades and fallback profiles agree.
                for setting in ("MESH_READ_ONLY", "MESH_ENFORCE_AGENT_OWNERSHIP", "MESH_BLOCK_PRIVATE_OVERRIDE", "MESH_MCP_INCLUDE_PRIVATE", "MESH_AGENT_SCOPES", "MESH_LOCKED_NAMESPACE", "MESH_LOCK_NAMESPACE", "MESH_AGENT_ID"):
                    if setting in server_environment:
                        env_vars[setting] = server_environment[setting]
                changed, diff, _ = update_mcp_config_file(mcp_path, cmd, args, env=env_vars, dry_run=True)
                agent_plan["mcp"] = {
                    "path": str(mcp_path),
                    "command": cmd,
                    "args": args,
                    "action": "update" if mcp_path.exists() else "create",
                    "changed": changed or legacy_migrating,
                    "diff": diff,
                    "env": env_vars,
                    "legacy_migration": str(legacy_path) if legacy_migrating else None,
                    "baseline_sha256": _config_fingerprint(mcp_path),
                }
                profile_root = p_dir / ".mesh" if clean_scope == "project" else target_mesh_home
                profile_path = profile_root / "connections" / f"{adapter.agent_id}.json"
                agent_plan["profiles"].append(
                    _connection_profile_plan(profile_path, resolved_namespace, target_mesh_home, resolved_agent, policy)
                )
                shared_profile = profile_root / "connection.json"
                if clean_scope == "project" and not project_profile_planned and not shared_profile.exists():
                    agent_plan["profiles"].append(
                        _connection_profile_plan(shared_profile, resolved_namespace, target_mesh_home, resolved_agent, policy)
                    )
                    project_profile_planned = True
            except Exception as exc:
                agent_plan["mcp"] = {"path": str(mcp_path), "error": str(exc), "changed": False}
                agent_plan["profiles"] = []

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

        # 4. Hooks (brief context injection; excludes private memories)
        if hooks and adapter.supports_hooks():
            hooks_path = adapter.get_hooks_path(clean_scope, p_dir)
            if hooks_path:
                try:
                    if agent_plan["mcp"] and agent_plan["mcp"].get("error"):
                        raise ValueError("Cannot install a hook without a verified matching connection profile.")
                    pointer = (
                        {"profile": agent_plan["profiles"][0]["path"]} if clean_scope == "project"
                        else {"discover": True, "agent": adapter.agent_id}
                    )
                    brief_cmd = "mesh-hook brief --no-private --max-tokens 2000 --profile-data " + encode_profile_pointer(pointer)
                    if adapter.agent_id == "antigravity":
                        brief_cmd += " --format antigravity"
                    changed, diff, _ = update_hooks_file(adapter, hooks_path, brief_cmd, dry_run=True)
                    agent_plan["hooks"] = {
                        "path": str(hooks_path),
                        "command": brief_cmd,
                        "action": "update" if hooks_path.exists() else "create",
                        "changed": changed,
                        "diff": diff,
                    }
                except Exception as exc:
                    agent_plan["hooks"] = {"path": str(hooks_path), "error": str(exc), "changed": False}

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
        adapter = ADAPTER_MAP.get(agent_id)
        res: dict[str, Any] = {
            "agent": agent_id,
            "display_name": item["display_name"],
            "verified": item.get("verified", True),
            "verification_note": item.get("verification_note", ""),
            "scope": scope,
            "mcp": None,
            "skill": None,
            "rules": None,
            "hooks": None,
            "profiles": [],
        }
        for component in ("mcp", "skill", "rules", "hooks"):
            planned = item.get(component)
            if planned and planned.get("error"):
                res[component] = {"path": planned["path"], "error": planned["error"], "changed": False}

        # Consent check if not dry_run and permission_fn provided
        if not dry_run and permission_fn:
            desc = f"Configure Mesh for {item['display_name']} ({scope} scope)"
            if not permission_fn(desc):
                res["status"] = "skipped (user declined)"
                results.append(res)
                continue

        # Profiles are installed before hooks, and shared policy survives uninstall.
        try:
            reviewed = [info for info in [item.get("mcp"), *item.get("profiles", [])] if info and "baseline_sha256" in info]
            for info in reviewed:
                if _config_fingerprint(Path(info["path"])) != info["baseline_sha256"]:
                    raise ValueError("Reviewed MCP/profile configuration changed; regenerate the installation plan.")
        except (OSError, ValueError) as exc:
            res["status"] = "stopped (configuration changed)"
            res["error"] = str(exc)
            results.append(res)
            continue
        for profile_info in item.get("profiles", []):
            path = Path(profile_info["path"])
            try:
                changed, _, backup = _atomic_write_with_backup(path, profile_info["content"], dry_run=dry_run)
                res["profiles"].append({
                    "path": str(path), "changed": changed, "backup": str(backup) if backup else None,
                    "status": "updated" if changed else "already up to date",
                })
            except Exception as exc:
                res["profiles"].append({"path": str(path), "error": str(exc), "changed": False})

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
                leg_migrated = None
                leg_path_str = mcp_info.get("legacy_migration")
                if leg_path_str:
                    leg_path = Path(leg_path_str)
                    if leg_path.exists():
                        try:
                            l_changed, _, _ = remove_mcp_config_entry(leg_path, dry_run=dry_run)
                            if l_changed:
                                leg_migrated = str(leg_path)
                        except Exception:
                            pass
                res["mcp"] = {
                    "path": str(path),
                    "changed": changed or (leg_migrated is not None),
                    "backup": str(bak) if bak else None,
                    "status": "updated" if (changed or leg_migrated) else "already up to date",
                    "legacy_migrated": leg_migrated,
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

        # 4. Hooks
        hooks_info = item.get("hooks")
        if hooks_info and not hooks_info.get("error"):
            path = Path(hooks_info["path"])
            brief_cmd = hooks_info.get("command")
            if adapter:
                try:
                    if not isinstance(brief_cmd, str) or not brief_cmd:
                        raise ValueError("Hook command was not present in the reviewed plan.")
                    if any(profile.get("error") for profile in res["profiles"]) or (res.get("mcp") or {}).get("error"):
                        raise ValueError("Hook installation stopped because its connection profile or MCP update failed.")
                    changed, diff, bak = update_hooks_file(adapter, path, brief_cmd, dry_run=dry_run)
                    res["hooks"] = {
                        "path": str(path),
                        "changed": changed,
                        "backup": str(bak) if bak else None,
                        "status": "updated" if changed else "already up to date",
                    }
                except Exception as exc:
                    res["hooks"] = {"path": str(path), "error": str(exc), "changed": False}

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
    Remove Mesh MCP configuration, rules blocks, skills, and hooks from target agents.
    Restores original files where possible.
    """
    clean_scope = scope.lower().strip()
    if clean_scope not in ("user", "project"):
        raise ValueError("Installation scope must be 'user' or 'project'.")
    if agents:
        target_agents = [_AGENT_ALIAS_MAP.get(a.lower().strip(), a.lower().strip()) for a in agents]
    else:
        target_agents = SUPPORTED_AGENTS

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
            "hooks": None,
        }

        # 1. MCP
        mcp_path = adapter.get_mcp_config_path(clean_scope, p_dir)
        legacy_path = adapter.get_legacy_mcp_config_path(clean_scope, p_dir)
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

        if legacy_path and legacy_path.exists() and legacy_path != mcp_path:
            try:
                l_changed, _, _ = remove_mcp_config_entry(legacy_path, dry_run=dry_run)
                if l_changed and (not res.get("mcp") or res["mcp"].get("status") == "not found"):
                    res["mcp"] = {"path": str(legacy_path), "changed": True, "status": "removed"}
            except Exception:
                pass

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

        # 4. Hooks
        if adapter.supports_hooks():
            hooks_path = adapter.get_hooks_path(clean_scope, p_dir)
            if hooks_path and hooks_path.exists():
                try:
                    changed, diff, bak = remove_hooks_file_entry(adapter, hooks_path, dry_run=dry_run)
                    res["hooks"] = {
                        "path": str(hooks_path),
                        "changed": changed,
                        "status": "removed" if changed else "not found",
                    }
                except Exception as exc:
                    res["hooks"] = {"path": str(hooks_path), "error": str(exc), "changed": False}

        results.append(res)

    return results
