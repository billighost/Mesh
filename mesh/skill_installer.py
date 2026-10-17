"""
mesh/skill_installer.py — Agent skill installer for mesh-context-layer.

Copies the bundled mesh-context-layer SKILL.md (and any sibling files) into
every AI coding-agent skill folder detected on this machine.

Supported agents (verified global skill directories):
  - Antigravity / Gemini CLI IDE   ~/.gemini/config/skills/
  - Gemini CLI                     ~/.gemini/skills/
  - Claude Code                    ~/.claude/skills/
  - Cursor                         ~/.cursor/skills/
                                   %APPDATA%/Cursor/User/skills/ (Win fallback)
                                   ~/Library/Application Support/Cursor/User/skills/ (macOS fallback)
                                   ~/.config/cursor/User/skills/ (Linux fallback)
  - OpenAI Codex CLI               ~/.codex/skills/
  - OpenCode                       ~/.config/opencode/skills/
  - Windsurf (Cascade)             ~/.codeium/windsurf/skills/
  - Generic .agents standard       ~/.agents/skills/
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from .version import VERSION

# ---------------------------------------------------------------------------
# Bundled skill source — lives next to this file inside the installed package
# ---------------------------------------------------------------------------

_SKILL_NAME = "mesh-context-layer"
_SKILLS_DIR = Path(__file__).parent / "skills"
_VERSION_MARKER_FILE = ".mesh-skill-version"


def _skill_source() -> Path:
    """Return the path to the bundled mesh-context-layer skill directory."""
    src = _SKILLS_DIR / _SKILL_NAME
    if not src.is_dir():
        raise FileNotFoundError(
            f"Bundled skill directory not found: {src}\n"
            "This usually means mesh-context-layer was not installed correctly.\n"
            "Try: pip install --force-reinstall mesh-context-layer"
        )
    return src


def _is_symlink_or_junction(path: Path) -> bool:
    """Return True if `path` is a POSIX/Windows symlink or a Windows NTFS junction."""
    if path.is_symlink():
        return True
    if hasattr(path, "is_junction"):
        try:
            if path.is_junction():  # Python 3.12+
                return True
        except OSError:
            pass
    if os.name == "nt":
        try:
            st = path.lstat()
            # FILE_ATTRIBUTE_REPARSE_POINT == 0x400
            if getattr(st, "st_file_attributes", 0) & 0x400:
                return True
        except OSError:
            pass
    return False


def _remove_path_or_link(path: Path) -> None:
    """Safely remove a directory, symlink, or Windows junction."""
    if _is_symlink_or_junction(path):
        try:
            path.unlink()
            return
        except OSError:
            if os.name == "nt":
                os.rmdir(path)
                return
            raise
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# Agent skill directory resolution
# ---------------------------------------------------------------------------


def _candidate_skill_roots() -> list[tuple[str, Path]]:
    """
    Return a deduplicated list of `(agent_label, skills_root_path)` tuples for
    every AI coding agent detected on this machine. Only includes directories
    whose *parent* already exists (we don't create new `~/.config/` trees).
    """
    home = Path.home()
    appdata_raw = os.environ.get("APPDATA", "").strip() if os.name == "nt" else ""
    appdata = Path(appdata_raw) if appdata_raw else None
    xdg_raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    xdg_config = Path(xdg_raw) if xdg_raw else (home / ".config")

    candidates: list[tuple[str, Path]] = [
        # ── Antigravity (Global scope: ~/.gemini/config/skills/)
        # Verified: https://antigravity.google/docs/skills/
        #           https://codelabs.developers.google.com/getting-started-with-antigravity-skills
        ("Antigravity", home / ".gemini" / "config" / "skills"),

        # ── Gemini CLI (Global scope: ~/.gemini/skills/)
        # Verified: https://geminicli.com/docs/cli/tutorials/skills-getting-started/
        ("Gemini CLI", home / ".gemini" / "skills"),

        # ── Claude Code (Global scope: ~/.claude/skills/)
        # Verified: https://docs.anthropic.com/en/docs/claude-code/skills
        ("Claude Code", home / ".claude" / "skills"),

        # ── Cursor (Global scope: ~/.cursor/skills/ + OS User/skills paths)
        # Verified: https://cursor.com/docs/skills
        ("Cursor", home / ".cursor" / "skills"),
        *(
            # Note: APPDATA on Windows already points to ...\AppData\Roaming
            [("Cursor", appdata / "Cursor" / "User" / "skills")]
            if appdata is not None
            else []
        ),
        *(
            [("Cursor", home / "Library" / "Application Support" / "Cursor" / "User" / "skills")]
            if sys.platform == "darwin"
            else []
        ),
        *(
            [("Cursor", xdg_config / "cursor" / "User" / "skills")]
            if sys.platform.startswith("linux")
            else []
        ),

        # ── OpenAI Codex CLI (Global scope: ~/.codex/skills/)
        # Verified: https://skills-hub.ai/codex-skills
        ("Codex CLI", home / ".codex" / "skills"),

        # ── OpenCode (Global scope: ~/.config/opencode/skills/)
        # Verified: https://opencode.ai/docs/skills/
        ("OpenCode", xdg_config / "opencode" / "skills"),

        # ── Windsurf / Cascade (Global scope: ~/.codeium/windsurf/skills/)
        # Verified: https://www.ui-skills.com/agents/windsurf
        ("Windsurf (Cascade)", home / ".codeium" / "windsurf" / "skills"),

        # ── Generic .agents standard (Global scope: ~/.agents/skills/)
        # Verified: https://cursor.com/docs/skills & https://opencode.ai/docs/skills/
        ("Generic .agents standard", home / ".agents" / "skills"),
    ]

    # Resolve any symlinks / junctions so we don't double-install
    seen: set[Path] = set()
    result: list[tuple[str, Path]] = []
    for label, path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen:
            continue
        # Only include if the parent directory (or the path itself) already exists on disk
        if path.parent.exists() or path.exists() or _is_symlink_or_junction(path):
            seen.add(resolved)
            result.append((label, path))

    return result


# ---------------------------------------------------------------------------
# Core installer
# ---------------------------------------------------------------------------


def _is_already_up_to_date(dst: Path) -> bool:
    """Return True if `dst` has `.mesh-skill-version` matching `VERSION` and `SKILL.md` exists."""
    if not (dst.exists() or _is_symlink_or_junction(dst)):
        return False
    version_file = dst / _VERSION_MARKER_FILE
    skill_md = dst / "SKILL.md"
    if not (version_file.is_file() and skill_md.is_file()):
        return False
    try:
        installed_ver = version_file.read_text(encoding="utf-8").strip()
        return installed_ver == VERSION
    except OSError:
        return False


def install_skill(
    target_skills_root: Path,
    *,
    overwrite: bool = True,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[str, bool]:
    """
    Atomically copy the bundled `mesh-context-layer` skill folder into `target_skills_root`:
      1. If `overwrite=False` and `dst` exists, skip with `"already installed"`.
      2. If `overwrite=True`, `force=False`, and `.mesh-skill-version == VERSION`, skip with `"already up to date"`.
      3. Copy to `dst.with_name(dst.name + ".tmp")`, write `.mesh-skill-version`, then atomically replace `dst`
         (unlinking `dst` first if it is a symlink or Windows junction so `rmtree` never fails).

    Returns `(status_message, was_changed)`.
    """
    src = _skill_source()
    dst = target_skills_root / _SKILL_NAME
    dst_exists = dst.exists() or _is_symlink_or_junction(dst)

    if dst_exists and not overwrite and not force:
        return f"already installed at {dst} (skipped)", False

    if dst_exists and not force and _is_already_up_to_date(dst):
        if dry_run:
            return f"would install to {dst} (already up to date v{VERSION})", True
        return f"already up to date (v{VERSION}) at {dst}", False

    if dry_run:
        return f"would install to {dst}", True

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.with_name(dst.name + ".tmp")
    backup_dst = dst.with_name(dst.name + ".bak")

    if tmp_dst.exists() or _is_symlink_or_junction(tmp_dst):
        _remove_path_or_link(tmp_dst)
    if backup_dst.exists() or _is_symlink_or_junction(backup_dst):
        _remove_path_or_link(backup_dst)

    try:
        shutil.copytree(src, tmp_dst)
        (tmp_dst / _VERSION_MARKER_FILE).write_text(f"{VERSION}\n", encoding="utf-8")

        if _is_symlink_or_junction(dst):
            _remove_path_or_link(dst)
            os.replace(tmp_dst, dst)
        elif dst.exists():
            os.replace(dst, backup_dst)
            try:
                os.replace(tmp_dst, dst)
            except Exception:
                if backup_dst.exists() and not dst.exists():
                    os.replace(backup_dst, dst)
                raise
            finally:
                if backup_dst.exists():
                    _remove_path_or_link(backup_dst)
        else:
            os.replace(tmp_dst, dst)
    finally:
        if tmp_dst.exists() or _is_symlink_or_junction(tmp_dst):
            try:
                _remove_path_or_link(tmp_dst)
            except OSError:
                pass

    return f"installed -> {dst}", True


def install_all(
    *,
    assume_yes: bool = False,
    dry_run: bool = False,
    overwrite: bool = True,
    force: bool = False,
    permission_fn: Callable[[str, bool], bool] | None = None,
) -> list[dict[str, Any]]:
    """
    Detect all agent skill directories and install the skill into each one.
    Never prompts for permission when `dry_run=True` (Phase 4.10).

    Args:
        assume_yes: Skip interactive y/n prompts.
        dry_run: Preview what would happen without prompting or writing anything.
        overwrite: Overwrite an already-installed copy if outdated.
        force: Reinstall even when `.mesh-skill-version` equals `VERSION`.
        permission_fn: Optional `callable(description, assume_yes=...) -> bool`.
                       Defaults to `daemon.request_user_permission`.

    Returns:
        List of result dicts with keys: `agent`, `path`, `status`, `changed`.
    """
    candidates = _candidate_skill_roots()
    results: list[dict[str, Any]] = []

    if not candidates:
        results.append(
            {
                "agent": "—",
                "path": None,
                "status": "No agent skill directories detected on this machine.",
                "changed": False,
            }
        )
        return results

    if permission_fn is None:
        from .daemon import request_user_permission as default_perm_fn

        perm_callable = default_perm_fn
    else:
        perm_callable = permission_fn

    for label, skills_root in candidates:
        dst = skills_root / _SKILL_NAME
        dst_exists = dst.exists() or _is_symlink_or_junction(dst)

        if dst_exists and not overwrite and not force:
            results.append(
                {
                    "agent": label,
                    "path": str(skills_root),
                    "status": "already installed (skipped)",
                    "changed": False,
                }
            )
            continue

        if not dry_run and dst_exists and not force and _is_already_up_to_date(dst):
            results.append(
                {
                    "agent": label,
                    "path": str(skills_root),
                    "status": f"already up to date (v{VERSION})",
                    "changed": False,
                }
            )
            continue

        # dry_run=True must never prompt for permission (Phase 4.10)
        if not dry_run:
            action_verb = "update" if dst_exists else "install"
            prompt_desc = (
                f"{action_verb} the Mesh agent skill into {label} skill folder\n"
                f"      ({skills_root / _SKILL_NAME})"
            )
            try:
                approved = perm_callable(prompt_desc, assume_yes=assume_yes)
            except TypeError:
                approved = perm_callable(prompt_desc, assume_yes)

            if not approved:
                results.append(
                    {
                        "agent": label,
                        "path": str(skills_root),
                        "status": "skipped (user declined)",
                        "changed": False,
                    }
                )
                continue

        try:
            msg, changed = install_skill(
                skills_root,
                overwrite=overwrite,
                dry_run=dry_run,
                force=force,
            )
            results.append(
                {
                    "agent": label,
                    "path": str(skills_root),
                    "status": msg,
                    "changed": changed,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "agent": label,
                    "path": str(skills_root),
                    "status": f"ERROR: {exc}",
                    "changed": False,
                }
            )

    return results


def get_installed_locations() -> list[Path]:
    """Return all paths where the mesh-context-layer skill is currently installed."""
    installed: list[Path] = []
    for _, skills_root in _candidate_skill_roots():
        dst = skills_root / _SKILL_NAME
        if dst.is_dir() and (dst / "SKILL.md").is_file():
            installed.append(dst)
    return installed