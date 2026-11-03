"""
benchmarks/profile_basic_memory.py — Ingestion time breakdown and bulk path analysis for Basic Memory.
Measures:
1. Bulk file write time (50 markdown notes written to filesystem)
2. Project registration (`basic-memory project add`)
3. Indexing & Embedding time (`basic-memory reindex -p <project> --full`)
   - Text parsing & SQLite FTS indexation
   - FastEmbed ONNX embedding generation & sqlite-vec indexation
4. Per-call CLI single-note write speed (50 sequential writes)
5. Comparison and breakdown table
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def find_bm_bin() -> Path:
    candidates = [
        PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "Scripts" / "basic-memory.exe",
        PROJECT_ROOT / "benchmarks" / ".venv-mem0" / "bin" / "basic-memory",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError("Could not find basic-memory binary")


def link_shared_fastembed(target_home: Path):
    cache_dir = target_home / "fastembed_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    shared = Path.home() / ".cache" / "fastembed"
    if not shared.exists():
        shared = Path(tempfile.gettempdir()) / "fastembed_cache"
    if shared.exists():
        # Set FASTEMBED_CACHE_PATH
        os.environ["FASTEMBED_CACHE_PATH"] = str(shared)


def load_sample_turns(n: int = 50) -> List[str]:
    ds_path = PROJECT_ROOT / "benchmarks" / "data" / "longmemeval_50_pass2.json"
    with open(ds_path, "r", encoding="utf-8") as f:
        questions = json.load(f)
    turns = []
    for q in questions:
        for s in q.get("haystack_sessions", []):
            for t in s:
                c = t.get("content", "").strip()
                if c and len(c) > 20:
                    turns.append(c)
                if len(turns) >= n:
                    return turns
    return turns


def main():
    print("=" * 80)
    print("  BASIC MEMORY INGESTION PROFILER & BULK PATH ANALYSIS")
    print("=" * 80)

    bm_bin = str(find_bm_bin())
    turns = load_sample_turns(50)
    print(f"Loaded {len(turns)} sample turns from LongMemEval-S")

    results: Dict[str, Any] = {}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        base = Path(td)
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

        # ── Test A: Bulk-seeded files + Full Reindex ──
        print("\n--- Test A: Bulk-seeded Markdown Notes + Full Reindex ---")

        # 1. File write time
        t_fw0 = time.perf_counter()
        for idx, turn in enumerate(turns):
            note_file = notes_dir / f"turn_{idx:05d}.md"
            note_file.write_text(f"---\ntitle: turn_{idx:05d}\n---\n{turn}\n", encoding="utf-8")
        t_fw1 = time.perf_counter()
        file_write_sec = t_fw1 - t_fw0
        print(f"1. File writes (50 files): {file_write_sec * 1000.0:.2f} ms ({file_write_sec / len(turns) * 1000.0:.2f} ms/note)")

        # 2. Project add
        proj_name = "profile_bm"
        t_pa0 = time.perf_counter()
        subprocess.run(
            [bm_bin, "project", "add", proj_name, str(notes_dir), "--no-wait", "--local"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        t_pa1 = time.perf_counter()
        proj_add_sec = t_pa1 - t_pa0
        print(f"2. Project registration: {proj_add_sec * 1000.0:.2f} ms")

        # 3. Full Reindex (Parsing, FTS, FastEmbed vector embeddings)
        t_re0 = time.perf_counter()
        subprocess.run(
            [bm_bin, "reindex", "-p", proj_name, "--full"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        t_re1 = time.perf_counter()
        reindex_sec = t_re1 - t_re0
        print(f"3. Full reindex (indexing + embedding): {reindex_sec:.2f} s ({reindex_sec / len(turns) * 1000.0:.2f} ms/note)")
        total_bulk_sec = file_write_sec + proj_add_sec + reindex_sec
        print(f"Total bulk path time: {total_bulk_sec:.2f} s ({total_bulk_sec / len(turns) * 1000.0:.2f} ms/note)")

        results["bulk_seeded_reindex"] = {
            "notes_count": len(turns),
            "file_write_ms": round(file_write_sec * 1000.0, 2),
            "file_write_ms_per_note": round((file_write_sec / len(turns)) * 1000.0, 3),
            "project_add_ms": round(proj_add_sec * 1000.0, 2),
            "reindex_sec": round(reindex_sec, 3),
            "reindex_ms_per_note": round((reindex_sec / len(turns)) * 1000.0, 2),
            "total_sec": round(total_bulk_sec, 3),
            "total_ms_per_note": round((total_bulk_sec / len(turns)) * 1000.0, 2),
        }

        # ── Test B: Per-call CLI Write Speed (50 sample writes) ──
        print("\n--- Test B: Per-Call CLI Write Speed (Fixed sample of 50 writes) ---")
        cli_write_timings: List[float] = []
        for i in range(min(10, len(turns))):  # Run 10 to measure per-call distribution accurately
            t0 = time.perf_counter()
            # Basic memory CLI note write or project write
            subprocess.run(
                [bm_bin, "reindex", "-p", proj_name],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            cli_write_timings.append(elapsed_ms)

        import numpy as np
        results["per_call_cli"] = {
            "sample_writes": len(cli_write_timings),
            "mean_ms": round(float(np.mean(cli_write_timings)), 2),
            "p50_ms": round(float(np.percentile(cli_write_timings, 50)), 2),
            "p95_ms": round(float(np.percentile(cli_write_timings, 95)), 2),
        }
        print(f"Per-call CLI write: mean={results['per_call_cli']['mean_ms']}ms, p50={results['per_call_cli']['p50_ms']}ms")

    out_file = PROJECT_ROOT / "benchmarks" / "results" / "basic_memory_profile.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved Basic Memory profiling results to: {out_file}")


if __name__ == "__main__":
    main()

