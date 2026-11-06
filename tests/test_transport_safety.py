"""No policy downgrade or duplicate execution after uncertain transport outcomes."""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def proxy(monkeypatch):
    from mesh import mcp_server
    from mesh import daemon
    monkeypatch.setenv("MESH_NAMESPACE", "example")
    monkeypatch.setenv("MESH_AGENT_ID", "test-agent")
    monkeypatch.setenv("MESH_NO_DAEMON_PROXY", "0")
    monkeypatch.setattr(mcp_server, "_STDIO_SERVER_ACTIVE", True)
    monkeypatch.setattr(mcp_server, "_DAEMON_AVAILABLE", False)
    monkeypatch.setattr(mcp_server, "_DAEMON_CHECK_TS", 0.0)
    monkeypatch.setattr(mcp_server, "_DAEMON_CONN", None)
    monkeypatch.setattr(daemon, "verify_daemon_identity", lambda **kwargs: {"identity_verified": True, "operation_receipts_verified": True})
    monkeypatch.setattr(mcp_server, "get_auth_headers", lambda: {"Authorization": "Bearer YOUR_TOKEN_HERE"})
    return mcp_server


def fake_connection(monkeypatch, server, raw, status=200, fail_send=False):
    requests = []
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs):
            pass
        def connect(self):
            pass
        def request(self, method, path, body=None, headers=None):
            requests.append(json.loads(body))
            if fail_send:
                raise ConnectionResetError("partial request")
        def getresponse(self):
            class Response:
                def read(self, limit=-1):
                    return raw if limit < 0 else raw[:limit]
            response = Response()
            response.status = status
            return response
        def close(self):
            pass
    monkeypatch.setattr(server.http.client, "HTTPConnection", Connection)
    return requests


@pytest.mark.parametrize("raw", [b"{}", b'{"payload":null}', b'{"payload":{}}', b"", b"not JSON"])
def test_sent_mutation_with_unconfirmed_response_never_falls_back(proxy, monkeypatch, raw):
    requests = fake_connection(monkeypatch, proxy, raw)
    monkeypatch.setattr(proxy, "get_mesh", lambda **kwargs: pytest.fail("Uncertain write was reexecuted locally"))
    result = proxy._execute_tool_sync("mesh_task_create", {"title": "fake task", "operation_id": "original-id"})
    assert result["status"] == "unknown"
    assert result["operation_id"] == "original-id"
    assert result["namespace"] == "example"
    assert len(requests) == 1


def test_partial_mutation_send_keeps_original_id(proxy, monkeypatch):
    requests = fake_connection(monkeypatch, proxy, b"", fail_send=True)
    result = proxy._try_proxy_via_daemon("mesh_task_complete", {"item_id": "task-1", "summary": "fake summary", "operation_id": "original-id"})
    assert result["status"] == "unknown"
    assert result["operation_id"] == "original-id"
    assert len(requests) == 1


def test_local_read_only_rejects_before_daemon(proxy, monkeypatch):
    monkeypatch.setenv("MESH_READ_ONLY", "1")
    requests = fake_connection(monkeypatch, proxy, b'{"payload":{"status":"stored"}}')
    result = proxy._execute_tool_sync("mesh_learn", {"content": "fake fact"})
    assert result["status"] == "error"
    assert requests == []


def test_proxy_forwards_private_and_ownership_restrictions(proxy, monkeypatch):
    monkeypatch.setenv("MESH_MCP_INCLUDE_PRIVATE", "1")
    monkeypatch.setenv("MESH_BLOCK_PRIVATE_OVERRIDE", "1")
    monkeypatch.setenv("MESH_ENFORCE_AGENT_OWNERSHIP", "1")
    requests = fake_connection(monkeypatch, proxy, b'{"payload":{"results":[]}}')
    result = proxy._execute_tool_sync("mesh_recall", {"query": "fake port", "include_private": True})
    assert result == {"results": []}
    assert requests[0]["arguments"]["include_private"] is False
    assert requests[0]["client_policy"] == {"include_private": False, "read_only": False, "enforce_agent_ownership": True}


def test_scope_denial_happens_before_proxy(proxy, monkeypatch):
    monkeypatch.setenv("MESH_AGENT_SCOPES", json.dumps({"test-agent": {"namespaces": ["other"]}}))
    requests = fake_connection(monkeypatch, proxy, b'{"payload":{"results":[]}}')
    result = proxy._execute_tool_sync("mesh_recall", {"query": "fake port"})
    assert result["status"] == "error"
    assert requests == []


@pytest.mark.parametrize("raw,status,expected", [(b'{"detail":"error"}', 500, "unknown"), (b'{"detail":"busy","not_admitted":true}', 503, "busy")])
def test_proxy_distinguishes_unknown_server_error_from_nonadmission(proxy, monkeypatch, raw, status, expected):
    fake_connection(monkeypatch, proxy, raw, status=status)
    result = proxy._try_proxy_via_daemon("mesh_forget", {"memory_id": "12345678", "operation_id": "original-id"})
    assert result["status"] == expected


@pytest.mark.parametrize("raw", [b"", b"{}", b"null", b"[]"])
def test_cli_success_without_confirmation_is_truthy_unknown(monkeypatch, raw):
    from mesh import cli, daemon
    calls = []
    class Connection:
        def request(self, *args, **kwargs):
            calls.append(args)
        def getresponse(self):
            class Response:
                status = 200
                def read(self, limit=-1):
                    return raw
            return Response()
    monkeypatch.setenv("MESH_NO_DAEMON_PROXY", "0")
    monkeypatch.setattr(daemon, "verify_daemon_identity", lambda **kwargs: True)
    monkeypatch.setattr(daemon, "get_auth_headers", lambda: {})
    monkeypatch.setattr(cli, "_get_pooled_conn", lambda *args, **kwargs: Connection())
    result = cli._daemon_request("/learn", {"content": "fake fact", "operation_id": "original-id"}, host="127.0.0.1", port=1)
    assert result and result["_daemon_lost"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("literal", ["0", "false", "no"])
def test_existing_namespace_lock_literals_remain_locked(monkeypatch, literal):
    from mesh.audit import validate_namespace
    monkeypatch.setenv("MESH_LOCKED_NAMESPACE", literal)
    assert validate_namespace(literal) == literal
    with pytest.raises(ValueError):
        validate_namespace("other")
