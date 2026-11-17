"""Source discovery must work without executing Mesh or optional dependency imports."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.schema_sources import read_cli_commands, read_http_routes, read_mcp_tool_schemas, read_version


def _sources(tmp_path: Path) -> Path:
    mesh = tmp_path / "mesh"
    mesh.mkdir()
    (mesh / "version.py").write_text('raise RuntimeError("must not import")\nVERSION = "1.2.3"\n', encoding="utf-8")
    (mesh / "mcp_server.py").write_text(
        'raise RuntimeError("must not import")\n'
        '_MCP_TOOLS = [types.Tool(name="mesh_sample", inputSchema={"properties": {"query": {}, "n": {}}})]\n',
        encoding="utf-8",
    )
    (mesh / "cli.py").write_text(
        'raise RuntimeError("must not import")\n_COMMAND_ALIASES = {"find": "ask"}\n'
        'def _get_command_mapping():\n    return {"ask": handler}\n',
        encoding="utf-8",
    )
    (mesh / "http_server.py").write_text(
        'raise RuntimeError("must not import")\n@app.post("/recall")\ndef recall(): pass\n'
        '@app.api_route("/identity", methods=["GET", "POST"])\nasync def identity(): pass\n',
        encoding="utf-8",
    )
    return tmp_path


def test_source_discovery_never_executes_modules(tmp_path):
    root = _sources(tmp_path)
    assert read_version(root) == "1.2.3"
    assert read_mcp_tool_schemas(root) == {"mesh_sample": {"query", "n"}}
    assert {"ask", "find"} <= read_cli_commands(root)
    assert read_http_routes(root) == {"/recall", "/identity"}


@pytest.mark.parametrize(
    "declarations",
    [
        '[types.Tool(name="mesh_sample", inputSchema={"properties": {}}), types.Tool(name="mesh_sample", inputSchema={"properties": {}})]',
        '[types.Tool(name=dynamic_name, inputSchema={"properties": {}})]',
        '[types.Tool(name="mesh_sample", inputSchema={"properties": dynamic_properties})]',
        '[types.Tool(name="mesh_sample", inputSchema={"properties": {"query": {}, "query": {}}})]',
        '[types.Tool(name="mesh_sample", inputSchema={"properties": {}, **dynamic_fields})]',
        'make_tools()',
    ],
)
def test_dynamic_or_ambiguous_schemas_fail_closed(tmp_path, declarations):
    root = _sources(tmp_path)
    (root / "mesh" / "mcp_server.py").write_text("_MCP_TOOLS = " + declarations, encoding="utf-8")
    with pytest.raises(ValueError):
        read_mcp_tool_schemas(root)


def test_duplicate_schema_assignment_fails_closed(tmp_path):
    root = _sources(tmp_path)
    path = root / "mesh" / "mcp_server.py"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("_MCP_TOOLS = dynamic_tools\n")
    with pytest.raises(ValueError, match="Ambiguous duplicate"):
        read_mcp_tool_schemas(root)


def test_static_schema_discovery_matches_runtime():
    from mesh.mcp_server import _MCP_TOOLS

    root = Path(__file__).resolve().parents[1]
    expected = {tool.name: set(tool.inputSchema.get("properties", {})) for tool in _MCP_TOOLS}
    assert read_mcp_tool_schemas(root) == expected


@pytest.mark.parametrize("name", ["check_skill.py", "check_docs.py"])
def test_checkers_run_without_site_packages(name):
    root = Path(__file__).resolve().parents[1]
    process = subprocess.run(
        [sys.executable, "-S", str(root / "scripts" / name)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr
