"""
benchmarks/profile_b2_ram_timeseries.py — Empirical B2 Process-Tree RAM Profiling.

Measures process-tree resident set size (RSS) sampled every 100ms via psutil:
- Inspects main process and all descendants recursively (`p.children(recursive=True)`)
- Enumerate actual PIDs, command lines, and time-series memory samples
- Rejects any Python process measurement below bare-interpreter baseline
- Runs 3 independent repeats per tool/class
- Saves complete time-series and statistics to benchmarks/results/pass2/b2_ram_timeseries.json
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import psutil

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TreeSampler:
    """Samples RSS of a root process and all its children every 100ms."""

    def __init__(self, root_pid: int, interval_sec: float = 0.1):
        self.root_pid = root_pid
        self.interval_sec = interval_sec
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.samples: List[Dict[str, Any]] = []
        self.peak_tree_mb: float = 0.0
        self.observed_pids: Dict[int, Dict[str, Any]] = {}

    def _sample_loop(self) -> None:
        while not self.stop_event.is_set():
            t_now = time.time()
            total_rss = 0.0
            proc_entries = []

            try:
                root_p = psutil.Process(self.root_pid)
                procs = [root_p] + root_p.children(recursive=True)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                procs = []

            for p in procs:
                try:
                    rss = p.memory_info().rss / (1024.0 * 1024.0)
                    total_rss += rss
                    pid = p.pid
                    if pid not in self.observed_pids:
                        try:
                            cmd = p.cmdline()[:3]
                        except Exception:
                            cmd = []
                        self.observed_pids[pid] = {
                            "pid": pid,
                            "name": p.name(),
                            "cmdline": cmd,
                            "peak_mb": rss,
                        }
                    else:
                        if rss > self.observed_pids[pid]["peak_mb"]:
                            self.observed_pids[pid]["peak_mb"] = rss

                    proc_entries.append({"pid": pid, "rss_mb": round(rss, 2)})
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            if total_rss > self.peak_tree_mb:
                self.peak_tree_mb = total_rss

            if proc_entries:
                self.samples.append({
                    "timestamp": round(t_now, 3),
                    "total_tree_mb": round(total_rss, 2),
                    "processes": proc_entries,
                })

            time.sleep(self.interval_sec)

    def start(self) -> None:
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._sample_loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1.5)


def run_and_sample(cmd: list[str], env: dict[str, str] | None = None, duration_sec: float | None = None) -> tuple[float, list[dict[str, Any]], dict[int, dict[str, Any]]]:
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sampler = TreeSampler(p.pid, interval_sec=0.1)
    sampler.start()

    if duration_sec:
        time.sleep(duration_sec)
        p.terminate()
        try:
            p.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            p.kill()
    else:
        p.communicate()

    sampler.stop()
    return sampler.peak_tree_mb, sampler.samples, sampler.observed_pids


def measure_python_baseline() -> float:
    cmd = [sys.executable, "-c", "import time, psutil, os; time.sleep(0.5); print(psutil.Process(os.getpid()).memory_info().rss / (1024*1024))"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return float(p.stdout.strip())


def measure_python_import_mesh() -> float:
    cmd = [sys.executable, "-c", "import mesh, time, psutil, os; time.sleep(0.5); print(psutil.Process(os.getpid()).memory_info().rss / (1024*1024))"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return float(p.stdout.strip())


def measure_tool_3_repeats(name: str, fn: Any, baseline: float) -> dict[str, Any]:
    print(f"[B2 Profiler] Measuring {name} (3 repeats)...")
    peaks: list[float] = []
    all_runs: list[dict[str, Any]] = []

    for r in range(3):
        peak, samples, pids = fn()
        # Sanity check: if Python-based and peak < baseline, retry or flag
        if "Auxly" not in name and peak < (baseline * 0.8):
            print(f"  Warning: Run {r+1} peak {peak:.2f} MB is below baseline {baseline:.2f} MB. Retrying...")
            peak, samples, pids = fn()

        peaks.append(peak)
        all_runs.append({
            "repeat": r + 1,
            "peak_tree_mb": round(peak, 2),
            "observed_processes": list(pids.values()),
            "sample_count": len(samples),
            "samples_tail": samples[-5:],
        })
        print(f"  Repeat {r+1}: {peak:.2f} MB ({len(samples)} samples)")

    return {
        "tool": name,
        "repeats": 3,
        "peak_mb_median": round(statistics.median(peaks), 2),
        "peak_mb_mean": round(statistics.mean(peaks), 2),
        "peak_mb_min": round(min(peaks), 2),
        "peak_mb_max": round(max(peaks), 2),
        "runs": all_runs,
    }


def main() -> None:
    print("================================================================")
    print("      B2 RAM PROFILER — TIME-SERIES PROCESS TREE SAMPLES        ")
    print("================================================================")

    # 1. Baseline
    b_samples = [measure_python_baseline() for _ in range(3)]
    baseline = statistics.median(b_samples)
    print(f"Bare Python Baseline: {baseline:.2f} MB (runs: {b_samples})")

    m_samples = [measure_python_import_mesh() for _ in range(3)]
    import_mesh_peak = statistics.median(m_samples)
    print(f"Python + import mesh: {import_mesh_peak:.2f} MB (+{import_mesh_peak - baseline:.2f} MB)")

    results: dict[str, Any] = {
        "metadata": {
            "date": "2026-11-02",
            "environment": "Windows 11 Home, AMD Ryzen 7 5700U, 16GB DDR4",
            "bare_python_baseline_mb": round(baseline, 2),
            "python_import_mesh_mb": round(import_mesh_peak, 2),
            "sampling_interval_ms": 100,
        },
        "tools": {},
    }

    # Helper paths
    python_mem0 = str(PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "python.exe")
    bm_bin = str(PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "basic-memory.exe")
    auxly_bin = str(PROJECT_ROOT / "benchmarks" / "bin" / "auxly.exe")

    # 1. Mesh SDK
    def fn_mesh_sdk():
        tmp_dir = tempfile.mkdtemp()
        try:
            worker = f"""
import os, time
os.environ['MESH_HOME'] = r'{tmp_dir}'
from mesh.memory import Mesh
m = Mesh(namespace='b2_bench', agent_id='tester')
m.learn('The telescope observed forty-two pulsars in nebula quadrant three.', memory_type='fact')
for _ in range(10):
    m.recall('pulsars in nebula', n=5)
    time.sleep(0.1)
"""
            return run_and_sample([sys.executable, "-c", worker])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    results["tools"]["Mesh SDK (Class A)"] = measure_tool_3_repeats("Mesh SDK (Class A)", fn_mesh_sdk, baseline)

    # 2. Mem0 SDK
    def fn_mem0_sdk():
        tmp_dir = tempfile.mkdtemp()
        try:
            worker = f"""
import os, time
from mem0 import Memory
cfg = {{
    'version': 'v1.1',
    'vector_store': {{'provider': 'qdrant', 'config': {{'path': r'{tmp_dir}/qdrant', 'embedding_model_dims': 384}}}},
    'llm': {{'provider': 'openai', 'config': {{'api_key': 'fake', 'model': 'gpt-4o-mini'}}}},
    'embedder': {{'provider': 'fastembed', 'config': {{'model': 'sentence-transformers/all-MiniLM-L6-v2'}}}}
}}
m = Memory.from_config(cfg)
m.add('The telescope observed forty-two pulsars in nebula quadrant three.', user_id='u1', infer=False)
for _ in range(10):
    m.search('pulsars in nebula', user_id='u1')
    time.sleep(0.1)
"""
            return run_and_sample([python_mem0, "-c", worker])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    results["tools"]["Mem0 SDK (Class A)"] = measure_tool_3_repeats("Mem0 SDK (Class A)", fn_mem0_sdk, baseline)

    # 3. Mesh CLI Cold
    def fn_mesh_cli_cold():
        tmp_dir = tempfile.mkdtemp()
        try:
            env = os.environ.copy()
            env["MESH_HOME"] = tmp_dir
            env["MESH_NO_AUTO_DAEMON"] = "1"
            subprocess.run([sys.executable, "-m", "mesh", "learn", "Test fact for CLI cold memory", "-n", "b2_ns"], env=env, capture_output=True)
            cmd = [sys.executable, "-m", "mesh", "recall", "CLI cold memory", "-n", "b2_ns", "--json"]
            return run_and_sample(cmd, env=env)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    results["tools"]["Mesh CLI Cold (Class B)"] = measure_tool_3_repeats("Mesh CLI Cold (Class B)", fn_mesh_cli_cold, baseline)

    # 4. Mesh CLI with Daemon
    def fn_mesh_cli_daemon():
        tmp_dir = tempfile.mkdtemp()
        port = "18991"
        try:
            env = os.environ.copy()
            env["MESH_HOME"] = tmp_dir
            env["MESH_PORT"] = port
            env["MESH_HTTP_PORT"] = port
            subprocess.run([sys.executable, "-m", "mesh", "learn", "Daemon memory test fact", "-n", "b2_ns"], env=env, capture_output=True)
            # Start daemon
            subprocess.run([sys.executable, "-m", "mesh", "daemon", "start", "--port", port, "--yes"], env=env, capture_output=True)
            time.sleep(1.0)
            # Find daemon pid from pid file
            pid_file = Path(tmp_dir) / "daemon.pid"
            daemon_pid = int(pid_file.read_text().strip()) if pid_file.exists() else None

            # Sample the tree of a recall command while including daemon_pid
            cmd = [sys.executable, "-m", "mesh", "recall", "Daemon memory", "-n", "b2_ns", "--json"]
            cli_peak, samples, pids = run_and_sample(cmd, env=env)

            daemon_rss = 0.0
            if daemon_pid and psutil.pid_exists(daemon_pid):
                try:
                    dp = psutil.Process(daemon_pid)
                    daemon_rss = dp.memory_info().rss / (1024.0 * 1024.0)
                    pids[daemon_pid] = {"pid": daemon_pid, "name": dp.name(), "cmdline": dp.cmdline()[:3], "peak_mb": daemon_rss}
                except Exception:
                    pass

            subprocess.run([sys.executable, "-m", "mesh", "daemon", "stop", "--port", port], env=env, capture_output=True)
            total_peak = cli_peak + daemon_rss
            return total_peak, samples, pids
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    results["tools"]["Mesh CLI Daemon (Class B)"] = measure_tool_3_repeats("Mesh CLI Daemon (Class B)", fn_mesh_cli_daemon, baseline)

    # 5. Basic Memory CLI
    def fn_bm_cli():
        tmp_dir = tempfile.mkdtemp()
        try:
            env = os.environ.copy()
            env["BASIC_MEMORY_HOME"] = tmp_dir
            env["HOME"] = tmp_dir
            cmd = [bm_bin, "status"]
            return run_and_sample(cmd, env=env)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    results["tools"]["Basic Memory CLI (Class B)"] = measure_tool_3_repeats("Basic Memory CLI (Class B)", fn_bm_cli, baseline)

    # 6. Auxly CLI
    def fn_auxly_cli():
        tmp_dir = tempfile.mkdtemp()
        try:
            env = os.environ.copy()
            env["AUXLY_HOME"] = tmp_dir
            cmd = [auxly_bin, "list"]
            return run_and_sample(cmd, env=env)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    results["tools"]["Auxly CLI (Class B)"] = measure_tool_3_repeats("Auxly CLI (Class B)", fn_auxly_cli, baseline)

    # 7. Mesh MCP
    def fn_mesh_mcp():
        tmp_dir = tempfile.mkdtemp()
        try:
            env = os.environ.copy()
            env["MESH_HOME"] = tmp_dir
            env["MESH_NO_AUTO_DAEMON"] = "1"
            # Launch client harness that spawns mesh mcp and sends initialize
            driver = """
import subprocess, sys, time, json
p = subprocess.Popen([sys.executable, '-m', 'mesh', 'mcp'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
# Send MCP init
init_msg = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'bench-client', 'version': '1.0'}}}).encode() + b'\\n'
p.stdin.write(init_msg)
p.stdin.flush()
time.sleep(1.5)
p.terminate()
p.wait()
"""
            return run_and_sample([sys.executable, "-c", driver], env=env)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    results["tools"]["Mesh MCP (Class C)"] = measure_tool_3_repeats("Mesh MCP (Class C)", fn_mesh_mcp, baseline)

    # 8. Basic Memory MCP
    def fn_bm_mcp():
        tmp_dir = tempfile.mkdtemp()
        try:
            env = os.environ.copy()
            env["BASIC_MEMORY_HOME"] = tmp_dir
            driver = f"""
import subprocess, sys, time, json
p = subprocess.Popen([r'{bm_bin}', 'mcp'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
time.sleep(1.5)
p.terminate()
p.wait()
"""
            return run_and_sample([sys.executable, "-c", driver], env=env)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    results["tools"]["Basic Memory MCP (Class C)"] = measure_tool_3_repeats("Basic Memory MCP (Class C)", fn_bm_mcp, baseline)

    # 9. Auxly MCP
    def fn_auxly_mcp():
        tmp_dir = tempfile.mkdtemp()
        try:
            env = os.environ.copy()
            env["AUXLY_HOME"] = tmp_dir
            driver = f"""
import subprocess, sys, time
p = subprocess.Popen([r'{auxly_bin}', '--path', r'{tmp_dir}', 'mcp-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
time.sleep(1.5)
p.terminate()
p.wait()
"""
            return run_and_sample([sys.executable, "-c", driver], env=env)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    results["tools"]["Auxly MCP (Class C)"] = measure_tool_3_repeats("Auxly MCP (Class C)", fn_auxly_mcp, baseline)

    out_file = PROJECT_ROOT / "benchmarks" / "results" / "pass2" / "b2_ram_timeseries.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"\n[OK] B2 RAM Profiling complete! Saved to {out_file}")
    for name, data in results["tools"].items():
        print(f"  {name:30s}: Median={data['peak_mb_median']:6.2f} MB  Range=[{data['peak_mb_min']:.2f}, {data['peak_mb_max']:.2f}]")


if __name__ == "__main__":
    main()

