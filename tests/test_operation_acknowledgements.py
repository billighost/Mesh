"""Verify receipt identity and preserve uncertain writes across local interfaces."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import sys

import pytest

from mesh import audit, operations, store as storage
from mesh.client import ConnectionProfile, MeshClient, ProfileError, TransportUnavailable, UnknownOutcome, store_identity
from mesh.operation_contract import OPERATION_RECEIPT_VERSION, receipt_capability_proof
from mesh.store import MeshStore, NamespaceNotFoundError


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    for name, child in {
        "MESH_HOME": "mesh", "MESH_DIR": "mesh", "HOME": "home", "USERPROFILE": "home",
        "APPDATA": "appdata", "LOCALAPPDATA": "localappdata", "XDG_CONFIG_HOME": "xdg",
    }.items():
        root = tmp_path / child
        root.mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(root))
    for name in (
        "MESH_NO_AUTO_DAEMON", "MESH_NO_MODEL_DOWNLOAD", "MESH_NO_UPDATE_CHECK", "MESH_NO_IMPLICIT_CLOUD",
        "MESH_NO_OUTBOX_WORKER", "MESH_FORCE_HASH_EMBED", "MESH_NO_DAEMON_PROXY",
    ):
        monkeypatch.setenv(name, "1")
    for name in (
        "MESH_LOCKED_NAMESPACE", "MESH_LOCK_NAMESPACE", "MESH_READ_ONLY", "MESH_AUTHENTICATED_AGENT_ID",
        "MESH_AGENT_SCOPES", "MESH_ENCRYPT_AT_REST", "MESH_ENCRYPTION_KEY", "MESH_PASSPHRASE",
        "MESH_NAMESPACE", "MESH_MCP_INCLUDE_PRIVATE", "MESH_BLOCK_PRIVATE_OVERRIDE", "MESH_ENFORCE_AGENT_OWNERSHIP",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MESH_AGENT_ID", "test-agent")
    monkeypatch.setenv("MESH_BG_ONNX", "0")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")
    monkeypatch.setenv("MESH_GROUP_COMMIT", "0")
    monkeypatch.setenv("MESH_QUIET", "1")
    audit.stop_audit_worker()
    monkeypatch.setattr(audit, "_ensure_audit_drain_thread", lambda: None)
    yield tmp_path / "mesh"
    operations.evict_mesh_cache()
    operations.close_operation_brokers()
    audit.stop_audit_worker()
    storage._close_all_pooled_connections()
    audit._close_all_audit_connections()


def acknowledgement(operation_id="original-id", namespace="example"):
    result = {"status": "created", "item_id": "task-created"}
    return {**result, "namespace": namespace, "operation_id": operation_id, "replayed": False,
            "receipt": {"operation_id": operation_id, "created_at": "2026-01-01T00:00:00+00:00", "result": result}}


def install_http_stub(monkeypatch, root, payload, *, capability="valid", close_error=False):
    requests = []

    class Connection:
        def __init__(self, *args, **kwargs):
            self.method = None
            self.nonce = None

        def request(self, method, path, body=None, headers=None):
            requests.append({"method": method, "path": path, "headers": headers})
            self.method = method
            if method == "GET":
                self.nonce = path.split("=", 1)[1]

        def getresponse(self):
            if self.method == "GET":
                scope = store_identity(root)
                identity = {"store_id": scope, "scope_hmac": hmac.new(
                    b"YOUR_TOKEN_HERE", (self.nonce + "|" + scope).encode(), hashlib.sha256,
                ).hexdigest()}
                if capability != "missing":
                    identity.update({"operation_receipt_version": OPERATION_RECEIPT_VERSION,
                                     "receipt_hmac": receipt_capability_proof("YOUR_TOKEN_HERE", self.nonce, scope)})
                    if capability == "bool_version":
                        identity["operation_receipt_version"] = True
                    elif capability == "bad_proof":
                        identity["receipt_hmac"] = "0" * 64
                raw = json.dumps(identity).encode()
            else:
                raw = payload if isinstance(payload, bytes) else json.dumps({"payload": payload}).encode()

            class Response:
                status = 200

                def read(self, limit):
                    return raw[:limit]

            return Response()

        def close(self):
            if close_error:
                raise OSError("Injected connection close failure")

    monkeypatch.setattr("mesh.client.http.client.HTTPConnection", Connection)
    token = root / "fake-token"
    token.write_text("YOUR_TOKEN_HERE", encoding="utf-8")
    profile = ConnectionProfile("example", root, endpoint="http://127.0.0.1:7701", token_file=token)
    return MeshClient(profile, transport="http"), requests


def corrupt_acknowledgement(mode):
    result = copy.deepcopy(acknowledgement())
    if mode == "missing_operation_id":
        result.pop("operation_id")
    elif mode == "wrong_operation_id":
        result["operation_id"] = "different-id"
    elif mode == "wrong_namespace":
        result["namespace"] = "other"
    elif mode == "missing_receipt":
        result.pop("receipt")
    elif mode == "wrong_receipt_id":
        result["receipt"]["operation_id"] = "different-id"
    elif mode == "missing_receipt_date":
        result["receipt"].pop("created_at")
    elif mode == "bad_receipt_type":
        result["receipt"]["result"] = None
    elif mode == "empty_receipt":
        result["receipt"]["result"] = {}
    elif mode == "missing_object_identity":
        result["receipt"]["result"].pop("item_id")
    elif mode == "conflicting_object_identity":
        result["receipt"]["result"]["item_id"] = "task-other"
    elif mode == "wrong_operation":
        result["receipt"]["operation"] = "mesh_forget"
    elif mode == "wrong_producer":
        result["receipt"]["producer_scope"] = "principal:other"
    elif mode == "wrong_receipt_namespace":
        result["receipt"]["namespace"] = "other"
    elif mode == "bad_replayed_type":
        result["replayed"] = 1
    elif mode == "bad_status_type":
        result["status"] = []
    else:
        raise AssertionError(mode)
    return result


@pytest.mark.parametrize("mode", [
    "missing_operation_id", "wrong_operation_id", "wrong_namespace", "missing_receipt", "wrong_receipt_id",
    "missing_receipt_date", "bad_receipt_type", "empty_receipt", "missing_object_identity",
    "conflicting_object_identity", "wrong_operation", "wrong_producer", "wrong_receipt_namespace",
    "bad_replayed_type", "bad_status_type",
])
def test_http_malformed_acknowledgement_is_unknown_and_is_never_retried(isolated_runtime, monkeypatch, mode):
    client, requests = install_http_stub(monkeypatch, isolated_runtime, corrupt_acknowledgement(mode))
    with pytest.raises(UnknownOutcome) as failure:
        client.call("mesh_task_create", {"title": "An acknowledgement must identify this task."}, "original-id")
    assert failure.value.operation_id == "original-id"
    assert [request["method"] for request in requests] == ["GET", "POST"]


@pytest.mark.parametrize("capability", ["missing", "bool_version", "bad_proof"])
def test_http_receipt_capability_is_verified_before_sending_mutation_or_token(isolated_runtime, monkeypatch, capability):
    client, requests = install_http_stub(monkeypatch, isolated_runtime, acknowledgement(), capability=capability)
    with pytest.raises(ProfileError, match="durable operation receipts") as failure:
        client.call("mesh_task_create", {"title": "This endpoint has not proved receipt support."}, "original-id")
    assert failure.value.operation_id == "original-id"
    assert [request["method"] for request in requests] == ["GET"]
    assert all(not request["headers"] for request in requests)


def test_legacy_http_endpoint_can_still_serve_reads(isolated_runtime, monkeypatch):
    client, requests = install_http_stub(monkeypatch, isolated_runtime, {"status": "ok", "count": 0}, capability="missing")
    assert client.call("mesh_patterns", {}) == {"status": "ok", "count": 0}
    assert [request["method"] for request in requests] == ["GET", "POST"]


def test_http_success_receipt_and_cleanup_failure_keep_original_identity(isolated_runtime, monkeypatch):
    client, requests = install_http_stub(monkeypatch, isolated_runtime, acknowledgement(), close_error=True)
    result = client.call("mesh_task_create", {"title": "A confirmed acknowledgement survives cleanup."}, "original-id")
    assert result["receipt"]["operation_id"] == "original-id"
    assert len(requests) == 2


def test_invalid_utf8_after_http_mutation_exposes_original_id(isolated_runtime, monkeypatch):
    client, requests = install_http_stub(monkeypatch, isolated_runtime, b'\xff', close_error=True)
    with pytest.raises(UnknownOutcome) as failure:
        client.call("mesh_task_create", {"title": "Malformed response is not a confirmed outcome."}, "original-id")
    assert failure.value.operation_id == "original-id"
    assert len(requests) == 2


def test_sdk_post_commit_delivery_failure_exposes_id_and_same_id_retry_creates_no_duplicate(isolated_runtime, monkeypatch):
    client = MeshClient(ConnectionProfile("example", isolated_runtime, agent_id="test-agent"), transport="sdk")
    response = operations._receipt_response
    monkeypatch.setattr(operations, "_receipt_response", lambda *args: (_ for _ in ()).throw(ValueError("Injected post-commit failure")))
    arguments = {"title": "Exactly one durable task must survive its failed response."}
    with pytest.raises(UnknownOutcome) as failure:
        client.call("mesh_task_create", arguments)
    operation_id = failure.value.operation_id
    store = MeshStore("example", create=False)
    assert store.get_operation_receipt(operation_id, "local-user") is not None
    monkeypatch.setattr(operations, "_receipt_response", response)
    result = client.call("mesh_task_create", arguments, operation_id)
    assert result["replayed"] is True
    assert len(store.list_work_items()) == 1


def test_sdk_validation_rejection_exposes_id_without_fallback(isolated_runtime, monkeypatch):
    client = MeshClient(ConnectionProfile("example", isolated_runtime), transport="auto")
    monkeypatch.setattr(client, "_call_http", lambda *args: pytest.fail("A rejected mutation reached another transport"))
    result = client.call("mesh_task_create", {"title": ""})
    assert result["status"] == "error"
    assert isinstance(result["operation_id"], str)
    assert MeshStore("example", create=False).list_work_items() == []


def test_sdk_dispatched_transport_error_never_falls_back(isolated_runtime, monkeypatch):
    client = MeshClient(ConnectionProfile("example", isolated_runtime), transport="auto")
    monkeypatch.setattr(operations, "execute_tool", lambda *args, **kwargs: (_ for _ in ()).throw(TransportUnavailable("After dispatch")))
    monkeypatch.setattr(client, "_call_http", lambda *args: pytest.fail("A dispatched mutation reached another transport"))
    with pytest.raises(UnknownOutcome) as failure:
        client.call("mesh_task_create", {"title": "Do not replay a dispatched mutation."}, "original-id")
    assert failure.value.operation_id == "original-id"


@pytest.mark.parametrize("invalid", ["", False, 0, [], {}])
def test_native_mcp_invalid_operation_identity_rejects_before_proxy_or_store(isolated_runtime, monkeypatch, invalid):
    from mesh import mcp_server

    monkeypatch.setattr(mcp_server, "_try_proxy_via_daemon", lambda *args: pytest.fail("Invalid identity reached a proxy"))
    result = mcp_server._execute_tool_sync("mesh_task_create", {"title": "An invalid ID must not create a task.", "operation_id": invalid},
                                           default_namespace="example", agent_id="test-agent")
    assert result["status"] == "error"
    assert result["operation_id"] == invalid
    with pytest.raises(NamespaceNotFoundError):
        MeshStore("example", create=False)


def test_native_mcp_post_commit_failure_marks_tool_error_and_keeps_generated_id(isolated_runtime, monkeypatch):
    from mesh import mcp_server

    monkeypatch.setenv("MESH_NAMESPACE", "example")
    response = operations._receipt_response
    monkeypatch.setattr(operations, "_receipt_response", lambda *args: (_ for _ in ()).throw(RuntimeError("Injected post-commit failure")))
    arguments = {"title": "Native MCP must preserve this durable task identity."}
    result = mcp_server._call_tool_sync("mesh_task_create", arguments)
    payload = json.loads(result.content[0].text)
    assert result.isError is True
    assert payload["status"] == "unknown"
    assert isinstance(payload["operation_id"], str)
    store = MeshStore("example", create=False)
    assert store.get_operation_receipt(payload["operation_id"], "local-user") is not None
    monkeypatch.setattr(operations, "_receipt_response", response)
    replay = mcp_server._execute_tool_sync("mesh_task_create", {**arguments, "operation_id": payload["operation_id"]},
                                           default_namespace="example", allow_daemon_proxy=False)
    assert replay["replayed"] is True
    assert len(store.list_work_items()) == 1


@pytest.mark.parametrize("mode", ["wrong_operation_id", "missing_receipt", "conflicting_object_identity"])
def test_mcp_daemon_malformed_receipt_stops_with_original_id(monkeypatch, mode):
    from tests.test_transport_safety import fake_connection
    from mesh import daemon, mcp_server

    monkeypatch.setenv("MESH_NAMESPACE", "example")
    monkeypatch.setenv("MESH_NO_DAEMON_PROXY", "0")
    monkeypatch.setattr(mcp_server, "_STDIO_SERVER_ACTIVE", True)
    monkeypatch.setattr(mcp_server, "_DAEMON_AVAILABLE", False)
    monkeypatch.setattr(mcp_server, "_DAEMON_CHECK_TS", 0.0)
    monkeypatch.setattr(mcp_server, "_DAEMON_CONN", None)
    monkeypatch.setattr(daemon, "verify_daemon_identity", lambda **kwargs: {"identity_verified": True, "operation_receipts_verified": True})
    monkeypatch.setattr(mcp_server, "get_auth_headers", lambda: {"Authorization": "Bearer YOUR_TOKEN_HERE"})
    requests = fake_connection(monkeypatch, mcp_server, json.dumps({"payload": corrupt_acknowledgement(mode)}).encode())
    result = mcp_server._try_proxy_via_daemon("mesh_task_create", {"title": "Only the original receipt can confirm this task.", "operation_id": "original-id"})
    assert result["status"] == "unknown"
    assert result["operation_id"] == "original-id"
    assert len(requests) == 1


def test_mcp_legacy_daemon_is_not_sent_a_receipted_mutation(monkeypatch):
    from mesh import daemon, mcp_server

    monkeypatch.setenv("MESH_NAMESPACE", "example")
    monkeypatch.setenv("MESH_NO_DAEMON_PROXY", "0")
    monkeypatch.setattr(mcp_server, "_STDIO_SERVER_ACTIVE", True)
    monkeypatch.setattr(mcp_server, "_DAEMON_AVAILABLE", False)
    monkeypatch.setattr(mcp_server, "_DAEMON_CHECK_TS", 0.0)
    monkeypatch.setattr(daemon, "verify_daemon_identity", lambda **kwargs: {"identity_verified": True})
    monkeypatch.setattr(mcp_server, "get_auth_headers", lambda: pytest.fail("Legacy endpoint reached token transmission"))
    assert mcp_server._try_proxy_via_daemon("mesh_task_create", {"title": "No receipt capability.", "operation_id": "original-id"}) is None


def prepare_cli(tmp_path, root, monkeypatch):
    path = tmp_path / "connection.json"
    path.write_text(json.dumps({"version": 1, "namespace": "example", "mesh_home": str(root),
                               "agent_id": "test-agent", "policy": {"include_private": False}}), encoding="utf-8")
    arguments = tmp_path / "arguments.json"
    arguments.write_text(json.dumps({"title": "A durable CLI task must retain its receipt."}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["mesh call", "mesh_task_create", "--profile", str(path), "--args-file", str(arguments), "--transport", "sdk"])


def test_cli_post_commit_failure_prints_generated_id_and_unknown_exit_status(isolated_runtime, tmp_path, monkeypatch, capsys):
    from mesh import cli

    prepare_cli(tmp_path, isolated_runtime, monkeypatch)
    monkeypatch.setattr(operations, "_receipt_response", lambda *args: (_ for _ in ()).throw(RuntimeError("Injected post-commit failure")))
    with pytest.raises(SystemExit) as failure:
        cli.call_cmd()
    assert failure.value.code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unknown"
    assert MeshStore("example", create=False).get_operation_receipt(payload["operation_id"], "local-user") is not None


def test_cli_serialization_failure_after_acknowledgement_keeps_original_id(isolated_runtime, tmp_path, monkeypatch, capsys):
    from mesh import cli

    prepare_cli(tmp_path, isolated_runtime, monkeypatch)
    response = operations._receipt_response
    monkeypatch.setattr(operations, "_receipt_response", lambda *args: {**response(*args), "invalid_extra": float("nan")})
    with pytest.raises(SystemExit) as failure:
        cli.call_cmd()
    assert failure.value.code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unknown"
    assert MeshStore("example", create=False).get_operation_receipt(payload["operation_id"], "local-user") is not None


def test_cli_broken_stdout_reports_unknown_identity_on_stderr(isolated_runtime, tmp_path, monkeypatch, capsys):
    from mesh import cli

    prepare_cli(tmp_path, isolated_runtime, monkeypatch)

    class BrokenOutput:
        def write(self, value):
            raise BrokenPipeError("Injected output delivery failure")

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", BrokenOutput())
    with pytest.raises(SystemExit) as failure:
        cli.call_cmd()
    assert failure.value.code == 3
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "unknown"
    assert MeshStore("example", create=False).get_operation_receipt(payload["operation_id"], "local-user") is not None
