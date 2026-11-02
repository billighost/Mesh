"""
benchmarks/run_scale_bench_pass2.py — Corrected Scale & Capacity Suite (Pass 2 Directive 9).
Rules enforced:
1. Warm-up discarded: initial initialization run is discarded from all reported statistics.
2. Timing semantics clearly defined: "Dense SIMD Recall" is matrix cosine similarity search in Python/NumPy over resident float32 vector array.
3. Workload clarity: Memories in the scale tiers are short synthetic facts (~150 chars).
4. Realistic-text LongMemEval ingestion run: 1,000 conversational turns from LongMemEval-S (~500 chars).
5. Linear RAM delta: Accurately reports RSS delta per 1,000 memories (~1.54 MB vector array + object metadata).
6. 3 repeats: Every tier is repeated 3 times; median and range [min, max] reported across repeats.
"""
from __future__ import annotations

import json
import os
import random
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.scale_benchmark import (
    generate_synthetic_corpus,
    get_process_ram_mb,
    benchmark_latencies,
)


def run_single_scale_tier(target_scale: int, corpus: List[Dict[str, Any]]) -> Dict[str, Any]:
    from mesh.memory import Mesh, _encode_texts_with_tag

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mesh_home = str(Path(td) / "mesh_home")
        os.environ["MESH_HOME"] = mesh_home
        os.environ["MESH_NO_AUTO_DAEMON"] = "1"
        ns = f"scale-{target_scale}-{uuid.uuid4().hex[:6]}"
        mesh = Mesh(namespace=ns, agent_id="scale-eval")

        initial_ram = get_process_ram_mb()

        # 1. Ingestion Throughput
        t0 = time.perf_counter()
        chunk_size = 500
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for offset in range(0, target_scale, chunk_size):
            chunk = corpus[offset : offset + chunk_size]
            texts = [m["content"] for m in chunk]
            vectors, emb_tag = _encode_texts_with_tag(texts)
            batch_items = []
            for idx, mem in enumerate(chunk):
                vec = vectors[idx].tolist()
                metadata = {
                    "memory_type": mem["memory_type"],
                    "confidence": mem["confidence"],
                    "source_agent": mem["source_agent"],
                    "tags": mem["tags"],
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "accessed_at": now_iso,
                    "access_count": 0,
                    "ttl_days": None,
                    "decay_start_at": None,
                    "embedding_model": emb_tag,
                    "trust_tier": "user",
                    "quarantined": False,
                    "secret_ref": None,
                }
                batch_items.append((mem["id"], mem["content"], vec, metadata, mem["local_only"]))
            mesh.store.upsert_batch(batch_items)
        elapsed_ingest = time.perf_counter() - t0
        throughput = target_scale / elapsed_ingest if elapsed_ingest > 0 else 0.0

        # 2. Database Footprint
        from mesh.audit import get_mesh_dir
        db_file = Path(get_mesh_dir()) / f"mesh_{ns}.db"
        wal_file = Path(get_mesh_dir()) / f"mesh_{ns}.db-wal"
        db_size_kb = (db_file.stat().st_size if db_file.exists() else 0) / 1024.0
        wal_size_kb = (wal_file.stat().st_size if wal_file.exists() else 0) / 1024.0
        total_db_kb = db_size_kb + wal_size_kb

        # 3. Vector Cache Load
        t_cache0 = time.perf_counter()
        mesh.store._get_vector_cache()
        elapsed_cache_ms = (time.perf_counter() - t_cache0) * 1000.0
        loaded_ram = get_process_ram_mb()
        ram_delta_mb = max(0.0, loaded_ram - initial_ram)

        # 4. Dense SIMD Recall Latency
        query_text = "cluster networking configuration replica timeout"
        recall_stats = benchmark_latencies("Dense SIMD Recall (Top 5)", lambda: mesh.recall(query_text, n=5), reps=15)

        # 5. Inspect Latency
        inspect_stats = benchmark_latencies("Inspect (limit 50)", lambda: mesh.inspect(limit=50), reps=15)

        # 6. Work items & Project Brief
        task_count = min(target_scale // 5, 2000)
        for ti in range(task_count):
            mesh.create_work_item(
                title=f"Task #{ti} for {ns}",
                description=f"Action item {ti} tracking deployment and maintenance",
                priority="medium" if ti % 3 == 0 else "low",
            )
        brief_stats = benchmark_latencies("Project Brief", lambda: mesh.project_brief(), reps=10)
        task_list_stats = benchmark_latencies("List Work Items", lambda: mesh.list_work_items(limit=50), reps=10)

        return {
            "scale": target_scale,
            "ingest_throughput_wps": round(throughput, 1),
            "ingest_seconds": round(elapsed_ingest, 2),
            "db_size_kb": round(total_db_kb, 1),
            "db_size_mb": round(total_db_kb / 1024.0, 2),
            "vector_cache_ms": round(elapsed_cache_ms, 2),
            "peak_ram_mb": round(loaded_ram, 1),
            "ram_delta_mb": round(ram_delta_mb, 1),
            "dense_simd_recall_p50_ms": recall_stats["p50_ms"],
            "dense_simd_recall_p95_ms": recall_stats["p95_ms"],
            "inspect_p50_ms": inspect_stats["p50_ms"],
            "project_brief_p50_ms": brief_stats["p50_ms"],
            "task_list_p50_ms": task_list_stats["p50_ms"],
        }


def aggregate_repeats(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    keys = [
        "ingest_throughput_wps",
        "ingest_seconds",
        "db_size_mb",
        "vector_cache_ms",
        "peak_ram_mb",
        "ram_delta_mb",
        "dense_simd_recall_p50_ms",
        "dense_simd_recall_p95_ms",
        "inspect_p50_ms",
        "project_brief_p50_ms",
        "task_list_p50_ms",
    ]
    agg: Dict[str, Any] = {"scale": runs[0]["scale"], "repeats": len(runs)}
    for k in keys:
        vals = [r[k] for r in runs]
        agg[k] = {
            "median": round(float(statistics.median(vals)), 2),
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "range": [round(min(vals), 2), round(max(vals), 2)],
        }
    return agg


def main():
    print("=" * 80)
    print("  MESH CORRECTED SCALE & CAPACITY BENCHMARK (PASS 2)")
    print("=" * 80)

    # 1. Warm-up run (discarded)
    print("\n[WARM-UP] Executing 500-memory warm-up run (discarded from statistics)...")
    warmup_corpus = generate_synthetic_corpus(500, seed=1337)
    _ = run_single_scale_tier(500, warmup_corpus)
    print("  Warm-up completed and discarded.")

    # 2. Main 3 repeats across synthetic scales: 1,000, 5,000, 10,000
    scales = [1000, 5000, 10000]
    num_repeats = 3
    scale_results: Dict[str, Any] = {}

    for scale in scales:
        print(f"\n[{scale:,} MEMORIES] Running {num_repeats} independent repeats...")
        corpus = generate_synthetic_corpus(scale, seed=42 + scale)
        runs = []
        for rep in range(1, num_repeats + 1):
            print(f"  Repeat {rep}/{num_repeats}...")
            r = run_single_scale_tier(scale, corpus)
            runs.append(r)
            print(f"    Ingest: {r['ingest_throughput_wps']} wps, Dense SIMD Recall: {r['dense_simd_recall_p50_ms']}ms, Brief: {r['project_brief_p50_ms']}ms")
        agg = aggregate_repeats(runs)
        scale_results[f"scale_{scale}"] = agg

    # 3. Realistic-text LongMemEval Ingestion Run (1,000 turns)
    print("\n[REALISTIC-TEXT LONGMEMEVAL RUN] Ingesting 1,000 conversational turns (~500 chars/turn)...")
    ds_path = PROJECT_ROOT / "benchmarks" / "data" / "longmemeval_50_pass2.json"
    with open(ds_path, "r", encoding="utf-8") as f:
        questions = json.load(f)

    from mesh.guards import guard_ingest
    lme_turns: List[str] = []
    for q in questions:
        for s in q.get("haystack_sessions", []):
            for t in s:
                c = t.get("content", "").strip()
                if c and len(c) > 20:
                    g = guard_ingest(c, local_only=False)
                    if not g.rejected and g.content.strip():
                        lme_turns.append(g.content.strip())
                if len(lme_turns) >= 1000:
                    break
            if len(lme_turns) >= 1000:
                break
        if len(lme_turns) >= 1000:
            break

    lme_corpus: List[Dict[str, Any]] = []
    for idx, text in enumerate(lme_turns):
        lme_corpus.append({
            "id": str(uuid.uuid4()),
            "content": text,
            "memory_type": "fact",
            "confidence": 1.0,
            "tags": ["longmemeval", "realistic"],
            "source_agent": "lme_user",
            "local_only": False,
        })

    lme_runs = []
    for rep in range(1, num_repeats + 1):
        print(f"  Realistic Repeat {rep}/{num_repeats}...")
        r = run_single_scale_tier(1000, lme_corpus)
        lme_runs.append(r)
        print(f"    Ingest: {r['ingest_throughput_wps']} wps, Dense SIMD Recall: {r['dense_simd_recall_p50_ms']}ms")
    scale_results["longmemeval_1000_realistic"] = aggregate_repeats(lme_runs)

    out_file = PROJECT_ROOT / "benchmarks" / "results" / "scale_bench_pass2_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(scale_results, f, indent=2)
    print(f"\nSaved Corrected Scale Benchmark results to: {out_file}")


if __name__ == "__main__":
    main()

