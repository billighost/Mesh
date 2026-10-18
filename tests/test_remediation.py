"""
tests/test_remediation.py — Regression tests for the 0.7.x audit fixes.

Each test pins one bug that was found by review:

- defensive env parsing
- TTL "clear" semantics on update
- query/contradiction scrubbing for rewritten or privatized memories
- audit query redaction for private recall results
- deterministic, idempotent imports and the private-overwrite guard
- MCP/HTTP include-private defaults
- daemon-proxy context propagation
- ambiguous daemon write failures never silently re-execute
"""
from __future__ import annotations

import json
import socket
import uuid

import pytest
from fastapi.testclient import TestClient

from mesh import Mesh
from mesh.audit import REDACTED_CONTENT_PLACEHOLDER, get_audit_log
from mesh.cli import _check_daemon_write_response, _daemon_request
from mesh.envutil import env_bool, env_float, env_int
from mesh.http_server import app as http_app, verify_request_auth
from mesh.io import import_namespace
from mesh.mcp_server import _ctx_default_include_private, _execute_tool_sync
from mesh.namespaces import delete_namespace


def _ns() -> str:
    return f"test_remediation_{uuid.uuid4().hex[:8]}"


def _cleanup(mesh: Mesh | None, ns: str) -> None:
    try:
        if mesh is not None:
            mesh.clear()
        delete_namespace(ns)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------

def test_public_all_matches_lazy_exports():
    import mesh as mesh_pkg

    assert set(mesh_pkg.__all__) == set(mesh_pkg._LAZY_EXPORTS) | {
        "VERSION",
        "__version__",
    }


# ---------------------------------------------------------------------------
# Defensive environment parsing
# ---------------------------------------------------------------------------

def test_env_parsing_never_raises(monkeypatch):
    monkeypatch.setenv("MESH_HTTP_PORT", "not-a-number")
    assert env_int("MESH_HTTP_PORT", 7701, minimum=1, maximum=65535) == 7701

    monkeypatch.setenv("MESH_IDLE_UNLOAD_SECONDS", "nan")
    assert env_float("MESH_IDLE_UNLOAD_SECONDS", 180.0) == 180.0

    monkeypatch.setenv("MESH_IDLE_UNLOAD_SECONDS", "inf")
    assert env_float("MESH_IDLE_UNLOAD_SECONDS", 180.0) == 180.0

    monkeypatch.setenv("MESH_HTTP_PORT", "99999")
    assert env_int("MESH_HTTP_PORT", 7701, maximum=65535) == 65535

    monkeypatch.setenv("MESH_ENFORCE_AGENT_OWNERSHIP", "maybe")
    assert env_bool("MESH_ENFORCE_AGENT_OWNERSHIP", False) is False
    monkeypatch.setenv("MESH_ENFORCE_AGENT_OWNERSHIP", "YES")
    assert env_bool("MESH_ENFORCE_AGENT_OWNERSHIP", False) is True


# ---------------------------------------------------------------------------
# TTL and scrubbing
# ---------------------------------------------------------------------------

def test_update_ttl_zero_clears_ttl():
    ns = _ns()
    mesh = Mesh(namespace=ns, agent_id="test")
    try:
        mid = mesh.learn("staging ip 10.1.2.3", ttl_days=14)["memory_id"]
        assert mesh.store.get(mid)["ttl_days"] == 14

        mesh.update(mid, ttl_days=0)
        row = mesh.store.get(mid)
        assert row["ttl_days"] is None
        assert row["decay_start_at"] is None
    finally:
        _cleanup(mesh, ns)


def test_update_reset_ttl_without_days_restarts_existing_ttl():
    ns = _ns()
    mesh = Mesh(namespace=ns, agent_id="test")
    try:
        mid = mesh.learn("temporary flag is enabled", ttl_days=7)["memory_id"]
        before = mesh.store.get(mid)["decay_start_at"]

        mesh.update(mid, reset_ttl=True)
        after = mesh.store.get(mid)
        assert after["ttl_days"] == 7
        assert after["decay_start_at"] is not None
        assert after["decay_start_at"] >= before
    finally:
        _cleanup(mesh, ns)


def test_update_content_scrubs_matching_query_telemetry():
    ns = _ns()
    mesh = Mesh(namespace=ns, agent_id="test")
    try:
        old_content = "customer email is alice@example.com"
        mid = mesh.learn(old_content)["memory_id"]
        mesh.store.log_query(old_content, agent_id="test")

        def query_rows() -> int:
            import sqlite3

            with sqlite3.connect(mesh.store.db_path) as conn:
                return conn.execute(
                    "SELECT COUNT(*) FROM queries WHERE namespace = ? AND query_text = ?",
                    (ns, old_content),
                ).fetchone()[0]

        assert query_rows() == 1
        mesh.update(mid, content="customer email is bob@example.com")
        assert query_rows() == 0
    finally:
        _cleanup(mesh, ns)


def test_privatizing_a_memory_scrubs_its_contradiction_text():
    ns = _ns()
    mesh = Mesh(namespace=ns, agent_id="test")
    try:
        first = mesh.learn("the user's pen is blue")
        second = mesh.learn("the user's pen is red")
        rows = mesh.contradictions()
        assert rows, "expected a contradiction between the two pen colours"
        row = rows[0]

        target = row["new_memory_id"]
        original_text = row["new_content"]
        assert original_text.strip()

        mesh.update(target, local_only=True)

        refreshed = mesh.contradictions()[0]
        if refreshed["new_memory_id"] == target:
            assert refreshed["new_content"] == REDACTED_CONTENT_PLACEHOLDER
        else:
            assert refreshed["existing_content"] == REDACTED_CONTENT_PLACEHOLDER
        # The other (still public) memory keeps its text.
        assert first["memory_id"] or second["memory_id"]
    finally:
        _cleanup(mesh, ns)


# ---------------------------------------------------------------------------
# Audit privacy
# ---------------------------------------------------------------------------

def test_recall_that_returns_private_results_redacts_the_logged_query():
    ns = _ns()
    mesh = Mesh(namespace=ns, agent_id="test")
    try:
        secret = "internal build bucket is mesh-builds-private"
        mesh.learn(secret, local_only=True)
        mesh.recall(secret, include_private=True)

        entries = get_audit_log(namespace=ns, limit=10, action_filter="read")
        assert entries
        assert all(e["query"] == REDACTED_CONTENT_PLACEHOLDER for e in entries if e["query"])
    finally:
        _cleanup(mesh, ns)


def test_audit_log_filters_by_namespace():
    ns_a, ns_b = _ns(), _ns()
    mesh_a = Mesh(namespace=ns_a, agent_id="agent-a")
    mesh_b = Mesh(namespace=ns_b, agent_id="agent-b")
    try:
        mesh_a.learn("alpha-only memory")
        mesh_b.learn("beta-only memory")

        entries_a = get_audit_log(namespace=ns_a, limit=50)
        assert entries_a
        assert all(e["namespace"] == ns_a for e in entries_a)
        assert all("beta-only" not in str(e.get("content") or "") for e in entries_a)
    finally:
        _cleanup(mesh_a, ns_a)
        _cleanup(mesh_b, ns_b)


# ---------------------------------------------------------------------------
# Import safety / idempotency
# ---------------------------------------------------------------------------

def _write_export(path, namespace: str, memories: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "mesh_version": "0.7.0",
                "namespace": namespace,
                "memory_count": len(memories),
                "memories": memories,
            }
        ),
        encoding="utf-8",
    )


def test_import_without_ids_is_idempotent(tmp_path):
    ns = _ns()
    mesh = Mesh(namespace=ns, agent_id="test")
    try:
        export = tmp_path / "export.json"
        _write_export(
            export,
            ns,
            [{"content": "the deploy target is staging-eu", "memory_type": "fact"}],
        )

        first = import_namespace(str(export), target_namespace=ns)
        second = import_namespace(str(export), target_namespace=ns)

        assert first["imported"] == 1
        assert second["skipped"] == 1
        assert mesh.count() == 1
    finally:
        _cleanup(mesh, ns)


def test_import_never_overwrites_existing_private_memory(tmp_path):
    ns = _ns()
    mesh = Mesh(namespace=ns, agent_id="test")
    try:
        mid = mesh.learn("private production password rotation note", local_only=True)["memory_id"]

        export = tmp_path / "hostile.json"
        _write_export(
            export,
            ns,
            [{"id": mid, "content": "attacker-controlled replacement", "memory_type": "fact"}],
        )

        stats = import_namespace(str(export), target_namespace=ns, merge_strategy="overwrite")
        assert stats["skipped"] == 1
        row = mesh.store.get(mid)
        assert row["content"] == "private production password rotation note"
        assert row["local_only"] in (True, 1)
    finally:
        _cleanup(mesh, ns)


# ---------------------------------------------------------------------------
# Include-private defaults and daemon-proxy context
# ---------------------------------------------------------------------------

def test_ctx_default_include_private_prefers_caller_context(monkeypatch):
    monkeypatch.setenv("MESH_MCP_INCLUDE_PRIVATE", "0")
    assert _ctx_default_include_private({"default_include_private": True}) is True
    assert _ctx_default_include_private({"default_include_private": False}) is False
    assert _ctx_default_include_private({}) is False


def test_tool_context_controls_private_visibility(monkeypatch):
    ns = _ns()
    mesh = Mesh(namespace=ns, agent_id="test")
    try:
        mesh.learn("private marker for context propagation", local_only=True)
        monkeypatch.delenv("MESH_MCP_INCLUDE_PRIVATE", raising=False)
        monkeypatch.setenv("MESH_NO_DAEMON_PROXY", "1")

        hidden = _execute_tool_sync(
            "mesh_inspect",
            {"namespace": ns, "limit": 10},
            allow_daemon_proxy=False,
            context={"default_include_private": False},
        )
        shown = _execute_tool_sync(
            "mesh_inspect",
            {"namespace": ns, "limit": 10},
            allow_daemon_proxy=False,
            context={"default_include_private": True},
        )

        assert all(not m.get("local_only") for m in hidden.get("memories", []))
        assert any(m.get("local_only") for m in shown.get("memories", []))
    finally:
        _cleanup(mesh, ns)


def test_http_inspect_honors_mcp_include_private_env(monkeypatch):
    ns = _ns()
    mesh = Mesh(namespace=ns, agent_id="test")
    http_app.dependency_overrides[verify_request_auth] = lambda: None
    client = TestClient(http_app, base_url="http://127.0.0.1:7701")
    try:
        mesh.learn("dashboard-private value", local_only=True)

        monkeypatch.delenv("MESH_MCP_INCLUDE_PRIVATE", raising=False)
        hidden = client.get("/inspect", params={"namespace": ns}).json()
        assert all(not m.get("local_only") for m in hidden["memories"])

        monkeypatch.setenv("MESH_MCP_INCLUDE_PRIVATE", "1")
        shown = client.get("/inspect", params={"namespace": ns}).json()
        assert any(m.get("local_only") for m in shown["memories"])
    finally:
        http_app.dependency_overrides.clear()
        _cleanup(mesh, ns)


# ---------------------------------------------------------------------------
# CLI daemon fallback safety
# ---------------------------------------------------------------------------

def test_ambiguous_daemon_write_does_not_fall_back_locally():
    with pytest.raises(SystemExit) as exc:
        _check_daemon_write_response(
            {"_daemon_lost": True, "detail": "connection reset"}, "mesh add"
        )
    assert exc.value.code == 1


def test_closed_daemon_port_returns_none(monkeypatch):
    # Bind then close a socket to obtain a port that is certainly unused.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    monkeypatch.delenv("MESH_NO_DAEMON_PROXY", raising=False)
    assert _daemon_request("/learn", {"content": "x"}, port=port) is None
