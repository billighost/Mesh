"""
benchmarks/run_mem0_worker.py - Dedicated Worker for Mem0 SDK in Python 3.12/3.14 Virtualenv
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
from typing import Any, Callable, Optional

try:
    import psutil
except ImportError:
    psutil = None

KNOWN_ITEM_ID = "[turn:99999]"
KNOWN_ITEM_CONTENT = (
    f"{KNOWN_ITEM_ID} The cerulean observatory celestial frequency atop Mount Horizon recorded an "
    "anomalous 432.85 MHz on stellar quadrant nine."
)
KNOWN_ITEM_QUERY = "cerulean observatory celestial frequency"
KNOWN_ITEM_ANSWER = "432.85 MHz"


def get_dir_size_mb(path: str | Path) -> float:
    total_bytes = 0
    p = Path(path)
    if not p.exists():
        return 0.0
    for entry in p.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total_bytes += entry.stat().st_size
        except (OSError, FileNotFoundError):
            pass
    return round(total_bytes / (1024 * 1024), 2)


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


def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = (len(s) - 1) * (p / 100.0)
    low = math.floor(idx)
    high = math.ceil(idx)
    if low == high:
        return s[int(idx)]
    return s[low] * (high - idx) + s[high] * (idx - low)


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


def bootstrap_percentile_ci(
    values: list[float],
    p: float,
    n_bootstrap: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    estimates = []
    for _ in range(n_bootstrap):
        sample = [rng.choice(values) for _ in range(n)]
        estimates.append(percentile(sample, p))
    estimates.sort()
    lower_idx = int((1.0 - ci) / 2.0 * n_bootstrap)
    upper_idx = int((1.0 + ci) / 2.0 * n_bootstrap) - 1
    lower = estimates[max(0, lower_idx)]
    upper = estimates[min(n_bootstrap - 1, upper_idx)]
    return (round(lower, 2), round(upper, 2))


def extract_candidate_session(
    cand_id: str,
    cand_content: str,
    tag_to_session: dict[str, str],
) -> Optional[str]:
    if cand_id in tag_to_session:
        return tag_to_session[cand_id]

    m = re.search(r"\[turn:(\d+)\]", cand_id)
    if not m:
        m = re.search(r"turn_(\d+)", cand_id)
    if m:
        t_tag = f"[turn:{int(m.group(1)):05d}]"
        if t_tag in tag_to_session:
            return tag_to_session[t_tag]

    m = re.search(r"\[turn:(\d+)\]", cand_content)
    if not m:
        m = re.search(r"turn_(\d+)", cand_content)
    if m:
        t_tag = f"[turn:{int(m.group(1)):05d}]"
        if t_tag in tag_to_session:
            return tag_to_session[t_tag]

    m = re.search(r"([a-f0-9]{8}_s\d+)", cand_id)
    if m:
        return m.group(1)
    m = re.search(r"([a-f0-9]{8}_s\d+)", cand_content)
    if m:
        return m.group(1)

    return None


def is_turn_level_hit(
    candidate_id: str,
    candidate_content: str,
    gold_turn_tags: set[str],
) -> bool:
    if candidate_id in gold_turn_tags:
        return True

    m_id = re.search(r"\[turn:(\d+)\]", candidate_id)
    if not m_id:
        m_id = re.search(r"turn_(\d+)", candidate_id)
    if m_id:
        tag = f"[turn:{int(m_id.group(1)):05d}]"
        if tag in gold_turn_tags:
            return True

    m_cnt = re.search(r"\[turn:(\d+)\]", candidate_content)
    if not m_cnt:
        m_cnt = re.search(r"turn_(\d+)", candidate_content)
    if m_cnt:
        tag = f"[turn:{int(m_cnt.group(1)):05d}]"
        if tag in gold_turn_tags:
            return True

    for gid in gold_turn_tags:
        if gid and gid in candidate_content:
            return True

    return False


def probe_searchable(probe_fn: Callable[[], bool], timeout: float = 60.0, interval: float = 0.05) -> float:
    t_start = time.perf_counter()
    while (time.perf_counter() - t_start) < timeout:
        try:
            if probe_fn():
                return time.perf_counter() - t_start
        except Exception:
            pass
        time.sleep(interval)
    return time.perf_counter() - t_start


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

    tag_to_session: dict[str, str] = {}
    for item in corpus:
        sess_id = f"{item['qid']}_s{item['s_idx']}"
        tag_to_session[item["tag"]] = sess_id
        tag_to_session[f"turn_{item['opaque_id']:05d}"] = sess_id
        tag_to_session[str(item["opaque_id"])] = sess_id

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
            def probe_mem0() -> bool:
                res = mem.search(KNOWN_ITEM_QUERY, filters={"user_id": "smoke_user"}, limit=1)
                results = res.get("results", [])
                return any(
                    KNOWN_ITEM_ANSWER.lower() in r.get("memory", "").lower()
                    or KNOWN_ITEM_ID in r.get("metadata", {}).get("turn_id", "")
                    for r in results
                )

            time_to_searchable = probe_searchable(probe_mem0, timeout=30.0, interval=0.05)

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
                res = mem.search(query.strip(), filters={"user_id": "smoke_user"}, limit=n)
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

            turn_h1_list: list[float] = []
            turn_h5_list: list[float] = []
            turn_h10_list: list[float] = []
            turn_rr_list: list[float] = []
            turn_ndcg_list: list[float] = []

            sess_h1_list: list[float] = []
            sess_h5_list: list[float] = []
            sess_h10_list: list[float] = []
            sess_rr_list: list[float] = []
            sess_ndcg_list: list[float] = []

            latencies_ms: list[float] = []
            raw_inspections: list[dict[str, Any]] = []

            for q_idx, q in enumerate(questions):
                qid = q["question_id"]
                gold_turn_tags = {
                    original_to_opaque[f"{qid}_s{s_idx}_t{t_idx}"]
                    for s_idx, session in enumerate(q.get("haystack_sessions", []))
                    for t_idx, turn in enumerate(session)
                    if turn.get("has_answer")
                }
                gold_sessions = {
                    f"{qid}_s{s_idx}"
                    for s_idx, session in enumerate(q.get("haystack_sessions", []))
                    if any(turn.get("has_answer") for turn in session)
                }
                gold_answer = q.get("answer", "")

                t_q = time.perf_counter()
                candidates = recall_fn(q["question"].strip(), 10)
                lat = (time.perf_counter() - t_q) * 1000.0
                latencies_ms.append(lat)

                # 1. Turn-level scoring
                first_turn_hit_rank: Optional[int] = None
                for r_idx, (c_id, c_text) in enumerate(candidates[:10], start=1):
                    if is_turn_level_hit(c_id, c_text, gold_turn_tags):
                        first_turn_hit_rank = r_idx
                        break
                h1_t = 1.0 if (first_turn_hit_rank is not None and first_turn_hit_rank <= 1) else 0.0
                h5_t = 1.0 if (first_turn_hit_rank is not None and first_turn_hit_rank <= 5) else 0.0
                h10_t = 1.0 if (first_turn_hit_rank is not None and first_turn_hit_rank <= 10) else 0.0
                rr_t = (1.0 / first_turn_hit_rank) if first_turn_hit_rank is not None else 0.0
                ndcg_t = (1.0 / math.log2(first_turn_hit_rank + 1)) if (first_turn_hit_rank is not None and first_turn_hit_rank <= 10) else 0.0
                turn_h1_list.append(h1_t)
                turn_h5_list.append(h5_t)
                turn_h10_list.append(h10_t)
                turn_rr_list.append(rr_t)
                turn_ndcg_list.append(ndcg_t)

                # 2. Session-level scoring
                seen_sessions: set[str] = set()
                deduped_sessions: list[str] = []
                for c_idx, (c_id, c_text) in enumerate(candidates[:10]):
                    sess = extract_candidate_session(c_id, c_text, tag_to_session)
                    sess_key = sess if sess is not None else f"_unknown_{c_idx}"
                    if sess_key not in seen_sessions:
                        seen_sessions.add(sess_key)
                        deduped_sessions.append(sess_key)

                first_session_hit_rank: Optional[int] = None
                for s_rank, sess in enumerate(deduped_sessions, start=1):
                    if sess in gold_sessions:
                        first_session_hit_rank = s_rank
                        break

                h1_s = 1.0 if (first_session_hit_rank is not None and first_session_hit_rank <= 1) else 0.0
                h5_s = 1.0 if (first_session_hit_rank is not None and first_session_hit_rank <= 5) else 0.0
                h10_s = 1.0 if (first_session_hit_rank is not None and first_session_hit_rank <= 10) else 0.0
                rr_s = (1.0 / first_session_hit_rank) if first_session_hit_rank is not None else 0.0
                ndcg_s = (1.0 / math.log2(first_session_hit_rank + 1)) if (first_session_hit_rank is not None and first_session_hit_rank <= 10) else 0.0
                sess_h1_list.append(h1_s)
                sess_h5_list.append(h5_s)
                sess_h10_list.append(h10_s)
                sess_rr_list.append(rr_s)
                sess_ndcg_list.append(ndcg_s)

                if q_idx < 5:
                    cand_inspections = []
                    for r_idx, (c_id, c_text) in enumerate(candidates[:5], start=1):
                        sess = extract_candidate_session(c_id, c_text, tag_to_session)
                        t_hit = is_turn_level_hit(c_id, c_text, gold_turn_tags)
                        s_hit = (sess in gold_sessions) if sess is not None else False
                        cand_inspections.append({
                            "rank": r_idx,
                            "id": c_id,
                            "session_id": sess,
                            "snippet": c_text[:140],
                            "turn_hit": t_hit,
                            "session_hit": s_hit,
                        })
                    raw_inspections.append({
                        "question_idx": q_idx + 1,
                        "question_id": qid,
                        "question_text": q["question"],
                        "gold_answer": gold_answer,
                        "gold_turn_tags": sorted(list(gold_turn_tags)),
                        "gold_sessions": sorted(list(gold_sessions)),
                        "first_turn_hit_rank": first_turn_hit_rank,
                        "first_session_hit_rank": first_session_hit_rank,
                        "top_5_results": cand_inspections,
                    })

            total_q = len(questions)

            turn_metrics = {
                "status": "evaluated",
                "recall_at_1": round(sum(turn_h1_list) / total_q, 4),
                "recall_at_1_ci": list(bootstrap_ci(turn_h1_list)),
                "recall_at_5": round(sum(turn_h5_list) / total_q, 4),
                "recall_at_5_ci": list(bootstrap_ci(turn_h5_list)),
                "recall_at_10": round(sum(turn_h10_list) / total_q, 4),
                "recall_at_10_ci": list(bootstrap_ci(turn_h10_list)),
                "mrr": round(statistics.mean(turn_rr_list), 4),
                "mrr_ci": list(bootstrap_ci(turn_rr_list)),
                "ndcg_at_10": round(statistics.mean(turn_ndcg_list), 4),
                "ndcg_at_10_ci": list(bootstrap_ci(turn_ndcg_list)),
            }

            sess_metrics = {
                "status": "evaluated",
                "recall_at_1": round(sum(sess_h1_list) / total_q, 4),
                "recall_at_1_ci": list(bootstrap_ci(sess_h1_list)),
                "recall_at_5": round(sum(sess_h5_list) / total_q, 4),
                "recall_at_5_ci": list(bootstrap_ci(sess_h5_list)),
                "recall_at_10": round(sum(sess_h10_list) / total_q, 4),
                "recall_at_10_ci": list(bootstrap_ci(sess_h10_list)),
                "mrr": round(statistics.mean(sess_rr_list), 4),
                "mrr_ci": list(bootstrap_ci(sess_rr_list)),
                "ndcg_at_10": round(statistics.mean(sess_ndcg_list), 4),
                "ndcg_at_10_ci": list(bootstrap_ci(sess_ndcg_list)),
            }

            p50_lat = percentile(latencies_ms, 50)
            p95_lat = percentile(latencies_ms, 95)
            p99_lat = percentile(latencies_ms, 99)
            latency_dict = {
                "p50_ms": round(p50_lat, 2),
                "p50_ms_ci": list(bootstrap_percentile_ci(latencies_ms, 50)),
                "p95_ms": round(p95_lat, 2),
                "p95_ms_ci": list(bootstrap_percentile_ci(latencies_ms, 95)),
                "p99_ms": round(p99_lat, 2),
                "p99_ms_ci": list(bootstrap_percentile_ci(latencies_ms, 99)),
                "mean_ms": round(statistics.mean(latencies_ms), 2),
                "latencies_ms": [round(l, 2) for l in latencies_ms],
            }

            disk_footprint = get_dir_size_mb(temp_dir)

            result = {
                "tool": "Mem0 (SDK)",
                "class": "Class A",
                "storage_unit": "turn",
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
                "turn_level_metrics": turn_metrics,
                "session_level_metrics": sess_metrics,
                "accuracy_metrics": {
                    "turn_level": turn_metrics,
                    "session_level": sess_metrics,
                    "recall_at_1": turn_metrics["recall_at_1"],
                    "recall_at_1_ci": turn_metrics["recall_at_1_ci"],
                    "recall_at_5": turn_metrics["recall_at_5"],
                    "recall_at_5_ci": turn_metrics["recall_at_5_ci"],
                    "recall_at_10": turn_metrics["recall_at_10"],
                    "recall_at_10_ci": turn_metrics["recall_at_10_ci"],
                    "mrr": turn_metrics["mrr"],
                    "mrr_ci": turn_metrics["mrr_ci"],
                    "ndcg_at_10": turn_metrics["ndcg_at_10"],
                    "ndcg_at_10_ci": turn_metrics["ndcg_at_10_ci"],
                },
                "latency_metrics": latency_dict,
                "latencies_ms": latency_dict["latencies_ms"],
                "peak_ram_mb": tracker.peak_mb,
                "disk_footprint_mb": disk_footprint,
                "raw_inspections_first_5": raw_inspections,
            }

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
            gc.collect()


if __name__ == "__main__":
    main()
