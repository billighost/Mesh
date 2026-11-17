# Mesh Performance & Scale Benchmark Results

This document provides empirical latency, throughput, memory, and scaling benchmarks for **Mesh** (`mesh-context-layer`). All tests measure real end-to-end execution paths including SQLite WAL transactions, token budgeting, FTS5 lexical matching, and ONNX SIMD cosine vector ranking.

## Mesh 0.9.0 measured checkpoint (2026-11-17, `b55c980`)

[Push CI](https://github.com/billighost/Mesh/actions/runs/44085115201) and [PR CI](https://github.com/billighost/Mesh/actions/runs/44085124087) each passed all 18 jobs. The [production run](https://github.com/billighost/Mesh/actions/runs/44085115248) passed both full correctness qualification jobs, storage and test-speed; all five ordinary retrieval jobs failed overall. [Provenance](results/production_20261117_b55c980_provenance.json) binds the completed observations to `b55c98013b95384a632878041cc499165bfe7610`, retains original artifact/member digests and 60 verified Git source bindings, and preserves Windows checkout hash interpretations separately from immutable Git blobs.

Each qualification XML retains 1,944 unique testcase records: primary has 1,941 passed and three skipped; floor has 1,940 passed and four skipped, with zero failures/errors. Each XML also declares a separate 2,149-test suite counter; it is retained distinctly rather than substituted for actual testcase records. Primary sdist collection reports 1,944 tests across 97 files. Coverage is 74.39%/74.34%. Real process-crash recovery and primary wheel/sdist, installed-wheel, CLI, doctor and installer validation passed. Primary uses Windows Python 3.14.0, APSW 3.54.1.0 and ONNX Runtime 1.32.0; floor uses Windows Python 3.10.11, APSW 3.51.3.0 and ONNX Runtime 1.23.2. These checks qualify those sources/environments; any later release commit requires current correctness qualification.

The preceding source `1f370d58` passed [normal CI](https://github.com/billighost/Mesh/actions/runs/44081865548) in 18 jobs, while its [production run](https://github.com/billighost/Mesh/actions/runs/44081865559) passed floor and failed primary on the cold-start writer-admission deadline; dependent measurements were skipped. `b55c980` replaces that test's elapsed-time assumption with causal lock/queue synchronization and does not change runtime behavior. Historical failures remain below.

The [storage ledger](results/production_performance_20261117_b55c980.json) retains all 58 planned cells: 55 completed, three deferred, zero failed/unavailable. Coverage is 46/48 primary, 3/4 scale, all five pilots and the separate 10,000-fact neural grouped HTTP diagnostic. Primary accounting records 23,552 attempted/acknowledged measured facts, scale 30,000/30,000 and diagnostic 10,000/10,000, with zero failed acknowledgements or unknown outcomes. Completed cells passed the published identity/content/revision, receipt, integrity, policy, readiness, resource and owned-process checks. Two sixteen-agent neural receipt-SDK cells remain resource deferred and the ungrouped neural scale cell remains budget deferred. Selected sixteen-agent grouped HTTP rates, excluding anchors and process close:

| Backend | Measured facts | Call | Durable facts/s | Indexed facts/s |
| --- | ---: | --- | ---: | ---: |
| Hash | 512 | Scalar | 80.92 | — |
| Hash | 512 | Atomic 8 | 143.55 | — |
| Neural | 512 | Scalar | 73.80 | 45.71 |
| Neural | 512 | Atomic 8 | 138.83 | 61.84 |
| Hash | 10,000 | Scalar | 70.11 | — |
| Neural | 10,000 | Scalar | 56.56 | 38.88 |

The storage pass used Windows 2025Server, four logical CPUs, 15.99 GiB RAM, patched SQLite 3.54.1 and WAL/FULL. The diagnostic now completed and retains bounded wrapped stage spans and histogram quantile upper bounds, with zero recorded profile loss counters. It is instrumented and excluded from headline throughput. Stage times overlap and are not additive critical-path percentages; calling-thread CPU excludes native ONNX threads and phase attribution is approximate. Failed wrapped spans include acquisition deferrals and do not establish failed user operations. Review checked public projection arithmetic/accounting, source seals and histogram consistency; raw private worker samples are absent from the uploaded ZIPs. The measured rates establish no generalized storage speedup. Summed sampled RSS can double-count shared pages and miss peaks, twelve anchors are functional checks, and process-crash recovery does not establish physical power-cut survival.

The [same-source test-speed observation](results/production_test_speed_20261117_b55c980.json) completed the identical reported 1,944-case selection, with four skips and zero failures/errors, once with one worker and once with two: 244.251 → 150.551 seconds (38.36% lower; 1.622× observed ratio). Sampled summed peak RSS rose from 955.67 to 1303.32 MiB and sampled CPU from 203.484 to 236.750 seconds. Published observation files, selected/skipped hashes, sources and cleanup agree; raw speed XML is not retained in that artifact. This fixed-order, single-host observation establishes no generalized speedup or repeatability claim.

The [ordinary retrieval aggregate](results/production_retrieval_20261117_b55c980.json) retains all five failed jobs, 25 failed gates and 95 phases. The table reports authorized annotated original-parent evidence Hit@1 on answerable queries; it does not measure generated-answer correctness or full-population LongMemEval. Rank-derived diagnostic counts were checked against the sealed quality reports. The separate competitor comparison below is a distinct fixed-profile pass and is not substituted for ordinary qualification.

| Fixture (answerable queries) | Fresh default Hit@1 | Runtime passages Hit@1 | Native compact-80 Hit@1 |
| --- | ---: | ---: | --- |
| Direct facts (16) | 13/16 (81.25%) | 13/16 (81.25%) | 16/16; own gates pass |
| Positional long text (6) | 0/6 (0.00%) | 2/6 (33.33%) | 6/6; own gates pass |
| Inspected LongMemEval subset (6) | 3/6 (50.00%) | 3/6 (50.00%) | 4/6; required 5/6, quality rejected |
| NFCorpus (323) | 160/323 (49.54%) | 148/323 (45.82%) | Resource rejected; unscored |
| SciFact (300) | 168/300 (56.00%) | 172/300 (57.33%) | Resource rejected; unscored |

Direct-fact and long-text native rerankers pass their own bounded gates, while LongMemEval retains 4/6 first-result hits against the required 5/6. A later separate legacy direct-fact C4 condition exceeds its added p95 gate (60.856 ms against 50 ms). Later legacy LongMemEval C4 exceeds sampled RSS (829,378,560 bytes against 805,306,368); scoring did not run and paired resource observations remain null. Those later failures do not erase the earlier completed native scores.

NFCorpus native added p95/p99/CPU are 110.748/908.765/92.009 ms and SciFact 138.336/1,019.750/115.833 ms against unchanged 50/100/50 ms ceilings. Both retain complete matched-query resource progress but remain unscored. No optional profile is qualified across these datasets, and passage indexing and reranking remain default off. Historical results and the independent field-test archive are preserved. Later storage/test-speed reuse requires exact Git path sets, file modes, blob IDs and ancestry, separately from fresh final-source CI and correctness qualification.

### Separate E5 model shadow

The [all-five E5 export](results/production_e5_20261117_b55c980.json) records the separate [run 44086750785](https://github.com/billighost/Mesh/actions/runs/44086750785) at the same `b55c980` source. All five native MiniLM baselines completed their selected queries and cleanup. E5 then ran on every dataset. The pinned quantized `e5-small-v2` profile uses 256-token prefixed overlapping windows and window-mean pooling; it differs from upstream 512-token encoding and remains a benchmark-only trial.

| Dataset | Native baseline queries completed | E5 outcome | E5 shadow queries completed |
| --- | ---: | --- | ---: |
| Direct facts | 22 | Scored; quality rejected | 22 |
| Positional long text | 9 | Scored; quality rejected | 9 |
| Inspected LongMemEval subset | 6 | Native RSS rejected; quality unscored | 0 |
| NFCorpus | 323 | Native RSS rejected; quality unscored | 0 |
| SciFact | 300 | Native RSS rejected; quality unscored | 0 |

Direct-fact first-result evidence hits remain 13/16 with both encoders, but E5 loses two baseline hits and gains two others while MRR/nDCG decrease. Long text remains 0/6 at the first result. Both scored shadows pass their paired resource gates and fail quality gates. Completion and equal aggregate Hit@1 therefore do not qualify the model.

The other three shadows began native indexing and exceeded their dynamic RSS ceilings before any shadow query. Those ceilings are the observed baseline RSS plus 128 MiB, separately from the 768 MiB global ceiling:

| Dataset | Observed native E5 RSS, bytes | Dynamic RSS ceiling, bytes |
| --- | ---: | ---: |
| Inspected LongMemEval subset | 393,875,456 | 371,191,808 |
| NFCorpus | 594,677,760 | 594,661,376 |
| SciFact | 663,019,520 | 662,933,504 |

Their quality scoring did not run and all final paired normal-cache/cache-cold resource observations remain null. Native sampled RSS is a separate retained observation. This differs from historical `e4284c8`, where E5 itself never ran after baseline subprocess failures. No E5 model adoption, production-default change or resource-limit relaxation follows from either experiment.


## Mesh 0.9.0 native competitor comparison (2026-11-17, `2db6229`)

[Run 44088496624](https://github.com/billighost/Mesh/actions/runs/44088496624) completed all twenty cells: Mesh 0.9.0, Mem0 2.2.1, Basic Memory 0.24.1 and Auxly 1.5.0 on five datasets. The [numerical export](results/comparison_retrieval_20261117_2db6229.json) retains every quality, latency, CPU/RSS and ingest-accounting observation. Its [provenance](results/comparison_20261117_2db6229_provenance.json) records the original twenty ZIP digests, member hashes, 52 Git source bindings across 1,040 source observations, fixed inputs/models, full host and official child inventories, and independent artifact/arithmetic review. Comparator code, contract tests and workflow remain on the separate immutable [comparison source](https://github.com/billighost/Mesh/blob/2db6229494a223feaa618de0cbf901650ba0d037/benchmarks/COMPARISON.md).

The cells actually consumed qualification snapshots and frozen host constraints from `e4284c8fb5ba565043accccbdd6353839c3eb43a` (production run `43906485048`). Their Mesh runtime and `pyproject.toml` Git content are independently identical to the currently qualified core `b55c98013b95384a632878041cc499165bfe7610`; the comparator overlay has its own source SHA. Native comparison used Linux x86-64, Python 3.12.16 and four logical CPUs. Correctness qualification used Windows Python 3.14/3.10. Shared distribution version equality does not establish identical native environments or platform wheel bytes. Official Mem0 and Basic Memory run in separate frozen child environments because their supported dependencies conflict with the qualified Mesh host.

| Dataset | Original parents per tool | Selected queries per tool | Answerable queries per tool |
| --- | ---: | ---: | ---: |
| Direct facts | 50 | 22 | 16 |
| Positional long text | 15 | 9 | 6 |
| Inspected LongMemEval subset | 191 | 6 | 6 |
| NFCorpus | 3,633 | 323 | 323 |
| SciFact | 5,183 | 300 | 300 |

Each tool submitted all 9,072 original parents and executed all 660 selected queries, giving 2,640 query observations. Ranked quality uses the 651 answerable queries per tool, preserving all original positive relevance judgments. Mesh retained ten unfaithful parents (two LongMemEval, one NFCorpus and seven SciFact); all were ingested, and there were no actual content rejections. Unfaithful parents remain in the original relevance denominators and unfaithful returned slots receive no credit at their original rank. The other profiles retained all parents faithfully. Labels are opened only after worker shutdown and closed ranked exports; they are excluded from ranking inputs.

Mesh uses production default SDK hybrid recall with pinned MiniLM, overlapping window pooling, and optional passages/reranker off. Mem0 uses native SDK local Qdrant hybrid search with `infer=False`, no reranker, native FastEmbed 0.9.1 BM25, and identical MiniLM assets truncated at 256 tokens. Basic Memory uses persistent MCP stdio native keyword search with `sqlite-vec` deliberately absent. Auxly uses persistent MCP stdio substring search and arbitrary Go map order. These explicitly configured profiles share the retrieval inputs while retaining different encoders, interfaces and dependency scopes.

The table reports Hit@1 / Hit@5 / Hit@10 as percentages: an answerable query hits when any original positive gold parent is returned within the cutoff. Document recall averages each query's fraction of gold parents found; MRR@10 uses the first positive rank reciprocal. nDCG retains graded qrels with exponential gain `(2^grade - 1)` and logarithmic rank discount. NFCorpus retains 12,334 positive judgments and SciFact 339. Exact-turn and session relevance are separate on LongMemEval; a session hit does not establish complete supporting-turn coverage or a generated answer. The export retains all these metrics.

| Dataset | Mesh 0.9.0 SDK | Mem0 2.2.1 SDK | Basic Memory 0.24.1 MCP |
| --- | ---: | ---: | ---: |
| Direct facts | 81.25 / 100.00 / 100.00 | 75.00 / 100.00 / 100.00 | 75.00 / 100.00 / 100.00 |
| Positional long text | 0.00 / 0.00 / 100.00 | 0.00 / 16.67 / 33.33 | 50.00 / 50.00 / 100.00 |
| Inspected LongMemEval subset | 50.00 / 100.00 / 100.00 | 66.67 / 100.00 / 100.00 | 66.67 / 83.33 / 83.33 |
| NFCorpus | 49.23 / 66.87 / 74.30 | 45.20 / 66.87 / 70.59 | 41.49 / 61.30 / 65.02 |
| SciFact | 56.00 / 78.33 / 84.67 | 52.67 / 75.67 / 83.00 | 43.33 / 58.00 / 63.00 |

Auxly's first ten distinct native matches form an unordered returned subset. They cannot be reported as ranked Hit@10, MRR or nDCG. Its subset observations are:

| Dataset | Returned subset hit, % | Returned subset document recall, % |
| --- | ---: | ---: |
| Direct facts | 6.25 | 6.25 |
| Positional long text | 0.00 | 0.00 |
| Inspected LongMemEval subset | 0.00 | 0.00 |
| NFCorpus | 30.03 | 7.05 |
| SciFact | 0.00 | 0.00 |

Query latency is one sequential pass per cell, including the first call. The following p50/p95 values are milliseconds. SDK and MCP observations are separate interface strata; cross-interface speed ratios and interchangeable storage-throughput claims are unsupported. Ingestion includes each tool's own acknowledgement/readiness or native note materialization/indexing contract.

| Dataset | Mesh SDK p50 / p95 | Mem0 SDK p50 / p95 |
| --- | ---: | ---: |
| Direct facts | 5.85 / 7.08 | 5.45 / 6.23 |
| Positional long text | 9.87 / 11.09 | 5.43 / 7.16 |
| Inspected LongMemEval subset | 13.36 / 14.23 | 7.32 / 7.69 |
| NFCorpus | 43.05 / 75.28 | 100.92 / 111.75 |
| SciFact | 88.42 / 158.41 | 143.00 / 153.46 |

| Dataset | Basic Memory MCP p50 / p95 | Auxly MCP p50 / p95 |
| --- | ---: | ---: |
| Direct facts | 16.80 / 23.01 | 2.58 / 3.39 |
| Positional long text | 33.34 / 927.78 | 1.00 / 1.73 |
| Inspected LongMemEval subset | 1315.04 / 1417.77 | 1.81 / 1.94 |
| NFCorpus | 29.06 / 1367.65 | 66.84 / 70.57 |
| SciFact | 62.50 / 1106.05 | 116.94 / 129.99 |

Each native cell stayed within its registered 45-minute wall limit and 4 GiB sampled worker-tree RSS ceiling. The exports retain sampled worker-plus-child CPU/RSS rather than a standalone service footprint; shared pages can be counted twice and sampling can miss peaks or exited children. All owned processes settled before temporary cleanup. Measurement descendants ran in disconnected Linux network namespaces with isolated homes and no cloud inference.

These are single-pass observations. The direct-fact, positional-long-text and six-query LongMemEval fixtures were previously inspected bounded regressions, without an independent held-out claim. The LongMemEval subset does not repeat the historical 470-answerable-question Mesh 0.8.3 comparison below. No confidence interval, repeatability, general superiority, full-population LongMemEval accuracy or generated-answer correctness is established by this run.

Three earlier Mesh comparator sources (`639df5e`, `74fb945`, `a825f059`; runs `44079651534`, `44082801025`, `44086055561`) produced invalid harness observations. They supplied `index_timeout=120` although Mesh accepts 0–60, and a blanket `ValueError` handler mislabeled configuration errors as content rejection. Their zero quality values are invalid, excluded from these exports and preserved only as superseded historical artifacts. The reviewed overlay validates the supported 60-second wait before ingestion, counts only actual content/guard rejection, and makes other errors fail unscored. The provenance retains this disposition and original summary/source digests.

## Mesh 0.9.0 measured checkpoint (2026-11-16, `e4284c8`)

[Normal CI](https://github.com/billighost/Mesh/actions/runs/43906485367) passed all 18 jobs after attempt 2 completed the two macOS jobs that had not received runners. The [production run](https://github.com/billighost/Mesh/actions/runs/43906485048) passed both full qualification suites and the same-selection test-speed observation, while its storage job and all five retrieval jobs failed overall. The storage failure belongs to an optional diagnostic that stopped before its workload; the primary cells have no failures. The separate [E5 run](https://github.com/billighost/Mesh/actions/runs/43909369990) failed all five baseline subprocesses before E5 executed. [Provenance](results/production_20261116_e4284c8_provenance.json) binds these observations to `e4284c8fb5ba565043accccbdd6353839c3eb43a` and records 60 Git source hashes, accounting for Windows checkout newlines.

Both qualification XMLs were independently checked: each selected 1,915 cases, with 1,912 passes/three skips on primary and 1,911 passes/four skips on floor, zero failures/errors, and 74.38%/74.34% coverage. Real process-crash recovery passed. Primary wheel/sdist, installed-wheel smoke, CLI, doctor and installer dry-run validation passed; the sdist collected the complete selection. Primary uses Python 3.14.0, APSW 3.54.1.0 and ONNX Runtime 1.32.0; floor uses Python 3.10.11, APSW 3.51.3.0 and ONNX Runtime 1.23.2. These checks qualify this source and these environments; later changes require fresh qualification.

The [storage ledger](results/production_performance_20261116_e4284c8.json) retains all 58 cells: 53 completed, four deferred and one optional diagnostic startup failure. Five pilots completed separately. Primary coverage is 46/48 with 23,552 attempted and acknowledged measured facts, zero failed acknowledgements, unknown outcomes or observed unacknowledged committed facts. Every completed primary cell passed independent identity/content/revision, receipt, integrity, policy, resource and cleanup checks. Two sixteen-agent neural receipt-SDK cells remain resource deferred, and the two ungrouped 10,000-fact HTTP cells remain budget deferred. The grouped scale cells completed. Selected sixteen-agent grouped HTTP rates, excluding anchors and process close:

| Backend | Measured facts | Call | Durable facts/s | Indexed facts/s |
| --- | ---: | --- | ---: | ---: |
| Hash | 512 | Scalar | 78.09 | — |
| Hash | 512 | Atomic 8 | 141.37 | — |
| Neural | 512 | Scalar | 74.23 | 45.11 |
| Neural | 512 | Atomic 8 | 131.73 | 61.22 |
| Hash | 10,000 | Scalar | 67.30 | — |
| Neural | 10,000 | Scalar | 50.59 | 35.70 |

This single serial pass uses Windows 2025Server, four logical CPUs and 15.99 GiB RAM, patched SQLite 3.54.1 and WAL/FULL. The optional storage diagnostic exited 2 after 0.506 seconds because its child environment omitted the required Actions marker; its profile remains unobserved and it supports no storage-stage optimization claim. The ledger retains its failure and all deferrals. The scale rates establish no generalized storage speedup; different hosted passes do not establish identical host load. Summed sampled RSS can count shared pages twice and miss short peaks. Twelve anchor queries are functional checks, and process-crash tests do not establish physical power-cut survival.

The [same-source test-speed observation](results/production_test_speed_20261116_e4284c8.json) completed the identical 1,915-case selection, with four skips and zero failures/errors, once with one worker and once with two: 236.447 → 144.240 seconds (39.00% lower; 1.639× observed ratio). Sampled summed peak RSS rose from 962.36 to 1,164.56 MiB and sampled CPU from 199.234 to 232.109 seconds. This fixed-order, single-host observation establishes no generalized speedup or repeatability claim; both invocations used fresh isolated homes and retained source/dependency/cleanup checks.

The [retrieval aggregate](results/production_retrieval_20261116_e4284c8.json) preserves all ten failed jobs, all 24 failed gates and all 155 phases. The table reports annotated authorized primary-source evidence Hit@1 on answerable queries; direct-fact and long-text complete accounting also includes unanswered queries. It does not measure generated-answer correctness or the complete LongMemEval population.

| Fixture (answerable queries) | Fresh default Hit@1 | Runtime passages Hit@1 | Native compact-80 Hit@1 |
| --- | ---: | ---: | --- |
| Direct facts (16) | 13/16 (81.25%) | 13/16 (81.25%) | 16/16; own gates pass |
| Long-text regression (6) | 0/6 (0%) | 2/6 (33.33%) | 6/6; own gates pass |
| Inspected LongMemEval subset (6) | 3/6 (50%) | 3/6 (50%) | 4/6; required 5/6, quality rejected |
| SciFact (300) | 168/300 (56%) | 172/300 (57.33%) | Resource rejected; unscored |
| NFCorpus (323) | 159/323 (49.23%) | 147/323 (45.51%) | Resource rejected; unscored |

The bounded native direct-fact and long-text conditions pass their own gates, but their overall jobs still fail default/passage accuracy gates. LongMemEval native resource gates pass and its score remains below the quality target. A separate later legacy C4 phase exceeds sampled process-tree RSS (811,372,544 bytes against 805,306,368); its scoring is not run and paired resources remain null. That failure does not erase the earlier native score.

SciFact native added p95/p99/CPU are 158.344/1,067.601/80.104 ms, and NFCorpus 107.860/1,254.468/85.720 ms, against unchanged 50/100/50 ms ceilings. Both resource-rejected native conditions retain complete query/resource progress but remain unscored. No optional profile is qualified across these datasets, and passage indexing and reranking remain default off.

The five separate E5 jobs completed all selected native-baseline queries (22/9/6/300/323 for direct facts/long text/LongMemEval/SciFact/NFCorpus), then each baseline subprocess exited 1 with generic `MeasuredSubprocessFailed`. Their baseline RSS stayed below the unchanged ceiling. E5 itself is `not_run`, zero queries, and paired normal-cache/cache-cold resource observations remain null. This is a baseline completion failure, not an E5 quality or resource rejection. The exports retain the failed baseline progress, model/source bindings and unavailable paired observations. No competitor superiority, qualified experimental default or generalized speedup follows from this checkpoint.


## Mesh 0.9.0 qualification checkpoint (2026-11-08, `2d67c704`)

[Normal CI](https://github.com/billighost/Mesh/actions/runs/42485932114) passed all 18 jobs. The [production run](https://github.com/billighost/Mesh/actions/runs/42485932167) passed both full qualification jobs and failed the storage matrix, test-speed reporting, and all five retrieval jobs. The [provenance](results/production_20261108_2d67c704_provenance.json) verifies 54 bound source files against that Git commit, with Windows checkout newlines accounted for, and binds the five retained retrieval artifacts. Qualification logs and the artifact seals agree on 1,853 selected cases, with 3/4 skips and zero failures/errors in primary/floor; the raw qualification XML is no longer retained and cannot be rechecked from these exports.

The [storage failure export](results/production_performance_20261108_2d67c704.json) records `UnsafeRelativeSourceManifest` during registration and finalization, before any matrix cell was measured. The [test-speed failure export](results/production_test_speed_20261108_2d67c704.json) records the same error during report publication. Producer/controller manifests list four benchmark files, while this tested ledger accepts only two. No valid storage ledger or published test-speed comparison exists for this source. The [comparison export](results/production_comparison_20261108_2d67c704.json) therefore remains unavailable; earlier measured rates below do not apply to this checkpoint.

The [retrieval aggregate](results/production_retrieval_20261108_2d67c704.json) preserves all five failed jobs, all 95 phases, all 23 failed gates, and both resource-rejected native conditions as unscored. Hit@k measures retrieval of authorized annotated primary-source evidence, rather than generated-answer accuracy. Two bounded native `compact-80` conditions pass their own gates; no optional profile is globally qualified.

| Fixture (answerable queries) | Fresh default Hit@1 / @5 / @10, % | Passages Hit@1 / @5 / @10, % | Native compact-80 |
| --- | --- | --- | --- |
| Direct facts (16) | 81.25 / 100 / 100 | 81.25 / 100 / 100 | 100 / 100 / 100; own gates pass |
| Long-text regression (6) | 0 / 0 / 100 | 33.33 / 100 / 100 | 100 / 100 / 100; own gates pass |
| Inspected LongMemEval subset (6) | 50 / 100 / 100 | 50 / 100 / 100 | 66.67 / 100 / 100; quality rejected |
| NFCorpus (323) | 49.54 / 67.18 / 74.30 | 45.82 / 64.40 / 71.52 | Resource rejected; unscored |
| SciFact (300) | 56 / 78.33 / 84.67 | 57.33 / 79.67 / 83.67 | Resource rejected; unscored |

LongMemEval native retrieval still gets 4/6 first-result hits against the unchanged 5/6 requirement, with zero lost baseline first-result hits and native resource gates passing. Its separate later legacy C4 phase exceeds the sampled process-tree RSS cap; unavailable final paired resource observations remain `null`. That later failure does not erase the earlier native score.

NFCorpus native added p95/p99 are 50.29/112.13 ms, exceeding the unchanged 50/100 ms ceilings; added CPU is 38.89 ms/query and passes. SciFact native added p95/p99/CPU are 143.58/1,210.89/121.09 ms, exceeding 50/100/50 ms. Both public native conditions stop before scoring. Their preceding passage conditions and fixed score diagnostics remain complete. The optional E5 shadow is explicitly unrun as a separate manual experiment. These single-pass observations and small bounded fixtures establish no competitor superiority, general speedup, qualified default, full LongMemEval result, generated-answer correctness, or physical power-cut validation.


## Mesh 0.9.0 measured checkpoint (2026-11-08, `db5e76a`)

[Normal CI](https://github.com/billighost/Mesh/actions/runs/42431857693) passed all 18 jobs. [Production](https://github.com/billighost/Mesh/actions/runs/42431857665) passed both full qualification suites, primary installed-wheel validation, process-crash recovery, the registered storage matrix and test-speed observation. All five `compact-80` retrieval jobs failed overall; no profile is globally qualified. [Provenance](results/production_20261108_db5e76a_provenance.json) binds the evidence to `db5e76a`. Primary/floor suites selected 1,785 cases with 3/4 skips, zero failures/errors and 74.38%/74.29% coverage. Earlier failed and unscored checkpoints remain below.

The [storage ledger](results/production_performance_20261108_db5e76a.json) retains all 57 cells: 54 completed and three deferred, zero failed/unavailable. Primary coverage is 46/48 with 23,552 measured facts acknowledged and zero unknown outcomes. Two sixteen-agent neural receipt-SDK cells remain resource deferred; the ungrouped neural 10,000-fact HTTP cell remains budget deferred. The ungrouped hash scale cell completed on this pass. Five pilots remain separate. Selected sixteen-agent grouped HTTP rates, excluding anchors and process close:

| Backend | Measured facts | Call | Durable facts/s | Indexed facts/s |
| --- | ---: | --- | ---: | ---: |
| Hash | 512 | Scalar | 73.99 | — |
| Hash | 512 | Atomic 8 | 138.83 | — |
| Neural | 512 | Scalar | 73.09 | 45.39 |
| Neural | 512 | Atomic 8 | 137.37 | 61.46 |
| Hash | 10,000 | Scalar | 69.95 | — |
| Neural | 10,000 | Scalar | 55.51 | 38.02 |

The grouped hash/neural scale rates are still below the earlier `e353bc1` single-pass 138.63/113.64 durable facts/s. Small increases over `fc7d6a5` establish no confirmed storage speedup. The run uses patched APSW 3.54.1.0/SQLite 3.54.1 and WAL/FULL, Python 3.14.0 and ONNX Runtime 1.31.1, on a Windows 2025Server hosted worker with four logical CPUs and 15.99 GiB RAM. Sampled RSS can miss short peaks and sum shared pages; the reported hardware does not establish identical host load. Process-crash tests do not establish physical power-cut survival.

The [pre-C4 passage comparison](results/production_comparison_20261108_db5e76a.json) compares matching declared passage workloads against `fc7d6a5`, with pinned inputs/model, identical preparer/protocol/readiness counters and resource limits. The old job selects compact-96 for its later reranker and the new job selects compact-80; the compared preceding passage condition does not use that profile. These numbers compare source changes in passage search and are not native reranker-profile speed measurements.

| Dataset | Pre-C4 observation | `fc7d6a5` | `db5e76a` | Change |
| --- | --- | ---: | ---: | ---: |
| nfcorpus | Query-call elapsed s | 53.897 | 60.774 | +12.76% |
| nfcorpus | Query-call CPU s | 50.469 | 55.047 | +9.07% |
| nfcorpus | Backfill elapsed s | 179.230 | 302.705 | +68.89% |
| nfcorpus | Backfill CPU s | 271.000 | 539.656 | +99.14% |
| nfcorpus | Sampled query RSS MiB | 541.76 | 517.87 | -4.41% |
| scifact | Query-call elapsed s | 164.263 | 107.821 | -34.36% |
| scifact | Query-call CPU s | 161.000 | 104.516 | -35.08% |
| scifact | Backfill elapsed s | 464.406 | 402.897 | -13.24% |
| scifact | Backfill CPU s | 829.812 | 712.531 | -14.13% |
| scifact | Sampled query RSS MiB | 483.50 | 491.03 | +1.56% |

SciFact has 5,183 parents, 12,166 passages and 300 queries; NFCorpus has 3,592 admitted original parents, 9,406 passages and 323 queries. Both complete with cached queries throughout. Charged generations remain 251,401,672 bytes (SciFact) and 183,862,837 bytes (NFCorpus) within the unchanged 256 MiB budget. SciFact query-call elapsed time fell by 34.36%; NFCorpus query-call time rose by 12.76%, and its backfill costs rose. These single passes on separate workers have distinct database IDs/snapshots and timestamp-bearing dataset manifests. Raw dataset manifest payloads are unavailable; pinned inputs match. They establish no generalized speedup or quiet latency/SLO, and the tradeoffs remain visible.

The [retrieval aggregate](results/production_retrieval_20261108_db5e76a.json) retains all five jobs, all 24 failed gates, every phase and both resource-rejected native conditions as unscored. Hit@k measures authorized annotated primary-source evidence, not a generated answer or complete multi-source reasoning. Two bounded native conditions pass their own gates; LongMemEval now scores but fails quality. Default and passage features remain unqualified.

| Fixture (answerable queries) | Fresh default Hit@1 / @5 / @10, % | Passages Hit@1 / @5 / @10, % | Native compact-80 |
| --- | --- | --- | --- |
| Direct facts (16) | 81.25 / 100.00 / 100.00 | 81.25 / 100.00 / 100.00 | 100 / 100 / 100; own gates pass |
| Long-text regression (6) | 0.00 / 0.00 / 100.00 | 33.33 / 100.00 / 100.00 | 100 / 100 / 100; own gates pass |
| Inspected LongMemEval subset (6) | 50.00 / 100.00 / 100.00 | 50.00 / 100.00 / 100.00 | 66.67 / 100 / 100; quality rejected |
| NFCorpus (323) | 49.23 / 66.87 / 74.30 | 45.51 / 64.09 / 71.52 | Resource rejected; unscored |
| SciFact (300) | 56.00 / 78.33 / 84.67 | 57.33 / 79.67 / 83.67 | Resource rejected; unscored |

Native compact-80 preserves 16/16 direct-fact and 6/6 long-text first-result hits with all native gates passed. Their preceding default/passage gates still fail. The inspected LongMemEval subset improves from 3/6 passage hits to 4/6 native hits with no lost top-one hits, but requires 5/6. Its native added p95/p99/CPU are 27.77/31.25/23.28 ms; total/added RSS are 286.62/38.84 MiB, and all native resource gates pass. Its separate later legacy C4 phase exceeds the 768 MiB RSS ceiling (790.73 MiB sampled), aborts before scoring and remains unscored; that failure does not invalidate the earlier source-bound native score.

NFCorpus native added p95/p99/CPU are 69.24/1,767.31/59.36 ms and SciFact 107.65/826.77/91.41 ms, exceeding the unchanged 50/100/50 ms ceilings. Both native conditions are resource rejected before scoring; their later legacy passage/C4 phases do not run. Public passage first-result hits remain 3.715 points below fresh default on NFCorpus and 1.333 points above on SciFact, whose Hit@10 is one point lower. These failed experiments establish no competitor superiority, full LongMemEval quality, generated-answer correctness or qualified optional default.

The [same-source test-speed observation](results/production_test_speed_20261108_db5e76a.json) completes the same 1,785-case selection, four skips and zero failures/errors, once with one worker and once with two: 192.470 → 117.068 seconds (39.18% lower; 1.644× observed ratio). Sampled summed RSS increases from 929.98 to 1138.70 MiB. This fixed-order hosted observation does not establish a generalized test speedup; the suite differs from the preceding 1,754-case checkpoint.


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
