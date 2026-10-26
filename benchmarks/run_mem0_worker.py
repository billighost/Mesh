"""
benchmarks/run_mem0_worker.py - Dedicated Worker for Mem0 SDK in Python 3.12 Virtualenv
"""

from __future__ import annotations

import gc
import json
import math
import os
import random
import re
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

try:
    import psutil
except ImportError:
    psutil = None

KNOWN_ITEM_ID = "[turn:99999]"
KNOWN_ITEM_CONTENT = (
    f"{KNOWN_ITEM_ID} The cerulean observatory atop Mount Horizon recorded an "
    "anomalous celestial frequency of 432.85 MHz on stellar quadrant nine."
)
KNOWN_ITEM_QUERY = "cerulean observatory celestial frequency"
KNOWN_ITEM_ANSWER = "432.85 MHz"


def get_tree_rss_mb() -> float:
    if not psutil:
        return 0.0
    try:
        parent = psutil.Process()
        total_rss = parent.memory_info().rss
        for child in parent.children(recursive=True):
            try:
                total_rss += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return round(total_rss / (1024 * 1024), 2)
    except Exception:
        return 0.0


class ProcessTreePeakTracker:
    def __init__(self, interval: float = 0.05):
        self.interval = interval
        self.peak_mb = 0.0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _monitor(self):
        while not self._stop_event.is_set():
            rss = get_tree_rss_mb()
            if rss > self.peak_mb:
                self.peak_mb = rss
            time.sleep(self.interval)

    def __enter__(self):
        self.peak_mb = get_tree_rss_mb()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        final_rss = get_tree_rss_mb()
        if final_rss > self.peak_mb:
            self.peak_mb = final_rss


def token_jaccard(s1: str, s2: str) -> float:
    t1 = set(re.findall(r"\w+", s1.lower()))
    t2 = set(re.findall(r"\w+", s2.lower()))
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / len(t1 | t2)


def is_retrieval_hit(
    candidate_id: str,
    candidate_content: str,
    gold_ids: set[str],
    gold_contents: list[str],
    gold_answer: str,
) -> bool:
    if candidate_id:
        for gid in gold_ids:
            if gid in candidate_id or candidate_id in gid:
                return True
    for gid in gold_ids:
        if gid and gid in candidate_content:
            return True
    if gold_answer and len(gold_answer) >= 4:
        if gold_answer.lower() in candidate_content.lower():
            return True
    for g_turn in gold_contents:
        if token_jaccard(candidate_content, g_turn) >= 0.70:
            return True
    return False


def bootstrap_ci(
    values: list[float],
    n_bootstrap: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(n_bootstrap):
        sample = [rng.choice(values) for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lower_idx = int((1.0 - ci) / 2.0 * n_bootstrap)
    upper_idx = int((1.0 + ci) / 2.0 * n_bootstrap) - 1
    lower = means[max(0, lower_idx)]
    upper = means[min(n_bootstrap - 1, upper_idx)]
    return (round(lower, 4), round(upper, 4))


def main():
    if len(sys.argv) < 5:
        print("Usage: run_mem0_worker.py <corpus_json> <questions_json> <orig_to_opaque_json> <out_json>", file=sys.stderr)
        sys.exit(1)

    corpus_path = sys.argv[1]
    questions_path = sys.argv[2]
    orig_to_opaque_path = sys.argv[3]
    out_path = sys.argv[4]

    with open(corpus_path, "r", encoding="utf-8") as f:
        corpus = json.load(f)
    with open(questions_path, "r", encoding="utf-8") as f:
        questions = json.load(f)
    with open(orig_to_opaque_path, "r", encoding="utf-8") as f:
        original_to_opaque = json.load(f)

    from mem0 import Memory

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="mem0_worker_") as temp_dir:
        os.environ["MEM0_TELEMETRY"] = "false"
        config = {
            "version": "v1.1",
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "path": os.path.join(temp_dir, "qdrant"),
                    "embedding_model_dims": 384,
                },
            },
            "llm": {
                "provider": "openai",
                "config": {"api_key": "fake-local-no-cloud-calls", "model": "gpt-4o-mini"},
            },
            "embedder": {
                "provider": "fastembed",
                "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"},
            },
        }

        with ProcessTreePeakTracker() as tracker:
            mem = Memory.from_config(config)

            # Ingest turns
            t_write_start = time.perf_counter()
            for item in corpus:
                mem.add(item["content"], user_id="smoke_user", metadata={"turn_id": item["tag"]}, infer=False)
            mem.add(KNOWN_ITEM_CONTENT, user_id="smoke_user", metadata={"turn_id": KNOWN_ITEM_ID}, infer=False)
            write_sec = time.perf_counter() - t_write_start

            # Time to searchable
            t_poll_start = time.perf_counter()
            _ = mem.search(KNOWN_ITEM_QUERY, filters={"user_id": "smoke_user"}, limit=1)
            time_to_searchable = time.perf_counter() - t_poll_start

            # Count verification
            expected_total = len(corpus) + 1
            stored_count = 0
            try:
                col_info = mem.vector_store.client.get_collection("mem0")
                stored_count = col_info.points_count
            except Exception:
                all_mem = mem.get_all(user_id="smoke_user")
                stored_count = len(all_mem.get("results", []))

            audit_notes = {
                "stored_points_count": stored_count,
                "notes": "Mem0 Qdrant vector store point count verified.",
            }

            def recall_fn(query: str, n: int = 10):
                res = mem.search(query, filters={"user_id": "smoke_user"}, limit=n)
                cands = []
                for item in res.get("results", []):
                    tid = item.get("metadata", {}).get("turn_id", "")
                    cands.append((tid, item.get("memory", "")))
                return cands

            # Pre-flight gate check
            print("  --> Pre-flight Known-Item Gate for Mem0 (SDK) (Class A)...", flush=True)
            ki_candidates = recall_fn(KNOWN_ITEM_QUERY, 5)
            ki_hit = False
            ki_rank = None
            for r_idx, (c_id, c_text) in enumerate(ki_candidates[:5], start=1):
                if KNOWN_ITEM_ID in c_id or KNOWN_ITEM_ID in c_text or KNOWN_ITEM_ANSWER.lower() in c_text.lower():
                    ki_hit = True
                    ki_rank = r_idx
                    break

            if not ki_hit or ki_rank != 1:
                err_msg = (
                    f"[FATAL GATE ERROR] Known-item test failed for Mem0 (SDK)!\n"
                    f"Expected rank 1, got rank={ki_rank}, hit={ki_hit}.\n"
                    f"Candidates: {ki_candidates[:3]}"
                )
                print(err_msg, file=sys.stderr, flush=True)
                sys.exit(2)

            print("  [GATE PASSED] Known-item found at rank 1 in Mem0 (SDK).", flush=True)

            # Evaluate 50 questions
            hits_1_list = []
            hits_5_list = []
            hits_10_list = []
            rr_list = []
            ndcg_list = []
            latencies_ms = []
            raw_inspections = []

            for q_idx, q in enumerate(questions):
                qid = q["question_id"]
                gold_tags = {
                    original_to_opaque[f"{qid}_s{s_idx}_t{t_idx}"]
                    for s_idx, session in enumerate(q.get("haystack_sessions", []))
                    for t_idx, turn in enumerate(session)
                    if turn.get("has_answer")
                }
                gold_contents = [
                    turn["content"]
                    for session in q.get("haystack_sessions", [])
                    for turn in session
                    if turn.get("has_answer")
                ]
                gold_answer = q.get("answer", "")

                t_q = time.perf_counter()
                candidates = recall_fn(q["question"], 10)
                lat = (time.perf_counter() - t_q) * 1000.0
                latencies_ms.append(lat)

                first_hit_rank = None
                cand_inspections = []
                for rank_idx, (cand_id, cand_content) in enumerate(candidates[:10], start=1):
                    hit = is_retrieval_hit(cand_id, cand_content, gold_tags, gold_contents, gold_answer)
                    if rank_idx <= 5 and q_idx < 5:
                        cand_inspections.append({
                            "rank": rank_idx,
                            "id": cand_id,
                            "snippet": cand_content[:140],
                            "is_hit": hit,
                        })
                    if hit and first_hit_rank is None:
                        first_hit_rank = rank_idx

                h1 = 1.0 if (first_hit_rank is not None and first_hit_rank <= 1) else 0.0
                h5 = 1.0 if (first_hit_rank is not None and first_hit_rank <= 5) else 0.0
                h10 = 1.0 if (first_hit_rank is not None and first_hit_rank <= 10) else 0.0
                rr = (1.0 / first_hit_rank) if first_hit_rank is not None else 0.0
                ndcg = (1.0 / math.log2(first_hit_rank + 1)) if first_hit_rank is not None else 0.0

                hits_1_list.append(h1)
                hits_5_list.append(h5)
                hits_10_list.append(h10)
                rr_list.append(rr)
                ndcg_list.append(ndcg)

                if q_idx < 5:
                    raw_inspections.append({
                        "question_idx": q_idx + 1,
                        "question_id": qid,
                        "question_text": q["question"],
                        "gold_answer": gold_answer,
                        "gold_turn_tags": list(gold_tags),
                        "first_hit_rank": first_hit_rank,
                        "top_5_results": cand_inspections,
                    })

            total_q = len(questions)
            latencies_sorted = sorted(latencies_ms)
            p50_lat = statistics.median(latencies_ms)
            p95_lat = latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies_sorted else 0.0

            result = {
                "tool": "Mem0 (SDK)",
                "class": "Class A",
                "count_verification": {
                    "expected_turns": expected_total,
                    "stored_count": stored_count,
                    "shortfall": expected_total - stored_count,
                    "verified_equal": (expected_total == stored_count),
                    "audit_notes": audit_notes,
                },
                "ingestion_timing": {
                    "write_seconds": round(write_sec, 3),
                    "time_to_searchable_seconds": round(time_to_searchable, 3),
                    "total_ingest_seconds": round(write_sec + time_to_searchable, 3),
                    "items_per_second": round(expected_total / max(0.001, write_sec), 1),
                },
                "known_item_preflight": {
                    "passed": ki_hit,
                    "rank": ki_rank,
                    "query": KNOWN_ITEM_QUERY,
                },
                "accuracy_metrics": {
                    "recall_at_1": round(sum(hits_1_list) / total_q, 4),
                    "recall_at_1_ci": list(bootstrap_ci(hits_1_list)),
                    "recall_at_5": round(sum(hits_5_list) / total_q, 4),
                    "recall_at_5_ci": list(bootstrap_ci(hits_5_list)),
                    "recall_at_10": round(sum(hits_10_list) / total_q, 4),
                    "recall_at_10_ci": list(bootstrap_ci(hits_10_list)),
                    "mrr": round(statistics.mean(rr_list), 4),
                    "mrr_ci": list(bootstrap_ci(rr_list)),
                    "ndcg_at_10": round(statistics.mean(ndcg_list), 4),
                    "ndcg_at_10_ci": list(bootstrap_ci(ndcg_list)),
                },
                "latency_metrics": {
                    "p50_ms": round(p50_lat, 2),
                    "p95_ms": round(p95_lat, 2),
                    "mean_ms": round(statistics.mean(latencies_ms), 2),
                },
                "peak_ram_mb": tracker.peak_mb,
                "raw_inspections_first_5": raw_inspections,
            }

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            gc.collect()


if __name__ == "__main__":
    main()
