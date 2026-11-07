"""Authenticated process measurements must prove transport, receipts and cleanup."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from benchmarks import production_bench as bench
from benchmarks import production_transport as transport


@pytest.fixture
def benchmark_root():
    temporary = tempfile.TemporaryDirectory(prefix="mesh-production-bench-")
    root = Path(temporary.name)
    try:
        yield root
    finally:
        inventory = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
        assert root.resolve().parent == Path(tempfile.gettempdir()).resolve()
        assert root.name.startswith("mesh-production-bench-")
        assert all(not Path(name).is_absolute() for name in inventory)
        temporary.cleanup()


def _config(root, **overrides):
    config = {
        "root": str(root), "repo": str(bench.REPO_ROOT), "token": "a" * 32,
        "source_manifest": bench.source_manifest(bench.REPO_ROOT),
        "action": "ingest", "backend": "hash", "surface": "http", "group_commit": 0,
        "actor": "benchmark-agent-0", "timeout": 15, "mode": "scalar", "batch_size": 2,
        "indices": [0, 1], "result": str(root / "result-0.json"), "ready": str(root / "ready-0.json"),
        "journal": str(root / "journal-0.jsonl"), "go": str(root / "go.json"), **overrides,
    }
    bench.write_json(root / "benchmark-only.json", {"token": config["token"], "fixture_version": bench.FIXTURE_VERSION})
    bench.write_json(root / "config-0.json", config)
    return config


def _ack(memory_id):
    return {"status": "stored", "memory_id": memory_id, "revision": 1, "deduplicated": False}


@contextmanager
def _http_fixture(root):
    """Use fake public data to exercise real client HTTP and identity verification."""
    from mesh.client import ConnectionPolicy, ConnectionProfile, store_identity
    from mesh.operation_contract import OPERATION_RECEIPT_VERSION, receipt_capability_proof

    mesh_home = root / "mesh"
    mesh_home.mkdir(exist_ok=True)
    token = "FAKE_BENCHMARK_TOKEN"
    token_file = mesh_home / "auth_token"
    token_file.write_text(token, encoding="utf-8")
    state = {"calls": [], "requests": [], "connections": set(), "receipts": {}, "writes": 0, "mcp_status": 200,
             "wrong_identity": False, "omit_receipt": False, "inspect_rows": [], "recall_rows": [],
             "group": {"enabled": False, "committed_batches": 0, "executed_commands": 0}}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args):
            pass
        def send_json(self, code, payload):
            body = json.dumps(payload, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            state["connections"].add(self.client_address)
            parsed = urlsplit(self.path)
            state["requests"].append(("GET", parsed.path))
            if parsed.path == "/identity":
                nonce = parse_qs(parsed.query)["nonce"][0]
                identity = store_identity(mesh_home)
                signature = hmac.new(token.encode(), (nonce + "|" + identity).encode(), hashlib.sha256).hexdigest()
                self.send_json(200, {"store_id": identity,
                                     "scope_hmac": "0" * 64 if state["wrong_identity"] else signature,
                                     "operation_receipt_version": OPERATION_RECEIPT_VERSION,
                                     "receipt_hmac": receipt_capability_proof(token, nonce, identity)})
            elif parsed.path == "/stats" and self.headers.get("Authorization") == "Bearer " + token:
                self.send_json(200, {"status": "ok", "group_commit": state["group"]})
            else:
                self.send_json(401, {"detail": "Unauthorized"})
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            data = json.loads(self.rfile.read(length))
            state["requests"].append(("POST", self.path))
            if self.path != "/mcp" or self.headers.get("Authorization") != "Bearer " + token:
                self.send_json(401, {"detail": "Unauthorized"})
                return
            state["calls"].append(data)
            name = data["name"]
            if name in {"mesh_learn", "mesh_learn_many"}:
                count = len(data["arguments"]["items"]) if name == "mesh_learn_many" else 1
                rows = [_ack("fixture-" + str(state["writes"] + index)) for index in range(count)]
                state["writes"] += count
                result = {"status": "stored", "count": count, "results": rows} if name == "mesh_learn_many" else rows[0]
                receipt = {"operation_id": data["operation_id"], "operation": name, "result": result,
                           "namespace": bench.NAMESPACE, "producer_scope": "local-user",
                           "created_at": "2026-01-01T00:00:00Z"}
                state["receipts"][data["operation_id"]] = receipt
                payload = {**result, "namespace": bench.NAMESPACE, "operation_id": data["operation_id"], "replayed": False}
                if not state["omit_receipt"]:
                    payload["receipt"] = receipt
            elif name == "mesh_inspect":
                payload = {"memories": state["inspect_rows"]}
            elif name == "mesh_recall":
                payload = {"results": state["recall_rows"]}
            elif name == "mesh_operation_get":
                operation_id = data["arguments"]["operation_id"]
                payload = {"status": "recorded", "receipt": state["receipts"][operation_id]}
            else:
                payload = {"status": "error", "error": "Unknown fixture tool"}
            if state["mcp_status"] != 200:
                self.send_json(state["mcp_status"], {"detail": "Synthetic response loss after execution"})
            else:
                self.send_json(200, {"payload": payload})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name="benchmark-fake-http")
    thread.start()
    try:
        endpoint = "http://127.0.0.1:" + str(server.server_address[1])
        profile = ConnectionProfile(namespace=bench.NAMESPACE, mesh_home=mesh_home, agent_id="benchmark-agent-0",
                                    endpoint=endpoint, token_file=token_file,
                                    policy=ConnectionPolicy(include_private=False, enforce_agent_ownership=True))
        yield profile, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_stable_mutation_identity_matches_surfaces_and_changes_with_actor_or_grouping(benchmark_root):
    config = _config(benchmark_root)
    identity = bench.stable_operation_id(config, [0, 1])
    for changes in ({"surface": "receipt-sdk", "group_commit": 0}, {"surface": "http", "group_commit": 1}):
        assert bench.stable_operation_id({**config, **changes, "token": "b" * 32}, [0, 1]) == identity
    for changes in ({"actor": "benchmark-agent-1"}, {"mode": "atomic"}, {"action": "seed"}):
        assert bench.stable_operation_id({**config, **changes}, [0, 1]) != identity
    assert bench.stable_operation_id(config, [1, 0]) != identity


@pytest.mark.parametrize("mode", ["scalar", "atomic"])
def test_reviewed_sdk_and_http_send_identical_fixture_arguments_policy_and_ids(benchmark_root, mode):
    from mesh.client import ConnectionPolicy, ConnectionProfile

    config = _config(benchmark_root, mode=mode)
    with _http_fixture(benchmark_root) as (profile, state):
        http = transport.ReceiptSurface(profile, transport="http", backend="hash", timeout=2)
        sdk_profile = ConnectionProfile(namespace=profile.namespace, mesh_home=profile.mesh_home, agent_id=profile.agent_id,
                                        policy=ConnectionPolicy(include_private=False, enforce_agent_ownership=True))
        sdk = transport.ReceiptSurface(sdk_profile, transport="sdk", backend="hash", timeout=2)
        recorded = []
        def sdk_call(name, arguments, operation_id):
            recorded.append((name, arguments, operation_id))
            rows = [_ack("sdk-" + str(number)) for number in range(len(arguments.get("items", [arguments])))]
            result = {"status": "stored", "results": rows} if name == "mesh_learn_many" else rows[0]
            return {**result, "namespace": bench.NAMESPACE, "operation_id": operation_id, "replayed": False,
                    "receipt": {"operation_id": operation_id, "result": result, "operation": name,
                                "namespace": bench.NAMESPACE, "producer_scope": "local-user",
                                "created_at": "2026-01-01T00:00:00Z"}}
        sdk.client._call_sdk = sdk_call
        selected = [0, 1] if mode == "atomic" else [0]
        items = [bench.fixture_record(index) for index in selected]
        operation_id = bench.stable_operation_id(config, selected)
        assert http.write(items, mode=mode, operation_id=operation_id)[1] == []
        assert sdk.write(items, mode=mode, operation_id=operation_id)[1] == []
        request = state["calls"][0]
        assert recorded == [(request["name"], request["arguments"], request["operation_id"])]
        assert request["arguments"]["deduplicate"] is False
        assert request["arguments"]["include_private"] is False
        assert request["expected_producer_scope"] == "local-user"
        assert request["client_policy"] == {"include_private": False, "read_only": False, "enforce_agent_ownership": True}


def test_exact_lookup_and_recall_parse_the_public_tool_response_shapes(benchmark_root):
    with _http_fixture(benchmark_root) as (profile, state):
        client = transport.ReceiptSurface(profile, transport="http", backend="hash", timeout=2)
        row = {"id": "fixture-one", "content": "Public test fixture", "revision": 1}
        state["inspect_rows"] = [row]
        state["recall_rows"] = [row]
        assert client.get("fixture-one") == row
        assert client.recall("Public test fixture", n=5, include_private=False, include_quarantined=False) == [row]
        assert state["calls"][0]["arguments"]["memory_id"] == "fixture-one"
        assert state["calls"][0]["arguments"]["include_quarantined"] is False
        assert state["calls"][1]["arguments"]["include_other_possible"] is False
        state["inspect_rows"] = [row, row]
        with pytest.raises(ValueError, match="zero or one"):
            client.get("fixture-one")


def test_authenticated_telemetry_never_sends_the_token_to_an_unproven_store(benchmark_root):
    from mesh.client import ProfileError

    with _http_fixture(benchmark_root) as (profile, state):
        state["wrong_identity"] = True
        with pytest.raises(ProfileError, match="configured store identity"):
            transport.authenticated_stats(profile, timeout=2)
        assert state["requests"] == [("GET", "/identity")]


def test_uncertain_http_batch_retains_one_stable_id_and_never_replays(benchmark_root):
    config = _config(benchmark_root, mode="atomic")
    with _http_fixture(benchmark_root) as (profile, state):
        state["mcp_status"] = 500
        client = transport.ReceiptSurface(profile, transport="http", backend="hash", timeout=2)
        report = bench._ingest(client, config)
        expected = bench.stable_operation_id(config, [0, 1])
        assert len(state["calls"]) == 1 and state["writes"] == 2
        assert report["attempted"] == 2 and report["acknowledgements"] == []
        assert {row["operation_id"] for row in report["failures"]} == {expected}
        assert all(row["outcome_may_be_committed"] for row in report["failures"])
        recovered = bench.recover_progress(config)
        assert recovered["failures"] == report["failures"]
        state["mcp_status"] = 200
        assert client.operation_receipt(expected)["operation_id"] == expected


def test_bad_receipt_envelope_records_unknown_without_confirming_the_write(benchmark_root):
    config = _config(benchmark_root, indices=[0])
    with _http_fixture(benchmark_root) as (profile, state):
        state["omit_receipt"] = True
        client = transport.ReceiptSurface(profile, transport="http", backend="hash", timeout=2)
        report = bench._ingest(client, config)
        expected = bench.stable_operation_id(config, [0])
        assert len(state["calls"]) == state["writes"] == 1
        assert report["acknowledgements"] == [] and len(report["failures"]) == 1
        assert report["failures"][0]["operation_id"] == expected
        assert report["failures"][0]["outcome_may_be_committed"] is True
        accounting = bench.write_accounting([report], 1)
        assert accounting["accounting_consistent"] is True
        assert accounting["acknowledged_rows"] == 0 and accounting["unknown_outcome_rows"] == 1
        assert client.operation_receipt(expected)["result"]["memory_id"] == "fixture-0"
        assert state["writes"] == 1


def test_pre_send_identity_failure_is_not_counted_as_an_unknown_committed_write(benchmark_root):
    config = _config(benchmark_root, indices=[0])
    with _http_fixture(benchmark_root) as (profile, state):
        state["wrong_identity"] = True
        client = transport.ReceiptSurface(profile, transport="http", backend="hash", timeout=2)
        report = bench._ingest(client, config)
        assert state["calls"] == [] and state["writes"] == 0
        assert bench.write_accounting([report], 1)["unknown_outcome_rows"] == 0


@pytest.mark.parametrize("stats", [
    {}, {"group_commit": {}}, {"group_commit": {"enabled": "1"}},
    {"group_commit": {"enabled": True, "committed_batches": 1, "executed_commands": True}},
    {"group_commit": {"enabled": True, "committed_batches": -1, "executed_commands": 2}},
])
def test_group_flag_without_actual_valid_execution_metadata_cannot_pass(stats):
    with pytest.raises(RuntimeError):
        transport.checked_group_stats(stats, True)


@pytest.mark.parametrize("batches,commands,expected,verified,coalesced", [
    (2, 8, 8, True, True), (8, 8, 8, True, False), (0, 0, 8, False, False),
    (9, 8, 8, False, False), (2, 7, 8, False, False), (2, 9, 8, False, False),
])
def test_execution_counters_prove_group_use_and_distinguish_actual_coalescing(batches, commands, expected, verified, coalesced):
    before = {"enabled": True, "committed_batches": 12, "executed_commands": 12}
    after = {"enabled": True, "committed_batches": 12 + batches, "executed_commands": 12 + commands}
    report = transport.group_execution(before, after, True, expected)
    assert report["execution_verified"] is verified
    assert report["multiple_commands_per_commit_observed"] is coalesced


def test_neural_http_profile_never_claims_hash_query_encoding(monkeypatch):
    monkeypatch.setenv("MESH_QUERY_MODE", "neural")
    mesh = SimpleNamespace(query_mode="neural", recall=lambda *args, **kwargs: pytest.fail("Must not run an unavailable hash suite"))
    report = bench._query_suite(mesh, {"surface": "http"}, {}, "hash", 2)
    assert report["status"] == "unavailable" and report["actual_query_encoding_mode"] == "neural"
    assert os.environ["MESH_QUERY_MODE"] == "neural"


def test_worker_root_binding_preserves_child_temp_isolation_and_rejects_config_escapes(benchmark_root, monkeypatch):
    monkeypatch.delenv(bench._COORDINATOR_ROOT_ENV, raising=False)
    monkeypatch.delenv(bench._COORDINATOR_TEMP_ENV, raising=False)
    config = _config(benchmark_root)
    environment = bench.worker_environment(benchmark_root, benchmark_root / "worker-home", "hash", bench.REPO_ROOT)
    with tempfile.TemporaryDirectory(prefix="mesh-production-bench-") as temporary:
        other_root = Path(temporary).resolve(strict=True)
        try:
            _config(other_root)
            escaped = other_root / "nested" / "config-0.json"
            escaped.parent.mkdir()
            bench.write_json(escaped, config)
            script = (
                "import json, sys, tempfile\n"
                "from pathlib import Path\n"
                "from benchmarks import production_bench as bench\n"
                "accepted = []\n"
                "for filename in sys.argv[1:]:\n"
                "    try:\n"
                "        bench._validate_config(Path(filename))\n"
                "    except ValueError:\n"
                "        accepted.append(False)\n"
                "    else:\n"
                "        accepted.append(True)\n"
                "root = Path(sys.argv[1]).parent\n"
                "temporary = Path(tempfile.gettempdir()).resolve(strict=True)\n"
                "print(json.dumps({'accepted': accepted, 'isolated_temp': temporary != root.parent and temporary.is_relative_to(root),\n"
                "                  'runtime_loaded': any(name == 'mesh' or name.startswith('mesh.') for name in sys.modules)}))\n"
            )
            process = subprocess.run(
                [sys.executable, "-c", script, str(benchmark_root / "config-0.json"),
                 str(other_root / "config-0.json"), str(escaped)],
                cwd=bench.REPO_ROOT, env=environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=15,
            )
            assert process.returncode == 0
            assert json.loads(process.stdout) == {
                "accepted": [True, False, False], "isolated_temp": True, "runtime_loaded": False,
            }
        finally:
            inventory = sorted(str(path.relative_to(other_root)) for path in other_root.rglob("*"))
            assert all(not Path(name).is_absolute() for name in inventory)
    assert bench._validate_config(benchmark_root / "config-0.json")["root"] == str(benchmark_root)
    with monkeypatch.context() as unbound:
        unbound.setattr(tempfile, "gettempdir", lambda: environment["TMPDIR"])
        with pytest.raises(ValueError, match="coordinator-created temporary"):
            bench._validate_config(benchmark_root / "config-0.json")


def test_coordinator_rejects_unowned_roots_before_writing(benchmark_root):
    root = benchmark_root / "mesh-production-bench-nested"
    root.mkdir()
    with pytest.raises(ValueError, match="coordinator-created temporary"):
        bench.Coordinator(root, bench.REPO_ROOT, "hash", 15)
    with pytest.raises(ValueError, match="coordinator-created temporary"):
        bench.worker_environment(root, root / "worker-home", "hash", bench.REPO_ROOT)
    assert list(root.iterdir()) == []


def test_actual_http_worker_process_never_imports_mesh_or_model_runtime(benchmark_root):
    coordinator = bench.Coordinator(benchmark_root, bench.REPO_ROOT, "hash", 15)
    with _http_fixture(benchmark_root) as (profile, state):
        config = _config(benchmark_root, token=coordinator.token, action="telemetry", endpoint=profile.endpoint,
                         server_metadata={"synchronous": "FULL", "journal_mode": "wal", "sqlite_version": "3.54.1",
                                          "query_model_tag": "hash-384", "model_loaded": False})
        environment = bench.worker_environment(benchmark_root, benchmark_root / "worker-home", "hash", bench.REPO_ROOT)
        process = subprocess.Popen([sys.executable, str(bench.REPO_ROOT / "benchmarks" / "production_bench.py"),
                                    "--worker-config", str(benchmark_root / "config-0.json")],
                                   cwd=bench.REPO_ROOT, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        coordinator.processes.append(process)
        try:
            report = coordinator.collect(process, config)
        finally:
            coordinator.stop()
        assert report["exit_code"] == 0 and not report.get("worker_failure")
        assert report["metadata"]["client_loads_memory_or_model"] is False
        assert "mesh.memory" in report["metadata"]["checked_model_modules"]
        assert state["calls"] == [] and ("GET", "/stats") in state["requests"]
        assert process.poll() is not None


def test_http_client_late_model_import_fails_without_erasing_its_confirmed_acknowledgement(benchmark_root):
    coordinator = bench.Coordinator(benchmark_root, bench.REPO_ROOT, "hash", 15)
    with _http_fixture(benchmark_root) as (profile, state):
        config = _config(benchmark_root, token=coordinator.token, action="seed", indices=[0], endpoint=profile.endpoint,
                         server_metadata={"synchronous": "FULL", "journal_mode": "wal", "sqlite_version": "3.54.1",
                                          "query_model_tag": "hash-384", "model_loaded": False})
        script = benchmark_root / "late_import_client.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from benchmarks import production_bench as bench\n"
            "original = bench._receipt_surface\n"
            "def surface(config, mesh=None):\n"
            "    result = original(config, mesh)\n"
            "    write = result.write\n"
            "    def late_write(*args, **kwargs):\n"
            "        import mesh.memory\n"
            "        return write(*args, **kwargs)\n"
            "    result.write = late_write\n"
            "    return result\n"
            "bench._receipt_surface = surface\n"
            "raise SystemExit(bench.worker(Path(sys.argv[1])))\n",
            encoding="utf-8",
        )
        environment = bench.worker_environment(benchmark_root, benchmark_root / "worker-home", "hash", bench.REPO_ROOT)
        process = subprocess.Popen([sys.executable, str(script), str(benchmark_root / "config-0.json")],
                                   cwd=bench.REPO_ROOT, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        coordinator.processes.append(process)
        try:
            report = coordinator.collect(process, config)
        finally:
            coordinator.stop()
        assert report["exit_code"] == 1 and report["worker_failure"] == "HttpClientImportedModelRuntime"
        assert report["metadata"]["client_loads_memory_or_model"] is True
        assert len(report["acknowledgements"]) == report["attempted"] == 1
        assert report["failures"] == [] and len(state["calls"]) == 1
        assert process.poll() is not None


@pytest.mark.parametrize("metadata", [None, "invalid", []])
def test_malformed_server_metadata_stays_a_structured_readiness_failure(benchmark_root, monkeypatch, metadata):
    config = _config(benchmark_root, action="seed", indices=[0], server_metadata=metadata)
    closed = []
    monkeypatch.setattr(bench, "_receipt_surface", lambda *args: SimpleNamespace(close=lambda: closed.append(True)))
    assert bench.worker(benchmark_root / "config-0.json") == 1
    report = bench._read_json(Path(config["result"]))
    assert report["error_stage"] == "runtime_readiness" and report["error_type"] == "RuntimeError"
    assert report["acknowledgements"] == [] and report["attempted"] == 0
    assert closed == [True]


def test_native_observer_cannot_create_or_mutate_the_server_database(benchmark_root):
    from mesh.sqlite_runtime import sqlite3

    mesh_home = benchmark_root / "mesh"
    mesh_home.mkdir()
    with pytest.raises(FileNotFoundError):
        transport.ReadonlyStore(mesh_home, bench.NAMESPACE)
    assert list(mesh_home.iterdir()) == []
    path = mesh_home / ("mesh_" + bench.NAMESPACE + ".db")
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE memories(id TEXT, namespace TEXT, revision INTEGER, embedding_model TEXT, local_only INTEGER, quarantined INTEGER)")
        connection.execute("INSERT INTO memories VALUES(?,?,?,?,?,?)", ("fixture-one", bench.NAMESPACE, 1, "onnx-minilm-l6-v2", 0, 0))
        connection.commit()
    finally:
        connection.close()
    observer = transport.ReadonlyStore(mesh_home, bench.NAMESPACE)
    with observer._connect() as readonly:
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("DELETE FROM memories")
    result = observer.wait_for_indexed(["fixture-one"], timeout=0, expected_revisions={"fixture-one": 1}, target_model="onnx-minilm-l6-v2")
    assert result["status"] == "indexed"
    for revision, model in [(2, "onnx-minilm-l6-v2"), (1, "hash-384")]:
        result = observer.wait_for_indexed(["fixture-one"], timeout=0, expected_revisions={"fixture-one": revision}, target_model=model)
        assert result["status"] == "not_ready" and result["indexed"] == []


@pytest.mark.parametrize("scope,revision", [("principal:wrong", 1), ("local-user", 2)])
def test_native_receipt_scope_and_revision_must_match_the_public_acknowledgement(scope, revision):
    operation_id = "bench-" + "a" * 64
    entry = {**bench.acknowledgement(_ack("fixture-one"), 0), "operation_id": operation_id, "tool_name": "mesh_learn"}
    native_result = {**_ack("fixture-one"), "revision": revision}
    rows = [(operation_id, scope, "mesh_learn", "b" * 64, json.dumps(native_result))]
    mesh = SimpleNamespace(store=SimpleNamespace(_connect=lambda: nullcontext(SimpleNamespace(execute=lambda *args: rows))),
                           operation_receipt=lambda *args: {"operation": "mesh_learn", "result": _ack("fixture-one")})
    report = bench._verify_receipts(mesh, {"surface": "http"}, [entry])
    assert report["status"] == "failed" and report["errors"]["NativeReceiptAcknowledgementMismatch"] == 1


def test_receipt_transport_cause_codes_are_content_free_and_do_not_confirm_receipts():
    from mesh.client import TransportUnavailable

    operation_id = "bench-" + "a" * 64
    entry = {**bench.acknowledgement(_ack("fixture-one"), 0), "operation_id": operation_id, "tool_name": "mesh_learn"}
    rows = [(operation_id, "local-user", "mesh_learn", "b" * 64, json.dumps(_ack("fixture-one")))]

    def unavailable(_operation_id):
        cause = OSError(10048, "FAKE_SECRET_VALUE C:/Users/EXAMPLE/private.json")
        cause.winerror = 10048
        raise TransportUnavailable("FAKE_SECRET_VALUE") from cause

    mesh = SimpleNamespace(store=SimpleNamespace(_connect=lambda: nullcontext(SimpleNamespace(execute=lambda *args: rows))),
                           operation_receipt=unavailable)
    report = bench._verify_receipts(mesh, {"surface": "http"}, [entry])
    assert report["status"] == "failed" and report["publicly_confirmed_receipts"] == 0
    assert report["native_receipts"] == report["expected_operation_ids"] == 1
    assert report["errors"]["TransportUnavailable"] == 1
    assert report["transport_errors"] == {"cause_type_counts": {"OSError": 1}, "errno_counts": {"10048": 1},
                                          "winerror_counts": {"10048": 1}}
    assert "FAKE_SECRET_VALUE" not in json.dumps(report)
    assert "C:/Users" not in json.dumps(report)


def test_http_receipt_reads_reuse_one_authenticated_connection_and_close_it(benchmark_root):
    from mesh.client import MeshClient

    with _http_fixture(benchmark_root) as (profile, state):
        with MeshClient(profile, transport="http", timeout=2) as client:
            client.call("mesh_learn", {"content": "fake fact", "deduplicate": False}, operation_id="stable-id")
            for _ in range(3):
                reply = client.call("mesh_operation_get", {"operation_id": "stable-id"})
                assert reply["receipt"]["operation_id"] == "stable-id"
            assert len(state["connections"]) == 1
            assert [method for method, _ in state["requests"]] == ["GET", "POST"] * 4
        assert client._http_connection is None


def test_missing_server_startup_publication_does_not_wait_until_the_full_deadline(benchmark_root):
    coordinator = bench.Coordinator(benchmark_root, bench.REPO_ROOT, "hash", 15, surface="http")
    dead = SimpleNamespace(poll=lambda: 1)
    coordinator.server = (dead, {"ready": str(benchmark_root / "never-ready.json")})
    with pytest.raises(RuntimeError, match="HttpServerExitedBeforeBarrier"):
        coordinator.wait_ready([(SimpleNamespace(poll=lambda: None), {"ready": str(benchmark_root / "worker-ready.json")})])
    coordinator.server = None


def test_surface_binding_and_stop_marker_cannot_escape_the_new_temporary_root(benchmark_root):
    _config(benchmark_root, surface="receipt-sdk", group_commit=1)
    with pytest.raises(ValueError, match="surface or group-commit"):
        bench._validate_config(benchmark_root / "config-0.json")
    _config(benchmark_root, action="serve", stop=str(benchmark_root.parent / "outside-stop.json"))
    with pytest.raises(ValueError, match="stop marker"):
        bench._validate_config(benchmark_root / "config-0.json")


def test_telemetry_failure_after_ingestion_preserves_all_confirmed_write_accounting(monkeypatch):
    class FailedTelemetryCoordinator(bench.Coordinator):
        telemetry_calls = 0
        def start_http_server(self):
            return {"endpoint": "http://127.0.0.1:1", "metadata": {}}
        def launch(self, action, **options):
            config = {"action": action, "actor": options["actor"], "indices": options["indices"],
                      "mode": options.get("mode", "scalar"), "batch_size": 2, **options}
            return SimpleNamespace(), config
        def collect(self, process, config, **options):
            report = bench._empty_write_report()
            report.update(attempted=len(config["indices"]), ingest_completed_ns=time.perf_counter_ns(), metadata={})
            for index in config["indices"]:
                row = bench.acknowledgement(_ack("fixture-" + str(index)), index)
                row["operation_id"] = bench.stable_operation_id(config, [index])
                report["acknowledgements"].append(row)
            return report
        def telemetry(self):
            self.telemetry_calls += 1
            if self.telemetry_calls == 2:
                raise RuntimeError("Synthetic monitoring failure after successful writes")
            return {"enabled": False, "committed_batches": 0, "executed_commands": 0}
        def wait_ready(self, pending):
            pass
        def stop(self):
            pass
    monkeypatch.setattr(bench, "Coordinator", FailedTelemetryCoordinator)
    options = SimpleNamespace(repo_root=bench.REPO_ROOT, timeout=15, memories=2,
                              batch_size=2, model_dir=None, query_repetitions=1)
    report = bench.run_case(options, 1, "scalar", "hash", "http", 0)
    assert report["status"] == "failed" and report["error_stage"] == "verify_actual_group_commit_execution"
    assert report["writes"]["attempted_rows"] == report["writes"]["acknowledged_rows"] == 2
    assert report["writes"]["unknown_outcome_rows"] == 0
    assert len(report["writes"]["acknowledgements"]) == 2


@pytest.mark.parametrize("mode,flag,agents,memories", [("scalar", 0, 1, 4), ("atomic", 1, 1, 4), ("scalar", 1, 16, 16)])
def test_real_authenticated_server_worker_and_receipts_stop_cleanly(mode, flag, agents, memories):
    runtime = Path(os.environ.get("MESH_PRODUCTION_BENCH_RUNTIME", bench.REPO_ROOT)).resolve()
    options = SimpleNamespace(repo_root=runtime, timeout=90, memories=memories, batch_size=2,
                              model_dir=None, query_repetitions=1)
    report = bench.run_case(options, agents, mode, "hash", "http", flag)
    assert report["status"] == "completed", report
    assert report["writes"]["attempted_rows"] == report["writes"]["acknowledged_rows"] == memories
    assert report["writes"]["deduplicated_acknowledgements"] == 0
    assert report["group_commit_execution"]["enabled"] is bool(flag)
    assert report["group_commit_execution"]["execution_verified"] is True
    verification = report["readiness_and_retrieval"]
    assert verification["integrity_ok"] is True and verification["exact_ids_visible"] == memories + 12
    assert verification["operation_receipts"]["status"] == "confirmed"
    assert verification["quick_check"] == ["ok"] and verification["foreign_key_errors"] == 0
    assert verification["dense_readiness"]["status"] == "unavailable"
    assert all(worker["client_loads_memory_or_model"] is False for worker in report["worker_metadata"])
    assert len(report["worker_metadata"]) == agents
    assert report["http_server_shutdown"]["exit_code"] == 0
    assert report["http_server_shutdown"]["graceful_shutdown"] is True
    assert report["http_server_shutdown"]["stop_marker_observed"] is True
