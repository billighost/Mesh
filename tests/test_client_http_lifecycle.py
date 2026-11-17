"""Small connection lifecycle contracts with authenticated fake HTTP responses."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from mesh.client import ConnectionProfile, MeshClient, ProfileError, TransportUnavailable, store_identity
from mesh.operation_contract import OPERATION_RECEIPT_VERSION, UnknownOutcome, receipt_capability_proof


class FakeConnection:
    def __init__(self, state, *args, **kwargs):
        self.state = state
        self.sock = SimpleNamespace(settimeout=lambda value: None)
        self.pending = None
        self.nonce = None
        self.payload = None
        self.closed = 0
        self.fail_identity = None
        self.fail_post = False
        self.state.connections.append(self)

    def request(self, method, path, body=None, headers=None):
        if self.pending is not None:
            raise AssertionError("Requests interleaved on one connection")
        self.state.requests.append((self, method, path, headers))
        if method == "GET":
            if self.fail_identity is not None:
                failure, self.fail_identity = self.fail_identity, None
                raise failure
            self.nonce = path.split("=", 1)[1]
            if self.state.hold_identity is not None:
                entered, release = self.state.hold_identity
                self.state.hold_identity = None
                self.pending = method
                entered.set()
                if not release.wait(2):
                    raise AssertionError("Identity barrier was not released")
        else:
            self.payload = json.loads(body)
            self.state.posts.append((self.payload, headers))
            if self.fail_post:
                raise ConnectionResetError("FAKE_PARTIAL_POST")
        self.pending = method

    def getresponse(self):
        method, self.pending = self.pending, None
        if method == "GET":
            identity = store_identity(self.state.profile.mesh_home)
            token = self.state.token
            signature = hmac.new(token.encode(), (self.nonce + "|" + identity).encode(), hashlib.sha256).hexdigest()
            result = {"store_id": identity, "scope_hmac": "wrong" if self.state.wrong_identity else signature}
            if self.state.capability:
                result.update(operation_receipt_version=OPERATION_RECEIPT_VERSION,
                              receipt_hmac=receipt_capability_proof(token, self.nonce, identity))
            raw = json.dumps(result).encode()
            close = self.state.close_identity == "header"
            if self.state.close_identity == "socket":
                self.sock = None
        else:
            data = self.payload
            if data["name"] == "mesh_learn":
                result = {"status": "stored", "memory_id": "fake-memory", "revision": 1, "deduplicated": False}
                receipt = {"operation_id": data["operation_id"], "operation": data["name"],
                           "namespace": data["arguments"]["namespace"], "producer_scope": data["expected_producer_scope"],
                           "result": result, "created_at": "2026-01-01T00:00:00Z"}
                result = {**result, "operation_id": data["operation_id"], "namespace": data["arguments"]["namespace"],
                          "receipt": receipt, "replayed": False}
            elif data["name"] == "mesh_operation_get":
                result = {"status": "recorded", "receipt": {"operation_id": data["arguments"]["operation_id"]}}
            else:
                result = {"results": []}
            raw = b"invalid" if self.state.invalid_result else json.dumps({"payload": result}).encode()
            close = self.state.close_response
            if close:
                self.sock = None
        return SimpleNamespace(status=200, will_close=close, read=lambda limit: raw[:limit])

    def close(self):
        self.closed += 1
        self.sock = None
        if self.state.fail_close:
            raise RuntimeError("FAKE_CLOSE_FAILURE")


class HttpClientLifecycleContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mesh-client-http-contract-")
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.addCleanup(self.cleanup_temporary)
        self.token_file = self.root / "token"
        self.token_file.write_text("FAKE_TOKEN_ONE", encoding="utf-8")
        self.profile = ConnectionProfile("example", self.root, endpoint="http://127.0.0.1:7701", token_file=self.token_file)
        self.state = SimpleNamespace(profile=self.profile, token="FAKE_TOKEN_ONE", connections=[], requests=[], posts=[],
                                     wrong_identity=False, capability=True, close_response=False, invalid_result=False,
                                     fail_close=False, hold_identity=None, close_identity=None)
        patcher = mock.patch("mesh.client.http.client.HTTPConnection", side_effect=lambda *a, **k: FakeConnection(self.state, *a, **k))
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = MeshClient(self.profile, transport="http", timeout=2)
        self.addCleanup(self.client.close)

    def cleanup_temporary(self):
        self.inventory = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.temporary.cleanup()

    def recall(self):
        return self.client.call("mesh_recall", {"query": "fake fact", "include_private": True})

    def test_connection_reuse_reloads_token_and_challenges_every_call(self):
        self.recall()
        self.token_file.write_text("FAKE_TOKEN_TWO", encoding="utf-8")
        self.state.token = "FAKE_TOKEN_TWO"
        result = self.client.call("mesh_learn", {"content": "fake fact"}, operation_id="stable-id")
        self.assertEqual(result["operation_id"], "stable-id")
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual([row[1] for row in self.state.requests], ["GET", "POST", "GET", "POST"])
        self.assertNotEqual(self.state.requests[0][2], self.state.requests[2][2])
        self.assertTrue(all(row[3] is None for row in self.state.requests if row[1] == "GET"))
        self.assertEqual([headers["Authorization"] for _, headers in self.state.posts],
                         ["Bearer FAKE_TOKEN_ONE", "Bearer FAKE_TOKEN_TWO"])
        self.assertFalse(self.state.posts[0][0]["arguments"]["include_private"])
        self.assertEqual(self.state.posts[1][0]["expected_store_id"], store_identity(self.root))
        self.assertEqual(self.state.posts[1][0]["expected_producer_scope"], "local-user")

    def test_reused_connection_never_caches_identity_or_receipt_proof(self):
        self.client.call("mesh_learn", {"content": "fake fact"}, operation_id="first-id")
        self.state.capability = False
        with self.assertRaises(ProfileError):
            self.client.call("mesh_learn", {"content": "fake fact"}, operation_id="second-id")
        self.assertEqual(len(self.state.posts), 1)
        self.assertEqual(self.state.connections[0].closed, 1)

    def test_identity_rejection_on_reused_connection_sends_no_bearer(self):
        self.recall()
        self.state.wrong_identity = True
        with self.assertRaises(ProfileError):
            self.recall()
        self.assertEqual(len(self.state.posts), 1)
        self.assertEqual(self.state.requests[-1][1], "GET")
        self.assertIsNone(self.state.requests[-1][3])
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.state.connections[0].closed, 1)

    def test_closed_identity_connection_never_reopens_for_a_bearer_post(self):
        for signal in ("header", "socket"):
            for reused in (False, True):
                with self.subTest(signal=signal, reused=reused), MeshClient(self.profile, transport="http", timeout=2) as client:
                    self.state.close_identity = None
                    if reused:
                        client.call("mesh_recall", {"query": "fake fact"})
                    before_posts, before_connections = len(self.state.posts), self.factory.call_count
                    self.state.close_identity = signal
                    with self.assertRaises(TransportUnavailable) as failure:
                        client.call("mesh_learn", {"content": "fake fact"}, operation_id="stable-id")
                    self.assertEqual(failure.exception.operation_id, "stable-id")
                    self.assertEqual(len(self.state.posts), before_posts)
                    self.assertEqual(self.factory.call_count, before_connections + (not reused))
                    self.assertEqual(self.state.requests[-1][1], "GET")
                    self.assertIsNone(self.state.requests[-1][3])
                    self.assertEqual(self.state.connections[-1].closed, 1)

    def test_stale_identity_reconnects_once_before_mutation_dispatch(self):
        self.recall()
        self.state.connections[0].fail_identity = http.client.RemoteDisconnected("FAKE_STALE_CONNECTION")
        result = self.client.call("mesh_learn", {"content": "fake fact"}, operation_id="stable-id")
        self.assertEqual(result["operation_id"], "stable-id")
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual([row[1] for row in self.state.requests], ["GET", "POST", "GET", "GET", "POST"])
        self.assertEqual(self.state.connections[0].closed, 1)
        self.assertEqual(sum(payload["name"] == "mesh_learn" for payload, _ in self.state.posts), 1)

    def test_mutation_partial_post_is_never_replayed_on_reused_connection(self):
        self.recall()
        self.state.connections[0].fail_post = True
        with self.assertRaises(UnknownOutcome) as failure:
            self.client.call("mesh_learn", {"content": "fake fact"}, operation_id="stable-id")
        self.assertEqual(failure.exception.operation_id, "stable-id")
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(sum(payload["name"] == "mesh_learn" for payload, _ in self.state.posts), 1)
        self.assertEqual(self.state.connections[0].closed, 1)

    def test_failed_reconnect_never_dispatches_and_has_no_third_attempt(self):
        self.recall()
        self.state.connections[0].fail_identity = http.client.RemoteDisconnected("FAKE_STALE_CONNECTION")

        def failing_connection(*args, **kwargs):
            connection = FakeConnection(self.state, *args, **kwargs)
            connection.fail_identity = ConnectionRefusedError("FAKE_OFFLINE_ENDPOINT")
            return connection

        self.factory.side_effect = failing_connection
        with self.assertRaises(TransportUnavailable):
            self.client.call("mesh_learn", {"content": "fake fact"}, operation_id="stable-id")
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(len(self.state.posts), 1)
        self.assertTrue(all(connection.closed == 1 for connection in self.state.connections))

    def test_identity_reconnect_cannot_extend_its_timeout_budget(self):
        clock = [0.0]
        with mock.patch("mesh.client.time.monotonic", side_effect=lambda: clock[0]):
            self.recall()
            original = self.state.connections[0].request

            def timed_out(*args, **kwargs):
                clock[0] = 2.0
                raise TimeoutError("FAKE_IDENTITY_TIMEOUT")

            self.state.connections[0].request = timed_out
            with self.assertRaises(TransportUnavailable):
                self.recall()
            self.state.connections[0].request = original
        self.assertEqual(self.factory.call_count, 1)

    def test_connection_age_call_limit_and_binding_changes_close_old_socket(self):
        clock = [0.0]
        with mock.patch("mesh.client.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch("mesh.client._HTTP_CONNECTION_MAX_CALLS", 2):
            self.recall()
            self.recall()
            self.assertEqual(self.factory.call_count, 1)
            self.recall()
            self.assertEqual(self.factory.call_count, 2)
            clock[0] = 61.0
            self.recall()
            self.assertEqual(self.factory.call_count, 3)
            self.client.profile = replace(self.profile, agent_id="another-reviewed-agent")
            self.recall()
            self.assertEqual(self.factory.call_count, 4)
        self.assertTrue(all(connection.closed == 1 for connection in self.state.connections[:-1]))
        self.assertEqual(self.state.posts[-1][0]["agent_id"], "another-reviewed-agent")

    def test_closed_response_and_invalid_payload_discard_connection(self):
        self.state.close_response = True
        self.recall()
        self.assertEqual(self.state.connections[0].closed, 1)
        self.state.close_response = False
        self.state.invalid_result = True
        with self.assertRaises(TransportUnavailable):
            self.recall()
        self.assertEqual(self.state.connections[1].closed, 1)

    def test_context_exit_closes_once_and_closed_client_cannot_dispatch(self):
        with self.assertRaisesRegex(RuntimeError, "FAKE_CONTEXT_FAILURE"):
            with self.client as client:
                self.assertIs(client, self.client)
                self.recall()
                raise RuntimeError("FAKE_CONTEXT_FAILURE")
        self.client.close()
        self.assertEqual(self.state.connections[0].closed, 1)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.recall()
        self.assertEqual(self.factory.call_count, 1)

    def test_close_failure_cannot_replace_a_confirmed_mutation(self):
        self.state.close_response = self.state.fail_close = True
        result = self.client.call("mesh_learn", {"content": "fake fact"}, operation_id="stable-id")
        self.assertEqual(result["operation_id"], "stable-id")
        self.assertEqual(self.state.connections[0].closed, 1)

    def test_http_calls_on_one_client_cannot_interleave_requests(self):
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        self.state.hold_identity = (entered, release)
        failures = []

        def run(started=None):
            if started is not None:
                started.set()
            try:
                self.recall()
            except BaseException as exc:
                failures.append(exc)

        first = threading.Thread(target=run)
        second = threading.Thread(target=run, args=(second_started,))
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            second.start()
            self.assertTrue(second_started.wait(2))
        finally:
            release.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual([row[1] for row in self.state.requests], ["GET", "POST", "GET", "POST"])
        self.assertEqual(self.factory.call_count, 1)

    def test_receipt_surface_reuses_client_and_closes_it_after_verification(self):
        from benchmarks.production_transport import ReceiptSurface

        surface = ReceiptSurface(self.profile, transport="http", backend="hash", timeout=2)
        try:
            for operation_id in ("first-operation", "second-operation", "third-operation"):
                self.assertEqual(surface.operation_receipt(operation_id)["operation_id"], operation_id)
            self.assertEqual(self.factory.call_count, 1)
        finally:
            surface.close()
        self.assertEqual(self.state.connections[0].closed, 1)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            surface.operation_receipt("another-operation")
