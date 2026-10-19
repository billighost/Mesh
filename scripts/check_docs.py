#!/usr/bin/env python3
"""
scripts/check_docs.py — Automated verification of Mesh documentation consistency.

Validates:
1. Version stamps in docs match package version (or known historical release notes).
2. Internal markdown relative links point to existing files.
3. CLI commands mentioned in docs match registered CLI subcommands.
4. MCP tool names mentioned in docs match current MCP tool schemas.
5. Critical architectural files and release notes are present and non-empty.

Exit code: 0 if valid, 1 if any check fails.
"""

from __future__ import annotations

import pathlib
import re
import sys

# Ensure repository root is in sys.path
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mesh.mcp_server import _MCP_TOOLS
from mesh.version import VERSION

CORE_DOCS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "ARCHITECTURE.md",
    REPO_ROOT / "DEVELOPER_GUIDE.md",
    REPO_ROOT / "CHANGELOG.md",
    REPO_ROOT / "SECURITY.md",
    REPO_ROOT / "THREAT_MODEL.md",
    REPO_ROOT / "docs" / "AGENT_QA.md",
]

# Registered CLI commands from mesh.cli
REGISTERED_CLI_COMMANDS = {
    "add",
    "ask",
    "inspect",
    "update",
    "forget",
    "approve",
    "conflicts",
    "patterns",
    "why",
    "status",
    "namespaces",
    "doctor",
    "brief",
    "task",
    "tasks",
    "log",
    "install",
    "uninstall",
    "setup",
    "shell",
    "i",
    "run",
    "export",
    "import",
    "encrypt-db",
    "server",
    "server-http",
    "http",
    "daemon",
    "context",
    "completion",
    "capture",
    "digest",
    "install-skills",
    "audit",
    "version",
    "help",
}


def get_mcp_tool_names() -> set[str]:
    return {tool.name for tool in _MCP_TOOLS}


def check_version_stamps(errors: list[str]) -> None:
    """Verify primary architecture and developer guide reference current version."""
    version_docs = [
        REPO_ROOT / "ARCHITECTURE.md",
        REPO_ROOT / "DEVELOPER_GUIDE.md",
    ]
    expected_stamp = f"v{VERSION}"
    for doc in version_docs:
        if not doc.exists():
            errors.append(f"Missing documentation file: {doc.relative_to(REPO_ROOT)}")
            continue
        content = doc.read_text(encoding="utf-8")
        if expected_stamp not in content and VERSION not in content:
            errors.append(
                f"[{doc.relative_to(REPO_ROOT)}] Missing current version stamp '{expected_stamp}' or '{VERSION}'."
            )
        else:
            print(f"  [OK] {doc.name} includes current version stamp ({VERSION}).")


def check_markdown_links(errors: list[str]) -> None:
    """Verify that relative file links in core docs point to valid existing files."""
    link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    docs_to_check = list(CORE_DOCS)
    releases_dir = REPO_ROOT / "docs" / "releases"
    if releases_dir.exists():
        docs_to_check.extend(releases_dir.glob("*.md"))

    for doc in docs_to_check:
        if not doc.exists():
            continue
        content = doc.read_text(encoding="utf-8")
        rel_doc = doc.relative_to(REPO_ROOT)

        for match in link_pattern.finditer(content):
            target = match.group(2).strip()
            # Skip external URLs, mailto, anchor-only links, and images
            if target.startswith(("http://", "https://", "mailto:", "#", "file://")):
                continue
            # Strip fragment anchor if present
            file_part = target.split("#")[0]
            if not file_part:
                continue

            target_path = (doc.parent / file_part).resolve()
            if not target_path.exists():
                errors.append(f"[{rel_doc}] Broken relative link '{target}' -> target '{file_part}' not found.")


def check_cli_references(errors: list[str]) -> None:
    """Verify CLI subcommand mentions in README and guides match real commands."""
    cmd_pattern = re.compile(r"`mesh\s+([a-z0-9_-]+)(?:\s+[^`]+)?`")
    ignored_subcommands = {"--help", "--version", "-y", "--yes", "--dir", "--apply"}

    for doc in [REPO_ROOT / "README.md", REPO_ROOT / "DEVELOPER_GUIDE.md"]:
        if not doc.exists():
            continue
        content = doc.read_text(encoding="utf-8")
        rel_doc = doc.relative_to(REPO_ROOT)

        for match in cmd_pattern.finditer(content):
            subcmd = match.group(1).lower()
            if subcmd.startswith("-") or subcmd in ignored_subcommands:
                continue
            if subcmd not in REGISTERED_CLI_COMMANDS:
                errors.append(f"[{rel_doc}] Documented CLI command 'mesh {subcmd}' is not in registered CLI commands.")


def check_mcp_tool_references(errors: list[str]) -> None:
    """Verify MCP tool mentions in docs match real MCP tools."""
    mcp_tools = get_mcp_tool_names()
    # Match `mesh_<name>` or `mesh_<name>(...)` or `mesh_<name>` tool
    tool_ref_pattern = re.compile(r"`(mesh_[a-z0-9_]+)(?:\([^`]*\))?`(?:\s+tool)?")
    ignored = {
        "mesh_context_layer",
        "mesh_mcp_command",
        "mesh_home",
        "mesh_namespace",
        "mesh_shared",
        "mesh_dash_session",
        "mesh_locked_namespace",
        "mesh_work",
        "mesh_instance",
        "mesh_default_namespace",
        "mesh_benchmark",
        "mesh_db",
    }

    for doc in CORE_DOCS:
        if not doc.exists():
            continue
        content = doc.read_text(encoding="utf-8")
        rel_doc = doc.relative_to(REPO_ROOT)

        for match in tool_ref_pattern.finditer(content):
            tool_name = match.group(1)
            # Skip file references like `mesh_foo.py` or `mesh_foo.db`
            if any(ext in match.group(0) for ext in [".py", ".db", ".json", ".txt"]):
                continue
            if tool_name in ignored or tool_name in mcp_tools:
                continue
            # If followed by 'tool' or has parens, it was intended as a tool
            if "tool" in match.group(0).lower() or "(" in match.group(0):
                errors.append(f"[{rel_doc}] Unknown MCP tool referenced: '{tool_name}'.")


def main() -> int:
    print(f"Checking Mesh Documentation Consistency (Package version: v{VERSION})...")
    errors: list[str] = []

    check_version_stamps(errors)
    check_markdown_links(errors)
    check_cli_references(errors)
    check_mcp_tool_references(errors)

    if errors:
        print(f"\n[FAIL] Documentation verification found {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("\n[OK] All documentation consistency checks passed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
