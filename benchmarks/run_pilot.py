"""
benchmarks/run_pilot.py — Official LongMemEval-S 20-Question Pilot Evaluation Runner.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import gc
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import psutil
except ImportError:
    psutil = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.sleep_guard import (
    DualTimer,
    ExecutionStateGuard,
    assert_power_and_sleep_readiness,
    assert_system_exclusive,
    cleanup_leftover_processes,
    get_power_status,
    query_kernel_power_events,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mesh_pilot")

os.environ["MESH_MAX_CONTENT_LENGTH"] = "131072"

PILOT_DATASET_PATH = PROJECT_ROOT / "benchmarks" / "data" / "longmemeval_pilot_20.json"
RESULTS_DIR = PROJECT_ROOT / "benchmarks" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

KNOWN_ITEM_QUERY = "cerulean observatory celestial frequency"
KNOWN_ITEM_TAG = "[turn:00000]"
KNOWN_ITEM_CONTENT = f"{KNOWN_ITEM_TAG} The cerulean observatory celestial frequency atop Mount Horizon recorded an anomalous 432.85 MHz on stellar quadrant nine."
KNOWN_ITEM_ANSWER = "432.85 MHz"

LONG_TURN_QUERY = "cobalt quantum telescope observed astronomical phenomena"
LONG_TURN_TAG = "[turn:00001]"
LONG_TURN_CONTENT = f"{LONG_TURN_TAG} " + ("The cobalt quantum telescope observed astronomical phenomena in deep space. " * 45)

STOP_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are",
    "aren't", "as", "at", "be", "because", "been", "before", "being", "below", "between", "both",
    "but", "by", "can", "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does",
    "doesn't", "doing", "don't", "down", "during", "each", "few", "for", "from", "further", "had",
    "hadn't", "has", "hasn't", "have", "haven't", "having", "he", "he'd", "he'll", "he's", "her",
    "here", "here's", "hers", "herself", "him", "himself", "his", "how", "how's", "i", "i'd",
    "i'll", "i'm", "i've", "if", "in", "into", "is", "isn't", "it", "it's", "its", "itself", "let's",
    "me", "more", "most", "mustn't", "my", "myself", "no", "nor", "not", "of", "off", "on", "once",
    "only", "or", "other", "ought", "our", "ours", "ourselves", "out", "over", "own", "same", "shan't",
    "she", "she'd", "she'll", "she's", "should", "shouldn't", "so", "some", "such", "than", "that",
    "that's", "the", "their", "theirs", "them", "themselves", "then", "there", "there's", "these",
    "they", "they'd", "they'll", "they're", "they've", "this", "those", "through", "to", "too",
    "under", "until", "up", "very", "was", "wasn't", "we", "we'd", "we'll", "we're", "we've", "were",
    "weren't", "what", "what's", "when", "when's", "where", "where's", "which", "while", "who",
    "who's", "whom", "why", "why's", "with", "won't", "would", "wouldn't", "you", "you'd", "you'll",
    "you're", "you've", "your", "yours", "yourself", "yourselves"
}


def extract_keywords(query: str) -> str:
    """Documented deterministic keyword extraction applied identically to all tools."""
    cleaned = re.sub(r"[^\w\s]", " ", query.lower())
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    keywords = [t for t in tokens if t not in STOP_WORDS]
    return " ".join(keywords) if keywords else query.strip()


def get_harness_baseline_rss_mb() -> float:
    if not psutil:
        return 0.0
    try:
        return round(psutil.Process().memory_info().rss / (1024 * 1024), 2)
    except Exception:
        return 0.0


def get_process_tree_rss_mb(include_parent: bool = False, extra_pids: Optional[list[int]] = None) -> float:
    if not psutil:
        return 0.0
    try:
        parent = psutil.Process()
        total_rss = parent.memory_info().rss if include_parent else 0
        for child in parent.children(recursive=True):
            try:
                total_rss += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if extra_pids:
            for pid in extra_pids:
                try:
                    p = psutil.Process(pid)
                    total_rss += p.memory_info().rss
                    for c in p.children(recursive=True):
                        total_rss += c.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        return round(total_rss / (1024 * 1024), 2)
    except Exception:
        return 0.0


class ProcessTreePeakTracker:
    def __init__(self, interval: float = 0.1, include_parent: bool = False, extra_pids: Optional[list[int]] = None):
        self.interval = interval
        self.include_parent = include_parent
        self.extra_pids = extra_pids or []
        self.peak_mb = 0.0
        self._stop_event = None
        self._thread = None

    def _monitor(self):
        while not self._stop_event.is_set():
            rss = get_process_tree_rss_mb(self.include_parent, self.extra_pids)
            if rss > self.peak_mb:
                self.peak_mb = rss
            time.sleep(self.interval)

    def __enter__(self):
        import threading
        self.peak_mb = get_process_tree_rss_mb(self.include_parent, self.extra_pids)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._stop_event:
            self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        final_rss = get_process_tree_rss_mb(self.include_parent, self.extra_pids)
        if final_rss > self.peak_mb:
            self.peak_mb = final_rss


def checkpoint_and_analyze_disk(directory: Path | str) -> dict[str, Any]:
    p = Path(directory)
    if not p.exists():
        return {"data_mb": 0.0, "model_mb": 0.0, "total_mb": 0.0, "files": {}}

    for db_path in p.rglob("*.db"):
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.close()
        except Exception:
            pass

    data_bytes = 0
    model_bytes = 0
    wal_bytes = 0
    audit_bytes = 0
    vault_bytes = 0
    log_bytes = 0
    file_map = {}

    for f in p.rglob("*"):
        try:
            if not f.is_file():
                continue
            if f.is_symlink():
                continue
            sz = f.stat().st_size
            rel = str(f.relative_to(p))
            file_map[rel] = round(sz / (1024 * 1024), 3)

            lower_name = f.name.lower()
            lower_path = str(f).lower()
            if "fastembed_cache" in lower_path or "models--" in lower_path or "blobs" in lower_path or "models" in lower_path or ".onnx" in lower_name:
                model_bytes += sz
            elif "audit" in lower_name:
                audit_bytes += sz
            elif lower_name.endswith(("-wal", "-shm", "-journal")):
                wal_bytes += sz
            elif lower_name.endswith((".db", ".sqlite")):
                data_bytes += sz
            elif lower_name.endswith((".md", ".txt")):
                vault_bytes += sz
            elif lower_name.endswith(".log"):
                log_bytes += sz
            else:
                data_bytes += sz
        except Exception:
            pass

    total_bytes = data_bytes + model_bytes + wal_bytes + audit_bytes + vault_bytes + log_bytes
    return {
        "data_mb": round((data_bytes + wal_bytes + vault_bytes) / (1024 * 1024), 2),
        "audit_mb": round(audit_bytes / (1024 * 1024), 3),
        "model_mb": round(model_bytes / (1024 * 1024), 2),
        "log_mb": round(log_bytes / (1024 * 1024), 3),
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "top_files": sorted(file_map.items(), key=lambda x: x[1], reverse=True)[:5],
    }


def link_shared_models(target_mesh_home: Path):
    shared_models = Path.home() / ".mesh" / "models"
    if not shared_models.exists():
        return

    dst_models = target_mesh_home / "models"
    if dst_models.exists():
        return

    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(shared_models), str(dst_models))
        else:
            os.symlink(shared_models, dst_models)
    except Exception:
        pass


SHARED_FASTEMBED_CACHE = Path.home() / ".cache" / "fastembed_shared"


def link_shared_fastembed(target_bm_home: Path):
    if not SHARED_FASTEMBED_CACHE.exists():
        return
    dst_cache = target_bm_home / ".basic-memory" / "fastembed_cache"
    dst_cache.parent.mkdir(parents=True, exist_ok=True)
    if dst_cache.exists():
        return
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(SHARED_FASTEMBED_CACHE), str(dst_cache))
        else:
            os.symlink(SHARED_FASTEMBED_CACHE, dst_cache)
    except Exception:
        pass


def snapshot_mesh_db(base: Path, src_ns: str, dst_ns: str) -> None:
    from mesh.namespaces import _evict_namespace_caches
    _evict_namespace_caches(src_ns)
    src_db = base / f"mesh_{src_ns}.db"
    dst_db = base / f"mesh_{dst_ns}.db"
    if not src_db.exists():
        return
    conn = sqlite3.connect(str(src_db))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    shutil.copy2(src_db, dst_db)
    conn2 = sqlite3.connect(str(dst_db))
    try:
        conn2.execute("UPDATE memories SET namespace = ?", (dst_ns,))
        conn2.commit()
        conn2.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn2.close()


def parse_auxly_output(raw_output: str) -> list[tuple[str, str]]:
    if not raw_output or not raw_output.strip():
        return []
    candidates = []
    blocks = re.split(r"(?:\n|^)\s*(?:📄|\U0001f4c4|\?)\s*([a-zA-Z0-9_\-\.]+\.md)", raw_output)
    if len(blocks) >= 3:
        for i in range(1, len(blocks), 2):
            fname = blocks[i].strip()
            body = blocks[i + 1].strip() if i + 1 < len(blocks) else ""
            candidates.append((fname, body))
    elif raw_output.strip():
        for m in re.finditer(r"(turn_\d+\.md)", raw_output):
            candidates.append((m.group(1), raw_output))
        if not candidates:
            candidates.append(("auxly_raw", raw_output))
    return candidates


def compute_bootstrap_ci(data: list[float], n_boot: int = 1000, ci: float = 0.95, seed: int = 42) -> list[float]:
    if not data:
        return [0.0, 0.0]
    import random
    rng = random.Random(seed)
    n = len(data)
    boot_means = []
    for _ in range(n_boot):
        sample = [data[rng.randint(0, n - 1)] for _ in range(n)]
        boot_means.append(sum(sample) / n)
    boot_means.sort()
    lower_idx = int((1 - ci) / 2 * n_boot)
    upper_idx = int((1 + ci) / 2 * n_boot)
    return [round(boot_means[lower_idx], 4), round(boot_means[min(upper_idx, n_boot - 1)], 4)]


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


def find_bm_bin() -> Path:
    p = PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "basic-memory.exe"
    if p.exists():
        return p
    return Path("C:/Users/bb201/desktop/mesh/benchmarks/.venv-mem0/Scripts/basic-memory.exe")


def find_auxly_bin() -> Path:
    p = PROJECT_ROOT / "benchmarks" / "bin" / "auxly.exe"
    if p.exists():
        return p
    return Path("C:/Users/bb201/desktop/mesh/benchmarks/bin/auxly.exe")


def find_mem0_py() -> str:
    p = PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "python.exe"
    if p.exists():
        return str(p)
    return str(Path("C:/Users/bb201/desktop/mesh/benchmarks/.venv-mem0/Scripts/python.exe"))


def prepare_question_haystack(q: dict[str, Any], q_idx: int) -> dict[str, Any]:
    """
    Build haystack items with tags placed at the START of memory text.
    Returns turns, gold tags, tag_to_session, and metadata.
    """
    haystack_sessions = q.get("haystack_sessions", [])
    session_ids = q.get("haystack_session_ids", [])
    gold_answer_session_ids = set(q.get("answer_session_ids", []))

    items = []
    # Prepend known item and long item gates
    items.append({
        "tag": KNOWN_ITEM_TAG,
        "content": KNOWN_ITEM_CONTENT,
        "session_id": "preflight_ki",
        "has_answer": False,
        "is_gate": True,
    })
    items.append({
        "tag": LONG_TURN_TAG,
        "content": LONG_TURN_CONTENT,
        "session_id": "preflight_long",
        "has_answer": False,
        "is_gate": True,
    })

    tag_to_session = {KNOWN_ITEM_TAG: "preflight_ki", LONG_TURN_TAG: "preflight_long"}
    gold_turn_tags = set()
    gold_session_tags = set()

    turn_counter = 2
    for s_idx, session in enumerate(haystack_sessions):
        sess_id = session_ids[s_idx] if s_idx < len(session_ids) else f"sess_{s_idx}"
        for turn_data in session:
            tag = f"[turn:{turn_counter:05d}]"
            turn_counter += 1
            raw_text = turn_data.get("content", "")
            # Tag placed strictly at START of turn text
            tagged_content = f"{tag} {raw_text}"
            has_ans = bool(turn_data.get("has_answer", False))

            tag_to_session[tag] = sess_id
            if has_ans:
                gold_turn_tags.add(tag)
            if sess_id in gold_answer_session_ids:
                gold_session_tags.add(tag)

            items.append({
                "tag": tag,
                "content": tagged_content,
                "session_id": sess_id,
                "has_answer": has_ans,
                "is_gate": False,
            })

    return {
        "question_id": q.get("question_id"),
        "question_type": q.get("question_type"),
        "question": q.get("question"),
        "question_kw": extract_keywords(q.get("question", "")),
        "answer": q.get("answer"),
        "items": items,
        "gold_turn_tags": gold_turn_tags,
        "gold_session_tags": gold_session_tags,
        "gold_answer_sessions": gold_answer_session_ids,
        "tag_to_session": tag_to_session,
        "total_haystack_turns": len(items) - 2,
    }

# -----------------------------------------------------------------------------
# TOOL EVALUATORS (PER-QUESTION FRESH STORE LIFECYCLE)
# -----------------------------------------------------------------------------

def extract_turn_identifier(c_id: str, c_text: str) -> Optional[str]:
    for s in [c_id, c_text]:
        if not s:
            continue
        m = re.search(r"\[turn:(\d+)\]", s, re.IGNORECASE)
        if m:
            return f"[turn:{int(m.group(1)):05d}]"
        m = re.search(r"turn[_\:](\d+)", s, re.IGNORECASE)
        if m:
            return f"[turn:{int(m.group(1)):05d}]"
        m = re.search(r"turn_(\d+)\.md", s, re.IGNORECASE)
        if m:
            return f"[turn:{int(m.group(1)):05d}]"
    return None


def score_candidates(
    candidates: list[tuple[str, str]],
    gold_turn_tags: set[str],
    gold_session_tags: set[str],
    tag_to_session: dict[str, str],
) -> dict[str, Any]:
    """Score retrieval candidates using ID-based mapping as primary, tag substring as fallback."""
    first_turn_rank = None
    for r, (c_id, c_text) in enumerate(candidates, start=1):
        tag = extract_turn_identifier(c_id, c_text)
        if tag and tag in gold_turn_tags:
            first_turn_rank = r
            break
        for gt in gold_turn_tags:
            if gt in c_id or gt in c_text:
                first_turn_rank = r
                break
        if first_turn_rank is not None:
            break

    first_sess_rank = None
    for r, (c_id, c_text) in enumerate(candidates, start=1):
        tag = extract_turn_identifier(c_id, c_text)
        sess = tag_to_session.get(tag) if tag else None
        if sess and sess in gold_session_tags:
            first_sess_rank = r
            break
        for st in gold_session_tags:
            if st in c_id or st in c_text:
                first_sess_rank = r
                break
        if first_sess_rank is not None:
            break

    return {
        "turn_hit_at_1": 1.0 if (first_turn_rank and first_turn_rank <= 1) else 0.0,
        "turn_hit_at_5": 1.0 if (first_turn_rank and first_turn_rank <= 5) else 0.0,
        "turn_hit_at_10": 1.0 if (first_turn_rank and first_turn_rank <= 10) else 0.0,
        "turn_mrr": (1.0 / first_turn_rank) if first_turn_rank else 0.0,
        "turn_rank": first_turn_rank,
        "sess_hit_at_1": 1.0 if (first_sess_rank and first_sess_rank <= 1) else 0.0,
        "sess_hit_at_5": 1.0 if (first_sess_rank and first_sess_rank <= 5) else 0.0,
        "sess_hit_at_10": 1.0 if (first_sess_rank and first_sess_rank <= 10) else 0.0,
        "sess_mrr": (1.0 / first_sess_rank) if first_sess_rank else 0.0,
        "sess_rank": first_sess_rank,
    }


def eval_mesh_sdk_question(q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    """Evaluate Mesh SDK in-process on a single fresh store."""
    from mesh.memory import Mesh, warmup_model
    from mesh.namespaces import _evict_namespace_caches

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix=f"pilot_mesh_sdk_{q_idx}_") as temp_dir:
        base = Path(temp_dir)
        link_shared_models(base)
        os.environ["MESH_HOME"] = str(base)
        os.environ["MESH_NO_AUTO_DAEMON"] = "1"
        os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
        os.environ["MESH_SYNC_ONNX"] = "1"
        os.environ["MESH_MAX_CONTENT_LENGTH"] = "131072"
        ns = f"pilot_q_{q_idx}"

        warmup_model(assume_yes=True, blocking=True)
        harness_baseline_mb = get_harness_baseline_rss_mb()

        with ProcessTreePeakTracker(include_parent=True) as tracker:
            mesh = Mesh(namespace=ns, agent_id="eval_agent")

            ingest_accounting = {
                "haystack_turns": q_data.get("total_haystack_turns", len(q_data["items"]) - 2),
                "total_submitted": len(q_data["items"]),
                "ingested": 0,
                "rejected": 0,
                "rejection_reasons": [],
                "failed": 0,
                "failed_reasons": [],
                "quarantined": 0,
                "gold_turn_status": "all_ingested",
            }

            turn_latencies_ms = []
            with DualTimer() as dt_ingest:
                for item in q_data["items"]:
                    is_gold = item.get("has_answer") is True
                    t0 = time.perf_counter()
                    try:
                        mesh.learn(item["content"], tags=[item["tag"]])
                        ingest_accounting["ingested"] += 1
                        turn_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
                    except ValueError as e:
                        err_str = str(e)
                        ingest_accounting["rejected"] += 1
                        ingest_accounting["rejection_reasons"].append(err_str)
                        if is_gold:
                            ingest_accounting["gold_turn_status"] = "gold_rejected"
                    except Exception as e:
                        ingest_accounting["failed"] += 1
                        ingest_accounting["failed_reasons"].append(str(e))
                        if is_gold:
                            ingest_accounting["gold_turn_status"] = "gold_failed"
            write_sec = dt_ingest.mono_elapsed

            first_50_mean = round(statistics.mean(turn_latencies_ms[:50]), 2) if len(turn_latencies_ms) >= 50 else round(statistics.mean(turn_latencies_ms), 2)
            last_50_mean = round(statistics.mean(turn_latencies_ms[-50:]), 2) if len(turn_latencies_ms) >= 50 else round(statistics.mean(turn_latencies_ms), 2)

            mesh.close()
            _evict_namespace_caches(ns)

            # Create isolated query snapshots
            snap_ki = f"{ns}_ki"
            snap_lt = f"{ns}_lt"
            snap_raw = f"{ns}_raw"
            snap_kw = f"{ns}_kw"
            snapshot_mesh_db(base, ns, snap_ki)
            snapshot_mesh_db(base, ns, snap_lt)
            snapshot_mesh_db(base, ns, snap_raw)
            snapshot_mesh_db(base, ns, snap_kw)

            # Known item check on snap_ki
            mesh_ki = Mesh(namespace=snap_ki, agent_id="eval_agent")
            ki_res = mesh_ki.recall(KNOWN_ITEM_QUERY, n=5)
            ki_rank = None
            for r, cand in enumerate(ki_res, start=1):
                if KNOWN_ITEM_TAG in cand.get("content", "") or any(KNOWN_ITEM_TAG in t for t in cand.get("tags", [])):
                    ki_rank = r
                    break
            mesh_ki.close()
            _evict_namespace_caches(snap_ki)

            # Long turn check on snap_lt
            mesh_lt = Mesh(namespace=snap_lt, agent_id="eval_agent")
            lt_res = mesh_lt.recall(LONG_TURN_QUERY, n=5)
            lt_tag_found = any(LONG_TURN_TAG in cand.get("content", "") for cand in lt_res)
            mesh_lt.close()
            _evict_namespace_caches(snap_lt)

            # Query Raw on snap_raw
            mesh_raw = Mesh(namespace=snap_raw, agent_id="eval_agent")
            with DualTimer() as dt_raw:
                raw_res = mesh_raw.recall(q_data["question"], n=10)
            raw_cands = [(r.get("id", ""), r.get("content", "")) for r in raw_res]
            raw_ids = [extract_turn_identifier(r.get("id", ""), r.get("content", "")) or r.get("id", "") for r in raw_res]
            backend = raw_res[0].get("embedding_backend", "unknown") if raw_res else "unknown"
            raw_scores = score_candidates(raw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])
            mesh_raw.close()
            _evict_namespace_caches(snap_raw)

            # Query Keyword on snap_kw
            mesh_kw = Mesh(namespace=snap_kw, agent_id="eval_agent")
            with DualTimer() as dt_kw:
                kw_res = mesh_kw.recall(q_data["question_kw"], n=10)
            kw_cands = [(r.get("id", ""), r.get("content", "")) for r in kw_res]
            kw_scores = score_candidates(kw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])
            mesh_kw.close()
            _evict_namespace_caches(snap_kw)

            disk_info = checkpoint_and_analyze_disk(base)
            gc.collect()

        tool_peak_ram = max(0.0, round(tracker.peak_mb - harness_baseline_mb, 2))

        return {
            "write_seconds": write_sec,
            "raw_ms": dt_raw.mono_elapsed * 1000,
            "kw_ms": dt_kw.mono_elapsed * 1000,
            "dt_flag": dt_raw.discrepancy_flag or dt_kw.discrepancy_flag,
            "ki_rank": ki_rank,
            "lt_tag_found": lt_tag_found,
            "raw_scores": raw_scores,
            "kw_scores": kw_scores,
            "raw_ids": raw_ids,
            "embedding_backend": backend,
            "disk": disk_info,
            "ram_mb": tool_peak_ram,
            "harness_baseline_ram_mb": harness_baseline_mb,
            "ingest_accounting": ingest_accounting,
            "timing_breakdown": {
                "first_50_turns_mean_ms": first_50_mean,
                "last_50_turns_mean_ms": last_50_mean,
                "latency_growth_ratio": round(last_50_mean / first_50_mean, 2) if first_50_mean else 1.0,
            },
            "top_candidates_raw": [c[1][:100] for c in raw_cands[:3]],
            "raw_results_payload": [
                {"id": extract_turn_identifier(r.get("id", ""), r.get("content", "")) or r.get("id"), "content": r.get("content", "")[:200], "score": r.get("similarity")}
                for r in raw_res[:5]
            ],
        }


def eval_mesh_cli_daemon_question(q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    """Evaluate Mesh CLI talking to background daemon via HTTP IPC."""
    from mesh.cli import _daemon_request
    from mesh.daemon import start_daemon, stop_daemon

    test_port = 17700 + (q_idx % 50)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix=f"pilot_mesh_daemon_{q_idx}_") as temp_dir:
        base = Path(temp_dir)
        link_shared_models(base)
        os.environ["MESH_HOME"] = str(base)
        os.environ["MESH_HTTP_PORT"] = str(test_port)
        os.environ["MESH_PORT"] = str(test_port)
        os.environ["MESH_SYNC_ONNX"] = "1"
        os.environ["MESH_MAX_CONTENT_LENGTH"] = "131072"
        os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
        os.environ["MESH_NO_UPDATE_CHECK"] = "1"
        ns = f"pilot_daemon_{q_idx}"

        harness_baseline_mb = get_harness_baseline_rss_mb()
        t0_daemon = time.perf_counter()
        start_daemon(port=test_port, wait=True, wait_for_ready=True, lazy=False)
        daemon_startup_sec = round(time.perf_counter() - t0_daemon, 3)

        with ProcessTreePeakTracker(include_parent=False) as tracker:
            ingest_accounting = {
                "haystack_turns": q_data.get("total_haystack_turns", len(q_data["items"]) - 2),
                "total_submitted": len(q_data["items"]),
                "ingested": 0,
                "rejected": 0,
                "rejection_reasons": [],
                "failed": 0,
                "failed_reasons": [],
                "quarantined": 0,
                "gold_turn_status": "all_ingested",
            }

            with DualTimer() as dt_ingest:
                for item in q_data["items"]:
                    is_gold = item.get("has_answer") is True
                    resp = _daemon_request("/learn", payload={"content": item["content"], "tags": [item["tag"]], "namespace": ns}, port=test_port)
                    if resp and resp.get("status") == "ok":
                        ingest_accounting["ingested"] += 1
                    elif resp and resp.get("status") == "error":
                        ingest_accounting["rejected"] += 1
                        ingest_accounting["rejection_reasons"].append(resp.get("error", "daemon error"))
                        if is_gold:
                            ingest_accounting["gold_turn_status"] = "gold_rejected"
                    else:
                        ingest_accounting["failed"] += 1
                        ingest_accounting["failed_reasons"].append("unknown daemon failure")
                        if is_gold:
                            ingest_accounting["gold_turn_status"] = "gold_failed"
            write_sec = dt_ingest.mono_elapsed

            # Snapshot store
            snap_ki = f"{ns}_ki"
            snap_lt = f"{ns}_lt"
            snap_raw = f"{ns}_raw"
            snap_kw = f"{ns}_kw"
            snapshot_mesh_db(base, ns, snap_ki)
            snapshot_mesh_db(base, ns, snap_lt)
            snapshot_mesh_db(base, ns, snap_raw)
            snapshot_mesh_db(base, ns, snap_kw)

            # First query after ready
            t0_first = time.perf_counter()
            ki_resp = _daemon_request("/recall", payload={"query": KNOWN_ITEM_QUERY, "n": 5, "namespace": snap_ki}, port=test_port)
            first_query_after_ready_ms = round((time.perf_counter() - t0_first) * 1000.0, 2)
            ki_res = (ki_resp or {}).get("results", [])
            ki_rank = None
            for r, cand in enumerate(ki_res, start=1):
                if KNOWN_ITEM_TAG in cand.get("content", ""):
                    ki_rank = r
                    break

            # Long turn check
            lt_resp = _daemon_request("/recall", payload={"query": LONG_TURN_QUERY, "n": 5, "namespace": snap_lt}, port=test_port)
            lt_res = (lt_resp or {}).get("results", [])
            lt_tag_found = any(LONG_TURN_TAG in cand.get("content", "") for cand in lt_res)

            # Query Raw
            with DualTimer() as dt_raw:
                raw_resp = _daemon_request("/recall", payload={"query": q_data["question"], "n": 10, "namespace": snap_raw}, port=test_port)
            raw_res = (raw_resp or {}).get("results", [])
            raw_cands = [(r.get("id", ""), r.get("content", "")) for r in raw_res]
            raw_ids = [extract_turn_identifier(r.get("id", ""), r.get("content", "")) or r.get("id", "") for r in raw_res]
            backend = (raw_resp or {}).get("embedding_backend", "unknown")
            raw_scores = score_candidates(raw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

            # Query Keyword
            with DualTimer() as dt_kw:
                kw_resp = _daemon_request("/recall", payload={"query": q_data["question_kw"], "n": 10, "namespace": snap_kw}, port=test_port)
            kw_res = (kw_resp or {}).get("results", [])
            kw_cands = [(r.get("id", ""), r.get("content", "")) for r in kw_res]
            kw_scores = score_candidates(kw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

            stop_daemon(port=test_port)
            time.sleep(0.3)
            disk_info = checkpoint_and_analyze_disk(base)
            gc.collect()

        return {
            "write_seconds": write_sec,
            "daemon_startup_sec": daemon_startup_sec,
            "first_query_after_ready_ms": first_query_after_ready_ms,
            "raw_ms": dt_raw.mono_elapsed * 1000,
            "kw_ms": dt_kw.mono_elapsed * 1000,
            "dt_flag": dt_raw.discrepancy_flag or dt_kw.discrepancy_flag,
            "ki_rank": ki_rank,
            "lt_tag_found": lt_tag_found,
            "raw_scores": raw_scores,
            "kw_scores": kw_scores,
            "raw_ids": raw_ids,
            "embedding_backend": backend,
            "disk": disk_info,
            "ram_mb": tracker.peak_mb,
            "harness_baseline_ram_mb": harness_baseline_mb,
            "ingest_accounting": ingest_accounting,
            "top_candidates_raw": [c[1][:100] for c in raw_cands[:3]],
            "raw_results_payload": [
                {"id": extract_turn_identifier(r.get("id", ""), r.get("content", "")) or r.get("id"), "content": r.get("content", "")[:200], "score": r.get("similarity")}
                for r in raw_res[:5]
            ],
        }


async def eval_mesh_mcp_question_async(q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    """Evaluate Mesh MCP in persistent stdio session on a single fresh store."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix=f"pilot_mesh_mcp_{q_idx}_") as temp_dir:
        base = Path(temp_dir)
        link_shared_models(base)
        env = os.environ.copy()
        env["MESH_HOME"] = str(base)
        env["MESH_NO_MODEL_DOWNLOAD"] = "1"
        env["MESH_NO_UPDATE_CHECK"] = "1"
        env["MESH_SYNC_ONNX"] = "1"
        env["MESH_MAX_CONTENT_LENGTH"] = "131072"
        ns = f"pilot_mcp_{q_idx}"

        server_cmd = (
            "import sys; "
            "from mesh.memory import warmup_model; warmup_model(blocking=True, assume_yes=True); "
            "from mesh.mcp_server import run; "
            "sys.argv = ['mesh-mcp', '--trusted-local']; "
            "run()"
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-u", "-c", server_cmd],
            env=env,
        )

        harness_baseline_mb = get_harness_baseline_rss_mb()
        with ProcessTreePeakTracker(include_parent=False) as tracker:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    ingest_accounting = {
                        "haystack_turns": q_data.get("total_haystack_turns", len(q_data["items"]) - 2),
                        "total_submitted": len(q_data["items"]),
                        "ingested": 0,
                        "rejected": 0,
                        "rejection_reasons": [],
                        "failed": 0,
                        "failed_reasons": [],
                        "quarantined": 0,
                        "gold_turn_status": "all_ingested",
                    }

                    # Ingest
                    with DualTimer() as dt_ingest:
                        for item in q_data["items"]:
                            is_gold = item.get("has_answer") is True
                            try:
                                await session.call_tool("mesh_learn", arguments={
                                    "content": item["content"],
                                    "tags": [item["tag"]],
                                    "namespace": ns,
                                })
                                ingest_accounting["ingested"] += 1
                            except Exception as e:
                                ingest_accounting["failed"] += 1
                                ingest_accounting["failed_reasons"].append(str(e))
                                if is_gold:
                                    ingest_accounting["gold_turn_status"] = "gold_failed"
                    write_sec = dt_ingest.mono_elapsed

                    # Snapshot store
                    snap_ki = f"{ns}_ki"
                    snap_lt = f"{ns}_lt"
                    snap_raw = f"{ns}_raw"
                    snap_kw = f"{ns}_kw"
                    snapshot_mesh_db(base, ns, snap_ki)
                    snapshot_mesh_db(base, ns, snap_lt)
                    snapshot_mesh_db(base, ns, snap_raw)
                    snapshot_mesh_db(base, ns, snap_kw)

                    # Known item check
                    ki_out = await session.call_tool("mesh_recall", arguments={"query": KNOWN_ITEM_QUERY, "n": 5, "namespace": snap_ki})
                    ki_text = "".join(str(c.text) for c in (ki_out.content or []))
                    ki_rank = None
                    try:
                        ki_data = json.loads(ki_text)
                        for r, cand in enumerate(ki_data.get("results", []), start=1):
                            if KNOWN_ITEM_TAG in cand.get("content", "") or any(KNOWN_ITEM_TAG in t for t in cand.get("tags", [])):
                                ki_rank = r
                                break
                    except Exception:
                        if KNOWN_ITEM_TAG in ki_text:
                            ki_rank = 1

                    # Long turn check
                    lt_out = await session.call_tool("mesh_recall", arguments={"query": LONG_TURN_QUERY, "n": 5, "namespace": snap_lt})
                    lt_text = "".join(str(c.text) for c in (lt_out.content or []))
                    lt_tag_found = False
                    try:
                        lt_data = json.loads(lt_text)
                        lt_tag_found = any(LONG_TURN_TAG in cand.get("content", "") for cand in lt_data.get("results", []))
                    except Exception:
                        lt_tag_found = LONG_TURN_TAG in lt_text

                    # Query Raw
                    with DualTimer() as dt_raw:
                        raw_out = await session.call_tool("mesh_recall", arguments={"query": q_data["question"], "n": 10, "namespace": snap_raw})
                    raw_text = "".join(str(c.text) for c in (raw_out.content or []))
                    raw_cands = []
                    raw_ids = []
                    backend = "unknown"
                    try:
                        raw_data = json.loads(raw_text)
                        raw_cands = [(r.get("id", ""), r.get("content", "")) for r in raw_data.get("results", [])]
                        raw_ids = [extract_turn_identifier(r.get("id", ""), r.get("content", "")) or r.get("id", "") for r in raw_data.get("results", [])]
                        backend = raw_data.get("embedding_backend", "unknown")
                    except Exception:
                        raw_cands = [("mcp_raw", raw_text)]
                    raw_scores = score_candidates(raw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

                    # Query Keyword
                    with DualTimer() as dt_kw:
                        kw_out = await session.call_tool("mesh_recall", arguments={"query": q_data["question_kw"], "n": 10, "namespace": snap_kw})
                    kw_text = "".join(str(c.text) for c in (kw_out.content or []))
                    kw_cands = []
                    try:
                        kw_data = json.loads(kw_text)
                        kw_cands = [(r.get("id", ""), r.get("content", "")) for r in kw_data.get("results", [])]
                    except Exception:
                        kw_cands = [("mcp_kw", kw_text)]
                    kw_scores = score_candidates(kw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

            disk_info = checkpoint_and_analyze_disk(base)
            gc.collect()

        return {
            "write_seconds": write_sec,
            "raw_ms": dt_raw.mono_elapsed * 1000,
            "kw_ms": dt_kw.mono_elapsed * 1000,
            "dt_flag": dt_raw.discrepancy_flag or dt_kw.discrepancy_flag,
            "ki_rank": ki_rank,
            "lt_tag_found": lt_tag_found,
            "raw_scores": raw_scores,
            "kw_scores": kw_scores,
            "raw_ids": raw_ids,
            "embedding_backend": backend,
            "disk": disk_info,
            "ram_mb": tracker.peak_mb,
            "harness_baseline_ram_mb": harness_baseline_mb,
            "ingest_accounting": ingest_accounting,
            "top_candidates_raw": [c[1][:100] for c in raw_cands[:3]],
            "raw_results_payload": [
                {"id": extract_turn_identifier(c[0], c[1]) or c[0], "content": c[1][:200]}
                for c in raw_cands[:5]
            ],
        }


def eval_mesh_mcp_question(q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    return asyncio.run(eval_mesh_mcp_question_async(q_data, q_idx))


def eval_auxly_cli_question(q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    """Evaluate Auxly CLI using 1 file per turn layout for turn-level comparability."""
    auxly_bin = str(find_auxly_bin())
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix=f"pilot_auxly_cli_{q_idx}_") as temp_dir:
        vault = Path(temp_dir) / "vault"
        vault.mkdir(parents=True, exist_ok=True)

        harness_baseline_mb = get_harness_baseline_rss_mb()
        with ProcessTreePeakTracker(include_parent=False) as tracker:
            ingest_accounting = {
                "haystack_turns": q_data.get("total_haystack_turns", len(q_data["items"]) - 2),
                "total_submitted": len(q_data["items"]),
                "ingested": 0,
                "rejected": 0,
                "rejection_reasons": [],
                "failed": 0,
                "failed_reasons": [],
                "quarantined": 0,
                "gold_turn_status": "all_ingested",
            }

            with DualTimer() as dt_ingest:
                for idx, item in enumerate(q_data["items"]):
                    note_path = vault / f"turn_{idx:05d}.md"
                    note_path.write_text(item["content"], encoding="utf-8")
                    ingest_accounting["ingested"] += 1
            write_sec = dt_ingest.mono_elapsed

            # Known item check
            p_ki = subprocess.run([auxly_bin, "--path", str(vault), "search", KNOWN_ITEM_QUERY], capture_output=True, text=True, encoding="utf-8", errors="replace")
            ki_cands = parse_auxly_output(p_ki.stdout)
            ki_rank = None
            for r, (fname, body) in enumerate(ki_cands, start=1):
                tid = extract_turn_identifier(fname, body)
                if tid == KNOWN_ITEM_TAG or KNOWN_ITEM_TAG in body or KNOWN_ITEM_ANSWER.lower() in body.lower():
                    ki_rank = r
                    break

            # Long turn check
            p_lt = subprocess.run([auxly_bin, "--path", str(vault), "search", LONG_TURN_QUERY], capture_output=True, text=True, encoding="utf-8", errors="replace")
            lt_cands = parse_auxly_output(p_lt.stdout)
            lt_tag_found = any(
                extract_turn_identifier(fname, body) == LONG_TURN_TAG or LONG_TURN_TAG in body
                for fname, body in lt_cands
            )

            # Query Raw
            with DualTimer() as dt_raw:
                p_raw = subprocess.run([auxly_bin, "--path", str(vault), "search", q_data["question"]], capture_output=True, text=True, encoding="utf-8", errors="replace")
            raw_cands = parse_auxly_output(p_raw.stdout)
            raw_scores = score_candidates(raw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

            # Query Keyword
            with DualTimer() as dt_kw:
                p_kw = subprocess.run([auxly_bin, "--path", str(vault), "search", q_data["question_kw"]], capture_output=True, text=True, encoding="utf-8", errors="replace")
            kw_cands = parse_auxly_output(p_kw.stdout)
            kw_scores = score_candidates(kw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

            disk_info = checkpoint_and_analyze_disk(vault)
            gc.collect()

        return {
            "write_seconds": write_sec,
            "raw_ms": dt_raw.mono_elapsed * 1000,
            "kw_ms": dt_kw.mono_elapsed * 1000,
            "dt_flag": dt_raw.discrepancy_flag or dt_kw.discrepancy_flag,
            "ki_rank": ki_rank,
            "lt_tag_found": lt_tag_found,
            "raw_scores": raw_scores,
            "kw_scores": kw_scores,
            "disk": disk_info,
            "ram_mb": tracker.peak_mb,
            "harness_baseline_ram_mb": harness_baseline_mb,
            "ingest_accounting": ingest_accounting,
            "top_candidates_raw": [c[1][:100] for c in raw_cands[:3]],
            "raw_results_payload": [
                {"id": extract_turn_identifier(c[0], c[1]) or c[0], "content": c[1][:200]}
                for c in raw_cands[:5]
            ],
            "sample_raw_stdout": p_raw.stdout[:300],
        }


async def eval_auxly_mcp_question_async(q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    """Evaluate Auxly MCP in persistent stdio session with 1 file per turn layout."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    auxly_bin = str(find_auxly_bin())
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix=f"pilot_auxly_mcp_{q_idx}_") as temp_dir:
        vault = Path(temp_dir) / "vault"
        vault.mkdir(parents=True, exist_ok=True)

        params = StdioServerParameters(command=auxly_bin, args=["--path", str(vault), "mcp-server"], env=os.environ.copy())

        harness_baseline_mb = get_harness_baseline_rss_mb()
        with ProcessTreePeakTracker(include_parent=False) as tracker:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    ingest_accounting = {
                        "haystack_turns": q_data.get("total_haystack_turns", len(q_data["items"]) - 2),
                        "total_submitted": len(q_data["items"]),
                        "ingested": 0,
                        "rejected": 0,
                        "rejection_reasons": [],
                        "failed": 0,
                        "failed_reasons": [],
                        "quarantined": 0,
                        "gold_turn_status": "all_ingested",
                    }

                    # Ingest: write 1 file per turn
                    with DualTimer() as dt_ingest:
                        for idx, item in enumerate(q_data["items"]):
                            note_path = vault / f"turn_{idx:05d}.md"
                            note_path.write_text(item["content"], encoding="utf-8")
                            ingest_accounting["ingested"] += 1
                    write_sec = dt_ingest.mono_elapsed

                    # Known item check
                    ki_out = await session.call_tool("auxly_memory_search", arguments={"query": KNOWN_ITEM_QUERY})
                    ki_text = "".join(str(c.text) for c in (ki_out.content or []))
                    ki_cands = parse_auxly_output(ki_text)
                    ki_rank = None
                    for r, (fname, body) in enumerate(ki_cands, start=1):
                        tid = extract_turn_identifier(fname, body)
                        if tid == KNOWN_ITEM_TAG or KNOWN_ITEM_TAG in body or KNOWN_ITEM_ANSWER.lower() in body.lower():
                            ki_rank = r
                            break

                    # Long turn check
                    lt_out = await session.call_tool("auxly_memory_search", arguments={"query": LONG_TURN_QUERY})
                    lt_text = "".join(str(c.text) for c in (lt_out.content or []))
                    lt_cands = parse_auxly_output(lt_text)
                    lt_tag_found = any(
                        extract_turn_identifier(fname, body) == LONG_TURN_TAG or LONG_TURN_TAG in body
                        for fname, body in lt_cands
                    )

                    # Query Raw
                    with DualTimer() as dt_raw:
                        raw_out = await session.call_tool("auxly_memory_search", arguments={"query": q_data["question"]})
                    raw_text = "".join(str(c.text) for c in (raw_out.content or []))
                    raw_cands = parse_auxly_output(raw_text)
                    raw_scores = score_candidates(raw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

                    # Query Keyword
                    with DualTimer() as dt_kw:
                        kw_out = await session.call_tool("auxly_memory_search", arguments={"query": q_data["question_kw"]})
                    kw_text = "".join(str(c.text) for c in (kw_out.content or []))
                    kw_cands = parse_auxly_output(kw_text)
                    kw_scores = score_candidates(kw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

            disk_info = checkpoint_and_analyze_disk(vault)
            gc.collect()

        return {
            "write_seconds": write_sec,
            "raw_ms": dt_raw.mono_elapsed * 1000,
            "kw_ms": dt_kw.mono_elapsed * 1000,
            "dt_flag": dt_raw.discrepancy_flag or dt_kw.discrepancy_flag,
            "ki_rank": ki_rank,
            "lt_tag_found": lt_tag_found,
            "raw_scores": raw_scores,
            "kw_scores": kw_scores,
            "disk": disk_info,
            "ram_mb": tracker.peak_mb,
            "harness_baseline_ram_mb": harness_baseline_mb,
            "ingest_accounting": ingest_accounting,
            "top_candidates_raw": [c[1][:100] for c in raw_cands[:3]],
            "raw_results_payload": [
                {"id": extract_turn_identifier(c[0], c[1]) or c[0], "content": c[1][:200]}
                for c in raw_cands[:5]
            ],
            "sample_raw_stdout": raw_text[:300],
        }


def eval_auxly_mcp_question(q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    return asyncio.run(eval_auxly_mcp_question_async(q_data, q_idx))


async def eval_bm_mcp_question_async(q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    """Evaluate Basic Memory MCP via FastMCP stdio session."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    bm_bin = str(find_bm_bin())
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix=f"pilot_bm_mcp_{q_idx}_") as temp_dir:
        base = Path(temp_dir)
        notes_dir = base / "notes"
        home_dir = base / "home"
        notes_dir.mkdir(parents=True, exist_ok=True)
        home_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["BASIC_MEMORY_HOME"] = str(home_dir)
        env["HOME"] = str(home_dir)
        env["USERPROFILE"] = str(home_dir)
        env["APPDATA"] = str(home_dir / "appdata")
        env["LOCALAPPDATA"] = str(home_dir / "localappdata")
        for p in [env["HOME"], env["APPDATA"], env["LOCALAPPDATA"]]:
            os.makedirs(p, exist_ok=True)

        link_shared_fastembed(home_dir)

        proj_name = f"lme_mcp_{q_idx}"
        params = StdioServerParameters(command=bm_bin, args=["mcp"], env=env)

        harness_baseline_mb = get_harness_baseline_rss_mb()
        with ProcessTreePeakTracker(include_parent=False) as tracker:
            ingest_accounting = {
                "haystack_turns": q_data.get("total_haystack_turns", len(q_data["items"]) - 2),
                "total_submitted": len(q_data["items"]),
                "ingested": 0,
                "rejected": 0,
                "rejection_reasons": [],
                "failed": 0,
                "failed_reasons": [],
                "quarantined": 0,
                "gold_turn_status": "all_ingested",
            }
            # 1. Ingest via direct note files
            with DualTimer() as dt_ingest:
                for idx, item in enumerate(q_data["items"]):
                    note_path = notes_dir / f"turn_{idx:05d}.md"
                    note_path.write_text(
                        f"---\ntitle: turn_{idx:05d}\n---\n{item['content']}\n",
                        encoding="utf-8",
                    )
                    ingest_accounting["ingested"] += 1
                # Project add and reindex --full
                subprocess.run([bm_bin, "project", "add", proj_name, str(notes_dir), "--no-wait", "--local"], env=env, capture_output=True, text=True, check=False)
                subprocess.run([bm_bin, "reindex", "-p", proj_name, "--full"], env=env, capture_output=True, text=True, check=False)
            write_sec = dt_ingest.mono_elapsed

            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    # Known item check
                    ki_out = await session.call_tool("search_notes", arguments={"query": KNOWN_ITEM_QUERY, "project": proj_name, "output_format": "json", "search_type": "hybrid", "page_size": 5})
                    ki_text = "".join(str(c.text) for c in (ki_out.content or []))
                    ki_rank = None
                    try:
                        ki_data = json.loads(ki_text)
                        for r, item in enumerate(ki_data.get("results", []), start=1):
                            tid = extract_turn_identifier(item.get("title", ""), item.get("content", ""))
                            if tid == KNOWN_ITEM_TAG or KNOWN_ITEM_TAG in item.get("content", "") or KNOWN_ITEM_ANSWER.lower() in item.get("content", "").lower():
                                ki_rank = r
                                break
                    except Exception:
                        if KNOWN_ITEM_TAG in ki_text:
                            ki_rank = 1

                    # Long turn check
                    lt_out = await session.call_tool("search_notes", arguments={"query": LONG_TURN_QUERY, "project": proj_name, "output_format": "json", "search_type": "hybrid", "page_size": 5})
                    lt_text = "".join(str(c.text) for c in (lt_out.content or []))
                    lt_tag_found = False
                    try:
                        lt_data = json.loads(lt_text)
                        lt_tag_found = any(
                            extract_turn_identifier(item.get("title", ""), item.get("content", "")) == LONG_TURN_TAG or LONG_TURN_TAG in item.get("content", "")
                            for item in lt_data.get("results", [])
                        )
                    except Exception:
                        lt_tag_found = LONG_TURN_TAG in lt_text

                    # Query Raw
                    with DualTimer() as dt_raw:
                        raw_out = await session.call_tool("search_notes", arguments={"query": q_data["question"], "project": proj_name, "output_format": "json", "search_type": "hybrid", "page_size": 10})
                    raw_text = "".join(str(c.text) for c in (raw_out.content or []))
                    raw_cands = []
                    raw_titles = []
                    try:
                        raw_data = json.loads(raw_text)
                        raw_cands = [(item.get("title", ""), item.get("content", "")) for item in raw_data.get("results", [])]
                        raw_titles = [item.get("title", "") for item in raw_data.get("results", [])]
                    except Exception:
                        raw_cands = [("bm_raw", raw_text)]
                    raw_scores = score_candidates(raw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

                    # Query Keyword
                    with DualTimer() as dt_kw:
                        kw_out = await session.call_tool("search_notes", arguments={"query": q_data["question_kw"], "project": proj_name, "output_format": "json", "search_type": "hybrid", "page_size": 10})
                    kw_text = "".join(str(c.text) for c in (kw_out.content or []))
                    kw_cands = []
                    try:
                        kw_data = json.loads(kw_text)
                        kw_cands = [(item.get("title", ""), item.get("content", "")) for item in kw_data.get("results", [])]
                    except Exception:
                        kw_cands = [("bm_kw", kw_text)]
                    kw_scores = score_candidates(kw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

            disk_info = checkpoint_and_analyze_disk(base)
            gc.collect()

        return {
            "write_seconds": write_sec,
            "raw_ms": dt_raw.mono_elapsed * 1000,
            "kw_ms": dt_kw.mono_elapsed * 1000,
            "dt_flag": dt_raw.discrepancy_flag or dt_kw.discrepancy_flag,
            "ki_rank": ki_rank,
            "lt_tag_found": lt_tag_found,
            "raw_scores": raw_scores,
            "kw_scores": kw_scores,
            "raw_titles": raw_titles,
            "disk": disk_info,
            "ram_mb": tracker.peak_mb,
            "harness_baseline_ram_mb": harness_baseline_mb,
            "ingest_accounting": ingest_accounting,
            "top_candidates_raw": [c[1][:100] for c in raw_cands[:3]],
            "raw_results_payload": [
                {"id": extract_turn_identifier(c[0], c[1]) or c[0], "content": c[1][:200]}
                for c in raw_cands[:5]
            ],
        }


def eval_bm_mcp_question(q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    return asyncio.run(eval_bm_mcp_question_async(q_data, q_idx))


def eval_mesh_cli_cold_question(q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    """Evaluate Mesh CLI cold subprocess invocation on a fresh store."""
    from mesh.memory import Mesh, warmup_model
    from mesh.namespaces import _evict_namespace_caches

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix=f"pilot_mesh_cold_{q_idx}_") as temp_dir:
        base = Path(temp_dir)
        link_shared_models(base)
        env = os.environ.copy()
        env["MESH_HOME"] = str(base)
        env["MESH_NO_AUTO_DAEMON"] = "1"
        env["MESH_NO_UPDATE_CHECK"] = "1"
        env["MESH_NO_MODEL_DOWNLOAD"] = "1"
        env["MESH_SYNC_ONNX"] = "1"
        env["MESH_MAX_CONTENT_LENGTH"] = "131072"
        env["MESH_CLI_FORCE_ONNX"] = "1"
        ns = f"pilot_cold_{q_idx}"

        os.environ["MESH_HOME"] = str(base)
        os.environ["MESH_NO_AUTO_DAEMON"] = "1"
        os.environ["MESH_NO_MODEL_DOWNLOAD"] = "1"
        os.environ["MESH_SYNC_ONNX"] = "1"
        os.environ["MESH_MAX_CONTENT_LENGTH"] = "131072"
        os.environ["MESH_CLI_FORCE_ONNX"] = "1"

        warmup_model(assume_yes=True, blocking=True)
        harness_baseline_mb = get_harness_baseline_rss_mb()

        with ProcessTreePeakTracker(include_parent=False) as tracker:
            mesh = Mesh(namespace=ns, agent_id="eval_agent")
            ingest_accounting = {
                "haystack_turns": q_data.get("total_haystack_turns", len(q_data["items"]) - 2),
                "total_submitted": len(q_data["items"]),
                "ingested": 0,
                "rejected": 0,
                "rejection_reasons": [],
                "failed": 0,
                "failed_reasons": [],
                "quarantined": 0,
                "gold_turn_status": "all_ingested",
            }
            with DualTimer() as dt_ingest:
                for item in q_data["items"]:
                    is_gold = item.get("has_answer") is True
                    try:
                        mesh.learn(item["content"], tags=[item["tag"]])
                        ingest_accounting["ingested"] += 1
                    except ValueError as e:
                        ingest_accounting["rejected"] += 1
                        ingest_accounting["rejection_reasons"].append(str(e))
                        if is_gold:
                            ingest_accounting["gold_turn_status"] = "gold_rejected"
                    except Exception as e:
                        ingest_accounting["failed"] += 1
                        ingest_accounting["failed_reasons"].append(str(e))
                        if is_gold:
                            ingest_accounting["gold_turn_status"] = "gold_failed"
            write_sec = dt_ingest.mono_elapsed

            mesh.close()
            _evict_namespace_caches(ns)
            del mesh
            gc.collect()

            # Snapshot store
            snap_ki = f"{ns}_ki"
            snap_lt = f"{ns}_lt"
            snap_raw = f"{ns}_raw"
            snap_kw = f"{ns}_kw"
            snapshot_mesh_db(base, ns, snap_ki)
            snapshot_mesh_db(base, ns, snap_lt)
            snapshot_mesh_db(base, ns, snap_raw)
            snapshot_mesh_db(base, ns, snap_kw)

            # Known item check via cold subprocess
            p_ki = subprocess.run(
                [sys.executable, "-m", "mesh", "recall", KNOWN_ITEM_QUERY, "--namespace", snap_ki, "-n", "5", "--json"],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            ki_rank = None
            if p_ki.returncode == 0:
                try:
                    data = json.loads(p_ki.stdout)
                    for r, cand in enumerate(data.get("results", []), start=1):
                        if KNOWN_ITEM_TAG in cand.get("content", "") or any(KNOWN_ITEM_TAG in t for t in cand.get("tags", [])):
                            ki_rank = r
                            break
                except Exception:
                    pass

            # Long turn check via cold subprocess
            p_lt = subprocess.run(
                [sys.executable, "-m", "mesh", "recall", LONG_TURN_QUERY, "--namespace", snap_lt, "-n", "5", "--json"],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            lt_tag_found = False
            if p_lt.returncode == 0:
                try:
                    data = json.loads(p_lt.stdout)
                    lt_tag_found = any(LONG_TURN_TAG in cand.get("content", "") for cand in data.get("results", []))
                except Exception:
                    pass

            # Query Raw via cold subprocess
            with DualTimer() as dt_raw:
                p_raw = subprocess.run(
                    [sys.executable, "-m", "mesh", "recall", q_data["question"], "--namespace", snap_raw, "-n", "10", "--json"],
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            raw_cands = []
            raw_ids = []
            backend = "unknown"
            if p_raw.returncode == 0:
                try:
                    data = json.loads(p_raw.stdout)
                    raw_cands = [(r.get("id", ""), r.get("content", "")) for r in data.get("results", [])]
                    raw_ids = [extract_turn_identifier(r.get("id", ""), r.get("content", "")) or r.get("id", "") for r in data.get("results", [])]
                    backend = data.get("embedding_backend", "unknown")
                except Exception:
                    pass
            raw_scores = score_candidates(raw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

            # Query Keyword via cold subprocess
            with DualTimer() as dt_kw:
                p_kw = subprocess.run(
                    [sys.executable, "-m", "mesh", "recall", q_data["question_kw"], "--namespace", snap_kw, "-n", "10", "--json"],
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            kw_cands = []
            if p_kw.returncode == 0:
                try:
                    data = json.loads(p_kw.stdout)
                    kw_cands = [(r.get("id", ""), r.get("content", "")) for r in data.get("results", [])]
                except Exception:
                    pass
            kw_scores = score_candidates(kw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

            disk_info = checkpoint_and_analyze_disk(base)
            gc.collect()

        return {
            "write_seconds": write_sec,
            "raw_ms": dt_raw.mono_elapsed * 1000,
            "kw_ms": dt_kw.mono_elapsed * 1000,
            "dt_flag": dt_raw.discrepancy_flag or dt_kw.discrepancy_flag,
            "ki_rank": ki_rank,
            "lt_tag_found": lt_tag_found,
            "raw_scores": raw_scores,
            "kw_scores": kw_scores,
            "raw_ids": raw_ids,
            "embedding_backend": backend,
            "disk": disk_info,
            "ram_mb": tracker.peak_mb,
            "harness_baseline_ram_mb": harness_baseline_mb,
            "ingest_accounting": ingest_accounting,
            "top_candidates_raw": [c[1][:100] for c in raw_cands[:3]],
            "raw_results_payload": [
                {"id": extract_turn_identifier(c[0], c[1]) or c[0], "content": c[1][:200]}
                for c in raw_cands[:5]
            ],
        }


def eval_bm_cli_question(q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    """Evaluate Basic Memory CLI subprocess commands on a fresh store."""
    bm_bin = str(find_bm_bin())
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix=f"pilot_bm_cli_{q_idx}_") as temp_dir:
        base = Path(temp_dir)
        notes_dir = base / "notes"
        home_dir = base / "home"
        notes_dir.mkdir(parents=True, exist_ok=True)
        home_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["HOME"] = str(home_dir)
        env["USERPROFILE"] = str(home_dir)
        env["APPDATA"] = str(home_dir / "appdata")
        env["LOCALAPPDATA"] = str(home_dir / "localappdata")
        env["BASIC_MEMORY_HOME"] = str(home_dir)
        for p in [env["HOME"], env["APPDATA"], env["LOCALAPPDATA"]]:
            os.makedirs(p, exist_ok=True)

        link_shared_fastembed(home_dir)

        proj_name = f"lme_cli_{q_idx}"
        harness_baseline_mb = get_harness_baseline_rss_mb()
        with ProcessTreePeakTracker(include_parent=False) as tracker:
            ingest_accounting = {
                "haystack_turns": q_data.get("total_haystack_turns", len(q_data["items"]) - 2),
                "total_submitted": len(q_data["items"]),
                "ingested": 0,
                "rejected": 0,
                "rejection_reasons": [],
                "failed": 0,
                "failed_reasons": [],
                "quarantined": 0,
                "gold_turn_status": "all_ingested",
            }
            with DualTimer() as dt_ingest:
                for idx, item in enumerate(q_data["items"]):
                    (notes_dir / f"turn_{idx:05d}.md").write_text(
                        f"---\ntitle: turn_{idx:05d}\n---\n{item['content']}\n",
                        encoding="utf-8",
                    )
                    ingest_accounting["ingested"] += 1
                # Project add and reindex --full (builds both FTS and sqlite-vec vector index)
                subprocess.run([bm_bin, "project", "add", proj_name, str(notes_dir), "--no-wait", "--local"], env=env, capture_output=True, text=True, check=False)
                subprocess.run([bm_bin, "reindex", "-p", proj_name, "--full"], env=env, capture_output=True, text=True, check=False)
            write_sec = dt_ingest.mono_elapsed

            # Known item check
            p_ki = subprocess.run([bm_bin, "grep", "--semantic", KNOWN_ITEM_QUERY, "--project", proj_name, "--page-size", "5", "--json", "--local"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            ki_rank = None
            if p_ki.returncode == 0:
                try:
                    data = json.loads(p_ki.stdout)
                    for r, item in enumerate(data.get("results", []), start=1):
                        tid = extract_turn_identifier(item.get("title", ""), item.get("content", ""))
                        if tid == KNOWN_ITEM_TAG or KNOWN_ITEM_TAG in item.get("content", "") or KNOWN_ITEM_ANSWER.lower() in item.get("content", "").lower():
                            ki_rank = r
                            break
                except Exception:
                    pass

            # Long turn check
            p_lt = subprocess.run([bm_bin, "grep", "--semantic", LONG_TURN_QUERY, "--project", proj_name, "--page-size", "5", "--json", "--local"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            lt_tag_found = False
            if p_lt.returncode == 0:
                try:
                    data = json.loads(p_lt.stdout)
                    lt_tag_found = any(
                        extract_turn_identifier(item.get("title", ""), item.get("content", "")) == LONG_TURN_TAG or LONG_TURN_TAG in item.get("content", "")
                        for item in data.get("results", [])
                    )
                except Exception:
                    pass

            # Query Raw
            with DualTimer() as dt_raw:
                p_raw = subprocess.run([bm_bin, "grep", "--semantic", q_data["question"], "--project", proj_name, "--page-size", "10", "--json", "--local"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            raw_cands = []
            raw_titles = []
            if p_raw.returncode == 0:
                try:
                    data = json.loads(p_raw.stdout)
                    raw_cands = [(item.get("title", ""), item.get("content", "")) for item in data.get("results", [])]
                    raw_titles = [item.get("title", "") for item in data.get("results", [])]
                except Exception:
                    pass
            raw_scores = score_candidates(raw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

            # Query Keyword
            with DualTimer() as dt_kw:
                p_kw = subprocess.run([bm_bin, "grep", "--semantic", q_data["question_kw"], "--project", proj_name, "--page-size", "10", "--json", "--local"], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            kw_cands = []
            if p_kw.returncode == 0:
                try:
                    data = json.loads(p_kw.stdout)
                    kw_cands = [(item.get("title", ""), item.get("content", "")) for item in data.get("results", [])]
                except Exception:
                    pass
            kw_scores = score_candidates(kw_cands, q_data["gold_turn_tags"], q_data["gold_session_tags"], q_data["tag_to_session"])

            disk_info = checkpoint_and_analyze_disk(base)
            gc.collect()

        return {
            "write_seconds": write_sec,
            "raw_ms": dt_raw.mono_elapsed * 1000,
            "kw_ms": dt_kw.mono_elapsed * 1000,
            "dt_flag": dt_raw.discrepancy_flag or dt_kw.discrepancy_flag,
            "ki_rank": ki_rank,
            "lt_tag_found": lt_tag_found,
            "raw_scores": raw_scores,
            "kw_scores": kw_scores,
            "raw_titles": raw_titles,
            "disk": disk_info,
            "ram_mb": tracker.peak_mb,
            "harness_baseline_ram_mb": harness_baseline_mb,
            "ingest_accounting": ingest_accounting,
            "top_candidates_raw": [c[1][:100] for c in raw_cands[:3]],
            "raw_results_payload": [
                {"id": extract_turn_identifier(c[0], c[1]) or c[0], "content": c[1][:200]}
                for c in raw_cands[:5]
            ],
        }


def run_mem0_pilot_all(prepared_questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run Mem0 SDK evaluation across all questions using dedicated Python 3.12 worker."""
    mem0_py = find_mem0_py()
    worker_script = PROJECT_ROOT / "benchmarks" / "run_mem0_pilot_worker.py"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="pilot_mem0_ipc_") as temp_dir:
        in_path = Path(temp_dir) / "questions_in.json"
        out_path = Path(temp_dir) / "results_out.json"

        # Serialize prepared questions (converting sets to lists)
        serializable = []
        for q in prepared_questions:
            q_copy = dict(q)
            q_copy["gold_turn_tags"] = list(q["gold_turn_tags"])
            q_copy["gold_session_tags"] = list(q["gold_session_tags"])
            q_copy["gold_answer_sessions"] = list(q["gold_answer_sessions"])
            serializable.append(q_copy)

        with open(in_path, "w", encoding="utf-8") as f:
            json.dump(serializable, f)

        p = subprocess.run(
            [mem0_py, "-u", str(worker_script), str(in_path), str(out_path)],
            capture_output=True,
            text=True,
        )
        if p.returncode != 0:
            logger.error("Mem0 worker failed:\nSTDOUT:\n%s\nSTDERR:\n%s", p.stdout, p.stderr)
            raise RuntimeError(f"Mem0 worker exited with code {p.returncode}")

        with open(out_path, "r", encoding="utf-8") as f:
            results = json.load(f)

        return results


def aggregate_tool_results(
    tool_name: str,
    tool_class: str,
    results: list[dict[str, Any]],
    q_data_list: list[dict[str, Any]],
) -> dict[str, Any]:
    n = len(results)
    if n == 0:
        return {}

    total_turns = sum(q["total_haystack_turns"] for q in q_data_list)
    total_write_sec = sum(r["write_seconds"] for r in results)
    mean_write_sec = total_write_sec / n
    write_ms_per_turn = (total_write_sec * 1000.0) / total_turns if total_turns else 0.0

    raw_ms_list = [r["raw_ms"] for r in results]
    kw_ms_list = [r["kw_ms"] for r in results]

    # Scores
    t_h1_raw = [r["raw_scores"]["turn_hit_at_1"] for r in results]
    t_h5_raw = [r["raw_scores"]["turn_hit_at_5"] for r in results]
    t_mrr_raw = [r["raw_scores"]["turn_mrr"] for r in results]
    s_h1_raw = [r["raw_scores"]["sess_hit_at_1"] for r in results]
    s_h5_raw = [r["raw_scores"]["sess_hit_at_5"] for r in results]
    s_mrr_raw = [r["raw_scores"]["sess_mrr"] for r in results]

    t_h1_kw = [r["kw_scores"]["turn_hit_at_1"] for r in results]
    t_h5_kw = [r["kw_scores"]["turn_hit_at_5"] for r in results]
    t_mrr_kw = [r["kw_scores"]["turn_mrr"] for r in results]
    s_h1_kw = [r["kw_scores"]["sess_hit_at_1"] for r in results]
    s_h5_kw = [r["kw_scores"]["sess_hit_at_5"] for r in results]
    s_mrr_kw = [r["kw_scores"]["sess_mrr"] for r in results]

    # Gates
    ki_success = sum(1 for r in results if r.get("ki_rank") == 1)
    lt_success = sum(1 for r in results if r.get("lt_tag_found") is True)

    # Footprint & RAM
    data_mb_list = [r["disk"]["data_mb"] for r in results]
    model_mb_list = [r["disk"]["model_mb"] for r in results]
    total_mb_list = [r["disk"]["total_mb"] for r in results]
    ram_mb_list = [r["ram_mb"] for r in results]

    # Discrepancies
    dt_flags = sum(1 for r in results if r.get("dt_flag"))

    # Question type breakdowns
    type_breakdown = {}
    for r, q in zip(results, q_data_list):
        qtype = q.get("question_type", "unknown")
        if qtype not in type_breakdown:
            type_breakdown[qtype] = {"n": 0, "turn_h1": [], "turn_h5": [], "turn_mrr": [], "sess_h1": [], "sess_h5": []}
        type_breakdown[qtype]["n"] += 1
        type_breakdown[qtype]["turn_h1"].append(r["raw_scores"]["turn_hit_at_1"])
        type_breakdown[qtype]["turn_h5"].append(r["raw_scores"]["turn_hit_at_5"])
        type_breakdown[qtype]["turn_mrr"].append(r["raw_scores"]["turn_mrr"])
        type_breakdown[qtype]["sess_h1"].append(r["raw_scores"]["sess_hit_at_1"])
        type_breakdown[qtype]["sess_h5"].append(r["raw_scores"]["sess_hit_at_5"])

    qtype_summary = {}
    for qtype, vals in type_breakdown.items():
        cnt = vals["n"]
        qtype_summary[qtype] = {
            "count": cnt,
            "turn_recall_at_1": round(sum(vals["turn_h1"]) / cnt, 4),
            "turn_recall_at_5": round(sum(vals["turn_h5"]) / cnt, 4),
            "turn_mrr": round(sum(vals["turn_mrr"]) / cnt, 4),
            "sess_recall_at_1": round(sum(vals["sess_h1"]) / cnt, 4),
            "sess_recall_at_5": round(sum(vals["sess_h5"]) / cnt, 4),
        }

    return {
        "tool_name": tool_name,
        "tool_class": tool_class,
        "questions_evaluated": n,
        "total_turns": total_turns,
        "mean_turns_per_q": round(total_turns / n, 1),
        "total_write_sec": round(total_write_sec, 2),
        "mean_write_sec_per_q": round(mean_write_sec, 3),
        "write_ms_per_turn": round(write_ms_per_turn, 2),
        "query_latency_raw": {
            "p50_ms": round(percentile(raw_ms_list, 50), 2),
            "p95_ms": round(percentile(raw_ms_list, 95), 2),
            "max_ms": round(max(raw_ms_list), 2),
            "mean_ms": round(statistics.mean(raw_ms_list), 2),
        },
        "query_latency_kw": {
            "p50_ms": round(percentile(kw_ms_list, 50), 2),
            "p95_ms": round(percentile(kw_ms_list, 95), 2),
            "max_ms": round(max(kw_ms_list), 2),
            "mean_ms": round(statistics.mean(kw_ms_list), 2),
        },
        "scores_raw": {
            "turn_recall_at_1": round(sum(t_h1_raw) / n, 4),
            "turn_recall_at_1_ci": compute_bootstrap_ci(t_h1_raw),
            "turn_recall_at_5": round(sum(t_h5_raw) / n, 4),
            "turn_recall_at_5_ci": compute_bootstrap_ci(t_h5_raw),
            "turn_mrr": round(sum(t_mrr_raw) / n, 4),
            "turn_mrr_ci": compute_bootstrap_ci(t_mrr_raw),
            "sess_recall_at_1": round(sum(s_h1_raw) / n, 4),
            "sess_recall_at_1_ci": compute_bootstrap_ci(s_h1_raw),
            "sess_recall_at_5": round(sum(s_h5_raw) / n, 4),
            "sess_recall_at_5_ci": compute_bootstrap_ci(s_h5_raw),
            "sess_mrr": round(sum(s_mrr_raw) / n, 4),
            "sess_mrr_ci": compute_bootstrap_ci(s_mrr_raw),
        },
        "scores_kw": {
            "turn_recall_at_1": round(sum(t_h1_kw) / n, 4),
            "turn_recall_at_5": round(sum(t_h5_kw) / n, 4),
            "turn_mrr": round(sum(t_mrr_kw) / n, 4),
            "sess_recall_at_1": round(sum(s_h1_kw) / n, 4),
            "sess_recall_at_5": round(sum(s_h5_kw) / n, 4),
            "sess_mrr": round(sum(s_mrr_kw) / n, 4),
        },
        "gates": {
            "known_item_pass_count": ki_success,
            "known_item_pass_pct": round(ki_success / n * 100, 1),
            "long_turn_pass_count": lt_success,
            "long_turn_pass_pct": round(lt_success / n * 100, 1),
        },
        "disk": {
            "mean_data_mb": round(statistics.mean(data_mb_list), 2),
            "mean_model_mb": round(statistics.mean(model_mb_list), 2),
            "mean_total_mb": round(statistics.mean(total_mb_list), 2),
            "sample_top_files": results[0]["disk"].get("top_files", []),
        },
        "ram": {
            "mean_peak_rss_mb": round(statistics.mean(ram_mb_list), 2),
            "max_peak_rss_mb": round(max(ram_mb_list), 2),
        },
        "discrepancies": dt_flags,
        "type_breakdown": qtype_summary,
        "per_question_results": results,
    }


def compute_paired_differences(
    res_a: list[dict[str, Any]],
    res_b: list[dict[str, Any]],
    metric_key: str,
    sub_key: Optional[str] = None,
) -> tuple[float, list[float]]:
    diffs = []
    for a, b in zip(res_a, res_b):
        va = a[metric_key][sub_key] if sub_key else a[metric_key]
        vb = b[metric_key][sub_key] if sub_key else b[metric_key]
        diffs.append(va - vb)
    mean_diff = round(statistics.mean(diffs), 4) if diffs else 0.0
    ci = compute_bootstrap_ci(diffs)
    return mean_diff, ci


def run_full_pilot(
    limit: Optional[int] = None,
    skip_tools: Optional[list[str]] = None,
    only_tools: Optional[list[str]] = None,
) -> dict[str, Any]:
    skip_set = set(skip_tools or [])
    only_set = set(only_tools) if only_tools else None
    logger.info("Starting Official 20-Question LongMemEval-S Pilot Evaluation")

    # 1. Assert sleep-proofing and exclusivity
    power_readiness = assert_power_and_sleep_readiness()
    logger.info("Power and sleep readiness asserted: %s", power_readiness)

    cleanup_leftover_processes()
    assert_system_exclusive(cpu_threshold_pct=15.0, sample_duration_sec=5.0)

    # 2. Load pilot dataset
    with open(PILOT_DATASET_PATH, "r", encoding="utf-8") as f:
        pilot_raw = json.load(f)

    if limit:
        pilot_raw = pilot_raw[:limit]

    logger.info("Preparing haystacks for %d questions with opaque tags at START...", len(pilot_raw))
    prepared_questions = [prepare_question_haystack(q, idx) for idx, q in enumerate(pilot_raw)]

    total_turns_all = sum(q["total_haystack_turns"] for q in prepared_questions)
    logger.info("Total turns across %d questions: %d (mean: %.1f turns/q)", len(prepared_questions), total_turns_all, total_turns_all / len(prepared_questions))

    out_file = RESULTS_DIR / "pilot_20_results.json"
    tool_results: dict[str, Any] = {}
    if out_file.exists():
        try:
            with open(out_file, "r", encoding="utf-8") as f:
                prev = json.load(f)
                if prev.get("metadata", {}).get("questions_evaluated") == len(prepared_questions):
                    tool_results = prev.get("tool_results", {})
        except Exception:
            pass

    run_start_time = datetime.datetime.now(datetime.timezone.utc)

    def should_run(tool_id: str) -> bool:
        if only_set is not None:
            return tool_id in only_set
        return tool_id not in skip_set

    def save_current_state():
        cur_end = datetime.datetime.now(datetime.timezone.utc)
        cur_kp = query_kernel_power_events(run_start_time, cur_end)
        projs = {}
        for key, agg in tool_results.items():
            mean_t_q = agg["mean_write_sec_per_q"] + (agg["query_latency_raw"]["mean_ms"] / 1000.0)
            total_sec_seq = mean_t_q * 500.0
            projs[key] = {
                "tool_name": agg["tool_name"],
                "tool_class": agg["tool_class"],
                "mean_sec_per_q": round(mean_t_q, 3),
                "seq_500_hours": round(total_sec_seq / 3600.0, 2),
                "seq_500_minutes": round(total_sec_seq / 60.0, 1),
                "parallel_2w_hours": round((total_sec_seq / 2.0) / 3600.0, 2),
                "parallel_2w_minutes": round((total_sec_seq / 2.0) / 60.0, 1),
                "parallel_3w_hours": round((total_sec_seq / 3.0) / 3600.0, 2),
                "parallel_3w_minutes": round((total_sec_seq / 3.0) / 60.0, 1),
            }
        payload = {
            "metadata": {
                "timestamp": cur_end.isoformat(),
                "questions_evaluated": len(prepared_questions),
                "total_turns": total_turns_all,
                "mean_turns_per_q": round(total_turns_all / len(prepared_questions), 1),
                "hardware": "AMD Ryzen 7 5700U (8 cores, 16 threads), 16 GB RAM",
                "os": "Windows 11 Home 10.0.26100",
                "python_version": sys.version.split()[0],
                "power_status_start": power_readiness,
                "kernel_power_sleep_events": cur_kp,
            },
            "tool_results": tool_results,
            "projections_500": projs,
        }
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    with ExecutionStateGuard():
        # -------------------------------------------------------------
        # CLASS A: SDK / IN-PROCESS
        # -------------------------------------------------------------
        if should_run("mesh_sdk"):
            logger.info("=== Running Class A: Mesh SDK ===")
            res_mesh_sdk = []
            for idx, q_data in enumerate(prepared_questions):
                r = eval_mesh_sdk_question(q_data, idx)
                res_mesh_sdk.append(r)
                logger.info("  [Mesh SDK Q %d/%d] write=%.2fs raw_query=%.1fms (turn_h1=%.0f)", idx + 1, len(prepared_questions), r["write_seconds"], r["raw_ms"], r["raw_scores"]["turn_hit_at_1"])
            tool_results["mesh_sdk"] = aggregate_tool_results("Mesh SDK", "Class A", res_mesh_sdk, prepared_questions)
            save_current_state()

        if should_run("mem0_sdk"):
            logger.info("=== Running Class A: Mem0 SDK (Python 3.12 worker) ===")
            res_mem0 = run_mem0_pilot_all(prepared_questions)
            tool_results["mem0_sdk"] = aggregate_tool_results("Mem0 SDK", "Class A", res_mem0, prepared_questions)
            save_current_state()

        # -------------------------------------------------------------
        # CLASS B: CLI SUBPROCESS
        # -------------------------------------------------------------
        if should_run("mesh_cli_daemon"):
            logger.info("=== Running Class B: Mesh CLI (Daemon) ===")
            res_mesh_daemon = []
            for idx, q_data in enumerate(prepared_questions):
                r = eval_mesh_cli_daemon_question(q_data, idx)
                res_mesh_daemon.append(r)
                logger.info("  [Mesh Daemon Q %d/%d] write=%.2fs raw_query=%.1fms", idx + 1, len(prepared_questions), r["write_seconds"], r["raw_ms"])
            tool_results["mesh_cli_daemon"] = aggregate_tool_results("Mesh CLI (Daemon)", "Class B", res_mesh_daemon, prepared_questions)
            save_current_state()

        if should_run("mesh_cli_cold"):
            logger.info("=== Running Class B: Mesh CLI (Cold) ===")
            res_mesh_cold = []
            for idx, q_data in enumerate(prepared_questions):
                r = eval_mesh_cli_cold_question(q_data, idx)
                res_mesh_cold.append(r)
                logger.info("  [Mesh Cold Q %d/%d] write=%.2fs raw_query=%.1fms", idx + 1, len(prepared_questions), r["write_seconds"], r["raw_ms"])
            tool_results["mesh_cli_cold"] = aggregate_tool_results("Mesh CLI (Cold)", "Class B", res_mesh_cold, prepared_questions)
            save_current_state()

        if should_run("auxly_cli"):
            logger.info("=== Running Class B: Auxly CLI (1 file per turn) ===")
            res_auxly_cli = []
            for idx, q_data in enumerate(prepared_questions):
                r = eval_auxly_cli_question(q_data, idx)
                res_auxly_cli.append(r)
                logger.info("  [Auxly CLI Q %d/%d] write=%.2fs raw_query=%.1fms", idx + 1, len(prepared_questions), r["write_seconds"], r["raw_ms"])
            tool_results["auxly_cli"] = aggregate_tool_results("Auxly CLI", "Class B", res_auxly_cli, prepared_questions)
            save_current_state()

        if should_run("bm_cli"):
            logger.info("=== Running Class B: Basic Memory CLI ===")
            res_bm_cli = []
            for idx, q_data in enumerate(prepared_questions):
                r = eval_bm_cli_question(q_data, idx)
                res_bm_cli.append(r)
                logger.info("  [BM CLI Q %d/%d] write=%.2fs raw_query=%.1fms", idx + 1, len(prepared_questions), r["write_seconds"], r["raw_ms"])
            tool_results["bm_cli"] = aggregate_tool_results("Basic Memory CLI", "Class B", res_bm_cli, prepared_questions)
            save_current_state()

        # -------------------------------------------------------------
        # CLASS C: MCP STDIO
        # -------------------------------------------------------------
        if should_run("mesh_mcp"):
            logger.info("=== Running Class C: Mesh MCP ===")
            res_mesh_mcp = []
            for idx, q_data in enumerate(prepared_questions):
                r = eval_mesh_mcp_question(q_data, idx)
                res_mesh_mcp.append(r)
                logger.info("  [Mesh MCP Q %d/%d] write=%.2fs raw_query=%.1fms", idx + 1, len(prepared_questions), r["write_seconds"], r["raw_ms"])
            tool_results["mesh_mcp"] = aggregate_tool_results("Mesh MCP", "Class C", res_mesh_mcp, prepared_questions)
            save_current_state()

        if should_run("auxly_mcp"):
            logger.info("=== Running Class C: Auxly MCP (1 file per turn) ===")
            res_auxly_mcp = []
            for idx, q_data in enumerate(prepared_questions):
                r = eval_auxly_mcp_question(q_data, idx)
                res_auxly_mcp.append(r)
                logger.info("  [Auxly MCP Q %d/%d] write=%.2fs raw_query=%.1fms", idx + 1, len(prepared_questions), r["write_seconds"], r["raw_ms"])
            tool_results["auxly_mcp"] = aggregate_tool_results("Auxly MCP", "Class C", res_auxly_mcp, prepared_questions)
            save_current_state()

        if should_run("bm_mcp"):
            logger.info("=== Running Class C: Basic Memory MCP ===")
            res_bm_mcp = []
            for idx, q_data in enumerate(prepared_questions):
                r = eval_bm_mcp_question(q_data, idx)
                res_bm_mcp.append(r)
                logger.info("  [BM MCP Q %d/%d] write=%.2fs raw_query=%.1fms", idx + 1, len(prepared_questions), r["write_seconds"], r["raw_ms"])
            tool_results["bm_mcp"] = aggregate_tool_results("Basic Memory MCP", "Class C", res_bm_mcp, prepared_questions)
            save_current_state()

    run_end_time = datetime.datetime.now(datetime.timezone.utc)
    save_current_state()
    logger.info("All selected tools completed. Results saved to %s", out_file)

    run_end_time = datetime.datetime.now(datetime.timezone.utc)

    # 3. Post-run Kernel-Power audit
    kp_events = query_kernel_power_events(run_start_time, run_end_time)
    logger.info("Post-run Kernel-Power sleep/resume audit: %d events found in run window", len(kp_events))
    if kp_events:
        logger.error("CONTAMINATED RUN: Kernel-power sleep/resume events detected during run: %s", kp_events)

    # 4. Projections for full 500 questions
    projections = {}
    for key, agg in tool_results.items():
        mean_t_q = agg["mean_write_sec_per_q"] + (agg["query_latency_raw"]["mean_ms"] / 1000.0)
        total_sec_seq = mean_t_q * 500.0
        projections[key] = {
            "tool_name": agg["tool_name"],
            "tool_class": agg["tool_class"],
            "mean_sec_per_q": round(mean_t_q, 3),
            "seq_500_hours": round(total_sec_seq / 3600.0, 2),
            "seq_500_minutes": round(total_sec_seq / 60.0, 1),
            "parallel_2w_hours": round((total_sec_seq / 2.0) / 3600.0, 2),
            "parallel_2w_minutes": round((total_sec_seq / 2.0) / 60.0, 1),
            "parallel_3w_hours": round((total_sec_seq / 3.0) / 3600.0, 2),
            "parallel_3w_minutes": round((total_sec_seq / 3.0) / 60.0, 1),
        }

    # 5. Parity assertions & audits
    mesh_parity_table = []
    has_all_mesh = all(k in tool_results for k in ["mesh_sdk", "mesh_cli_daemon", "mesh_cli_cold", "mesh_mcp"])
    if has_all_mesh:
        sdk_res = tool_results["mesh_sdk"]["per_question_results"]
        daemon_res = tool_results["mesh_cli_daemon"]["per_question_results"]
        cold_res = tool_results["mesh_cli_cold"]["per_question_results"]
        mcp_res = tool_results["mesh_mcp"]["per_question_results"]

        for q_i in range(len(sdk_res)):
            s_ids = sdk_res[q_i].get("raw_ids", [])
            d_ids = daemon_res[q_i].get("raw_ids", [])
            c_ids = cold_res[q_i].get("raw_ids", [])
            m_ids = mcp_res[q_i].get("raw_ids", [])

            eq_sd = (s_ids == d_ids)
            eq_sc = (s_ids == c_ids)
            eq_sm = (s_ids == m_ids)
            all_eq = (eq_sd and eq_sc and eq_sm)

            mesh_parity_table.append({
                "question": q_i,
                "sdk_ids": s_ids[:5],
                "daemon_ids": d_ids[:5],
                "cold_ids": c_ids[:5],
                "mcp_ids": m_ids[:5],
                "sdk_matches_daemon": eq_sd,
                "sdk_matches_cold": eq_sc,
                "sdk_matches_mcp": eq_sm,
                "all_interfaces_equal": all_eq,
            })
            if not all_eq:
                logger.error("MESH PARITY FAILURE on Q%d: sdk=%s, daemon=%s, cold=%s, mcp=%s", q_i, s_ids[:3], d_ids[:3], c_ids[:3], m_ids[:3])

    bm_parity_table = []
    has_all_bm = all(k in tool_results for k in ["bm_cli", "bm_mcp"])
    if has_all_bm:
        cli_bm = tool_results["bm_cli"]["per_question_results"]
        mcp_bm = tool_results["bm_mcp"]["per_question_results"]
        for q_i in range(len(cli_bm)):
            c_t = cli_bm[q_i].get("raw_titles", [])
            m_t = mcp_bm[q_i].get("raw_titles", [])
            bm_parity_table.append({
                "question": q_i,
                "cli_titles": c_t[:5],
                "mcp_titles": m_t[:5],
                "equal": (c_t == m_t),
            })

    # Ingestion integrity summary
    ingestion_integrity = {}
    for t_k, t_agg in tool_results.items():
        pq = t_agg.get("per_question_results", [])
        total_sub = sum(r.get("ingest_accounting", {}).get("total_submitted", 0) for r in pq)
        total_ing = sum(r.get("ingest_accounting", {}).get("ingested", 0) for r in pq)
        total_rej = sum(r.get("ingest_accounting", {}).get("rejected", 0) for r in pq)
        total_fail = sum(r.get("ingest_accounting", {}).get("failed", 0) for r in pq)
        gold_statuses = [r.get("ingest_accounting", {}).get("gold_turn_status", "unknown") for r in pq]
        all_gold_ok = all(s == "all_ingested" for s in gold_statuses)
        ingestion_integrity[t_k] = {
            "total_submitted": total_sub,
            "total_ingested": total_ing,
            "total_rejected": total_rej,
            "total_failed": total_fail,
            "gold_turns_ok": all_gold_ok,
            "sample_rejection_reasons": list(set(
                reason for r in pq for reason in r.get("ingest_accounting", {}).get("rejection_reasons", [])
            ))[:5],
        }

    # 5. Compile output payload
    output_payload = {
        "metadata": {
            "timestamp": run_end_time.isoformat(),
            "questions_evaluated": len(prepared_questions),
            "total_turns": total_turns_all,
            "mean_turns_per_q": round(total_turns_all / len(prepared_questions), 1),
            "hardware": "AMD Ryzen 7 5700U (8 cores, 16 threads), 16 GB RAM",
            "os": "Windows 11 Home 10.0.26100",
            "python_version": sys.version.split()[0],
            "power_status_start": power_readiness,
            "kernel_power_sleep_events": kp_events,
            "config_disclosures": {
                "Mesh": {
                    "MESH_MAX_CONTENT_LENGTH": 131072,
                    "MESH_SYNC_ONNX": 1,
                    "MESH_CLI_FORCE_ONNX": 1,
                    "default_behavior_note": "With default MESH_MAX_CONTENT_LENGTH=16384, exactly 2 turns across the 20 questions exceed 16k chars (Q3 distractor turn of 23,821 chars and Q17 distractor turn of 37,208 chars). Neither is a gold turn; 0 gold turns are rejected under defaults.",
                },
                "Mem0": {
                    "infer": False,
                    "model": "sentence-transformers/all-MiniLM-L6-v2 (cached in HuggingFace Hub, ~487.5 MB)",
                },
                "BasicMemory": {
                    "embedding_model": "BAAI/bge-small-en-v1.5 (FastEmbed cached, ~65 MB)",
                    "reindex_command": "basic-memory reindex -p <proj> --full",
                    "search_type": "hybrid / semantic",
                },
                "Auxly": {
                    "local_engine": "Exact contiguous substring search over vault markdown files; falls back to substring search when no external cloud LLM embedding model is configured.",
                },
            },
            "mesh_parity_table": mesh_parity_table,
            "basic_memory_parity_table": bm_parity_table,
            "ingestion_integrity": ingestion_integrity,
            "inspections_top5_raw": {
                t_k: [
                    {
                        "question_idx": q_i,
                        "question": prepared_questions[q_i]["question"],
                        "gold_turn_tags": list(prepared_questions[q_i]["gold_turn_tags"]),
                        "raw_candidates": tool_results[t_k]["per_question_results"][q_i].get("raw_results_payload", [])[:5],
                    }
                    for q_i in range(min(5, len(prepared_questions)))
                ]
                for t_k in tool_results
            },
        },
        "tool_results": tool_results,
        "projections_500": projections,
    }

    out_file = RESULTS_DIR / "pilot_20_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2)
    logger.info("Pilot results saved to %s", out_file)

    return output_payload


def main():
    parser = argparse.ArgumentParser(description="Official LongMemEval-S 20-Question Pilot Runner")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of questions (e.g. for testing)")
    parser.add_argument("--skip", nargs="*", default=[], help="Tools to skip (e.g. mem0_sdk bm_cli)")
    parser.add_argument("--tools", nargs="*", default=None, help="Specific tools to run exclusively")
    args = parser.parse_args()

    _ = run_full_pilot(limit=args.limit, skip_tools=args.skip, only_tools=args.tools)
    print("\nPilot evaluation run completed successfully.")


if __name__ == "__main__":
    main()

