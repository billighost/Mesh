"""
benchmarks/scale_benchmark.py — Scale and capacity benchmark for Mesh.
Evaluates throughput, latency percentiles (p50, p95, p99), RAM, and database
footprint across 1,000, 5,000, and 10,000 memories.

Run:
    python benchmarks/scale_benchmark.py
    python benchmarks/scale_benchmark.py --scales 1000,5000,10000 --json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

# Console UTF-8 handling on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def get_process_ram_mb() -> float:
    """Return process resident set size (RSS) in megabytes, stdlib only."""
    if sys.platform == "win32":
        try:
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            psapi = ctypes.WinDLL("psapi")
            kernel32 = ctypes.WinDLL("kernel32")
            GetProcessMemoryInfo = psapi.GetProcessMemoryInfo
            GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            GetProcessMemoryInfo.restype = wintypes.BOOL

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            handle = kernel32.GetCurrentProcess()
            if GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize / (1024.0 * 1024.0)
        except Exception:
            pass
        return 0.0
    else:
        try:
            import resource

            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if sys.platform == "darwin":
                return rss / (1024.0 * 1024.0)
            return rss / 1024.0
        except Exception:
            return 0.0


def benchmark_latencies(label: str, fn: Callable[[], Any], reps: int = 15) -> Dict[str, float]:
    """Measure latency percentiles in milliseconds across multiple runs."""
    times_ms: List[float] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        times_ms.append(elapsed_ms)

    times_ms.sort()
    n = len(times_ms)
    p50 = statistics.median(times_ms)
    p95 = times_ms[int(n * 0.95)] if n >= 20 else times_ms[-1]
    p99 = times_ms[int(n * 0.99)] if n >= 100 else times_ms[-1]
    mean_val = statistics.mean(times_ms)
    min_val = min(times_ms)
    max_val = max(times_ms)

    return {
        "label": label,
        "reps": reps,
        "mean_ms": round(mean_val, 3),
        "min_ms": round(min_val, 3),
        "max_ms": round(max_val, 3),
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "p99_ms": round(p99, 3),
    }


def generate_synthetic_corpus(count: int, seed: int = 42) -> List[Dict[str, Any]]:
    """Generate structured synthetic memories for scale benchmarks."""
    rng = random.Random(seed)
    domains = ["database", "networking", "auth", "infra", "billing", "frontend"]
    types = ["fact", "decision", "preference", "context"]

    memories: List[Dict[str, Any]] = []
    for i in range(count):
        domain = rng.choice(domains)
        m_type = rng.choice(types)
        content = (
            f"Service {domain}-{i % 100} in cluster {domain} configured with "
            f"port={8000 + (i % 500)}, replica_count={1 + (i % 16)}, "
            f"timeout_seconds={30 + (i % 60)}. Hostname is {domain}-node-{i}.internal.net."
        )
        memories.append(
            {
                "id": str(uuid.uuid4()),
                "content": content,
                "memory_type": m_type,
                "confidence": round(0.7 + (rng.random() * 0.3), 2),
                "tags": [domain, f"cluster-{i % 20}", f"scale-{count}"],
                "source_agent": f"agent-{domain}",
                "local_only": False,
            }
        )
    return memories


def run_scale_test(target_scale: int, verbose: bool = True) -> Dict[str, Any]:
    """Run full scale benchmark for a specific memory count."""
    from mesh.audit import get_mesh_dir
    from mesh.memory import Mesh
    from mesh.namespaces import delete_namespace

    ns = f"scale-bench-{target_scale}-{uuid.uuid4().hex[:6]}"
    mesh = Mesh(namespace=ns, agent_id="scale-agent")

    initial_ram = get_process_ram_mb()
    corpus = generate_synthetic_corpus(target_scale)

    if verbose:
        print(f"\n[{target_scale:,} MEMORIES] Generating & inserting batch...")

    # 1. Batch Ingestion Throughput
    from mesh.memory import _encode_texts_with_tag

    t_ingest_start = time.perf_counter()
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
    t_ingest_elapsed = time.perf_counter() - t_ingest_start
    ingest_throughput = target_scale / t_ingest_elapsed if t_ingest_elapsed > 0 else 0

    # 2. Database Footprint
    db_file = Path(get_mesh_dir()) / f"mesh_{ns}.db"
    wal_file = Path(get_mesh_dir()) / f"mesh_{ns}.db-wal"
    db_size_kb = (db_file.stat().st_size if db_file.exists() else 0) / 1024.0
    wal_size_kb = (wal_file.stat().st_size if wal_file.exists() else 0) / 1024.0
    total_db_kb = db_size_kb + wal_size_kb

    # 3. RAM after loading Vector Cache
    loaded_ram = get_process_ram_mb()
    ram_delta_mb = max(0.0, loaded_ram - initial_ram)

    if verbose:
        print(
            f"  Ingested {target_scale:,} in {t_ingest_elapsed:.2f}s "
            f"({ingest_throughput:.1f} memories/sec) | DB size: {total_db_kb:.1f} KB | RAM delta: {ram_delta_mb:.2f} MB"
        )

    # 4. Populate work items for task and brief scale evaluation
    work_count = max(50, target_scale // 5)
    with mesh.store._connect() as conn:
        for i in range(work_count):
            w_id = f"wi-{i}-{uuid.uuid4().hex[:6]}"
            s_id = f"task-{i + 1}" if i % 4 != 0 else f"goal-{(i // 4) + 1}"
            kind = "goal" if i % 4 == 0 else "task"
            status = "done" if i % 3 == 0 else ("in_progress" if i % 3 == 1 else "todo")
            title = f"Scale work item #{i}: Optimize subsystem {i % 20}"
            completed_at = now_iso if status == "done" else None
            conn.execute(
                """
                INSERT INTO work_items (id, short_id, namespace, kind, parent_id, title,
                                        description, status, priority, created_by, created_at,
                                        started_at, completed_at, completed_by, summary,
                                        evidence, tags, local_only)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    w_id,
                    s_id,
                    ns,
                    kind,
                    None,
                    title,
                    "Synthetic description for scale benchmark",
                    status,
                    "medium",
                    "scale-agent",
                    now_iso,
                    now_iso if status != "todo" else None,
                    completed_at,
                    "scale-agent" if status == "done" else None,
                    f"Summary of work #{i}" if status == "done" else None,
                    json.dumps(["file1.py", "file2.py"]) if status == "done" else json.dumps([]),
                    json.dumps(["scale", f"sub-{i % 10}"]),
                    0,
                ),
            )
            # Log an event
            conn.execute(
                """
                INSERT INTO work_events (item_id, namespace, event_type, old_status,
                                         new_status, note, agent_id, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    w_id,
                    ns,
                    "status_change",
                    None,
                    status,
                    f"Event for {s_id}",
                    "scale-agent",
                    now_iso,
                ),
            )
        conn.commit()

    # Recalculate database footprint including work_items & events
    db_size_kb = (db_file.stat().st_size if db_file.exists() else 0) / 1024.0
    wal_size_kb = (wal_file.stat().st_size if wal_file.exists() else 0) / 1024.0
    total_db_kb = db_size_kb + wal_size_kb

    # 5. Latency Benchmarks
    # A. Dense Cosine SIMD Recall
    dense_lat = benchmark_latencies(
        "Dense SIMD Recall (top-5)",
        lambda: mesh.recall("Service database-42 in cluster database", n=5),
        reps=20,
    )

    # B. Exact Identifier / Lexical BM25 Recall
    exact_lat = benchmark_latencies(
        "Exact Token BM25 Recall",
        lambda: mesh.recall("port=8420 database-node-42.internal.net", n=5),
        reps=20,
    )

    # C. Filtered Recall (Mask over all entries)
    filtered_lat = benchmark_latencies(
        "Filtered Recall (tags=['database'], type='fact')",
        lambda: mesh.recall(
            "Service database replica_count",
            n=5,
            tags=["database"],
            memory_type="fact",
        ),
        reps=20,
    )

    # D. Full Namespace Inspection / Pagination
    inspect_lat = benchmark_latencies(
        "Namespace Inspect (top-50)",
        lambda: mesh.inspect(limit=50),
        reps=20,
    )

    # E. Project Brief (combines key facts, top tasks, recent completions)
    brief_lat = benchmark_latencies(
        "Project Brief (budget 1000 tokens)",
        lambda: mesh.project_brief(max_tokens=1000),
        reps=15,
    )

    # F. Task List Query
    task_list_lat = benchmark_latencies(
        "Task List (status=todo, limit=50)",
        lambda: mesh.list_work_items(status="todo", limit=50),
        reps=20,
    )

    # G. Changelog Generation
    changelog_lat = benchmark_latencies(
        "Changelog Generation (markdown)",
        lambda: mesh.changelog(since="7d", format="markdown"),
        reps=15,
    )

    # H. Schema Verification / Migration Timing
    def check_schema_reopen():
        from mesh.store import MeshStore

        s = MeshStore(namespace=ns)
        s._ensure_initialized()

    schema_lat = benchmark_latencies(
        "Schema Verification Reopen",
        check_schema_reopen,
        reps=10,
    )

    # Cleanup
    try:
        delete_namespace(ns)
    except Exception:
        pass

    return {
        "scale": target_scale,
        "work_items_count": work_count,
        "ingest_time_seconds": round(t_ingest_elapsed, 3),
        "ingest_throughput_eps": round(ingest_throughput, 1),
        "db_size_kb": round(total_db_kb, 1),
        "ram_delta_mb": round(ram_delta_mb, 2),
        "latencies": {
            "dense_simd": dense_lat,
            "exact_bm25": exact_lat,
            "filtered": filtered_lat,
            "inspect": inspect_lat,
            "brief": brief_lat,
            "task_list": task_list_lat,
            "changelog": changelog_lat,
            "schema_check": schema_lat,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Mesh Scale Benchmark Suite")
    parser.add_argument(
        "--scales",
        type=str,
        default="1000,5000,10000",
        help="Comma-separated memory scale tiers (e.g. 1000,5000,10000)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON for CI or automation",
    )
    args = parser.parse_args()

    scale_tiers = [int(s.strip()) for s in args.scales.split(",") if s.strip()]

    print("=" * 86)
    print("  MESH SCALE & LATENCY BENCHMARK SUITE (Memories + Work Items + Project Brief)")
    print("=" * 86)

    results: List[Dict[str, Any]] = []
    for tier in scale_tiers:
        res = run_scale_test(tier, verbose=not args.json)
        results.append(res)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    print("\n" + "=" * 96)
    print("  SCALE BENCHMARK SUMMARY")
    print("=" * 96)
    header = f"{'Scale':<9} | {'Tasks':<7} | {'Ingest':<11} | {'DB Size':<9} | {'Dense p50':<10} | {'Brief p50':<10} | {'Tasks p50':<10} | {'Schema':<8} | {'RAM'}"
    print(header)
    print("-" * 96)

    for r in results:
        scale_str = f"{r['scale']:,}"
        tasks_str = f"{r.get('work_items_count', 0):,}"
        tp_str = f"{r['ingest_throughput_eps']:.0f} mem/s"
        db_str = f"{r['db_size_kb'] / 1024.0:.2f} MB"
        p50 = f"{r['latencies']['dense_simd']['p50_ms']:.2f} ms"
        brief_p50 = f"{r['latencies']['brief']['p50_ms']:.2f} ms"
        task_p50 = f"{r['latencies']['task_list']['p50_ms']:.2f} ms"
        schema_p50 = f"{r['latencies']['schema_check']['p50_ms']:.2f} ms"
        ram_str = f"+{r['ram_delta_mb']:.1f} MB"
        print(
            f"{scale_str:<9} | {tasks_str:<7} | {tp_str:<11} | {db_str:<9} | {p50:<10} | {brief_p50:<10} | {task_p50:<10} | {schema_p50:<8} | {ram_str}"
        )
    print("=" * 96 + "\n")


if __name__ == "__main__":
    main()
