"""Public mutation surfaces preserve caller identity checks and indexing waits."""
from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from mesh import http_server, memory, mcp_server
from tests.test_production_storage import fact, isolated_storage as isolated_storage


@pytest.fixture
def replacement(monkeypatch):
    monkeypatch.setenv("MESH_FORCE_HASH_EMBED", "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    monkeypatch.setenv("MESH_NO_DAEMON_PROXY", "1")
    mesh = memory.Mesh("surface-identity", agent_id="test-agent")
    fact(mesh.store)
    old = mesh.store.get("fact-1")
    mesh.store.delete("fact-1")
    fact(mesh.store)
    return mesh, {"expected_revision": old["revision"], "expected_incarnation": old["incarnation"],
                  "expected_store_generation": old["store_generation"]}


@pytest.mark.parametrize("action", ["update", "approve", "forget"])
def test_rest_mutations_refuse_a_reused_id(replacement, monkeypatch, action):
    mesh, expected = replacement
    before = mesh.store.get("fact-1")
    monkeypatch.setattr(http_server, "get_mesh", lambda *args, **kwargs: mesh)
    request_type, handler = {
        "update": (http_server.UpdateRequest, http_server.update),
        "approve": (http_server.ApproveRequest, http_server.approve_memory_endpoint),
        "forget": (http_server.ForgetRequest, http_server.forget),
    }[action]
    arguments = {"memory_id": "fact-1", **expected}
    if action == "update":
        arguments["content"] = "This stale edit must not replace the current fact."
    with pytest.raises(http_server.HTTPException):
        handler(request_type(**arguments))
    assert mesh.store.get("fact-1") == before


def test_rest_delete_refuses_a_reused_id(replacement, monkeypatch):
    mesh, expected = replacement
    monkeypatch.setattr(http_server, "get_mesh", lambda *args, **kwargs: mesh)
    with pytest.raises(http_server.HTTPException):
        http_server.delete_memory_rest("fact-1", namespace=mesh.namespace, **expected)
    assert mesh.store.get("fact-1") is not None


@pytest.mark.parametrize("action", ["mesh_update", "mesh_approve", "mesh_forget"])
def test_logical_mcp_mutations_refuse_a_reused_id(replacement, monkeypatch, action):
    mesh, expected = replacement
    before = mesh.store.get("fact-1")
    monkeypatch.setattr(mcp_server, "get_mesh", lambda *args, **kwargs: mesh)
    arguments = {"memory_id": "fact-1", "namespace": mesh.namespace, "operation_id": "refused-stale-mutation", **expected}
    if action == "mesh_update":
        arguments["content"] = "This stale edit must not replace the current fact."
    try:
        result = mcp_server._execute_tool_sync(action, arguments, agent_id="test-agent", allow_daemon_proxy=False)
    except ValueError as exc:
        assert "incarnation" in str(exc)
        assert exc.operation_id == "refused-stale-mutation"
    else:
        assert result["status"] == "error" and "incarnation" in result["error"]
        assert result["operation_id"] == "refused-stale-mutation"
    assert mesh.store.get("fact-1") == before
    receipt = mesh.store.get_operation_receipt("refused-stale-mutation", "local-user")
    if receipt is not None:
        # A recorded rejection is safe to reconcile; it must describe no effect.
        assert receipt["result"] == {"status": "error"}


@pytest.mark.parametrize("revision", [True, 0, -1, 1.5, "1"])
def test_rest_identity_revision_is_a_strict_positive_integer(revision):
    with pytest.raises(ValidationError):
        http_server.UpdateRequest(memory_id="fact-1", expected_revision=revision)


@pytest.mark.parametrize("field", ["expected_incarnation", "expected_store_generation"])
def test_rest_identity_tokens_reject_malformed_values(field):
    with pytest.raises(ValidationError):
        http_server.UpdateRequest(memory_id="fact-1", **{field: "wrong"})
    assert getattr(http_server.UpdateRequest(memory_id="fact-1", **{field: uuid.uuid4().hex}), field)


def test_rest_learn_forwards_index_wait_options(replacement, monkeypatch):
    mesh, _ = replacement
    observed = {}

    def learn(**arguments):
        observed.update(arguments)
        return {"memory_id": "accepted", "indexing_state": "pending"}

    monkeypatch.setattr(mesh, "learn", learn)
    monkeypatch.setattr(http_server, "get_mesh", lambda *args, **kwargs: mesh)
    result = http_server.learn(http_server.LearnRequest(content="The cedar endpoint uses port 7443.",
                                                      wait_for_indexed=True, index_timeout=0.25))
    assert result["status"] == "ok"
    assert observed["wait_for_indexed"] is True and observed["index_timeout"] == 0.25
