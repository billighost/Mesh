# Mesh v0.9.0

**Release candidate; publication pending.** See the [full changelog](../../CHANGELOG.md)
and [source-bound qualification record](../CI_VALIDATION.md).

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

[RESULTS.md](../../benchmarks/RESULTS.md) records each measured source and preserves
failed, deferred and unscored conditions. Normal CI and both full qualification
suites passed at `e4284c8`, with 1,915 selected cases in each full suite. Its
storage ledger retains 53 completed cells, four deferrals and one optional
diagnostic startup failure; primary cells have zero failures. The same-selection
test-speed observation completed. Later release changes need their own qualification.
Process-crash checks support observed recovery and do not establish physical
power-cut survival.

`MESH_PASSAGE_INDEX=1` and `MESH_RERANK_ENABLED=1` explicitly enable separate
experimental features. The default remains off. Small direct-fact and positional
long-text reranker gains do not establish population accuracy; conversational
quality and public-corpus resource qualification remain incomplete. All five
separate E5 jobs stopped in the native baseline after its selected queries;
E5 itself did not run. The shadow remains a benchmark-only model trial.

The historical 470-query comparison uses Mesh 0.8.3, Mem0 2.2.1, Basic Memory
0.24.1 and Auxly 1.5.0. It does not measure this release candidate. No generalized
storage speedup or competitor superiority follows from the current checkpoints.
