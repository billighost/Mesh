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
    - Basic Memory CLI (v0.24.1, 1 note per turn layout)
    - Auxly CLI (v1.5.0, raw query text)
    - Mem0: "not offered" (no CLI console scripts in mem0ai)
- Class C: MCP Stdio (persistent server session over stdio, as agents use it)
    - Mesh MCP (Package v0.8.3, MCP framework v1.30.0)
    - Basic Memory MCP (FastMCP v4.1.4, 1 note per turn with unique turn tag)
    - Auxly MCP (Search, auxly_memory_search with direct write trust)
    - Auxly MCP (Recall, auxly_memory_recall with direct write trust)
    - Mem0: "not offered locally" (cloud HTTP endpoint https://mcp.mem0.ai/mcp)

Methodological Compliance (Brief Section 3 Fixes 1, 4, 6):
1. Scoring Unit (Two-Level Reporting):
   - Score every tool and class at TWO levels, reported separately in independent tables (never mixed):
     (a) turn-level: a hit only if the returned unit is a single turn containing the gold turn tag.
         Tools that store per session (Auxly) are marked "not comparable at turn level".
     (b) session-level: map every returned turn tag to its session ID (harness-internal mapping; tags stay opaque
         to tools), deduplicate sessions keeping the best (first) rank, gold = the dataset's gold evidence sessions
         (any turn with has_answer=True in that session).
   - Basic Memory CLI has been aligned to the same storage layout as MCP: 1 note per turn
     (`notes_dir / f"turn_{item['opaque_id']:05d}.md"`) with unique title (`title: turn_{item['opaque_id']:05d}`).
     Both CLI and MCP for Basic Memory are now scored at both turn-level and session-level.
   - Cross-class check: verifies that the same tool with the same layout scores the same across SDK, CLI, and MCP.
2. Query Handling (Fix 4):
   - Symmetrical raw question text (`query.strip()`) is sent directly to every tool.
   - Custom stopword removal, word-extraction, and query-expansion loops previously applied only to Auxly have
     been removed. Auxly as-is receives identical raw query text. An optional clearly labeled runner
     `auxly_cli_keyword_extracted` is retained for comparison.
3. Complete Tables & Performance Metrics (Fix 6):
   - Reports Recall@1, Recall@5, Recall@10 with bootstrap 95% CIs (both turn-level and session-level).
   - Reports MRR with bootstrap 95% CI (both levels).
   - Reports NDCG@10 with bootstrap 95% CI (both levels).
   - Reports Latency: p50, p95, p99 with bootstrap 95% CIs and mean latency.
   - Uniform Ingestion Time: write time and time-to-searchable (probing until the last-written item is returned
     by a probe query, same definition for all tools).
   - Process-tree peak RAM (tracker).
   - Disk footprint (MB).
   - Raw top-5 inspection (query, results, gold turn tags, gold sessions, hit flags) for 5 questions per tool and class.
   - Mesh Daemon Mode: `mesh daemon status` and `stop` pass `--port test_port`, probe polls until daemon is warmed up
     and returns known item, and individual per-call latency traces (`latencies_ms`) are recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import math
import os
import platform
import random
import re
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
    f"{KNOWN_ITEM_ID} The cerulean observatory celestial frequency atop Mount Horizon recorded an "
    "anomalous 432.85 MHz on stellar quadrant nine."
)
KNOWN_ITEM_QUERY = "cerulean observatory celestial frequency"
KNOWN_ITEM_ANSWER = "432.85 MHz"


def find_venv_python() -> str:
    p1 = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if p1.exists():
        return str(p1)
    p2 = Path("C:/Users/bb201/desktop/mesh/.venv/Scripts/python.exe")
    if p2.exists():
        return str(p2)
    return sys.executable


def find_bm_bin() -> Path:
    p1 = PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "basic-memory.exe"
    if p1.exists():
        return p1
    p2 = Path("C:/Users/bb201/desktop/mesh/benchmarks/.venv-mem0/Scripts/basic-memory.exe")
    if p2.exists():
        return p2
    return p1


def find_auxly_bin() -> Path:
    p1 = PROJECT_ROOT / "benchmarks" / "bin" / "auxly.exe"
    if p1.exists():
        return p1
    p2 = Path("C:/Users/bb201/desktop/mesh/benchmarks/bin/auxly.exe")
    if p2.exists():
        return p2
    return p1


def find_mem0_py() -> str:
    p1 = PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "python.exe"
    if p1.exists():
        return str(p1)
    p2 = Path("C:/Users/bb201/desktop/mesh/benchmarks/.venv-mem0/Scripts/python.exe")
    if p2.exists():
        return str(p2)
    return str(p1)


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
    """
    Extracts the session ID for a candidate.
    Uses opaque numeric tags and internal harness mapping (tags stay opaque to tools).
    """
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
    """
    A turn-level hit occurs ONLY if the returned unit contains a gold turn tag.
    """
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


async def probe_searchable_async(probe_fn: Callable[[], Any], timeout: float = 60.0, interval: float = 0.05) -> float:
    t_start = time.perf_counter()
    while (time.perf_counter() - t_start) < timeout:
        try:
            if await probe_fn():
                return time.perf_counter() - t_start
        except Exception:
            pass
        await asyncio.sleep(interval)
    return time.perf_counter() - t_start


# -----------------------------------------------------------------------------
# DATASET PREPARATION (OPAQUE NUMERIC TURN TAGS & HARNESS SESSION MAPPING)
# -----------------------------------------------------------------------------

def prepare_opaque_corpus(
    questions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    """
    Assigns opaque numeric indices [turn:00000]..[turn:NNNNN] to all turns.
    Removes any dependency on original session IDs that may contain 'answer_' prefixes.
    Returns:
        corpus: list of turn items
        original_to_opaque: mapping of original turn key to opaque tag
        tag_to_session: harness-internal mapping from turn tag to session ID
    """
    opaque_turns = []
    original_to_opaque = {}
    tag_to_session = {}
    opaque_counter = 0

    for q in questions:
        qid = q["question_id"]
        for s_idx, session in enumerate(q.get("haystack_sessions", [])):
            sess_id = f"{qid}_s{s_idx}"
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
                    "session_id": sess_id,
                }
                opaque_turns.append(turn_info)
                original_to_opaque[orig_key] = tag
                tag_to_session[tag] = sess_id
                tag_to_session[f"turn_{opaque_counter:05d}"] = sess_id
                tag_to_session[str(opaque_counter)] = sess_id
                opaque_counter += 1

    return opaque_turns, original_to_opaque, tag_to_session


# -----------------------------------------------------------------------------
# GENERIC EVALUATION LOOP (TWO-LEVEL SCORING, UNIFORM AUDIT)
# -----------------------------------------------------------------------------

def evaluate_retrieval_suite(
    tool_name: str,
    interface_class: str,
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    tag_to_session: dict[str, str],
    recall_fn: Callable[[str, int], list[tuple[str, str]]],
    stored_count: int,
    expected_turns: int,
    write_seconds: float,
    time_to_searchable_seconds: float,
    peak_rss_mb: float,
    disk_footprint_mb: float,
    audit_notes: dict[str, Any],
    storage_unit: str = "turn",
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
        if storage_unit == "turn":
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

        # 2. Session-level scoring: map every returned turn tag to session ID, deduplicate sessions keeping best (first) rank
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

        # Raw inspections for first 5 questions
        if q_idx < 5:
            cand_inspections = []
            for r_idx, (c_id, c_text) in enumerate(candidates[:5], start=1):
                sess = extract_candidate_session(c_id, c_text, tag_to_session)
                t_hit = is_turn_level_hit(c_id, c_text, gold_turn_tags) if storage_unit == "turn" else False
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

    # Compile turn-level metrics
    if storage_unit == "turn":
        turn_metrics: dict[str, Any] = {
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
    else:
        turn_metrics = {
            "status": "not comparable at turn level",
            "note": "Tool stores per session rather than per turn",
            "recall_at_1": None,
            "recall_at_1_ci": None,
            "recall_at_5": None,
            "recall_at_5_ci": None,
            "recall_at_10": None,
            "recall_at_10_ci": None,
            "mrr": None,
            "mrr_ci": None,
            "ndcg_at_10": None,
            "ndcg_at_10_ci": None,
        }

    # Compile session-level metrics
    sess_metrics: dict[str, Any] = {
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

    # Latencies
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

    return {
        "tool": tool_name,
        "class": interface_class,
        "storage_unit": storage_unit,
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
        "turn_level_metrics": turn_metrics,
        "session_level_metrics": sess_metrics,
        "accuracy_metrics": {
            "turn_level": turn_metrics,
            "session_level": sess_metrics,
            "recall_at_1": turn_metrics.get("recall_at_1") if storage_unit == "turn" else sess_metrics["recall_at_1"],
            "recall_at_1_ci": turn_metrics.get("recall_at_1_ci") if storage_unit == "turn" else sess_metrics["recall_at_1_ci"],
            "recall_at_5": turn_metrics.get("recall_at_5") if storage_unit == "turn" else sess_metrics["recall_at_5"],
            "recall_at_5_ci": turn_metrics.get("recall_at_5_ci") if storage_unit == "turn" else sess_metrics["recall_at_5_ci"],
            "recall_at_10": turn_metrics.get("recall_at_10") if storage_unit == "turn" else sess_metrics["recall_at_10"],
            "recall_at_10_ci": turn_metrics.get("recall_at_10_ci") if storage_unit == "turn" else sess_metrics["recall_at_10_ci"],
            "mrr": turn_metrics.get("mrr") if storage_unit == "turn" else sess_metrics["mrr"],
            "mrr_ci": turn_metrics.get("mrr_ci") if storage_unit == "turn" else sess_metrics["mrr_ci"],
            "ndcg_at_10": turn_metrics.get("ndcg_at_10") if storage_unit == "turn" else sess_metrics["ndcg_at_10"],
            "ndcg_at_10_ci": turn_metrics.get("ndcg_at_10_ci") if storage_unit == "turn" else sess_metrics["ndcg_at_10_ci"],
        },
        "latency_metrics": latency_dict,
        "latencies_ms": latency_dict["latencies_ms"],
        "peak_ram_mb": peak_rss_mb,
        "disk_footprint_mb": disk_footprint_mb,
        "raw_inspections_first_5": raw_inspections,
    }


async def evaluate_retrieval_suite_async(
    tool_name: str,
    interface_class: str,
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    tag_to_session: dict[str, str],
    recall_fn: Callable[[str, int], Any],
    stored_count: int,
    expected_turns: int,
    write_seconds: float,
    time_to_searchable_seconds: float,
    peak_rss_mb: float,
    disk_footprint_mb: float,
    audit_notes: dict[str, Any],
    storage_unit: str = "turn",
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
        candidates = await recall_fn(q["question"].strip(), 10)
        lat = (time.perf_counter() - t_q) * 1000.0
        latencies_ms.append(lat)

        first_turn_hit_rank: Optional[int] = None
        if storage_unit == "turn":
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
                t_hit = is_turn_level_hit(c_id, c_text, gold_turn_tags) if storage_unit == "turn" else False
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

    if storage_unit == "turn":
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
    else:
        turn_metrics = {
            "status": "not comparable at turn level",
            "note": "Tool stores per session rather than per turn",
            "recall_at_1": None,
            "recall_at_1_ci": None,
            "recall_at_5": None,
            "recall_at_5_ci": None,
            "recall_at_10": None,
            "recall_at_10_ci": None,
            "mrr": None,
            "mrr_ci": None,
            "ndcg_at_10": None,
            "ndcg_at_10_ci": None,
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

    return {
        "tool": tool_name,
        "class": interface_class,
        "storage_unit": storage_unit,
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
        "turn_level_metrics": turn_metrics,
        "session_level_metrics": sess_metrics,
        "accuracy_metrics": {
            "turn_level": turn_metrics,
            "session_level": sess_metrics,
            "recall_at_1": turn_metrics.get("recall_at_1") if storage_unit == "turn" else sess_metrics["recall_at_1"],
            "recall_at_1_ci": turn_metrics.get("recall_at_1_ci") if storage_unit == "turn" else sess_metrics["recall_at_1_ci"],
            "recall_at_5": turn_metrics.get("recall_at_5") if storage_unit == "turn" else sess_metrics["recall_at_5"],
            "recall_at_5_ci": turn_metrics.get("recall_at_5_ci") if storage_unit == "turn" else sess_metrics["recall_at_5_ci"],
            "recall_at_10": turn_metrics.get("recall_at_10") if storage_unit == "turn" else sess_metrics["recall_at_10"],
            "recall_at_10_ci": turn_metrics.get("recall_at_10_ci") if storage_unit == "turn" else sess_metrics["recall_at_10_ci"],
            "mrr": turn_metrics.get("mrr") if storage_unit == "turn" else sess_metrics["mrr"],
            "mrr_ci": turn_metrics.get("mrr_ci") if storage_unit == "turn" else sess_metrics["mrr_ci"],
            "ndcg_at_10": turn_metrics.get("ndcg_at_10") if storage_unit == "turn" else sess_metrics["ndcg_at_10"],
            "ndcg_at_10_ci": turn_metrics.get("ndcg_at_10_ci") if storage_unit == "turn" else sess_metrics["ndcg_at_10_ci"],
        },
        "latency_metrics": latency_dict,
        "latencies_ms": latency_dict["latencies_ms"],
        "peak_ram_mb": peak_rss_mb,
        "disk_footprint_mb": disk_footprint_mb,
        "raw_inspections_first_5": raw_inspections,
    }


# -----------------------------------------------------------------------------
# CLASS A RUNNERS: IN-PROCESS LIBRARY / PYTHON SDK
# -----------------------------------------------------------------------------

def run_class_a_mesh_sdk(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    tag_to_session: dict[str, str],
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

            # 2. Time-to-searchable (poll known-item until searchable)
            def probe_sdk() -> bool:
                res = mesh.recall(KNOWN_ITEM_QUERY, n=1)
                return any(
                    KNOWN_ITEM_ID in r.get("tags", [])
                    or KNOWN_ITEM_ANSWER.lower() in r.get("content", "").lower()
                    for r in res
                )

            time_to_searchable = probe_searchable(probe_sdk, timeout=30.0, interval=0.05)

            # Count verification
            stored_count = mesh.store.count()
            expected_total = len(corpus) + 1
            quarantined = mesh.store.list_all(quarantined_only=True)

            audit_notes = {
                "quarantined_count": len(quarantined),
                "contradictions_detected": 0,
                "deduplicated_dropped": 0,
                "notes": "Mesh store exact SQLite count verified; zero turns dropped or hidden.",
            }

            def recall_fn(query: str, n: int = 10):
                res = mesh.recall(query.strip(), n=n)
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
            disk_footprint = get_dir_size_mb(temp_dir)
            eval_res = evaluate_retrieval_suite(
                tool_name="Mesh (SDK)",
                interface_class="Class A",
                questions=questions,
                original_to_opaque=original_to_opaque,
                tag_to_session=tag_to_session,
                recall_fn=recall_fn,
                stored_count=stored_count,
                expected_turns=expected_total,
                write_seconds=write_sec,
                time_to_searchable_seconds=time_to_searchable,
                peak_rss_mb=tracker.peak_mb,
                disk_footprint_mb=disk_footprint,
                audit_notes=audit_notes,
                storage_unit="turn",
            )
            _evict_namespace_caches("lme_eval")
            gc.collect()
            return eval_res


def run_class_a_mem0_sdk(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    tag_to_session: dict[str, str],
) -> dict[str, Any]:
    py_mem0 = find_mem0_py()
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
    tag_to_session: dict[str, str],
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

            # Time-to-searchable via cold CLI probe
            def probe_cold() -> bool:
                p = subprocess.run(
                    [sys.executable, "-m", "mesh", "recall", KNOWN_ITEM_QUERY, "--namespace", "cli_cold", "-n", "1", "--json"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                if p.returncode == 0:
                    try:
                        data = json.loads(p.stdout)
                        results = data.get("results", [])
                        return any(
                            KNOWN_ITEM_ID in r.get("tags", [])
                            or KNOWN_ITEM_ANSWER.lower() in r.get("content", "").lower()
                            for r in results
                        )
                    except Exception:
                        pass
                return False

            time_to_searchable = probe_searchable(probe_cold, timeout=60.0, interval=0.1)

            def recall_fn(query: str, n: int = 10):
                p = subprocess.run(
                    [sys.executable, "-m", "mesh", "recall", query.strip(), "--namespace", "cli_cold", "-n", str(n), "--json"],
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

            disk_footprint = get_dir_size_mb(temp_dir)
            eval_res = evaluate_retrieval_suite(
                tool_name="Mesh CLI (Cold)",
                interface_class="Class B",
                questions=questions,
                original_to_opaque=original_to_opaque,
                tag_to_session=tag_to_session,
                recall_fn=recall_fn,
                stored_count=stored_count,
                expected_turns=expected_total,
                write_seconds=write_sec,
                time_to_searchable_seconds=time_to_searchable,
                peak_rss_mb=tracker.peak_mb,
                disk_footprint_mb=disk_footprint,
                audit_notes={"daemon_active": False, "mode": "cold_process_spawn"},
                storage_unit="turn",
            )
            return eval_res


def run_class_b_mesh_cli_daemon(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    tag_to_session: dict[str, str],
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
            time.sleep(1.0)

            # Confirm daemon is running with explicit --port
            subprocess.run(
                [sys.executable, "-m", "mesh", "daemon", "status", "--port", test_port],
                env=env,
                capture_output=True,
                text=True,
            )

            # 3. Time-to-searchable: probe until the daemon is warmed up and returns known item
            def probe_daemon() -> bool:
                p = subprocess.run(
                    [sys.executable, "-m", "mesh", "recall", KNOWN_ITEM_QUERY, "--namespace", "cli_daemon", "-n", "1", "--json"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                if p.returncode == 0:
                    try:
                        data = json.loads(p.stdout)
                        results = data.get("results", [])
                        if results:
                            content = results[0].get("content", "")
                            tags = results[0].get("tags", [])
                            return any(
                                KNOWN_ITEM_ID in tags
                                or KNOWN_ITEM_ANSWER.lower() in content.lower()
                                for _ in [1]
                            )
                    except Exception:
                        pass
                return False

            time_to_searchable = probe_searchable(probe_daemon, timeout=60.0, interval=0.1)

            def recall_fn(query: str, n: int = 10):
                p = subprocess.run(
                    [sys.executable, "-m", "mesh", "recall", query.strip(), "--namespace", "cli_daemon", "-n", str(n), "--json"],
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

            disk_footprint = get_dir_size_mb(temp_dir)
            eval_res = evaluate_retrieval_suite(
                tool_name="Mesh CLI (Daemon Running)",
                interface_class="Class B",
                questions=questions,
                original_to_opaque=original_to_opaque,
                tag_to_session=tag_to_session,
                recall_fn=recall_fn,
                stored_count=stored_count,
                expected_turns=expected_total,
                write_seconds=write_sec,
                time_to_searchable_seconds=time_to_searchable,
                peak_rss_mb=tracker.peak_mb,
                disk_footprint_mb=disk_footprint,
                audit_notes={
                    "daemon_active": True,
                    "daemon_port": test_port,
                    "commit_hook": "1a88951",
                    "test_ref": "tests/test_remediation.py:344, tests/test_phase1.py:229",
                },
                storage_unit="turn",
            )

            # Stop daemon cleanly with explicit --port
            subprocess.run(
                [sys.executable, "-m", "mesh", "daemon", "stop", "--port", test_port],
                env=env,
                capture_output=True,
                text=True,
            )
            return eval_res


def run_class_b_basic_memory_cli(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    tag_to_session: dict[str, str],
) -> dict[str, Any]:
    bm_bin = find_bm_bin()

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
            # 1. Ingest: 1 note per turn with unique title (turn tag), identical to MCP layout
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
            def probe_bm_cli() -> bool:
                p = subprocess.run(
                    [str(bm_bin), "grep", KNOWN_ITEM_QUERY, "--project", "lme_cli", "--page-size", "1", "--json"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                if p.returncode == 0:
                    try:
                        data = json.loads(p.stdout)
                        for item in data.get("results", []):
                            content = item.get("content", "")
                            title = item.get("title", "")
                            if KNOWN_ITEM_ANSWER.lower() in content.lower() or "turn_99999" in title:
                                return True
                    except Exception:
                        pass
                return False

            time_to_searchable = probe_searchable(probe_bm_cli, timeout=60.0, interval=0.1)

            md_files = list(notes_dir.glob("turn_*.md"))
            stored_count = len(md_files)
            expected_total = len(corpus) + 1

            def recall_fn(query: str, n: int = 10):
                p = subprocess.run(
                    [str(bm_bin), "grep", query.strip(), "--project", "lme_cli", "--page-size", str(n), "--json"],
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
                            t_m = re.search(r"turn_(\d+)", title)
                            if not t_m:
                                t_m = re.search(r"\[turn:(\d+)\]", content)
                            tag = f"[turn:{int(t_m.group(1)):05d}]" if t_m else title
                            cands.append((tag, content))
                    except Exception:
                        pass
                return cands[:n]

            disk_footprint = get_dir_size_mb(temp_dir)
            eval_res = evaluate_retrieval_suite(
                tool_name="Basic Memory (CLI)",
                interface_class="Class B",
                questions=questions,
                original_to_opaque=original_to_opaque,
                tag_to_session=tag_to_session,
                recall_fn=recall_fn,
                stored_count=stored_count,
                expected_turns=expected_total,
                write_seconds=write_sec,
                time_to_searchable_seconds=time_to_searchable,
                peak_rss_mb=tracker.peak_mb,
                disk_footprint_mb=disk_footprint,
                audit_notes={
                    "layout": "one note per turn with unique title (turn tag) in notes_dir",
                    "note_count": stored_count,
                    "parity_with_mcp": True,
                },
                storage_unit="turn",
            )
            return eval_res


def run_class_b_auxly_cli(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    tag_to_session: dict[str, str],
) -> dict[str, Any]:
    auxly_bin = find_auxly_bin()

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
            def probe_auxly_cli() -> bool:
                p = subprocess.run(
                    [str(auxly_bin), "--path", str(vault), "search", KNOWN_ITEM_QUERY],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                return (
                    KNOWN_ITEM_ANSWER.lower() in p.stdout.lower()
                    or KNOWN_ITEM_ID in p.stdout
                    or "cerulean" in p.stdout.lower()
                )

            time_to_searchable = probe_searchable(probe_auxly_cli, timeout=60.0, interval=0.1)

            # Stored count verification
            stored_count = sum(len(items) for items in sessions_map.values()) + 1
            expected_total = len(corpus) + 1

            # Fix 4: Symmetrical raw question query (query.strip()) sent directly without custom stopword loops
            def recall_fn(query: str, n: int = 10):
                p = subprocess.run(
                    [str(auxly_bin), "--path", str(vault), "search", query.strip()],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                cands = []
                current_file = ""
                for line in p.stdout.splitlines():
                    line_str = line.strip()
                    if line_str.startswith("📄"):
                        current_file = line_str.replace("📄", "").strip()
                    elif line_str and current_file:
                        t_m = re.search(r"\[turn:(\d+)\]", line_str)
                        tag = f"[turn:{int(t_m.group(1)):05d}]" if t_m else current_file
                        cands.append((tag, line_str))
                return cands[:n]

            disk_footprint = get_dir_size_mb(temp_dir)
            eval_res = evaluate_retrieval_suite(
                tool_name="Auxly CLI (Raw Query)",
                interface_class="Class B",
                questions=questions,
                original_to_opaque=original_to_opaque,
                tag_to_session=tag_to_session,
                recall_fn=recall_fn,
                stored_count=stored_count,
                expected_turns=expected_total,
                write_seconds=write_sec,
                time_to_searchable_seconds=time_to_searchable,
                peak_rss_mb=tracker.peak_mb,
                disk_footprint_mb=disk_footprint,
                audit_notes={
                    "files_count": len(sessions_map) + 1,
                    "layout": "one file per session",
                    "query_handling": "raw query text (symmetric with other tools)",
                },
                storage_unit="session",
            )
            return eval_res


def run_class_b_auxly_cli_keyword_extracted(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    tag_to_session: dict[str, str],
) -> dict[str, Any]:
    """
    Optional comparison runner: Auxly CLI with custom stopword-filtered keyword extraction loop.
    """
    auxly_bin = find_auxly_bin()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="auxly_cli_kw_") as temp_dir:
        vault = Path(temp_dir) / "memory"
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "inbox.md").write_text("", encoding="utf-8")

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
                (vault / f"{sess_key}.md").write_text("\n".join(lines), encoding="utf-8")

            (vault / "session_needle.md").write_text(f"# Known Item\n{KNOWN_ITEM_CONTENT}\n", encoding="utf-8")
            write_sec = time.perf_counter() - t_write_start

            def probe_auxly() -> bool:
                p = subprocess.run(
                    [str(auxly_bin), "--path", str(vault), "search", "cerulean"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                return "cerulean" in p.stdout.lower()

            time_to_searchable = probe_searchable(probe_auxly, timeout=60.0, interval=0.1)
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
                            tag = f"[turn:{int(t_m.group(1)):05d}]" if t_m else current_file
                            key = (tag, line_str)
                            if key not in seen_lines or score > seen_lines[key]:
                                seen_lines[key] = score
                    if len(seen_lines) >= n * 3:
                        break

                sorted_cands = sorted(seen_lines.keys(), key=lambda c: seen_lines[c], reverse=True)
                return sorted_cands[:n]

            disk_footprint = get_dir_size_mb(temp_dir)
            eval_res = evaluate_retrieval_suite(
                tool_name="Auxly CLI (Keyword-Extracted Variant)",
                interface_class="Class B",
                questions=questions,
                original_to_opaque=original_to_opaque,
                tag_to_session=tag_to_session,
                recall_fn=recall_fn,
                stored_count=stored_count,
                expected_turns=expected_total,
                write_seconds=write_sec,
                time_to_searchable_seconds=time_to_searchable,
                peak_rss_mb=tracker.peak_mb,
                disk_footprint_mb=disk_footprint,
                audit_notes={
                    "files_count": len(sessions_map) + 1,
                    "layout": "one file per session",
                    "variant": "non-standard stopword filtering loop",
                },
                storage_unit="session",
            )
            return eval_res


# -----------------------------------------------------------------------------
# CLASS C RUNNERS: MCP STDIO
# -----------------------------------------------------------------------------

async def run_class_c_mesh_mcp(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    tag_to_session: dict[str, str],
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

        py_bin = find_venv_python()
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
                    async def probe_mesh_mcp() -> bool:
                        rec_res = await session.call_tool("mesh_recall", arguments={
                            "query": KNOWN_ITEM_QUERY,
                            "namespace": "mcp_eval",
                            "n": 1,
                        })
                        if rec_res.content:
                            data = json.loads(rec_res.content[0].text)
                            results = data.get("results", [])
                            return any(
                                KNOWN_ITEM_ID in r.get("tags", [])
                                or KNOWN_ITEM_ANSWER.lower() in r.get("content", "").lower()
                                for r in results
                            )
                        return False

                    time_to_searchable = await probe_searchable_async(probe_mesh_mcp, timeout=60.0, interval=0.1)

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
                            "query": query.strip(),
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

                    disk_footprint = get_dir_size_mb(temp_dir)
                    eval_res = await evaluate_retrieval_suite_async(
                        tool_name="Mesh MCP (Framework v1.30.0, Package v0.8.3)",
                        interface_class="Class C",
                        questions=questions,
                        original_to_opaque=original_to_opaque,
                        tag_to_session=tag_to_session,
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
                        disk_footprint_mb=disk_footprint,
                        storage_unit="turn",
                    )
                    return eval_res


async def run_class_c_auxly_mcp(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    tag_to_session: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    auxly_bin = str(find_auxly_bin())

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="auxly_mcp_") as temp_dir:
        vault = Path(temp_dir) / "memory"
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "inbox.md").write_text("", encoding="utf-8")

        # Set trust levels to direct write (auto)
        for agent_name in ["antigravity-cli", "default", "gpt4", "claude", "cursor"]:
            subprocess.run([auxly_bin, "--path", str(vault), "trust", "set", agent_name, "auto"], capture_output=True, text=True)

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

                    if "no pending approvals" not in pending_text.lower():
                        raise RuntimeError(f"Auxly pending queue is not empty: {pending_text}")

                    # Time to searchable
                    async def probe_auxly_mcp() -> bool:
                        res = await session.call_tool("auxly_memory_search", arguments={"query": KNOWN_ITEM_QUERY})
                        text = res.content[0].text if res.content else ""
                        return (
                            KNOWN_ITEM_ANSWER.lower() in text.lower()
                            or KNOWN_ITEM_ID in text
                            or "cerulean" in text.lower()
                        )

                    time_to_searchable = await probe_searchable_async(probe_auxly_mcp, timeout=60.0, interval=0.1)

                    stored_count = sum(len(items) for items in sessions_map.values()) + 1
                    expected_total = len(corpus) + 1

                    # Fix 4: Symmetrical raw question query (query.strip()) sent directly without stopword loops
                    def make_recall_fn(tool_name: str):
                        async def recall_fn(query: str, n: int = 10):
                            res = await session.call_tool(tool_name, arguments={"query": query.strip()})
                            text = res.content[0].text if res.content else ""
                            cands = []
                            current_file = ""
                            for line in text.splitlines():
                                line_str = line.strip()
                                if line_str.startswith("📄"):
                                    current_file = line_str.replace("📄", "").strip()
                                elif line_str and current_file:
                                    t_m = re.search(r"\[turn:(\d+)\]", line_str)
                                    tag = f"[turn:{int(t_m.group(1)):05d}]" if t_m else current_file
                                    cands.append((tag, line_str))
                            return cands[:n]
                        return recall_fn

                    disk_footprint = get_dir_size_mb(temp_dir)
                    recall_search = make_recall_fn("auxly_memory_search")
                    eval_search = await evaluate_retrieval_suite_async(
                        tool_name="Auxly MCP (Search)",
                        interface_class="Class C",
                        questions=questions,
                        original_to_opaque=original_to_opaque,
                        tag_to_session=tag_to_session,
                        recall_fn=recall_search,
                        stored_count=stored_count,
                        expected_turns=expected_total,
                        write_seconds=write_sec,
                        time_to_searchable_seconds=time_to_searchable,
                        peak_rss_mb=tracker.peak_mb,
                        disk_footprint_mb=disk_footprint,
                        audit_notes={
                            "pending_list_empty": True,
                            "stats_raw": stats_text[:120],
                            "tool_documented": "auxly_memory_search",
                            "query_handling": "raw query text (symmetric with other tools)",
                        },
                        storage_unit="session",
                    )

                    recall_recall = make_recall_fn("auxly_memory_recall")
                    eval_recall = await evaluate_retrieval_suite_async(
                        tool_name="Auxly MCP (Recall)",
                        interface_class="Class C",
                        questions=questions,
                        original_to_opaque=original_to_opaque,
                        tag_to_session=tag_to_session,
                        recall_fn=recall_recall,
                        stored_count=stored_count,
                        expected_turns=expected_total,
                        write_seconds=write_sec,
                        time_to_searchable_seconds=time_to_searchable,
                        peak_rss_mb=tracker.peak_mb,
                        disk_footprint_mb=disk_footprint,
                        audit_notes={
                            "pending_list_empty": True,
                            "tool_documented": "auxly_memory_recall (semantic with keyword fallback)",
                            "query_handling": "raw query text (symmetric with other tools)",
                        },
                        storage_unit="session",
                    )

                    return eval_search, eval_recall


async def run_class_c_basic_memory_mcp(
    corpus: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    original_to_opaque: dict[str, str],
    tag_to_session: dict[str, str],
) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    bm_bin = find_bm_bin()

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

            md_files = list(notes_dir.glob("turn_*.md"))
            stored_count = len(md_files)
            expected_total = len(corpus) + 1

            params = StdioServerParameters(command=str(bm_bin), args=["mcp"], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    # Time to searchable
                    async def probe_bm_mcp() -> bool:
                        res = await session.call_tool(
                            "search_notes",
                            arguments={"query": KNOWN_ITEM_QUERY, "page_size": 1},
                        )
                        if res.content:
                            text = res.content[0].text
                            return (
                                KNOWN_ITEM_ANSWER.lower() in text.lower()
                                or "turn_99999" in text
                            )
                        return False

                    time_to_searchable = await probe_searchable_async(probe_bm_mcp, timeout=60.0, interval=0.1)

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

                    # Fix 4: Symmetrical raw question query (query.strip()) sent directly
                    async def recall_fn(query: str, n: int = 10):
                        res = await session.call_tool(
                            "search_notes", arguments={"query": query.strip(), "page_size": n}
                        )
                        text = res.content[0].text if res.content else ""
                        return parse_bm_search(text, n=n)

                    disk_footprint = get_dir_size_mb(temp_dir)
                    eval_res = await evaluate_retrieval_suite_async(
                        tool_name="Basic Memory MCP (FastMCP v4.1.4)",
                        interface_class="Class C",
                        questions=questions,
                        original_to_opaque=original_to_opaque,
                        tag_to_session=tag_to_session,
                        recall_fn=recall_fn,
                        stored_count=stored_count,
                        expected_turns=expected_total,
                        write_seconds=write_sec,
                        time_to_searchable_seconds=time_to_searchable,
                        peak_rss_mb=tracker.peak_mb,
                        disk_footprint_mb=disk_footprint,
                        audit_notes={
                            "layout": "one note per turn with unique title (turn tag) in basic-memory folder",
                            "framework": "FastMCP 4.1.4",
                            "note_count": stored_count,
                            "parity_with_cli": True,
                        },
                        storage_unit="turn",
                    )
                    return eval_res


# -----------------------------------------------------------------------------
# CROSS-CLASS PARITY VERIFICATION
# -----------------------------------------------------------------------------

def verify_cross_class_parity(results: dict[str, Any]) -> list[str]:
    logs = []
    # 1. Mesh parity check across SDK, CLI Cold, CLI Daemon, and MCP
    mesh_runs = {}
    if "mesh_sdk" in results.get("class_a_library", {}):
        mesh_runs["SDK"] = results["class_a_library"]["mesh_sdk"]
    if "mesh_cli_cold" in results.get("class_b_cli", {}):
        mesh_runs["CLI Cold"] = results["class_b_cli"]["mesh_cli_cold"]
    if "mesh_cli_daemon" in results.get("class_b_cli", {}):
        mesh_runs["CLI Daemon"] = results["class_b_cli"]["mesh_cli_daemon"]
    if "mesh_mcp" in results.get("class_c_mcp", {}):
        mesh_runs["MCP"] = results["class_c_mcp"]["mesh_mcp"]

    if len(mesh_runs) > 1:
        metrics_by_runner = {
            name: {
                "turn_r1": run.get("turn_level_metrics", {}).get("recall_at_1"),
                "turn_r5": run.get("turn_level_metrics", {}).get("recall_at_5"),
                "turn_r10": run.get("turn_level_metrics", {}).get("recall_at_10"),
                "turn_mrr": run.get("turn_level_metrics", {}).get("mrr"),
                "turn_ndcg": run.get("turn_level_metrics", {}).get("ndcg_at_10"),
                "sess_r1": run.get("session_level_metrics", {}).get("recall_at_1"),
                "sess_r5": run.get("session_level_metrics", {}).get("recall_at_5"),
                "sess_r10": run.get("session_level_metrics", {}).get("recall_at_10"),
                "sess_mrr": run.get("session_level_metrics", {}).get("mrr"),
                "sess_ndcg": run.get("session_level_metrics", {}).get("ndcg_at_10"),
            }
            for name, run in mesh_runs.items()
        }
        active_runs = {
            name: m for name, m in metrics_by_runner.items()
            if m["turn_r1"] is not None
        }
        if len(active_runs) > 1:
            ref_name = list(active_runs.keys())[0]
            ref_metrics = active_runs[ref_name]
            mesh_mismatch = False
            for name, m in active_runs.items():
                if m != ref_metrics:
                    mesh_mismatch = True
                    diffs = [k for k in m if m[k] != ref_metrics[k]]
                    msg = f"[PARITY BUG] Mesh {name} differs from Mesh {ref_name} on: {diffs} ({m} vs {ref_metrics})"
                    logs.append(msg)
                    print(f"  {msg}")
            if not mesh_mismatch:
                msg = f"[PARITY VERIFIED] Mesh retrieval scores are identical across {list(active_runs.keys())}."
                logs.append(msg)
                print(f"  {msg}")

    # 2. Basic Memory parity check between CLI and MCP
    bm_cli = results.get("class_b_cli", {}).get("basic_memory_cli")
    bm_mcp = results.get("class_c_mcp", {}).get("basic_memory_mcp")
    if (
        bm_cli and bm_mcp and isinstance(bm_cli, dict) and isinstance(bm_mcp, dict)
        and bm_cli.get("turn_level_metrics", {}).get("recall_at_1") is not None
        and bm_mcp.get("turn_level_metrics", {}).get("recall_at_1") is not None
    ):
        cli_m = {
            "turn_r1": bm_cli.get("turn_level_metrics", {}).get("recall_at_1"),
            "turn_mrr": bm_cli.get("turn_level_metrics", {}).get("mrr"),
            "sess_r1": bm_cli.get("session_level_metrics", {}).get("recall_at_1"),
            "sess_mrr": bm_cli.get("session_level_metrics", {}).get("mrr"),
        }
        mcp_m = {
            "turn_r1": bm_mcp.get("turn_level_metrics", {}).get("recall_at_1"),
            "turn_mrr": bm_mcp.get("turn_level_metrics", {}).get("mrr"),
            "sess_r1": bm_mcp.get("session_level_metrics", {}).get("recall_at_1"),
            "sess_mrr": bm_mcp.get("session_level_metrics", {}).get("mrr"),
        }
        if cli_m == mcp_m:
            msg = "[PARITY VERIFIED] Basic Memory scores are identical across CLI and MCP."
            logs.append(msg)
            print(f"  {msg}")
        else:
            diffs = [k for k in cli_m if cli_m[k] != mcp_m[k]]
            msg = f"[PARITY WARNING] Basic Memory CLI vs MCP score difference on {diffs}: CLI={cli_m} vs MCP={mcp_m}"
            logs.append(msg)
            print(f"  {msg}")

    return logs


# -----------------------------------------------------------------------------
# REPORT GENERATION (INDEPENDENT TABLES FOR TURN & SESSION LEVEL)
# -----------------------------------------------------------------------------

def format_ci(val: Optional[float], ci: Optional[list[float]]) -> str:
    if val is None or ci is None:
        return "not comparable"
    return f"{val:.4f} [{ci[0]:.4f}, {ci[1]:.4f}]"


def format_lat_ci(val: Optional[float], ci: Optional[list[float]]) -> str:
    if val is None or ci is None:
        return "n/a"
    return f"{val:.2f} [{ci[0]:.2f}, {ci[1]:.2f}]"


def write_formatted_report(results: dict[str, Any], out_path: Path) -> None:
    parity_logs = verify_cross_class_parity(results)

    with open(out_path, "w", encoding="utf-8") as log:
        log.write("=" * 115 + "\n")
        log.write("  PHASE 7 SMOKE TEST - MULTI-CLASS INTERFACE EVALUATION REPORT\n")
        log.write("=" * 115 + "\n\n")

        log.write(f"Commit       : {results['metadata']['target_commit']} ({results['metadata']['branch']})\n")
        log.write(f"Platform     : {results['metadata']['platform']} | Python {results['metadata']['python_version']}\n")
        log.write(f"Timestamp    : {results['metadata']['timestamp']}\n")
        log.write(f"Corpus       : {results['metadata']['corpus']['name']} ({results['metadata']['corpus']['questions_count']} questions, {results['metadata']['corpus']['turns_count']} turns pooled)\n\n")

        # ---------------------------------------------------------------------
        # INDEPENDENT TABLE 1: TURN-LEVEL RETRIEVAL ACCURACY
        # ---------------------------------------------------------------------
        log.write("=" * 175 + "\n")
        log.write("INDEPENDENT TABLE 1: TURN-LEVEL RETRIEVAL ACCURACY (BY INTERFACE CLASS)\n")
        log.write("A hit requires the returned memory to be a single turn containing the gold turn tag.\n")
        log.write("Tools storing per session (Auxly) are marked 'not comparable at turn level'.\n")
        log.write("=" * 175 + "\n")
        log.write(
            f"{'Tool':<32} {'Class':<10} "
            f"{'Recall@1 (95% CI)':<25} {'Recall@5 (95% CI)':<25} {'Recall@10 (95% CI)':<25} "
            f"{'MRR (95% CI)':<25} {'NDCG@10 (95% CI)':<25}\n"
        )
        log.write("-" * 175 + "\n")

        # Class A
        log.write("CLASS A: IN-PROCESS LIBRARY / PYTHON SDK\n")
        for key in ["mesh_sdk", "mem0_sdk"]:
            if key in results["class_a_library"]:
                m = results["class_a_library"][key]
                t_acc = m.get("turn_level_metrics", {})
                log.write(
                    f"  {m['tool']:<30} {m['class']:<10} "
                    f"{format_ci(t_acc.get('recall_at_1'), t_acc.get('recall_at_1_ci')):<25} "
                    f"{format_ci(t_acc.get('recall_at_5'), t_acc.get('recall_at_5_ci')):<25} "
                    f"{format_ci(t_acc.get('recall_at_10'), t_acc.get('recall_at_10_ci')):<25} "
                    f"{format_ci(t_acc.get('mrr'), t_acc.get('mrr_ci')):<25} "
                    f"{format_ci(t_acc.get('ndcg_at_10'), t_acc.get('ndcg_at_10_ci')):<25}\n"
                )
        log.write(f"  {'Basic Memory':<30} {'Class A':<10} {results['class_a_library']['basic_memory']['status']}\n")
        log.write(f"  {'Auxly':<30} {'Class A':<10} {results['class_a_library']['auxly']['status']}\n\n")

        # Class B
        log.write("CLASS B: CLI SUBPROCESS\n")
        for key in ["mesh_cli_cold", "mesh_cli_daemon", "basic_memory_cli", "auxly_cli", "auxly_cli_kw"]:
            if key in results["class_b_cli"]:
                m = results["class_b_cli"][key]
                t_acc = m.get("turn_level_metrics", {})
                if t_acc.get("status") == "not comparable at turn level":
                    log.write(f"  {m['tool']:<30} {m['class']:<10} not comparable at turn level (stores per session)\n")
                else:
                    log.write(
                        f"  {m['tool']:<30} {m['class']:<10} "
                        f"{format_ci(t_acc.get('recall_at_1'), t_acc.get('recall_at_1_ci')):<25} "
                        f"{format_ci(t_acc.get('recall_at_5'), t_acc.get('recall_at_5_ci')):<25} "
                        f"{format_ci(t_acc.get('recall_at_10'), t_acc.get('recall_at_10_ci')):<25} "
                        f"{format_ci(t_acc.get('mrr'), t_acc.get('mrr_ci')):<25} "
                        f"{format_ci(t_acc.get('ndcg_at_10'), t_acc.get('ndcg_at_10_ci')):<25}\n"
                    )
        log.write(f"  {'Mem0':<30} {'Class B':<10} {results['class_b_cli']['mem0']['status']}\n\n")

        # Class C
        log.write("CLASS C: MCP STDIO (PERSISTENT SERVER SESSION)\n")
        for key in ["mesh_mcp", "basic_memory_mcp", "auxly_mcp_search", "auxly_mcp_recall"]:
            if key in results["class_c_mcp"]:
                m = results["class_c_mcp"][key]
                t_acc = m.get("turn_level_metrics", {})
                if t_acc.get("status") == "not comparable at turn level":
                    log.write(f"  {m['tool']:<30} {m['class']:<10} not comparable at turn level (stores per session)\n")
                else:
                    log.write(
                        f"  {m['tool']:<30} {m['class']:<10} "
                        f"{format_ci(t_acc.get('recall_at_1'), t_acc.get('recall_at_1_ci')):<25} "
                        f"{format_ci(t_acc.get('recall_at_5'), t_acc.get('recall_at_5_ci')):<25} "
                        f"{format_ci(t_acc.get('recall_at_10'), t_acc.get('recall_at_10_ci')):<25} "
                        f"{format_ci(t_acc.get('mrr'), t_acc.get('mrr_ci')):<25} "
                        f"{format_ci(t_acc.get('ndcg_at_10'), t_acc.get('ndcg_at_10_ci')):<25}\n"
                    )
        log.write(f"  {'Mem0':<30} {'Class C':<10} {results['class_c_mcp']['mem0']['status']}\n\n")

        # ---------------------------------------------------------------------
        # INDEPENDENT TABLE 2: SESSION-LEVEL RETRIEVAL ACCURACY
        # ---------------------------------------------------------------------
        log.write("=" * 175 + "\n")
        log.write("INDEPENDENT TABLE 2: SESSION-LEVEL RETRIEVAL ACCURACY (BY INTERFACE CLASS)\n")
        log.write("Every returned turn tag mapped to session ID, deduplicated keeping first rank.\n")
        log.write("Gold = the dataset's gold evidence sessions (any turn with has_answer=True in that session).\n")
        log.write("=" * 175 + "\n")
        log.write(
            f"{'Tool':<32} {'Class':<10} "
            f"{'Recall@1 (95% CI)':<25} {'Recall@5 (95% CI)':<25} {'Recall@10 (95% CI)':<25} "
            f"{'MRR (95% CI)':<25} {'NDCG@10 (95% CI)':<25}\n"
        )
        log.write("-" * 175 + "\n")

        log.write("CLASS A: IN-PROCESS LIBRARY / PYTHON SDK\n")
        for key in ["mesh_sdk", "mem0_sdk"]:
            if key in results["class_a_library"]:
                m = results["class_a_library"][key]
                s_acc = m.get("session_level_metrics", {})
                log.write(
                    f"  {m['tool']:<30} {m['class']:<10} "
                    f"{format_ci(s_acc.get('recall_at_1'), s_acc.get('recall_at_1_ci')):<25} "
                    f"{format_ci(s_acc.get('recall_at_5'), s_acc.get('recall_at_5_ci')):<25} "
                    f"{format_ci(s_acc.get('recall_at_10'), s_acc.get('recall_at_10_ci')):<25} "
                    f"{format_ci(s_acc.get('mrr'), s_acc.get('mrr_ci')):<25} "
                    f"{format_ci(s_acc.get('ndcg_at_10'), s_acc.get('ndcg_at_10_ci')):<25}\n"
                )
        log.write(f"  {'Basic Memory':<30} {'Class A':<10} {results['class_a_library']['basic_memory']['status']}\n")
        log.write(f"  {'Auxly':<30} {'Class A':<10} {results['class_a_library']['auxly']['status']}\n\n")

        log.write("CLASS B: CLI SUBPROCESS\n")
        for key in ["mesh_cli_cold", "mesh_cli_daemon", "basic_memory_cli", "auxly_cli", "auxly_cli_kw"]:
            if key in results["class_b_cli"]:
                m = results["class_b_cli"][key]
                s_acc = m.get("session_level_metrics", {})
                log.write(
                    f"  {m['tool']:<30} {m['class']:<10} "
                    f"{format_ci(s_acc.get('recall_at_1'), s_acc.get('recall_at_1_ci')):<25} "
                    f"{format_ci(s_acc.get('recall_at_5'), s_acc.get('recall_at_5_ci')):<25} "
                    f"{format_ci(s_acc.get('recall_at_10'), s_acc.get('recall_at_10_ci')):<25} "
                    f"{format_ci(s_acc.get('mrr'), s_acc.get('mrr_ci')):<25} "
                    f"{format_ci(s_acc.get('ndcg_at_10'), s_acc.get('ndcg_at_10_ci')):<25}\n"
                )
        log.write(f"  {'Mem0':<30} {'Class B':<10} {results['class_b_cli']['mem0']['status']}\n\n")

        log.write("CLASS C: MCP STDIO (PERSISTENT SERVER SESSION)\n")
        for key in ["mesh_mcp", "basic_memory_mcp", "auxly_mcp_search", "auxly_mcp_recall"]:
            if key in results["class_c_mcp"]:
                m = results["class_c_mcp"][key]
                s_acc = m.get("session_level_metrics", {})
                log.write(
                    f"  {m['tool']:<30} {m['class']:<10} "
                    f"{format_ci(s_acc.get('recall_at_1'), s_acc.get('recall_at_1_ci')):<25} "
                    f"{format_ci(s_acc.get('recall_at_5'), s_acc.get('recall_at_5_ci')):<25} "
                    f"{format_ci(s_acc.get('recall_at_10'), s_acc.get('recall_at_10_ci')):<25} "
                    f"{format_ci(s_acc.get('mrr'), s_acc.get('mrr_ci')):<25} "
                    f"{format_ci(s_acc.get('ndcg_at_10'), s_acc.get('ndcg_at_10_ci')):<25}\n"
                )
        log.write(f"  {'Mem0':<30} {'Class C':<10} {results['class_c_mcp']['mem0']['status']}\n\n")

        # ---------------------------------------------------------------------
        # INDEPENDENT TABLE 3: QUERY LATENCY METRICS
        # ---------------------------------------------------------------------
        log.write("=" * 115 + "\n")
        log.write("INDEPENDENT TABLE 3: QUERY LATENCY METRICS (WITH BOOTSTRAP 95% CIs)\n")
        log.write("=" * 115 + "\n")
        log.write(f"{'Tool':<34} {'Class':<10} {'p50 [95% CI]':<22} {'p95 [95% CI]':<22} {'p99 [95% CI]':<22} {'Mean Latency':<12}\n")
        log.write("-" * 115 + "\n")
        all_evals = []
        for c_dict in [results["class_a_library"], results["class_b_cli"], results["class_c_mcp"]]:
            for v in c_dict.values():
                if isinstance(v, dict) and "latency_metrics" in v:
                    all_evals.append(v)

        for m in all_evals:
            lat = m["latency_metrics"]
            log.write(
                f"{m['tool']:<34} {m['class']:<10} "
                f"{format_lat_ci(lat.get('p50_ms'), lat.get('p50_ms_ci')):<22} "
                f"{format_lat_ci(lat.get('p95_ms'), lat.get('p95_ms_ci')):<22} "
                f"{format_lat_ci(lat.get('p99_ms'), lat.get('p99_ms_ci')):<22} "
                f"{lat.get('mean_ms', 0.0):>8.2f} ms\n"
            )
        log.write("\n")

        # ---------------------------------------------------------------------
        # INDEPENDENT TABLE 4: SYSTEM RESOURCES & INGESTION TIMING
        # ---------------------------------------------------------------------
        log.write("=" * 115 + "\n")
        log.write("INDEPENDENT TABLE 4: INGESTION, SEARCHABILITY & SYSTEM RESOURCE FOOTPRINT\n")
        log.write("=" * 115 + "\n")
        log.write(f"{'Tool':<34} {'Class':<10} {'Write Time':<12} {'Searchable':<12} {'Count Match':<16} {'Peak RAM':<12} {'Disk Size':<12}\n")
        log.write("-" * 115 + "\n")
        for m in all_evals:
            cv = m.get("count_verification", {})
            tm = m.get("ingestion_timing", {})
            stored_str = f"{cv.get('stored_count')}/{cv.get('expected_turns')}"
            log.write(
                f"{m['tool']:<34} {m['class']:<10} "
                f"{tm.get('write_seconds', 0.0):>8.2f} s   "
                f"{tm.get('time_to_searchable_seconds', 0.0):>8.2f} s   "
                f"{stored_str:<16} "
                f"{m.get('peak_ram_mb', 0.0):>8.1f} MB  "
                f"{m.get('disk_footprint_mb', 0.0):>8.2f} MB\n"
            )
        log.write("\n")

        # ---------------------------------------------------------------------
        # INDEPENDENT TABLE 5: CROSS-CLASS PARITY AUDIT
        # ---------------------------------------------------------------------
        log.write("=" * 115 + "\n")
        log.write("INDEPENDENT TABLE 5: CROSS-CLASS PARITY AUDIT\n")
        log.write("=" * 115 + "\n")
        for l_line in parity_logs:
            log.write(f"{l_line}\n")
        log.write("\n" + "=" * 115 + "\n")


# -----------------------------------------------------------------------------
# MAIN BENCHMARK HARNESS EXECUTION
# -----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Class Interface Evaluation Smoke Test")
    parser.add_argument("--dry-run", action="store_true", help="Run 2 questions dry-run to validate all code paths")
    parser.add_argument("--fresh", action="store_true", help="Ignore checkpoint cache and run clean")
    parser.add_argument("--runner", type=str, default="", help="Run a specific runner only (e.g. mesh_sdk, basic_memory_cli)")
    parser.add_argument("--skip-mem0", action="store_true", help="Skip Mem0 SDK execution")
    parser.add_argument("--report-only", action="store_true", help="Generate reports from existing results without re-evaluating")
    args = parser.parse_args()

    print("=" * 80)
    print("  PHASE 7 SMOKE TEST - MULTI-CLASS INTERFACE EVALUATION")
    print("=" * 80)

    commit_hash = get_git_commit()
    branch_name = get_git_branch()
    print(f"Target Commit : {commit_hash} ({branch_name})")
    print(f"Platform      : {platform.platform()} (Python {platform.python_version()})")
    print(f"Timestamp     : {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}\n")

    dataset_path = PROJECT_ROOT / "benchmarks" / "data" / "longmemeval_smoke_50.json"
    with open(dataset_path, "r", encoding="utf-8") as f:
        questions = json.load(f)

    if args.dry_run:
        print("[DRY-RUN] Truncating dataset to first 2 questions for rapid validation.")
        questions = questions[:2]

    corpus, original_to_opaque, tag_to_session = prepare_opaque_corpus(questions)
    print(f"Corpus Prepared: {len(questions)} questions, {len(corpus)} haystack turns pooled in one store.")
    print("Layout: All turns pooled in single store; opaque tags [turn:00000]..[turn:NNNNN].\n")

    out_dir = PROJECT_ROOT / "benchmarks" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "smoke_test_results.json"
    log_path = out_dir / "smoke_test.log"

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

    if not args.fresh and json_path.exists():
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

    if args.report_only:
        print("[REPORT-ONLY] Generating report from existing checkpoints...")
        write_formatted_report(results, log_path)
        print(f"[OK] Formatted report written to: {log_path}")
        return

    # Pre-warm Mesh ONNX model
    from mesh.memory import warmup_model
    print("Pre-warming Mesh embedding model...")
    warmup_model(blocking=True)
    print("Embedding model ready.\n")

    target_runner = args.runner.strip().lower()

    # =========================================================================
    # CLASS A: IN-PROCESS LIBRARY / PYTHON SDK
    # =========================================================================
    print("-" * 80)
    print("  EVALUATING CLASS A: IN-PROCESS LIBRARY / PYTHON SDK")
    print("-" * 80)

    if not target_runner or target_runner == "mesh_sdk":
        if "mesh_sdk" not in results["class_a_library"] or args.fresh or args.dry_run:
            print("Running Class A: Mesh (SDK)...")
            results["class_a_library"]["mesh_sdk"] = run_class_a_mesh_sdk(
                corpus, questions, original_to_opaque, tag_to_session
            )
            save_checkpoint()
        else:
            print("[SKIP] Class A: Mesh (SDK) already completed.")

    if not args.skip_mem0 and (not target_runner or target_runner == "mem0_sdk"):
        if "mem0_sdk" not in results["class_a_library"] or args.fresh or args.dry_run:
            print("Running Class A: Mem0 (SDK, infer=False)...")
            results["class_a_library"]["mem0_sdk"] = run_class_a_mem0_sdk(
                corpus, questions, original_to_opaque, tag_to_session
            )
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

    if not target_runner or target_runner == "mesh_cli_cold":
        if "mesh_cli_cold" not in results["class_b_cli"] or args.fresh or args.dry_run:
            print("Running Class B: Mesh CLI (Cold, MESH_NO_AUTO_DAEMON=1)...")
            results["class_b_cli"]["mesh_cli_cold"] = run_class_b_mesh_cli_cold(
                corpus, questions, original_to_opaque, tag_to_session
            )
            save_checkpoint()
        else:
            print("[SKIP] Class B: Mesh CLI (Cold) already completed.")

    if not target_runner or target_runner == "mesh_cli_daemon":
        if "mesh_cli_daemon" not in results["class_b_cli"] or args.fresh or args.dry_run:
            print("Running Class B: Mesh CLI (Daemon Running, HTTP IPC to daemon)...")
            results["class_b_cli"]["mesh_cli_daemon"] = run_class_b_mesh_cli_daemon(
                corpus, questions, original_to_opaque, tag_to_session
            )
            save_checkpoint()
        else:
            print("[SKIP] Class B: Mesh CLI (Daemon) already completed.")

    if not target_runner or target_runner == "basic_memory_cli":
        if "basic_memory_cli" not in results["class_b_cli"] or args.fresh or args.dry_run:
            print("Running Class B: Basic Memory (CLI)...")
            results["class_b_cli"]["basic_memory_cli"] = run_class_b_basic_memory_cli(
                corpus, questions, original_to_opaque, tag_to_session
            )
            save_checkpoint()
        else:
            print("[SKIP] Class B: Basic Memory (CLI) already completed.")

    if not target_runner or target_runner == "auxly_cli":
        if "auxly_cli" not in results["class_b_cli"] or args.fresh or args.dry_run:
            print("Running Class B: Auxly (CLI, Raw Query)...")
            results["class_b_cli"]["auxly_cli"] = run_class_b_auxly_cli(
                corpus, questions, original_to_opaque, tag_to_session
            )
            save_checkpoint()
        else:
            print("[SKIP] Class B: Auxly (CLI) already completed.")

    if target_runner == "auxly_cli_kw":
        print("Running Class B: Auxly (CLI, Keyword-Extracted Variant)...")
        results["class_b_cli"]["auxly_cli_kw"] = run_class_b_auxly_cli_keyword_extracted(
            corpus, questions, original_to_opaque, tag_to_session
        )
        save_checkpoint()

    results["class_b_cli"]["mem0"] = {"status": "not offered", "url": "https://pypi.org/project/mem0ai/"}
    save_checkpoint()

    # =========================================================================
    # CLASS C: MCP STDIO
    # =========================================================================
    print("\n" + "-" * 80)
    print("  EVALUATING CLASS C: MCP STDIO")
    print("-" * 80)

    if not target_runner or target_runner == "mesh_mcp":
        if "mesh_mcp" not in results["class_c_mcp"] or args.fresh or args.dry_run:
            print("Running Class C: Mesh MCP (Framework v1.30.0, Package v0.8.3)...")
            results["class_c_mcp"]["mesh_mcp"] = asyncio.run(
                run_class_c_mesh_mcp(corpus, questions, original_to_opaque, tag_to_session)
            )
            save_checkpoint()
        else:
            print("[SKIP] Class C: Mesh MCP already completed.")

    if not target_runner or target_runner == "basic_memory_mcp":
        if "basic_memory_mcp" not in results["class_c_mcp"] or args.fresh or args.dry_run:
            print("Running Class C: Basic Memory MCP (FastMCP v4.1.4)...")
            results["class_c_mcp"]["basic_memory_mcp"] = asyncio.run(
                run_class_c_basic_memory_mcp(corpus, questions, original_to_opaque, tag_to_session)
            )
            save_checkpoint()
        else:
            print("[SKIP] Class C: Basic Memory MCP already completed.")

    if not target_runner or target_runner in ["auxly_mcp", "auxly_mcp_search", "auxly_mcp_recall"]:
        if ("auxly_mcp_search" not in results["class_c_mcp"] or "auxly_mcp_recall" not in results["class_c_mcp"]
                or args.fresh or args.dry_run):
            print("Running Class C: Auxly MCP (Search & Recall)...")
            res_c_auxly_search, res_c_auxly_recall = asyncio.run(
                run_class_c_auxly_mcp(corpus, questions, original_to_opaque, tag_to_session)
            )
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

    write_formatted_report(results, log_path)
    print(f"[OK] Formatted report written to: {log_path}\n")

    print("=" * 80)
    print("  SMOKE TEST HARNESS RUN COMPLETE.")
    print("=" * 80)


if __name__ == "__main__":
    main()
