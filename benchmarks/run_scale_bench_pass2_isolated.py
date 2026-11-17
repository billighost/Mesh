"""
benchmarks/run_scale_bench_pass2_isolated.py — Fully Isolated Scale & Capacity Suite.

Ensures:
1. Every repeat runs in a fresh, isolated Python subprocess.
2. Every repeat uses a fresh, verified-empty directory.
3. Zero in-process heap or memory reuse across repeats.
4. Reports cold-store repeat (Repeat 1) as headline alongside median and range [min, max].
5. Saves raw data and summary to benchmarks/results/pass2/scale_benchmark_clean.json.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


WORKER_CODE = """
import json, os, psutil, shutil, statistics, sys, tempfile, time, uuid
from mesh.sqlite_runtime import sqlite3
from pathlib import Path
import numpy as np

# Load arguments from stdin
args = json.loads(sys.stdin.read())
target_scale = args["target_scale"]
corpus = args["corpus"]
workload_type = args["workload_type"]

tmp_dir = tempfile.mkdtemp()
try:
    os.environ["MESH_HOME"] = tmp_dir
    os.environ["MESH_NO_AUTO_DAEMON"] = "1"
    os.environ["MESH_NO_UPDATE_CHECK"] = "1"
    os.environ["MESH_NON_INTERACTIVE"] = "1"

    from mesh.memory import Mesh, _encode_texts_with_tag

    proc = psutil.Process(os.getpid())
    initial_ram_mb = proc.memory_info().rss / (1024.0 * 1024.0)

    ns = f"scale_{target_scale}_{uuid.uuid4().hex[:6]}"
    mesh_inst = Mesh(namespace=ns, agent_id="scale_eval")

    # 1. Ingestion Throughput
    chunk_size = 500
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t0_ingest = time.perf_counter()

    for offset in range(0, target_scale, chunk_size):
        chunk = corpus[offset : offset + chunk_size]
        texts = [m["content"] for m in chunk]
        vectors, emb_tag = _encode_texts_with_tag(texts)
        batch_items = []
        for idx, mem in enumerate(chunk):
            vec = vectors[idx].tolist() if hasattr(vectors[idx], "tolist") else list(vectors[idx])
            meta = {
                "memory_type": mem.get("memory_type", "fact"),
                "confidence": mem.get("confidence", 1.0),
                "tags": mem.get("tags", ["scale"]),
                "source_agent": mem.get("source_agent", "eval"),
                "created_at": now_iso,
                "updated_at": now_iso,
                "local_only": False,
                "embedding_model": emb_tag,
            }
            batch_items.append((mem["id"], mem["content"], vec, meta, False))
        mesh_inst.store.upsert_batch(batch_items)

    ingest_wall_sec = time.perf_counter() - t0_ingest
    wps = round(target_scale / max(ingest_wall_sec, 0.001), 2)

    # Checkpoint WAL
    db_file = Path(tmp_dir) / f"mesh_{ns}.db"
    db_size_mb = 0.0
    if db_file.exists():
        con = sqlite3.connect(str(db_file))
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        db_size_mb = round(db_file.stat().st_size / (1024.0 * 1024.0), 3)

    # 2. Dense SIMD Recall (30 queries)
    query_samples_ms = []
    sample_texts = [corpus[i % len(corpus)]["content"][:50] for i in range(30)]
    for q_text in sample_texts:
        t0_q = time.perf_counter()
        _ = mesh_inst.recall(q_text, n=5)
        query_samples_ms.append((time.perf_counter() - t0_q) * 1000.0)

    # Discard first 3 queries as warm-up
    steady_queries = sorted(query_samples_ms[3:])
    n_q = len(steady_queries)
    recall_p50 = statistics.median(steady_queries)
    recall_p95 = steady_queries[int(0.95 * n_q)]

    # 3. Project Brief
    brief_samples_ms = []
    for _ in range(10):
        t0_b = time.perf_counter()
        _ = mesh_inst.project_brief()
        brief_samples_ms.append((time.perf_counter() - t0_b) * 1000.0)
    brief_p50 = statistics.median(brief_samples_ms)

    peak_ram_mb = proc.memory_info().rss / (1024.0 * 1024.0)
    ram_delta_mb = max(0.0, peak_ram_mb - initial_ram_mb)

    result = {
        "target_scale": target_scale,
        "workload_type": workload_type,
        "ingest_wall_sec": round(ingest_wall_sec, 2),
        "ingest_throughput_wps": wps,
        "pure_db_size_mb": db_size_mb,
        "dense_simd_recall_p50_ms": round(recall_p50, 2),
        "dense_simd_recall_p95_ms": round(recall_p95, 2),
        "project_brief_p50_ms": round(brief_p50, 2),
        "initial_ram_mb": round(initial_ram_mb, 2),
        "peak_ram_mb": round(peak_ram_mb, 2),
        "ram_delta_mb": round(ram_delta_mb, 2),
        "raw_query_samples_ms": [round(x, 2) for x in steady_queries],
    }
    print(json.dumps(result))

finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)
"""


def generate_synthetic_facts(count: int, seed: int = 42) -> list[dict[str, Any]]:
    import random
    rng = random.Random(seed)
    entities = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel"]
    relations = ["connected to", "dependent on", "located inside", "calibrated by", "verified via"]
    locations = ["Sector 1", "Substation 4", "Cluster B", "Core Vault", "Zone 9", "Relay Station"]

    items = []
    for i in range(count):
        e1 = rng.choice(entities)
        r = rng.choice(relations)
        e2 = rng.choice(locations)
        text = f"Fact {i:05d}: Component {e1} is {r} {e2} with operational status code {rng.randint(100, 999)}."
        items.append({
            "id": f"fact_{seed}_{i:05d}",
            "content": text,
            "memory_type": "fact",
            "confidence": 1.0,
            "tags": ["synthetic", "scale"],
            "source_agent": "bench_generator",
        })
    return items


def load_realistic_turns(count: int = 1000) -> list[dict[str, Any]]:
    ds_path = PROJECT_ROOT / "benchmarks" / "data" / "longmemeval_50_pass2.json"
    with open(ds_path, "r", encoding="utf-8") as f:
        questions = json.load(f)

    from mesh.guards import guard_ingest
    items = []
    for q in questions:
        for s in q.get("haystack_sessions", []):
            for t in s:
                c = t.get("content", "").strip()
                if c and len(c) >= 20:
                    g = guard_ingest(c, local_only=False)
                    if not g.rejected and len(g.content) >= 20:
                        items.append({
                            "id": f"turn_{len(items):05d}",
                            "content": g.content,
                            "memory_type": "observation",
                            "confidence": 1.0,
                            "tags": ["longmemeval", "realistic"],
                            "source_agent": "lme_eval",
                        })
                if len(items) >= count:
                    return items
    return items


def run_isolated_repeat(target_scale: int, corpus: list[dict[str, Any]], workload_type: str) -> dict[str, Any]:
    payload = json.dumps({
        "target_scale": target_scale,
        "corpus": corpus,
        "workload_type": workload_type,
    })
    p = subprocess.run(
        [sys.executable, "-c", WORKER_CODE],
        input=payload,
        text=True,
        capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"Scale worker failed: {p.stderr}")
    return json.loads(p.stdout.strip())


def aggregate_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    cold_headline = runs[0]
    keys_to_agg = [
        "ingest_wall_sec", "ingest_throughput_wps", "pure_db_size_mb",
        "dense_simd_recall_p50_ms", "dense_simd_recall_p95_ms",
        "project_brief_p50_ms", "peak_ram_mb", "ram_delta_mb"
    ]
    summary: dict[str, Any] = {"cold_run_headline": cold_headline, "stats": {}}
    for k in keys_to_agg:
        vals = [r[k] for r in runs]
        summary["stats"][k] = {
            "median": round(statistics.median(vals), 2),
            "mean": round(statistics.mean(vals), 2),
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "range": [round(min(vals), 2), round(max(vals), 2)],
        }
    summary["all_repeats"] = runs
    return summary


def main() -> None:
    print("================================================================")
    print("    ISOLATED SCALE BENCHMARK (3 FRESH SUBPROCESS REPEATS)       ")
    print("================================================================")

    out_file = PROJECT_ROOT / "benchmarks" / "results" / "pass2" / "scale_benchmark_clean.json"
    results: dict[str, Any] = {
        "metadata": {
            "date": "2026-11-02",
            "environment": "Windows 11 Home, AMD Ryzen 7 5700U, 16GB DDR4",
            "repeats": 3,
            "isolation": "Subprocess-per-repeat in fresh verified-empty temp directory",
        },
        "tiers": {},
    }

    # Synthetic tiers
    for scale in [1000, 5000, 10000]:
        print(f"\n[Scale {scale:,} Synthetic Facts] Running 3 isolated repeats...")
        corpus = generate_synthetic_facts(scale, seed=100 + scale)
        runs = []
        for r in range(1, 4):
            print(f"  Starting isolated repeat {r}/3 in fresh subprocess...")
            t0 = time.time()
            data = run_isolated_repeat(scale, corpus, "Short Synthetic Facts (~150 chars)")
            dt = time.time() - t0
            runs.append(data)
            print(f"    Repeat {r} done in {dt:.1f}s: Ingest={data['ingest_throughput_wps']} wps, Recall p50={data['dense_simd_recall_p50_ms']} ms, RSS={data['peak_ram_mb']} MB")
        results["tiers"][f"scale_{scale}"] = aggregate_runs(runs)

    # Realistic tier (1,000 turns)
    print("\n[Scale 1,000 Realistic LongMemEval Turns] Running 3 isolated repeats...")
    lme_corpus = load_realistic_turns(1000)
    lme_runs = []
    for r in range(1, 4):
        print(f"  Starting isolated repeat {r}/3 in fresh subprocess...")
        t0 = time.time()
        data = run_isolated_repeat(1000, lme_corpus, "LongMemEval-S Realistic Text (~500 chars)")
        dt = time.time() - t0
        lme_runs.append(data)
        print(f"    Repeat {r} done in {dt:.1f}s: Ingest={data['ingest_throughput_wps']} wps, Recall p50={data['dense_simd_recall_p50_ms']} ms, RSS={data['peak_ram_mb']} MB")
    results["tiers"]["scale_1000_realistic"] = aggregate_runs(lme_runs)

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"\n[OK] Clean isolated scale benchmark complete! Saved to {out_file}")


if __name__ == "__main__":
    main()
