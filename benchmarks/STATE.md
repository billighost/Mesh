# Benchmark & Evidence State (Pass 2 Bare-Metal Measurements)

Last updated: November 2, 2026.
Commit: Pass 2 Evidence Run (tag: `benchmark-v1-pass2`).
Hardware: AMD Ryzen 7 5700U (8 cores, 16 threads), 16 GB DDR4 RAM, NVMe PCIe SSD, Windows 11 Home 64-bit, Python 3.14.0 GA.

---

## 1. Quality & Integrity Gate Summary

| Gate / Quality Check | Command | Result | Status |
| :--- | :--- | :--- | :---: |
| **Full Unit & Integration Suite** | `pytest -q` | **432 passed, 3 skipped** (148.43 s) | 🟢 **PASSED** |
| **Code Freeze Verification** | `git diff benchmark-v1 -- mesh/` | **Empty diff** (bit-for-bit identical) | 🟢 **PASSED** |
| **Ruff Linter & Formatter** | `ruff check .` | **All checks passed** (0 errors) | 🟢 **PASSED** |
| **Documentation Check** | `python scripts/check_docs.py` | **Consistency verified** (v0.8.3) | 🟢 **PASSED** |
| **Skill & Rules Check** | `python scripts/check_skill.py` | **17 tools verified, <=250 lines** | 🟢 **PASSED** |
| **Static Type Analysis** | `mypy mesh` | **Success: no issues in 25 files** | 🟢 **PASSED** |
| **Security SAST Audit** | `bandit -ll -c pyproject.toml -r mesh` | **0 High, 0 Medium issues** | 🟢 **PASSED** |
| **Vulnerability Audit** | `pip-audit` | **No known vulnerabilities found** | 🟢 **PASSED** |

---

## 2. Pass 2 Bare-Metal Benchmark Results

### A. Scale Benchmark (3 Independent Repeats, Discarded Warm-up)
- **1,000 Memories:** Ingest: **475.7 wps** `[33.8, 591.9]`, Dense SIMD Recall: **7.73 ms p50** `[6.38, 7.85]`, Pure DB: **3.05 MB**, Peak RAM: **299.3 MB**.
- **5,000 Memories:** Ingest: **33.5 wps** `[19.5, 41.6]`, Dense SIMD Recall: **41.20 ms p50** `[28.47, 114.74]`, Pure DB: **16.83 MB**, Peak RAM: **652.9 MB**.
- **10,000 Memories:** Ingest: **38.1 wps** `[34.1, 41.8]`, Dense SIMD Recall: **56.21 ms p50** `[46.69, 62.90]`, Pure DB: **32.24 MB**, Peak RAM: **1255.1 MB**.
- **1,000 Turns (LongMemEval-S Realistic Text):** Ingest: **97.4 wps** `[5.0, 122.3]`, Dense SIMD Recall: **24.36 ms p50** `[22.02, 47.17]`, Pure DB: **12.29 MB**, Peak RAM: **2102.2 MB**.

### B. Concurrency Extensions Suite
- **5,000-Memory Integrity Test:** Ingested 5,000 memories across 16 threads in 43.13s (**115.9 writes/s**). `PRAGMA integrity_check = ok`, exactly 5,001 unique records, zero duplicates, known-item recall passed.
- **Multi-Agent Process Scaling:** 2 agents: **6.5 wps** / **12.4 rps**; 5 agents: **3.3 wps** / **13.1 rps**; 10 agents: **2.7 wps** / **11.6 rps**. 100% database integrity verified.
- **Lock Profiling:** Uncontended hold time: **2.62 ms**. Windows `msvcrt.locking` contention explains 5 wps multi-process vs 106+ wps multi-thread.

### C. B2 RAM & B3 Disk Breakdown
- **RAM Peak:** Mesh SDK: **157.64 MB** vs Mem0 SDK: **328.40 MB**. Mesh CLI Cold: **26.45 MB**; Mesh Daemon: **145.00 MB**; Basic Memory CLI: **287.07 MB**; Auxly CLI: **1.64 MB**.
- **Disk Accounting:** Data files: **20.52 MB**; Static model assets: **86.64 MB** (86.2 MB `model.onnx` + 0.44 MB `tokenizer.json`). Total directory size: **107.16 MB**.

### D. B4 Ingestion & B5 Basic Memory Profiling
- **Mesh Ingestion Latency:** Short (<100c): **4.86 ms** (Mem0: 22.77 ms); Medium (200-500c): **27.13 ms** (Mem0: 74.79 ms); Long (1k-3kc): **30.56 ms** (Mem0: 145.13 ms); Very Long (>4kc): **74.66 ms** (Mem0: 156.50 ms).
- **Basic Memory Profiling:** 50 file writes: **108.23 ms** (2.16 ms/file); project add: **23.80 s**; full reindex: **80.81 s** (1,616 ms/file); per-call CLI write latency: mean **13,796 ms** (p50: 12,215 ms).

---
