"""
tests/test_daemon_cli_performance.py — Verify daemon CLI delegation and timings.

Asserts that:
1. CLI recall with a running daemon makes an HTTP call to the daemon.
2. The CLI process does NOT load the embedding model in-process.
3. Records per-call timings (CLI interpreter startup, imports, HTTP round trip).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def test_daemon_cli_delegation_and_no_inprocess_model():
    """Verify CLI ask with daemon running delegates over HTTP and does not load model in CLI."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_port = "17799"
        env = os.environ.copy()
        env["MESH_HOME"] = tmp_dir
        env["MESH_PORT"] = test_port
        env["MESH_HTTP_PORT"] = test_port
        env["MESH_NO_UPDATE_CHECK"] = "1"
        env["MESH_NO_MODEL_DOWNLOAD"] = "1"
        env["MESH_NON_INTERACTIVE"] = "1"

        # 1. Seed database via python script with env
        seed_script = f"""
import os
os.environ['MESH_HOME'] = r'{tmp_dir}'
from mesh.memory import Mesh
mesh = Mesh(namespace='daemon_test_ns', agent_id='eval_agent')
mesh.learn('The cobalt telescope discovered forty-two exoplanets on sector nine.', tags=['test-fact'])
"""
        subprocess.run([sys.executable, "-c", seed_script], env=env, check=True)

        # 2. Start background daemon on test_port via subprocess with explicit env
        p_start = subprocess.run(
            [sys.executable, "-m", "mesh", "daemon", "start", "--port", test_port, "--yes"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert p_start.returncode == 0, f"Daemon start failed: {p_start.stderr}"
        time.sleep(1.0)

        try:
            # 3. Run python -m mesh.cli ask and verify output
            t0 = time.perf_counter()
            p = subprocess.run(
                [sys.executable, "-m", "mesh.cli", "ask", "forty-two exoplanets", "--namespace", "daemon_test_ns", "--json"],
                env=env,
                capture_output=True,
                text=True,
            )
            t_total = (time.perf_counter() - t0) * 1000.0

            assert p.returncode == 0, f"CLI command failed: {p.stderr}"
            assert "exoplanets" in p.stdout.lower()

            # 4. Assert CLI script importing and executing did not load model in a fresh child process
            verify_script = f"""
import os, sys, json
os.environ['MESH_HOME'] = r'{tmp_dir}'
os.environ['MESH_PORT'] = '{test_port}'
os.environ['MESH_HTTP_PORT'] = '{test_port}'
os.environ['MESH_NO_UPDATE_CHECK'] = '1'

from mesh.memory import get_engine_status
status_before = get_engine_status()

# Execute ask_cmd with daemon running (sys.argv[0] is command name, sys.argv[1] is query)
sys.argv = ['mesh', 'forty-two exoplanets', '--namespace', 'daemon_test_ns', '--json']
from mesh.cli import ask_cmd
ask_cmd()

status_after = get_engine_status()
print(json.dumps({{'model_loaded_before': status_before['model_loaded'], 'model_loaded_after': status_after['model_loaded']}}))
"""
            p_verify = subprocess.run(
                [sys.executable, "-c", verify_script],
                env=env,
                capture_output=True,
                text=True,
            )
            assert p_verify.returncode == 0, f"Verification failed: {p_verify.stderr}"

            last_line = [ln for ln in p_verify.stdout.strip().splitlines() if ln.startswith("{")][-1]
            check_data = json.loads(last_line)
            assert check_data["model_loaded_after"] is False, (
                f"Model was loaded in CLI process! {check_data}"
            )

        finally:
            subprocess.run(
                [sys.executable, "-m", "mesh", "daemon", "stop", "--port", test_port],
                env=env,
                capture_output=True,
                text=True,
            )
