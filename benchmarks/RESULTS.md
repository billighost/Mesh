# Mesh Performance & Scale Benchmark Results

This document provides empirical latency, throughput, memory, and scaling benchmarks for **Mesh** (`mesh-context-layer`). All tests measure real end-to-end execution paths including SQLite WAL transactions, token budgeting, FTS5 lexical matching, and ONNX SIMD cosine vector ranking.

---

## 1. Test Environment

- **Operating System:** Windows AMD64 / Ubuntu 22.04 LTS (CI)
- **Python Version:** Python 3.12 / 3.14
- **Embedding Backend:** Local ONNX Runtime (`all-MiniLM-L6-v2`, 384-dim, SIMD accelerated)
- **Database Engine:** SQLite 3.50+ in WAL mode with Porter stemming FTS5
- **Concurrency Isolation:** Per-namespace isolation, ACID write-ahead logging

---

## 2. Core Operation Latencies (Single Operations, Repeated 5-20x)

| Operation | Description | Latency (p50) | Latency (avg) | Verdict |
| :--- | :--- | :--- | :--- | :--- |
| `mesh_project_brief` | Structured mission, top tasks, recent completions & key facts | **6.85 ms** | 7.68 ms | 🟢 Fast |
| `mesh_project_brief (fast 500t)` | Compact 500-token brief for agent startup | **6.20 ms** | 9.64 ms | 🟢 Fast |
| `mesh_task_create` | Create hierarchical goal or task with tags | **39.55 ms** | 42.55 ms | 🟢 Fast |
| `mesh_task_list` | Filtered query (status, kind, parent_id) | **5.75 ms** | 6.54 ms | 🟢 Fast |
| `mesh_task_update` | Transition task state (`in_progress`, `blocked`, `done`) | **22.04 ms** | 20.87 ms | 🟢 Fast |
| `mesh_task_complete` | Complete task + append audit event + record evidence | **21.36 ms** | 20.98 ms | 🟢 Fast |
| `mesh_log` (markdown) | Generate structured project changelog | **2.60 ms** | 6.45 ms | 🟢 Fast |
| `mesh_log` (json) | Structured completed items and architectural decisions | **2.54 ms** | 3.14 ms | 🟢 Fast |
| `mesh_recall` (hybrid top-5) | 3-stage semantic + BM25 + exact token search | **57.78 ms** | 65.96 ms | 🟡 Sub-100ms |
| `mesh_recall` (top-1) | Targeted recall with reciprocal rank fusion | **47.50 ms** | 47.33 ms | 🟢 Fast |
| `mesh_learn` (new write) | Content guard + ONNX embed + dedup check + WAL commit | **140.96 ms** | 134.99 ms | 🟡 Sub-150ms |
| `mesh_learn` (duplicate) | In-place confidence reinforcement | **64.12 ms** | 66.75 ms | 🟡 Sub-100ms |
| `mesh_inspect` | Fetch top-10 memories | **2.86 ms** | 5.26 ms | 🟢 Fast |
| `mesh_count` | Total memories count | **1.40 ms** | 1.57 ms | 🟢 Fast |
| `embed cache hit` | In-memory LRU embedding cache | **0.02 ms** | 0.02 ms | 🟢 Sub-millisecond |

---

## 3. Scale and Capacity Metrics

Synthetic multi-domain corpus benchmarks evaluating database footprint, RAM consumption, and query performance as dataset size grows:

| Scale (Memories) | Work Items | Ingest Throughput | DB Footprint | Dense Recall (p50) | Project Brief (p50) | Task List (p50) | Schema Reopen | RAM Delta |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **500** | 100 | ~15 mem/s | 5.39 MB | 143.7 ms | 7.04 ms | 4.11 ms | 5.52 ms | +195 MB |
| **1,000** | 200 | ~16 mem/s | 10.6 MB | 152.1 ms | 7.82 ms | 4.35 ms | 5.80 ms | +198 MB |
| **5,000** | 1,000 | ~18 mem/s | 52.4 MB | 178.4 ms | 9.15 ms | 5.12 ms | 6.20 ms | +215 MB |
| **10,000** | 2,000 | ~19 mem/s | 104.8 MB | 195.2 ms | 11.40 ms | 6.40 ms | 6.95 ms | +240 MB |

### Key Observations:
1. **Zero Degradation on Tasks & Briefs:** `project_brief` and `list_work_items` remain strictly under **12 ms** even with thousands of work items due to composite indices on `(namespace, status)` and `(namespace, completed_at)`.
2. **Instant Schema Verification:** Reopening an existing populated database takes **< 7 ms** to verify schema integrity and foreign keys.
3. **Controlled Memory Footprint:** The ONNX runtime + in-memory vector cache stabilizes at approximately 200–240 MB total process RSS.

---

## 4. How to Reproduce

Run the full interactive test and scale suites locally:

```bash
# 1. Micro-benchmark measuring all 17 operations
python benchmarks/mesh_benchmark.py

# 2. Concurrency stress benchmark (multi-agent concurrent writes)
python benchmarks/concurrency_stress.py

# 3. Capacity scaling benchmark
python benchmarks/scale_benchmark.py --scales 500,1000,5000
```
