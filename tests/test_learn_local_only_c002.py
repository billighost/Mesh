"""
tests/test_learn_local_only_c002.py — Regression suite for C-002.

C-002: `Mesh.learn()` did not return the stored privacy flag, so SDK callers
(and any surface that forwards the result) could not tell that the ingestion
guard had silently forced a secret-bearing memory to `local_only=True`, nor
could they distinguish public from private writes. The same gap existed on the
deduplication return path.

The contract asserted here is *stored value after guards*: every surface must
report the `local_only` value that was actually persisted, including
`True` when the caller asked for `False` but a credential was detected.

All tests use a fresh temp MESH_HOME, no daemon, no model download and no
network access.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mesh.mcp_server as mcp_mod
from mesh import Mesh
from mesh.dashboard import api as dash_app
from mesh.dashboard import verify_dashboard_auth
from mesh.http_server import app as http_app
from mesh.http_server import verify_request_auth

SECRET = "password = 'hunter2!SuperSecret'"
SECRET_CANARY = "hunter2"


@pytest.fixture
def isolated_mesh_env(monkeypatch):
    temp_dir = tempfile.mkdtemp(prefix="mesh_c002_")
    home = Path(temp_dir)
    mesh_home = home / ".mesh"
    mesh_home.mkdir(parents=True)

    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_AUTO_APPROVE", "0")
    monkeypatch.setenv("MESH_NO_MODEL_DOWNLOAD", "1")
    monkeypatch.setenv("MESH_NO_UPDATE_CHECK", "1")
    try:
        yield mesh_home
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _ns(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _read_db_row(mesh: Mesh, memory_id: str) -> tuple[int, str]:
    conn = sqlite3.connect(str(mesh.store.db_path))
    try:
        row = conn.execute(
            "SELECT local_only, content FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "memory was not persisted"
    return int(row[0]), str(row[1])


# ==============================================================================
# SDK contract
# ==============================================================================


def test_learn_public_returns_stored_local_only_false(isolated_mesh_env):
    mesh = Mesh(namespace=_ns("c002-public"), agent_id="test")
    result = mesh.learn("A perfectly ordinary public project fact")
    assert result["local_only"] is False
    stored, _ = _read_db_row(mesh, result["memory_id"])
    assert stored == 0


def test_learn_private_returns_stored_local_only_true(isolated_mesh_env):
    mesh = Mesh(namespace=_ns("c002-private"), agent_id="test")
    result = mesh.learn("A deliberately private project fact", local_only=True)
    assert result["local_only"] is True
    stored, _ = _read_db_row(mesh, result["memory_id"])
    assert stored == 1


def test_learn_secret_requested_public_returns_stored_true_and_redacts(isolated_mesh_env):
    mesh = Mesh(namespace=_ns("c002-secret"), agent_id="test")
    result = mesh.learn(SECRET, local_only=False)
    assert result["local_only"] is True
    stored_flag, stored_content = _read_db_row(mesh, result["memory_id"])
    assert stored_flag == 1
    assert SECRET_CANARY not in stored_content
    assert "REDACTED" in stored_content


def test_learn_forced_public_after_redaction_returns_stored_false(isolated_mesh_env):
    """`--force-public` on a trusted-local caller persists a public redacted row."""
    mesh = Mesh(namespace=_ns("c002-force-public"), agent_id="human", trusted_local=True)
    result = mesh.learn(SECRET, local_only=False, allow_redacted_public=True)
    assert result["local_only"] is False
    stored_flag, stored_content = _read_db_row(mesh, result["memory_id"])
    assert stored_flag == 0
    assert SECRET_CANARY not in stored_content


def test_learn_dedup_returns_stored_local_only_private(isolated_mesh_env):
    mesh = Mesh(namespace=_ns("c002-dedup-private"), agent_id="test")
    first = mesh.learn("Repeated private deployment fact", local_only=True)
    second = mesh.learn("Repeated private deployment fact", local_only=True)
    assert second["deduplicated"] is True
    assert second["memory_id"] == first["memory_id"]
    assert second["local_only"] is True
    stored_flag, _ = _read_db_row(mesh, second["memory_id"])
    assert stored_flag == 1


def test_learn_dedup_returns_stored_local_only_public(isolated_mesh_env):
    mesh = Mesh(namespace=_ns("c002-dedup-public"), agent_id="test")
    first = mesh.learn("Repeated public deployment fact")
    second = mesh.learn("Repeated public deployment fact")
    assert second["deduplicated"] is True
    assert second["memory_id"] == first["memory_id"]
    assert second["local_only"] is False
    stored_flag, _ = _read_db_row(mesh, second["memory_id"])
    assert stored_flag == 0


# ==============================================================================
# Surfaces
# ==============================================================================


def test_http_learn_reports_stored_local_only(isolated_mesh_env):
    namespace = _ns("c002-http")
    http_app.dependency_overrides[verify_request_auth] = lambda: None
    client = TestClient(http_app)
    try:
        response = client.post(
            "/learn",
            json={"content": SECRET, "namespace": namespace, "local_only": False},
        )
    finally:
        http_app.dependency_overrides.clear()

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["local_only"] is True
    stored_flag, stored_content = _read_db_row(
        Mesh(namespace=namespace, agent_id="checker"), result["memory_id"]
    )
    assert stored_flag == 1
    assert SECRET_CANARY not in stored_content


def test_dashboard_learn_reports_stored_local_only(isolated_mesh_env):
    namespace = _ns("c002-dashboard")
    dash_app.dependency_overrides[verify_dashboard_auth] = lambda: None
    client = TestClient(dash_app, base_url="http://127.0.0.1:7433")
    try:
        response = client.post(
            "/api/memories",
            json={"content": SECRET, "namespace": namespace, "local_only": False},
        )
    finally:
        dash_app.dependency_overrides.clear()

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["local_only"] is True
    stored_flag, _ = _read_db_row(
        Mesh(namespace=namespace, agent_id="checker"), result["memory_id"]
    )
    assert stored_flag == 1


def test_mcp_learn_reports_stored_local_only(isolated_mesh_env, monkeypatch):
    namespace = _ns("c002-mcp")
    monkeypatch.setattr(mcp_mod, "_try_proxy_via_daemon", lambda *a, **k: None)

    async def call(content: str, local_only: bool) -> dict:
        res = await mcp_mod.call_tool(
            "mesh_learn",
            {"content": content, "namespace": namespace, "local_only": local_only},
        )
        assert res.isError is False, res.content[0].text
        return json.loads(res.content[0].text)

    secret_result = asyncio.run(call(SECRET, False))
    assert secret_result["local_only"] is True
    stored_flag, _ = _read_db_row(
        Mesh(namespace=namespace, agent_id="checker"), secret_result["memory_id"]
    )
    assert stored_flag == 1

    public_result = asyncio.run(call("An ordinary public MCP fact", False))
    assert public_result["local_only"] is False
    stored_flag, _ = _read_db_row(
        Mesh(namespace=namespace, agent_id="checker"), public_result["memory_id"]
    )
    assert stored_flag == 0


def test_cli_add_reports_stored_local_only(isolated_mesh_env, monkeypatch, capsys):
    namespace = _ns("c002-cli")
    monkeypatch.setattr(sys, "argv", ["mesh", SECRET, "--namespace", namespace])
    # Never contact a background daemon; exercise the in-process SDK path.
    monkeypatch.setattr("mesh.cli._daemon_request", lambda *a, **k: None)

    from mesh.cli import add_cmd

    add_cmd()
    out = capsys.readouterr().out
    assert "private" in out.lower()

    mesh = Mesh(namespace=namespace, agent_id="checker")
    rows = mesh.recall("password", n=5, include_private=True)
    assert len(rows) == 1
    assert rows[0]["local_only"] is True
    stored_flag, stored_content = _read_db_row(mesh, rows[0]["id"])
    assert stored_flag == 1
    assert SECRET_CANARY not in stored_content
