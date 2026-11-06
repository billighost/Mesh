"""Receipt negotiation and cached identity must retain root/token bindings."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from starlette.requests import Request

from mesh import audit, daemon
from mesh.client import store_identity
from mesh.operation_contract import OPERATION_RECEIPT_VERSION, receipt_capability_proof, supports_operation_receipts, validate_mutation_acknowledgement
from mesh.store import MeshStore, NamespaceNotFoundError
from tests.test_operation_acknowledgements import isolated_runtime as isolated_runtime


@pytest.fixture(autouse=True)
def clear_identity_cache():
    with daemon._VERIFIED_DAEMON_LOCK:
        daemon._VERIFIED_DAEMON_CACHE.clear()
    yield
    with daemon._VERIFIED_DAEMON_LOCK:
        daemon._VERIFIED_DAEMON_CACHE.clear()


def identity_stub(monkeypatch, root, *, mode="valid"):
    roots = [root]
    tokens = ["YOUR_TOKEN_HERE"]
    requests = []
    limits = []
    monkeypatch.setattr(audit, "get_mesh_dir", lambda: roots[0])
    monkeypatch.setattr(daemon, "get_or_create_auth_token", lambda: tokens[0])

    class Connection:
        sock = None

        def __init__(self, *args, **kwargs):
            self.nonce = None

        def connect(self):
            pass

        def request(self, method, path, headers=None):
            requests.append((method, path))
            self.nonce = path.split("=", 1)[1]

        def getresponse(self):
            scope = store_identity(roots[0])
            data = {"status": "ok", "version": "test", "pid": 123,
                    "proof": hmac.new(tokens[0].encode(), bytes.fromhex(self.nonce), hashlib.sha256).hexdigest(),
                    "store_id": scope, "operation_receipt_version": OPERATION_RECEIPT_VERSION,
                    "receipt_hmac": receipt_capability_proof(tokens[0], self.nonce, scope)}
            if mode == "invalid_capability":
                data["receipt_hmac"] = "0" * 64
            elif mode == "legacy":
                data.pop("operation_receipt_version")
                data.pop("receipt_hmac")
            raw = b"x" * 16385 if mode == "oversized" else json.dumps(data).encode()

            class Response:
                status = 200

                def read(self, limit):
                    limits.append(limit)
                    return raw[:limit]

            return Response()

        def close(self):
            pass

    monkeypatch.setattr(daemon.http.client, "HTTPConnection", Connection)
    return roots, tokens, requests, limits


def test_identity_cache_reuses_only_the_same_root_and_token(isolated_runtime, tmp_path, monkeypatch):
    roots, tokens, requests, _ = identity_stub(monkeypatch, isolated_runtime)
    first = daemon.verify_daemon_identity()
    assert first["operation_receipts_verified"] is True
    assert daemon.verify_daemon_identity() == first
    assert len(requests) == 1
    second_root = tmp_path / "other-data"
    second_root.mkdir()
    roots[0] = second_root
    assert daemon.verify_daemon_identity()["operation_receipts_verified"] is True
    assert len(requests) == 2
    tokens[0] = "ANOTHER_FAKE_TOKEN"
    assert daemon.verify_daemon_identity()["operation_receipts_verified"] is True
    assert len(requests) == 3
    assert all("token" not in key for key in first)


def test_identity_cache_invalidation_covers_all_root_and_token_bindings(isolated_runtime, monkeypatch):
    _, tokens, requests, _ = identity_stub(monkeypatch, isolated_runtime)
    assert daemon.verify_daemon_identity()
    tokens[0] = "ANOTHER_FAKE_TOKEN"
    assert daemon.verify_daemon_identity()
    daemon.invalidate_daemon_identity_cache()
    assert not daemon._VERIFIED_DAEMON_CACHE
    assert daemon.verify_daemon_identity()
    assert len(requests) == 3


def test_identity_cache_retention_is_bounded_and_expired_entries_are_pruned(isolated_runtime, monkeypatch):
    _, tokens, requests, _ = identity_stub(monkeypatch, isolated_runtime)
    clock = [1.0]
    monkeypatch.setattr(daemon.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(daemon, "_VERIFIED_DAEMON_CACHE_LIMIT", 4)
    for index in range(10):
        tokens[0] = f"FAKE_TEST_TOKEN_{index}"
        assert daemon.verify_daemon_identity()["operation_receipts_verified"] is True
        assert len(daemon._VERIFIED_DAEMON_CACHE) <= 4
    clock[0] = 40.0
    assert daemon.verify_daemon_identity()
    assert len(requests) == 11
    assert len(daemon._VERIFIED_DAEMON_CACHE) == 1


@pytest.mark.parametrize("mode", ["legacy", "invalid_capability"])
def test_basic_identity_does_not_claim_unproved_receipt_support(isolated_runtime, monkeypatch, mode):
    identity_stub(monkeypatch, isolated_runtime, mode=mode)
    result = daemon.verify_daemon_identity()
    assert result["identity_verified"] is True
    assert result["operation_receipts_verified"] is False


def test_identity_response_read_is_bounded(isolated_runtime, monkeypatch):
    _, _, requests, limits = identity_stub(monkeypatch, isolated_runtime, mode="oversized")
    assert daemon.verify_daemon_identity() is None
    assert len(requests) == 1
    assert limits == [16385]
    assert not daemon._VERIFIED_DAEMON_CACHE


def json_request(data, path="/mcp"):
    raw = json.dumps(data).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": path,
                    "query_string": b"", "headers": [(b"content-type", b"application/json")],
                    "client": ("127.0.0.1", 1)}, receive)


@pytest.mark.parametrize("group_commit", ["0", "1"])
def test_http_store_identity_mismatch_rejects_before_any_dispatch(isolated_runtime, monkeypatch, group_commit):
    from fastapi import HTTPException
    from mesh import http_server, mcp_server, operations

    monkeypatch.setenv("MESH_GROUP_COMMIT", group_commit)
    monkeypatch.setattr(mcp_server, "_execute_tool_sync", lambda **kwargs: pytest.fail("Wrong store reached synchronous dispatch"))
    monkeypatch.setattr(operations, "execute_tool_async", lambda *args, **kwargs: pytest.fail("Wrong store reached group dispatch"))
    request = json_request({"name": "mesh_task_create", "arguments": {"title": "Do not create this in another store."},
                            "default_namespace": "example", "operation_id": "original-id", "expected_store_id": "0" * 64})
    with pytest.raises(HTTPException) as failure:
        asyncio.run(http_server.mcp_proxy(request))
    assert failure.value.status_code == 403
    with pytest.raises(NamespaceNotFoundError):
        MeshStore("example", create=False)


def test_http_identity_receipt_capability_is_authenticated_for_this_root(isolated_runtime, monkeypatch):
    from mesh import http_server

    monkeypatch.setattr(http_server, "get_or_create_auth_token", lambda: "YOUR_TOKEN_HERE")
    nonce = "a1" * 24
    result = asyncio.run(http_server.identity(json_request({}, "/identity"), nonce=nonce))
    assert supports_operation_receipts(result, "YOUR_TOKEN_HERE", nonce, store_identity(isolated_runtime))
    assert not supports_operation_receipts(result, "YOUR_TOKEN_HERE", nonce, "0" * 64)


@pytest.mark.parametrize("group_commit", ["0", "1"])
def test_http_matching_store_commits_a_valid_receipt_and_replays_once(isolated_runtime, monkeypatch, group_commit):
    from mesh import http_server

    monkeypatch.setenv("MESH_GROUP_COMMIT", group_commit)
    body = {"name": "mesh_task_create", "arguments": {"title": "The matching store accepts exactly one task."},
            "default_namespace": "example", "agent_id": "test-agent", "operation_id": "original-id",
            "expected_producer_scope": "local-user", "expected_store_id": store_identity(isolated_runtime),
            "client_policy": {"include_private": False, "read_only": False, "enforce_agent_ownership": False}}
    first = asyncio.run(http_server.mcp_proxy(json_request(body)))["payload"]
    validate_mutation_acknowledgement(first, "original-id", "example", operation="mesh_task_create", producer_scope="local-user")
    second = asyncio.run(http_server.mcp_proxy(json_request(body)))["payload"]
    assert second["replayed"] is True
    assert first["id"] == second["id"]
    assert len(MeshStore("example", create=False).list_work_items()) == 1
