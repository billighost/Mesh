#!/usr/bin/env python3
"""
Retrieval Benchmark for Mesh.
Measures Recall@1, Recall@3, Recall@5, MRR, and Rank-1 Hard Negative False Positive Rate.
"""

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mesh.memory import Mesh, _embed_with_tag
from mesh.store import MeshStore, ensure_secure_mesh_dir


def run_benchmark(save_baseline: bool = False) -> dict[str, Any]:
    bench_dir = Path(__file__).resolve().parent
    corpus_path = bench_dir / "corpus.json"
    queries_path = bench_dir / "queries.json"

    with open(corpus_path, "r", encoding="utf-8") as f:
        corpus = json.load(f)

    with open(queries_path, "r", encoding="utf-8") as f:
        query_data = json.load(f)

    normal_queries = query_data["normal_queries"]
    hard_negatives = query_data["hard_negatives"]

    # Use an isolated namespace in a temporary directory
    temp_home = tempfile.mkdtemp(prefix="mesh_bench_")
    old_mesh_home = os.environ.get("MESH_HOME")
    os.environ["MESH_HOME"] = temp_home

    try:
        ns = f"bench_{uuid.uuid4().hex[:8]}"
        mesh = Mesh(namespace=ns, agent_id="benchmark")

        # Ingest corpus
        for item in corpus:
            cid = item["id"]
            content = item["content"]
            # To ensure exact custom IDs, we insert into store
            emb, tag = _embed_with_tag(content)
            mesh.store.upsert(
                memory_id=cid,
                content=content,
                embedding=emb,
                metadata={"memory_type": "fact", "confidence": 1.0, "source_agent": "benchmark"},
                local_only=False,
                embedding_model=tag,
            )

        # Evaluate normal queries
        r1_hits = 0
        r3_hits = 0
        r5_hits = 0
        rr_sum = 0.0

        for q in normal_queries:
            query_text = q["query"]
            expected = set(q["expected"])
            results = mesh.recall(query_text, n=5, record_telemetry=False)
            result_ids = [r["id"] for r in results]

            # Recall@1
            if result_ids and result_ids[0] in expected:
                r1_hits += 1

            # Recall@3
            if any(rid in expected for rid in result_ids[:3]):
                r3_hits += 1

            # Recall@5
            if any(rid in expected for rid in result_ids[:5]):
                r5_hits += 1

            # Reciprocal Rank
            rr = 0.0
            for rank_idx, rid in enumerate(result_ids, start=1):
                if rid in expected:
                    rr = 1.0 / rank_idx
                    break
            rr_sum += rr

        total_norm = len(normal_queries)
        recall_at_1 = round(r1_hits / total_norm, 4) if total_norm else 0.0
        recall_at_3 = round(r3_hits / total_norm, 4) if total_norm else 0.0
        recall_at_5 = round(r5_hits / total_norm, 4) if total_norm else 0.0
        mrr = round(rr_sum / total_norm, 4) if total_norm else 0.0

        # Evaluate hard negatives
        hn_fp_rank1 = 0
        for hn in hard_negatives:
            q_text = hn["query"]
            hn_targets = set(hn["hard_negative_targets"])
            results = mesh.recall(q_text, n=5, record_telemetry=False)
            if results and results[0]["id"] in hn_targets:
                hn_fp_rank1 += 1

        total_hn = len(hard_negatives)
        hn_fp_rate = round(hn_fp_rank1 / total_hn, 4) if total_hn else 0.0

        metrics = {
            "total_corpus_size": len(corpus),
            "total_normal_queries": total_norm,
            "total_hard_negative_queries": total_hn,
            "recall_at_1": recall_at_1,
            "recall_at_3": recall_at_3,
            "recall_at_5": recall_at_5,
            "mrr": mrr,
            "hard_negative_fp_rate_rank1": hn_fp_rate,
            "hard_negative_fp_count_rank1": hn_fp_rank1,
        }

        print("\n" + "=" * 50)
        print("MESH RETRIEVAL BENCHMARK RESULTS")
        print("=" * 50)
        print(f"Corpus size:                 {metrics['total_corpus_size']}")
        print(f"Normal queries:              {metrics['total_normal_queries']}")
        print(f"Recall@1:                    {metrics['recall_at_1']:.4f}")
        print(f"Recall@3:                    {metrics['recall_at_3']:.4f}")
        print(f"Recall@5:                    {metrics['recall_at_5']:.4f}")
        print(f"MRR:                         {metrics['mrr']:.4f}")
        print(f"Hard-negative queries:       {metrics['total_hard_negative_queries']}")
        print(f"Hard-negative FP @ rank 1:   {metrics['hard_negative_fp_rate_rank1']:.4f} ({hn_fp_rank1}/{total_hn})")
        print("=" * 50 + "\n")

        if save_baseline:
            baseline_path = bench_dir / "baseline.json"
            with open(baseline_path, "w", encoding="utf-8") as f:
                json.dump(metrics, f, indent=2)
            print(f"Saved baseline metrics to {baseline_path}")

        return metrics

    finally:
        if old_mesh_home is not None:
            os.environ["MESH_HOME"] = old_mesh_home
        else:
            os.environ.pop("MESH_HOME", None)


if __name__ == "__main__":
    save = "--save-baseline" in sys.argv
    run_benchmark(save_baseline=save)
