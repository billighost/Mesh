"""Read Mesh's published interface names without importing its runtime dependencies."""
from __future__ import annotations

import ast
from pathlib import Path


def _assignment(tree: ast.Module, name: str) -> ast.expr:
    assignments: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            assignments.append(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            if node.value is not None:
                assignments.append(node.value)
    if len(assignments) == 1:
        return assignments[0]
    if len(assignments) > 1:
        raise ValueError(f"Ambiguous duplicate {name} assignment.")
    raise ValueError(f"Source has no static {name} assignment.")


def _dict_field(node: ast.expr, name: str) -> ast.expr:
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):
            raise ValueError("Schema dictionary unpacking is not static.")
        matches = []
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == name:
                matches.append(value)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Duplicate dictionary field {name!r}.")
    raise ValueError(f"Expected a static dictionary field {name!r}.")


def read_version(repo_root: Path) -> str:
    tree = ast.parse((repo_root / "mesh" / "version.py").read_text(encoding="utf-8"))
    value = _assignment(tree, "VERSION")
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str) or not value.value:
        raise ValueError("Package VERSION must be a nonempty string literal.")
    return value.value


def read_mcp_tool_schemas(repo_root: Path) -> dict[str, set[str]]:
    tree = ast.parse((repo_root / "mesh" / "mcp_server.py").read_text(encoding="utf-8"))
    tools = _assignment(tree, "_MCP_TOOLS")
    if not isinstance(tools, (ast.List, ast.Tuple)) or not tools.elts:
        raise ValueError("_MCP_TOOLS must be a nonempty static sequence of Tool declarations.")
    schemas: dict[str, set[str]] = {}
    for declaration in tools.elts:
        if not isinstance(declaration, ast.Call):
            raise ValueError("MCP tool declarations must be static Tool calls.")
        if any(keyword.arg is None for keyword in declaration.keywords):
            raise ValueError("MCP declaration keyword unpacking is not static.")
        keywords = {keyword.arg: keyword.value for keyword in declaration.keywords}
        name = keywords.get("name")
        if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
            raise ValueError("MCP tool names must be string literals.")
        if name.value in schemas:
            raise ValueError(f"Duplicate MCP tool declaration: {name.value}.")
        input_schema = keywords.get("inputSchema")
        if input_schema is None:
            raise ValueError(f"MCP tool {name.value} has no input schema.")
        properties = _dict_field(input_schema, "properties")
        if not isinstance(properties, ast.Dict) or any(
            not isinstance(key, ast.Constant) or not isinstance(key.value, str) for key in properties.keys
        ):
            raise ValueError(f"MCP tool {name.value} properties must have literal string keys.")
        schemas[name.value] = {key.value for key in properties.keys if isinstance(key, ast.Constant)}
        if len(schemas[name.value]) != len(properties.keys):
            raise ValueError(f"MCP tool {name.value} has duplicate property keys.")
    return schemas


def read_cli_commands(repo_root: Path) -> set[str]:
    tree = ast.parse((repo_root / "mesh" / "cli.py").read_text(encoding="utf-8"))
    commands: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_get_command_mapping":
            for statement in node.body:
                if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Dict):
                    commands.update(key.value for key in statement.value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str))
    aliases = _assignment(tree, "_COMMAND_ALIASES")
    if isinstance(aliases, ast.Dict):
        commands.update(key.value for key in aliases.keys if isinstance(key, ast.Constant) and isinstance(key.value, str))
    if not commands:
        raise ValueError("CLI command mapping has no static command names.")
    # These entry points are dispatched before the normal command mapping.
    return commands | {"version", "server-http"}


def read_http_routes(repo_root: Path) -> set[str]:
    tree = ast.parse((repo_root / "mesh" / "http_server.py").read_text(encoding="utf-8"))
    routes: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr not in {"get", "post", "put", "delete", "patch", "api_route"} or not decorator.args:
                continue
            path = decorator.args[0]
            if isinstance(path, ast.Constant) and isinstance(path.value, str):
                routes.add(path.value)
    return routes
