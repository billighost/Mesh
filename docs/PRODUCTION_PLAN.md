# Mesh production performance, retrieval, and agent reliability plan

Prepared 2026-11-06 after two source-review passes by the primary agent and three specialist agents.
Inspected branch: feat/ci-evidence, HEAD 61ec16de97a5604a19a81c6396f8bfc46d982c42, with the maintainer's existing 0.9.0 version changes.

## Implementation checkpoint

The approved safety, storage, query, bounded-execution, transport, brief, and
packaged-agent instruction changes are implemented in the working tree. Patched
APSW supplies one SQLite driver, WAL uses FULL synchronization, and schema 5
binds durable indexing jobs and operation receipts to memory revisions,
incarnations, and store generations. Namespace recovery, public encryption,
privacy, and uncertain mutation outcomes have focused regression coverage.

Final full-suite and supported-platform qualification remains pending. Earlier
full-suite failures are preserved as failures; focused passing checks and an
installed-wheel smoke do not replace final CI. The
[production Actions workflow](../.github/workflows/production-performance.yml)
records full qualification and runs its measurements against that same source.
The maintainer authorized publication of `ci/mesh-production-run-c4-20261106`.
The first remote run passed lint, security, packaging, and wheel validation;
full-suite failures and workflow validation errors are being repaired before
qualification and measurement continue on the corrected source.

The reviewed retrieval and optional four-candidate reranker harnesses are now
integrated as CI utilities. The production workflow calls the five-dataset
retrieval workflow after primary and minimum qualification; source hashes,
dependency constraints, exact evidence IDs, and failure artifacts bind each
result to the source tested. [CI_VALIDATION.md](CI_VALIDATION.md) describes the
remote checks and remaining evidence requirements. These harnesses have passed
small local contract checks; their native model and full-corpus runs remain
pending, and the production retrieval default has no experimental reranker.

The maintainer's current machine has 8 GiB RAM and four logical CPUs. Development
verification now stays limited to small, isolated, one-worker smokes; full tests,
model/corpus evaluations, and concurrent performance measurements run remotely.
The first measurement pass registers 512-fact cells plus resource-gated 10,000-fact
HTTP cells. The larger mixed-workload targets below remain later qualification
work, with no inferred speedup from this initial coverage pass.

Long-memory passage retrieval remains a CI experiment. The current evidence shows
both representation weakness in long memories and short-query errors involving
qualifiers or answerless references. General paragraph/sentence boundaries and
maximum joint passage scoring are being evaluated against original parent IDs.
A separate optional four-candidate local reranker must meet the declared accuracy,
CPU, RAM, and latency gates. Neither experiment is a validated production default.
The historical field-test archive remains unchanged.

## Roadmap (not implemented)

The maintainer approved implementation on 2026-11-06 and requested autonomous execution, multiple review/test cycles, faster testing, and minimal RAM/CPU while prioritizing performance and accuracy. Bundled patched SQLite was also approved. Findings below describe the pre-implementation working tree; proposed capabilities and targets are not achieved results until validated.

### 1. Scope and decisions already approved

The objective is to make Mesh substantially faster and more reliable for many agents sharing one machine, while improving whether the first search result actually contains the needed evidence.

The maintainer has approved:

1. Optimize many local agents first. A required distributed database, remote service, and cloud dependency are outside this release's primary design.
2. Acknowledged persistent writes must survive process crashes and power loss under the documented storage durability contract.
3. Evaluate an optional local reranking model, with explicit CPU, RAM, download, and latency budgets.
4. First complete research and this approval plan; implement after approval.

The work covers learn, recall, update, forget, inspect, context, project briefs, tasks, changelog, namespace operations, audit delivery, import/export, startup, and supported SDK/CLI/MCP/HTTP paths. It includes the skill, rules, examples, installation, and agent workflow.

Keep Python's public interface and SQLite initially. Correctness, query configuration, batching, lock scope, and indexing are the first changes because they address demonstrated problems. Evaluate a selective Rust extension, different embedding model, ANN index, or alternate SQLite driver only where measured benefits justify the added installation and maintenance cost.

### 2. What the two reviews found

Confirmed means source inspection plus an isolated executable reproduction where practical. Ranking and scale hypotheses need evaluation rather than being treated as guaranteed root causes.

| ID | Finding and evidence | Consequence / priority |
|---|---|---|
| D1 | [store.py](../mesh/store.py), line 2053, defaults to synchronous=OFF; its strict setting selects NORMAL. [audit.py](../mesh/audit.py), line 637, has corresponding durability concerns. | Current settings do not meet the approved power-loss contract. P0. |
| D2 | The tested interpreter links SQLite 3.50.4. Current [SQLite WAL documentation](https://www.sqlite.org/wal.html) describes a checkpoint/write corruption race fixed in 3.51.3+ and specific backports. | FULL synchronization does not fix this engine race. Require a verified patched runtime before production concurrent WAL use. P0. |
| D3 | [store.py](../mesh/store.py), lines 5304, 5378, and 5506, changes tasks and appends events without an explicit enclosing transaction on an autocommit connection. A failing event insert left a committed task after creation raised. | Task state and history can disagree. Make each logical change atomic. P0. |
| D4 | [memory.py](../mesh/memory.py), lines 1435 and 2524, compares content before a separate vector update. An edit inserted between those operations left new content with an old vector marked as ONNX. | Embeddings can describe the wrong content. Use transaction-time conditional revision updates. P0. |
| D5 | [store.py](../mesh/store.py), line 5727, omits the private filter from recently completed tasks in briefs. Both primary and specialist probes returned private task titles and summaries in a public brief. | Cross-surface privacy leak. P0. |
| D6 | [store.py](../mesh/store.py), lines 5323, 5422, and 5516, writes normal task text and event notes without application-encryption helpers; import has different behavior. | Encryption coverage differs by write path. Fix and migrate before expanding throughput. P0. |
| D7 | [mcp_server.py](../mesh/mcp_server.py), line 2152, and [memory.py](../mesh/memory.py), line 3437, allow task mutation responses to expose private fields under restrictive settings. | Enforce task visibility and enabled ownership policy for writes and responses. P0. |
| D8 | [cli.py](../mesh/cli.py), line 314, retries lost responses for mutations. [mcp_server.py](../mesh/mcp_server.py), line 491, can classify a partially sent mutation as eligible for local fallback. Both were reproduced with fake connections. | Duplicate effects or incorrect success after uncertain outcomes. Stop unsafe replay, then add durable operation receipts. P0. |
| D9 | [installer.py](../mesh/installer.py), lines 933 and 1023, binds the MCP project store but not its startup hook. Namespace text is interpolated at line 1027 without adequate validation. | Wrong-project context and unsafe shell construction. Fix with isolated installation tests. P0. |
| R1 | [memory.py](../mesh/memory.py), lines 1913 and 1941, uses the fast hash path for queries as well as ingestion. A ready model therefore does not ensure neural queries. The benchmark forces a different setting at [run_pilot.py](../benchmarks/run_pilot.py), line 576. | Default natural-language retrieval differs materially from benchmark neural retrieval. P1. |
| R2 | [memory.py](../mesh/memory.py), line 1971, caches normalized queries by text without backend identity. Primary and specialist probes retained hash queries after switching to neural mode. Lines 1851-1883 also mix cached neural vectors and fresh hash vectors under one batch tag. | Cache transitions and batched model provenance are incorrect. P1, before new batching. |
| R3 | [store.py](../mesh/store.py), line 3629, defines an FTS/BM25 scorer with no current caller. The active path substitutes token-hit scores and strong lexical floors at line 4076. | Paying FTS maintenance costs does not mean active retrieval uses BM25. A controlled answerless distractor outranked a better semantic answer. P1. |
| R4 | [store.py](../mesh/store.py), line 3994, prunes using raw lexical and semantic signals before final ranking. [memory.py](../mesh/memory.py), line 2932, ranks again using confidence and usage. | Candidate loss and relevance distortion are plausible; popularity feedback needs an ablation. P1 research. |
| R5 | [memory.py](../mesh/memory.py), lines 610 and 699, caps long-text windows and averages them into one vector. [store.py](../mesh/store.py), lines 129-134, has narrow tokenization and removes meaningful temporal/negation terms. | Evidence late in a memory or expressed indirectly can be missed. P2 representation work. |
| S1 | [store.py](../mesh/store.py), lines 2021, 2678, and 3865, pools one serialized connection and holds a process-global cache lock across expensive work. Learn dedup scans vectors inside the namespace write lock at [memory.py](../mesh/memory.py), line 2646. | Readers and independent namespaces obstruct each other; write work grows with namespace size. P1/P2. |
| S2 | [memory.py](../mesh/memory.py), lines 164 and 1412, schedules embedding work in an evicting in-memory queue, with idle-gated progress and per-row commits. [audit.py](../mesh/audit.py), lines 434 and 769, has unbounded buffering and failed-flush loss. | A fast acknowledgement can hide growing or lost work. Persist required jobs/events and bound admission. P1. |
| S3 | [mcp_server.py](../mesh/mcp_server.py), line 2313, and [http_server.py](../mesh/http_server.py), line 964, run synchronous work in async handlers. | Slow storage/model work blocks other requests. Use bounded offloading and explicit cancellation. P1. |
| A1 | [store.py](../mesh/store.py), lines 5770 and 5806, selects five startup facts and renders four, after tasks. Handoffs are excluded; IDs are not shown; truncation can exceed the budget. | Larger budgets do not improve coverage, and agents can miss existing facts. P1. |
| A2 | [SKILL.md](../mesh/skills/mesh-context-layer/SKILL.md) and troubleshooting stop at MCP failure. CLI/SDK use different privacy defaults and identity; runtime namespace resolution does not implement the skill's complete convention. | Missing MCP registration was incorrectly treated as complete unavailability in this session. Naive fallback can also weaken policy or select another store. P1. |
| E1 | [benchmark_all_surfaces.py](../benchmark_all_surfaces.py), line 839, calls the MCP handler directly. [concurrency_stress.py](../benchmarks/concurrency_stress.py), line 158, does not collect all future exceptions. Other profilers use obsolete result keys/statuses or sampled retrieval while describing broader coverage. | Establish correct accounting and real transport measurements before making speed claims. P0 measurement. |

The current public comparator archive indicates that the strongest comparator leads on turn-level first-result retrieval; top-five parity and session-level success are different conclusions. Do not describe the existing results as domination of all competitors.

Published numerical context permitted by repository policy: [RESULTS.md](../benchmarks/RESULTS.md) reports Mesh 0.8.3 warm SDK learn p50 of 9.63 ms, and a different scale workload of approximately 15-19 memories/second. These are historical workloads, not a current FULL-durability baseline, and the reciprocals of median latencies are not aggregate throughput.

The accuracy archive currently lives in [BENCHMARKS.md](BENCHMARKS.md) and separate raw artifacts. Before numerical claims are republished, consolidate provenance through the approved evidence document without rewriting the independent field-test archive.

### 3. Proposed architecture and contracts

#### Local storage and runtime

- Retain a SQLite database per namespace as the authoritative store, with short explicit transactions and WAL FULL synchronization for acknowledged persistent effects.
- Verify the actually linked SQLite library, FTS5, thread-safety settings, and sync behavior. Accept fixed 3.51.3+ or verified patched backport branches; the version check must not accidentally admit vulnerable intervening releases.
- Require that verified runtime across every writer and maintenance path, including audit, crypto, migration, backup, CLI inspection, and checkpointing. If a supported platform cannot obtain it through a supported interpreter, bring coherent bundled-driver packaging forward rather than leaving that platform unsafe.
- Support a documented patched Python/SQLite distribution first. A bundled alternative driver is a packaging experiment, not a partial replacement of store.py: store, audit, crypto, backup, migration, and diagnostics must use a coherent driver.
- Separate bounded writer coordination from bounded reader connections. Use per-store state locks and short global registry locks.
- Read rows, content revisions, lexical metadata, and model eligibility from one short database snapshot. Publish an immutable namespace generation and release locks/read transactions before matrix multiplication or reranking.
- Treat vectors, FTS, chunks, and caches as rebuildable derived data. Content, visibility, task transitions, receipts, and required mutation history remain authoritative.

#### What a successful write means

- A durable acknowledgement follows the transaction commit, including required content/metadata, eligible lexical indexing, content revision, operation receipt, embedding job, and mutation-event outbox.
- Expose stored revision and indexing state. Distinguish installed model, ready model, effective query backend, and namespace index readiness.
- Exact and lexical lookup must find acknowledged content even while a vector is pending. The current early return when a vector matrix is absent must be removed from independent lexical/exact retrieval.
- Offer an explicit wait-for-indexed option for callers requiring semantic read-after-write. Do not call durable acceptance fully indexed ingestion.
- Update a vector only when its expected content revision and target model space still match. A stale job is superseded, not published.
- Audit mutation history uses content-free durable events. Read-access statistics can use a separately documented loss-tolerant buffer.

#### Bulk operations

- Add a guarded high-level learn_many API, using the same validation, redaction, quarantine, ownership, privacy, and encryption rules as scalar operations.
- Do not expose the existing raw upsert_batch path as the public bulk API.
- Bound items, total bytes, and token work. Guard/redact before embedding; prepare expensive work outside write transactions, then check ownership, revisions, dedup, and visibility at commit.
- Define all-or-nothing and per-item behavior explicitly. Return ordered per-item IDs, revisions, stored/deduplicated/rejected disposition, and indexing state.
- Exact content identity must not depend on which embedding backend is active. Evaluate encryption-safe equality fingerprints before adding durable dedup keys.
- Process within-batch duplicate/conflicting facts deterministically. Never merge distinct numeric, negated, temporal, subject, or environment values merely because their embeddings are similar.

#### Safe retries and shared local batching

- Persist operation IDs atomically with effects. The unique key is store identity, namespace, approved producer scope, and operation ID; store the operation and canonical parameter digest as values and compare them to reject mismatches.
- Reject key reuse with a different request. Reauthorize replays. Store content-free receipt IDs/revisions/statuses, not copies of memory/task bodies. Shared-token agent_id labels alone do not authenticate distinct principals; the approved connection profile defines producer binding without a multi-tenant claim.
- A timeout or partially sent mutation means unknown outcome. Reconcile the original ID before replay, including across transports.
- Define receipt retention and expired/unknown outcomes; do not promise indefinitely exactly-once effects.
- Make the daemon a bounded request aggregator for short FULL group commits. It coordinates batching rather than silently becoming a different store.
- Start with a deliberately supported direct-writer path using the same transactional preconditions and revision/receipt rules. Recommended clients use the aggregator when configured; if exclusive broker ownership is later adopted, add fencing before claiming a single writer.
- Bound queue items and bytes, maximum group delay, inference workers, and outbox growth. Schedule fairly across producers and namespaces; bulk work must not starve interactive reads or embedding progress.
- Cancellation before admission can remove work. After admission or commit begins, keep and reconcile the outcome even if the caller disconnects.

#### Policy-preserving fallback

The requested order is MCP, then CLI, then SDK, then an already running authenticated HTTP endpoint.

- Use one shared logical operation contract and a verified connection profile carrying store identity, namespace, actor, capabilities, and applicable policy.
- MCP child-process restrictions may be absent from the agent shell. A CLI flag excluding private reads is not a replacement for verified equivalent server policy.
- Try another compatible transport only for absence, connection failure, or unsupported capability, and only when the same authorized profile is available.
- Authentication, scope, ownership, privacy, rejected content, invalid namespace/input, and unsafe-runtime errors are terminal for that operation.
- Mutations use the same operation ID; uncertain effects are reconciled rather than duplicated. Before receipts exist, ambiguous mutation failures stop conservatively.
- Do not launch another server, download models, expose an endpoint, or alter user configuration merely to make fallback work.
- Provide structured JSON outcomes and meaningful exit statuses. Prefer one small client/dispatch implementation over four divergent implementations in the skill.
- Report unavailable only after all compatible permitted alternatives fail, including which capabilities were actually unavailable.

### 4. How retrieval becomes more accurate and faster

#### Correct the effective query path first

Separate ingestion's fast-stage setting from query quality. Use the configured verified neural model when ready, and describe fallback accurately when unavailable. Key embedding/query caches by model revision, tokenizer, preprocessing, normalization, encoding role, and relevant configuration generation.

Do not mix hash/neural models just because dimensions match. Homogeneous batching and compatible model masks are prerequisites for reliable candidate ranking. Apply those masks before dense pruning.

#### Independent candidate generation

Evaluate three bounded channels:

1. Exact identifiers, phrases, subject/attribute facts, numbers, paths, and error strings.
2. Actual FTS5 BM25 for eligible public plaintext content.
3. Dense candidates from the compatible active embedding space.

Private or application-encrypted content must remain out of plaintext persistent FTS. Use a protected ephemeral lexical path or a separately verified encrypted-index design. Chunk text, tags, fingerprints, and derived indexes obey the same privacy rules.

Unauthorized private/quarantined rows must not enter a reranker or influence unauthorized candidate selection through shared lexical statistics. Build eligibility and permitted statistics from a consistent authorized snapshot; document any explicitly permitted aggregate-statistics exposure.

Measure whether gold evidence enters the candidate pool before tuning the final order. Preserve complementary candidates rather than pruning by raw hit counts too early. Compare RRF with calibrated fusion on development data; select the winner by held-out quality and loaded latency, not by algorithm reputation.

#### First-result ranking and answer evidence

- Reduce unconstrained lexical floors and popularity/confidence reranking where ablations show they override relevance.
- Treat confidence, freshness, source, and resolved replacements as explicit policy or bounded ranking signals.
- Retain meaningful negation, temporal words, Unicode, identifiers, and numeric values; add relevant hard negatives.
- Keep ranking scores separate from similarity and min_similarity contracts. RRF scores and cross-encoder logits are not probabilities.
- Evaluate the approved optional local quantized reranker on a bounded candidate pool. Compare always-on, ambiguity-triggered, and no-reranker modes; an ambiguity gate must itself be evaluated.
- Cap inference concurrency and work, provide deadlines and visible mode/fallback metadata, and keep foreground queries from starving indexing.
- Return source memory IDs and supporting spans. Calibrate no-answer behavior separately; do not fabricate an answer from weak evidence.

#### Long memories, models, and larger namespaces

- Evaluate token-aware chunks with parent IDs, offsets, and content revisions; retrieve relevant spans and aggregate distinct parent results.
- Ensure later text is represented, rather than silently dropping it at a window cap. Parent edits/deletions/privacy changes invalidate every derived chunk and cached response.
- Evaluate a retrieval-trained local model against the current MiniLM model with correct query/passage preprocessing, licensing, pinned checksums, RAM, and packaging.
- Use shadow evaluation and revision-bound re-embedding. Maintain a coherent active space, migration progress, restart recovery, and rollback.
- Retain exact vector search as the smaller-namespace baseline. Introduce ANN only if measured size/latency thresholds justify approximation, memory, filtered-search, deletion, and recovery costs.
- Revalidate visibility/revisions when slow ranking races with deletion or privacy changes; define the read consistency boundary explicitly.

### 5. Reliable measurement and release targets

Every target below is provisional and unmeasured. Phase 0 establishes the reference hardware, baseline, and feasible budgets; a target change requires an explicit recorded reason rather than silently changing the workload.

#### Benchmark matrix

- Corpus sizes: 1,000, 10,000, and 100,000 user memories; one million as a conditional capacity experiment after ordinary runs are stable.
- Concurrency: 1, 2, 4, 8, 16, and 32 clients, including separate processes as well as threads.
- Same-namespace contention and separate-namespace activity; short facts and realistic long turns; reads, writes, duplicates, updates, deletes, tasks, and mixed workloads.
- Real SDK calls, cold CLI subprocesses, persistent MCP stdio sessions, and authenticated daemon HTTP. Label direct handlers/in-process ASGI separately.
- Cold and warm process/model/index states; unique and repeated queries; default and neural-ready behavior; encryption/private policy variants.
- Batches across bounded item/token sizes; sustained rates and overload, not only a brief peak burst.
- Fresh isolated subprocess repeats, fixed seeds, hardware/storage/runtime manifests, dataset hashes, effective flags, and raw machine-readable results.

Report:

- Submitted, admitted, unique committed, deduplicated, rejected, uncertain, and failed requests, with every future/process exit inspected.
- Durable user memories/second and fully indexed user memories/second, separately from generated chunks and pending jobs.
- p50/p95/p99 end-to-end latency, lock wait/hold time, event-loop lag, startup cost, CPU, RSS, WAL growth, queue bytes, oldest job age, and recovery time.
- Sustainable throughput requires a stable bounded backlog, acceptable latency, and correct results.
- Operation-specific latency for count/inspect/update/forget/tasks/brief/log/import/export and namespace/cache management, not only learn/recall.
- Whole-dataset ingest fidelity, plus search candidate recall, final hit@1/5/10, evidence coverage, MRR/NDCG, correct abstention, and query-category results.

Use independent development and held-out tests grouped by project/topic/session. Inspected LongMemEval remains a regression archive; it is not an untouched holdout. Do not tune on test answers or expose gold tags to query expansion. Define strict exact-ID evidence metrics separately from fuzzy or session-level matching.

For single-evidence queries, hit@1 requires a sufficient authorized memory. For multi-evidence questions, retain any-gold hit@k for archive comparison and also report required-evidence-group coverage and complete evidence sufficiency@k. Measure false evidence returns and correct abstention separately on no-answer queries. Keep evaluator labels/tags out of ranking inputs as well as query expansion.

Use paired bootstrap intervals for comparisons and declare primary endpoints in advance. Separate controlled same-model comparisons from product-configuration variants using different local models/rerankers. Match interface, evidence unit, resources, and measured contract; disclose unknown competitor durability instead of assuming it. Offline adapters only; no cloud providers or approval bypass.

Freeze the optimization baseline after safety and query/model-cache correctness repairs and before lock, connection, batching, and ranking optimizations. Pin both query and ingest model/revision, encoding mode, readiness, cache state, policy, and result quality for FULL-versus-FULL speed comparisons. Report hash-to-neural correctness gains and their cost separately.

Proposed primary speed cell: 16 agents, 10,000 short facts, 70% reads / 30% scalar writes, five returned results, verified ready local MiniLM, FULL synchronization, public policy, and unique query strings. Compare each real transport within its class and record admission timing to detect coordinated omission. The loaded latency cell uses the same conditions at 100,000 facts, a fixed candidate cap C=64, no result-cache hits, and a preloaded model/index; reranked mode may evaluate at most 24 authorized candidates. Report single-client warm cells, encryption/private variants, and cold start separately.

#### Proposed acceptance goals

| Area | Proposed gate |
|---|---|
| Durable scalar throughput | At least 2x aggregate throughput over the new FULL baseline in the selected 16-agent mixed workload, with latency, privacy, and indexing readiness held constant. |
| Bulk throughput | At least 4x guarded short-fact bulk throughput over equivalent individual FULL writes; investigate higher gains without counting queue accumulation as throughput. |
| Loaded retrieval speed | At least 50% lower p95 latency in the selected high-volume mixed workload, with no quality regression. |
| Warm query budget | Provisional p95 of 100 ms in the identified 100,000-fact loaded latency cell; optional reranked mode has a separate provisional 250 ms budget in that cell. Model/index are warm, query strings are unique, n=5, C=64, reranking cap=24, and public policy is explicit. |
| Direct-fact quality | At least 95% first-result evidence hits on a locked independent direct-fact set, with hard negatives and separate no-answer tests. |
| Conversational quality | Aim for at least a 10 percentage-point first-result gain over the stronger current Mesh baseline, including its neural-ready variant, plus a statistically clear paired gain over the strongest comparable competitor. Pre-register a two percentage-point non-inferiority margin for overall top-five/top-ten: the one-sided 95% paired lower bound must exceed -2 points. Important categories use a preregistered five-point margin with an explicit sample-power check. |
| Candidate generation | Provisional target of at least 98% candidate hits@64 on answerable, authorized queries whose gold survived ingestion, with category analysis. Also report unconditional end-to-end evidence success counting missing/rejected/lost gold, so the conditional measure cannot conceal ingest regressions. No-answer queries have separate denominators. |
| Reranker | Ship only if held-out first-result gains justify loaded latency/RAM/model costs; report failure and timeout behavior. |
| Native extension | Ship only for a demonstrated hotspot with at least 20% meaningful end-to-end improvement, correct fallback, and supported cross-platform wheels. |
| Memory and backlog | Set byte-based bounds before implementation. Indexing keeps up at the advertised sustainable ingress rate; queue age, RSS, and WAL size do not grow indefinitely. |
| Correctness | No acknowledged-effect loss in abrupt-process tests, stale vectors, orphan task/conflict events, silent job loss, unsafe retries, or privacy/ownership/encryption regression. |

Targets are release goals, not a promise that Mesh will beat every tool on every dataset. Report failures and tradeoffs honestly and continue optimizing the measured bottleneck.

### 6. Work packages and order

#### Phase 0 — Baseline and reproducible failures

- Capture the current WIP and keep existing maintainer changes intact.
- Turn confirmed bugs into focused invariant tests after approval.
- Repair benchmark accounting, operation IDs, exception propagation, ingest fidelity, and actual transport coverage.
- Record historical default and neural-ready baselines. Freeze the patched-runtime FULL optimization baseline after Phase 1 safety and Phase 2 query/cache correctness, before Phase 2 performance changes; pin effective encoding and readiness. Never claim FULL-versus-OFF or hash-versus-neural timing as an optimization.
- Define the independent direct-fact/long-conversation development and test sets and the reference workload.

Deliverable: reproducible bug cases, valid measurement harness, manifests, and agreed baseline/targets.

#### Phase 1 — Durable correctness and policy repairs

- Patch/runtime capability gate and actionable diagnostics.
- FULL commit synchronization for required persistent writes; verify platform-specific full-sync behavior, including macOS where applicable.
- Atomic task/history and memory/conflict changes; safe backups and coordinated migrations.
- Conditional vector updates; homogeneous embedding-space batches.
- Minimal durable ID/revision embedding jobs and content-free required mutation-event outboxes, with bounded progress and restart recovery. Later phases optimize these mechanisms.
- Private brief and task-response fixes; application-encrypted task/event paths and migration coverage.
- Conservative uncertain-write handling before automatic retry.
- Installer namespace validation, safe argument construction, and shared data-root binding.

Deliverable: the safety foundation and failing-invariant tests passing.

#### Phase 2 — Correct query behavior and low-risk speed work

- Separate neural query selection from fast ingest and version all relevant caches.
- Preserve immediate exact/lexical visibility for pending memories.
- Freeze the correctness-repaired FULL baseline before the remaining performance changes in this phase.
- Per-namespace immutable snapshots and bounded reader connections; shorten lock and transaction scopes.
- Exact-dedup fast path, fewer redundant SQL/permission/cache operations, and bounded telemetry.
- Bounded async offloading with propagated security context; review process-global stdout redirection before enabling parallel tool execution.

Deliverable: more reliable default natural-language search and measured lower contention without requiring a daemon.

#### Phase 3 — Ranking and evidence quality

- Independent exact, actual BM25, and compatible dense candidates.
- Candidate recall diagnostics, RRF/calibrated fusion ablations, constrained metadata influence, and score compatibility.
- Unicode/identifier/number/negation/temporal handling, hard negatives, and correct abstention.
- Supporting IDs/spans and parent deduplication.
- Eliminate silent semantic coverage loss for accepted long text. Test paraphrased evidence near its end so exact lexical matching cannot mask the defect; validate/report any unavoidable representation limit. The larger persistent chunk-index experiment remains later.

Deliverable: measured first-result quality improvement and transparent evidence behavior.

#### Phase 4 — Durable indexing, bulk ingestion, and shared batching

- Batch and tune the durable revision-bound embedding jobs and content-free mutation-event outboxes introduced in Phase 1.
- Safe operation receipts and status reconciliation.
- Guarded learn_many with explicit failure and privacy semantics across supported surfaces.
- Token-length batching and ONNX thread/concurrency tuning.
- Bounded optional daemon group commits, fair scheduling, overload responses, and cancellation recovery.
- Correct cross-process cache invalidation first; add versioned deltas/tombstones only if measurements justify them.

Deliverable: higher sustainable durable and indexed throughput, with stable backlog and tested retries.

#### Phase 5 — Agent skill, context, and fallback

- Shared profile/client contract and structured outcomes; MCP-to-CLI-to-SDK-to-existing-HTTP capability fallback.
- Approximately twenty selected current facts, a short handoff, and open work within a configurable startup prompt budget; recommend an explicit 2,000-token estimated startup budget and a compact mode. Budget the complete serialized prompt context, notices and formatting; do not duplicate unbounded full bodies beside bounded Markdown.
- Reserve space for facts, include IDs/freshness, expose accurate visible counts and omissions, and support continuation.
- Prefer current diverse project facts, approved decisions, and relevant conventions; exclude stale/replaced/quarantined/private data as policy requires.
- Bootstrap on genuine session/project change or lost context after compaction, not every greeting or ordinary turn.
- Require subject-specific recall before substantive decisions or implementation. If weak, make one focused follow-up and browse relevant facts; absence of a match is not backend unavailability.
- Check recalled evidence against current files and keep memories as untrusted historical data.
- Synchronize packaged skill, references, rules block, README's identical embedded block, installer fallback text, CLI help, and checkers.
- Preserve SKILL.md's 250-line and rules_block.md's 12-line limits. Put detailed transport examples in references.
- Validate supported host hooks; document unsupported hooks rather than implying Codex has verified SessionStart support.
- Offer refreshes of installed older copies through a reviewable installer dry-run/backup workflow after implementation. Applying changes to real agent configuration is separate from updating the packaged repository skill and requires explicit installation authorization.

Deliverable: tested transport recovery, useful startup context, and observable recall-before-decision workflows.

#### Phase 6 — Optional measured enhancements

- Evaluate the approved local reranker and stronger embedding model against held-out accuracy, CPU/RAM, model availability, and latency budgets.
- Evaluate long-memory chunks and migration/storage costs before choosing defaults.
- Prototype a native hotspot only if profiling still identifies Python CPU work as material. Consider Rust posting/filter/top-k work before a whole-service rewrite.
- Evaluate ANN at the proven scale threshold against an exact oracle, including filtered queries and parent deletion.
- Evaluate a unified bundled SQLite driver only if supported patched interpreter deployment is inadequate.

Deliverable: documented experiments and only justified production additions.

#### Phase 7 — Release hardening and evidence

- Run required checks and supported OS/Python/runtime matrices; include latest supported Python packaging as distinct from historical CI claims.
- Long-running mixed-process soak, restart/recovery, migrations, model upgrade/rollback, and installer upgrade/uninstall.
- Version schemas for content revisions, receipts/jobs/outbox/chunks as needed; take verified backups and test previous-version upgrades, interrupted migrations, and downgrade refusal.
- Prevent old/new clients from racing migrations or using incompatible schemas.
- Publish source-backed results with tested version, effective durability/readiness, limitations, and reproducible raw artifacts.
- Keep independent field-test results unchanged and update current documentation to describe implemented behavior only.

Deliverable: a reviewable release candidate with production validation evidence.

### 7. Validation details that cannot be skipped

Use temporary MESH_HOME and temporary HOME, USERPROFILE, APPDATA, LOCALAPPDATA, and XDG_CONFIG_HOME. Set MESH_NO_AUTO_DAEMON=1, MESH_NO_MODEL_DOWNLOAD=1, and MESH_NO_UPDATE_CHECK=1; no provider calls or cloud-path auto-approval. Stop every launched daemon. Preserve package discovery without pointing Mesh or agent configuration at the real home.

Tests must cover:

- Abrupt process termination before/after commit, checkpoint/write/close races, restart recovery, and acknowledged operation accounting.
- SQLite fixed/vulnerable version branches and actual linked-library capabilities.
- Memory/task/conflict/receipt/outbox transaction failures, disk-full/I/O failures, backups, migration exclusivity, and encrypted upgrade paths.
- Stale jobs, content edits/deletes during inference, backend transitions, cache eviction, and mixed-model batches.
- Private completed tasks, task mutation responses, chunk/index/fingerprint exposure, and privacy changes during ranking.
- Lost responses, partial sends, repeated IDs, mismatched payloads, expired receipts, and caller cancellation after commit.
- Bulk/scalar policy parity, batch-local duplicate/conflict behavior, ordered results, and malformed external inputs.
- Parallel namespaces, same-namespace producer fairness, bounded event-loop responsiveness, overload, cache coherence, and indexing progress under continuous foreground traffic.
- Fallback combinations and capability gaps, terminal policy errors, store-root identity, meaningful JSON/exit status, and no implicit daemon/model setup.
- Twenty-fact startup coverage, accurate counts, IDs, current-state selection, handoffs, fact-space reservation, omissions, and tiny/invalid budgets across the complete serialized prompt payload, with token estimates labeled honestly.
- Installer dry-run, backup, symlink protections, namespace argument safety, project root binding, uninstall, and preservation of user settings.
- Scripted agent call traces and clean observed supported-host scenarios showing a brief and targeted recall before decisions.

Static skill checks and scripted traces cannot prove that a model will obey instructions. The [field-test archive](field-test/README.md), tested Mesh 0.8.1b2, supports evaluating rules plus integration rather than relying on skill text alone; retain its small-sample and contamination limitations.

Process-kill tests are crash tests, not physical power-loss proof. The software contract requires patched SQLite, FULL synchronization, transaction ordering, and documented filesystem/device flush guarantees. Add controlled VM/device fault evidence before claiming empirical power-loss validation.

Required handoff checks: ruff check ., python scripts/check_skill.py, python scripts/check_docs.py, and pytest -q. The docs checker currently imports MCP dependencies despite the stated stdlib-only requirement; make schema/reference checking genuinely stdlib-only during the implementation.

### 8. Tradeoffs and recommended choices

| Choice | Recommendation and reason | Status |
|---|---|---|
| Deployment | Many agents on one machine, because it is the realistic user workload. | Approved. |
| Durability | FULL and verified patched SQLite, because recent acknowledged memories must survive crash/power loss. | Approved requirement; runtime packaging still needs validation. |
| Language | Keep Python/SQLite first; allow selective native work only after end-to-end profiling. Current logical defects and fsync costs survive a rewrite. | Recommended in this plan. |
| Reranker | Evaluate an optional local model with bounded work and explicit budgets; select by measured first-result gains. | Evaluation approved; default/model choice gated by evidence. |
| Query mode | Neural when the verified configured model is ready, explicit degraded fallback otherwise. A ready model currently does not guarantee neural search. | Recommended. |
| Startup facts | Twenty useful current facts within a 2,000-token explicit startup budget, with compact/continuation options. An unbounded dump can overwhelm context. | Recommended. |
| Task ownership | Apply enabled ownership/privacy enforcement consistently to tasks and memories; trusted-owner administration remains an explicit policy role. | Recommended. |
| Broker | Optional local group-commit aggregator with supported direct access and shared authority; exclusive-writer claims require fencing. | Recommended. |
| New store / APSW / ANN | Conditional experiments after the direct path and accurate baselines; adopt only when deployment or scale evidence justifies them. | Deferred selection. |

Approval authorizes the staged work and evidence-gated experiments, not an unconditional language/database/model replacement. Return to the maintainer if a measured result requires a material change in durability, local operation, API compatibility, model resource budget, installation requirements, or the agreed targets.

### 9. Research sources and review status

Primary sources were fetched and reviewed, not inferred solely from snippets:

- [SQLite WAL](https://www.sqlite.org/wal.html): writer concurrency, checkpoints, synchronization, and the WAL-reset race.
- [SQLite synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous): FULL/NORMAL/OFF durability contracts.
- [SQLite FTS5](https://www.sqlite.org/fts5.html): BM25 ordering and index maintenance.
- [ONNX Runtime threading](https://onnxruntime.ai/docs/performance/tune-performance/threading.html): thread, execution, and spinning controls.
- [Sentence Transformers retrieve/rerank](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html): candidate retrieval and cross-encoder tradeoffs.
- [BEIR](https://arxiv.org/abs/2104.08663): heterogeneous retrieval evaluation.
- [Elastic RRF](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion): rank fusion and candidate windows.
- [E5-small model card](https://huggingface.co/intfloat/e5-small-v2/raw/main/README.md): retrieval-model preprocessing requirements.
- [Faiss index guidance](https://github.com/facebookresearch/faiss/wiki/Guidelines-to-choose-an-index): exact/approximate, memory, and deletion tradeoffs.
- [LongMemEval](https://github.com/xiaowu0162/LongMemEval): evaluation categories and time-aware retrieval; its LLM-assisted expansion is not a default local-only Mesh proposal.
- [Tantivy](https://github.com/quickwit-oss/tantivy): a possible native lexical/index alternative.
- [sqlite-vec](https://alexgarcia.xyz/sqlite-vec/features/knn.html): an alternative vector integration to evaluate, not an assumed ANN improvement.
- [APSW installation](https://rogerbinns.github.io/apsw/install.html) and [driver comparison](https://rogerbinns.github.io/apsw/pysqlite.html): bundled-runtime possibilities and mixed-driver hazards.

Review completed: storage/concurrency, retrieval/evaluation, skill/security/installation, then independent cross-review of their interactions. The primary agent repeated important reproductions and reviewed the combined design.

Baseline validation during investigation: ruff, skill, docs, whitespace, and plan-relative-link checks passed; 71 focused primary-agent tests passed. The full isolated suite passed with 436 tests passed and 3 skipped. External connections were blocked; provider-metadata detection remained testable without calling providers. An initial run using the global no-implicit-provider flag conflicted with three metadata-detection fixtures; the guarded run resolved that environment mismatch without repository changes.

All three specialists reviewed this concrete plan after their two audit passes. Their corrections to job/outbox ordering, baseline comparability, exact receipt uniqueness, complete startup-payload budgets, long-text coverage priority, evidence sufficiency, and explicit quality/latency gates have been incorporated. Implementation is authorized and underway; final results will be reported separately.
