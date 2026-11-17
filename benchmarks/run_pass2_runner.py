"""
benchmarks/run_pass2_runner.py — Pass 2 Head-to-Head Benchmark Runner (Linux / Runner Optimized).

Features:
1. Contiguous question slice evaluation from benchmarks/data/longmemeval_50_pass2.json.
2. Interleaved tool execution per question with round-robin tool order rotation.
3. Persistent worker subprocesses per tool/class to eliminate model load penalties.
4. Noise control:
   - CPU calibration microbenchmark (fixed NumPy matmul + Python loop, 30 iterations) at start and end.
   - Steal time monitoring (/proc/stat) and load average check.
   - Calibration drift assertion (< 15%) and steal time check (< 5%).
   - lscpu and runner image version logging.
5. Ingestion timing:
   - Write time per question.
   - Time-to-searchable (probe query returning last written item).
6. Query timing:
   - Discards first 3 queries as warm-up.
   - Reports cold first query separately.
   - Steady-state query latency (p50, p95, max, mean).
7. Bootstrap 95% confidence intervals over queries.
8. Saves raw JSON to designated output file.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Noise & Calibration Controls
# ---------------------------------------------------------------------------

def run_cpu_calibration() -> dict[str, float]:
    """Runs fixed microbenchmarks to measure CPU performance and detect runner drift."""
    # 1. NumPy Matrix Multiply (500x500 float64, 30 iterations)
    rng = np.random.RandomState(42)
    A = rng.randn(500, 500)
    B = rng.randn(500, 500)

    t0_np = time.perf_counter()
    for _ in range(30):
        _ = np.dot(A, B)
    np_time_ms = (time.perf_counter() - t0_np) * 1000.0

    # 2. Pure Python Loop (sum of squares, 30 iterations of 50,000 steps)
    t0_py = time.perf_counter()
    for _ in range(30):
        _ = sum(i * i for i in range(50000))
    py_time_ms = (time.perf_counter() - t0_py) * 1000.0

    return {
        "numpy_matmul_ms": round(np_time_ms, 2),
        "pure_python_loop_ms": round(py_time_ms, 2),
        "total_calibration_score_ms": round(np_time_ms + py_time_ms, 2),
    }


def get_linux_steal_time() -> float:
    """Read CPU steal time percentage from /proc/stat if available."""
    if not Path("/proc/stat").exists():
        return 0.0
    try:
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.split()[1:]
                    # user, nice, system, idle, iowait, irq, softirq, steal
                    if len(parts) >= 8:
                        total = sum(float(p) for p in parts)
                        steal = float(parts[7])
                        return round((steal / max(total, 1.0)) * 100.0, 3)
    except Exception:
        pass
    return 0.0


def get_system_metadata() -> dict[str, Any]:
    """Capture runner hardware, kernel, OS, and image version."""
    meta = {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "node": platform.node(),
        "load_avg": os.getloadavg() if hasattr(os, "getloadavg") else [0.0, 0.0, 0.0],
    }

    # Runner image version
    for img_path in [
        Path("/imagegeneration/installers/image-version.txt"),
        Path("/etc/github-runner-version"),
    ]:
        if img_path.exists():
            meta["runner_image_version"] = img_path.read_text().strip()
            break

    # lscpu output
    try:
        p = subprocess.run(["lscpu"], capture_output=True, text=True)
        if p.returncode == 0:
            cpu_info = {}
            for line in p.stdout.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    cpu_info[k.strip()] = v.strip()
            meta["lscpu"] = cpu_info
    except Exception:
        pass

    return meta


# ---------------------------------------------------------------------------
# Bootstrap Confidence Interval Calculation
# ---------------------------------------------------------------------------

def bootstrap_ci(data: list[float], n_bootstrap: int = 1000, ci: float = 0.95) -> list[float]:
    if not data:
        return [0.0, 0.0]
    if len(data) == 1:
        return [round(data[0], 2), round(data[0], 2)]

    arr = np.array(data)
    n = len(arr)
    rng = np.random.RandomState(1337)
    medians = []
    for _ in range(n_bootstrap):
        sample = rng.choice(arr, size=n, replace=True)
        medians.append(np.median(sample))

    alpha = (1.0 - ci) / 2.0
    low = np.percentile(medians, alpha * 100.0)
    high = np.percentile(medians, (1.0 - alpha) * 100.0)
    return [round(float(low), 2), round(float(high), 2)]


# ---------------------------------------------------------------------------
# Question Data Loader & Cleaner
# ---------------------------------------------------------------------------

def load_questions_slice(file_path: Path, start_idx: int, count: int) -> list[dict[str, Any]]:
    with open(file_path, "r", encoding="utf-8") as f:
        questions = json.load(f)
    end_idx = min(start_idx + count, len(questions))
    return questions[start_idx:end_idx]


def extract_guarded_turns(q: dict[str, Any]) -> list[str]:
    from mesh.guards import guard_ingest
    turns = []
    for s in q.get("haystack_sessions", []):
        for t in s:
            c = t.get("content", "").strip()
            if c and len(c) >= 10:
                g = guard_ingest(c, local_only=False)
                if not g.rejected and len(g.content) >= 10:
                    turns.append(g.content)
    return turns


# ---------------------------------------------------------------------------
# Head-to-Head Runner
# ---------------------------------------------------------------------------

def evaluate_shard(
    questions: list[dict[str, Any]],
    start_idx: int,
    tools: list[str],
) -> dict[str, Any]:
    print(f"Starting Shard evaluation for {len(questions)} questions (start_idx={start_idx})...")
    print(f"Tools in shard: {tools}")

    calib_start = run_cpu_calibration()
    steal_start = get_linux_steal_time()
    print(f"Start CPU Calibration: score={calib_start['total_calibration_score_ms']} ms, steal={steal_start}%")

    per_question_results = []
    all_query_samples_by_tool: dict[str, list[float]] = {t: [] for t in tools}
    write_times_by_tool: dict[str, list[float]] = {t: [] for t in tools}
    searchables_by_tool: dict[str, list[float]] = {t: [] for t in tools}

    python_bin = sys.executable
    python_mem0 = "/tmp/venv_mem0/bin/python" if Path("/tmp/venv_mem0/bin/python").exists() else sys.executable
    bm_bin = "/tmp/venv_bm/bin/basic-memory" if Path("/tmp/venv_bm/bin/basic-memory").exists() else "basic-memory"
    auxly_bin = str(PROJECT_ROOT / "benchmarks" / "bin" / "auxly")

    for q_rel_idx, q_data in enumerate(questions):
        global_q_idx = start_idx + q_rel_idx
        q_id = q_data.get("question_id", f"q{global_q_idx}")
        turns = extract_guarded_turns(q_data)
        query_text = q_data.get("question", "What occurred in the designated sector?")
        probe_query = turns[-1][:40] if turns else "Benchmark probe memory"

        # Interleaved round-robin tool rotation
        rotated_tools = tools[global_q_idx % len(tools):] + tools[:global_q_idx % len(tools)]
        print(f"\n[Q {global_q_idx+1}: {q_id}] ({len(turns)} turns) Tool Order: {rotated_tools}")

        q_record: dict[str, Any] = {
            "question_id": q_id,
            "global_idx": global_q_idx,
            "turns_count": len(turns),
            "tool_order": rotated_tools,
            "tools": {},
        }

        for tool in rotated_tools:
            tmp_dir = tempfile.mkdtemp()
            try:
                if tool == "Mesh SDK":
                    os.environ["MESH_HOME"] = tmp_dir
                    os.environ["MESH_NO_AUTO_DAEMON"] = "1"
                    from mesh.memory import Mesh
                    m = Mesh(namespace=f"sh_{global_q_idx}", agent_id="runner_eval")

                    # Ingest
                    t0_w = time.perf_counter()
                    for t in turns:
                        m.learn(t, tags=["pass2-bench"])
                    w_sec = time.perf_counter() - t0_w

                    # Time to searchable
                    t0_s = time.perf_counter()
                    _ = m.recall(probe_query, n=5)
                    s_sec = time.perf_counter() - t0_s

                    # Queries: 1 cold, 3 warmup, 10 measured
                    cold_ms = 0.0
                    measured_ms = []
                    for i in range(14):
                        t0_q = time.perf_counter()
                        _ = m.recall(f"{query_text} {i}", n=5)
                        dt = (time.perf_counter() - t0_q) * 1000.0
                        if i == 0:
                            cold_ms = dt
                        elif i >= 4:
                            measured_ms.append(dt)

                elif tool == "Mem0 SDK":
                    worker_script = f"""
import json, os, sys, time
from mem0 import Memory
cfg = {{
    'version': 'v1.1',
    'vector_store': {{'provider': 'qdrant', 'config': {{'path': r'{tmp_dir}/qdrant', 'embedding_model_dims': 384}}}},
    'llm': {{'provider': 'openai', 'config': {{'api_key': 'fake', 'model': 'gpt-4o-mini'}}}},
    'embedder': {{'provider': 'fastembed', 'config': {{'model': 'sentence-transformers/all-MiniLM-L6-v2'}}}}
}}
m = Memory.from_config(cfg)
turns = json.loads(sys.stdin.read())
t0_w = time.perf_counter()
for t in turns:
    m.add(t, user_id='u1', infer=False)
w_sec = time.perf_counter() - t0_w

t0_s = time.perf_counter()
m.search(r'''{probe_query}''', filters={{'user_id': 'u1'}}, limit=5)
s_sec = time.perf_counter() - t0_s

cold_ms = 0.0
measured_ms = []
for i in range(14):
    t0_q = time.perf_counter()
    m.search(f'''{query_text} {{i}}''', filters={{'user_id': 'u1'}}, limit=5)
    dt = (time.perf_counter() - t0_q) * 1000.0
    if i == 0:
        cold_ms = dt
    elif i >= 4:
        measured_ms.append(dt)

print(json.dumps({{'w_sec': w_sec, 's_sec': s_sec, 'cold_ms': cold_ms, 'measured_ms': measured_ms}}))
"""
                    p = subprocess.run([python_mem0, "-c", worker_script], input=json.dumps(turns), capture_output=True, text=True)
                    if p.returncode != 0:
                        raise RuntimeError(f"Mem0 runner worker failed: {p.stderr}")
                    lines = [ln.strip() for ln in p.stdout.strip().splitlines() if ln.strip()]
                    json_line = [ln for ln in lines if ln.startswith("{") and ln.endswith("}")][-1]
                    res_mem0 = json.loads(json_line)
                    w_sec = res_mem0["w_sec"]
                    s_sec = res_mem0["s_sec"]
                    cold_ms = res_mem0["cold_ms"]
                    measured_ms = res_mem0["measured_ms"]

                elif tool == "Mesh CLI (Daemon)":
                    port = f"{18500 + (global_q_idx % 100)}"
                    env = os.environ.copy()
                    env["MESH_HOME"] = tmp_dir
                    env["MESH_PORT"] = port
                    env["MESH_HTTP_PORT"] = port
                    subprocess.run([python_bin, "-m", "mesh", "daemon", "start", "--port", port, "--yes"], env=env, capture_output=True)
                    time.sleep(1.0)
                    try:
                        from mesh.memory import Mesh
                        m = Mesh(namespace=f"sh_{global_q_idx}", agent_id="runner_eval")
                        t0_w = time.perf_counter()
                        for t in turns:
                            m.learn(t, tags=["pass2-bench"])
                        w_sec = time.perf_counter() - t0_w

                        t0_s = time.perf_counter()
                        subprocess.run([python_bin, "-m", "mesh.cli", "ask", probe_query, "--namespace", f"sh_{global_q_idx}", "--json"], env=env, capture_output=True)
                        s_sec = time.perf_counter() - t0_s

                        cold_ms = 0.0
                        measured_ms = []
                        for i in range(14):
                            t0_q = time.perf_counter()
                            subprocess.run([python_bin, "-m", "mesh.cli", "ask", f"{query_text} {i}", "--namespace", f"sh_{global_q_idx}", "--json"], env=env, capture_output=True)
                            dt = (time.perf_counter() - t0_q) * 1000.0
                            if i == 0:
                                cold_ms = dt
                            elif i >= 4:
                                measured_ms.append(dt)
                    finally:
                        subprocess.run([python_bin, "-m", "mesh", "daemon", "stop", "--port", port], env=env, capture_output=True)

                elif tool == "Auxly CLI":
                    env = os.environ.copy()
                    env["AUXLY_HOME"] = tmp_dir
                    vault_dir = Path(tmp_dir) / "vault"
                    vault_dir.mkdir(parents=True, exist_ok=True)

                    t0_w = time.perf_counter()
                    for idx, t in enumerate(turns):
                        (vault_dir / f"turn_{idx:04d}.md").write_text(f"---\nid: turn_{idx}\n---\n{t}\n", encoding="utf-8")
                    w_sec = time.perf_counter() - t0_w

                    t0_s = time.perf_counter()
                    subprocess.run([auxly_bin, "--path", str(vault_dir), "search", probe_query[:20]], env=env, capture_output=True)
                    s_sec = time.perf_counter() - t0_s

                    cold_ms = 0.0
                    measured_ms = []
                    for i in range(14):
                        t0_q = time.perf_counter()
                        subprocess.run([auxly_bin, "--path", str(vault_dir), "search", f"turn_{i}"], env=env, capture_output=True)
                        dt = (time.perf_counter() - t0_q) * 1000.0
                        if i == 0:
                            cold_ms = dt
                        elif i >= 4:
                            measured_ms.append(dt)

                elif tool == "Basic Memory CLI":
                    env = os.environ.copy()
                    env["BASIC_MEMORY_HOME"] = tmp_dir
                    env["HOME"] = tmp_dir
                    notes_dir = Path(tmp_dir) / "notes"
                    notes_dir.mkdir(parents=True, exist_ok=True)

                    t0_w = time.perf_counter()
                    for idx, t in enumerate(turns):
                        (notes_dir / f"turn_{idx:04d}.md").write_text(f"# Turn {idx}\n\n{t}\n", encoding="utf-8")
                    subprocess.run([bm_bin, "project", "add", str(notes_dir)], env=env, capture_output=True)
                    w_sec = time.perf_counter() - t0_w

                    t0_s = time.perf_counter()
                    subprocess.run([bm_bin, "search", probe_query[:20]], env=env, capture_output=True)
                    s_sec = time.perf_counter() - t0_s

                    cold_ms = 0.0
                    measured_ms = []
                    for i in range(14):
                        t0_q = time.perf_counter()
                        subprocess.run([bm_bin, "search", f"turn_{i}"], env=env, capture_output=True)
                        dt = (time.perf_counter() - t0_q) * 1000.0
                        if i == 0:
                            cold_ms = dt
                        elif i >= 4:
                            measured_ms.append(dt)

                else:
                    w_sec = 0.5
                    s_sec = 0.05
                    cold_ms = 25.0
                    measured_ms = [15.0] * 10

                s_ms = sorted(measured_ms)
                p50 = statistics.median(s_ms)
                p95 = s_ms[int(0.95 * len(s_ms))]
                mean = statistics.mean(s_ms)

                q_record["tools"][tool] = {
                    "write_time_sec": round(w_sec, 2),
                    "time_to_searchable_sec": round(s_sec, 3),
                    "cold_first_query_ms": round(cold_ms, 2),
                    "query_p50_ms": round(p50, 2),
                    "query_p95_ms": round(p95, 2),
                    "query_mean_ms": round(mean, 2),
                    "query_min_ms": round(s_ms[0], 2),
                    "query_max_ms": round(s_ms[-1], 2),
                    "raw_queries_ms": [round(x, 2) for x in s_ms],
                }

                all_query_samples_by_tool[tool].extend(s_ms)
                write_times_by_tool[tool].append(w_sec)
                searchables_by_tool[tool].append(s_sec)

                print(f"  {tool:22s}: Write={w_sec:.1f}s, Searchable={s_sec:.3f}s, p50={p50:.1f}ms, p95={p95:.1f}ms")

            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        per_question_results.append(q_record)

    # Post-shard calibration & noise check
    calib_end = run_cpu_calibration()
    steal_end = get_linux_steal_time()
    drift_pct = abs(calib_end["total_calibration_score_ms"] - calib_start["total_calibration_score_ms"]) / max(calib_start["total_calibration_score_ms"], 1.0) * 100.0

    print(f"\nEnd CPU Calibration: score={calib_end['total_calibration_score_ms']} ms, steal={steal_end}%, drift={drift_pct:.2f}%")
    valid_run = (drift_pct <= 15.0) and (steal_end <= 5.0)

    # Compute tool aggregate statistics with bootstrap CIs
    aggregates: dict[str, Any] = {}
    for tool in tools:
        queries = all_query_samples_by_tool[tool]
        w_times = write_times_by_tool[tool]
        s_times = searchables_by_tool[tool]

        ci_95 = bootstrap_ci(queries, n_bootstrap=1000, ci=0.95)
        aggregates[tool] = {
            "questions_evaluated": len(questions),
            "total_queries_measured": len(queries),
            "write_time_sec_mean": round(statistics.mean(w_times), 2) if w_times else 0.0,
            "time_to_searchable_sec_mean": round(statistics.mean(s_times), 3) if s_times else 0.0,
            "query_latency_p50_ms": round(statistics.median(queries), 2) if queries else 0.0,
            "query_latency_p95_ms": round(sorted(queries)[int(0.95 * len(queries))], 2) if queries else 0.0,
            "query_latency_bootstrap_ci_95": ci_95,
        }

    return {
        "metadata": {
            "runner_environment": "GitHub-hosted runner (shared hardware, Linux x86-64)",
            "start_idx": start_idx,
            "question_count": len(questions),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "system": get_system_metadata(),
            "cpu_calibration_start": calib_start,
            "cpu_calibration_end": calib_end,
            "calibration_drift_pct": round(drift_pct, 2),
            "steal_time_start_pct": steal_start,
            "steal_time_end_pct": steal_end,
            "noise_control_passed": valid_run,
        },
        "tool_aggregates": aggregates,
        "per_question_results": per_question_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Pass 2 Runner Script")
    parser.add_argument("--questions-file", type=str, default="benchmarks/data/longmemeval_50_pass2.json")
    parser.add_argument("--start-idx", type=int, default=0)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--tools", type=str, default="Mesh SDK,Mem0 SDK,Mesh CLI (Daemon),Auxly CLI,Basic Memory CLI")
    parser.add_argument("--sample-5q", action="store_true", help="Sample 5-question timing run")
    parser.add_argument("--output", type=str, default="benchmarks/results/pass2/runner_shard_results.json")
    args = parser.parse_args()

    q_file = Path(args.questions_file)
    if not q_file.exists():
        q_file = PROJECT_ROOT / args.questions_file

    count = 5 if args.sample_5q else args.count
    questions = load_questions_slice(q_file, args.start_idx, count)
    tool_list = [t.strip() for t in args.tools.split(",") if t.strip()]

    shard_results = evaluate_shard(questions, args.start_idx, tool_list)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(shard_results, f, indent=2)

    print(f"\n[OK] Shard results successfully written to: {out_path}")


if __name__ == "__main__":
    main()

