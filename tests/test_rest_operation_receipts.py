"""REST and HTTP MCP preserve durable mutation identity, errors, and server policy."""
from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError
from starlette.requests import Request
from fastapi.testclient import TestClient

from mesh import http_server, memory, operations
from mesh.operation_contract import validate_mutation_acknowledgement
from tests.test_production_storage import isolated_storage as isolated_storage


@pytest.fixture(autouse=True)
def rest_runtime(isolated_storage, monkeypatch):
    monkeypatch.setenv("MESH_FORCE_HASH_EMBED", "1")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    monkeypatch.setenv("MESH_NO_DAEMON_PROXY", "1")
    monkeypatch.setenv("MESH_MCP_INCLUDE_PRIVATE", "0")
    http_server.evict_http_mesh_cache()
    operations.evict_mesh_cache()
    yield
    operations.close_operation_brokers()
    http_server.evict_http_mesh_cache()
    operations.evict_mesh_cache()


def payload(response):
    return json.loads(response.body) if isinstance(response, http_server.JSONResponse) else response


def request(document):
    raw = json.dumps(document).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/mcp", "headers": [], "state": {}}, receive)


@pytest.mark.parametrize("action", ["learn", "update", "approve", "forget"])
def test_rest_memory_receipt_replays_the_effect_once(action):
    mesh = memory.Mesh("rest-receipts", agent_id="test-agent")
    learned = mesh.learn("The Cedar cache expires after 90 seconds.", quarantined=action == "approve")
    mid = learned["memory_id"]
    args = {"namespace": mesh.namespace, "agent_id": mesh.agent_id, "operation_id": "rest-original"}
    if action == "learn":
        req = http_server.LearnRequest(content="The Maple deployment uses port 7443.", **args)
        handler = http_server.learn
    else:
        args["memory_id"] = mid
        row = mesh.store.get(mid)
        args.update(expected_revision=row["revision"], expected_incarnation=row["incarnation"],
                    expected_store_generation=row["store_generation"])
        request_type, handler = {
            "update": (http_server.UpdateRequest, http_server.update),
            "approve": (http_server.ApproveRequest, http_server.approve_memory_endpoint),
            "forget": (http_server.ForgetRequest, http_server.forget),
        }[action]
        if action == "update":
            args["content"] = "The Cedar cache expires after 120 seconds."
        req = request_type(**args)
    first = payload(handler(req))
    after = mesh.store.get(mid)
    count = mesh.count()
    second = payload(handler(req))
    assert first["status"] == second["status"] == "ok"
    assert first["operation_id"] == second["operation_id"] == "rest-original"
    assert first["replayed"] is False and second["replayed"] is True
    assert mesh.count() == count and mesh.store.get(mid) == after
    assert first["receipt"] == second["receipt"]
    for result in (first["result"], second["result"]):
        validate_mutation_acknowledgement(result, "rest-original", mesh.namespace,
                                         operation="mesh_" + action, producer_scope="local-user")
    assert "cache expires" not in json.dumps(first["receipt"])


def test_rest_operation_key_reuse_rejects_a_different_request():
    args = {"namespace": "rest-receipts", "agent_id": "test-agent", "operation_id": "rest-original"}
    first = payload(http_server.learn(http_server.LearnRequest(content="The Cedar cache expires after 90 seconds.", **args)))
    refusal = http_server.learn(http_server.LearnRequest(content="The Cedar cache expires after 120 seconds.", **args))
    assert refusal.status_code == 409
    assert payload(refusal)["operation_id"] == "rest-original"
    mesh = memory.Mesh("rest-receipts", agent_id="test-agent")
    assert mesh.count() == 1
    assert mesh.get(first["result"]["memory_id"])["content"] == "The Cedar cache expires after 90 seconds."


def test_rest_unknown_after_commit_keeps_the_original_receipt(monkeypatch):
    req = http_server.LearnRequest(namespace="rest-receipts", agent_id="test-agent", operation_id="rest-original",
                                  content="The Maple deployment uses port 7443.")
    original = operations._receipt_response
    monkeypatch.setattr(operations, "_receipt_response", lambda *args: (_ for _ in ()).throw(RuntimeError("Injected response failure")))
    unknown = http_server.learn(req)
    assert unknown.status_code == 503
    assert payload(unknown)["status"] == "unknown"
    assert payload(unknown)["operation_id"] == "rest-original"
    mesh = memory.Mesh("rest-receipts", agent_id="test-agent")
    assert mesh.count() == 1
    assert mesh.store.get_operation_receipt("rest-original", "local-user") is not None
    monkeypatch.setattr(operations, "_receipt_response", original)
    replay = payload(http_server.learn(req))
    assert replay["replayed"] is True and mesh.count() == 1


@pytest.mark.parametrize("extra", [float("nan"), object()])
def test_rest_wire_encoding_failure_preserves_the_committed_operation(monkeypatch, extra):
    original = operations._receipt_response
    monkeypatch.setattr(operations, "_receipt_response", lambda *args: {**original(*args), "extra": extra})
    http_server.app.dependency_overrides[http_server.verify_request_auth] = lambda: None
    try:
        with TestClient(http_server.app, base_url="http://127.0.0.1:7701") as client:
            args = {"namespace": "rest-receipts", "agent_id": "test-agent", "operation_id": "rest-original",
                    "content": "The Maple deployment uses port 7443."}
            unknown = client.post("/learn", json=args)
            assert unknown.status_code == 503
            assert unknown.json()["status"] == "unknown" and unknown.json()["operation_id"] == "rest-original"
            assert "not_admitted" not in unknown.json()
            mesh = memory.Mesh("rest-receipts", agent_id="test-agent")
            assert mesh.count() == 1 and mesh.store.get_operation_receipt("rest-original", "local-user") is not None
            monkeypatch.setattr(operations, "_receipt_response", original)
            replay = client.post("/learn", json=args)
            assert replay.status_code == 200 and replay.json()["replayed"] is True
            assert mesh.count() == 1
    finally:
        http_server.app.dependency_overrides.clear()


def test_identified_rest_delete_preserves_not_found_status_and_receipt():
    mesh = memory.Mesh("rest-receipts", agent_id="test-agent")
    response = http_server.delete_memory_rest("absent-id", namespace=mesh.namespace, agent_id=mesh.agent_id,
                                              operation_id="rest-original")
    assert response.status_code == 404 and payload(response)["status"] == "not_found"
    assert payload(response)["operation_id"] == "rest-original"
    assert mesh.store.get_operation_receipt("rest-original", "local-user") is not None


@pytest.mark.parametrize("action", ["update", "approve", "forget", "delete"])
def test_rest_private_mutation_refuses_without_a_policy_bypass(action):
    mesh = memory.Mesh("rest-receipts", agent_id="test-agent")
    mid = mesh.learn("The private staging cache expires after 90 seconds.", local_only=True)["memory_id"]
    before = mesh.store.get(mid)
    args = {"namespace": mesh.namespace, "agent_id": mesh.agent_id,
            "memory_id": mid, "operation_id": "rest-original"}
    if action == "delete":
        refusal = http_server.delete_memory_rest(**args)
    else:
        request_type, handler = {
            "update": (http_server.UpdateRequest, http_server.update),
            "approve": (http_server.ApproveRequest, http_server.approve_memory_endpoint),
            "forget": (http_server.ForgetRequest, http_server.forget),
        }[action]
        if action == "update":
            args["content"] = "The cache expires after 120 seconds."
        refusal = handler(request_type(**args))
    assert refusal.status_code == 403, payload(refusal)
    assert payload(refusal)["operation_id"] == "rest-original"
    assert mesh.store.get(mid) == before
    assert mesh.store.get_operation_receipt("rest-original", "local-user") is None


@pytest.mark.parametrize("operation_id", ["", False, 0, [], {}, "a/b", "a\x00b"])
def test_rest_invalid_operation_id_is_rejected(operation_id):
    with pytest.raises(ValidationError):
        http_server.LearnRequest(content="The Cedar cache expires after 90 seconds.", operation_id=operation_id)


@pytest.mark.parametrize("group_commit", ["0", "1"])
def test_http_mcp_known_refusal_is_an_error_result_with_original_id(monkeypatch, group_commit):
    monkeypatch.setenv("MESH_GROUP_COMMIT", group_commit)
    mesh = memory.Mesh("rest-receipts", agent_id="test-agent")
    mid = mesh.learn("The Cedar cache expires after 90 seconds.")["memory_id"]
    old = mesh.store.get(mid)
    mesh.update(mid, content="The Cedar cache expires after 120 seconds.")
    result = asyncio.run(http_server.mcp_proxy(request({"name": "mesh_update", "operation_id": "rest-original",
                       "agent_id": mesh.agent_id, "arguments": {"namespace": mesh.namespace, "memory_id": mid,
                       "content": "This stale edit must not change the cache.", "expected_revision": old["revision"]}})))
    assert result["isError"] is result["result"]["isError"] is True
    assert result["payload"]["status"] == "error"
    assert result["payload"]["operation_id"] == "rest-original"
    assert "revision changed" in result["payload"]["error"]
    assert mesh.get(mid)["content"] == "The Cedar cache expires after 120 seconds."


@pytest.mark.parametrize("group_commit", ["0", "1"])
def test_http_mcp_response_serialization_failure_is_unknown_after_commit(monkeypatch, group_commit):
    monkeypatch.setenv("MESH_GROUP_COMMIT", group_commit)
    original = operations._receipt_response
    monkeypatch.setattr(operations, "_receipt_response", lambda *args: {**original(*args), "extra": float("nan")})
    result = asyncio.run(http_server.mcp_proxy(request({"name": "mesh_learn", "operation_id": "rest-original",
                       "agent_id": "test-agent", "arguments": {"namespace": "rest-receipts",
                       "content": "The Maple deployment uses port 7443."}})))
    assert result["isError"] is True and result["payload"]["status"] == "unknown"
    assert result["payload"]["operation_id"] == "rest-original"
    mesh = memory.Mesh("rest-receipts", agent_id="test-agent")
    assert mesh.count() == 1 and mesh.store.get_operation_receipt("rest-original", "local-user") is not None
