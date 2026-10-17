"""
mesh/skills.py — Agent skill installer for mesh-context-layer.

Copies the bundled mesh-context-layer SKILL.md (and any sibling files) into
every AI coding-agent skill folder detected on this machine.

Supported agents (global skill directories):
  - Antigravity / Gemini CLI IDE   ~/.gemini/config/skills/
                                   ~/.agents/skills/           (junction target)
  - Gemini CLI                     ~/.gemini/skills/
  - Claude Code                    ~/.claude/skills/
  - Cursor                         %APPDATA%/Roaming/Cursor/User/skills/  (Win)
                                   ~/Library/Application Support/Cursor/User/skills/ (mac)
                                   ~/.config/cursor/User/skills/ (Linux)
  - OpenAI Codex CLI               ~/.codex/skills/
  - OpenCode                       ~/.config/opencode/skills/
                                   ~/.agents/skills/
  - Windsurf (Cascade)             ~/.codeium/windsurf/skills/
  - Generic .agents standard       ~/.agents/skills/
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Bundled skill source — lives next to this file inside the installed package
# ---------------------------------------------------------------------------

_SKILL_NAME = "mesh-context-layer"
_SKILLS_DIR = Path(__file__).parent / "skills"


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


# ---------------------------------------------------------------------------
# Agent skill directory resolution
# ---------------------------------------------------------------------------

def _candidate_skill_roots() -> list[tuple[str, Path]]:
    """
    Return a deduplicated list of (agent_label, skills_root_path) tuples for
    every AI coding agent detected on this machine.  Only includes directories
    whose *parent* already exists (we don't create new ~/.config/ trees).
    """
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", "")) if os.name == "nt" else None
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))

    candidates: list[tuple[str, Path]] = [
        # ── Antigravity / Gemini CLI IDE (global config) ──────────────────
        ("Antigravity / Gemini CLI IDE", home / ".gemini" / "config" / "skills"),

        # ── Gemini CLI ────────────────────────────────────────────────────
        ("Gemini CLI", home / ".gemini" / "skills"),

        # ── Claude Code ───────────────────────────────────────────────────
        ("Claude Code", home / ".claude" / "skills"),

        # ── Cursor (platform-specific) ────────────────────────────────────
        *(
            [("Cursor", appdata / "Roaming" / "Cursor" / "User" / "skills")]
            if appdata
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

        # ── OpenAI Codex CLI ──────────────────────────────────────────────
        ("Codex CLI", home / ".codex" / "skills"),

        # ── OpenCode ──────────────────────────────────────────────────────
        ("OpenCode", xdg_config / "opencode" / "skills"),

        # ── Windsurf / Cascade ────────────────────────────────────────────
        ("Windsurf (Cascade)", home / ".codeium" / "windsurf" / "skills"),

        # ── Generic .agents standard (also covers OpenCode / Codex fallback)
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
        # Only include if the parent directory already exists on disk
        if path.parent.exists() or path.exists():
            seen.add(resolved)
            result.append((label, path))

    return result


# ---------------------------------------------------------------------------
# Core installer
# ---------------------------------------------------------------------------

def install_skill(
    target_skills_root: Path,
    *,
    overwrite: bool = True,
    dry_run: bool = False,
) -> tuple[str, bool]:
    """
    Copy the bundled mesh-context-layer skill folder into *target_skills_root*.

    Returns (status_message, was_changed).
    """
    src = _skill_source()
    dst = target_skills_root / _SKILL_NAME

    if dst.exists() and not overwrite:
        return f"already installed at {dst} (skipped)", False

    if dry_run:
        return f"would install to {dst}", True

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return f"installed -> {dst}", True


def install_all(
    *,
    assume_yes: bool = False,
    dry_run: bool = False,
    overwrite: bool = True,
    permission_fn=None,
) -> list[dict]:
    """
    Detect all agent skill directories and install the skill into each one.

    Args:
        assume_yes: Skip interactive y/n prompts.
        dry_run: Preview what would happen without writing anything.
        overwrite: Overwrite an already-installed copy.
        permission_fn: Optional callable(description, assume_yes) -> bool.
                       Falls back to a simple input() prompt if None.

    Returns:
        List of result dicts with keys: agent, path, status, changed.
    """
    candidates = _candidate_skill_roots()
    results: list[dict] = []

    if not candidates:
        results.append({
            "agent": "—",
            "path": None,
            "status": "No agent skill directories detected on this machine.",
            "changed": False,
        })
        return results

    for label, skills_root in candidates:
        dst = skills_root / _SKILL_NAME

        # Skip if already installed and overwrite is False
        if dst.exists() and not overwrite:
            results.append({
                "agent": label,
                "path": str(skills_root),
                "status": "already installed (skipped)",
                "changed": False,
            })
            continue

        action_verb = "update" if dst.exists() else "install"
        prompt_desc = (
            f"{action_verb} the Mesh agent skill into {label} skill folder\n"
            f"      ({skills_root / _SKILL_NAME})"
        )

        # Ask permission
        if permission_fn is not None:
            approved = permission_fn(prompt_desc, assume_yes=assume_yes)
        elif assume_yes:
            approved = True
        else:
            try:
                reply = input(f"  Allow mesh to {prompt_desc}? [y/N] ").strip().lower()
                approved = reply in ("y", "yes")
            except (KeyboardInterrupt, EOFError):
                print()
                approved = False

        if not approved:
            results.append({
                "agent": label,
                "path": str(skills_root),
                "status": "skipped (user declined)",
                "changed": False,
            })
            continue

        try:
            msg, changed = install_skill(
                skills_root,
                overwrite=overwrite,
                dry_run=dry_run,
            )
            results.append({
                "agent": label,
                "path": str(skills_root),
                "status": msg,
                "changed": changed,
            })
        except Exception as exc:
            results.append({
                "agent": label,
                "path": str(skills_root),
                "status": f"ERROR: {exc}",
                "changed": False,
            })

    return results


def get_installed_locations() -> list[Path]:
    """Return all paths where the mesh-context-layer skill is currently installed."""
    installed = []
    for _, skills_root in _candidate_skill_roots():
        dst = skills_root / _SKILL_NAME
        if dst.is_dir() and (dst / "SKILL.md").is_file():
            installed.append(dst)
    return installed
