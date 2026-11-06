# Production process benchmark

[production_bench.py](production_bench.py) measures writes from one, four, eight,
and sixteen independent local agent processes. Scalar calls and atomic batches
receive the same total input fixture. All workers and the HTTP server use new
temporary Mesh/home/configuration environments, with cloud access, automatic
daemon startup, synchronization, model downloads, and update checks disabled.
The writer must use patched SQLite, WAL, and `synchronous=FULL`; an incompatible
runtime is a reported failure.

The default scenarios compare the reviewed receipt SDK and authenticated HTTP
interfaces. [production_transport.py](production_transport.py) keeps HTTP
clients thin: they use `MeshClient` with an explicit HTTP transport, verify the
configured store's HMAC identity on every call, and never import `Mesh` or load a
model. Before writes, the same challenge also proves the signed receipt-version
capability; missing support prevents submission. Successful mutation replies
must match the operation ID, namespace, receipt and result identity. A real
uvicorn server owns the model, caches, and background document
indexing. It binds an ephemeral loopback socket and never invokes interactive
daemon startup or model installation.

| Surface | Write contract |
| --- | --- |
| `receipt-sdk` | Reviewed `MeshClient`, explicit SDK transport, policy checks and durable operation receipts |
| `http` | The same profile policy, actor labels, stable IDs and fixture through authenticated `/mcp` |
| `bare-sdk` | Optional diagnostic using guarded `Mesh.learn`/`learn_many` directly, without receipts |

Only HTTP scenarios vary `MESH_GROUP_COMMIT=0/1`. Authenticated `/stats` counters
are captured after the seed writes and after measured ingestion. A requested
enabled flag must match actual execution counters. The report distinguishes use
of the group coordinator from observed coalescing of several commands into one
commit. A single active agent may execute every command in a separate group.

## Small local smokes and remote measurements

Use [the production Actions workflow](../.github/workflows/production-performance.yml)
for full suites and performance matrices. It qualifies its own source on the
primary and minimum Python runtimes, verifies crash recovery and coverage, and
checks the installed wheel before measurement. Ordinary `ci/mesh-production-*`
branches run qualification. Explicit `ci/mesh-production-run-*` branches also
request the serial matrix, full-suite test-speed comparison, and the five
[retrieval datasets](RETRIEVAL.md). The narrower `ci/mesh-production-run-c4-*`
prefix additionally requests the [optional local reranker experiment](RETRIEVAL_C4.md).
Manual dispatch offers separate controls; requesting C4 also enables retrieval.
The complete remote qualification flow is described in
[CI_VALIDATION.md](../docs/CI_VALIDATION.md).

The registered matrix has five small resource pilots, 48 primary cells, and four
conditional scale cells. Each primary cell has 512 measured facts plus twelve
anchors, one/four/eight/sixteen agents, hash/neural modes, scalar/atomic calls,
and receipt SDK/HTTP interfaces. Each cell runs alone and uploads a numerical
record before the next starts. Scale cells use 10,000 facts and require resource,
readiness, and time-budget gates. Executed failures fail the final check; skipped
resource or budget cells retain explicit reasons. These are registered workloads,
with no throughput claim until their source-bound results are published.

For a small local check, use one agent and one hash-backed input fact:

```bash
python benchmarks/production_bench.py --agents 1 --memories 1 --surfaces http --group-commit 0 --modes scalar --backends hash --query-repetitions 1 --timeout 30 --skip-crash-probes --output .work/http-smoke.json
```

This also seeds the twelve functional anchors. It confirms the transport and
receipt path; it does not qualify concurrency, scale, neural quality, or crashes.

Run the default offline fixture from the repository root:

```bash
python benchmarks/production_bench.py --agents 1 4 8 16 --memories 512 --surfaces receipt-sdk http --group-commit 0 1 --modes scalar atomic --backends hash --output production-bench.json
```

Run the separate direct SDK diagnostic with `--surfaces bare-sdk`. Its call
boundary differs from the default receipt interfaces and must remain labelled
separately in any comparison.

Neural runs require an explicit dedicated fixture directory containing
allowlisted `model.onnx` and `tokenizer.json` artifacts. The harness copies and
hashes the same bytes in one pass, verifies the repository allowlists, and uses
the copies inside its temporary data root. It never downloads a model. Use a
fixture directory rather than a production Mesh data directory:

```bash
python benchmarks/production_bench.py --agents 1 4 8 16 --memories 512 --surfaces receipt-sdk http --backends hash neural --model-dir ./fixture-model --output production-bench.json
```

Without explicit model artifacts, requested neural scenarios are reported as
unavailable. Hash hybrid retrieval is reported separately from verified neural
hybrid retrieval. `--repo-root` selects the source checkout under test; all data
homes still remain temporary. Runtime and harness source hashes are checked
before and after worker execution so edits during a scenario invalidate it. Run
performance comparisons while source edits, heavy tests, and other benchmarks
are stopped. Report version 2 includes the interface and group flag for each
scenario; fixture version 1 is unchanged.

## Measurement boundaries

The twelve handwritten anchor facts and their keyword/paraphrase queries are a
functional retrieval fixture. Synthetic write records provide varied topic
distractors. This fixture does not establish held-out competitor performance or
general natural-language recall quality.

The report keeps these measurements separate:

| Measurement | Included work |
| --- | --- |
| Write call p50/p95/p99 | Selected scalar or atomic interface, including fixture argument construction, encoding, guards, durable storage and receipt work; HTTP also includes per-call identity verification and transport; a batch is one latency sample |
| Durable acceptance wall time | Released startup barrier through the last worker finishing ingestion, including progress recording and excluding worker close |
| Worker lifecycle wall time | Released startup barrier through the last worker exit, including close |
| Exact lookup | Public guarded `get` for bare SDK, or `mesh_inspect(memory_id=...)` for receipt SDK/HTTP; ID, content, revision and visibility must match |
| Lexical index readiness | Independent native FTS visibility for the twelve public anchors |
| Fully indexed throughput | Barrier through fresh-process confirmation of expected revisions in the verified neural model space, including worker close, verification startup, exact/native FTS checks and waiting; the HTTP observer loads no model |
| Retrieval latency and Recall@1/@5 | Selected public hybrid interface, including query encoding, default telemetry and HTTP identity/transport where applicable; first and repeated passes are reported separately |
| Receipt confirmation | Public `mesh_operation_get` and native receipt scope, operation, IDs, revisions and disposition checks |
| Resource usage | Sampled coordinator/descendant summed RSS and CPU by scenario phase and process role, including the HTTP server; reported writer process and ingestion CPU remain separate |
| Footprint | Database, WAL, model, and other file bytes after workers and the HTTP server close |

Summed RSS can count shared pages more than once. Sampling can miss short-lived
processes and is optional when `psutil` is unavailable. CPU attribution near a
phase boundary is approximate. Repeated query passes can use production caches;
the persistent HTTP server may already have warm document/vector caches from
ingestion. Startup includes model copying, server startup where applicable, seed
writes, process imports, runtime verification, and the readiness barrier.

Receipt SDK and HTTP query encoding follows the fixed runtime profile. Hash
scenarios report hash hybrid keyword retrieval. Neural scenarios report keyword
and paraphrase retrieval with neural query encoding after verified readiness.
Changing a client's environment cannot change the server's query mode, so neural
HTTP scenarios explicitly mark hash-encoded hybrid retrieval unavailable. The
optional bare SDK diagnostic can report its separate hash keyword pass.

## Accounting and failure handling

The fixture disables deduplication, while the accounting still distinguishes
attempted rows, confirmed acknowledgements, inserted IDs, deduplicated
acknowledgements, failed replies, and unknown outcomes. A native database pass
checks IDs, content, revisions, visibility, row counts, foreign keys, and
`quick_check`. Committed rows without an acknowledgement are reported explicitly.
A response containing a memory ID but no valid receipt remains an unknown write
outcome. The report retains its stable operation ID without counting it as an
acknowledgement or automatically replaying the mutation.

Each call writes a flushed attempt marker and outcome marker without an
additional per-call journal `fsync`. Receipt interfaces record the deterministic
operation ID before transmission and preserve it in both confirmed and unresolved
outcomes. A final completion marker records the ingestion endpoint. This overhead
is included in acceptance wall time; per-call latency excludes journal publication.
A killed worker's known confirmed writes survive result collection. Its last
unresolved attempts remain unknown and are never automatically replayed. An
incomplete journal cannot turn unobserved inputs into a claimed count of
unattempted inputs. Confirmed pre-send/authentication failures remain distinct
from exceptions that may follow a commit.

Failed workers, malformed replies, incomplete batches, accounting discrepancies,
retrieval exceptions, and failed integrity/readiness checks make a scenario fail.
Retrieval misses remain visible in Recall@1/@5 and the per-anchor miss counts.
Latency summaries exclude failed calls. A failed scenario's rates are diagnostic
measurements rather than a successful production throughput result.

## Process crash probes

The default run also launches actual subprocess crash probes. One process exits
inside an open enclosing write transaction. A second exits immediately after its
real `FULL` commit, before its registered post-commit actions. A fresh process
checks the memory, stable operation receipt, durable mutation outbox, database
integrity, and replay of the same operation ID. Recovery may have delivered the
event before observation, so the initial check accepts a pending outbox event or
an already durable audit event. Audit observation opens an existing database
read-only and never creates an absent database. The two database reads are not
one snapshot; their event counts are not required to add to one. After replay
and explicit delivery, exactly one unique durable audit event must remain, the
outbox must be empty, and a second delivery must report zero new events.
These are process-death tests; physical power-loss durability requires separate
hardware/filesystem testing.

All launched workers and servers have bounded waits and are stopped before
cleanup. A validated stop marker requests graceful HTTP shutdown; a stuck or
failed server is killed, joined, and reported as a failed scenario. The JSON
report includes an inventory of each new temporary directory before automatic
cleanup. `--skip-crash-probes` permits an ingestion-only measurement; it should
not be used as a replacement for the recovery checks.

Focused verification uses the repository's isolated launcher:

```bash
python scripts/run_tests.py --workers 1 -- -q tests/test_production_benchmark.py tests/test_production_http_benchmark.py tests/test_production_crash_recovery.py
```

Keep the normal full-suite coverage gate and property-test settings unchanged.
Publish measured performance claims only after the required project checks and
the relevant benchmark runs pass.
