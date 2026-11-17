"""
benchmarks/profile_ingestion_stages.py — Per-stage ingestion latency breakdown for Mesh vs Mem0.
Measures:
1. Mesh stage breakdown:
   - guards (guard_ingest, guard_tags)
   - embedding (ONNX all-MiniLM-L6-v2)
   - write lock acquisition
   - dedupe lookup (store.query vector search)
   - contradiction check
   - DB write (SQLite upsert)
   - audit write (log_learn)
2. Turn length bucketing:
   - Short (<100 chars)
   - Medium (200-500 chars)
   - Long (1000-3000 chars)
   - Very Long (>4000 chars)
3. Mem0 comparison:
   - Mem0 SDK (infer=False) write time on the same turns
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def percentile(data: List[float], p: float) -> float:
    if not data:
        return 0.0
    return float(np.percentile(np.array(data), p))


def load_turns_by_bucket() -> Dict[str, List[str]]:
    ds_path = PROJECT_ROOT / "benchmarks" / "data" / "longmemeval_50_pass2.json"
    with open(ds_path, "r", encoding="utf-8") as f:
        questions = json.load(f)

    buckets: Dict[str, List[str]] = {
        "short": [],       # < 100 chars
        "medium": [],      # 200-500 chars
        "long": [],        # 1000-3000 chars
        "very_long": [],   # > 4000 chars
    }

    for q in questions:
        for s in q.get("haystack_sessions", []):
            for turn in s:
                c = turn.get("content", "").strip()
                if not c or len(c) < 5:
                    continue
                # ensure guard accepts it
                from mesh.guards import guard_ingest
                g = guard_ingest(c, local_only=False)
                if g.rejected or not g.content.strip():
                    continue
                c = g.content.strip()
                n = len(c)
                if n < 100 and len(buckets["short"]) < 50:
                    buckets["short"].append(c)
                elif 200 <= n <= 500 and len(buckets["medium"]) < 50:
                    buckets["medium"].append(c)
                elif 1000 <= n <= 3000 and len(buckets["long"]) < 50:
                    buckets["long"].append(c)
                elif n >= 4000 and len(buckets["very_long"]) < 30:
                    buckets["very_long"].append(c)

    return buckets


def profile_mesh_turn(mesh_instance, content: str) -> Dict[str, float]:
    from mesh.guards import guard_ingest, guard_tags
    from mesh.memory import (
        _embed_with_tag,
        _parse_tags,
        _clamp_confidence,
        _utc_now_iso,
        _normalize_trust_tier,
        _is_same_known_space,
        _is_true_duplicate,
        _is_true_contradiction,
        namespace_write_lock,
        log_learn,
    )

    t0 = time.perf_counter()
    # 1. Guards
    tg0 = time.perf_counter()
    guarded = guard_ingest(content, local_only=False)
    clean_tags, tags_priv, _ = guard_tags([], local_only=guarded.local_only)
    clean_content = guarded.content.strip()
    tg1 = time.perf_counter()
    guard_ms = (tg1 - tg0) * 1000.0

    # 2. Embedding
    te0 = time.perf_counter()
    embedding, emb_model_tag = _embed_with_tag(clean_content)
    te1 = time.perf_counter()
    embed_ms = (te1 - te0) * 1000.0

    # 3. Lock & Dedupe
    tl0 = time.perf_counter()
    with namespace_write_lock(mesh_instance.namespace):
        tl1 = time.perf_counter()
        lock_ms = (tl1 - tl0) * 1000.0

        td0 = time.perf_counter()
        raw = mesh_instance.store.query(embedding, n=3, query_embedding_model=emb_model_tag)
        docs = raw.get("documents", [[]])[0]
        distances = raw.get("distances", [[]])[0]
        metadatas = raw.get("metadatas", [[]])[0]
        for i, doc in enumerate(docs):
            meta = metadatas[i] if metadatas and i < len(metadatas) else {}
            if _is_same_known_space(meta.get("embedding_model"), emb_model_tag):
                sim = round(1.0 - float(distances[i]), 4)
                _is_true_duplicate(clean_content, doc, sim)
        td1 = time.perf_counter()
        dedupe_ms = (td1 - td0) * 1000.0

        # 4. Contradiction check
        tc0 = time.perf_counter()
        for i, doc in enumerate(docs):
            meta = metadatas[i] if metadatas and i < len(metadatas) else {}
            if _is_same_known_space(meta.get("embedding_model"), emb_model_tag):
                sim = round(1.0 - float(distances[i]), 4)
                _is_true_contradiction(clean_content, doc, sim)
        tc1 = time.perf_counter()
        contra_ms = (tc1 - tc0) * 1000.0

        # 5. DB Write
        memory_id = str(uuid.uuid4())
        now = _utc_now_iso()
        tw0 = time.perf_counter()
        mesh_instance.store.upsert(
            memory_id=memory_id,
            content=clean_content,
            embedding=embedding,
            metadata={
                "memory_type": "fact",
                "confidence": 1.0,
                "source_agent": "profiler",
                "tags": clean_tags,
                "created_at": now,
                "updated_at": now,
                "accessed_at": now,
                "access_count": 0,
                "ttl_days": None,
                "embedding_model": emb_model_tag,
                "trust_tier": "agent",
                "quarantined": False,
                "secret_ref": None,
            },
            local_only=False,
            embedding_model=emb_model_tag,
        )
        tw1 = time.perf_counter()
        db_ms = (tw1 - tw0) * 1000.0

    # 6. Audit
    ta0 = time.perf_counter()
    log_learn(
        content=clean_content,
        namespace=mesh_instance.namespace,
        agent_id="profiler",
        result={"memory_id": memory_id, "status": "stored", "deduplicated": False},
        local_only=False,
    )
    ta1 = time.perf_counter()
    audit_ms = (ta1 - ta0) * 1000.0

    total_ms = (time.perf_counter() - t0) * 1000.0

    return {
        "guards_ms": guard_ms,
        "embedding_ms": embed_ms,
        "lock_ms": lock_ms,
        "dedupe_ms": dedupe_ms,
        "contradiction_ms": contra_ms,
        "db_write_ms": db_ms,
        "audit_ms": audit_ms,
        "total_ms": total_ms,
    }


def run_mem0_profile(turns: List[str]) -> List[float]:
    import subprocess
    worker_script = """
import sys, json, time, tempfile
from pathlib import Path
from mem0 import Memory

turns = json.loads(sys.stdin.read())
timings = []

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    base = Path(td)
    cfg = {
        "version": "v1.1",
        "vector_store": {
            "provider": "qdrant",
            "config": {"path": str(base / "qdrant"), "embedding_model_dims": 384}
        },
        "llm": {
            "provider": "openai",
            "config": {"api_key": "fake-local-no-cloud-calls", "model": "gpt-4o-mini"}
        },
        "embedder": {
            "provider": "fastembed",
            "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"}
        }
    }
    m = Memory.from_config(cfg)
    # warm up
    m.add("warm up turn", user_id="u0", infer=False)
    for t in turns:
        t0 = time.perf_counter()
        m.add(t, user_id="u0", infer=False)
        timings.append((time.perf_counter() - t0) * 1000.0)

print(json.dumps(timings))
"""
    python_mem0 = str(PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "python.exe")
    p = subprocess.run(
        [python_mem0, "-c", worker_script],
        input=json.dumps(turns),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if p.returncode != 0:
        print("Mem0 error:", p.stderr)
        return []
    return json.loads(p.stdout)


def main():
    print("=" * 80)
    print("  INGESTION STAGE PROFILER: MESH VS MEM0 (BARE-METAL)")
    print("=" * 80)

    buckets = load_turns_by_bucket()
    for b_name, turns in buckets.items():
        print(f"Bucket '{b_name}': {len(turns)} turns sampled")

    results: Dict[str, Any] = {}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        os.environ["MESH_HOME"] = str(Path(td) / "mesh_home")
        os.environ["MESH_NO_AUTO_DAEMON"] = "1"
        from mesh.memory import Mesh

        # Warm up Mesh
        m_warm = Mesh(namespace="warmup", agent_id="profiler")
        m_warm.learn("warm up text", memory_type="fact")

        for b_name, turns in buckets.items():
            print(f"\n--- Profiling Bucket: {b_name} (N={len(turns)}) ---")
            ns = f"prof-{b_name}"
            mesh = Mesh(namespace=ns, agent_id="profiler")

            mesh_timings: Dict[str, List[float]] = {
                "guards_ms": [],
                "embedding_ms": [],
                "lock_ms": [],
                "dedupe_ms": [],
                "contradiction_ms": [],
                "db_write_ms": [],
                "audit_ms": [],
                "total_ms": [],
            }

            for t in turns:
                res = profile_mesh_turn(mesh, t)
                for k, v in res.items():
                    mesh_timings[k].append(v)

            # Mem0 profiling on the exact same turns
            mem0_timings = run_mem0_profile(turns)

            stage_stats = {}
            for k, vals in mesh_timings.items():
                stage_stats[k] = {
                    "mean_ms": round(float(np.mean(vals)), 2),
                    "p50_ms": round(percentile(vals, 50), 2),
                    "p95_ms": round(percentile(vals, 95), 2),
                }

            mem0_stats = {
                "mean_ms": round(float(np.mean(mem0_timings)), 2) if mem0_timings else 0.0,
                "p50_ms": round(percentile(mem0_timings, 50), 2) if mem0_timings else 0.0,
                "p95_ms": round(percentile(mem0_timings, 95), 2) if mem0_timings else 0.0,
            }

            results[b_name] = {
                "turn_count": len(turns),
                "mesh_stages": stage_stats,
                "mem0_sdk": mem0_stats,
            }

            print(f"Mesh Total: mean={stage_stats['total_ms']['mean_ms']}ms, p50={stage_stats['total_ms']['p50_ms']}ms, p95={stage_stats['total_ms']['p95_ms']}ms")
            print(f"  Guards   : {stage_stats['guards_ms']['mean_ms']}ms")
            print(f"  Embedding: {stage_stats['embedding_ms']['mean_ms']}ms (p95: {stage_stats['embedding_ms']['p95_ms']}ms)")
            print(f"  Dedupe   : {stage_stats['dedupe_ms']['mean_ms']}ms")
            print(f"  Contra   : {stage_stats['contradiction_ms']['mean_ms']}ms")
            print(f"  DB Write : {stage_stats['db_write_ms']['mean_ms']}ms")
            print(f"  Audit    : {stage_stats['audit_ms']['mean_ms']}ms")
            print(f"Mem0 Total: mean={mem0_stats['mean_ms']}ms, p50={mem0_stats['p50_ms']}ms, p95={mem0_stats['p95_ms']}ms")

    out_path = PROJECT_ROOT / "benchmarks" / "results" / "ingestion_stages_profile.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved ingestion profiling results to: {out_path}")


if __name__ == "__main__":
    main()
