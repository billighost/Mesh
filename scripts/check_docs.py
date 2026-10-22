#!/usr/bin/env python3
"""
scripts/check_docs.py — Automated verification of Mesh documentation consistency.

Validates:
1. Version stamps in docs match package version (or known historical release notes).
2. Internal markdown relative links and anchors resolve.
3. CLI commands, environment variables, MCP tools, and HTTP routes in the code
   are present in DOCS.md.
4. CLI and MCP tool names mentioned in the docs exist in the code.
5. The README rules block matches its source-of-truth file byte-for-byte.
6. Field-test sections state the tested version.
7. docs/field-test/PUBLIC_MANIFEST.json matches the published files.

Exit code: 0 if valid, 1 if any check fails.
"""

from __future__ import annotations

import hashlib
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

CORE_DOCS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "DOCS.md",
    REPO_ROOT / "ARCHITECTURE.md",
    REPO_ROOT / "DEVELOPER_GUIDE.md",
    REPO_ROOT / "CHANGELOG.md",
    REPO_ROOT / "SECURITY.md",
    REPO_ROOT / "THREAT_MODEL.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "examples" / "README.md",
    REPO_ROOT / "docs" / "field-test" / "README.md",
    REPO_ROOT / "docs" / "AGENT_QA.md",
]

DOCS_FILE = REPO_ROOT / "DOCS.md"
RULES_FILE = REPO_ROOT / "mesh" / "skills" / "mesh-context-layer" / "rules_block.md"
PUBLIC_MANIFEST = REPO_ROOT / "docs" / "field-test" / "PUBLIC_MANIFEST.json"

# Variables read by Mesh or its documented integrations. Extracted from mesh/*.py
# at check time; this seed list covers names constructed dynamically or read by
# third-party clients.
EXTRA_ENV_VARS = {
    "NO_COLOR",
    "FORCE_COLOR",
    "XDG_CONFIG_HOME",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
}

# CLI command names registered in mesh.cli.
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


def _github_anchor(heading: str) -> str:
    """Approximate GitHub's heading slug for in-repo anchors."""
    text = heading.strip().lower()
    text = re.sub(r"`|\*|_|~|\[|\]|\(|\)|<|>|!|\"|'|:|;|,|\.|\?|/|\\|\||&|=|\+", "", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text).strip("-")


def _markdown_anchors(path: pathlib.Path) -> set[str]:
    content = path.read_text(encoding="utf-8")
    anchors: set[str] = set()
    in_fence = False
    for line in content.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            anchors.add(_github_anchor(match.group(2)))
    return anchors


def check_markdown_links(errors: list[str]) -> None:
    """Verify relative file links and anchors in core docs resolve."""
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
            # Skip external URLs, mailto, images, and file:// links.
            if target.startswith(("http://", "https://", "mailto:", "file://")):
                continue
            # Split the fragment and query before resolving the path.
            file_part, _, anchor = target.partition("#")
            file_part = file_part.split("?", 1)[0]
            if not file_part:
                target_path = doc
            else:
                target_path = (doc.parent / file_part).resolve()
                if not target_path.exists():
                    errors.append(f"[{rel_doc}] Broken relative link '{target}' -> target '{file_part}' not found.")
                    continue
            if anchor and target_path.suffix.lower() == ".md" and target_path.is_file():
                if anchor not in _markdown_anchors(target_path):
                    errors.append(f"[{rel_doc}] Broken anchor '{target}' -> no heading '{anchor}' in '{file_part or rel_doc}'.")


def _code_blob() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in (REPO_ROOT / "mesh").rglob("*.py")
    )


def check_code_surface_in_docs(errors: list[str]) -> None:
    """Every CLI command, env var, MCP tool, and HTTP route must appear in DOCS.md."""
    if not DOCS_FILE.exists():
        errors.append("Missing DOCS.md.")
        return
    docs = DOCS_FILE.read_text(encoding="utf-8")
    blob = _code_blob()

    for command in sorted(REGISTERED_CLI_COMMANDS):
        pattern = re.compile(r"mesh\s+" + re.escape(command) + r"(?![\w-])")
        if not pattern.search(docs):
            errors.append(f"[DOCS.md] CLI command 'mesh {command}' is registered in code but not documented.")

    env_vars = set(re.findall(r"\bMESH_[A-Z0-9_]+\b", blob)) | EXTRA_ENV_VARS
    for name in sorted(env_vars):
        if name not in docs:
            errors.append(f"[DOCS.md] Environment variable '{name}' is read by code but not documented.")

    for tool in sorted(get_mcp_tool_names()):
        if tool not in docs:
            errors.append(f"[DOCS.md] MCP tool '{tool}' exists in code but is not documented.")

    http_source = (REPO_ROOT / "mesh" / "http_server.py").read_text(encoding="utf-8")
    routes = set(re.findall(r"@app\.(?:get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]", http_source))
    for route in sorted(routes):
        if route not in docs:
            errors.append(f"[DOCS.md] HTTP route '{route}' exists in code but is not documented.")


def check_rules_block(errors: list[str]) -> None:
    """README's embedded rules block must equal the source file."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    marker = "### Rules block (verbatim source of truth)"
    start = readme.find(marker)
    if start == -1:
        errors.append("[README.md] Missing 'Rules block (verbatim source of truth)' section.")
        return
    fence_start = readme.find("```", start)
    if fence_start == -1:
        errors.append("[README.md] Rules block section has no fenced code block.")
        return
    fence_start = readme.find("\n", fence_start) + 1
    fence_end = readme.find("```", fence_start)
    if fence_end == -1:
        errors.append("[README.md] Rules block fenced code block is not closed.")
        return
    embedded = readme[fence_start:fence_end].strip()
    if not RULES_FILE.exists():
        errors.append("Missing rules_block.md source file.")
        return
    source = RULES_FILE.read_text(encoding="utf-8").strip()
    if embedded != source:
        errors.append(
            "[README.md] Embedded rules block differs from mesh/skills/mesh-context-layer/rules_block.md."
        )


def _sections(content: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) sections."""
    sections: list[tuple[str, str]] = []
    heading = ""
    body: list[str] = []
    in_fence = False
    for line in content.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and re.match(r"^#{1,6}\s+", line):
            sections.append((heading, "\n".join(body)))
            heading = line
            body = []
        else:
            body.append(line)
    sections.append((heading, "\n".join(body)))
    return sections


def check_field_test_version_labels(errors: list[str]) -> None:
    """Sections quoting field-test numbers must state the tested version."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    number_pattern = re.compile(r"\b\d+(?:\.\d+)?\s*(?:seconds?|s)\b|\b\d+\s*(?:out of|/)\s*\d+\b")
    for heading, body in _sections(readme):
        lower = (heading + "\n" + body).lower()
        if "field test" not in lower and "probe" not in lower:
            continue
        if number_pattern.search(body) and VERSION not in (heading + "\n" + body):
            errors.append(
                f"[README.md] Field-test section '{heading.strip()[:60]}' quotes numbers without the tested version '{VERSION}'."
            )


def check_public_manifest(errors: list[str]) -> None:
    """Verify the published field-test subset against its checksum manifest."""
    if not PUBLIC_MANIFEST.exists():
        errors.append("[docs/field-test] Missing PUBLIC_MANIFEST.json.")
        return
    try:
        manifest = json.loads(PUBLIC_MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"[docs/field-test/PUBLIC_MANIFEST.json] Invalid JSON: {exc}")
        return
    root = PUBLIC_MANIFEST.parent
    listed: set[str] = set()
    for entry in manifest.get("files", []):
        rel = entry.get("path", "")
        listed.add(rel)
        target = root / rel
        if not target.is_file():
            errors.append(f"[docs/field-test/PUBLIC_MANIFEST.json] Listed file is missing: '{rel}'.")
            continue
        data = target.read_bytes()
        actual_size = len(data)
        actual_sha = hashlib.sha256(data).hexdigest()
        data_lf = data.replace(b"\r\n", b"\n")
        lf_size = len(data_lf)
        lf_sha = hashlib.sha256(data_lf).hexdigest()

        expected_size = entry.get("size_bytes")
        expected_sha = entry.get("sha256")

        if actual_size != expected_size and lf_size != expected_size:
            errors.append(f"[docs/field-test/PUBLIC_MANIFEST.json] Size mismatch for '{rel}'.")
        if actual_sha != expected_sha and lf_sha != expected_sha:
            errors.append(f"[docs/field-test/PUBLIC_MANIFEST.json] SHA-256 mismatch for '{rel}'.")

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in ("MANIFEST.json", "PUBLIC_MANIFEST.json"):
            continue
        if rel not in listed:
            errors.append(f"[docs/field-test/PUBLIC_MANIFEST.json] Published file is not listed: '{rel}'.")


def check_cli_references(errors: list[str]) -> None:
    """Verify CLI subcommand mentions in docs match real commands."""
    cmd_pattern = re.compile(r"`mesh\s+([a-z][a-z0-9_-]*)(?:\s+[^`]+)?`")
    ignored_subcommands = {"--help", "--version", "-y", "--yes", "--dir", "--apply"}

    for doc in [
        REPO_ROOT / "README.md",
        REPO_ROOT / "DOCS.md",
        REPO_ROOT / "DEVELOPER_GUIDE.md",
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "CONTRIBUTING.md",
    ]:
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
    check_code_surface_in_docs(errors)
    check_rules_block(errors)
    check_field_test_version_labels(errors)
    check_public_manifest(errors)

    if errors:
        print(f"\n[FAIL] Documentation verification found {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("\n[OK] All documentation consistency checks passed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
