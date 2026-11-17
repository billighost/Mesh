"""Reviewed profiles, terminal failures and safe fallback across local interfaces."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from mesh.client import (
    ConnectionPolicy, ConnectionProfile, MeshClient, ProfileError,
    TransportUnavailable, UnknownOutcome, store_identity,
)


def profile_file(tmp_path, **changes):
    path = tmp_path / "project" / ".mesh" / "connection.json"
    path.parent.mkdir(parents=True)
    data = {"version": 1, "namespace": "example", "mesh_home": "../data", "agent_id": "test-agent", "policy": {"include_private": False}}
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_profile_binds_relative_root_and_normalizes_namespace(tmp_path):
    path = profile_file(tmp_path, namespace="example ")
    profile = ConnectionProfile.from_file(path)
    assert profile.namespace == "example"
    assert profile.mesh_home == path.parent.parent / "data"
    assert profile.policy.include_private is False
    assert profile.policy.allow_namespace_override is False


@pytest.mark.parametrize("changes", [
    {"version": True}, {"version": 2}, {"token": "FAKE_TEST_TOKEN"},
    {"policy": {"include_private": "false"}}, {"policy": {"allow_cloud": True}},
    {"endpoint": "http://example.invalid", "token_file": "token"},
    {"endpoint": "http://localhost:7701", "token_file": "token"},
    {"endpoint": "http://127.0.0.1:7701/path", "token_file": "token"},
    {"endpoint": "http://user:password@127.0.0.1:7701", "token_file": "token"},
    {"endpoint": "http://127.0.0.1:7701"},
])
def test_profile_rejects_unsafe_or_ambiguous_fields(tmp_path, changes):
    with pytest.raises((ProfileError, ValueError)) as failure:
        ConnectionProfile.from_file(profile_file(tmp_path, **changes))
    assert "FAKE_TEST_TOKEN" not in str(failure.value)


def test_duplicate_policy_field_cannot_override_restriction(tmp_path):
    path = profile_file(tmp_path)
    path.write_text('{"version":1,"namespace":"example","mesh_home":"data","agent_id":"agent","policy":{"read_only":true,"read_only":false}}')
    with pytest.raises(ProfileError, match="Duplicate"):
        ConnectionProfile.from_file(path)


def test_reviewed_authenticated_profile_binds_sdk_receipt_scope(tmp_path, monkeypatch):
    profile = ConnectionProfile.from_file(profile_file(tmp_path, agent_id="alice", producer_scope="principal:alice"))
    seen = []
    monkeypatch.setattr("mesh.operations.execute_tool", lambda name, args, **kwargs: seen.append(kwargs) or {
        "status": "stored", "memory_id": "fake-memory", "namespace": args["namespace"],
        "operation_id": kwargs["operation_id"], "replayed": False,
        "receipt": {"operation_id": kwargs["operation_id"], "created_at": "2026-01-01T00:00:00+00:00",
                    "result": {"status": "stored", "memory_id": "fake-memory"}},
    })
    MeshClient(profile).call("mesh_learn", {"content": "A fake project fact."}, operation_id="original-id")
    assert seen[0]["context"]["approved_producer_scope"] == "principal:alice"
    assert seen[0]["agent_id"] == "alice"
    assert seen[0]["operation_id"] == "original-id"


@pytest.mark.parametrize("scope,actor", [("arbitrary", "alice"), ("principal:alice", "bob"), ("principal:", "alice"), (None, "alice")])
def test_ambiguous_producer_scope_is_terminal(tmp_path, scope, actor):
    with pytest.raises(ProfileError):
        ConnectionProfile.from_file(profile_file(tmp_path, agent_id=actor, producer_scope=scope))


def test_profile_symlink_is_rejected(tmp_path):
    target = profile_file(tmp_path)
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("This account cannot create symlinks")
    with pytest.raises(ValueError):
        ConnectionProfile.from_file(link)


def test_profile_narrows_flags_and_keeps_original_request(tmp_path, monkeypatch):
    client = MeshClient(ConnectionProfile("example", tmp_path))
    seen = []
    monkeypatch.setattr(client, "_call_sdk", lambda name, args, operation: seen.append((args, operation)) or {"status": "ok"})
    original = {"query": "fake service port", "include_private": True}
    client.call("mesh_recall", original)
    assert seen[0][0]["include_private"] is False
    assert seen[0][0]["namespace"] == "example"
    assert "namespace" not in original


@pytest.mark.parametrize("name", ["mesh_learn", "mesh_learn_many", "mesh_task_create", "mesh_task_update", "mesh_task_complete"])
def test_read_only_profile_blocks_every_mutation_before_transport(tmp_path, name, monkeypatch):
    client = MeshClient(ConnectionProfile("example", tmp_path, policy=ConnectionPolicy(read_only=True)))
    monkeypatch.setattr(client, "_call_sdk", lambda *args: pytest.fail("Read-only mutation reached transport"))
    with pytest.raises(ProfileError, match="read-only"):
        client.call(name, {})


def test_namespace_override_and_nonfinite_values_are_terminal(tmp_path, monkeypatch):
    client = MeshClient(ConnectionProfile("example", tmp_path))
    monkeypatch.setattr(client, "_call_sdk", lambda *args: pytest.fail("Invalid request reached transport"))
    with pytest.raises(ProfileError, match="Namespace override"):
        client.call("mesh_recall", {"namespace": "other", "query": "test"})
    with pytest.raises(ValueError, match="finite JSON"):
        client.call("mesh_recall", {"query": "test", "min_confidence": float("nan")})


def test_fallback_only_on_transport_absence_and_receipt_id_is_preserved(tmp_path, monkeypatch):
    client = MeshClient(ConnectionProfile("example", tmp_path))
    def absent(*args):
        raise TransportUnavailable("missing SDK")
    monkeypatch.setattr(client, "_call_sdk", absent)
    seen = []
    monkeypatch.setattr(client, "_call_http", lambda name, args, operation, mutation: seen.append((args, operation)) or {"status": "ok"})
    client.call("mesh_operation_get", {"operation_id": "original-id"})
    assert seen[0][0]["operation_id"] == "original-id"
    def terminal(*args):
        raise RuntimeError("unsafe SQLite runtime")
    monkeypatch.setattr(client, "_call_sdk", terminal)
    with pytest.raises(RuntimeError, match="unsafe SQLite"):
        client.call("mesh_learn", {"content": "fake fact"}, operation_id="original-id")
    assert len(seen) == 1


def test_partial_http_mutation_is_unknown_and_never_retried(tmp_path, monkeypatch):
    token_path = tmp_path / "token"
    token_path.write_text("YOUR_TOKEN_HERE")
    profile = ConnectionProfile("example", tmp_path, endpoint="http://127.0.0.1:7701", token_file=token_path)
    seen = []
    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.nonce = None
        def request(self, method, path, body=None, headers=None):
            seen.append((method, path, headers))
            if method == "GET":
                self.nonce = path.split("=", 1)[1]
            else:
                raise ConnectionResetError("Partial send")
        def getresponse(self):
            nonce = self.nonce
            class Response:
                status = 200
                def read(self, limit):
                    scope = store_identity(tmp_path)
                    signature = hmac.new(b"YOUR_TOKEN_HERE", (nonce + "|" + scope).encode(), hashlib.sha256).hexdigest()
                    from mesh.operation_contract import OPERATION_RECEIPT_VERSION, receipt_capability_proof

                    return json.dumps({"store_id": scope, "scope_hmac": signature,
                                       "operation_receipt_version": OPERATION_RECEIPT_VERSION,
                                       "receipt_hmac": receipt_capability_proof("YOUR_TOKEN_HERE", nonce, scope)}).encode()
            return Response()
        def close(self):
            pass
    monkeypatch.setattr("mesh.client.http.client.HTTPConnection", FakeConnection)
    with pytest.raises(UnknownOutcome) as failure:
        MeshClient(profile, transport="http").call("mesh_task_create", {"title": "fake task"}, "same-id")
    assert failure.value.operation_id == "same-id"
    assert [entry[0] for entry in seen] == ["GET", "POST"]


def test_http_identity_failure_never_sends_bearer_token(tmp_path, monkeypatch):
    token_path = tmp_path / "token"
    token_path.write_text("YOUR_TOKEN_HERE")
    profile = ConnectionProfile("example", tmp_path, endpoint="http://127.0.0.1:7701", token_file=token_path)
    requests = []
    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass
        def request(self, *args, **kwargs):
            requests.append((args, kwargs))
        def getresponse(self):
            class Response:
                status = 200
                def read(self, limit):
                    return b'{"store_id":"wrong","scope_hmac":"wrong"}'
            return Response()
        def close(self):
            pass
    monkeypatch.setattr("mesh.client.http.client.HTTPConnection", FakeConnection)
    with pytest.raises(ProfileError, match="store identity"):
        MeshClient(profile, transport="http").call("mesh_recall", {"query": "fake fact"})
    assert len(requests) == 1
    assert requests[0][0][0] == "GET"
    assert "headers" not in requests[0][1]
