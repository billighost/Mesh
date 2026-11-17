# Optional C4 retrieval evaluation

Native passage query exports retain numeric cached/streaming/unreported result
counts, cache allocation charges and loaded query resources. Completed sanitized
conditions include these counters and both native C4 worker observations. Fixed
role-specific numeric checkpoints also survive abrupt native worker termination.
The supervisor reads unfinished rows only after stopping the worker tree, binds
them to the expected runtime, model, dataset and protocol, ignores an unfinished
final JSONL record, and labels the result `unscored_progress`. Such progress does
not establish ingestion identity, answer accuracy or resource qualification.
Older exports without telemetry retain their existing resource contract.

The legacy default-off CI experiment scores the first four distinct authorized
parents from the immutable no-reranker passage result. It uses the previously
pinned MiniLM cross-encoder and introduces no production model catalog entry,
runtime ranking change, dependency, cloud LLM call or local download. The separate
production implementation is explicitly enabled as described in [DOCS.md](../DOCS.md).
The original
long-memory runner and the C64 MiniLM and TinyBERT evidence remain untouched.

The legacy [runner](retrieval_c4.py), [fixed fixture metadata](retrieval_c4_model.py)
and [remote provisioner](provision_retrieval_c4.py) use the shared
[passage harness](passage_retrieval.py). They are benchmark utilities. The normal
Mesh runtime keeps local reranking disabled unless explicitly configured.

When both `run_c4` and `run_runtime_passages` are requested, the separate
[runtime qualification](runtime_rerank_c4.py) runs before the legacy child-memory
experiment. It calls actual `Mesh.recall` on the actual schema-6 passage index,
paired with the hash-verified ordinary runtime-passage export. Matched native
subprocesses preserve parent identities, base scores, exact supporting spans,
first-four membership and tail order. Every query also gets a separately recorded
`n=1` check against its `n=10` first result. Native session, inference and fresh
query encoding calls are counted. Each configuration uses at least 100 matched
resource samples and the same C4 resource maxima below; cold loading
of verified model bytes contributes to the process-tree RSS measurement. The
separate scorer requires zero paired metric loss and the declared bounded exact
evidence targets. Earlier failed or unqualified conditions remain visible.

The runtime pair freezes a read-only database backup before either measured
recall phase, then uses separate isolated copies of that backup. Both copies
must retain the original store generation, namespace, memory identities, model
and fully indexed passages. The paired phases reuse the verified stored vectors;
primary or passage document inference during these phases rejects the run.
The source database and its immutable ordinary passage export stay unchanged.

The owned frozen backup is finalized in DELETE journal mode and closed before
its fingerprint is recorded. This prevents a later read-only backup from creating
an empty WAL beside the immutable source. Query copies restore the production
WAL/FULL configuration; database and WAL tampering still reject the run.

The actual runtime supports four explicit input profiles:

| `MESH_RERANK_PROFILE` | Query tokens | Passage tokens | Maximum pair tokens |
| --- | ---: | ---: | ---: |
| `full-256` (default) | 29 | 224 | 256 |
| `compact-128` | 29 | 96 | 128 |
| `compact-96` | 29 | 64 | 96 |
| `compact-80` (experimental, unqualified) | 29 | 48 | 80 |

`compact-80` is an opt-in experiment to measure the compute cost of a smaller
input. It retains the pinned model, one CPU thread, the exact first four parents,
original bodies and supporting spans, default-off behavior, and every existing
quality and resource ceiling. Its shorter excerpt may omit useful evidence;
neither lower latency nor a quality gain is established. It requires separate
native qualification under the same gates before any release recommendation.

Each pair includes three special tokens. Compact profiles select one contiguous
verbatim excerpt within the original selected passage, or the accepted original
body when no passage is selected. The deterministic rule maximizes distinct
query-token coverage, then total query-token matches, then balances context around
matches; ties retain the earliest window and no matches select a prefix. Recorded
input offsets, original and kept token counts, removed characters and the profile
must match the source. Supporting spans, parent identities and base scores remain
intact. Each profile has a distinct protocol and independently passes the same
resource and quality gates before qualification. Selecting a profile never
enables reranking.

When runtime passages and C4 are requested together, the `standard` Actions
profile set evaluates the three existing profiles in separate jobs with distinct
artifact names. Select `rerank_profile_set: compact-80` to evaluate the smaller
experimental profile across all five datasets with the same gates. The production
workflow exposes the same manual input. On its authorized automatic CI branch,
the commit marker `[compact-80]` selects this experiment without skipping full
qualification, storage measurements, or the test-speed observation. Unsupported
profile sets fail before model preparation. Profile selection does not enable
C4 or runtime passages; their separate switches still apply.

Compact native qualification remains incomplete; passing mocked contracts does
not establish quality or resource qualification. The legacy experiment retains
its original limits and its failures remain separate from actual runtime results.

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

Failed, aborted and unavailable C4 workers retain their fixed failure code,
exception type and at most four source filenames with line numbers in the
sanitized job summary. Diagnostics identify a failed `baseline` or `c4` measured
phase and preserve finite numeric abort evidence and one child failure when
available. Exception text, absolute paths and unknown fields are omitted. A
`null` resource observation remains unavailable; it does not establish an overrun.

Complete output manifests retain the base embedding revision, model hashes,
runtime hashes and base protocol hash for the existing scorer. They record the CE
fixture and protocol separately, and `paired_original_condition` is
`no_reranker_passages`. `original_sdk_top10` is that immutable passage baseline.
All exported JSONL filenames fit the reviewed wrapper's existing allowlist.

The separate scorer may open labels only after candidate exports are fixed.
For completed native C4 exports, the score command also writes
`reranker-diagnostics.json` and binds its SHA-256 in `score-summary.json`.
The supervisor verifies that digest, the labels/scorer/source/model/protocol
provenance and opaque query references before adding a strict content-free
projection to `conditions.runtime-c4.reranker_diagnostics` in the existing Actions
artifact. It records original/final primary-gold ranks, primary versus alternate
faithful-source gold membership in the fixed prefix, finite logits, numeric token
accounting and supporting-span geometry. Query text, parent IDs, bodies and span
text are excluded. Answer-span containment remains explicitly unavailable because
document qrels do not annotate answer ranges. The diagnostic adds no inference,
changes no ranks or gates, and remains absent for non-native-C4 scores and older
scores without a diagnostic reference. Ranked exports and their manifests remain
immutable; repeated scoring of the same inputs produces the same diagnostic.

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

The [legacy contract checks](../tests/test_retrieval_c4_contracts.py),
[runtime checks](../tests/test_runtime_reranker_ci.py) and
[runtime input checks](../tests/test_local_reranker.py) use mocks and synthetic
data. The combined compact-profile check covered 122 selected tests with one
worker on Python 3.10. No native retrieval, model inference, model download,
daemon, provider call or full test suite was run locally for this candidate.
Native results and failures remain attached to their tested source in Actions;
the compact profiles require their own native qualification.
