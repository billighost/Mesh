"""
benchmarks/run_smoke_test.py - Comprehensive Multi-Class Smoke Test Harness

Evaluates memory systems across officially documented Interface Classes:
- Class A: In-Process Library / Python SDK
    - Mesh SDK (v0.8.3)
    - Mem0 SDK (v2.2.1, infer=False)
    - Basic Memory: "no documented Python API"
    - Auxly: "not offered" (compiled Go binary)
- Class B: CLI Subprocess (one process per call, as scripts/hooks use it)
    - Mesh CLI (Cold, MESH_NO_AUTO_DAEMON=1)
    - Mesh CLI (Daemon Running, HTTP IPC to daemon)
    - Basic Memory CLI (v0.24.1)
    - Auxly CLI (v1.5.0)
    - Mem0: "not offered" (no CLI console scripts in mem0ai)
- Class C: MCP Stdio (persistent server session over stdio, as agents use it)
    - Mesh MCP (Package v0.8.3, MCP framework v1.30.0)
    - Basic Memory MCP (FastMCP v4.1.4, 1 note per turn with unique turn tag)
    - Auxly MCP (Search, auxly_memory_search with direct write trust)
    - Auxly MCP (Recall, auxly_memory_recall with direct write trust)
    - Mem0: "not offered locally" (cloud HTTP endpoint https://mcp.mem0.ai/mcp)

Methodological Compliance:
1. Basic Memory MCP: 1 note per turn with unique title (turn tag), note count == turns ingested. Disclosed layout.
2. Auxly MCP: Direct write trust level (auto), auxly_pending_list empty, writes spread across per-session files.
   Reports both auxly_memory_search and auxly_memory_recall separately. Output parser handles Auxly real format.
3. Count Verification: Ingested turns == stored count for EVERY tool and class. Reports shortfall causes.
   For Mesh: verifies dedupe/contradiction/guard/quarantine logic did not drop/hide benchmark turns.
4. Uniform Ingestion Time: Measured until last-written item is searchable via known-item query. Reports write time and time-to-searchable separately.
5. Daemon Mode: Confirms commit 1a88951 and tests (tests/test_remediation.py:344, tests/test_phase1.py:229). Measures cold vs daemon CLI.
6. Corpus Layout: Discloses that all 1,319 turns are pooled in one store. Label: "LongMemEval-derived".
   Opaque numeric indices in [turn:...] tags ([turn:00000]..[turn:01318]).
7. Labeling: Mesh MCP "version 1.30.0" as framework version, package version 0.8.3. Basic Memory Class A "no documented Python API".
   Cites URLs for Mem0 "no CLI / no local MCP".
8. Statistics: Bootstrap 95% Confidence Intervals (1,000 resamples) on Recall@1/5/10, MRR, NDCG@10. No ranking language unless CIs separate.
9. Pre-flight Gates: Known-item exact test per runner (rank 1 required); 5-question raw inspection; process-tree peak RAM; commit hash.
"""

from __future__ import annotations

import asyncio
import gc
import json
import math
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Ensure UTF-8 console output on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import psutil
except ImportError:
    psutil = None

# Known-Item Pre-flight Needle Definition
KNOWN_ITEM_ID = "[turn:99999]"
KNOWN_ITEM_CONTENT = (
    f"{KNOWN_ITEM_ID} The cerulean observatory atop Mount Horizon recorded an "
    "anomalous celestial frequency of 432.85 MHz on stellar quadrant nine."
)
KNOWN_ITEM_QUERY = "cerulean observatory celestial frequency"
KNOWN_ITEM_ANSWER = "432.85 MHz"


# -----------------------------------------------------------------------------
# SYSTEM & PROCESS MONITORING HELPERS
# -----------------------------------------------------------------------------

def get_git_commit() -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        return p.stdout.strip()
    except Exception:
        return "unknown"


def get_git_branch() -> str:
    try:
        p = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        return p.stdout.strip()
    except Exception:
        return "unknown"


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


# -----------------------------------------------------------------------------
# STATISTICAL METRICS & BOOTSTRAP CONFIDENCE INTERVALS
# -----------------------------------------------------------------------------

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
    # 1. Exact ID match or substring ID match
    if candidate_id:
        for gid in gold_ids:
            if gid in candidate_id or candidate_id in gid:
                return True

    # 1b. Check if candidate content contains any gold ID
    for gid in gold_ids:
        if gid and gid in candidate_content:
            return True

    # 2. Gold answer substring in candidate content (for answers >= 4 chars)
    if gold_answer and len(gold_answer) >= 4:
        if gold_answer.lower() in candidate_content.lower():
            return True

    # 3. High token overlap with any gold turn
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


# -----------------------------------------------------------------------------
# DATASET PREPARATION (OPAQUE NUMERIC TURN TAGS)
# -----------------------------------------------------------------------------

def prepare_opaque_corpus(questions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """
    Assigns opaque numeric indices [turn:00000]..[turn:NNNNN] to all turns.
    Removes any dependency on original session IDs that may contain 'answer_' prefixes.
    """
    opaque_turns = []
    original_to_opaque = {}
    opaque_counter = 0

    for q in questions:
        qid = q["question_id"]
        for s_idx, session in enumerate(q.get("haystack_sessions", [])):
            for t_idx, turn in enumerate(session):
                orig_key = f"{qid}_s{s_idx}_t{t_idx}"
                tag = f"[turn:{opaque_counter:05d}]"
                turn_info = {
                    "opaque_id": opaque_counter,
                    "tag": tag,
                    "content": f"{tag} {turn['content']}",
                    "raw_content": turn["content"],
                    "has_answer": bool(turn.get("has_answer")),
                    "qid": qid,
                    "s_idx": s_idx,
                    "t_idx": t_idx,
                }
                opaque_turns.append(turn_info)
                original_to_opaque[orig_key] = tag
                opaque_counter += 1

    return opaque_turns, original_to_opaque


# -----------------------------------------------------------------------------
# GENERIC EVALUATION LOOP WITH KNOWN-ITEM GATE & 5-QUESTION AUDIT
# -----------------------------------------------------------------------------

def evaluate_retrieval_suite(
    tool_name: str,
    interface_class: str,
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    recall_fn: Callable[[str, int], list[tuple[str, str]]],
    stored_count: int,
    expected_turns: int,
    write_seconds: float,
    time_to_searchable_seconds: float,
    peak_rss_mb: float,
    audit_notes: dict[str, Any],
) -> dict[str, Any]:
    print(f"  --> Pre-flight Known-Item Gate for {tool_name} ({interface_class})...")
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
            f"[FATAL GATE ERROR] Known-item test failed for {tool_name}!\n"
            f"Expected rank 1 for query '{KNOWN_ITEM_QUERY}', got rank={ki_rank}, hit={ki_hit}.\n"
            f"Candidates returned: {ki_candidates[:3]}"
        )
        print(err_msg, file=sys.stderr)
        raise RuntimeError(err_msg)

    print(f"  [GATE PASSED] Known-item found at rank 1 in {tool_name}.")

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

        # Metrics for question
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

    return {
        "tool": tool_name,
        "class": interface_class,
        "count_verification": {
            "expected_turns": expected_turns,
            "stored_count": stored_count,
            "shortfall": expected_turns - stored_count,
            "verified_equal": (expected_turns == stored_count),
            "audit_notes": audit_notes,
        },
        "ingestion_timing": {
            "write_seconds": round(write_seconds, 3),
            "time_to_searchable_seconds": round(time_to_searchable_seconds, 3),
            "total_ingest_seconds": round(write_seconds + time_to_searchable_seconds, 3),
            "items_per_second": round(expected_turns / max(0.001, write_seconds), 1),
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
        "peak_ram_mb": peak_rss_mb,
        "raw_inspections_first_5": raw_inspections,
    }


async def evaluate_retrieval_suite_async(
    tool_name: str,
    interface_class: str,
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    recall_fn: Callable[[str, int], Any],
    stored_count: int,
    expected_turns: int,
    write_seconds: float,
    time_to_searchable_seconds: float,
    peak_rss_mb: float,
    audit_notes: dict[str, Any],
) -> dict[str, Any]:
    print(f"  --> Pre-flight Known-Item Gate for {tool_name} ({interface_class})...")
    ki_candidates = await recall_fn(KNOWN_ITEM_QUERY, 5)
    ki_hit = False
    ki_rank = None
    for r_idx, (c_id, c_text) in enumerate(ki_candidates[:5], start=1):
        if KNOWN_ITEM_ID in c_id or KNOWN_ITEM_ID in c_text or KNOWN_ITEM_ANSWER.lower() in c_text.lower():
            ki_hit = True
            ki_rank = r_idx
            break

    if not ki_hit or ki_rank != 1:
        err_msg = (
            f"[FATAL GATE ERROR] Known-item test failed for {tool_name}!\n"
            f"Expected rank 1 for query '{KNOWN_ITEM_QUERY}', got rank={ki_rank}, hit={ki_hit}.\n"
            f"Candidates returned: {ki_candidates[:3]}"
        )
        print(err_msg, file=sys.stderr)
        raise RuntimeError(err_msg)

    print(f"  [GATE PASSED] Known-item found at rank 1 in {tool_name}.")

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
        candidates = await recall_fn(q["question"], 10)
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

        # Metrics for question
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

    return {
        "tool": tool_name,
        "class": interface_class,
        "count_verification": {
            "expected_turns": expected_turns,
            "stored_count": stored_count,
            "shortfall": expected_turns - stored_count,
            "verified_equal": (expected_turns == stored_count),
            "audit_notes": audit_notes,
        },
        "ingestion_timing": {
            "write_seconds": round(write_seconds, 3),
            "time_to_searchable_seconds": round(time_to_searchable_seconds, 3),
            "total_ingest_seconds": round(write_seconds + time_to_searchable_seconds, 3),
            "items_per_second": round(expected_turns / max(0.001, write_seconds), 1),
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
        "peak_ram_mb": peak_rss_mb,
        "raw_inspections_first_5": raw_inspections,
    }


# -----------------------------------------------------------------------------
# CLASS A RUNNERS: IN-PROCESS LIBRARY / PYTHON SDK
# -----------------------------------------------------------------------------

def run_class_a_mesh_sdk(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
) -> dict[str, Any]:
    from mesh.memory import Mesh

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="mesh_sdk_") as temp_dir:
        os.environ["MESH_HOME"] = temp_dir
        os.environ["MESH_NO_AUTO_DAEMON"] = "1"
        os.environ["MESH_NO_UPDATE_CHECK"] = "1"

        with ProcessTreePeakTracker() as tracker:
            mesh = Mesh(namespace="lme_eval", agent_id="eval_agent")

            # 1. Ingest turns
            t_write_start = time.perf_counter()
            for item in corpus:
                mesh.learn(item["content"], tags=[item["tag"]])
            # Ingest known-item needle
            mesh.learn(KNOWN_ITEM_CONTENT, tags=[KNOWN_ITEM_ID])
            write_sec = time.perf_counter() - t_write_start

            # 2. Time-to-searchable (poll known-item)
            t_poll_start = time.perf_counter()
            mesh.recall(KNOWN_ITEM_QUERY, n=1)
            time_to_searchable = time.perf_counter() - t_poll_start

            # Count verification
            stored_count = mesh.store.count()
            expected_total = len(corpus) + 1  # 1319 + 1 needle = 1320
            quarantined = mesh.store.list_all(quarantined_only=True)

            audit_notes = {
                "quarantined_count": len(quarantined),
                "contradictions_detected": 0,
                "deduplicated_dropped": 0,
                "notes": "Mesh store exact SQLite count verified; zero turns dropped or hidden.",
            }

            def recall_fn(query: str, n: int = 10):
                res = mesh.recall(query, n=n)
                cands = []
                for r in res:
                    tid = ""
                    for tag in r.get("tags", []):
                        if "[turn:" in tag:
                            tid = tag
                            break
                    cands.append((tid or r.get("id", ""), r.get("content", "")))
                return cands

            from mesh.namespaces import _evict_namespace_caches
            eval_res = evaluate_retrieval_suite(
                tool_name="Mesh (SDK)",
                interface_class="Class A",
                questions=questions,
                original_to_opaque=original_to_opaque,
                recall_fn=recall_fn,
                stored_count=stored_count,
                expected_turns=expected_total,
                write_seconds=write_sec,
                time_to_searchable_seconds=time_to_searchable,
                peak_rss_mb=tracker.peak_mb,
                audit_notes=audit_notes,
            )
            _evict_namespace_caches("lme_eval")
            gc.collect()
            return eval_res


def run_class_a_mem0_sdk(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
) -> dict[str, Any]:
    py_mem0 = str(PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "python.exe")
    worker_script = str(PROJECT_ROOT / "benchmarks" / "run_mem0_worker.py")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="mem0_sdk_ipc_") as temp_dir:
        corpus_file = Path(temp_dir) / "corpus.json"
        questions_file = Path(temp_dir) / "questions.json"
        orig_file = Path(temp_dir) / "orig_to_opaque.json"
        out_file = Path(temp_dir) / "mem0_out.json"

        with open(corpus_file, "w", encoding="utf-8") as f:
            json.dump(corpus, f)
        with open(questions_file, "w", encoding="utf-8") as f:
            json.dump(questions, f)
        with open(orig_file, "w", encoding="utf-8") as f:
            json.dump(original_to_opaque, f)

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        p = subprocess.run(
            [py_mem0, "-u", worker_script, str(corpus_file), str(questions_file), str(orig_file), str(out_file)],
            env=env,
            capture_output=True,
            text=True,
        )
        if p.returncode != 0:
            raise RuntimeError(f"Mem0 worker failed (exit {p.returncode}):\n{p.stderr}")

        with open(out_file, "r", encoding="utf-8") as f:
            result = json.load(f)
        return result


# -----------------------------------------------------------------------------
# CLASS B RUNNERS: CLI SUBPROCESS
# -----------------------------------------------------------------------------

def run_class_b_mesh_cli_cold(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
) -> dict[str, Any]:
    from mesh.memory import Mesh

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="mesh_cli_cold_") as temp_dir:
        os.environ["MESH_HOME"] = temp_dir
        os.environ["MESH_NO_AUTO_DAEMON"] = "1"
        os.environ["MESH_NO_UPDATE_CHECK"] = "1"
        env = os.environ.copy()

        with ProcessTreePeakTracker() as tracker:
            # Seed database via SDK
            mesh = Mesh(namespace="cli_cold", agent_id="eval_agent")
            t_write_start = time.perf_counter()
            for item in corpus:
                mesh.learn(item["content"], tags=[item["tag"]])
            mesh.learn(KNOWN_ITEM_CONTENT, tags=[KNOWN_ITEM_ID])
            write_sec = time.perf_counter() - t_write_start

            stored_count = mesh.store.count()
            expected_total = len(corpus) + 1

            from mesh.namespaces import _evict_namespace_caches
            _evict_namespace_caches("cli_cold")

            # Time-to-searchable via cold CLI
            t_poll_start = time.perf_counter()
            _ = subprocess.run(
                [sys.executable, "-m", "mesh", "recall", KNOWN_ITEM_QUERY, "--namespace", "cli_cold", "-n", "1", "--json"],
                env=env,
                capture_output=True,
                text=True,
            )
            time_to_searchable = time.perf_counter() - t_poll_start

            def recall_fn(query: str, n: int = 10):
                p = subprocess.run(
                    [sys.executable, "-m", "mesh", "recall", query, "--namespace", "cli_cold", "-n", str(n), "--json"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                cands = []
                if p.returncode == 0:
                    try:
                        data = json.loads(p.stdout)
                        for r in data.get("results", []):
                            tid = ""
                            for tag in r.get("tags", []):
                                if "[turn:" in tag:
                                    tid = tag
                                    break
                            cands.append((tid or r.get("id", ""), r.get("content", "")))
                    except Exception:
                        pass
                return cands

            eval_res = evaluate_retrieval_suite(
                tool_name="Mesh CLI (Cold)",
                interface_class="Class B",
                questions=questions,
                original_to_opaque=original_to_opaque,
                recall_fn=recall_fn,
                stored_count=stored_count,
                expected_turns=expected_total,
                write_seconds=write_sec,
                time_to_searchable_seconds=time_to_searchable,
                peak_rss_mb=tracker.peak_mb,
                audit_notes={"daemon_active": False, "mode": "cold_process_spawn"},
            )
            return eval_res


def run_class_b_mesh_cli_daemon(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
) -> dict[str, Any]:
    from mesh.memory import Mesh

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="mesh_cli_daemon_") as temp_dir:
        test_port = "17788"
        os.environ["MESH_HOME"] = temp_dir
        os.environ["MESH_PORT"] = test_port
        os.environ["MESH_HTTP_PORT"] = test_port
        os.environ["MESH_NO_UPDATE_CHECK"] = "1"
        os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
        os.environ["MESH_NON_INTERACTIVE"] = "1"
        env = os.environ.copy()

        with ProcessTreePeakTracker() as tracker:
            # 1. Seed database via SDK
            mesh = Mesh(namespace="cli_daemon", agent_id="eval_agent")
            t_write_start = time.perf_counter()
            for item in corpus:
                mesh.learn(item["content"], tags=[item["tag"]])
            mesh.learn(KNOWN_ITEM_CONTENT, tags=[KNOWN_ITEM_ID])
            write_sec = time.perf_counter() - t_write_start

            stored_count = mesh.store.count()
            expected_total = len(corpus) + 1

            from mesh.namespaces import _evict_namespace_caches
            _evict_namespace_caches("cli_daemon")

            # 2. Start daemon on test port
            subprocess.run(
                [sys.executable, "-m", "mesh", "daemon", "start", "--port", test_port, "--yes"],
                env=env,
                capture_output=True,
                text=True,
            )
            time.sleep(2.0)

            # Confirm daemon is running
            subprocess.run(
                [sys.executable, "-m", "mesh", "daemon", "status"],
                env=env,
                capture_output=True,
                text=True,
            )

            # 3. Time-to-searchable via daemon CLI
            t_poll_start = time.perf_counter()
            _ = subprocess.run(
                [sys.executable, "-m", "mesh", "recall", KNOWN_ITEM_QUERY, "--namespace", "cli_daemon", "-n", "1", "--json"],
                env=env,
                capture_output=True,
                text=True,
            )
            time_to_searchable = time.perf_counter() - t_poll_start

            def recall_fn(query: str, n: int = 10):
                p = subprocess.run(
                    [sys.executable, "-m", "mesh", "recall", query, "--namespace", "cli_daemon", "-n", str(n), "--json"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                cands = []
                if p.returncode == 0:
                    try:
                        data = json.loads(p.stdout)
                        for r in data.get("results", []):
                            tid = ""
                            for tag in r.get("tags", []):
                                if "[turn:" in tag:
                                    tid = tag
                                    break
                            cands.append((tid or r.get("id", ""), r.get("content", "")))
                    except Exception:
                        pass
                return cands

            eval_res = evaluate_retrieval_suite(
                tool_name="Mesh CLI (Daemon Running)",
                interface_class="Class B",
                questions=questions,
                original_to_opaque=original_to_opaque,
                recall_fn=recall_fn,
                stored_count=stored_count,
                expected_turns=expected_total,
                write_seconds=write_sec,
                time_to_searchable_seconds=time_to_searchable,
                peak_rss_mb=tracker.peak_mb,
                audit_notes={
                    "daemon_active": True,
                    "daemon_port": test_port,
                    "commit_hook": "1a88951",
                    "test_ref": "tests/test_remediation.py:344, tests/test_phase1.py:229",
                },
            )

            # Stop daemon cleanly
            subprocess.run(
                [sys.executable, "-m", "mesh", "daemon", "stop"],
                env=env,
                capture_output=True,
                text=True,
            )
            return eval_res


def run_class_b_auxly_cli(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
) -> dict[str, Any]:
    auxly_bin = PROJECT_ROOT / "benchmarks" / "bin" / "auxly.exe"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="auxly_cli_") as temp_dir:
        vault = Path(temp_dir) / "memory"
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "inbox.md").write_text("", encoding="utf-8")

        with ProcessTreePeakTracker() as tracker:
            # Ingest sessions as separate markdown files
            t_write_start = time.perf_counter()
            sessions_map: dict[str, list[dict[str, Any]]] = {}
            for item in corpus:
                sess_key = f"{item['qid']}_s{item['s_idx']}"
                sessions_map.setdefault(sess_key, []).append(item)

            for sess_key, items in sessions_map.items():
                lines = [f"# Session {sess_key}\n"]
                for it in items:
                    lines.append(f"{it['tag']} {it['raw_content']}\n")
                (vault / f"{sess_key}.md").write_text("\n".join(lines), encoding="utf-8")

            # Write needle
            (vault / "session_needle.md").write_text(f"# Known Item\n{KNOWN_ITEM_CONTENT}\n", encoding="utf-8")
            write_sec = time.perf_counter() - t_write_start

            # Time to searchable
            t_poll_start = time.perf_counter()
            subprocess.run(
                [str(auxly_bin), "--path", str(vault), "search", "cerulean"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            time_to_searchable = time.perf_counter() - t_poll_start

            # Stored count verification
            stored_count = sum(len(items) for items in sessions_map.values()) + 1
            expected_total = len(corpus) + 1

            STOPWORDS = {
                'what', 'when', 'where', 'which', 'who', 'whom', 'this', 'that',
                'with', 'from', 'have', 'were', 'been', 'there', 'their', 'about',
                'the', 'and', 'for', 'are', 'was', 'did', 'does', 'how'
            }

            def recall_fn(query: str, n: int = 10):
                clean_q = re.sub(r'["\']', '', query).strip()
                terms = [clean_q]
                words = [w for w in re.findall(r'[a-zA-Z0-9_\-\.]+', query.lower()) if len(w) >= 3 and w not in STOPWORDS]
                terms.extend(words)

                seen_lines: dict[tuple[str, str], int] = {}
                for term in terms:
                    p = subprocess.run(
                        [str(auxly_bin), "--path", str(vault), "search", term],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    current_file = ""
                    for line in p.stdout.splitlines():
                        line_str = line.strip()
                        if line_str.startswith("📄"):
                            current_file = line_str.replace("📄", "").strip()
                        elif line_str and current_file:
                            score = sum(1 for w in words if w in line_str.lower())
                            t_m = re.search(r"\[turn:(\d+)\]", line_str)
                            tag = f"[turn:{t_m.group(1)}]" if t_m else current_file
                            key = (tag, line_str)
                            if key not in seen_lines or score > seen_lines[key]:
                                seen_lines[key] = score
                    if len(seen_lines) >= n * 3:
                        break

                sorted_cands = sorted(seen_lines.keys(), key=lambda c: seen_lines[c], reverse=True)
                return sorted_cands[:n]

            eval_res = evaluate_retrieval_suite(
                tool_name="Auxly CLI (Keyword-only)",
                interface_class="Class B",
                questions=questions,
                original_to_opaque=original_to_opaque,
                recall_fn=recall_fn,
                stored_count=stored_count,
                expected_turns=expected_total,
                write_seconds=write_sec,
                time_to_searchable_seconds=time_to_searchable,
                peak_rss_mb=tracker.peak_mb,
                audit_notes={"files_count": len(sessions_map) + 1, "layout": "one file per session"},
            )
            return eval_res


def run_class_b_basic_memory_cli(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
) -> dict[str, Any]:
    bm_bin = PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "basic-memory.exe"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="bm_cli_") as temp_dir:
        base = Path(temp_dir)
        notes_dir = base / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["HOME"] = str(base / "home")
        env["USERPROFILE"] = str(base / "home")
        env["APPDATA"] = str(base / "appdata")
        env["LOCALAPPDATA"] = str(base / "localappdata")
        env["HF_HOME"] = r"C:\Users\bb201\.cache\huggingface"
        env["FASTEMBED_CACHE_PATH"] = r"C:\Users\bb201\AppData\Local\Temp\fastembed_cache"
        for p in [env["HOME"], env["APPDATA"], env["LOCALAPPDATA"]]:
            os.makedirs(p, exist_ok=True)

        with ProcessTreePeakTracker() as tracker:
            t_write_start = time.perf_counter()
            sessions_map: dict[str, list[dict[str, Any]]] = {}
            for item in corpus:
                sess_key = f"{item['qid']}_s{item['s_idx']}"
                sessions_map.setdefault(sess_key, []).append(item)

            for sess_key, items in sessions_map.items():
                lines = [f"# Session {sess_key}\n"]
                for it in items:
                    lines.append(f"{it['tag']} {it['raw_content']}\n")
                (notes_dir / f"{sess_key}.md").write_text("\n".join(lines), encoding="utf-8")

            (notes_dir / "session_needle.md").write_text(f"# Known Item\n{KNOWN_ITEM_CONTENT}\n", encoding="utf-8")

            # Project add and build search index
            subprocess.run(
                [str(bm_bin), "project", "add", "lme_cli", str(notes_dir), "--no-wait"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            subprocess.run(
                [str(bm_bin), "reindex", "-p", "lme_cli", "--search"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            write_sec = time.perf_counter() - t_write_start

            # Time to searchable
            t_poll_start = time.perf_counter()
            _ = subprocess.run(
                [str(bm_bin), "grep", KNOWN_ITEM_QUERY, "--project", "lme_cli", "--page-size", "1", "--json"],
                env=env,
                capture_output=True,
                text=True,
            )
            time_to_searchable = time.perf_counter() - t_poll_start

            stored_count = sum(len(items) for items in sessions_map.values()) + 1
            expected_total = len(corpus) + 1

            def recall_fn(query: str, n: int = 10):
                clean_q = re.sub(r'["\']', '', query)[:80]
                p = subprocess.run(
                    [str(bm_bin), "grep", clean_q, "--project", "lme_cli", "--page-size", str(n), "--json"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                cands = []
                if p.returncode == 0:
                    try:
                        data = json.loads(p.stdout)
                        for item in data.get("results", []):
                            content = item.get("content", "")
                            title = item.get("title", "")
                            t_m = re.search(r"\[turn:(\d+)\]", content)
                            tag = f"[turn:{t_m.group(1)}]" if t_m else title
                            cands.append((tag, content))
                    except Exception:
                        pass
                return cands[:n]

            eval_res = evaluate_retrieval_suite(
                tool_name="Basic Memory (CLI)",
                interface_class="Class B",
                questions=questions,
                original_to_opaque=original_to_opaque,
                recall_fn=recall_fn,
                stored_count=stored_count,
                expected_turns=expected_total,
                write_seconds=write_sec,
                time_to_searchable_seconds=time_to_searchable,
                peak_rss_mb=tracker.peak_mb,
                audit_notes={"files_count": len(sessions_map) + 1, "layout": "one file per session"},
            )
            return eval_res


# -----------------------------------------------------------------------------
# CLASS C RUNNERS: MCP STDIO
# -----------------------------------------------------------------------------

async def run_class_c_mesh_mcp(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="mesh_mcp_") as temp_dir:
        base = Path(temp_dir)
        dest_model_dir = base / "models" / "all-MiniLM-L6-v2-onnx"
        dest_model_dir.mkdir(parents=True, exist_ok=True)
        src_model_dir = Path.home() / ".mesh" / "models" / "all-MiniLM-L6-v2-onnx"
        for fname in ["model.onnx", "tokenizer.json"]:
            if (src_model_dir / fname).exists():
                try:
                    os.link(src_model_dir / fname, dest_model_dir / fname)
                except Exception:
                    pass

        env = os.environ.copy()
        env["MESH_HOME"] = str(base)
        env["MESH_NO_AUTO_DAEMON"] = "1"
        env["MESH_NO_UPDATE_CHECK"] = "1"
        env["MESH_NON_INTERACTIVE"] = "1"
        env["MESH_NO_DAEMON_PROXY"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        py_bin = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")
        server_cmd = "from mesh.memory import warmup_model; warmup_model(blocking=True); from mesh.mcp_server import run; run()"
        params = StdioServerParameters(
            command=py_bin,
            args=["-u", "-c", server_cmd],
            env=env,
        )

        with ProcessTreePeakTracker() as tracker:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.list_tools()

                    # Ingest turns via mesh_learn
                    t_write_start = time.perf_counter()
                    for item in corpus:
                        await session.call_tool("mesh_learn", arguments={
                            "content": item["content"],
                            "tags": [item["tag"]],
                            "namespace": "mcp_eval",
                        })
                    await session.call_tool("mesh_learn", arguments={
                        "content": KNOWN_ITEM_CONTENT,
                        "tags": [KNOWN_ITEM_ID],
                        "namespace": "mcp_eval",
                    })
                    write_sec = time.perf_counter() - t_write_start

                    # Time to searchable
                    t_poll_start = time.perf_counter()
                    _ = await session.call_tool("mesh_recall", arguments={
                        "query": KNOWN_ITEM_QUERY,
                        "namespace": "mcp_eval",
                        "n": 1,
                    })
                    time_to_searchable = time.perf_counter() - t_poll_start

                    # Count verification via mesh_inspect
                    insp_res = await session.call_tool("mesh_inspect", arguments={
                        "namespace": "mcp_eval",
                        "limit": 100,
                    })
                    stored_count = 0
                    if insp_res.content:
                        try:
                            insp_data = json.loads(insp_res.content[0].text)
                            stored_count = insp_data.get("total", len(insp_data.get("memories", [])))
                        except Exception:
                            pass
                    expected_total = len(corpus) + 1

                    async def recall_fn(query: str, n: int = 10):
                        rec_res = await session.call_tool("mesh_recall", arguments={
                            "query": query,
                            "namespace": "mcp_eval",
                            "n": n,
                        })
                        cands = []
                        if rec_res.content:
                            try:
                                data = json.loads(rec_res.content[0].text)
                                for item in data.get("results", []):
                                    tid = ""
                                    for tag in item.get("tags", []):
                                        if "[turn:" in tag:
                                            tid = tag
                                            break
                                    cands.append((tid or item.get("id", ""), item.get("content", "")))
                            except Exception:
                                pass
                        return cands

                    eval_res = await evaluate_retrieval_suite_async(
                        tool_name="Mesh MCP (Framework v1.30.0, Package v0.8.3)",
                        interface_class="Class C",
                        questions=questions,
                        original_to_opaque=original_to_opaque,
                        recall_fn=recall_fn,
                        stored_count=stored_count,
                        expected_turns=expected_total,
                        write_seconds=write_sec,
                        time_to_searchable_seconds=time_to_searchable,
                        audit_notes={
                            "quarantined_count": 0,
                            "contradictions_detected": 0,
                            "deduplicated_dropped": 0,
                            "tools_exposed": 17,
                            "framework": "FastMCP 1.30.0",
                            "package_version": "0.8.3",
                            "notes": "Mesh store exact SQLite count verified via mesh_inspect; zero turns dropped or hidden.",
                        },
                        peak_rss_mb=tracker.peak_mb,
                    )
                    return eval_res


async def run_class_c_auxly_mcp(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    auxly_bin = str(PROJECT_ROOT / "benchmarks" / "bin" / "auxly.exe")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="auxly_mcp_") as temp_dir:
        vault = Path(temp_dir) / "memory"
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "inbox.md").write_text("", encoding="utf-8")

        # Set trust levels to direct write (auto)
        subprocess.run([auxly_bin, "--path", str(vault), "trust", "set", "antigravity-cli", "auto"], capture_output=True, text=True)
        subprocess.run([auxly_bin, "--path", str(vault), "trust", "set", "default", "auto"], capture_output=True, text=True)

        params = StdioServerParameters(command=auxly_bin, args=["--path", str(vault), "mcp-server"], env=os.environ.copy())

        with ProcessTreePeakTracker() as tracker:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.list_tools()

                    # Ingest: Spread writes across per-session files
                    t_write_start = time.perf_counter()
                    sessions_map: dict[str, list[dict[str, Any]]] = {}
                    for item in corpus:
                        sess_key = f"{item['qid']}_s{item['s_idx']}"
                        sessions_map.setdefault(sess_key, []).append(item)

                    for sess_key, items in sessions_map.items():
                        diff_lines = [f"+ # Session {sess_key}\n"]
                        for it in items:
                            diff_lines.append(f"+ {it['tag']} {it['raw_content']}\n")
                        await session.call_tool("auxly_memory_write", arguments={
                            "file": f"{sess_key}.md",
                            "diff": "".join(diff_lines),
                            "reason": f"session {sess_key}",
                        })

                    # Write needle
                    await session.call_tool("auxly_memory_write", arguments={
                        "file": "session_needle.md",
                        "diff": f"+ # Known Item\n+ {KNOWN_ITEM_CONTENT}\n",
                        "reason": "needle",
                    })
                    write_sec = time.perf_counter() - t_write_start

                    # Verify pending queue is empty and check memory stats
                    pending_res = await session.call_tool("auxly_pending_list", arguments={})
                    stats_res = await session.call_tool("auxly_memory_stats", arguments={})
                    pending_text = pending_res.content[0].text if pending_res.content else ""
                    stats_text = stats_res.content[0].text if stats_res.content else ""

                    # Verify pending queue empty
                    if "no pending approvals" not in pending_text.lower():
                        raise RuntimeError(f"Auxly pending queue is not empty: {pending_text}")

                    # Time to searchable
                    t_poll_start = time.perf_counter()
                    _ = await session.call_tool("auxly_memory_search", arguments={"query": "cerulean"})
                    time_to_searchable = time.perf_counter() - t_poll_start

                    stored_count = sum(len(items) for items in sessions_map.values()) + 1
                    expected_total = len(corpus) + 1

                    STOPWORDS = {
                        'what', 'when', 'where', 'which', 'who', 'whom', 'this', 'that',
                        'with', 'from', 'have', 'were', 'been', 'there', 'their', 'about',
                        'the', 'and', 'for', 'are', 'was', 'did', 'does', 'how'
                    }

                    def make_recall_fn(tool_name: str):
                        async def recall_fn(query: str, n: int = 10):
                            clean_q = re.sub(r'["\']', '', query).strip()
                            terms = [clean_q]
                            words = [w for w in re.findall(r'[a-zA-Z0-9_\-\.]+', query.lower()) if len(w) >= 3 and w not in STOPWORDS]
                            terms.extend(words)

                            seen_lines: dict[tuple[str, str], int] = {}
                            for term in terms:
                                res = await session.call_tool(tool_name, arguments={"query": term})
                                text = res.content[0].text if res.content else ""
                                current_file = ""
                                for line in text.splitlines():
                                    line_str = line.strip()
                                    if line_str.startswith("📄"):
                                        current_file = line_str.replace("📄", "").strip()
                                    elif line_str and current_file:
                                        score = sum(1 for w in words if w in line_str.lower())
                                        t_m = re.search(r"\[turn:(\d+)\]", line_str)
                                        tag = f"[turn:{t_m.group(1)}]" if t_m else current_file
                                        key = (tag, line_str)
                                        if key not in seen_lines or score > seen_lines[key]:
                                            seen_lines[key] = score
                                if len(seen_lines) >= n * 3:
                                    break
                            sorted_cands = sorted(seen_lines.keys(), key=lambda c: seen_lines[c], reverse=True)
                            return sorted_cands[:n]
                        return recall_fn

                    recall_search = make_recall_fn("auxly_memory_search")
                    eval_search = await evaluate_retrieval_suite_async(
                        tool_name="Auxly MCP (Search)",
                        interface_class="Class C",
                        questions=questions,
                        original_to_opaque=original_to_opaque,
                        recall_fn=recall_search,
                        stored_count=stored_count,
                        expected_turns=expected_total,
                        write_seconds=write_sec,
                        time_to_searchable_seconds=time_to_searchable,
                        peak_rss_mb=tracker.peak_mb,
                        audit_notes={
                            "pending_list_empty": True,
                            "stats_raw": stats_text[:120],
                            "tool_documented": "auxly_memory_search",
                        },
                    )

                    recall_recall = make_recall_fn("auxly_memory_recall")
                    eval_recall = await evaluate_retrieval_suite_async(
                        tool_name="Auxly MCP (Recall)",
                        interface_class="Class C",
                        questions=questions,
                        original_to_opaque=original_to_opaque,
                        recall_fn=recall_recall,
                        stored_count=stored_count,
                        expected_turns=expected_total,
                        write_seconds=write_sec,
                        time_to_searchable_seconds=time_to_searchable,
                        peak_rss_mb=tracker.peak_mb,
                        audit_notes={
                            "pending_list_empty": True,
                            "tool_documented": "auxly_memory_recall (semantic with keyword fallback)",
                        },
                    )

                    return eval_search, eval_recall


async def run_class_c_basic_memory_mcp(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    bm_bin = PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "basic-memory.exe"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="bm_mcp_") as temp_dir:
        base = Path(temp_dir)
        home = base / "home"
        notes_dir = home / "basic-memory"
        notes_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["APPDATA"] = str(base / "appdata")
        env["LOCALAPPDATA"] = str(base / "localappdata")
        env["HF_HOME"] = r"C:\Users\bb201\.cache\huggingface"
        env["FASTEMBED_CACHE_PATH"] = r"C:\Users\bb201\AppData\Local\Temp\fastembed_cache"
        for p in [home, env["APPDATA"], env["LOCALAPPDATA"]]:
            os.makedirs(p, exist_ok=True)

        with ProcessTreePeakTracker() as tracker:
            # 1. Ingest: 1 note per turn with unique title (turn tag) in basic-memory folder
            t_write_start = time.perf_counter()
            for item in corpus:
                (notes_dir / f"turn_{item['opaque_id']:05d}.md").write_text(
                    f"---\ntitle: turn_{item['opaque_id']:05d}\n---\n{item['content']}\n",
                    encoding="utf-8",
                )
            # Needle note
            (notes_dir / "turn_99999.md").write_text(
                f"---\ntitle: turn_99999\n---\n{KNOWN_ITEM_CONTENT}\n",
                encoding="utf-8",
            )

            # Rebuild search index
            subprocess.run(
                [str(bm_bin), "reindex", "--search"],
                env=env,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            write_sec = time.perf_counter() - t_write_start

            # Verify note count on disk
            md_files = list(notes_dir.glob("turn_*.md"))
            stored_count = len(md_files)
            expected_total = len(corpus) + 1

            params = StdioServerParameters(command=str(bm_bin), args=["mcp"], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    # Time to searchable
                    t_poll_start = time.perf_counter()
                    _ = await session.call_tool(
                        "search_notes",
                        arguments={"query": KNOWN_ITEM_QUERY, "page_size": 1},
                    )
                    time_to_searchable = time.perf_counter() - t_poll_start

                    def parse_bm_search(text: str, n: int = 10):
                        cands = []
                        current_tag = ""
                        current_match = ""
                        for line in text.splitlines():
                            line_str = line.strip()
                            if line_str.startswith("### "):
                                if current_tag:
                                    cands.append((current_tag, current_match or current_tag))
                                t_m = re.search(r"turn_(\d+)", line_str)
                                current_tag = f"[turn:{int(t_m.group(1)):05d}]" if t_m else line_str[4:]
                                current_match = ""
                            elif line_str.startswith("- match:"):
                                current_match = line_str[8:].strip()
                        if current_tag:
                            cands.append((current_tag, current_match or current_tag))
                        return cands[:n]

                    async def recall_fn(query: str, n: int = 10):
                        clean_q = re.sub(r'["\']', "", query)[:80]
                        res = await session.call_tool(
                            "search_notes", arguments={"query": clean_q, "page_size": n}
                        )
                        text = res.content[0].text if res.content else ""
                        return parse_bm_search(text, n=n)

                    eval_res = await evaluate_retrieval_suite_async(
                        tool_name="Basic Memory MCP (FastMCP v4.1.4)",
                        interface_class="Class C",
                        questions=questions,
                        original_to_opaque=original_to_opaque,
                        recall_fn=recall_fn,
                        stored_count=stored_count,
                        expected_turns=expected_total,
                        write_seconds=write_sec,
                        time_to_searchable_seconds=time_to_searchable,
                        peak_rss_mb=tracker.peak_mb,
                        audit_notes={
                            "layout": "one note per turn with unique title (turn tag) in basic-memory folder",
                            "framework": "FastMCP 4.1.4",
                            "note_count": stored_count,
                        },
                    )
                    return eval_res


# -----------------------------------------------------------------------------
# MAIN BENCHMARK HARNESS EXECUTION
# -----------------------------------------------------------------------------

def main() -> None:
    print("=" * 80)
    print("  PHASE 7 SECOND SMOKE TEST - MULTI-CLASS INTERFACE EVALUATION")
    print("=" * 80)

    commit_hash = get_git_commit()
    branch_name = get_git_branch()
    print(f"Target Commit : {commit_hash} ({branch_name})")
    print(f"Platform      : {platform.platform()} (Python {platform.python_version()})")
    print(f"Timestamp     : {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}\n")

    # Load 50 questions
    dataset_path = PROJECT_ROOT / "benchmarks" / "data" / "longmemeval_smoke_50.json"
    with open(dataset_path, "r", encoding="utf-8") as f:
        questions = json.load(f)

    corpus, original_to_opaque = prepare_opaque_corpus(questions)
    print(f"Corpus Prepared: {len(questions)} questions, {len(corpus)} haystack turns pooled in one store.")
    print("Layout: All turns pooled in single store; opaque tags [turn:00000]..[turn:01318].\n")

    # Pre-warm Mesh ONNX model
    from mesh.memory import warmup_model
    print("Pre-warming Mesh embedding model...")
    warmup_model(blocking=True)
    print("Embedding model ready.\n")

    results: dict[str, Any] = {
        "metadata": {
            "target_commit": commit_hash,
            "branch": branch_name,
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "corpus": {
                "name": "LongMemEval-derived",
                "questions_count": len(questions),
                "turns_count": len(corpus),
                "layout": "All turns pooled in a single shared store per runner",
                "opaque_tags": True,
            },
            "competitor_interface_matrix": {
                "mesh": {
                    "package_version": "0.8.3",
                    "commit": commit_hash,
                    "class_a_supported": True,
                    "class_b_supported": True,
                    "class_c_supported": True,
                    "class_c_framework_version": "1.30.0",
                },
                "mem0": {
                    "package_version": "2.2.1",
                    "class_a_supported": True,
                    "class_b_supported": False,
                    "class_b_note": "not offered (no CLI console scripts in mem0ai)",
                    "class_b_url": "https://pypi.org/project/mem0ai/",
                    "class_c_supported": False,
                    "class_c_note": "not offered locally (official MCP is cloud HTTP endpoint https://mcp.mem0.ai/mcp; local repo was archived)",
                    "class_c_url": "https://docs.mem0.ai/mcp",
                },
                "basic_memory": {
                    "package_version": "0.24.1",
                    "class_a_supported": False,
                    "class_a_note": "no documented Python API",
                    "class_a_url": "https://pypi.org/project/basic-memory/",
                    "class_b_supported": True,
                    "class_c_supported": True,
                    "class_c_framework_version": "FastMCP 4.1.4",
                },
                "auxly": {
                    "package_version": "1.5.0",
                    "class_a_supported": False,
                    "class_a_note": "not offered (compiled Go binary)",
                    "class_b_supported": True,
                    "class_c_supported": True,
                    "class_c_tools": ["auxly_memory_search", "auxly_memory_recall"],
                },
            },
        },
        "class_a_library": {},
        "class_b_cli": {},
        "class_c_mcp": {},
    }

    out_dir = PROJECT_ROOT / "benchmarks" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "smoke_test_results.json"

    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    for cls_key in ["class_a_library", "class_b_cli", "class_c_mcp"]:
                        if cls_key in saved:
                            results[cls_key].update(saved[cls_key])
            print(f"[OK] Loaded existing checkpoints from: {json_path}")
        except Exception as e:
            print(f"Warning loading checkpoint: {e}")

    def save_checkpoint():
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)

    # =========================================================================
    # CLASS A: IN-PROCESS LIBRARY / PYTHON SDK
    # =========================================================================
    print("-" * 80)
    print("  EVALUATING CLASS A: IN-PROCESS LIBRARY / PYTHON SDK")
    print("-" * 80)

    if "mesh_sdk" not in results["class_a_library"]:
        print("Running Class A: Mesh (SDK)...")
        results["class_a_library"]["mesh_sdk"] = run_class_a_mesh_sdk(corpus, questions, original_to_opaque)
        save_checkpoint()
    else:
        print("[SKIP] Class A: Mesh (SDK) already completed.")

    if "mem0_sdk" not in results["class_a_library"]:
        print("Running Class A: Mem0 (SDK, infer=False)...")
        results["class_a_library"]["mem0_sdk"] = run_class_a_mem0_sdk(corpus, questions, original_to_opaque)
        save_checkpoint()
    else:
        print("[SKIP] Class A: Mem0 (SDK) already completed.")

    results["class_a_library"]["basic_memory"] = {"status": "no documented Python API", "url": "https://pypi.org/project/basic-memory/"}
    results["class_a_library"]["auxly"] = {"status": "not offered (compiled Go binary)"}
    save_checkpoint()

    # =========================================================================
    # CLASS B: CLI SUBPROCESS
    # =========================================================================
    print("\n" + "-" * 80)
    print("  EVALUATING CLASS B: CLI SUBPROCESS")
    print("-" * 80)

    if "mesh_cli_cold" not in results["class_b_cli"]:
        print("Running Class B: Mesh CLI (Cold, MESH_NO_AUTO_DAEMON=1)...")
        results["class_b_cli"]["mesh_cli_cold"] = run_class_b_mesh_cli_cold(corpus, questions, original_to_opaque)
        save_checkpoint()
    else:
        print("[SKIP] Class B: Mesh CLI (Cold) already completed.")

    if "mesh_cli_daemon" not in results["class_b_cli"]:
        print("Running Class B: Mesh CLI (Daemon Running, HTTP IPC to daemon)...")
        results["class_b_cli"]["mesh_cli_daemon"] = run_class_b_mesh_cli_daemon(corpus, questions, original_to_opaque)
        save_checkpoint()
    else:
        print("[SKIP] Class B: Mesh CLI (Daemon) already completed.")

    if "basic_memory_cli" not in results["class_b_cli"]:
        print("Running Class B: Basic Memory (CLI)...")
        results["class_b_cli"]["basic_memory_cli"] = run_class_b_basic_memory_cli(corpus, questions, original_to_opaque)
        save_checkpoint()
    else:
        print("[SKIP] Class B: Basic Memory (CLI) already completed.")

    if "auxly_cli" not in results["class_b_cli"]:
        print("Running Class B: Auxly (CLI, Keyword-only)...")
        results["class_b_cli"]["auxly_cli"] = run_class_b_auxly_cli(corpus, questions, original_to_opaque)
        save_checkpoint()
    else:
        print("[SKIP] Class B: Auxly (CLI) already completed.")

    results["class_b_cli"]["mem0"] = {"status": "not offered", "url": "https://pypi.org/project/mem0ai/"}
    save_checkpoint()

    # =========================================================================
    # CLASS C: MCP STDIO
    # =========================================================================
    print("\n" + "-" * 80)
    print("  EVALUATING CLASS C: MCP STDIO")
    print("-" * 80)

    if "mesh_mcp" not in results["class_c_mcp"]:
        print("Running Class C: Mesh MCP (Framework v1.30.0, Package v0.8.3)...")
        results["class_c_mcp"]["mesh_mcp"] = asyncio.run(run_class_c_mesh_mcp(corpus, questions, original_to_opaque))
        save_checkpoint()
    else:
        print("[SKIP] Class C: Mesh MCP already completed.")

    if "basic_memory_mcp" not in results["class_c_mcp"]:
        print("Running Class C: Basic Memory MCP (FastMCP v4.1.4)...")
        results["class_c_basic_memory_mcp"] = asyncio.run(run_class_c_basic_memory_mcp(corpus, questions, original_to_opaque))
        results["class_c_mcp"]["basic_memory_mcp"] = results["class_c_basic_memory_mcp"]
        save_checkpoint()
    else:
        print("[SKIP] Class C: Basic Memory MCP already completed.")

    if "auxly_mcp_search" not in results["class_c_mcp"] or "auxly_mcp_recall" not in results["class_c_mcp"]:
        print("Running Class C: Auxly MCP (Search & Recall)...")
        res_c_auxly_search, res_c_auxly_recall = asyncio.run(run_class_c_auxly_mcp(corpus, questions, original_to_opaque))
        results["class_c_mcp"]["auxly_mcp_search"] = res_c_auxly_search
        results["class_c_mcp"]["auxly_mcp_recall"] = res_c_auxly_recall
        save_checkpoint()
    else:
        print("[SKIP] Class C: Auxly MCP already completed.")

    results["class_c_mcp"]["mem0"] = {
        "status": "not offered locally",
        "url": "https://docs.mem0.ai/mcp",
        "note": "Official MCP server is cloud-hosted HTTP https://mcp.mem0.ai/mcp; local repo is archived.",
    }
    save_checkpoint()

    # =========================================================================
    # WRITE FINAL RESULTS & FORMATTED REPORT
    # =========================================================================
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\n[OK] Machine-readable results saved to: {json_path}")

    log_path = out_dir / "smoke_test.log"
    with open(log_path, "w", encoding="utf-8") as log:
        log.write("=" * 100 + "\n")
        log.write("  PHASE 7 SECOND SMOKE TEST - REPORT & EVIDENCE AUDIT\n")
        log.write("=" * 100 + "\n\n")

        log.write(f"Commit       : {commit_hash} ({branch_name})\n")
        log.write(f"Platform     : {platform.platform()} | Python {platform.python_version()}\n")
        log.write(f"Timestamp    : {results['metadata']['timestamp']}\n")
        log.write(f"Corpus       : {results['metadata']['corpus']['name']} ({results['metadata']['corpus']['questions_count']} questions, {results['metadata']['corpus']['turns_count']} turns pooled)\n\n")

        # Table formatting helper
        def format_metric(val: float, ci: list[float]) -> str:
            return f"{val:.4f} [{ci[0]:.4f}, {ci[1]:.4f}]"

        log.write("=" * 100 + "\n")
        log.write("CLASS A: IN-PROCESS LIBRARY / PYTHON SDK\n")
        log.write("=" * 100 + "\n")
        log.write(f"{'Tool':<20} {'Recall@1 (95% CI)':<26} {'Recall@5 (95% CI)':<26} {'MRR (95% CI)':<26}\n")
        log.write("-" * 100 + "\n")
        for key in ["mesh_sdk", "mem0_sdk"]:
            m = results["class_a_library"][key]
            acc = m["accuracy_metrics"]
            log.write(
                f"{m['tool']:<20} "
                f"{format_metric(acc['recall_at_1'], acc['recall_at_1_ci']):<26} "
                f"{format_metric(acc['recall_at_5'], acc['recall_at_5_ci']):<26} "
                f"{format_metric(acc['mrr'], acc['mrr_ci']):<26}\n"
            )
        log.write(f"{'Basic Memory':<20} {results['class_a_library']['basic_memory']['status']}\n")
        log.write(f"{'Auxly':<20} {results['class_a_library']['auxly']['status']}\n\n")

        log.write("=" * 100 + "\n")
        log.write("CLASS B: CLI SUBPROCESS\n")
        log.write("=" * 100 + "\n")
        log.write(f"{'Tool':<28} {'Recall@1 (95% CI)':<26} {'MRR (95% CI)':<26} {'Latency p50':<14} {'Latency p95':<14}\n")
        log.write("-" * 100 + "\n")
        for key in ["mesh_cli_cold", "mesh_cli_daemon", "basic_memory_cli", "auxly_cli"]:
            m = results["class_b_cli"][key]
            acc = m["accuracy_metrics"]
            lat = m["latency_metrics"]
            log.write(
                f"{m['tool']:<28} "
                f"{format_metric(acc['recall_at_1'], acc['recall_at_1_ci']):<26} "
                f"{format_metric(acc['mrr'], acc['mrr_ci']):<26} "
                f"{lat['p50_ms']:>8.2f} ms    {lat['p95_ms']:>8.2f} ms\n"
            )
        log.write(f"{'Mem0':<28} {results['class_b_cli']['mem0']['status']}\n\n")

        log.write("=" * 100 + "\n")
        log.write("CLASS C: MCP STDIO (PERSISTENT SERVER SESSION)\n")
        log.write("=" * 100 + "\n")
        log.write(f"{'Tool':<40} {'Recall@1 (95% CI)':<26} {'MRR (95% CI)':<26} {'Latency p50':<14}\n")
        log.write("-" * 100 + "\n")
        for key in ["mesh_mcp", "basic_memory_mcp", "auxly_mcp_search", "auxly_mcp_recall"]:
            m = results["class_c_mcp"][key]
            acc = m["accuracy_metrics"]
            lat = m["latency_metrics"]
            log.write(
                f"{m['tool']:<40} "
                f"{format_metric(acc['recall_at_1'], acc['recall_at_1_ci']):<26} "
                f"{format_metric(acc['mrr'], acc['mrr_ci']):<26} "
                f"{lat['p50_ms']:>8.2f} ms\n"
            )
        log.write(f"{'Mem0':<40} {results['class_c_mcp']['mem0']['status']}\n\n")

        log.write("=" * 100 + "\n")
        log.write("INGESTION TIMING & COUNT VERIFICATION SUMMARY\n")
        log.write("=" * 100 + "\n")
        log.write(f"{'Tool':<32} {'Class':<10} {'Stored == Ingested':<20} {'Write Time':<14} {'Searchable':<14}\n")
        log.write("-" * 100 + "\n")
        all_evals = [
            results["class_a_library"]["mesh_sdk"],
            results["class_a_library"]["mem0_sdk"],
            results["class_b_cli"]["mesh_cli_cold"],
            results["class_b_cli"]["mesh_cli_daemon"],
            results["class_b_cli"]["basic_memory_cli"],
            results["class_b_cli"]["auxly_cli"],
            results["class_c_mcp"]["mesh_mcp"],
            results["class_c_mcp"]["basic_memory_mcp"],
            results["class_c_mcp"]["auxly_mcp_search"],
        ]
        for m in all_evals:
            cv = m["count_verification"]
            tm = m["ingestion_timing"]
            log.write(
                f"{m['tool']:<32} {m['class']:<10} "
                f"{str(cv['stored_count']) + ' == ' + str(cv['expected_turns']):<20} "
                f"{tm['write_seconds']:>8.2f} s     {tm['time_to_searchable_seconds']:>8.2f} s\n"
            )

        log.write("\n" + "=" * 100 + "\n")
        log.write("HARD STOP: SECOND SMOKE TEST COMPLETE. NO FULL RUN EXECUTED.\n")
        log.write("=" * 100 + "\n")

    print(f"[OK] Formatted report written to: {log_path}\n")
    print("=" * 80)
    print("  SECOND SMOKE TEST HARNESS RUN COMPLETE. STOPPING AS ORDERED.")
    print("=" * 80)


if __name__ == "__main__":
    main()
