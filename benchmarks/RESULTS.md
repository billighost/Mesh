# Mesh Performance & Scale Benchmark Results

This document provides empirical latency, throughput, memory, and scaling benchmarks for **Mesh** (`mesh-context-layer`). All tests measure real end-to-end execution paths including SQLite WAL transactions, token budgeting, FTS5 lexical matching, and ONNX SIMD cosine vector ranking.

## Mesh 0.9.0 measured checkpoint (2026-11-08, `fc7d6a5`)

[Normal CI](https://github.com/billighost/Mesh/actions/runs/42424338520) passed all 18 jobs. [Production](https://github.com/billighost/Mesh/actions/runs/42424338605) passed full primary/floor qualification, installed-wheel validation, process-crash recovery, the registered storage matrix and test-speed observation; all fifteen retrieval jobs failed overall. Primary/floor suites each selected 1,754 cases, with 3/4 skips, no failures/errors, and 74.30%/74.26% coverage. [Provenance](results/production_20261108_fc7d6a5_provenance.json) binds every measurement to `fc7d6a5`; the earlier checkpoints remain below.

This is a single registered pass on Windows 2025Server, four logical CPUs and 15.99 GiB RAM, with Python 3.14.0, APSW 3.54.1.0, SQLite 3.54.1 and ONNX Runtime 1.31.1. Storage uses WAL/FULL, public visibility and verified receipts. Sampling can miss short-lived processes, summed RSS can count shared pages twice, and reported hardware does not establish identical host load. Crash recovery tests do not establish physical power-cut survival.

The [storage ledger](results/production_performance_20261108_fc7d6a5.json) retains all 57 cells: 53 completed and four deferred, with no failed or unavailable cells. Primary coverage is 46/48; all 23,552 attempted measured facts were acknowledged with zero unknown outcomes. Two sixteen-agent neural receipt-SDK cells were resource deferred; two ungrouped 10,000-fact HTTP cells were budget deferred. Five pilots remain separate. Selected sixteen-agent grouped HTTP rates, excluding anchors and process close:

| Backend | Measured facts | Call | Durable facts/s | Indexed facts/s |
| --- | ---: | --- | ---: | ---: |
| Hash | 512 | Scalar | 82.38 | — |
| Hash | 512 | Atomic 8 | 150.29 | — |
| Neural | 512 | Scalar | 75.42 | 46.02 |
| Neural | 512 | Atomic 8 | 137.61 | 62.77 |
| Hash | 10,000 | Scalar | 67.31 | — |
| Neural | 10,000 | Scalar | 52.28 | 37.37 |

The sixteen-agent 512-fact neural scalar call measured p50/p95/p99 202.09/221.61/285.91 ms; atomic-eight calls measured 851.78/923.68/1,020.59 ms per call. At 10,000 facts, neural exact-ID lookup p50/p95 was 5.83/10.84 ms and neural-hybrid anchor search 50.97/91.62 ms. These sixty anchor queries are functional checks. Scale peak summed RSS was 909.00 MiB (hash) and 1,041.31 MiB (neural), including clients; the corresponding HTTP-server role peaks were 228.99/347.81 MiB. **There is no confirmed storage speedup**: the completed grouped scale rates are below the earlier `e353bc1` single-pass 138.63/113.64 facts/s. The ledger retains all latency, resource, admission, indexing and cleanup boundaries.

The [matched SciFact comparison](results/production_comparison_20261108_fc7d6a5.json) uses the same declared compact-96 workload: 5,183 original parents, 12,166 passages, 300 queries, pinned inputs/model, readiness settings, protocol and reported dependency versions. Against `e353bc1`, query-call elapsed time fell by 92.25% (12.902× observed ratio), while backfill became more expensive and query RSS increased:

| Matched compact-96 observation | `e353bc1` | `fc7d6a5` | Change |
| --- | ---: | ---: | ---: |
| 300 query calls: elapsed seconds | 2119.335 | 164.263 | -92.25% |
| 300 query calls: CPU seconds | 2106.859 | 161.000 | -92.36% |
| Passage backfill: elapsed seconds | 204.102 | 464.406 | +127.54% |
| Passage backfill: CPU seconds | 346.891 | 829.812 | +139.21% |
| Sampled query process RSS, MiB | 296.41 | 483.50 | +63.12% |

Derived sequential query-call throughput was 0.14155 → 1.82634 queries/s (query count divided by summed measured call elapsed time). The current passage phase reports 300/300 cached queries, no streaming or oversized generations, and 251,401,672 bytes charged within the unchanged 256 MiB cache budget. This is one pass on separate hosted workers and different sources/database snapshots. The identical preparer includes a preparation timestamp, so dataset-manifest hashes differ; pinned input hashes match, while raw manifest payloads are unavailable. Old cache-mode telemetry is unavailable. These observations establish no generalized speedup or quiet latency/SLO. Matched SciFact passage ranking metrics are unchanged since `e353bc1`; the earlier NFCorpus passage conditions were unscored.

For the [same-source retrieval aggregate](results/production_retrieval_20261108_fc7d6a5.json), Hit@k means at least one authorized annotated primary-source memory is present in the first k results; it does not measure a generated answer or multi-source completeness. All three profiles repeat the same datasets; ranges reflect observed preceding-phase variation and are not attributed to the later profile selection. Six bounded native C4 conditions pass their own gates; nine reject resource limits before scoring. No profile is globally qualified and defaults remain unchanged.

| Fixture (queries) | Fresh default Hit@1 / @5 / @10, % | Passages Hit@1 / @5 / @10, % | Native C4, all three profiles |
| --- | --- | --- | --- |
| Direct facts (16) | 81.25 / 100.00 / 100.00 | 81.25 / 100.00 / 100.00 | 100 / 100 / 100; native gates pass |
| Long-text regression (6) | 0.00 / 0.00 / 100.00 | 33.33 / 100.00 / 100.00 | 100 / 100 / 100; native gates pass |
| Inspected LongMemEval subset (6) | 50.00 / 100.00 / 100.00 | 50.00 / 100.00 / 100.00 | Resource rejected; unscored |
| NFCorpus (323) | 49.54–49.85 / 67.18 / 74.30 | 45.82–46.13 / 64.40 / 71.52 | Resource rejected; unscored |
| SciFact (300) | 56.00 / 78.33 / 84.67 | 57.33 / 79.67 / 83.67 | Resource rejected; unscored |

Direct-fact baseline/passage gates still require 95% Hit@1; their C64/C96, fresh-default and runtime-passage results fail. Long-text baseline/passage gates require 5/6 and all four fail. The LongMemEval runtime-passage gate also fails its 5/6 threshold. The native gains on the sixteen- and six-query regression fixtures do not establish population accuracy or competitor superiority. SciFact passages add four net first-result hits (+1.33 percentage points), with Hit@10 one point lower; this preceding-phase result already existed in `e353bc1`.

Every failed gate, threshold, phase, source/model/protocol/dataset binding and unscored progress record is preserved in the aggregate. Native C4 rejected added resource costs below; values are milliseconds, with p95/CPU limits of 50 and p99 of 100:

| Dataset | Profile | Failed native resource gates |
| --- | --- | --- |
| longmemeval | compact-128 | p95 96.98 > 50; p99 119.70 > 100; cpu 82.11 > 50 |
| longmemeval | compact-96 | p95 64.04 > 50; cpu 54.84 > 50 |
| longmemeval | full-256 | p95 198.26 > 50; p99 203.73 > 100; cpu 164.83 > 50 |
| nfcorpus | compact-128 | p95 65.08 > 50; p99 101.36 > 100 |
| nfcorpus | compact-96 | p95 92.61 > 50; p99 1315.94 > 100; cpu 60.37 > 50 |
| nfcorpus | full-256 | p95 122.16 > 50; p99 1025.47 > 100; cpu 73.34 > 50 |
| scifact | compact-128 | p95 175.44 > 50; p99 884.38 > 100; cpu 147.45 > 50 |
| scifact | compact-96 | p95 152.03 > 50; p99 1198.35 > 100; cpu 127.92 > 50 |
| scifact | full-256 | p95 193.39 > 50; p99 1191.94 > 100; cpu 162.86 > 50 |

The [test-speed comparison](results/production_test_speed_20261108_fc7d6a5.json) uses the same 1,754-case selection with four skips and no failures/errors. One worker took 239.241 seconds; two took 136.650 seconds (1.751× observed ratio; 42.88% lower wall time). Sampled summed process-tree RSS rose from 934.69 to 1158.38 MiB. This fixed-order hosted-runner observation preserves full selection/source/cleanup checks and does not support a generalized speedup claim. Optional passage/reranker changes and successor sources still require independent qualification before becoming defaults.


## Mesh 0.9.0 completed qualification checkpoint (2026-11-07, `e353bc1`)

[Normal CI](https://github.com/billighost/Mesh/actions/runs/42378934838) passed the supported platform/Python checks. [The production run](https://github.com/billighost/Mesh/actions/runs/42378934727) passed both full primary/minimum qualification suites, same-source process-crash recovery, coverage, installed-wheel validation, the registered concurrency matrix and test-speed observation. The production run remains failed because retrieval does not qualify. [Provenance](results/production_20261107_e353bc1_provenance.json) binds these new numerical artifacts to the tested commit; the earlier `6060925` results remain below.

### Windows production matrix — Mesh 0.9.0 (2026-11-07)

A preregistered single serial pass on Windows 2025Server, 4 logical CPUs, 15.99 GiB RAM; Python 3.14.0, APSW 3.54.1.0, SQLite 3.54.1, ONNX Runtime 1.31.1. Each primary cell uses 512 measured facts plus twelve anchors; atomic calls contain eight facts. The receipt SDK and authenticated HTTP use the same public fixture, ownership policy and WAL/FULL durability. The process-tree RSS cap was 4.00 GiB, with 2 GiB available-RAM headroom for resource qualification.

The tested source is Mesh 0.9.0 commit `e353bc1`. Its Windows checkout bytes are identified by the relative source and harness manifests in [the numerical ledger](results/production_performance_20261107_e353bc1.json) (`runtime_source_sha256` and `harness_source_sha256`); these measurements apply to that commit. The ledger also binds the local model files, fixture digests and qualification reports.

Acknowledged measured facts per second, excluding anchors and process close:

| Backend | Surface | Call | Group | 1 agent | 4 agents | 8 agents | 16 agents |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| Hash | Receipt SDK | Scalar | 0 | 236.5 | 33.7 | 33.1 | 34.0 |
| Hash | HTTP | Scalar | 0 | 139.0 | 136.6 | 129.6 | 138.8 |
| Hash | HTTP | Scalar | 1 | 51.6 | 149.3 | 157.4 | 160.0 |
| Hash | Receipt SDK | Atomic 8 | 0 | 352.4 | 187.9 | 104.9 | 84.4 |
| Hash | HTTP | Atomic 8 | 0 | 295.3 | 312.4 | 299.8 | 269.0 |
| Hash | HTTP | Atomic 8 | 1 | 244.9 | 311.7 | 305.1 | 273.7 |
| Neural | Receipt SDK | Scalar | 0 | 208.4 | 31.8 | 34.4 | deferred(resource) |
| Neural | HTTP | Scalar | 0 | 125.2 | 108.4 | 128.3 | 129.1 |
| Neural | HTTP | Scalar | 1 | 61.5 | 119.7 | 152.7 | 143.9 |
| Neural | Receipt SDK | Atomic 8 | 0 | 228.1 | 142.4 | 79.1 | deferred(resource) |
| Neural | HTTP | Atomic 8 | 0 | 207.7 | 272.5 | 259.7 | 254.8 |
| Neural | HTTP | Atomic 8 | 1 | 159.5 | 220.7 | 275.7 | 270.6 |

Primary coverage: 48 planned; 46 completed, 0 failed, 2 deferred, 0 unavailable. Known accounting: 23,552 attempted, 23,552 acknowledged, 0 failed acknowledgements, 0 unknown outcomes and 0 observed committed facts without acknowledgement. All completed cells passed independent ID/content/revision, receipt, integrity, policy, resource and cleanup checks. Every missing cell retains its planned dimensions and reason in the ledger; failed rates are diagnostic.

The separate sixteen-agent HTTP scalar scale extension uses 10,000 measured facts plus twelve anchors:

| Backend | Group | Status | Acknowledged facts/s | Indexed facts/s | Peak summed RSS MiB |
| --- | --- | --- | ---: | ---: | ---: |
| Hash | 0 | completed | 115.2 | — | 942.2 |
| Hash | 1 | completed | 138.6 | — | 942.6 |
| Neural | 0 | completed | 100.9 | 71.1 | 1,097.5 |
| Neural | 1 | completed | 113.6 | 77.1 | 1,097.7 |

Five planned 128-fact resource pilots are recorded separately and excluded from primary and scale rates. The ledger preserves independently verified insertion rates, neural indexing boundaries, successful scalar or batch-call p50/p95/p99, first/repeated public recall latency, per-anchor Recall@1/@5, actual group coalescing counters and RSS/CPU by phase and role, plus the post-close footprint. Summed RSS can double-count shared pages; sampling can miss short-lived processes and phase CPU attribution is approximate. One pass establishes coverage, with no run-to-run repeatability claim. The twelve anchors are functional retrieval checks; they do not establish held-out competitor superiority. Same-source crash tests establish process recovery, without establishing physical power-loss durability.

### Full-suite test speed — Mesh 0.9.0 (`e353bc1`)

The same 1,691-test selection, fixed Hypothesis seed and CI profile completed with zero failures/errors in both observations, with 4 skips. One worker took 204.027 seconds; two took 125.953 seconds (observed 1.62× wall ratio). Sampled peak summed process-tree RSS was 970.7 MiB and 1,230.5 MiB, respectively. This is one fixed-order observation on a four-CPU Windows hosted runner. Cache/order effects and sampled process accounting prevent a generalized speedup claim. [The comparison](results/production_test_speed_20261107_e353bc1.json) preserves source, selection, dependency and cleanup accounting.

### Retrieval qualification — Mesh 0.9.0 (`e353bc1`)

[The sanitized aggregate](results/production_retrieval_20261107_e353bc1.json) preserves all fifteen dataset/profile jobs, source/model/protocol/dataset hashes, original evidence accounting, scored conditions, failed gates and phases that did not run. All fifteen jobs failed overall. Five bounded native C4 cells pass their own required gates; no profile is qualified across the complete grid. `complete` records execution; scored metrics and qualification gates are reported separately.

| Bounded fixture, Mesh 0.9.0 `e353bc1` | Answerable queries | Fresh-default Hit@1 | Runtime-passages Hit@1 | Native C4 result |
| --- | ---: | ---: | ---: | --- |
| Direct facts | 16 | 13/16 | 13/16 | 16/16; all three profiles pass their native gates |
| Early/middle/late long text | 6 | 0/6 | 2/6 | Compact-96 and compact-128: 6/6 and native gates pass; full-256 resource rejected, unscored |
| Inspected LongMemEval exact-turn subset | 6 | 3/6 | 3/6 | Both compact profiles: 4/6, below required 5/6; full-256 resource rejected, unscored |

The unchanged baseline/passage gates remain failed: direct-fact Hit@1 requires 95%, and both six-query fixtures require 5/6. Full-256 exceeds native added p99 on long text and added p95/p99/CPU on LongMemEval. Later legacy child-memory C4 runs also exceed their RSS cap on both compact LongMemEval jobs. Their failure is separate from the scored native quality miss. All nine bounded runtime-passage conditions now complete, including verification of immutable SQLite snapshots; this is a harness-correctness result rather than a retrieval speedup.

All six public NFCorpus/SciFact jobs exhaust the unchanged 3,600 sampled accumulated process-tree CPU-second limit. Five stop during runtime passage indexing/queries; SciFact compact-96 completes that condition and then stops during native C4 measurement. No public native C4 condition has a scored or qualified result. Selecting a C4 input profile does not alter its preceding passage-index/query phase, so the one completed passage run does not establish a compact-profile speedup.

The completed SciFact passage condition on Mesh 0.9.0 `e353bc1` has 5,183 original parents and 12,166 passage rows. Backfill/readiness takes 204.102 wall seconds and 346.891 worker CPU seconds; 300 fresh SDK passage queries take 2,119.335 elapsed seconds and 2,106.859 worker CPU seconds. Queries account for about 86% of the worker's recorded CPU. These are loaded development diagnostics, with no quiet-latency or production-SLO claim; the exact cached/streaming mechanism is not exposed in this sanitized result.

| SciFact metric, 300 answered queries; Mesh 0.9.0 `e353bc1` | Fresh default | Runtime passages |
| --- | ---: | ---: |
| Hit@1 | 56.00% | 57.33% |
| Hit@5 | 78.33% | 79.67% |
| Hit@10 | 84.67% | 83.67% |
| MRR@10 | 0.657718 | 0.667353 |
| nDCG@10 | 0.695711 | 0.704664 |
| Document recall@10 | 0.832722 | 0.826222 |

Top-one paired accounting is 168 baseline hits, 172 passage hits, 21 lost baseline hits and 25 gained hits. The net gain of four does not establish zero per-query loss. Six exported passage candidates have no faithful source association and remain unscored. The existing passage nDCG/document-recall nonregression gates pass their unchanged −0.01 thresholds; they do not replace native C4's separate zero-loss and resource gates. These bounded fixtures and failed public experiments establish no competitor superiority, full LongMemEval result, generated-answer accuracy or physical power-cut validation. Both optional features require further qualification before use as defaults.

## Mesh 0.9.0 production qualification (2026-11-07, `6060925`)

[Normal CI](https://github.com/billighost/Mesh/actions/runs/42338200399) passed all fifteen OS/Python combinations (Ubuntu, macOS, Windows; Python 3.10–3.14), security, documentation, skills, packaging and installed-wheel checks. [Production qualification](https://github.com/billighost/Mesh/actions/runs/42338200383) passed the full isolated primary and minimum suites, process-crash recovery, coverage and primary installed-wheel validation on the same source. [Provenance](results/production_20261107_provenance.json) binds the exported numerical evidence. Retrieval and the subsequent schema-6 passage candidate still require qualification.

### Windows production matrix — Mesh 0.9.0 (2026-11-07)

A preregistered single serial pass on Windows 2025Server, 4 logical CPUs, 15.99 GiB RAM; Python 3.14.0, APSW 3.54.1.0, SQLite 3.54.1, ONNX Runtime 1.31.1. Each primary cell uses 512 measured facts plus twelve anchors; atomic calls contain eight facts. The receipt SDK and authenticated HTTP use the same public fixture, ownership policy and WAL/FULL durability. The process-tree RSS cap was 4.00 GiB, with 2 GiB available-RAM headroom for resource qualification.

The tested source is commit `6060925`. Its Windows checkout bytes are identified by the relative source and harness manifests in [the numerical ledger](results/production_performance_20261107.json) (`runtime_source_sha256` and `harness_source_sha256`); these measurements apply to that commit. The ledger also binds the local model files, fixture digests and qualification reports.

Acknowledged measured facts per second, excluding anchors and process close:

| Backend | Surface | Call | Group | 1 agent | 4 agents | 8 agents | 16 agents |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| Hash | Receipt SDK | Scalar | 0 | 132.7 | 17.5 | 17.5 | 12.7 |
| Hash | HTTP | Scalar | 0 | 85.2 | 82.3 | 76.4 | 80.9 |
| Hash | HTTP | Scalar | 1 | 45.7 | 86.2 | 90.2 | 89.1 |
| Hash | Receipt SDK | Atomic 8 | 0 | 199.5 | 128.0 | 64.2 | 37.8 |
| Hash | HTTP | Atomic 8 | 0 | 179.1 | 178.7 | 172.3 | 166.6 |
| Hash | HTTP | Atomic 8 | 1 | 149.0 | 175.0 | 181.6 | 144.8 |
| Neural | Receipt SDK | Scalar | 0 | 115.2 | 17.8 | 16.6 | deferred(resource) |
| Neural | HTTP | Scalar | 0 | 76.4 | 75.0 | 75.9 | 74.8 |
| Neural | HTTP | Scalar | 1 | 42.2 | 77.6 | 83.0 | 83.4 |
| Neural | Receipt SDK | Atomic 8 | 0 | 133.3 | 74.5 | 45.1 | deferred(resource) |
| Neural | HTTP | Atomic 8 | 0 | 122.8 | 157.9 | 152.8 | 146.8 |
| Neural | HTTP | Atomic 8 | 1 | 118.4 | 144.1 | 161.8 | 154.2 |

Primary coverage: 48 planned; 46 completed, 0 failed, 2 deferred, 0 unavailable. Known accounting: 23,552 attempted, 23,552 acknowledged, 0 failed acknowledgements, 0 unknown outcomes and 0 observed committed facts without acknowledgement. All completed cells passed independent ID/content/revision, receipt, integrity, policy, resource and cleanup checks. Every missing cell retains its planned dimensions and reason in the ledger; failed rates are diagnostic.

The separate sixteen-agent HTTP scalar scale extension uses 10,000 measured facts plus twelve anchors:

| Backend | Group | Status | Acknowledged facts/s | Indexed facts/s | Peak summed RSS MiB |
| --- | --- | --- | ---: | ---: | ---: |
| Hash | 0 | completed | 68.6 | — | 915.7 |
| Hash | 1 | completed | 79.9 | — | 924.3 |
| Neural | 0 | deferred(budget) | — | — | — |
| Neural | 1 | completed | 63.0 | 44.5 | 1,057.8 |

Five planned 128-fact resource pilots are recorded separately and excluded from primary and scale rates. The ledger preserves independently verified insertion rates, neural indexing boundaries, successful scalar or batch-call p50/p95/p99, first/repeated public recall latency, per-anchor Recall@1/@5, actual group coalescing counters and RSS/CPU by phase and role, plus the post-close footprint. Summed RSS can double-count shared pages; sampling can miss short-lived processes and phase CPU attribution is approximate. One pass establishes coverage, with no run-to-run repeatability claim. The twelve anchors are functional retrieval checks; they do not establish held-out competitor superiority. Same-source crash tests establish process recovery, without establishing physical power-loss durability.

### Full-suite test speed — Mesh 0.9.0 (`6060925`)

The same 1,364-test selection, fixed Hypothesis seed and CI profile completed with zero failures in both observations, with four skips. One worker took 162.279 seconds; two took 100.104 seconds (an observed 1.62× wall ratio). Sampled peak summed process-tree RSS was 916.7 MiB and 1,162.2 MiB, respectively. This is one fixed-order observation on a four-CPU Windows hosted runner; cache/order effects and sampled process accounting prevent a generalized speedup claim. [The comparison](results/production_test_speed_20261107.json) preserves source, dependency, selection, skip, cleanup and resource accounting.

## Mesh 0.9.0 working-tree retrieval investigation (2026-11-06)

[The sanitized aggregate](results/production_retrieval_20261106.json) identifies
each tested runtime, dataset, model, protocol, and scorer by SHA-256. These are
development retrieval checkpoints, preceding the published production matrix above. Final retrieval qualification remains pending; these cells establish no competitor superiority or physical power-loss result.

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
