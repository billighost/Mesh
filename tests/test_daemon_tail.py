"""
tests/test_daemon_tail.py — Unit tests for Windows daemon tail latency, timeouts,
and real process tracking fixes.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import mesh.cli as cli_mod
import mesh.daemon as daemon_mod
from mesh.cli import _daemon_request
from mesh.daemon import (
    _is_mesh_daemon_process,
    start_daemon,
    stop_daemon,
    verify_daemon_identity,
)


@pytest.fixture(autouse=True)
def isolated_mesh_env(tmp_path, monkeypatch):
    """Ensure all daemon tests run isolated from the real ~/.mesh directory."""
    mesh_home = tmp_path / "mesh_home"
    mesh_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MESH_HOME", str(mesh_home))
    monkeypatch.setenv("MESH_NO_AUTO_DAEMON", "1")
    monkeypatch.setenv("MESH_NO_MODEL_DOWNLOAD", "1")
    monkeypatch.setenv("MESH_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("MESH_NO_DAEMON_PROXY", raising=False)
    monkeypatch.delenv("MESH_DAEMON_REQUEST_TIMEOUT", raising=False)
    monkeypatch.delenv("MESH_DAEMON_VERIFY_TIMEOUT", raising=False)

    # Clear identity cache
    with daemon_mod._VERIFIED_DAEMON_LOCK:
        daemon_mod._VERIFIED_DAEMON_CACHE.clear()

    # Clear CLI http pool
    with cli_mod._CLI_HTTP_POOL_LOCK:
        cli_mod._CLI_HTTP_POOL.clear()

    return mesh_home


# ---------------------------------------------------------------------------
# 1. _is_mesh_daemon_process: Windows fast-path wmic + robust PowerShell fallback
# ---------------------------------------------------------------------------

def test_is_mesh_daemon_process_dead_pid(monkeypatch):
    """If PID is dead, return False immediately without invoking external commands."""
    monkeypatch.setattr(daemon_mod, "_is_pid_alive", lambda pid: False)
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "check_output", mock_run)

    assert _is_mesh_daemon_process(99999) is False
    mock_run.assert_not_called()


def test_is_mesh_daemon_process_windows_wmic_success(monkeypatch):
    """On Windows, wmic succeeds and identifies mesh daemon process without PowerShell."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(daemon_mod, "_is_pid_alive", lambda pid: True)

    wmic_output = b"CommandLine   \r\r\n\"C:\\Python311\\python.exe\" -m mesh.http_server --port 7701\r\r\n"

    def fake_check_output(cmd, **kwargs):
        if cmd[0] == "wmic":
            assert cmd[1:6] == ["process", "where", "ProcessId=12345", "get", "CommandLine"]
            return wmic_output
        raise AssertionError("PowerShell should not be called when wmic succeeds")

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    assert _is_mesh_daemon_process(12345) is True


def test_is_mesh_daemon_process_windows_wmic_non_mesh(monkeypatch):
    """On Windows, wmic succeeds on non-mesh process and returns False without PowerShell."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(daemon_mod, "_is_pid_alive", lambda pid: True)

    wmic_output = b"CommandLine\r\r\nnotepad.exe C:\\notes.txt\r\r\n"

    def fake_check_output(cmd, **kwargs):
        if cmd[0] == "wmic":
            return wmic_output
        raise AssertionError("PowerShell should not be called when wmic determines non-mesh")

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    assert _is_mesh_daemon_process(12345) is False


def test_is_mesh_daemon_process_windows_wmic_no_instance(monkeypatch):
    """On Windows, if wmic reports 'No Instance(s) Available', return False immediately."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(daemon_mod, "_is_pid_alive", lambda pid: True)

    def fake_check_output(cmd, **kwargs):
        if cmd[0] == "wmic":
            return b"No Instance(s) Available.\r\r\n"
        raise AssertionError("PowerShell should not be called when wmic reports no instance")

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    assert _is_mesh_daemon_process(12345) is False


def test_is_mesh_daemon_process_windows_wmic_fails_powershell_fallback_success(monkeypatch):
    """On Windows, if wmic fails or times out, fall back to PowerShell with 5.0s timeout."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(daemon_mod, "_is_pid_alive", lambda pid: True)

    called_commands = []

    def fake_check_output(cmd, **kwargs):
        called_commands.append((cmd[0], kwargs.get("timeout")))
        if cmd[0] == "wmic":
            raise subprocess.CalledProcessError(1, cmd)
        if cmd[0] == "powershell":
            assert kwargs.get("timeout") == 5.0
            return b"C:\\Python311\\python.exe -m mesh.daemon"
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    assert _is_mesh_daemon_process(12345) is True
    assert called_commands == [("wmic", 2.0), ("powershell", 5.0)]


def test_is_mesh_daemon_process_windows_both_fail(monkeypatch):
    """If both wmic and PowerShell fail, return False cleanly without unhandled exceptions."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(daemon_mod, "_is_pid_alive", lambda pid: True)

    def fake_check_output(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1.0))

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    assert _is_mesh_daemon_process(12345) is False


# ---------------------------------------------------------------------------
# 2. _daemon_request: Configurable timeout from MESH_DAEMON_REQUEST_TIMEOUT
# ---------------------------------------------------------------------------

def test_daemon_request_uses_default_timeout(monkeypatch):
    """_daemon_request uses default 10.0s timeout when env var is unset."""
    monkeypatch.setattr(daemon_mod, "verify_daemon_identity", lambda **k: {"identity_verified": True})
    (daemon_mod.get_mesh_dir() / "daemon.pid").write_text("1234", encoding="utf-8")

    captured_timeouts = []

    class FakeConn:
        def __init__(self, host, port, timeout):
            captured_timeouts.append(timeout)

        def request(self, *a, **k):
            pass

        def getresponse(self):
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps({"ok": True}).encode("utf-8")
            return resp

    monkeypatch.setattr(cli_mod, "_FastHTTPConnection", FakeConn)

    res = _daemon_request("/search", payload={"q": "test"})
    assert res == {"ok": True}
    assert captured_timeouts == [10.0]


def test_daemon_request_uses_env_timeout(monkeypatch):
    """_daemon_request reads MESH_DAEMON_REQUEST_TIMEOUT at runtime."""
    monkeypatch.setenv("MESH_DAEMON_REQUEST_TIMEOUT", "18.5")
    monkeypatch.setattr(daemon_mod, "verify_daemon_identity", lambda **k: {"identity_verified": True})
    (daemon_mod.get_mesh_dir() / "daemon.pid").write_text("1234", encoding="utf-8")

    captured_timeouts = []

    class FakeConn:
        def __init__(self, host, port, timeout):
            captured_timeouts.append(timeout)

        def request(self, *a, **k):
            pass

        def getresponse(self):
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps({"data": 123}).encode("utf-8")
            return resp

    monkeypatch.setattr(cli_mod, "_FastHTTPConnection", FakeConn)

    res = _daemon_request("/context", payload={"tokens": 100})
    assert res == {"data": 123}
    assert captured_timeouts == [18.5]


def test_daemon_request_enforces_minimum_timeout(monkeypatch):
    """_daemon_request enforces minimum=1.0s even if env sets a tiny value."""
    monkeypatch.setenv("MESH_DAEMON_REQUEST_TIMEOUT", "0.1")
    monkeypatch.setattr(daemon_mod, "verify_daemon_identity", lambda **k: {"identity_verified": True})
    (daemon_mod.get_mesh_dir() / "daemon.pid").write_text("1234", encoding="utf-8")

    captured_timeouts = []

    class FakeConn:
        def __init__(self, host, port, timeout):
            captured_timeouts.append(timeout)

        def request(self, *a, **k):
            pass

        def getresponse(self):
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = b"{}"
            return resp

    monkeypatch.setattr(cli_mod, "_FastHTTPConnection", FakeConn)

    _daemon_request("/ping")
    assert captured_timeouts == [1.0]


def test_daemon_request_explicit_timeout_override(monkeypatch):
    """Explicit timeout parameter overrides env var."""
    monkeypatch.setenv("MESH_DAEMON_REQUEST_TIMEOUT", "20.0")
    monkeypatch.setattr(daemon_mod, "verify_daemon_identity", lambda **k: {"identity_verified": True})
    (daemon_mod.get_mesh_dir() / "daemon.pid").write_text("1234", encoding="utf-8")

    captured_timeouts = []

    class FakeConn:
        def __init__(self, host, port, timeout):
            captured_timeouts.append(timeout)

        def request(self, *a, **k):
            pass

        def getresponse(self):
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = b"{}"
            return resp

    monkeypatch.setattr(cli_mod, "_FastHTTPConnection", FakeConn)

    _daemon_request("/ping", timeout=3.5)
    assert captured_timeouts == [3.5]


def test_daemon_request_timeout_error_reports_configured_timeout(monkeypatch):
    """When a socket timeout occurs, error payload reports the actual timeout configured."""
    monkeypatch.setenv("MESH_DAEMON_REQUEST_TIMEOUT", "12.0")
    monkeypatch.setattr(daemon_mod, "verify_daemon_identity", lambda **k: {"identity_verified": True})
    (daemon_mod.get_mesh_dir() / "daemon.pid").write_text("1234", encoding="utf-8")

    class TimingOutConn:
        def __init__(self, host, port, timeout):
            pass

        def request(self, *a, **k):
            raise socket.timeout("timed out")

    monkeypatch.setattr(cli_mod, "_FastHTTPConnection", TimingOutConn)

    res = _daemon_request("/heavy-read")
    assert res is not None
    assert res.get("_http_error") == 504
    assert res.get("_timeout") is True
    assert "12.0s" in res.get("detail", "")


# ---------------------------------------------------------------------------
# 3. verify_daemon_identity: Configurable timeout from MESH_DAEMON_VERIFY_TIMEOUT
# ---------------------------------------------------------------------------

def test_verify_daemon_identity_default_timeout(monkeypatch):
    """verify_daemon_identity uses default 1.0s timeout when unset."""
    captured_timeout = []

    orig_http_conn = http.client.HTTPConnection

    def fake_conn(host, port, **kwargs):
        captured_timeout.append(kwargs.get("timeout"))
        conn = orig_http_conn(host, port, **kwargs)
        conn.connect = MagicMock()
        conn.sock = None
        conn.request = MagicMock()
        resp = MagicMock()
        resp.status = 500
        conn.getresponse = MagicMock(return_value=resp)
        return conn

    monkeypatch.setattr(http.client, "HTTPConnection", fake_conn)

    verify_daemon_identity("127.0.0.1", 7701)
    assert captured_timeout == [1.0]


def test_verify_daemon_identity_env_timeout(monkeypatch):
    """verify_daemon_identity respects MESH_DAEMON_VERIFY_TIMEOUT at runtime."""
    monkeypatch.setenv("MESH_DAEMON_VERIFY_TIMEOUT", "3.2")
    captured_timeout = []

    orig_http_conn = http.client.HTTPConnection

    def fake_conn(host, port, **kwargs):
        captured_timeout.append(kwargs.get("timeout"))
        conn = orig_http_conn(host, port, **kwargs)
        conn.connect = MagicMock()
        conn.sock = None
        conn.request = MagicMock()
        resp = MagicMock()
        resp.status = 500
        conn.getresponse = MagicMock(return_value=resp)
        return conn

    monkeypatch.setattr(http.client, "HTTPConnection", fake_conn)

    verify_daemon_identity("127.0.0.1", 7701)
    assert captured_timeout == [3.2]


def test_verify_daemon_identity_enforces_minimum_timeout(monkeypatch):
    """verify_daemon_identity enforces minimum=0.2s."""
    monkeypatch.setenv("MESH_DAEMON_VERIFY_TIMEOUT", "0.01")
    captured_timeout = []

    orig_http_conn = http.client.HTTPConnection

    def fake_conn(host, port, **kwargs):
        captured_timeout.append(kwargs.get("timeout"))
        conn = orig_http_conn(host, port, **kwargs)
        conn.connect = MagicMock()
        conn.sock = None
        conn.request = MagicMock()
        resp = MagicMock()
        resp.status = 500
        conn.getresponse = MagicMock(return_value=resp)
        return conn

    monkeypatch.setattr(http.client, "HTTPConnection", fake_conn)

    verify_daemon_identity("127.0.0.1", 7701)
    assert captured_timeout == [0.2]


def test_verify_daemon_identity_explicit_timeout_override(monkeypatch):
    """Explicit timeout overrides MESH_DAEMON_VERIFY_TIMEOUT."""
    monkeypatch.setenv("MESH_DAEMON_VERIFY_TIMEOUT", "4.0")
    captured_timeout = []

    orig_http_conn = http.client.HTTPConnection

    def fake_conn(host, port, **kwargs):
        captured_timeout.append(kwargs.get("timeout"))
        conn = orig_http_conn(host, port, **kwargs)
        conn.connect = MagicMock()
        conn.sock = None
        conn.request = MagicMock()
        resp = MagicMock()
        resp.status = 500
        conn.getresponse = MagicMock(return_value=resp)
        return conn

    monkeypatch.setattr(http.client, "HTTPConnection", fake_conn)

    verify_daemon_identity("127.0.0.1", 7701, timeout=0.45)
    assert captured_timeout == [0.45]


# ---------------------------------------------------------------------------
# 4. start_daemon and stop_daemon: Real PID tracking and process cleanup
# ---------------------------------------------------------------------------

def test_start_daemon_records_verified_server_pid_not_shim(monkeypatch):
    """In virtualenvs (especially on Windows), start_daemon writes the real verified PID."""
    fake_proc = MagicMock()
    fake_proc.pid = 1001  # Shim PID
    fake_proc.poll.return_value = None

    verified_info = {
        "status": "ok",
        "pid": 2002,  # Actual background server PID
        "version": "0.8.3",
        "identity_verified": True,
    }

    with patch("mesh.daemon.subprocess.Popen", return_value=fake_proc), patch(
        "mesh.daemon.verify_daemon_identity", return_value=verified_info
    ), patch("mesh.daemon.time.sleep", return_value=None):
        res = start_daemon(host="127.0.0.1", port=7701, wait=True)

    assert res["running"] is True
    assert res["pid"] == 2002  # Real PID returned
    pid_file = daemon_mod._pid_file()
    assert pid_file.exists()
    assert pid_file.read_text(encoding="utf-8").strip() == "2002"


def test_stop_daemon_cleans_up_real_daemon_process(monkeypatch):
    """stop_daemon terminates the tracked daemon process and removes PID file."""
    pid_file = daemon_mod._pid_file()
    pid_file.write_text("2002", encoding="utf-8")

    monkeypatch.setattr(daemon_mod, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(daemon_mod, "_is_mesh_daemon_process", lambda pid: True)
    monkeypatch.setattr(
        daemon_mod,
        "verify_daemon_identity",
        lambda host, port: {"pid": 2002, "identity_verified": True},
    )

    killed_cmds = []

    def fake_run(cmd, **kwargs):
        killed_cmds.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(os, "name", "nt")

    res = stop_daemon(host="127.0.0.1", port=7701)
    assert res["stopped"] is True
    assert res["pid"] == 2002
    assert not pid_file.exists()
    assert any("taskkill" in cmd[0] and "2002" in cmd for cmd in killed_cmds)


def test_stop_daemon_refuses_to_kill_non_mesh_process(monkeypatch):
    """stop_daemon refuses to kill a process if it is not a mesh daemon process."""
    pid_file = daemon_mod._pid_file()
    pid_file.write_text("8888", encoding="utf-8")

    monkeypatch.setattr(daemon_mod, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(daemon_mod, "_is_mesh_daemon_process", lambda pid: False)
    monkeypatch.setattr(daemon_mod, "verify_daemon_identity", lambda host, port: None)

    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)

    res = stop_daemon(host="127.0.0.1", port=7701)
    assert res["stopped"] is False
    assert res["reason"] == "pid_not_mesh"
    assert res["pid"] == 8888
    # taskkill must not be called
    mock_run.assert_not_called()
    # Stale PID file removed
    assert not pid_file.exists()
