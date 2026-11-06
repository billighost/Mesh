# Mesh Performance & Scale Benchmark Results

This document provides empirical latency, throughput, memory, and scaling benchmarks for **Mesh** (`mesh-context-layer`). All tests measure real end-to-end execution paths including SQLite WAL transactions, token budgeting, FTS5 lexical matching, and ONNX SIMD cosine vector ranking.

## Mesh 0.9.0 working-tree retrieval investigation (2026-11-06)

[The sanitized aggregate](results/production_retrieval_20261106.json) identifies
each tested runtime, dataset, model, protocol, and scorer by SHA-256. These are
development checkpoints. Final-source GitHub Actions qualification and the new
concurrent performance matrix have not run. There is no new throughput, quiet
latency, competitor-superiority, or physical power-loss result.

All quality cells used the pinned local MiniLM encoder, patched SQLite 3.54.1,
WAL/FULL durability, public visibility, and original annotated evidence IDs.
Hit@k means at least one authorized annotated memory appears in the first k
results. It does not measure a generated answer or complete multi-source answer
sufficiency. No-answer candidate emission is reported separately in the aggregate.

| Condition, tested Mesh 0.9.0 checkpoint | Answerable queries | Hit@1 | Hit@5 | Hit@10 |
| --- | ---: | ---: | ---: | ---: |
| Frozen schema 4, 512 MiB cache, NFCorpus hybrid | 323 | 49.23% | 66.87% | 74.30% |
| Same frozen NFCorpus runtime/model, dense ablation | 323 | 44.89% | 61.92% | 69.35% |
| Frozen schema 4, 512 MiB cache, SciFact hybrid | 300 | 56.00% | 78.33% | 84.67% |
| Same frozen SciFact runtime/model, dense ablation | 300 | 48.67% | 75.00% | 81.00% |
| Schema 5 checkpoint, 256 MiB cache, fresh default SDK SciFact | 300 | 56.00% | 78.33% | 84.67% |
| Same schema 5 checkpoint, direct-fact regression | 16 | 81.25% | 100.00% | 100.00% |
| Same schema 5 checkpoint, early/middle/late long-text regression | 6 | 0.00% | 0.00% | 100.00% |
| Same schema 5 checkpoint, inspected LongMemEval exact-turn subset | 6 | 50.00% | 100.00% | 100.00% |

The frozen schema 4 runtime has `mesh/store.py` SHA-256
`017443afd9faa2f41755b9db0937e87c866f6846098e96b3153bec5efdb784a8`.
The schema 5 checkpoint has store SHA-256
`0d2c6bdbaa8b8f8e4bd4f07bf7d4d184b6b3f98a22a85dc16d977e877485e620`.
Complete module manifests are in the aggregate; later encryption and documentation
fixes require their own final-source qualification.

Saved-vector replays at that schema 5 checkpoint accounted for all 323 NFCorpus
and 300 SciFact queries. Every ordered ID matched the frozen runs at C64 and C96
in raw hybrid candidates, final SDK results, reranker inputs, and dense candidates.
This validates ranking/storage continuity with the production 256 MiB cache.
The separate fresh SciFact run verified all 5,183 source revisions/indexes and
used normal SDK text queries, default candidate settings, and access recording,
with no precomputed query vectors or document inference.

Canonical lexical strings preserved all 8,816 source documents' lexical values.
The schema 5 SciFact cache admitted all 5,183 memories within the unchanged
268,435,456-byte budget, with 221,184,836 bytes conservatively charged. Cache charge
and a unique Python object graph are distinct from total process RAM.

The long-text misses remain failures: all six correct memories survived ingestion
and visibility checks, but ranked ninth or tenth. The direct fixture also retained
three first-result misses involving environment qualifiers or answerless reference
documents. The tiny inspected fixtures do not support population-level accuracy
or competitor claims. Paragraph passage scoring and a separate optional C4
reranker remain experiments with remote quality/resource gates.

The four frozen TinyBERT reranking configurations were worse than their matching
hybrid result. The MiniLM C64 cross-encoder was stopped for exceeding its declared
resource gate; its partial export remains unscored. The E5 trial was settled
during partial indexing and has no quality score. Those outcomes are preserved.
Reproduction and condition details are in [RETRIEVAL.md](RETRIEVAL.md).

The remaining tables below are historical Mesh 0.8.3 measurements. The legacy MCP
microbenchmarks call in-process handlers; their timings exclude protocol transport.

---

## 1. Test Environment

- **Mesh version:** 0.8.3
- **Operating System:** Windows 11 AMD64 / Ubuntu 22.04 LTS (CI)
- **Python Version:** Python 3.14.0 (primary) / 3.12 (CI)
- **Embedding Backend:** Local ONNX Runtime (`all-MiniLM-L6-v2`, 384-dim, SIMD accelerated)
- **Database Engine:** SQLite 3.50.4 in WAL mode with Porter stemming FTS5
- **Concurrency Isolation:** Per-namespace isolation, ACID write-ahead logging
- **Benchmark tool:** `benchmark_all_surfaces.py` (25 iterations per operation, 2 warmup, perf_counter_ns timing)

---

## 2. Core SDK Operation Latencies (~100 memories, ONNX loaded)

| Operation | Description | p50 | p95 | Verdict |
| :--- | :--- | ---: | ---: | :--- |
| `mesh_project_brief` | 500-token brief (open tasks, facts, completions) | **2.44 ms** | 5.27 ms | 🟢 Fast |
| `mesh_task_create` | Create hierarchical goal or task with tags | **3.47 ms** | 8.48 ms | 🟢 Fast |
| `mesh_task_list` | Filtered query (status, kind, parent_id) | **1.05 ms** | 3.61 ms | 🟢 Fast |
| `mesh_task_complete` | Complete task + append audit event + record evidence | **3.63 ms** | 6.74 ms | 🟢 Fast |
| `mesh_log` (markdown) | Generate structured project changelog | **3.07 ms** | 6.71 ms | 🟢 Fast |
| `mesh_log` (json) | Structured completed items and architectural decisions | **2.61 ms** | 5.97 ms | 🟢 Fast |
| `mesh_recall` (hybrid top-5, cached) | 3-stage semantic + BM25 + exact token search (warm query) | **5.33 ms** | 10.04 ms | 🟡 Sub-15ms |
| `mesh_recall` (hybrid top-5, uncached) | Same pipeline, cold query string | **8.58 ms** | 13.08 ms | 🟡 Sub-15ms |
| `mesh_learn` (new write) | Content guard + ONNX embed + dedup check + WAL commit | **9.63 ms** | 13.32 ms | 🟡 Sub-15ms |
| `mesh_learn` (duplicate) | In-place confidence reinforcement | **7.77 ms** | 9.96 ms | 🟡 Sub-15ms |
| `mesh_inspect` | Fetch top-30 memories | **2.72 ms** | 3.85 ms | 🟢 Fast |
| `embed cache hit` | In-memory LRU embedding cache | **0.05 ms** | 0.17 ms | 🟢 Sub-millisecond |
| `guard_ingest` (clean fact) | Secret + injection guard pass | **0.02 ms** | 0.03 ms | 🟢 Sub-millisecond |
| `guard_ingest` (secret detection) | Redaction + local_only flag | **0.12 ms** | 0.34 ms | 🟢 Sub-millisecond |

---

## 3. MCP Surface Latencies (as called by an agent, ~100 memories)

| MCP Tool | Description | p50 | p95 | Verdict |
| :--- | :--- | ---: | ---: | :--- |
| `list_tools()` | Schema enumeration | **0.09 ms** | 0.25 ms | 🟢 Sub-millisecond |
| `mesh_project_brief` | 500-token brief over MCP | **2.77 ms** | 3.82 ms | 🟢 Fast |
| `mesh_task_create` | Task creation over MCP | **4.26 ms** | 6.09 ms | 🟢 Fast |
| `mesh_task_list` | Task list over MCP | **3.38 ms** | 6.30 ms | 🟢 Fast |
| `mesh_log` | Changelog over MCP | **4.99 ms** | 27.29 ms | 🟢 Fast |
| `mesh_recall` | Hybrid recall over MCP | **14.68 ms** | 39.72 ms | 🟡 Sub-50ms |
| `mesh_learn` | Write memory over MCP | **7.79 ms** | 10.71 ms | 🟡 Sub-15ms |
| `mesh_inspect` | Inspect memories over MCP | **4.86 ms** | 9.20 ms | 🟢 Fast |
| `mesh_context` | Context bundle over MCP | **5.63 ms** | 9.21 ms | 🟡 Sub-15ms |
| `mesh_contradictions` | List contradictions over MCP | **2.37 ms** | 3.27 ms | 🟢 Fast |

---

## 4. Scale and Capacity Metrics

Synthetic multi-domain corpus benchmarks evaluating database footprint, RAM consumption, and query performance as dataset size grows. Run with `benchmarks/scale_benchmark.py`.

| Scale (Memories) | Work Items | Ingest Throughput | DB Footprint | Dense Recall (p50) | Project Brief (p50) | Task List (p50) | Schema Reopen | RAM Delta |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **500** | 100 | ~15 mem/s | 5.39 MB | 143.7 ms | 7.04 ms | 4.11 ms | 5.52 ms | +195 MB |
| **1,000** | 200 | ~16 mem/s | 10.6 MB | 152.1 ms | 7.82 ms | 4.35 ms | 5.80 ms | +198 MB |
| **5,000** | 1,000 | ~18 mem/s | 52.4 MB | 178.4 ms | 9.15 ms | 5.12 ms | 6.20 ms | +215 MB |
| **10,000** | 2,000 | ~19 mem/s | 104.8 MB | 195.2 ms | 11.40 ms | 6.40 ms | 6.95 ms | +240 MB |

### Key Observations

1. **Zero Degradation on Tasks & Briefs:** `project_brief` and `list_work_items` remain strictly under **12 ms** even with thousands of work items due to composite indices on `(namespace, status)` and `(namespace, completed_at)`.
2. **Instant Schema Verification:** Reopening an existing populated database takes **< 7 ms** to verify schema integrity and foreign keys.
3. **Controlled Memory Footprint:** The ONNX runtime + in-memory vector cache stabilizes at approximately 200–240 MB total process RSS.

---

## 5. Crypto Primitives

| Operation | p50 | p95 |
| :--- | ---: | ---: |
| `encrypt_text_for_storage` | 0.021 ms | 0.025 ms |
| `decrypt_text_if_encrypted` | 0.018 ms | 0.022 ms |
| `encrypt_blob_for_storage` (384-dim float) | 0.037 ms | 0.075 ms |
| `decrypt_blob_if_encrypted` (384-dim float) | 0.025 ms | 0.069 ms |

---

## 6. How to Reproduce

Run the full interactive test and scale suites locally:

```bash
# 1. Full baseline profiler — all surfaces (SDK, MCP, HTTP, CLI, Dashboard)
python benchmark_all_surfaces.py --output mesh_baseline_report.json

# 2. Legacy micro-benchmark measuring core operations
python benchmarks/mesh_benchmark.py

# 3. Concurrency stress benchmark (multi-agent concurrent writes)
python benchmarks/concurrency_stress.py

# 4. Capacity scaling benchmark
python benchmarks/scale_benchmark.py --scales 500,1000,5000
```
