"""
benchmarks/run_b4_like_for_like.py — Rigorous Like-for-Like Ingestion Comparison.

Measures Question 0 (548 valid turns) of LongMemEval-S across:
(a) Mesh Default Mode:
    - per-turn time-to-accept (fast hashing + guards + slot checks + SQLite write)
    - total time until background re-embedding queue drains (time-to-fully-embedded)
(b) Mesh Synchronous ONNX Mode (MESH_SYNC_ONNX=1):
    - per-turn synchronous ONNX embedding + guards + slot checks + SQLite write
(c) Mem0 SDK (infer=False):
    - per-turn synchronous FastEmbed embedding + Qdrant write

Saves raw per-turn samples and summary statistics to benchmarks/results/pass2/b4_like_for_like.json.
"""

from __future__ import annotations

import json
import os
import shutil
from mesh.sqlite_runtime import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mesh.guards import guard_ingest
from benchmarks.profile_b4_real_loop import load_question_0_turns

SHARED_MODELS_DIR = Path.home() / ".mesh" / "models"
CACHE_DIR = PROJECT_ROOT / "benchmarks" / "results" / "pass2" / ".cache_b4"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def run_mesh_default(turns: List[str]) -> Dict[str, Any]:
    cache_file = CACHE_DIR / "mesh_default.json"
    if cache_file.exists():
        print("[1/3] Loading Mesh Default Mode from cache...", flush=True)
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    print("[1/3] Running Mesh Default Mode (Deferred Background ONNX)...", flush=True)
    worker_script = f"""
import json, os, shutil, sys, tempfile, time, sqlite3
from pathlib import Path
sys.path.insert(0, r"{PROJECT_ROOT}")

tmp_dir = tempfile.mkdtemp()
models_src = Path.home() / ".mesh" / "models"
if models_src.exists():
    shutil.copytree(models_src, Path(tmp_dir) / "models")

os.environ["MESH_HOME"] = tmp_dir
os.environ["MESH_NO_UPDATE_CHECK"] = "1"
os.environ["MESH_NON_INTERACTIVE"] = "1"
if "MESH_SYNC_ONNX" in os.environ:
    del os.environ["MESH_SYNC_ONNX"]

from mesh.memory import Mesh, drain_reembed_worker

turns = json.loads(sys.stdin.read())
try:
    m = Mesh(namespace="like_for_like_default", agent_id="eval")
    per_turn_ms = []
    t0_accept_phase = time.perf_counter()
    for idx, turn in enumerate(turns):
        t0 = time.perf_counter()
        m.learn(turn, tags=["b4", f"t-{{idx}}"])
        dt = (time.perf_counter() - t0) * 1000.0
        per_turn_ms.append(dt)
    accept_phase_sec = time.perf_counter() - t0_accept_phase

    t0_drain = time.perf_counter()
    drain_ok = drain_reembed_worker(timeout=180.0)
    drain_sec = time.perf_counter() - t0_drain

    db_path = os.path.join(tmp_dir, "mesh_like_for_like_default.db")
    model_counts = {{}}
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT embedding_model, COUNT(*) FROM memories GROUP BY embedding_model")
        model_counts = dict(cur.fetchall())
        conn.close()

    total_embedded_sec = accept_phase_sec + drain_sec
    res = {{
        "per_turn_accept_ms": per_turn_ms,
        "accept_phase_sec": accept_phase_sec,
        "drain_sec": drain_sec,
        "total_time_to_fully_embedded_sec": total_embedded_sec,
        "drain_success": drain_ok,
        "model_counts": model_counts,
    }}
    print(json.dumps(res))
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)
"""
    p = subprocess.run([sys.executable, "-c", worker_script], input=json.dumps(turns), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Mesh default failed:\nSTDOUT: {p.stdout}\nSTDERR: {p.stderr}")
    for line in p.stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            res = json.loads(line)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(res, f)
            return res
    raise RuntimeError(f"No JSON found in Mesh default output: {p.stdout}")


def run_mesh_sync_onnx(turns: List[str]) -> Dict[str, Any]:
    cache_file = CACHE_DIR / "mesh_sync.json"
    if cache_file.exists():
        print("[2/3] Loading Mesh MESH_SYNC_ONNX=1 Mode from cache...", flush=True)
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    print("[2/3] Running Mesh MESH_SYNC_ONNX=1 Mode (Synchronous CPU ONNX)...", flush=True)
    worker_script = f"""
import json, os, shutil, sys, tempfile, time, sqlite3
from pathlib import Path
sys.path.insert(0, r"{PROJECT_ROOT}")

tmp_dir = tempfile.mkdtemp()
models_src = Path.home() / ".mesh" / "models"
if models_src.exists():
    shutil.copytree(models_src, Path(tmp_dir) / "models")

os.environ["MESH_HOME"] = tmp_dir
os.environ["MESH_SYNC_ONNX"] = "1"
os.environ["MESH_NO_UPDATE_CHECK"] = "1"
os.environ["MESH_NON_INTERACTIVE"] = "1"

from mesh.memory import Mesh

turns = json.loads(sys.stdin.read())
try:
    m = Mesh(namespace="like_for_like_sync", agent_id="eval")
    per_turn_ms = []
    t0_sync_phase = time.perf_counter()
    for idx, turn in enumerate(turns):
        t0 = time.perf_counter()
        m.learn(turn, tags=["b4", f"t-{{idx}}"])
        dt = (time.perf_counter() - t0) * 1000.0
        per_turn_ms.append(dt)
    sync_phase_sec = time.perf_counter() - t0_sync_phase

    db_path = os.path.join(tmp_dir, "mesh_like_for_like_sync.db")
    model_counts = {{}}
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT embedding_model, COUNT(*) FROM memories GROUP BY embedding_model")
        model_counts = dict(cur.fetchall())
        conn.close()

    res = {{
        "per_turn_sync_ms": per_turn_ms,
        "sync_phase_sec": sync_phase_sec,
        "total_time_to_fully_embedded_sec": sync_phase_sec,
        "model_counts": model_counts,
    }}
    print(json.dumps(res))
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)
"""
    p = subprocess.run([sys.executable, "-c", worker_script], input=json.dumps(turns), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Mesh sync failed:\nSTDOUT: {p.stdout}\nSTDERR: {p.stderr}")
    for line in p.stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            res = json.loads(line)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(res, f)
            return res
    raise RuntimeError(f"No JSON found in Mesh sync output: {p.stdout}")


def run_mem0_sdk(turns: List[str]) -> Dict[str, Any]:
    cache_file = CACHE_DIR / "mem0_sdk.json"
    if cache_file.exists():
        print("[3/3] Loading Mem0 SDK from cache...", flush=True)
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    print("[3/3] Running Mem0 SDK (infer=False)...", flush=True)
    mem0_python = PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "python.exe"
    if not mem0_python.exists():
        mem0_python = Path(sys.executable)

    worker_script = """
import json, os, shutil, sys, tempfile, time
from mem0 import Memory

turns = json.loads(sys.stdin.read())
tmp_dir = tempfile.mkdtemp()
try:
    cfg = {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "path": os.path.join(tmp_dir, "qdrant"),
                "embedding_model_dims": 384,
                "on_disk": True,
            }
        },
        "llm": {
            "provider": "openai",
            "config": {"api_key": "fake-local-no-cloud-calls", "model": "gpt-4o-mini"}
        },
        "embedder": {
            "provider": "fastembed",
            "config": {
                "model": "sentence-transformers/all-MiniLM-L6-v2"
            }
        },
        "version": "v1.1",
        "history_db_path": os.path.join(tmp_dir, "history.db"),
    }
    m = Memory.from_config(cfg)
    per_turn_ms = []
    t0_mem0 = time.perf_counter()
    for idx, turn in enumerate(turns):
        t0 = time.perf_counter()
        m.add(turn, user_id="u1", infer=False)
        dt = (time.perf_counter() - t0) * 1000.0
        per_turn_ms.append(dt)
    mem0_phase_sec = time.perf_counter() - t0_mem0

    res = {
        "per_turn_mem0_ms": per_turn_ms,
        "mem0_phase_sec": mem0_phase_sec,
        "total_time_to_fully_embedded_sec": mem0_phase_sec,
    }
    print(json.dumps(res))
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)
"""
    p = subprocess.run([str(mem0_python), "-c", worker_script], input=json.dumps(turns), capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Mem0 failed:\nSTDOUT: {p.stdout}\nSTDERR: {p.stderr}")
    for line in p.stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            res = json.loads(line)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(res, f)
            return res
    raise RuntimeError(f"No JSON found in Mem0 output: {p.stdout}")


def compute_distribution(samples: List[float]) -> Dict[str, float]:
    s = sorted(samples)
    return {
        "mean_ms": round(statistics.mean(s), 2),
        "p50_ms": round(statistics.median(s), 2),
        "p95_ms": round(s[int(0.95 * len(s))], 2),
        "min_ms": round(s[0], 2),
        "max_ms": round(s[-1], 2),
    }


def main():
    print("================================================================")
    print("   RUNNING LIKE-FOR-LIKE INGESTION COMPARISON (QUESTION 0)     ")
    print("================================================================")

    qid, raw_turns = load_question_0_turns()
    safe_turns = [guard_ingest(t).content for t in raw_turns if not guard_ingest(t).rejected]
    print(f"Loaded Question 0 ({qid}) with {len(safe_turns)} safe, valid turns (out of {len(raw_turns)} raw turns).")

    default_res = run_mesh_default(safe_turns)
    sync_res = run_mesh_sync_onnx(safe_turns)
    mem0_res = run_mem0_sdk(safe_turns)

    default_dist = compute_distribution(default_res["per_turn_accept_ms"])
    sync_dist = compute_distribution(sync_res["per_turn_sync_ms"])
    mem0_dist = compute_distribution(mem0_res["per_turn_mem0_ms"])

    out_data = {
        "metadata": {
            "question_id": qid,
            "turns_count": len(safe_turns),
            "date": "2026-11-03",
            "environment": "AMD Ryzen 7 5700U, Windows 11 Home, Python 3.14 GA / Python 3.12 (Mem0)",
        },
        "summary": {
            "mesh_default_time_to_accept": {
                **default_dist,
                "accept_phase_total_sec": round(default_res["accept_phase_sec"], 2),
                "throughput_turns_per_sec": round(len(safe_turns) / default_res["accept_phase_sec"], 2),
            },
            "mesh_default_background_drain": {
                "drain_elapsed_sec": round(default_res["drain_sec"], 2),
                "total_time_to_fully_embedded_sec": round(default_res["total_time_to_fully_embedded_sec"], 2),
                "drain_success": default_res["drain_success"],
                "final_sqlite_model_counts": default_res["model_counts"],
            },
            "mesh_sync_onnx_mode": {
                **sync_dist,
                "sync_phase_total_sec": round(sync_res["sync_phase_sec"], 2),
                "total_time_to_fully_embedded_sec": round(sync_res["total_time_to_fully_embedded_sec"], 2),
                "throughput_turns_per_sec": round(len(safe_turns) / sync_res["sync_phase_sec"], 2),
                "final_sqlite_model_counts": sync_res["model_counts"],
            },
            "mem0_sdk_mode": {
                **mem0_dist,
                "mem0_phase_total_sec": round(mem0_res["mem0_phase_sec"], 2),
                "total_time_to_fully_embedded_sec": round(mem0_res["total_time_to_fully_embedded_sec"], 2),
                "throughput_turns_per_sec": round(len(safe_turns) / mem0_res["mem0_phase_sec"], 2),
            },
        },
        "raw_samples": {
            "mesh_default_per_turn_accept_ms": [round(x, 2) for x in default_res["per_turn_accept_ms"]],
            "mesh_sync_onnx_per_turn_ms": [round(x, 2) for x in sync_res["per_turn_sync_ms"]],
            "mem0_sdk_per_turn_ms": [round(x, 2) for x in mem0_res["per_turn_mem0_ms"]],
        },
    }

    out_file = PROJECT_ROOT / "benchmarks" / "results" / "pass2" / "b4_like_for_like.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out_data, f, indent=2)

    # Clean cache
    shutil.rmtree(CACHE_DIR, ignore_errors=True)

    print(f"\n[OK] Saved results to {out_file}")
    print("\n--- SUMMARY OF LIKE-FOR-LIKE COMPARISON ---")
    print(f"1. Mesh Default (Time to Accept): Mean={default_dist['mean_ms']}ms, p50={default_dist['p50_ms']}ms, p95={default_dist['p95_ms']}ms | Total Accept={default_res['accept_phase_sec']:.2f}s ({len(safe_turns)/default_res['accept_phase_sec']:.1f} tps)")
    print(f"   Background Re-embed Queue Drain: {default_res['drain_sec']:.2f}s | Total Time to Fully Embedded: {default_res['total_time_to_fully_embedded_sec']:.2f}s ({len(safe_turns)/default_res['total_time_to_fully_embedded_sec']:.1f} tps)")
    print(f"2. Mesh Sync ONNX (MESH_SYNC_ONNX=1): Mean={sync_dist['mean_ms']}ms, p50={sync_dist['p50_ms']}ms, p95={sync_dist['p95_ms']}ms | Total Fully Embedded={sync_res['sync_phase_sec']:.2f}s ({len(safe_turns)/sync_res['sync_phase_sec']:.1f} tps)")
    print(f"3. Mem0 SDK (infer=False): Mean={mem0_dist['mean_ms']}ms, p50={mem0_dist['p50_ms']}ms, p95={mem0_dist['p95_ms']}ms | Total Fully Embedded={mem0_res['mem0_phase_sec']:.2f}s ({len(safe_turns)/mem0_res['mem0_phase_sec']:.1f} tps)")


if __name__ == "__main__":
    main()
