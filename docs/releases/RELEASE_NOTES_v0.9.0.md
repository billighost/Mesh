# Mesh v0.9.0

See the [full changelog](../../CHANGELOG.md) and
[source-bound qualification record](../CI_VALIDATION.md).

Mesh 0.9.0 improves the durability and coordination of agents sharing local
memory. It keeps the Python package and supported SDK, CLI, MCP and HTTP entry
points while adding a required patched SQLite runtime through APSW.

## Changes

- **Durable storage:** WAL/FULL synchronization and runtime capability checks;
  task changes and their history commit atomically.
- **Revision-bound work:** Schema 6 binds indexing jobs, mutation delivery and
  operation receipts to memory revisions, incarnations and store generations.
  Lost replies can be reconciled using the original operation ID.
- **Bounded execution:** Atomic bulk ingestion, optional server group commits,
  bounded worker admission, shared cache accounting and complete streaming
  fallback when a generation exceeds its budget.
- **Namespace and privacy recovery:** Synchronized staged namespace publication,
  validated recovery journals, current identity checks and server-enforced
  privacy/ownership rules.
- **Agent context:** Expanded project briefs, recall-before-work instructions,
  reviewed connection profiles and equivalent-policy MCP/CLI/SDK/HTTP fallback.
- **Qualification tooling:** Isolated test launchers and source-bound benchmarks
  separate durable acceptance, completed indexing, quality and resource costs.

## Upgrade

Python 3.10+ is required. Mesh's wheel is platform independent; APSW supplies its
native SQLite dependency. Stop agents and the daemon and back up the complete
`MESH_HOME` before upgrading. Populated namespace migration verifies a backup
before schema changes. An older package must not open the upgraded schema;
rollback requires restoring the matching pre-upgrade backup.

After installing the intended package, verify it with `mesh version`, restart
MCP clients/daemons, and preview integration updates with `mesh install AGENT
--dry-run`. See [the upgrade guide](../../DOCS.md#upgrading-to-090).

## Evidence and experimental features

[RESULTS.md](../../benchmarks/RESULTS.md) records the tested sources and preserves failed, deferred and unscored conditions. At `b55c980`, normal push/PR CI and both full qualification suites passed. Each XML retains 1,944 unique testcase records; its separately declared 2,149-test counter is preserved distinctly. The storage ledger completed 55 of 58 cells with three deferrals and zero failures, including the separate storage diagnostic. The same-selection one/two-worker test-speed observation completed. Process-crash checks support observed recovery and do not establish physical power-cut survival. Later source changes require their own correctness qualification and exact source proofs for any measurement reuse.

The fresh twenty-cell comparison at `2db6229` evaluates Mesh 0.9.0, Mem0 2.2.1, Basic Memory 0.24.1 and Auxly 1.5.0 on five shared datasets. It retains 9,072 original parents and 660 selected queries per tool, with original relevance denominators and source-equivalent Mesh runtime/package content. Native Linux Python 3.12.16 observations consume earlier `e4284c8` Windows qualification snapshots and retain separate official child environments. SDK/MCP interfaces, windowed/truncated MiniLM and keyword/substring profiles differ; Auxly has unordered subset metrics. These single-pass observations establish no universal superiority or cross-interface speed ratio. The six-query inspected LongMemEval subset does not repeat the historical 470-query Mesh 0.8.3 comparison.

`MESH_PASSAGE_INDEX=1` and `MESH_RERANK_ENABLED=1` explicitly enable separate experimental features; both remain default off and unqualified across the tested datasets. Small direct-fact and positional-long-text reranker gains do not establish population accuracy. In the separate E5 trial at `b55c980`, all five native baselines completed. Two E5 shadows completed/scored and failed quality; three began indexing and exceeded baseline-plus-128-MiB RSS ceilings before any shadow query, leaving quality unscored and final paired resource observations null. E5 was not adopted, the production embedding model is unchanged, and no resource gate was relaxed.
