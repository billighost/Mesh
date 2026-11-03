"""
tests/test_daemon_readiness.py — Tests for daemon readiness probe (/ready), startup preloading,
and timeout handling.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

import mesh.daemon as daemon_mod
import mesh.http_server as http_server_mod
from mesh.daemon import (
    get_daemon_readiness,
    start_daemon,
)


@pytest.fixture(autouse=True)
def isolated_mesh_env(tmp_path, monkeypatch):
    mesh_home = tmp_path / "mesh_home"
    mesh_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_NO_MODEL_DOWNLOAD", "1")
    monkeypatch.setenv("MESH_NO_UPDATE_CHECK", "1")

    # Clear identity cache
    with daemon_mod._VERIFIED_DAEMON_LOCK:
        daemon_mod._VERIFIED_DAEMON_CACHE.clear()

    return mesh_home


def test_ready_endpoint_status_codes():
    """Verify /ready returns 503 when warming up and 200 when ready."""
    client = TestClient(http_server_mod.app, base_url="http://127.0.0.1:7701")

    # 1. Unset event -> 503
    http_server_mod._DAEMON_READY_EVENT.clear()
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert "warming up" in resp.json()["detail"]

    # 2. Set event -> 200
    http_server_mod._DAEMON_READY_EVENT.set()
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_get_daemon_readiness():
    """Test get_daemon_readiness helper with mocked urlopen."""
    # When server returns 200
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{"status": "ready", "version": "0.8.3"}'

    with patch("urllib.request.urlopen") as mock_url:
        mock_url.return_value.__enter__.return_value = mock_resp
        res = get_daemon_readiness(host="127.0.0.1", port=7701)
        assert res is not None
        assert res["status"] == "ready"

    # When server raises error (e.g. 503 or connection refused)
    with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
        res = get_daemon_readiness(host="127.0.0.1", port=7701)
        assert res is None


def test_start_daemon_waits_for_ready(monkeypatch):
    """Test start_daemon waits for get_daemon_readiness to succeed."""
    monkeypatch.setattr(daemon_mod, "verify_daemon_identity", lambda **kw: {"identity_verified": True, "pid": 12345})

    # Mock readiness polling: first returns None, second returns ready
    calls = []
    def fake_readiness(*args, **kwargs):
        calls.append(1)
        if len(calls) >= 2:
            return {"status": "ready"}
        return None

    monkeypatch.setattr(daemon_mod, "get_daemon_readiness", fake_readiness)
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.pid = 12345
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: fake_proc)

    # Clear pid file check
    monkeypatch.setattr(daemon_mod, "_read_pid_file", lambda: None)
    monkeypatch.setattr(daemon_mod, "_write_pid_file", lambda pid: None)

    # First call with existing_ident=None, verified=mocked
    monkeypatch.setattr(daemon_mod, "verify_daemon_identity", MagicMock(side_effect=[None, {"identity_verified": True, "pid": 12345}]))

    res = start_daemon(host="127.0.0.1", port=7701, wait=True, wait_for_ready=True, lazy=False)
    assert res["running"] is True
    assert res["ready"] is True
    assert "startup_duration_seconds" in res
    assert len(calls) >= 2


def test_start_daemon_ready_timeout(monkeypatch):
    """Test start_daemon handles readiness timeout gracefully with informative error."""
    monkeypatch.setattr(daemon_mod, "_read_pid_file", lambda: None)
    monkeypatch.setattr(daemon_mod, "_write_pid_file", lambda pid: None)
    monkeypatch.setattr(daemon_mod, "verify_daemon_identity", MagicMock(side_effect=[None, {"identity_verified": True, "pid": 12345}]))

    # get_daemon_readiness always returns None (simulating model warmup taking longer than timeout)
    monkeypatch.setattr(daemon_mod, "get_daemon_readiness", lambda **kw: None)
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.pid = 12345
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: fake_proc)

    # Set very short timeout for test
    monkeypatch.setenv("MESH_DAEMON_READY_TIMEOUT", "0.2")

    res = start_daemon(host="127.0.0.1", port=7701, wait=True, wait_for_ready=True, lazy=False)
    assert res["ready"] is False
    assert "readiness probe timed out" in res["error"]
