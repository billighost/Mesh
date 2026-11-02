# Benchmark & Evidence State (Checkpoint P2-B Bare-Metal Measurements)

Last updated: November 2, 2026.
Commit / Tag Target: `benchmark-v1-pass2b`.
Branch: `bench/pass2-results`.
Hardware:
- Linux Runner: GitHub-hosted `ubuntu-24.04`, 4 vCPU (Intel/AMD x86-64), 16 GB RAM, 14 GB SSD.
- Windows Laptop: AMD Ryzen 7 5700U (8 cores, 16 threads, 1.80 GHz base, up to 4.30 GHz boost), 16 GB DDR4 RAM, NVMe PCIe SSD, Windows 11 Home 64-bit, Python 3.14.0 GA.

---

## 1. Quality & Integrity Gate Summary

| Gate / Quality Check | Command | Result | Status |
| :--- | :--- | :--- | :---: |
| **Code Freeze Verification** | `git diff benchmark-v1 HEAD -- mesh/` | **Empty diff** (0 lines changed, bit-for-bit identical) | 🟢 **PASSED** |
| **Traceability Gate** | `python scripts/check_docs.py` | **100% cited, valid raw JSON files & keys, pilot disclosures** | 🟢 **PASSED** |
| **Skill & Rules Gate** | `python scripts/check_skill.py` | **17 tools verified, rules block <=250 lines byte-identical** | 🟢 **PASSED** |
| **Ruff Linter & Formatter** | `ruff check .` | **All checks passed** (0 errors) | 🟢 **PASSED** |
| **Full Unit & Integration Suite** | `pytest -q` | **432 passed, 3 skipped** | 🟢 **PASSED** |
| **Daemon HTTP Recall Gate**| `pytest tests/test_daemon_cli_performance.py` | **PASSED** (asserts HTTP delegation, no in-process model load) | 🟢 **PASSED** |
| **Static Type Analysis** | `mypy mesh` | **Success: no issues in 25 files** | 🟢 **PASSED** |
| **Security SAST Audit** | `bandit -ll -c pyproject.toml -r mesh` | **0 High, 0 Medium issues** | 🟢 **PASSED** |
| **Vulnerability Audit** | `pip-audit` | **No known vulnerabilities found** | 🟢 **PASSED** |

---

## 2. Pass 2 Bare-Metal Diagnostic Root Cause Resolutions

### A. Scale Benchmark (Isolated Subprocess Repeats)
*Source: `benchmarks/results/pass2/scale_benchmark_clean.json`*
- **Isolation Fix:** Every repeat runs in a fresh, dedicated Python subprocess with a newly verified-empty directory (`assert len(os.listdir(tmp_dir)) == 0`). This completely eliminates the heap accumulation (previously 2.1 GB) and store reuse anomalies (previously 199 s vs 8 s).
- **1,000 Synthetic Facts:** Cold headline throughput: **54.0 wps**; Median throughput: **57.1 wps** `[54.0, 58.1]`; Dense SIMD Recall p50: **7.71 ms** `[6.88, 8.44]`; Pure DB: **3.05 MB**; Peak RSS: **217.6 MB** `[214.5, 219.0]`.
- **5,000 Synthetic Facts:** Cold headline throughput: **61.7 wps**; Median throughput: **61.7 wps** `[59.9, 62.4]`; Dense SIMD Recall p50: **53.25 ms** `[48.91, 55.10]`; Pure DB: **16.83 MB**; Peak RSS: **300.6 MB** `[297.8, 303.4]`.
- **10,000 Synthetic Facts:** Cold headline throughput: **60.9 wps**; Median throughput: **60.9 wps** `[58.2, 61.5]`; Dense SIMD Recall p50: **76.52 ms** `[72.10, 81.30]`; Pure DB: **32.24 MB**; Peak RSS: **391.4 MB** `[388.2, 394.1]`.
- **1,000 Realistic Turns (LongMemEval-S Text):** Cold headline throughput: **3.59 wps** (278 ms/turn); Median throughput: **3.61 wps** `[3.59, 3.65]`; Dense SIMD Recall p50: **30.15 ms** `[28.40, 31.90]`; Pure DB: **12.29 MB**; Peak RSS: **772.6 MB** `[768.4, 775.0]`. Peak RSS is completely stable across repeats.

### B. Concurrency Extensions & Multi-Process Lock Profiling
*Source: `benchmarks/results/pass2/concurrency_multiagent.json`*
- **5,000-Memory Integrity Test:** Ingested 5,000 distinct memories across 16 concurrent threads in 65.73 s (**76.1 writes/s**). `PRAGMA integrity_check = ok`, exactly 5,001 records, 0 duplicates, known-item retrieval verified.
- **Multi-Agent Process Scaling (Separate OS Processes):**
  - 2 Agents: **7.59 writes/s** / **14.82 reads/s**, 100% database integrity.
  - 5 Agents: **6.50 writes/s** / **13.91 reads/s**, 100% database integrity.
  - 10 Agents: **4.58 writes/s** / **12.44 reads/s**, 100% database integrity.
- **Lock Profiling:** Uncontended lock hold time is **2.99 ms p50** (p95: **4.82 ms**).
- **Contention Root Cause Diagnosis:** Multi-threaded throughput is 76-106 writes/s because threads share in-memory connection pooling and SQLite internal mutexes. Multi-process throughput drops to 4.5-7.6 writes/s because cross-process concurrency relies on file-level locking (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) protecting the entire `learn()` critical section (including read-only vector deduplication queries), combined with coarse 5-50 ms exponential backoff sleep intervals that induce lock convoy formation.
- **Architectural Remediation Proposal (Scheduled for Future Release, Zero Code Changes on Current Branch):**
  1. *Granular Lock Scope:* Move read-only vector deduplication queries outside `namespace_write_lock`, scoping the advisory lock strictly to the atomic SQLite insert/update.
  2. *SQLite Native Concurrency:* Utilize SQLite WAL-mode `PRAGMA busy_timeout = 5000` to delegate lock scheduling to SQLite's native page-level lock manager.
  3. *Randomized Micro-Jitter:* Replace coarse 5-50 ms backoff loops with microsecond randomized jitter (500 µs – 2 ms).

### C. Process-Tree RAM Footprint (B2 Time-Series Sampling)
*Source: `benchmarks/results/pass2/b2_ram_timeseries.json`*
- **Methodology:** 100 ms interval sampling of entire process trees (`p.children(recursive=True)`). Zero launcher stub artifacts.
- **Bare Python Baseline:** **14.96 MB** (Python 3.14 GA).
- **Python + import mesh:** **16.14 MB** (+1.18 MB lazy-import delta).
- **Mesh SDK:** **156.29 MB** (Python runtime + loaded ONNX runtime session + embedding tensor buffers).
- **Mem0 SDK:** **279.70 MB** (Python runtime + fastembed ONNX runtime + Qdrant embedded engine).
- **Mesh Cold CLI:** **27.67 MB** (transient CLI command invocation).
- **Mesh CLI (Daemon):** **214.01 MB** (persistent background daemon process holding warmed model).
- **Basic Memory CLI:** **287.04 MB** (captures the full child Python process; the 3.42 MB figure in prior drafts was an artifact of sampling the 100 KB Windows launcher stub).
- **Auxly CLI:** **5.29 MB** (compiled Rust native binary).
- **Mesh MCP:** **51.26 MB** (FastMCP stdio server process).
- **Basic Memory MCP:** **60.61 MB** (FastMCP server + client child processes; prior 3.42 MB was also the launcher stub).
- **Auxly MCP:** **98.34 MB** (Node.js runtime + MCP server).

### D. Disk Storage Anatomy & Model Reconciliation (B3 Accounting)
*Source: `benchmarks/results/pass2/b3_disk_inventory.json`*
- **User Data Footprint (Fresh 489-turn store, WAL checkpointed):**
  - Mesh Data: **7.65 MB** (1 file: SQLite database containing memories, vectors, tags, slots, and content-free audit trail).
  - Mem0 Data: **3.31 MB** (4 files: Qdrant SQLite database + WAL + segment files).
  - Basic Memory Data: **0.41 MB** (490 files: Markdown turn files + project metadata).
  - Auxly Data: **0.42 MB** (490 files: Markdown frontmatter turn files).
- **Model Cache Reconciliation (86.66 MB vs 195.66 MB):**
  - Clean model assets on disk are exactly **86.66 MB**: `model.onnx` (**86.22 MB**) + `tokenizer.json` (**0.44 MB**).
  - The pilot v2 directory size of 195.66 MB was caused by 109.00 MB of stale temporary download artifacts (`model.tmp.16336.2464` at 68.38 MB and `model.tmp.3888.15404` at 40.63 MB) left by earlier interrupted downloads.

### E. Ingestion Latency Breakdown & Crossover (B4 Profiling)
*Source: `benchmarks/results/pass2/b4_ingestion_profile.json`*
- **Profiling Setup:** Turn-by-turn breakdown across 6 stages on Question 0 (550 turns) comparing Mesh `learn()` vs Mem0 SDK (`infer=False`).
- **Turn Length Breakdown:**
  - Short (<100c, n=54): Mesh p50 **10.59 ms** | Mem0 p50 **55.45 ms** (Mesh 5.2x faster).
  - Medium (200-500c, n=200): Mesh p50 **13.56 ms** | Mem0 p50 **82.35 ms** (Mesh 6.1x faster).
  - Long (1k-3kc, n=225): Mesh p50 **17.91 ms** | Mem0 p50 **95.22 ms** (Mesh 5.3x faster).
  - Very Long (>4kc, n=71): Mesh p50 **24.52 ms** | Mem0 p50 **110.15 ms** (Mesh 4.5x faster).
- **Internal Stage Breakdown (Mesh overall mean: 22.88 ms, p50: 15.98 ms):**
  - Guards & injection checks: **0.02 ms p50**.
  - Local ONNX embedding: **11.45 ms p50**.
  - In-store vector deduplication query: **2.35 ms p50**.
  - Slot contradiction detection: **0.01 ms p50**.
  - SQLite write transaction: **2.15 ms p50**.
- **Reconciliation Analysis (~165 ms vs ~86 ms in pilot):**
  Mem0 SDK with `infer=False` bypasses prompt injection guards, tag regex sanitization, slot tracking, and contradiction detection. Mesh executes full validation on every turn. In an empty store, Mesh processes turns in 10-15 ms. In long runs without batching where store scanning occurs against hundreds of existing turns, Mesh per-turn latency grows to ~165 ms, whereas Mem0 appends raw points without pre-write semantic verification.

### F. Daemon CLI Breakdown (B1 Resolution)
*Source: `benchmarks/results/pass2/b1_daemon_breakdown.json`*
- **Test Gate:** `tests/test_daemon_cli_performance.py` asserts that CLI recall with a running daemon delegates over HTTP without loading the ONNX model in-process (PASSED in 18.80s).
- **Discrepancy Resolution:**
  - Direct HTTP query to running daemon (keep-alive TCP socket): **6.50 ms p50** (p95: **8.55 ms**).
  - Bare Python startup: **104.04 ms p50**.
  - Import `mesh.cli` and dependencies (Typer, httpx, Starlette): **336.54 ms p50**.
  - CLI `mesh ask` subprocess execution: **694.38 ms p50** (p95: **1125.64 ms**).
  - CLI `mesh brief` subprocess execution: **640.71 ms p50** (p95: **1058.48 ms**).
  - The ~440 ms figure in early tests measured in-process Typer invocation without Python startup overhead; the ~2.9 s figure in pilot v2 included cold model initialization and network probe overhead. The daemon completely eliminates in-process ONNX model loading (~25 s cold load).

### G. Windows Laptop Spot-Check (10 Questions, Sleep-Proofed Bare-Metal)
*Source: `benchmarks/results/pass2/laptop_spotcheck_10q.json`*
- **Methodology:** 10 non-abstention questions from LongMemEval-S (~485–610 turns each) evaluated under `ExecutionStateGuard` with Windows Kernel-Power event log pre/post audit. Rotating tool order per question; fresh store per question per tool; first 3 queries discarded as warm-up; cold first query recorded separately.
- **Results Table:**

| Tool | Interface Class | Ingest Write Time (mean/q) | Time-to-Searchable (mean) | Cold First Query | Steady-State Query p50 | Steady-State Query p95 | Sample Size |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Mesh SDK** | Class A | **20.40 s** | **0.028 s** | 23.32 ms | **25.82 ms** | 37.68 ms | 100 queries |
| **Mem0 SDK** | Class A | **82.26 s** | **0.317 s** | 202.40 ms | **175.48 ms** | 281.84 ms | 100 queries |
| **Mesh CLI (Daemon)** | Class B | **20.13 s** | **1.133 s** | 610.04 ms | **613.96 ms** | 1015.93 ms | 100 queries |
| **Auxly CLI** | Class B | **1.23 s** | **17.788 s** | 2863.26 ms | **2021.58 ms** | 3661.73 ms | 100 queries |
| **Basic Memory CLI** | Class B | **19.30 s** | **7.153 s** | 5535.26 ms | **5740.50 ms** | 8256.05 ms | 100 queries |

---

## 3. Public Numbers Audit (README.md & Landing Page Verification)

| Surface / File | Claimed Metric / Statement | Stated Number | Supporting Raw Evidence File & Key | Verification Status |
| :--- | :--- | :---: | :--- | :---: |
| `README.md:414` | Core SDK `mesh_project_brief` p50 | 2.44 ms | `benchmarks/results/all_surfaces_results.json` (`benchmarks[brief].p50_ms`) | 🟢 Verified |
| `README.md:415` | Core SDK `mesh_task_create` p50 | 3.47 ms | `benchmarks/results/all_surfaces_results.json` (`benchmarks[task_create].p50_ms`) | 🟢 Verified |
| `README.md:416` | Core SDK `mesh_task_list` p50 | 1.05 ms | `benchmarks/results/all_surfaces_results.json` (`benchmarks[task_list].p50_ms`) | 🟢 Verified |
| `README.md:419` | Core SDK `mesh_recall` cached p50 | 5.33 ms | `benchmarks/results/all_surfaces_results.json` (`benchmarks[recall_cached].p50_ms`) | 🟢 Verified |
| `README.md:421` | Core SDK `mesh_learn` new write p50 | 9.63 ms | `benchmarks/results/all_surfaces_results.json` (`benchmarks[learn_new].p50_ms`) | 🟢 Verified |
| `README.md:433` | MCP Surface `mesh_recall` p50 | 14.68 ms | `benchmarks/results/all_surfaces_results.json` (`benchmarks[mcp_recall].p50_ms`) | 🟢 Verified |
| `README.md:500` | Scale 500 memories DB size | 5.39 MB | `benchmarks/RESULTS.md` / `benchmarks/scale_benchmark.py` | 🟢 Verified |
| `README.md:503` | Scale 10,000 memories DB size | 104.8 MB | `benchmarks/RESULTS.md` / `benchmarks/scale_benchmark.py` | 🟢 Verified |
| `README.md:688` | Brief and task list at 10,000 memories | under 12 ms | `benchmarks/results/scale_benchmark_results.json` (`brief.p50_ms: 10.3ms`) | 🟢 Verified |
| `PerformanceSection.jsx:29` | Sub-millisecond to low-millisecond | Headline | `benchmarks/results/all_surfaces_results.json` | 🟢 Verified |
| `PerformanceSection.jsx:129`| Brief & task list stay under 12ms | under 12 ms | `benchmarks/results/scale_benchmark_results.json` | 🟢 Verified |

---

## 4. Archival Benchmark Data

### Historical Pilot v2 Benchmark
> [!NOTE]
> **Mandatory Traceability Disclosure:** pilot v2, 20 questions, single run, not exclusive repeats.
> Retained strictly for archival and comparative continuity with the initial Pass 1 pilot analysis.
> *Source: `benchmarks/results/pilot_20_results.json`*
