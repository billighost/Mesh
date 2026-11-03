# Benchmark & Evidence State (Checkpoint P2-C Bare-Metal Measurements)

Last updated: November 3, 2026.
Commit / Tag Target: `benchmark-v1-pass2c`.
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
- **Isolation Fix:** Every repeat runs in a fresh, dedicated Python subprocess with a newly verified-empty directory (`assert len(os.listdir(tmp_dir)) == 0`). This completely eliminates heap accumulation and store reuse anomalies.
- **1,000 Synthetic Facts:** Cold headline throughput: **54.0 wps**; Median throughput: **57.1 wps** `[54.0, 58.1]`; Dense SIMD Recall p50: **7.71 ms** `[6.88, 8.44]`; Pure DB: **3.05 MB**; Peak RSS: **217.6 MB** `[214.5, 219.0]`.
- **5,000 Synthetic Facts:** Cold headline throughput: **61.7 wps**; Median throughput: **61.7 wps** `[59.9, 62.4]`; Dense SIMD Recall p50: **53.25 ms** `[48.91, 55.10]`; Pure DB: **16.83 MB**; Peak RSS: **300.6 MB** `[297.8, 303.4]`.
- **10,000 Synthetic Facts:** Cold headline throughput: **60.9 wps**; Median throughput: **60.9 wps** `[58.2, 61.5]`; Dense SIMD Recall p50: **76.52 ms** `[72.10, 81.30]`; Pure DB: **32.24 MB**; Peak RSS: **391.4 MB** `[388.2, 394.1]`.
- **1,000 Realistic Turns (LongMemEval-S Text):** Cold headline throughput: **3.59 wps** (278 ms/turn); Median throughput: **3.61 wps** `[3.59, 3.65]`; Dense SIMD Recall p50: **30.15 ms** `[28.40, 31.90]`; Pure DB: **12.29 MB**; Peak RSS: **772.6 MB** `[768.4, 775.0]`. Peak RSS is stable across repeats with zero in-process accumulation.

### B. Concurrency Extensions & Multi-Process Lock Profiling
*Source: `benchmarks/results/pass2/concurrency_multiagent.json`*
- **5,000-Memory Integrity Test:** Ingested 5,000 distinct memories across 16 concurrent threads in 57.38 s (**87.14 writes/s**). Exactly 5,000 submitted, 5,000 stored, 0 duplicates, 0 rejected, 0 errors, `PRAGMA integrity_check = ok`, retrieval 10/10 verified.
- **Accounting Explanation:** All 5,000 submitted writes are accounted for. The earlier 4,992 count was caused by integer division truncation (`5000 // 16 = 312` with remainder 8 dropped), which has been corrected by remainder partitioning (`8 * 313 + 8 * 312 = 5000`). Zero duplicate reinforcements, zero guard rejections, zero lost writes.
- **Multi-Agent Process Scaling (Separate OS Processes, 3 Repeats, multiprocessing.Barrier):**
  - 2 Agents: Steady Write Throughput: **12.03 wps** `[8.45, 12.28]`; Read Throughput: **116.95 rps** `[101.46, 151.22]`; Lock wait p50: **0.05 ms**; Lock hold p50: **5.52 ms**; Status: PASSED (100% database integrity, 0 errors across all 3 repeats).
  - 5 Agents: Steady Write Throughput: **10.04 wps** `[6.29, 10.79]`; Read Throughput: **62.63 rps** `[25.90, 73.62]`; Lock wait p50: **0.05 ms**; Lock hold p50: **6.91 ms**; Status: PASSED (100% database integrity, 0 errors across all 3 repeats).
  - 10 Agents: Steady Write Throughput: **7.89 wps** `[7.25, 7.95]`; Read Throughput: **108.72 rps** `[103.57, 231.77]`; Lock wait p50: **0.05 ms**; Lock hold p50: **6.66 ms**; Status: PASSED (100% database integrity, 0 errors across all 3 repeats).
- **Lock Profiling:** Uncontended lock hold time is **5.48 ms p50** (p95: **7.42 ms**).
- **Contention Root Cause Diagnosis:** Multi-threaded throughput is 87 writes/s because threads share in-memory connection pooling and SQLite internal mutexes. Multi-process throughput drops to 7.9–12.0 writes/s because cross-process concurrency relies on file-level locking (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) protecting the entire `learn()` critical section (including read-only vector deduplication queries), combined with coarse 5-50 ms exponential backoff sleep intervals that induce lock convoy formation.
- **Architectural Remediation Proposal (Scheduled for Future Release, Zero Code Changes on Current Branch):**
  1. *Granular Lock Scope:* Move read-only vector deduplication queries outside `namespace_write_lock`, scoping the advisory lock strictly to the atomic SQLite insert/update.
  2. *SQLite Native Concurrency:* Utilize SQLite WAL-mode `PRAGMA busy_timeout = 5000` to delegate lock scheduling to SQLite's native page-level lock manager.
  3. *Randomized Micro-Jitter:* Replace coarse 5-50 ms backoff loops with microsecond randomized jitter (500 µs – 2 ms).

### C. Process-Tree RAM Footprint (B2 50ms Real-Workload Sampling)
*Source: `benchmarks/results/pass2/b2_ram_timeseries.json`*
- **Methodology:** 50 ms interval sampling of entire process trees (`p.children(recursive=True)`) across a real 489-turn LongMemEval-S ingestion and 10 queries with model loaded. Evaluated across 3 independent repeats. Harness wrappers are categorized as `harness_wrapper` and excluded from tool RSS; only tool processes are counted.
- **Bare Python Baseline:** **15.20 MB** (Python 3.14 GA). Every tool is verified strictly above baseline.
- **Mesh SDK (Class A):** **209.31 MB** median `[209.09, 209.42]` (Python runtime + in-memory ONNX session + SQLite).
- **Mem0 SDK (Class A):** **305.03 MB** median `[304.38, 308.84]` (Python runtime + fastembed ONNX runtime + Qdrant embedded engine).
- **Mesh CLI Cold (Class B):** **77.67 MB** median `[77.66, 77.82]` (Transient CLI command invocation loading ONNX on query).
- **Mesh CLI Daemon (Class B):** **256.25 MB** median `[256.20, 256.45]` (Persistent background daemon holding warmed model).
- **Basic Memory CLI (Class B):** **291.46 MB** median `[291.24, 291.47]` (Python 3.12 child process + FastMCP + SQLite).
- **Auxly CLI (Class B):** **15.78 MB** median `[15.67, 15.90]` (Compiled Go binary x86_64, unbuffered directory scan).
- **Mesh MCP (Class C):** **197.21 MB** median `[195.76, 212.67]` (FastMCP stdio server process + in-memory ONNX).
- **Basic Memory MCP (Class C):** **125.38 MB** median `[125.32, 125.43]` (FastMCP server child process).
- **Auxly MCP (Class C):** **15.84 MB** median `[15.65, 16.00]` (Compiled Go binary x86_64, persistent stdio session).

### D. Disk Storage Anatomy & Model Reconciliation (B3 Accounting)
*Source: `benchmarks/results/pass2/b3_disk_inventory.json`*
- **User Data Footprint (Fresh 489-turn store, WAL checkpointed):**
  - Mesh Data: **7.40 MB** (12 files: SQLite database containing memories, vectors, tags, slots, and content-free audit trail).
  - Mem0 Data: **3.30 MB** (3 files: Qdrant SQLite database + WAL + segment files).
  - Basic Memory Data: **9.40 MB** (506 files: 489 Markdown note files + `.basic-memory/memory.db`).
  - Auxly Data: **0.42 MB** (489 files: Markdown frontmatter turn files).
- **Model Cache Reconciliation:**
  - Clean model assets on disk are exactly **86.66 MB**: `model.onnx` (**86.22 MB**) + `tokenizer.json` (**0.44 MB**).
  - Mem0 shared model asset: **86.88 MB** (`fastembed_cache/models--Qdrant--all-MiniLM-L6-v2-onnx`).
  - Basic Memory model asset: **64.07 MB** (`fastembed_cache/models--Qdrant--bge-small-en-v1.5-onnx-Q`).
  - Auxly: **0.00 MB** (No neural model assets).
  - The pilot v2 directory size of 195.66 MB was caused by 109.00 MB of stale temporary download artifacts (`model.tmp.16336.2464` at 68.38 MB and `model.tmp.3888.15404` at 40.63 MB) left by earlier interrupted downloads.

### E. Ingestion Latency Breakdown & Reconciliation (B4 Profiling)
*Source: `benchmarks/results/pass2/b4_ingestion_profile.json`*
- **Overall Mesh Ingestion:** Mean **22.88 ms**, Median **15.98 ms**, p95 **44.82 ms**.
- **Overall Mem0 Ingestion:** Mean **117.22 ms**, Median **89.70 ms**, p95 **241.01 ms**.
- **Turn Length Breakdown (Mesh vs Mem0):**
  - <100 chars (n=54): Mesh p50 **10.59 ms** | Mem0 p50 **55.45 ms**
  - 100–250 chars (n=85): Mesh p50 **11.23 ms** | Mem0 p50 **67.12 ms**
  - 250–500 chars (n=115): Mesh p50 **13.56 ms** | Mem0 p50 **82.35 ms**
  - 500–1000 chars (n=120): Mesh p50 **15.42 ms** | Mem0 p50 **91.80 ms**
  - 1000–2000 chars (n=105): Mesh p50 **17.91 ms** | Mem0 p50 **95.22 ms**
  - 2000+ chars (n=71): Mesh p50 **24.52 ms** | Mem0 p50 **110.15 ms**
- **Internal Stage Breakdown (Mesh):**
  - Input guards: **0.02 ms p50**
  - ONNX embedding: **11.45 ms p50**
  - Deduplication query: **2.35 ms p50**
  - Contradiction check: **0.01 ms p50**
  - SQLite write: **2.15 ms p50**
- **Reconciliation of Latency Measurements (23 ms vs 42 ms vs 165 ms vs 278 ms):**
  - 23 ms (B4): Ingests raw clean turns through default Stage 1 fast hashing (`hash-384`, sub-millisecond) with background ONNX re-embedding.
  - 42 ms (Spot-Check): Multi-turn conversational ingestion with session tracking and conflict slot verification.
  - 165 ms (Pilot v2): `MESH_SYNC_ONNX=1` in `run_pilot.py` forced synchronous CPU ONNX inference per turn (~140 ms CPU overhead).
  - 278 ms (Scale Benchmark): 1,000 realistic-length turns with synchronous embedding and full validation.

### F. Daemon CLI Breakdown (B1 Resolution)
*Source: `benchmarks/results/pass2/b1_daemon_breakdown.json`*
- **Test Gate:** `tests/test_daemon_cli_performance.py` asserts that CLI recall with a running daemon delegates over HTTP without loading the ONNX model in-process (PASSED).
- **Discrepancy Resolution:**
  - Bare Python startup: **46.21 ms p50** (Mean: 51.15 ms, p95: 78.40 ms).
  - Import `mesh.cli` and dependencies (Typer, httpx, Starlette): **188.35 ms p50** (Mean: 196.80 ms, p95: 235.10 ms).
  - Direct HTTP query to running daemon (keep-alive TCP socket): **4.54 ms p50** (Mean: 5.12 ms, p95: 7.20 ms).
  - CLI `mesh ask` subprocess execution: **604.42 ms p50** (Mean: 622.18 ms, p95: 785.40 ms).
  - CLI `mesh brief` subprocess execution: **641.48 ms p50** (Mean: 658.90 ms, p95: 812.30 ms).
  - The ~440 ms figure in early tests measured in-process Typer invocation without Python startup overhead; the ~2.9 s figure in pilot v2 included cold model initialization and network probe overhead. The daemon completely eliminates in-process ONNX model loading (~25 s cold load).

### G. Windows Laptop Spot-Check (10 Questions, Sleep-Proofed Bare-Metal)
*Source: `benchmarks/results/pass2/laptop_spotcheck_10q.json`*
- **Methodology:** 10 non-abstention questions from LongMemEval-S (~485–610 turns each) evaluated under `ExecutionStateGuard` with Windows Kernel-Power event log pre/post audit. Rotating tool order per question; fresh store per question per tool; first 3 queries discarded as warm-up; cold first query recorded separately.
- **Results Table:**

| Tool | Interface Class | Ingest Write Time (mean/q) | Time-to-Searchable (mean) | Cold First Query | Per-Question p50 Spread [Min, Max] | Overall Steady-State Query p50 | Overall Steady-State Query p95 | Overall Query Max | Sample Size |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Mesh SDK** | Class A | **20.40 s** | **0.028 s** | 23.32 ms | [9.22, 106.87] ms | **24.52 ms** | **107.30 ms** | 228.05 ms | 100 queries |
| **Mem0 SDK** | Class A | **82.26 s** | **0.317 s** | 202.40 ms | [95.73, 312.42] ms | **170.89 ms** | **456.98 ms** | 1839.67 ms | 100 queries |
| **Mesh CLI (Daemon)** | Class B | **20.13 s** | **1.133 s** | 610.04 ms | [437.22, 3775.04] ms | **595.42 ms** | **3868.08 ms** | 5437.88 ms | 100 queries |
| **Auxly CLI** | Class B | **1.23 s** | **17.788 s** | 2863.26 ms | [1154.46, 13900.59] ms | **1945.01 ms** | **14635.22 ms** | 49929.01 ms | 100 queries |
| **Basic Memory CLI** | Class B | **19.30 s** | **7.153 s** | 5535.26 ms | [3545.27, 34973.31] ms | **5870.27 ms** | **40018.01 ms** | 64532.93 ms | 100 queries |

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
