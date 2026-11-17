# Remote qualification for Mesh 0.9.0

## Current completed checkpoint (reviewed November 17, 2026)

At `b55c98013b95384a632878041cc499165bfe7610`, [push CI](https://github.com/billighost/Mesh/actions/runs/44085115201) and [PR CI](https://github.com/billighost/Mesh/actions/runs/44085124087) passed all 18 jobs each. The [production run](https://github.com/billighost/Mesh/actions/runs/44085115248) passed primary/floor correctness qualification, storage and test-speed jobs. Independently checked XMLs retain 1,944 unique testcase records each: 1,941 passed/three skipped primary and 1,940 passed/four skipped floor, zero failures/errors. Each XML separately declares a 2,149-test suite counter; that counter is not substituted for retained testcase records. Primary sdist collection reports 1,944 tests across 97 files. Coverage is 74.39%/74.34%; real process-crash recovery and primary package, installed-wheel, CLI, doctor and installer checks passed.

The complete storage ledger records 55 completed and three deferred cells, with zero failed/unavailable: 46/48 primary, 3/4 scale, all five pilots and the separate 10,000-fact neural grouped HTTP diagnostic. Two sixteen-agent neural receipt-SDK cells are resource deferred and the ungrouped neural scale cell is budget deferred. The diagnostic retains stage observations separately from primary throughput. The one/two-worker test-speed observation retained identical full-suite selections. [RESULTS.md](../benchmarks/RESULTS.md) and its linked exports preserve resources, source/constraints seals, cleanup and single-pass limits.

All five ordinary `compact-80` retrieval jobs failed overall; no optional profile qualifies across the datasets. The separate [E5 run](https://github.com/billighost/Mesh/actions/runs/44086750785) completed all five native baselines. Direct-fact and long-text E5 shadows completed/scored and failed quality. LongMemEval, NFCorpus and SciFact shadows began indexing and exceeded baseline-plus-128-MiB RSS ceilings before any shadow query; their quality is unscored and final paired resource observations remain null. E5 was not adopted and no experimental default or resource limit changed.

The [twenty-cell competitor run](https://github.com/billighost/Mesh/actions/runs/44088496624) at comparator `2db6229494a223feaa618de0cbf901650ba0d037` completed all configured cells with separately verified artifacts and recomputed numerics. Its Mesh runtime plus `pyproject.toml` Git content is identical to `b55c980`; cells actually consumed the earlier `e4284c8` qualification snapshots and frozen constraints. Linux Python 3.12.16 observations and separate official child environments do not establish identical native platforms to Windows correctness qualification. The [immutable method](https://github.com/billighost/Mesh/blob/2db6229494a223feaa618de0cbf901650ba0d037/benchmarks/COMPARISON.md) remains outside core. These source-bound observations do not substitute for normal CI and primary/floor qualification of a later release commit; measurement reuse requires exact Git paths, modes, blobs and ancestry for the measured scope.

At previous `1f370d58`, [normal CI](https://github.com/billighost/Mesh/actions/runs/44081865548) passed 18 jobs, while [production](https://github.com/billighost/Mesh/actions/runs/44081865559) passed floor and failed primary on the cold-start writer-admission deadline; dependent measurements were skipped. `b55c980` replaces that test's elapsed-time assumption with causal lock/queue ordering, without runtime changes. Earlier failed results remain historical evidence.

## November 16 checkpoint (`e4284c8`)


At `e4284c8`, [normal CI](https://github.com/billighost/Mesh/actions/runs/43906485367)
passed all 18 jobs after attempt 2 completed the two macOS jobs that had not
received runners on the first attempt. The
[production run](https://github.com/billighost/Mesh/actions/runs/43906485048)
passed both full qualification suites. Their independently checked XMLs each
contain 1,915 cases: primary has 1,912 passes and three skips; floor has 1,911
passes and four skips, with zero failures/errors and 74.38%/74.34% coverage.
Real process-crash recovery and primary wheel/sdist, installed-wheel, CLI,
doctor and installer dry-run qualification passed.

The registered storage ledger retains all 58 cells: 53 completed, four deferred
and one optional diagnostic failure. Primary coverage is 46/48, with zero
primary failures and two resource deferrals. Two grouped 10,000-fact scale
cells completed; the two ungrouped cells are budget deferred. The optional
diagnostic exited 2 before workload startup because its child environment
omitted the required Actions marker. It provides no storage-stage measurements.
The same-selection test-speed observation completed with one and two workers.

All five `compact-80` retrieval jobs failed overall. The runtime reranker meets
its own gates on the bounded direct-fact and long-text fixtures, while the
inspected LongMemEval first-result score remains 4/6 against the required 5/6.
SciFact and NFCorpus native reranking exceed added latency and CPU gates and
remain unscored. In the separate
[E5 run](https://github.com/billighost/Mesh/actions/runs/43909369990), all five
native baselines completed their selected queries and then exited 1. E5 itself
is `not_run`, with zero queries and null paired resource observations; this
does not measure E5 quality or resource rejection.

[RESULTS.md](../benchmarks/RESULTS.md) publishes the content-free numerical
exports and source provenance. This checkpoint qualifies its tested source and
environments. Subsequent source changes require their own qualification;
experimental fixture gains do not qualify a default or establish a competitor
result. The historical 470-query comparison evaluates Mesh 0.8.3.

## Earlier source checkpoints

At `2d67c704`, [normal CI](https://github.com/billighost/Mesh/actions/runs/42485932114)
and both full primary/minimum suites in the
[production run](https://github.com/billighost/Mesh/actions/runs/42485932167)
passed. The production workflow finished on November 8; it is no longer running.
Its storage matrix failed during source-manifest registration before measured
cells began. The test-speed comparison failed while validating its report
manifest. The controller and producer submitted four benchmark source files,
but the ledger's allowed paths admitted only two. Correctness qualification
therefore passed without a successful new storage or test-speed observation.

All five retrieval jobs failed overall. Native reranking meets its own gates on
the small direct-fact and positional-long-text fixtures, but ordinary default
accuracy still misses its targets. The inspected LongMemEval native score also
misses its quality target. NFCorpus native reranking exceeds added latency
limits; SciFact exceeds added latency and CPU limits. Rejected native conditions
remain unscored. The optional E5 shadow is a separate manual experiment and was
not run. [RESULTS.md](../benchmarks/RESULTS.md) retains source-specific evidence.

Subsequent source changes require their own full qualification and relevant
measurements. Neither an earlier passing suite nor an experimental fixture gain
qualifies a changed default or establishes a competitor result. The historical
470-query comparison evaluates Mesh 0.8.3; the five-dataset workflow below
qualifies Mesh configurations rather than running its competitors.

At `e353bc1`, [normal CI](https://github.com/billighost/Mesh/actions/runs/42378934838)
passed, and [the production run](https://github.com/billighost/Mesh/actions/runs/42378934727)
passed both full qualification suites, the concurrent-write matrix, and the
same-selection test-speed comparison. The immutable SQLite snapshot repair is
verified in native retrieval runs. [RESULTS.md](../benchmarks/RESULTS.md) records
the tested source and retains earlier evidence.

At that checkpoint, retrieval qualification failed. Compact native reranker conditions met
their own gates on the direct-fact and positional long-text fixtures, while the
inspected LongMemEval conditions miss the accuracy target. Some full-length
conditions exceed the incremental resource limits. Every public-corpus job at `e353bc1`
exceeded the unchanged cumulative CPU budget; the completed SciFact passage
condition attributes most worker CPU to queries. No profile qualifies across
all evaluated datasets. Baseline failures, rejected profiles, and unscored
phases remain visible; passing storage tests do not waive retrieval gates.

The reviewed cache repair omits redundant primary lexical terms only for complete
validated long-parent passage generations, with a complete primary streaming
fallback. Later source-bound passage conditions complete on the public corpora;
full optional reranker quality and resource qualification remains incomplete. Post-export native reranker diagnostics
record candidate ranks and input geometry after scoring, without exposing text
or changing ranking inputs. They can identify an evidence-prefix limitation or
an inside-prefix miss; they cannot infer answer-span containment from document
relevance labels.

Completed passage queries retain numeric cache charges and returned-result
cache/streaming counts. Flushed query-resource rows and atomic provenance
checkpoints also preserve partial work after process termination, including the
fixed baseline and C4 roles. Supervisors read them after workers stop; progress
remains unscored and cannot satisfy completion, accuracy, or resource gates.

The production changes and reviewed measurement utilities are published on
`ci/mesh-production-run-c4-20261106`. At `6060925`, [normal CI](https://github.com/billighost/Mesh/actions/runs/42338200399)
passed all fifteen OS/Python combinations, packaging, security, documentation,
skills and installed-wheel checks. [Production qualification](https://github.com/billighost/Mesh/actions/runs/42338200383)
passed the primary and minimum full isolated suites, real process-crash recovery,
coverage and primary installed-wheel validation. Its performance matrix and
same-selection one-versus-two-worker observations also completed successfully.
The source-bound numerical results and resource deferrals are published in
[RESULTS.md](../benchmarks/RESULTS.md).

The foreground-progress and test-fixture interference repairs are qualified.
Reviewed HTTP clients reuse bounded connections while proving identity afresh on
every call, including rejecting a closed identity connection before token dispatch.

At `ecd96b5`, [normal CI](https://github.com/billighost/Mesh/actions/runs/42342135569)
also passed all fifteen combinations, and [production qualification](https://github.com/billighost/Mesh/actions/runs/42342135644)
passed the primary and minimum suites, crash recovery and primary package checks.
Native scoring exposed optional-ordinal metadata incompatibility in the legacy
child experiment. The latest focused repairs preserve absent-ordinal semantics
and record content-free worker diagnostics. Completed zero-child primary
generations now activate the same authorized scorer as long-parent generations.

The production passage condition runs after its fresh-default pair and before
legacy experiments; its evidence survives a later experimental failure. Exact
parent, model, span, readiness and source pairing checks remain required. Final
native passage and retrieval qualification remains pending. At `675970f`,
[normal CI](https://github.com/billighost/Mesh/actions/runs/42344319962) passed all
fifteen combinations, and both full production qualification suites passed in
[the production run](https://github.com/billighost/Mesh/actions/runs/42344319969).
Worker diagnostics identified a string-versus-integer schema check that stopped
the retrieval experiment before passage indexing. The repaired benchmark boundary
accepts only canonical schema 6 and records its numeric form consistently.
Focused checks do not replace corrected native quality and resource gates.

At `7ee42afb`, [normal CI](https://github.com/billighost/Mesh/actions/runs/42346677047)
and both full qualification suites in
[the production run](https://github.com/billighost/Mesh/actions/runs/42346677098)
passed. That production run failed retrieval qualification: the bounded fixtures
retained first-result misses, and passage backfill stalled on both public corpora.
The latest reviewed changes add a default-off production reranker and actual
`Mesh.recall` qualification paired with the ordinary runtime passage results.
They also protect active passage backfill and reranker inference from idle model
unloading. A small integrated Python 3.10 check passed; native quality and resource
qualification of these changes remains pending.

The development machine has 8 GiB RAM. Local work uses small isolated checks
with one test worker and no model or corpus evaluation. Full suites, concurrent
workloads, and model evaluations run on GitHub-hosted runners.

## Qualification and measurement flow

```mermaid
flowchart TD
    S[Reviewed source commit] --> C[Ubuntu, macOS and Windows CI]
    S --> Q[Windows primary and minimum qualification]
    Q --> P[Serial concurrent-write matrix]
    Q --> R[Five independent retrieval datasets]
    Q --> T[One versus two test workers]
    R --> O[Optional four-candidate reranker]
    C --> E[Source-bound results and failures]
    P --> E
    R --> E
    O --> E
    T --> E
```

[Normal CI](../.github/workflows/ci.yml) runs lint, security, documentation and
skill checks, full suites with the existing property-test and coverage gates on
Python 3.10–3.14 across three operating systems, and installed-wheel validation.
The source archive must also contain the helpers needed to collect its bundled
tests; Actions checks the extracted archive in an isolated environment.

[Production qualification](../.github/workflows/production-performance.yml)
tests its exact commit on Windows with Python 3.14.0 and Python 3.10. The minimum
job pins APSW 3.51.3.0. Both suites must pass; the primary also validates process
crash recovery, at least 60% coverage, and the built package. Each job records
source hashes and exact installed dependency versions before measurement.
Full qualification uses two pytest workers, retaining the complete selection,
property-test profile, coverage gate, and each test's own concurrency probes.

Qualification is scheduled independently for each Python kind. Performance
matrices and test-speed observations keep separate per-branch concurrency groups,
so an older matrix does not delay qualification or retrieval of a repaired source.

Measurements use those source hashes and dependency constraints. A missing
qualification artifact, changed source, failed integrity check, unknown receipt,
or executed workload failure prevents a successful final result. Artifacts
preserve failures and explicit resource or budget deferrals.

## Registered workloads

| Job | Scope and purpose |
| --- | --- |
| Concurrent storage | Five pilots, 48 serial primary cells with 512 input facts, scalar and atomic calls, receipt SDK and HTTP, hash and neural modes, and one/four/eight/sixteen local agents |
| Scale extension | Four conditional sixteen-agent HTTP cells with 10,000 input facts; requires declared RAM, readiness and time-budget gates |
| Retrieval | NFCorpus, SciFact, direct facts, positional long memories, and the inspected six-case LongMemEval fixture; keeps guarded, saved-vector replay, fresh default queries, child-passage experiments and opt-in production passage queries separate |
| Optional C4 | Reranks four authorized parent candidates in a separate experiment; requires complete paired quality accounting and fixed RAM, CPU and latency limits |
| Test speed | Runs the same full selection once with one worker and once with two; retains source, dependency, seed, skipped-test and resource accounting |

The production process-tree RSS cap defaults to 4,096 MiB, with 2 GiB of runner
headroom. Larger scenarios can be deferred when their measured resource
projection is unsafe. Retrieval uses whole-run wall, sampled RSS and CPU limits.
The optional reranker uses stricter incremental resource limits documented in
[RETRIEVAL_C4.md](../benchmarks/RETRIEVAL_C4.md). None of these registered budgets
is a measured speed or accuracy claim.

The accuracy gates treat retrieval misses as failures. A passage experiment
can pass its gate without qualifying the current
fresh-query default. Selecting a production representation or model still
requires the relevant quality, identity, privacy, resource and regression
checks to pass.

## Starting the remote checks

An ordinary `ci/mesh-production-*` branch runs qualification. A reviewed
`ci/mesh-production-run-*` branch also requests performance, retrieval and test
speed. The narrower `ci/mesh-production-run-c4-*` prefix explicitly requests
the optional reranker experiment. Manual workflow dispatch exposes equivalent
controls and can reduce the registered scope.

For reviewed benchmark-only repairs, a `[retrieval-only]` commit marker keeps all
qualification and retrieval jobs while omitting the extra storage matrix and
test-speed observations. A `[qualification-only]` marker keeps full qualification
for documentation/evidence updates. The
[scope checker](../scripts/check_ci_measurement_scope.py) inspects the entire push
diff and rejects these markers when runtime, storage-harness or test-launcher
sources changed. Marker comparison matches GitHub's case-insensitive expressions.
A missing push base also rejects the shortcut; fetch its history or use full
scope. Manual workflow inputs still choose measurements directly. Earlier
measurements retain their tested commit, version and source hashes; these
shortcuts do not qualify changed sources by reusing old numerical evidence.

The maintainer explicitly authorized publishing this CI branch, overriding the
repository [AGENTS.md](../AGENTS.md) push restriction for this work. Subsequent
repairs use ordinary commits and fast-forward pushes to the same branch.
Scoped GitHub access reads run status and evidence without exposing credentials.

## Evidence required before completion

Final completion requires passing normal CI and same-source production
qualification, followed by the relevant successful measurements and retrieval
gates. Performance claims must be published with their tested version and
provenance in [RESULTS.md](../benchmarks/RESULTS.md). Failed or deferred cells
remain visible. Process-death tests support crash recovery; physical power-cut
qualification remains separate hardware/filesystem work.
