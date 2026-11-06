# Optional C4 retrieval evaluation

This separate, default-off CI experiment scores the first four distinct authorized
parents from the immutable no-reranker passage result. It uses the previously
pinned MiniLM cross-encoder and introduces no production model catalog entry,
runtime ranking change, dependency, cloud LLM call or local download. The original
long-memory runner and the C64 MiniLM and TinyBERT evidence remain untouched.

The [runner](retrieval_c4.py), [fixed fixture metadata](retrieval_c4_model.py)
and [remote provisioner](provision_retrieval_c4.py) use the shared
[passage harness](passage_retrieval.py). They are benchmark utilities;
the optional cross-encoder is not loaded by the normal Mesh runtime.

The CI API is `retrieval_c4.py freeze`, `provision_retrieval_c4.py`, then
`retrieval_c4.py run`. The run requires separate `--model-directory` and
`--embedding-model-directory`, plus the repository, dataset, base protocol,
C4 protocol, guarded source run, immutable passage run and new output directory.
Every executable phase requires GitHub Actions. Select the separate experiment
with the wrapper's `--reranker-experiment c4` or the reusable workflow's
`run_c4: true` input. Both switches default to off. A failed optional phase preserves
the completed base metrics and marks the optional experiment unqualified.

Matched baseline and C4 phases rebuild the same guarded passage corpus in fresh
subprocesses. Both use the same runtime, saved neural query vectors, 256 MiB cache,
parent budget, query order and five-or-fewer fixed namespace warmups. The baseline
completes before C4 loads one CPU session. CE warmup reuses already retrieved
candidates so it adds no retrieval-cache calls. Resource passes are fixed as
`max(1, ceil(100 / frozen query count))`; repeated samples stay separate from the
single quality export for each query and candidate-cap cell.

Before and after every inference, guarded source reads verify public and
nonquarantined status, namespace, model tag, revision, incarnation, generation,
accepted body hash, selected span identity, source offsets and span hash. A rebuilt
child ID is ephemeral; its parent, passage ordinal and exact source span must match
the immutable result. Supporting spans exceeding 224 CE tokens fail explicitly.
The original-body fallback uses a recorded, deterministic prefix. Queries have a
separate 29-token prefix limit, and BERT's three pair-special tokens keep every
pair within 256 tokens without silently truncating a supporting passage.

Hybrid scores and eligibility fields remain unchanged. The CE logit is exported
separately, ties preserve the original order, and all parents after the first four
retain their order. Attempted, returned, warmup and measured inference counters
remain visible even when a later source fence rejects the result.

These fixed gates are rejection budgets, not performance measurements:

| Manifest resource gate | Maximum |
| --- | ---: |
| `total_rss_mib` | 768 MiB |
| `delta_rss_mib` | 128 MiB |
| `delta_p95_ms` | 50 ms |
| `delta_p99_ms` | 100 ms |
| `delta_cpu_ms` | 50 ms/query |

RSS is the sampled process-tree peak across each matching phase, including its
wrapper, at a 10 ms sampling interval. Latency and CPU include source guards and
use the worse C64/C96 paired result; cold indexing, loading and CE warmup are
reported separately. This is a sampled observation, not an exact allocator peak.
Every gate has a fixed `maximum` and a finite `observed` value or explicit `null`
when measurement was unavailable. Missing or mismatched fixtures, failed source
fences, resource rejection and interruption produce content-free typed evidence.
They stop optional scoring, clean owned processes/files and never select a default.

Complete output manifests retain the base embedding revision, model hashes,
runtime hashes and base protocol hash for the existing scorer. They record the CE
fixture and protocol separately, and `paired_original_condition` is
`no_reranker_passages`. `original_sdk_top10` is that immutable passage baseline.
All exported JSONL filenames fit the reviewed wrapper's existing allowlist.

The separate scorer may open labels only after candidate exports are fixed.
The frozen C4 quality policy requires zero paired loss in parent `hit_at_1`,
`hit_at_5`, `hit_at_10`, `mrr_at_10` and `ndcg_at_10`, separately for C64 and C96
on the public corpora. Each paired mean uses all selected answered queries with
nonempty exact-document qrels; its sample count must match
`gold_survival.answered_queries`. Survival-filtered, strong-grade and session-hit
cohorts remain diagnostics.

The bounded fixtures require zero new exact-evidence top1 regressions against the
matching passage baseline. A regression is a query whose baseline top1 contains
the canonical exact evidence document and whose C4 top1 does not. A mean delta can
hide a regression offset by a gain, so the strict gate needs a per-query-derived
regression count from the separate scorer. Canonical source IDs still use the
first faithfully preserved ingestion association; relevance labels never choose
an alias. The per-cell `candidate_hit_at_1_regressions` report contains
`answered_queries`, `baseline_hits`, `candidate_hits`, `lost_baseline_hits` and
`gained_hits`; the bounded gate requires `lost_baseline_hits == 0` and consistent
counts over the full answered-query denominator. The owning review decides final
qualification after the remote quality, resource, privacy and identity gates.

The [small contract checks](../tests/test_retrieval_c4_contracts.py) use only stdlib
mocks and synthetic data. Local validation covered 20 contracts, AST parsing and
focused lint. No native retrieval, model inference, model download, daemon,
provider call or full test suite was run locally. Remote quality and resource
evaluation remains unrun; the implemented harness and its passing mocked checks
are not evidence of model quality or performance qualification.
