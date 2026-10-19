#!/usr/bin/env python3
"""
scripts/check_skill.py — Automated verification of Mesh agent skills, rules, and references.

Validates:
1. Tool names mentioned in SKILL.md, rules_block.md, and references match MCP schemas.
2. Argument names mentioned in tool calls or examples match inputSchema properties.
3. Package version matches skill version stamps.
4. Description length and line count limits (SKILL.md <= 250 lines, rules_block <= 12 lines).

Exit code: 0 if valid, 1 if any check fails.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

# Ensure repository root is in sys.path
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mesh.mcp_server import _MCP_TOOLS
from mesh.version import VERSION

SKILL_DIR = REPO_ROOT / "mesh" / "skills" / "mesh-context-layer"
SKILL_MD = SKILL_DIR / "SKILL.md"
RULES_BLOCK_MD = SKILL_DIR / "rules_block.md"
REFERENCES_DIR = SKILL_DIR / "references"


def get_mcp_tool_schemas() -> dict[str, set[str]]:
    """Extract tool names and valid argument names from MCP tool schemas."""
    schemas: dict[str, set[str]] = {}
    for tool in _MCP_TOOLS:
        props = tool.inputSchema.get("properties", {})
        schemas[tool.name] = set(props.keys())
    return schemas


def check_version_stamp(content: str, filename: str, errors: list[str]) -> None:
    """Verify that file contains the expected package version stamp."""
    expected_stamp = f"v{VERSION}"
    if expected_stamp not in content and VERSION not in content:
        errors.append(f"[{filename}] Missing version stamp '{expected_stamp}' or '{VERSION}'.")


def check_line_and_desc_limits(errors: list[str]) -> None:
    """Validate line count and description length limits."""
    # 1. SKILL.md line count <= 250
    if not SKILL_MD.exists():
        errors.append(f"Missing {SKILL_MD}")
        return

    skill_lines = SKILL_MD.read_text(encoding="utf-8").splitlines()
    if len(skill_lines) > 250:
        errors.append(f"[SKILL.md] Exceeds 250 lines limit: {len(skill_lines)} lines.")
    else:
        print(f"  [OK] SKILL.md length: {len(skill_lines)} lines (<= 250 limit).")

    # Frontmatter check
    text = "\n".join(skill_lines)
    fm_match = re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not fm_match:
        errors.append("[SKILL.md] Missing YAML frontmatter.")
    else:
        fm_text = fm_match.group(1)
        if "name: mesh-context-layer" not in fm_text:
            errors.append("[SKILL.md] Frontmatter missing 'name: mesh-context-layer'.")
        desc_match = re.search(r"description:\s*(?:>-\s*|\"|')?(.*?)(?:\n[a-zA-Z0-9_-]+:|\Z)", fm_text, re.DOTALL)
        if desc_match:
            desc = desc_match.group(1).strip()
            if len(desc) > 1024:
                errors.append(f"[SKILL.md] Description length {len(desc)} exceeds 1024 characters limit.")
            else:
                print(f"  [OK] SKILL.md frontmatter description length: {len(desc)} chars (<= 1024 limit).")
        else:
            errors.append("[SKILL.md] Frontmatter missing 'description'.")

    # 2. rules_block.md line count <= 12
    if RULES_BLOCK_MD.exists():
        rule_lines = RULES_BLOCK_MD.read_text(encoding="utf-8").splitlines()
        if len(rule_lines) > 12:
            errors.append(f"[rules_block.md] Exceeds 12 lines limit: {len(rule_lines)} lines.")
        else:
            print(f"  [OK] rules_block.md length: {len(rule_lines)} lines (<= 12 limit).")


def check_tool_references(
    schemas: dict[str, set[str]],
    errors: list[str],
) -> None:
    """Verify tool names and parameter references across markdown files."""
    md_files = [SKILL_MD, RULES_BLOCK_MD]
    if REFERENCES_DIR.exists():
        md_files.extend(REFERENCES_DIR.glob("*.md"))

    # Regex for tool calls: mesh_<name>(...)
    call_re = re.compile(r"\b(mesh_[a-z0-9_]+)\((.*?)\)", re.DOTALL)
    # Regex for json calls: mesh_<name>({ ... })
    json_call_re = re.compile(r"\b(mesh_[a-z0-9_]+)\s*\(\s*(\{.*?\})\s*\)", re.DOTALL)
    # Regex for any mesh tool name: mesh_[a-z0-9_]+
    any_tool_re = re.compile(r"\b(mesh_[a-z0-9_]+)\b")

    for md_file in md_files:
        if not md_file.exists():
            continue
        rel_path = md_file.relative_to(REPO_ROOT)
        content = md_file.read_text(encoding="utf-8")

        # Check version stamp
        if md_file.name in ("SKILL.md", "tool_reference.md", "examples.md", "troubleshooting.md"):
            check_version_stamp(content, str(rel_path), errors)

        # Check any tool identifier
        for m in any_tool_re.finditer(content):
            tool_name = m.group(1)
            # Ignore false positives like mesh_namespace env var or code identifiers
            if tool_name in ("mesh_context_layer", "mesh_mcp_command", "mesh_home"):
                continue
            if tool_name not in schemas:
                errors.append(f"[{rel_path}] Unknown tool name referenced: '{tool_name}'.")

        # Check call arguments: e.g. mesh_tool(arg1=val1, arg2=val2)
        for m in call_re.finditer(content):
            tool_name = m.group(1)
            arg_str = m.group(2).strip()
            if tool_name not in schemas:
                continue
            if not arg_str or arg_str.startswith("{"):
                continue
            # Extract param names like `query=...` or `max_tokens=500`
            param_matches = re.findall(r"([a-z_][a-z0-9_]*)\s*=", arg_str)
            for param in param_matches:
                if param not in schemas[tool_name]:
                    errors.append(
                        f"[{rel_path}] Invalid argument '{param}' for tool '{tool_name}'. Valid arguments: {sorted(schemas[tool_name])}"
                    )

        # Check json call arguments: e.g. mesh_tool({"arg1": val1})
        for m in json_call_re.finditer(content):
            tool_name = m.group(1)
            json_blob = m.group(2).strip()
            if tool_name not in schemas:
                continue
            try:
                parsed = json.loads(json_blob)
                if isinstance(parsed, dict):
                    for k in parsed.keys():
                        if k not in schemas[tool_name]:
                            errors.append(
                                f"[{rel_path}] Invalid argument key '{k}' in JSON call for '{tool_name}'. Valid: {sorted(schemas[tool_name])}"
                            )
            except Exception:
                # If not strict JSON (e.g. pseudo-json in comment), pass
                pass


def main() -> int:
    print(f"Checking Mesh Skill, Rules, and References (Package version: v{VERSION})...")
    schemas = get_mcp_tool_schemas()
    print(f"  Loaded {len(schemas)} MCP tools from schema.")

    errors: list[str] = []
    check_line_and_desc_limits(errors)
    check_tool_references(schemas, errors)

    if errors:
        print("\n[FAIL] Skill verification failed:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("\n[OK] All skill, rules, and tool reference checks passed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
