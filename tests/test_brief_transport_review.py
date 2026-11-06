"""Public startup-brief contracts across SDK, MCP, HTTP, and CLI transports."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys

import pytest


INVALID_JSON_OPTIONS = (
    {"max_tokens": 0}, {"max_tokens": 32769}, {"max_tokens": False}, {"max_tokens": True},
    {"max_tokens": 1.9}, {"max_tokens": 1.0}, {"max_tokens": "500"}, {"max_tokens": None},
    {"max_facts": -1}, {"max_facts": 101}, {"max_facts": 200}, {"max_facts": True},
    {"max_facts": 1.9}, {"max_facts": 1.0}, {"max_facts": "20"}, {"max_facts": None},
    {"fact_offset": -1}, {"fact_offset": True}, {"fact_offset": 0.9},
    {"fact_offset": 0.0}, {"fact_offset": "0"}, {"fact_offset": None},
)
INVALID_INTEGER_OPTIONS = (
    {"max_tokens": 0}, {"max_tokens": 32769}, {"max_facts": -1},
    {"max_facts": 101}, {"max_facts": 200}, {"fact_offset": -1},
)
JSON_SURFACES = ("operations", "mcp_local", "http_mcp")
SURFACES = ("sdk", "operations", "mcp_local", "http_rest", "http_mcp", "cli_json")
TARGETS = [(surface, exists) for surface in SURFACES for exists in (False, True) if surface != "sdk" or exists]
PRIVATE_MARKER = "PRIVATE-CONTEXT-MARKER"


@pytest.fixture
def brief_state(tmp_path, monkeypatch):
    from scripts.run_tests import isolated_environment

    inherited = {key: value for key, value in os.environ.items() if not key.startswith("MESH_")}
    environment = isolated_environment(tmp_path / "environment", inherited)
    environment.update(MESH_NO_OUTBOX_WORKER="1", MESH_FORCE_HASH_EMBED="1", MESH_BG_ONNX="0", MESH_MCP_INCLUDE_PRIVATE="0")
    for name in list(os.environ):
        if name.startswith("MESH_"):
            monkeypatch.delenv(name)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    from fastapi.testclient import TestClient
    from mesh import Mesh, audit, http_server, store as storage

    audit.stop_audit_worker()
    monkeypatch.setattr(audit, "_ensure_audit_drain_thread", lambda: None)
    mesh = Mesh(namespace="brief-existing", agent_id="brief-test")
    mesh.store.upsert("public-fact", "The public durable fact uses the production brief contract.", None, {"source_agent": "brief-test"})
    mesh.store.upsert("private-fact", PRIVATE_MARKER, None, {"source_agent": "brief-test"}, local_only=True)
    client = TestClient(http_server.app, base_url="http://127.0.0.1:7701")
    client.headers["Authorization"] = "Bearer " + http_server.get_or_create_auth_token()
    try:
        yield {"mesh": mesh, "client": client, "existing": mesh.namespace, "missing": "brief-missing", "root": Path(environment["MESH_HOME"])}
    finally:
        client.close()
        http_server.shutdown_http_runtime()
        audit.stop_audit_worker()
        audit.flush_audit_log()
        storage._close_all_pooled_connections()
        audit._close_all_audit_connections()
        storage._cleanup_open_lock_fds()
        storage._VECTOR_CACHE.clear()


def call_cli(monkeypatch, namespace, options, output_format="json"):
    from mesh import cli

    argv = ["mesh", "brief", "--namespace", namespace, "--format", output_format, "--no-private"]
    for key, value in options.items():
        argv.extend(["--" + key.replace("_", "-"), str(value)])
    output, errors = io.StringIO(), io.StringIO()
    exit_code = 0
    with monkeypatch.context() as context:
        context.setattr(sys, "argv", argv)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                cli.main()
            except SystemExit as exc:
                exit_code = exc.code
    return exit_code, output.getvalue(), errors.getvalue()


def call_brief(surface, state, monkeypatch, exists, options):
    namespace = state["existing"] if exists else state["missing"]
    if surface == "sdk":
        return state["mesh"].project_brief(**options, include_private=False), None
    if surface == "operations":
        from mesh.operations import execute_tool
        return execute_tool("mesh_project_brief", {"namespace": namespace, **options}, agent_id="brief-test"), None
    if surface == "mcp_local":
        from mesh.mcp_server import _execute_tool_sync
        return _execute_tool_sync("mesh_project_brief", {"namespace": namespace, **options}, agent_id="brief-test", allow_daemon_proxy=False), None
    if surface == "http_rest":
        response = state["client"].get("/brief", params={"namespace": namespace, **options})
        return response.json(), response.status_code
    if surface == "http_mcp":
        response = state["client"].post("/mcp", json={"name": "mesh_project_brief", "arguments": {"namespace": namespace, **options}, "agent_id": "brief-test"})
        outer = response.json()
        return outer.get("payload", outer), response.status_code
    if surface == "cli_json":
        exit_code, output, _ = call_cli(monkeypatch, namespace, options)
        return json.loads(output), exit_code
    raise AssertionError(surface)


def assert_estimates(payload):
    assert payload["budget_scope"] == "brief_markdown"
    assert payload["estimated_tokens"] == (len(payload["brief_markdown"]) + 3) // 4
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert payload["payload_estimated_tokens"] == (len(compact) + 3) // 4


@pytest.mark.parametrize("surface,exists", TARGETS)
@pytest.mark.parametrize("max_tokens", (1, 2, 8, 500, 1500, 2000))
def test_brief_text_and_final_estimates_cover_existing_and_missing_namespaces(brief_state, monkeypatch, surface, exists, max_tokens):
    payload, status = call_brief(surface, brief_state, monkeypatch, exists, {"max_tokens": max_tokens})
    assert status in (None, 0, 200)
    assert "error" not in payload
    assert len(payload["brief_markdown"]) <= max_tokens * 4
    assert PRIVATE_MARKER not in json.dumps(payload)
    assert_estimates(payload)
    assert payload["facts_available"] == int(exists)
    assert payload["untrusted_reference_data"] is True
    if not exists:
        assert payload["namespace_exists"] is False
        assert payload["selected_memory_ids"] == []
        assert payload["next_fact_offset"] is None
        assert payload["facts_included"] == payload["facts_omitted"] == 0
        assert not (brief_state["root"] / "mesh_brief-missing.db").exists()
    if max_tokens == 1:
        assert payload["brief_markdown"] == ""
        assert payload["estimated_tokens"] == 0
        assert payload["payload_estimated_tokens"] > max_tokens
        if exists:
            assert payload["next_fact_offset"] == 0


def test_sdk_rejects_coercible_options_before_storage_reads(brief_state, monkeypatch):
    def forbidden_read():
        raise AssertionError("Invalid brief options reached storage initialization")

    monkeypatch.setattr(brief_state["mesh"].store, "_ensure_initialized", forbidden_read)
    for options in INVALID_JSON_OPTIONS:
        with pytest.raises(ValueError, match=next(iter(options))):
            brief_state["mesh"].project_brief(**options)


@pytest.mark.parametrize("surface", JSON_SURFACES)
@pytest.mark.parametrize("exists", (False, True))
def test_json_brief_options_are_strict_for_existing_and_missing_namespaces(brief_state, monkeypatch, surface, exists):
    for options in INVALID_JSON_OPTIONS:
        payload, _ = call_brief(surface, brief_state, monkeypatch, exists, options)
        assert payload.get("status") == "error", (surface, exists, options, payload)
        assert next(iter(options)) in payload["error"]
    assert not (brief_state["root"] / "mesh_brief-missing.db").exists()


@pytest.mark.parametrize("surface", ("http_rest", "cli_json"))
@pytest.mark.parametrize("exists", (False, True))
def test_parsed_integer_limits_do_not_depend_on_namespace_existence(brief_state, monkeypatch, surface, exists):
    for options in INVALID_INTEGER_OPTIONS:
        payload, status = call_brief(surface, brief_state, monkeypatch, exists, options)
        assert status in (1, 400, 422), (surface, exists, options, status, payload)
        message = payload.get("error") or payload.get("detail")
        assert next(iter(options)) in str(message)
        assert PRIVATE_MARKER not in json.dumps(payload)
    assert not (brief_state["root"] / "mesh_brief-missing.db").exists()


@pytest.mark.parametrize("surface,exists", TARGETS)
def test_brief_bounds_and_zero_fact_selection_remain_valid(brief_state, monkeypatch, surface, exists):
    for options in (
        {"max_tokens": 32768, "max_facts": 100}, {"max_facts": 0}, {"fact_offset": 1000},
        {"fact_offset": 2 ** 63}, {"fact_offset": 2 ** 63 + 1}, {"fact_offset": 10 ** 1000},
    ):
        payload, status = call_brief(surface, brief_state, monkeypatch, exists, options)
        assert status in (None, 0, 200)
        assert "error" not in payload
        assert_estimates(payload)
        if options.get("max_facts") == 0 or "fact_offset" in options:
            assert payload["facts_included"] == 0


@pytest.mark.parametrize("wire", ("1.9", "true", "null"))
def test_http_query_integer_parsing_remains_explicit(brief_state, wire):
    response = brief_state["client"].get("/brief", params={"namespace": brief_state["missing"], "max_tokens": wire})
    assert response.status_code == 422
    valid = brief_state["client"].get("/brief", params={"namespace": brief_state["missing"], "max_tokens": "500"})
    assert valid.status_code == 200
    assert_estimates(valid.json())


@pytest.mark.parametrize("output_format", ("json", "markdown", "antigravity"))
@pytest.mark.parametrize("exists", (False, True))
def test_cli_brief_errors_exit_nonzero_without_exposing_context(brief_state, monkeypatch, exists, output_format):
    namespace = brief_state["existing"] if exists else brief_state["missing"]
    exit_code, output, errors = call_cli(monkeypatch, namespace, {"max_tokens": 0}, output_format)
    assert exit_code == 1
    assert PRIVATE_MARKER not in output + errors
    assert "public durable fact" not in output + errors
    if output_format == "json":
        assert json.loads(output) == {"error": "max_tokens must be an integer between 1 and 32768.", "brief_markdown": ""}
    elif output_format == "antigravity":
        assert json.loads(output) == {"injectSteps": []}
    else:
        assert output == ""
        assert "max_tokens" in errors


def test_mcp_advertises_the_runtime_brief_limits(brief_state):
    from mesh.mcp_server import _MCP_TOOLS

    schema = next(tool.inputSchema for tool in _MCP_TOOLS if tool.name == "mesh_project_brief")
    assert schema["properties"]["max_tokens"]["minimum"] == 1
    assert schema["properties"]["max_tokens"]["maximum"] == 32768
    assert schema["properties"]["max_facts"]["maximum"] == 100
    assert schema["properties"]["fact_offset"]["minimum"] == 0
