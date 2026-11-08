# Retrieval verification

These standalone runners require prepared, hash-identified data and existing
pinned local MiniLM model files. They initialize isolated Mesh and agent homes
before importing a copied runtime. They disable daemons, downloads, update
checks and implicit cloud calls, remove provider credentials, and isolate
Codex, Claude and XDG configuration before importing Mesh. They close their owned SQLite and audit pools before
temporary cleanup. No answer generator is invoked.

The tested version and module hashes, model revision, data fingerprints, gold
survival, exact query accounting and aggregate metrics are recorded in
[production_retrieval_20261106.json](results/production_retrieval_20261106.json).
Performance claims belong in [RESULTS.md](RESULTS.md), alongside their tested
version. Raw corpora, private run manifests and model weights are local inputs;
they are not included in the published aggregate.

## Keep the evidence conditions distinct

The original public-dataset quality runs used frozen schema 4, FULL SQLite
durability and a separately declared 512 MiB cache. Their same-model dense
comparison is an ablation of that checkpoint. It is not a historical or
competitor baseline.

The historical schema 5 replay used `mesh/store.py` SHA-256
`0d2c6bdbaa8b8f8e4bd4f07bf7d4d184b6b3f98a22a85dc16d977e877485e620`,
the unchanged production 256 MiB cache, and
saved neural query vectors from those complete guarded runs. It compares every
ordered memory ID in raw hybrid candidates, final SDK results, SDK reranker
inputs and dense candidates at both C64 and C96. This verifies ranking and
storage continuity; it does not measure fresh query inference or quiet latency.

The historical fresh SciFact query-only run at that source backs up the existing full guarded corpus into
a new isolated home. It verifies every accepted revision and model-bound index,
and each incarnation/store generation, then calls the normal SDK with fresh query text. Query mode, fusion and candidate
cap use their defaults. The requested output is ten public results, with default
access recording enabled. It supplies no precomputed query vectors and forbids
document inference. Its results are a separate condition because access recording
can change subsequent usage scores.

The direct-fact, early/middle/late long-text and six-case LongMemEval fixtures are
bounded regressions. The LongMemEval subset contains exact annotated gold turns
and inspected original distractors; it does not represent the full benchmark.
Answerless cases measure candidate emission, not generated wrong answers.

Optional TinyBERT cells remain attached to the frozen quality checkpoint. The
MiniLM cross-encoder was stopped after a gross resource gate. The optional E5
experiment was explicitly settled during partial indexing, with no public
quality score. None of these trials changes the default embedding model or adds
a default reranker.

Cache charge describes retained objects under Mesh's conservative accounting.
It is not whole-process memory. Supervised development RSS and wall time are
diagnostics, with a separate resource ceiling; they are not quiet p95 or SLO
results.

## Run the checks with explicit local inputs

Use Python with the repository dependencies and patched SQLite runtime. All
paths below are placeholders for local artifacts. The scripts do not fetch data
or models. Pass `--dependency-directory` only when a verified local dependency
directory is needed.

```text
python benchmarks/production_retrieval.py regression prospective --repository . --dataset-directory PREPARED_FIXTURE --protocol FROZEN_PROTOCOL.json --model-directory EXISTING_MODEL_DIRECTORY --output LOCAL_RUN
python benchmarks/production_retrieval.py score LOCAL_RUN --dataset-directory PREPARED_FIXTURE

python benchmarks/production_retrieval.py query-only scifact --repository . --dataset-directory PREPARED_SCIFACT --protocol FROZEN_PROTOCOL.json --model-directory EXISTING_MODEL_DIRECTORY --source-run GUARDED_SOURCE_RUN --source-namespace GUARDED_NAMESPACE --source-home NAMED_TEMP_MESH_HOME --output FRESH_QUERY_RUN
python benchmarks/production_retrieval.py score FRESH_QUERY_RUN --dataset-directory PREPARED_SCIFACT

python benchmarks/replay_retrieval.py GUARDED_SOURCE_RUN --repository . --source-home NAMED_TEMP_MESH_HOME --output REPLAY_RUN
```

The replay source must be complete and untruncated, with hash-verified fidelity,
query-vector and C64/C96 rank exports. Its database must reside in an explicitly
named `mesh-frozen-quality-*` directory under the system temporary directory,
with a `mesh-home` child. This guard prevents accidental access to a real Mesh
home. A sanitized source manifest requires `--source-home` to provide that
verified local location.

`regression` accepts `prospective`, `longtext-prospective` and the inspected
`longmemeval` fixture, using the predeclared C64/C96 conditions. It also accepts
prepared public datasets for explicitly authorized new evaluations. A run refuses
to overwrite an existing output directory. Temporary corpora are cleaned by
default; `--retain-guarded-home` explicitly retains the isolated checkpoint for
later local replay.

A prepared dataset directory contains `manifest.json`,
`queries-selected.jsonl`, `labels.json` and `corpus.jsonl`, or one corpus file
per inspected LongMemEval question. The manifest lists each relative input file
and its SHA-256, plus `corpus_count` and `query_count`. Relative names cannot
escape the dataset or source export root, and all ancestors are checked for
symlinks and Windows reparse points before canonicalization. Queries contain `id` and
`query`; per-case queries also name their `corpus` file. Corpus rows contain `id`
and `content`, or BEIR `_id`, `title` and `text`. The scorer reads `qrels` from
`labels.json` only after completed retrieval and verifies all query IDs exactly
once. It preserves original gold denominators and reports gold-survived cohorts
separately. Deduplicated memories receive the first faithful source document by
ingestion order; labels never choose that association.

The protocol supplies the pinned model name, revision, ONNX and tokenizer
SHA-256 values, `fusion` and `candidate_caps: [64, 96]`. Local model files are
`model.onnx` and `tokenizer.json`. The query-only command observes the normal SDK
model tag without replacing its encoding or injecting a vector. The offline
scorer reports query-bootstrap intervals and paired differences only when a
dense comparison was actually exported.

## CI quality runs

[The retrieval quality workflow](../.github/workflows/retrieval-quality.yml)
is reusable through `workflow_call`, with optional `qualification_run_id`
defaulting to the caller's current run. A caller must require its primary and
minimum qualification jobs first. The callee downloads
`production-qualification-primary` and `production-qualification-floor`, checks
complete passing suites including the primary crash-recovery case, coverage of
at least 60%, the passing installed-wheel report, exact current runtime and
project hashes, and primary Python/package versions. It installs with the
qualified `constraints.txt`. Windows with Python 3.14.0 matches qualification's
raw source bytes and native package versions. Portable helper behavior does not
claim an unrun Linux or macOS quality result.

The reusable job allows quality checks on a reviewed branch before changing the
default branch. Standalone manual dispatch requires the successful same-source
qualification run ID. The workflow has read-only contents/actions permissions,
isolated Mesh/HOME/USERPROFILE/APPDATA/LOCALAPPDATA/XDG/Codex/Claude roots, and its
own noncanceling concurrency group. Calling-workflow environment variables are
not required for privacy or isolation.

[prepare_retrieval_ci.py](prepare_retrieval_ci.py) verifies the existing fixed
local embedding pair supplied by `benchmarks/provision_production_model.py`;
it never downloads model weights itself. The shared provisioner only writes
within repository `.work`. Public NFCorpus and SciFact archives have fixed
hashes and bounded static downloads. The already inspected direct, positional
and six-case LongMemEval corpus/label bytes are preserved in
[retrieval_fixtures](retrieval_fixtures/README.md); their new ranking manifests
omit exact gold IDs. Static and model paths must remain in repository `.work`
or explicitly named runner temporary roots. No cloud LLM inference occurs.

Each of the five datasets gets a complete same-current-runtime guarded C64/C96
baseline, exact saved-vector replay, separate fresh default SDK queries, and the
experimental paragraph/sentence passage condition. Replay gates exact ordered
continuity across all four fields at both caps. Fresh queries use a
`results-default` cell, preserving default candidate-cap behavior and access
recording; their ordered ranks need not equal the controlled baseline. Identity,
model/protocol hashes, public fidelity, canonical first-source associations and
complete query/ingestion accounting are checked independently of quality. The
passage condition uses saved vectors, unchanged short representations and
distinct parent caps of 64 and 96, with final privacy and full-identity fences.
No production schema or default is selected from these test results.

[retrieval_ci.py](retrieval_ci.py) supervises each subprocess and its descendants
under a whole-run 4,200-second wall cap, sampled 4,096-MiB process-tree RSS cap,
and accumulated 3,600 sampled CPU seconds. It retains exited descendants' CPU
observations and stops owned processes on bounds, failure or interruption.
RSS and CPU are sampled every 0.2 seconds; transient peaks and short-lived
descendants between samples can be missed. These are resource rejection gates
and diagnostics, not quiet latency, power-cut or SLO evidence. Retained native
homes are inside a single owned temporary container that is cleaned afterwards.

The preregistered exploratory release gates require answered direct hit@1 of
at least 0.95, positional and exact-turn LongMemEval hit@1 of at least 5/6, and
paired passage nDCG@10 and document-recall@10 degradation of at most 0.01 on
each BEIR cap. Fresh default and passage scores are distinct; perfect session
hits cannot replace exact-turn LongMemEval hits. The tiny inspected fixtures
cannot establish broad accuracy, competitor or statistical superiority.
Scorer completion is insufficient: unmet thresholds fail the job and require
further work. Historical direct 13/16, positional 0/6 and LongMemEval 3/6
top-one results in the preserved source-identified evidence are not new-source
or successful experimental results.

## Production passage-index experiment

The separate `run_runtime_passages: true` / `--runtime-passages` condition tests
the implemented schema-6 passage index through fresh `Mesh.recall` queries.
The production qualification workflow requests it on reviewed
`ci/mesh-production-run-*` branches; standalone retrieval dispatch keeps it
optional. It copies the guarded database, enables `MESH_PASSAGE_INDEX=1` only
inside that isolated experiment, and drains bounded durable indexing batches.
Readiness must match each original parent's revision, incarnation, store
generation, local model and `paragraph-sentences-v1` representation. Source
database/WAL fingerprints must remain unchanged.

The `runtime-passages` condition preserves the hash-verified fresh-default
ranks alongside its own original-parent ranks. Content-free original offsets,
parent identities, loaded-query inference counts, and indexing/query RSS, CPU
and wall observations are verified under the existing whole-run caps. It
reports paired hit@1/@5/@10, MRR, nDCG and document recall, plus lost/gained
exact top-one hits. Bounded fixtures require the registered hit@1 threshold
and zero new exact top-one regressions; BEIR paired nDCG and document recall
may degrade by at most 0.01. Missing readiness, provenance or measurements
fails the experiment. The fresh-default, legacy child-passage and production
passage conditions remain distinct; their scores do not select a runtime
default automatically.

Source-accounted public parents with no faithful original document aliases
retain their original rank and receive zero relevance credit. Private,
quarantined, unknown or identity-mismatched parents remain rejected. Legacy
child manifests may inherit missing model-file hashes only from their
independently verified, matching guarded source.

When requested, the production runtime condition runs immediately after the
fresh-default pair and before legacy child-passage experiments. Its scored
metrics and gates survive a later legacy or C4 failure; the final combined
status still applies every requested gate and the same whole-run resource
limits. Worker failures retain only a fixed stage, exception type and source
line numbers, so diagnostics expose no memory bodies or exception arguments.

## Optional C4 experiment

The optional `run_c4: true` / `--reranker-experiment c4` hook is separate and off
by default. The reviewed [C4 runner](retrieval_c4.py),
[provisioner](provision_retrieval_c4.py), and passage query hooks are implemented
as CI utilities. It
compares only the first four distinct authorized parent passages against a
matching no-reranker passage run, with fixed 768-MiB total RSS, 128-MiB added
RSS, 50-ms added p95, 100-ms added p99 and 50-ms added CPU/query rejection
budgets. Every C64/C96 paired hit, MRR and nDCG mean must have the complete
answered-query denominator and zero loss; bounded fixtures also require zero
new exact-evidence top-one regressions. The scorer records lost and gained hits
separately so gains cannot hide a regression. Missing measurements fail
qualification. [RETRIEVAL_C4.md](RETRIEVAL_C4.md) describes the source, privacy,
resource and paired-quality checks.

Remote C4 qualification remains pending. The earlier C64 cross-encoder resource
elimination remains separate historical evidence. The normal Mesh runtime
continues to load its configured embedding model without a cross-encoder.

## Artifact output

The workflow restores and saves only the two hash-pinned embedding files and,
when C4 is explicitly requested, its two separately pinned model files. Verified
files are saved before retrieval so a later accuracy or resource rejection does
not prevent cache warming. Preparation/cache failures leave the required runner
phases in place; those phases still independently verify exact sizes, hashes and
source allowlists. Initial static downloads can therefore happen during workflow
setup, while the registered provision phase measures its cached verification.
Query inference, complete original-corpus accounting, scoring and resource limits
remain unchanged. Databases, datasets, ranks and agent configuration are never
cached. No retrieval speedup is inferred from the asset cache.

Result export builds faithful source aliases only for returned parents, keeping
their original alias order and all privacy/identity checks. It avoids allocating
aliases for the entire corpus on every query; complete ingestion-fidelity
validation still covers every original document. This reduces benchmark overhead
and establishes no measured improvement to runtime search latency.

The workflow creates `job-summary.json` before dependency setup and always
finalizes/uploads that sanitized success or failure artifact. Each completed
base condition's aggregate metrics are saved before optional later work. It
includes source/model/export hashes, exact accounting, limits, sampled phase
resources, explicit quality differences and gate failures. It uploads no raw
query text, memory bodies, databases, model weights, source snapshots or logs.
`production_retrieval.py report LOCAL_RUN --output AGGREGATE.json` remains
available for separately scored local exports. Full tests, quality runs and
resource experiments belong in GitHub Actions; local verification stays limited
to small isolated stdlib/mocked contracts and syntax checks.
