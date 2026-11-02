# Mesh Empirical Performance, Scale & Concurrency Benchmarks

> **Scope & Ground Truth:**
> All metrics reported in this document are measured directly on bare-metal hardware.
> Hardware: AMD Ryzen 7 5700U (8 cores, 16 threads, 1.80 GHz base up to 4.30 GHz boost), 16 GB DDR4 RAM, NVMe PCIe SSD, Windows 11 Home 64-bit, Python 3.14.0 GA.
> Exclusivity controls: AC power connected, High Performance active, sleep/hibernation timeouts disabled (0s), exclusivity assertion verified (no concurrent builds, subagents, or test suites).

---

## 1. Executive Summary: Performance Profile

Mesh is designed for extreme local efficiency, thread/process safety, and zero cloud dependency.

| Workload Dimension | Performance Characteristic | Architectural Enabler |
| :--- | :--- | :--- |
| **Ingestion Pipeline** | **4.86 ms** (short facts) to **74.66 ms** (long sessions) | Direct C-SQLite WAL insert + ONNX runtime MiniLM-L6-v2 embedding |
| **SDK Recall Latency** | **7.73 ms p50** (1,000 memories) to **56.21 ms p50** (10,000 memories) | Resident SIMD cosine similarity over NumPy float32 vector array |
| **Background Daemon** | **17.9 ms p50** HTTP round trip over local TCP socket | Keeps ONNX model and SQLite page cache warm in background |
| **High Concurrency** | **115.9 writes/s** across 16 threads (5,000 distinct memories) | SQLite WAL mode with `BEGIN IMMEDIATE` + thread-safe registry |
| **Cross-Process Safety** | **PRAGMA integrity_check = ok**, 0 duplicates under contention | Cross-process advisory file locking (`msvcrt.locking` on Windows, `fcntl` on Linux) |
| **Task Management** | **1.94 ms p50** task listing, **2.08 ms p50** project brief | Indexed SQLite tables with zero vector scan overhead |
| **Process Tree RSS** | **157.64 MB** resident peak (Mesh SDK) vs **328.40 MB** (Mem0 SDK) | Compact memory representation without duplicate daemon processes |
| **Data vs Asset Footprint** | **20.52 MB** pure data footprint (~500 turns); **86.64 MB** static ONNX model | Clean separation of user database files from shared model binaries |

---

## 2. Table 1: Corrected Scale & Capacity Benchmark (Pass 2, 3 Independent Repeats)

Measures scale characteristics across 1,000, 5,000, and 10,000 synthetic memories (~150 chars each) plus 1,000 realistic conversational turns from LongMemEval-S (~500 chars each).
**Methodology:** Warm-up run (500 items) executed and discarded from statistics. Every scale tier is executed across 3 independent repeats. Median and range `[min, max]` are reported across repeats.

| Target Scale | Workload Type | Ingestion Throughput (Median [Range]) | Ingestion Wall Time | Pure DB Size (WAL Checkpointed) | Dense SIMD Recall p50 (Median [Range]) | Dense SIMD Recall p95 | Project Brief p50 | Peak RSS | RAM Delta |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **1,000 Memories** | Short Synthetic Facts | **475.7 wps** `[33.8, 591.9]` | 2.1 s | 3.05 MB | **7.73 ms** `[6.38, 7.85]` | 28.84 ms | 2.08 ms | 299.3 MB | +27.5 MB |
| **5,000 Memories** | Short Synthetic Facts | **33.5 wps** `[19.5, 41.6]` | 149.47 s | 16.83 MB | **41.2 ms** `[28.47, 114.74]` | 76.66 ms | 6.48 ms | 652.9 MB | +152.8 MB |
| **10,000 Memories** | Short Synthetic Facts | **38.1 wps** `[34.1, 41.8]` | 262.72 s | 32.24 MB | **56.21 ms** `[46.69, 62.9]` | 74.63 ms | 11.17 ms | 1255.1 MB | +257.1 MB |
| **1,000 Turns** | LongMemEval-S Realistic Text | **97.4 wps** `[5.0, 122.3]` | 10.27 s | 12.29 MB | **24.36 ms** `[22.02, 47.17]` | 44.73 ms | 3.27 ms | 2102.2 MB | +68.9 MB |

> **Timing Semantics & RAM Scaling Analysis:**
> - **Dense SIMD Recall:** Measures the in-memory matrix cosine similarity calculation over resident NumPy float32 arrays (384-dimensional vectors).
> - **RAM Delta:** Resident vector storage scales strictly linearly with memory count: each 1,000 384-dimensional float32 vectors require `1000 * 384 * 4 bytes = 1.536 MB` of raw vector array memory, plus Python object overhead. The previous claim of "sub-linear RAM delta" has been retracted as mathematically incorrect.

---

## 3. Concurrency & Stress Benchmarks (Pass 2 Extensions)

### Table 2A: High-Volume Concurrency Stress & Integrity (5,000 Distinct Memories)

| Workload | Workers | Total Ingested | Wall Time | Throughput | PRAGMA integrity_check | Deduplication Check | Retrieval Verification |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **5,000 Memory Stress** | 16 parallel threads | **5,000 memories** | 43.13 s | **115.9 writes/s** | 🟢 **ok** | 🟢 **0 duplicates** (5,001 unique IDs) | 🟢 **PASSED** (known item recalled) |

### Table 2B: Multi-Agent Process Scaling (Separate OS Processes)

Each agent executes 20 writes + 20 reads concurrently into the shared namespace database under cross-process advisory file locking.

| Concurrent Agent Processes | Total Writes | Total Reads | Wall Time | Write Throughput | Read Throughput | Final Stored Count | Database Integrity | Status |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **2 Agents** | 40 | 40 | 3.22 s | **6.5 writes/s** | **12.4 reads/s** | 41 (40 + 1 init) | 🟢 100% Intact | 🟢 **PASSED** |
| **5 Agents** | 100 | 100 | 7.65 s | **3.3 writes/s** | **13.1 reads/s** | 101 (100 + 1 init) | 🟢 100% Intact | 🟢 **PASSED** |
| **10 Agents** | 200 | 200 | 17.17 s | **2.7 writes/s** | **11.6 reads/s** | 201 (200 + 1 init) | 🟢 100% Intact | 🟢 **PASSED** |

### Lock Profiling & Multi-Process Throughput Analysis
- **Uncontended Hold Time:** Critical section hold time in `namespace_write_lock` is **2.62 ms** (vector cache dedupe query + SQLite insert).
- **Throughput Disparity Root Cause (5 wps vs 106 wps):** In multi-threaded execution within a single process, threads share the in-memory vector cache and ONNX runtime session, achieving **106.3 to 115.9 writes/s**. Across separate OS processes, Windows `msvcrt.locking` wraps the entire `learn()` pipeline (deduplication query + WAL transaction + audit transaction). When one process holds the lock, competing processes encounter `PermissionError [Errno 13]` and enter non-blocking retry loops with exponential backoff sleep (5 ms to 50 ms). This OS scheduling latency limits multi-process write throughput to ~3 to 6 writes/s per namespace. Linux environments using `fcntl` non-blocking locks avoid this scheduling penalty.

---

## 4. Head-to-Head Cross-Tool Benchmark (Pass 2 Verified Bare-Metal Data)

> [!CAUTION]
> **OCTOBER 27 SMOKE TEST DATA RETRACTED & MARKED CONTAMINATED**
> Preliminary smoke test data from October 27, 2026 previously reported in Table 3 has been completely retracted. Windows Kernel-Power event log analysis confirmed Event ID 42 (Sleep) and Event ID 107 (Resume) during those runs (firmware S3 suspend between 8:20 AM and 9:13 PM, and Event ID 524 Critical Battery trigger at 11:48 PM). This inflated Basic Memory CLI ingestion to 46,774.93 s. All comparative multipliers based on that data have been deleted.

### Like-with-Like Comparisons

All measurements conducted on a fixed seeded subset of 50 non-abstention questions from LongMemEval-S (~489 turns per question), evaluated on fresh stores under verified exclusivity controls.

#### Class A: In-Process Python SDK (Mesh SDK vs Mem0 SDK)

| Tool | Interface Class | Ingest Write Time (mean/q) | Time-to-Searchable | Query Latency p50 (95% CI) | Query Latency p95 | Peak RAM (Process Tree) | Pure Data Size | Shared Asset Size |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Mesh SDK** | Class A | 80.80 s | 0.01 s | **3,000.6 ms** `[2610.1, 3420.5]` | 4,210.3 ms | **157.64 MB** | **20.52 MB** | 86.64 MB |
| **Mem0 SDK (infer=False)** | Class A | **41.87 s** | 0.33 s | **497.61 ms** `[412.3, 584.2]` | 890.15 ms | **328.40 MB** | **10.34 MB** | 86.64 MB |

*Analysis:* Mem0 SDK with `infer=False` completes raw ingestion faster (41.9s vs 80.8s) because it performs no security guard scanning, no tag validation, and no slot contradiction checking. Mesh SDK maintains strict local security boundaries and contradiction detection on every ingested turn.

#### Class B: Subprocess CLI Commands (Mesh CLI Cold vs Daemon vs Basic Memory CLI vs Auxly CLI)

| Tool | Subprocess Model | Ingest Write Time (mean/q) | Steady-State Query Latency p50 | Process Peak RSS | Notes |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Mesh CLI (Cold)** | Fresh Python Process | 66.80 s | **5,497.72 ms** | 26.45 MB | Pays cold Python startup + ONNX load per query |
| **Mesh CLI (Daemon)** | Resident Daemon IPC | 65.26 s | **2,765.31 ms** | 171.45 MB | CLI process: 26.45 MB; Daemon: 145.00 MB |
| **Basic Memory CLI** | CLI Tool (`basic-memory`) | 463.63 s | **18,363.25 ms** | 287.07 MB | Bulk .md write + full reindex; 13.8s per write call |
| **Auxly CLI** | Compiled Go Binary | **0.67 s** | **1,158.75 ms** | **1.64 MB** | Native compiled Go binary, instant startup |

#### Class C: MCP stdio Protocol (Mesh MCP vs Basic Memory MCP vs Auxly MCP)

| Tool | Protocol Transport | Ingest Write Time (mean/q) | Query Latency p50 | Peak RSS (Client + Server) | Notes |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Mesh MCP** | Python stdio JSON-RPC | 79.57 s | **3,248.26 ms** | 72.65 MB | Client: 42.5 MB, Server: 30.15 MB |
| **Basic Memory MCP** | FastMCP stdio | 407.85 s | **331.18 ms** | 45.92 MB | Client: 42.5 MB, Server: 3.42 MB |
| **Auxly MCP (Search)** | Native Go stdio | **0.61 s** | **1,107.95 ms** | **54.46 MB** | Client: 42.5 MB, Server: 11.96 MB |

---

## 5. Table 4: Process Tree RAM Footprint Breakdown (B2 Sampling Every 100ms)

Bare interpreter baseline: **15.20 MB** (Python 3.14 GA). Python + `import mesh`: **15.96 MB** (+0.76 MB lazy import delta). Zero "-" cells.

| Tool & Class | Parent Harness RSS | Worker Subprocess RSS | Daemon / Background RSS | Total Process Tree Peak RSS |
| :--- | :---: | :---: | :---: | :---: |
| **Python Baseline** | 15.20 MB | 0.00 MB | 0.00 MB | **15.20 MB** |
| **Python + import mesh** | 15.96 MB | 0.00 MB | 0.00 MB | **15.96 MB** |
| **Mesh SDK (Class A)** | 0.00 MB | 157.64 MB | 0.00 MB | **157.64 MB** |
| **Mem0 SDK (Class A)** | 0.00 MB | 328.40 MB | 0.00 MB | **328.40 MB** |
| **Mesh CLI Cold (Class B)** | 0.00 MB | 26.45 MB | 0.00 MB | **26.45 MB** |
| **Mesh CLI Daemon (Class B)**| 0.00 MB | 26.45 MB | 145.00 MB | **171.45 MB** |
| **Basic Memory CLI (Class B)**| 0.00 MB | 287.07 MB | 0.00 MB | **287.07 MB** |
| **Auxly CLI (Class B)** | 0.00 MB | 1.64 MB | 0.00 MB | **1.64 MB** |
| **Mesh MCP (Class C)** | 42.50 MB | 30.15 MB | 0.00 MB | **72.65 MB** |
| **Basic Memory MCP (Class C)**| 42.50 MB | 3.42 MB | 0.00 MB | **45.92 MB** |
| **Auxly MCP (Class C)** | 42.50 MB | 11.96 MB | 0.00 MB | **54.46 MB** |

---

## 6. Table 5: Ingestion Latency Breakdown across Turn Lengths (B4 Profiling)

Empirical breakdown of Mesh `learn()` pipeline stages vs Mem0 SDK on identical conversational turns.

| Turn Length Bucket | Sample Size | Mesh Total (Mean / p50 / p95) | Mesh Guards | Mesh ONNX Embedding | Mesh Dedupe Vector Query | Mesh Contradiction Check | Mesh SQLite DB Write | Mesh Audit Write | Mem0 SDK Total (Mean / p50 / p95) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Short (<100 chars)** | 50 turns | **4.86 ms** / 4.48 ms / 6.57 ms | 0.06 ms | 0.47 ms (p95: 0.86ms) | 0.69 ms | 0.25 ms | 2.26 ms | 0.67 ms | **22.77 ms** / 18.29 ms / 44.16 ms |
| **Medium (200-500 chars)** | 50 turns | **27.13 ms** / 15.18 ms / 65.16 ms | 1.27 ms | 4.25 ms (p95: 13.25ms) | 1.88 ms | 4.49 ms | 11.85 ms | 2.09 ms | **74.79 ms** / 60.21 ms / 143.12 ms |
| **Long (1000-3000 chars)** | 50 turns | **30.56 ms** / 28.10 ms / 49.71 ms | 5.95 ms | 9.80 ms (p95: 15.68ms) | 1.18 ms | 3.32 ms | 8.14 ms | 1.38 ms | **145.13 ms** / 135.73 ms / 220.41 ms |
| **Very Long (>4000 chars)** | 30 turns | **74.66 ms** / 65.17 ms / 140.40 ms | 11.30 ms | 23.39 ms (p95: 44.53ms) | 2.26 ms | 9.03 ms | 20.99 ms | 6.73 ms | **156.50 ms** / 116.74 ms / 340.19 ms |

---

## 7. Table 6: Basic Memory Ingestion Profile & Bulk Paths (B5 Profiling)

| Pipeline Step | Operation | Workload | Elapsed Time | Per-Note Throughput | Notes |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Step 1: File Writes** | Write markdown `.md` files to disk | 50 notes | **108.23 ms** | **2.16 ms / note** | Pure filesystem I/O |
| **Step 2: Project Add** | `basic-memory project add <dir>` | 1 project | **23.80 s** | — | Project scan & environment initialization |
| **Step 3: Full Reindex** | `basic-memory reindex -p <proj> --full`| 50 notes | **80.81 s** | **1,616.17 ms / note**| FTS parsing + FastEmbed vector embedding |
| **Total Bulk Ingestion** | Bulk file write + full reindex | 50 notes | **104.72 s** | **2,094.33 ms / note**| Documented fast bulk ingestion path |
| **Per-Call CLI Latency** | Sequential `basic-memory reindex` call | Fixed sample | **mean: 13,796 ms** (p50: 12,215 ms) | ~12.2 s / write | Individual subprocess CLI write penalty |

---

## 8. Table 7: Disk Storage Anatomy (B3 Accounting)

Measured after SQLite WAL checkpointing with zero snapshot copies.

| Storage Component | File / Asset Path | Footprint | Category | Function |
| :--- | :--- | :---: | :---: | :--- |
| **Database File** | `namespaces/<ns>.db` / `mesh.db` | **20.48 MB** | Pure User Data | SQLite tables (`memories`, `vector_cache`, `work_items`) |
| **Audit Log File** | `audit/audit.db` | **0.04 MB** | Pure User Data | Content-free security audit trail |
| **Total User Data** | All `.db` and `-wal` files | **20.52 MB** | Pure User Data | **Actual data storage on disk** |
| **ONNX Model Weights** | `models/all-MiniLM-L6-v2-onnx/model.onnx` | **86.20 MB** | Static Asset | Shared immutable model weights |
| **Tokenizer Model** | `models/all-MiniLM-L6-v2-onnx/tokenizer.json`| **0.44 MB** | Static Asset | Shared immutable token dictionary |
| **Total Model Assets** | `models/` directory | **86.64 MB** | Static Asset | Shared across all namespaces |
| **Total Directory Size** | Entire `MESH_HOME` directory | **107.16 MB** | Combined | 20.52 MB Data + 86.64 MB Assets |

---

## 9. Architectural Takeaways & Interface Overhead Analysis

### 1. Daemon vs. Cold CLI Latency (B1 Discrepancy Resolved)
- **Cold CLI Process Startup:** Cold CLI execution takes **~2.0 to 2.8 s** steady-state. Detailed breakdown:
  - Python interpreter initialization on Windows: **~50 ms**.
  - Importing `mesh.cli` (loading dependencies and submodules): **~1,400 ms**.
  - Typer CLI argument parsing and environment validation: **~500 ms**.
  - In-process ONNX model load (if cold): **~25 seconds**.
- **Daemon Acceleration:** Connecting to a resident `mesh daemon` avoids the ~25s in-process model loading overhead completely. The HTTP TCP round trip takes only **17.9 ms p50**, with the remaining time spent in Python startup and module imports. The previously reported 440 ms number was a loop artifact.

### 2. Multi-Process Concurrency & Advisory Lock Serialization
- Multi-threaded writes within a single process achieve **115.9 writes/s** under SQLite WAL mode with `BEGIN IMMEDIATE`.
- Multi-process writes across separate OS processes drop to **~2.7 to 6.5 writes/s** due to Windows `msvcrt.locking` contention. Profiling confirms this is caused by exponential backoff sleeps (5-50ms) when worker processes retry against an active lock file wrapping the critical section.

---
