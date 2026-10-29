"""
benchmarks/run_mem0_pilot_worker.py — Mem0 SDK dedicated worker for LongMemEval pilot.
Runs in Python 3.12 venv where mem0 and fastembed are installed.
"""
from __future__ import annotations

import gc
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

try:
    import psutil
except ImportError:
    psutil = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

KNOWN_ITEM_QUERY = "cerulean observatory celestial frequency"
KNOWN_ITEM_TAG = "[turn:00000]"
KNOWN_ITEM_CONTENT = f"{KNOWN_ITEM_TAG} The cerulean observatory celestial frequency atop Mount Horizon recorded an anomalous 432.85 MHz on stellar quadrant nine."
KNOWN_ITEM_ANSWER = "432.85 MHz"

LONG_TURN_QUERY = "cobalt quantum telescope astronomical phenomena"
LONG_TURN_TAG = "[turn:00001]"
LONG_TURN_CONTENT = f"{LONG_TURN_TAG} " + ("The cobalt quantum telescope observed astronomical phenomena in deep space. " * 45)


def get_process_tree_rss_mb() -> float:
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
    def __init__(self, interval: float = 0.1):
        self.interval = interval
        self.peak_mb = 0.0
        self._stop_event = None
        self._thread = None

    def _monitor(self):
        while not self._stop_event.is_set():
            rss = get_process_tree_rss_mb()
            if rss > self.peak_mb:
                self.peak_mb = rss
            time.sleep(self.interval)

    def __enter__(self):
        import threading
        self.peak_mb = get_process_tree_rss_mb()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._stop_event:
            self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        final_rss = get_process_tree_rss_mb()
        if final_rss > self.peak_mb:
            self.peak_mb = final_rss


class DualTimer:
    def __init__(self):
        self.wall_start = 0.0
        self.mono_start = 0.0
        self.wall_elapsed = 0.0
        self.mono_elapsed = 0.0
        self.discrepancy_flag = False

    def __enter__(self):
        self.wall_start = time.time()
        self.mono_start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        wall_end = time.time()
        mono_end = time.perf_counter()
        self.wall_elapsed = wall_end - self.wall_start
        self.mono_elapsed = mono_end - self.mono_start
        if abs(self.wall_elapsed - self.mono_elapsed) > max(0.5, 0.15 * self.mono_elapsed):
            self.discrepancy_flag = True


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
            if not f.is_file() or f.is_symlink():
                continue
            sz = f.stat().st_size
            rel = str(f.relative_to(p))
            file_map[rel] = round(sz / (1024 * 1024), 3)

            lower_name = f.name.lower()
            lower_path = str(f).lower()
            if "model" in lower_path or ".onnx" in lower_name:
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

    hf_cache = Path.home() / ".cache" / "huggingface" / "hub" / "models--sentence-transformers--all-MiniLM-L6-v2"
    if hf_cache.exists():
        try:
            model_bytes += sum(f.stat().st_size for f in hf_cache.rglob("*") if f.is_file())
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


def eval_one_question(mem0_cls: Any, q_data: dict[str, Any], q_idx: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix=f"pilot_mem0_{q_idx}_") as temp_dir:
        qdrant_path = os.path.join(temp_dir, "qdrant")
        os.makedirs(qdrant_path, exist_ok=True)
        config = {
            "version": "v1.1",
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "path": qdrant_path,
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

        harness_baseline_mb = get_process_tree_rss_mb()
        with ProcessTreePeakTracker() as tracker:
            mem = mem0_cls.from_config(config)

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

            # Ingest turns
            with DualTimer() as dt_ingest:
                for item in q_data["items"]:
                    is_gold = item.get("has_answer") is True
                    try:
                        mem.add(item["content"], user_id=f"user_{q_idx}", metadata={"turn_id": item["tag"]}, infer=False)
                        ingest_accounting["ingested"] += 1
                    except Exception as e:
                        ingest_accounting["failed"] += 1
                        ingest_accounting["failed_reasons"].append(str(e))
                        if is_gold:
                            ingest_accounting["gold_turn_status"] = "gold_failed"
            write_sec = dt_ingest.mono_elapsed

            # Snapshot store for isolated queries
            def make_mem(sub_dir: str):
                cfg = dict(config)
                cfg["vector_store"] = {"provider": "qdrant", "config": {"path": sub_dir, "embedding_model_dims": 384}}
                return mem0_cls.from_config(cfg)

            try:
                if hasattr(mem, "vector_store") and hasattr(mem.vector_store, "client"):
                    mem.vector_store.client.close()
            except Exception:
                pass
            del mem
            gc.collect()

            snap_ki = os.path.join(temp_dir, "qdrant_ki")
            snap_lt = os.path.join(temp_dir, "qdrant_lt")
            snap_raw = os.path.join(temp_dir, "qdrant_raw")
            snap_kw = os.path.join(temp_dir, "qdrant_kw")
            ign = shutil.ignore_patterns("*.lock", ".lock")
            shutil.copytree(qdrant_path, snap_ki, ignore=ign)
            shutil.copytree(qdrant_path, snap_lt, ignore=ign)
            shutil.copytree(qdrant_path, snap_raw, ignore=ign)
            shutil.copytree(qdrant_path, snap_kw, ignore=ign)

            # Known item check on snapshot
            mem_ki = make_mem(snap_ki)
            ki_res = mem_ki.search(KNOWN_ITEM_QUERY, filters={"user_id": f"user_{q_idx}"}, limit=5)
            ki_rank = None
            for r, cand in enumerate(ki_res.get("results", []), start=1):
                tid = cand.get("metadata", {}).get("turn_id", "")
                mem_txt = cand.get("memory", "")
                tag = extract_turn_identifier(tid, mem_txt)
                if tag == KNOWN_ITEM_TAG or KNOWN_ITEM_ANSWER.lower() in mem_txt.lower():
                    ki_rank = r
                    break
            try:
                mem_ki.vector_store.client.close()
            except Exception:
                pass
            del mem_ki

            # Long turn check on snapshot
            mem_lt = make_mem(snap_lt)
            lt_res = mem_lt.search(LONG_TURN_QUERY, filters={"user_id": f"user_{q_idx}"}, limit=5)
            lt_tag_found = any(
                extract_turn_identifier(c.get("metadata", {}).get("turn_id", ""), c.get("memory", "")) == LONG_TURN_TAG
                for c in lt_res.get("results", [])
            )
            try:
                mem_lt.vector_store.client.close()
            except Exception:
                pass
            del mem_lt

            # Query Raw on snapshot
            mem_raw = make_mem(snap_raw)
            with DualTimer() as dt_raw:
                raw_res = mem_raw.search(q_data["question"], filters={"user_id": f"user_{q_idx}"}, limit=10)
            raw_cands = [
                (c.get("metadata", {}).get("turn_id", ""), c.get("memory", ""))
                for c in raw_res.get("results", [])
            ]
            raw_scores = score_candidates(
                raw_cands,
                set(q_data["gold_turn_tags"]),
                set(q_data["gold_session_tags"]),
                q_data["tag_to_session"],
            )
            try:
                mem_raw.vector_store.client.close()
            except Exception:
                pass
            del mem_raw

            # Query Keyword on snapshot
            mem_kw = make_mem(snap_kw)
            with DualTimer() as dt_kw:
                kw_res = mem_kw.search(q_data["question_kw"], filters={"user_id": f"user_{q_idx}"}, limit=10)
            kw_cands = [
                (c.get("metadata", {}).get("turn_id", ""), c.get("memory", ""))
                for c in kw_res.get("results", [])
            ]
            kw_scores = score_candidates(
                kw_cands,
                set(q_data["gold_turn_tags"]),
                set(q_data["gold_session_tags"]),
                q_data["tag_to_session"],
            )
            try:
                mem_kw.vector_store.client.close()
            except Exception:
                pass
            del mem_kw

            disk_info = checkpoint_and_analyze_disk(temp_dir)
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
            "disk": disk_info,
            "ram_mb": tool_peak_ram,
            "harness_baseline_ram_mb": harness_baseline_mb,
            "ingest_accounting": ingest_accounting,
            "top_candidates_raw": [c[1][:100] for c in raw_cands[:3]],
            "raw_results_payload": [
                {"id": c.get("metadata", {}).get("turn_id", ""), "memory": c.get("memory", "")[:200], "score": c.get("score")}
                for c in raw_res.get("results", [])[:5]
            ],
        }


def main():
    if len(sys.argv) < 3:
        print("Usage: run_mem0_pilot_worker.py <questions_json_file> <output_json_file>", file=sys.stderr)
        sys.exit(1)

    in_file = Path(sys.argv[1])
    out_file = Path(sys.argv[2])

    with open(in_file, "r", encoding="utf-8") as f:
        prepared_questions = json.load(f)

    os.environ["MEM0_TELEMETRY"] = "false"
    from mem0 import Memory

    results = []
    print(f"Mem0 worker running {len(prepared_questions)} questions...", flush=True)
    for idx, q_data in enumerate(prepared_questions):
        t0 = time.perf_counter()
        res = eval_one_question(Memory, q_data, idx)
        elapsed = time.perf_counter() - t0
        print(f"  [Q {idx+1}/{len(prepared_questions)}] done in {elapsed:.2f}s (write: {res['write_seconds']:.2f}s, query: {res['raw_ms']:.1f}ms)", flush=True)
        results.append(res)

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"Mem0 worker completed {len(results)} questions. Output saved to {out_file}", flush=True)


if __name__ == "__main__":
    main()
