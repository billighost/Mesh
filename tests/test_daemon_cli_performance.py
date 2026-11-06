"""
tests/test_daemon_cli_performance.py — Verify daemon CLI delegation and timings.

Asserts that:
1. CLI recall with a running daemon makes an HTTP call to the daemon.
2. The CLI process does NOT load the embedding model in-process.
3. Records per-call timings (CLI interpreter startup, imports, HTTP round trip).
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def _script_json(script, environment, *, timeout=45):
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, f"Child process failed: {result.stderr}"
    payloads = [line.removeprefix("TEST_RESULT=") for line in result.stdout.splitlines() if line.startswith("TEST_RESULT=")]
    assert len(payloads) == 1, f"Expected one child result: {result.stdout}"
    return json.loads(payloads[0])


def test_daemon_cli_delegation_and_no_inprocess_model(tmp_path: Path, record_property):
    """Verify CLI ask with daemon running delegates over HTTP and does not load model in CLI."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        test_port = str(probe.getsockname()[1])
    mesh_home = tmp_path / "mesh"
    mesh_home.mkdir()
    env = os.environ.copy()
    env.update({
        "MESH_HOME": str(mesh_home),
        "MESH_DIR": str(mesh_home),
        "MESH_PORT": test_port,
        "MESH_HTTP_HOST": "127.0.0.1",
        "MESH_HTTP_PORT": test_port,
        "MESH_NO_AUTO_DAEMON": "1",
        "MESH_NO_UPDATE_CHECK": "1",
        "MESH_NO_MODEL_DOWNLOAD": "1",
        "MESH_NO_IMPLICIT_CLOUD": "1",
        "MESH_NO_DAEMON_PROXY": "0",
        "MESH_AUTO_APPROVE": "0",
        "MESH_ALLOW_DAEMON_DOWNLOAD": "0",
        "MESH_ENABLE_AUTO_SYNC": "0",
        "MESH_NON_INTERACTIVE": "1",
    })

    seed_script = """
from mesh.memory import Mesh
mesh = Mesh(namespace='daemon_test_ns', agent_id='eval_agent')
mesh.learn('The cobalt telescope discovered forty-two exoplanets on sector nine.', tags=['test-fact'])
"""
    subprocess.run([sys.executable, "-c", seed_script], env=env, check=True, timeout=45)

    try:
        # The API waits for authenticated identity and readiness; no fixed delay or download approval.
        startup = _script_json("""
import os, sys, json
from mesh.daemon import start_daemon
result = start_daemon(host='127.0.0.1', port=int(os.environ['MESH_HTTP_PORT']), wait=True, wait_for_ready=True)
print('TEST_RESULT=' + json.dumps(result))
""", env)
        assert startup["running"] is True, f"Daemon start failed: {startup}"
        assert startup["ready"] is True, f"Daemon was not ready: {startup}"
        assert startup["health"]["identity_verified"] is True

        t0 = time.perf_counter()
        result = subprocess.run(
            [sys.executable, "-m", "mesh.cli", "ask", "forty-two exoplanets", "--namespace", "daemon_test_ns", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        record_property("cli_recall_ms", (time.perf_counter() - t0) * 1000.0)
        assert result.returncode == 0, f"CLI command failed: {result.stderr}"
        assert "exoplanets" in result.stdout.lower()

        check_data = _script_json("""
import sys, json
from mesh.memory import get_engine_status
status_before = get_engine_status()
from mesh import cli
daemon_calls = []
original_request = cli._daemon_request
def tracked_request(path, *args, **kwargs):
    result = original_request(path, *args, **kwargs)
    daemon_calls.append({'path': path, 'success': isinstance(result, dict) and result.get('status') == 'ok'})
    return result
cli._daemon_request = tracked_request

sys.argv = ['mesh', 'forty-two exoplanets', '--namespace', 'daemon_test_ns', '--json']
cli.ask_cmd()
status_after = get_engine_status()
print('TEST_RESULT=' + json.dumps({'model_loaded_before': status_before['model_loaded'], 'model_loaded_after': status_after['model_loaded'], 'daemon_calls': daemon_calls}))
""", env)
        assert check_data["daemon_calls"] == [{"path": "/recall", "success": True}]
        assert check_data["model_loaded_before"] is False
        assert check_data["model_loaded_after"] is False, f"Model was loaded in CLI process: {check_data}"
    finally:
        stopped = _script_json("""
import os, json, time
from mesh.daemon import stop_daemon, _is_pid_alive, _is_mesh_daemon_process
result = stop_daemon(host='127.0.0.1', port=int(os.environ['MESH_HTTP_PORT']))
pid = result.get('pid')
deadline = time.monotonic() + 5.0
while pid and _is_pid_alive(pid) and _is_mesh_daemon_process(pid) and time.monotonic() < deadline:
    time.sleep(0.05)
result['still_running'] = bool(pid and _is_pid_alive(pid) and _is_mesh_daemon_process(pid))
print('TEST_RESULT=' + json.dumps(result))
""", env, timeout=30)
        assert stopped["still_running"] is False, f"Daemon cleanup failed: {stopped}"
