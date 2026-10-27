# Benchmarks State & Tracking

**Document Version:** 1.2.0  
**Current Step:** STEP 1 Complete -> STEP 2 Smoke Rerun (3 Sequential Runs)  
**Target Commit / Base:** `afa203c` (`feat/ci-evidence`)  
**Timestamp:** 2026-10-27T06:19:00+01:00  

---

## 1. Current Step
- **Active Step:** STEP 1 (Fixes) Completed cleanly. Ready for STEP 2 (Smoke Rerun: 3 clean, sequential runs).
- **Sub-tasks Status:**
  1. **Scoring Unit (Two-Level Reporting):** COMPLETED (`f38c1de` / `afa203c`). Independent tables for turn-level and session-level retrieval metrics; Basic Memory CLI aligned to 1-note-per-turn; cross-class parity verification verified identical across all runners.
  2. **Mesh Daemon-mode CLI Tail Latency:** COMPLETED (`129c203` / `0f944c0`). Fast `wmic` process lookup with PowerShell fallback; server child PID persistence; configurable timeouts (`MESH_DAEMON_REQUEST_TIMEOUT=10.0s`). Zero spurious duplicate daemons; 18 new unit tests passing.
  3. **Mesh Ranking Analysis:** COMPLETED. All 33 non-rank-1 queries analyzed and cataloged in `benchmarks/results/mesh_ranking_analysis.json`. Lexical distractor interference cataloged in backlog. Algorithm left strictly untouched.
  4. **Query Handling (Symmetric Raw Text):** COMPLETED (`f38c1de` / `afa203c`). Exact raw question text sent to all tools. Auxly-only word splitting loop removed; Auxly evaluated as-is (keyword-only).
  5. **Repeatability & Determinism:** COMPLETED. 100% deterministic turn-tag ranking confirmed across runs. Root causes of Smoke 1 vs Smoke 2 shifts documented and verified.
  6. **Complete Tables & Metrics:** COMPLETED (`f38c1de` / `afa203c`). Full bootstrap 95% CIs on Recall@1/5/10, MRR, NDCG@10, latency p50/p95/p99, RAM, disk, time-to-searchable, and raw top-5 audit inspections.
  7. **Commit Hygiene:** COMPLETED. `mesh_inspect` serialization fix isolated in `0cb4636` (zero impact on recall); daemon fix in `0f944c0`; harness fixes in `afa203c`.
  8. **Security Leftovers:** COMPLETED. Bandit B105 and B406 false positives documented; pip-audit clean; CI bandit artifact in place.

---

## 2. Completed Items & Commits
- `afa203c`: merge task/harness-fixes: implement two-level scoring, symmetric query handling, and complete metrics tables
- `f38c1de`: benchmarks: implement two-level scoring, symmetric query handling, and complete metrics tables
- `0f944c0`: merge task/daemon-fix: eliminate CLI tail latency, timeouts, and process tracking on Windows
- `129c203`: fix(daemon): eliminate CLI tail latency, timeouts, and process tracking on Windows
- `ae8c599`: benchmarks: record final Phase 7 second smoke test results across Classes A, B, and C
- `8c36bf8`: benchmarks: use schema-valid limit for mesh_inspect
- `08cbce1`: benchmarks: add full audit notes to Mesh MCP
- `ee0da1c`: benchmarks: fix Auxly MCP pending queue assertion
- `0cb4636`: benchmarks: update multi-class smoke test harness and fix mesh_inspect serialization
- `e258ad3`: fix(benchmarks): set os.environ[MESH_HOME] before seeding CLI databases
- `bf1bb70`: feat(benchmarks): add dedicated python 3.12 worker for Mem0 SDK execution
- `5d4372c`: fix(benchmarks): use mesh.store.list_all(quarantined_only=True)
- `e57f6fe`: fix(benchmarks): use mesh.store.list(quarantined_only=True) for quarantine audit
- `85d635a`: test(benchmarks): commit Phase 7 multi-class smoke test harness before execution
- `9058771`: docs(benchmarks): adopt comprehensive Interface Classes protocol (Class A, B, C) in Phase 7 Plan
- `1df4d50`: test(benchmarks): implement Phase 7 plan and execute smoke test (B.1-B.10)
- `9a21be1`: feat(audit): apply Fixes A.1-A.4 for access tracking, doc URLs, backup hardening, and CI bandit artifacts

---

## 3. Running Subagents & Worktrees
- None. All subagents killed and worktrees cleaned up. Main agent has exclusive machine access.

---

## 4. Next Action
1. Commit `benchmarks/STATE.md` and `benchmarks/results/mesh_ranking_analysis.json` so the working tree is 100% clean.
2. Execute 3 sequential, exclusive smoke runs of the 50-question set from clean stores on an idle machine.
3. Save each run's raw outputs (`smoke_test_results_run1.json`, `run2.json`, `run3.json`).
4. Compile Checkpoint A report and STOP for user go-ahead.

---

## 5. Decisions Made
1. **Scoring Unit Parity:** Both turn-level and session-level metrics are computed and displayed in separate tables. Basic Memory CLI and MCP share identical 1-note-per-turn layouts.
2. **Raw Query Symmetry:** All tools receive the exact raw question string without asymmetric stopword removal or substring expansion.
3. **No Benchmark Ranking Changes:** Mesh ranking algorithm remains strictly untouched for the duration of the benchmarks.
4. **Daemon Latency Diagnosis:** Tail latency driven by PowerShell timeout (spawning duplicate daemons) and tight 4s CLI timeout falling back to in-process execution with lock contention. Fixed in `0f944c0`.

---

## 6. Known Issues / Improvement Backlog (For future release, NOT tuned during benchmarks)
1. **Query Framing Word Dampening:** Common English temporal/ordinal query framing words ("first", "last", "before", "after", "which", "service") receive excessive lexical BM25 weight in distractor turns, outranking semantically closer gold turns (observed in 32 of 33 non-rank-1 queries).
2. **Semantic Floor vs Lexical Distractors:** In hybrid score fusion, when semantic similarity is very low (<0.10), high lexical score can still yield top rank. Consider a dynamic semantic floor that scales down lexical boost when semantic alignment is negligible.
3. **Short Distractor Length Normalization:** Short conversational turns with 1-2 overlapping terms achieve high TF-IDF density.
