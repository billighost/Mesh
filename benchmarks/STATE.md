# Benchmarks State & Tracking

**Document Version:** 1.4.0  
**Current Step:** STEP 3 (Pilot with Official Per-Question Haystacks)  
**Target Commit / Base:** `589e2df` (`feat/ci-evidence`)  
**Timestamp:** 2026-10-27T23:15:00+01:00  

---

## 1. Current Step: STEP 3 (Pilot Run - 20 Seeded Questions)
- **Status:** Checkpoint A approved by user. Active execution of STEP 3.
- **Protocol:** Official LongMemEval-S per-question haystack protocol:
  - 20 questions (seeded subset, `seed=42`).
  - Fresh store created per question, per tool, per interface class.
  - Ingest that question's haystack turns (one memory per turn with opaque `[turn:<idx>]` suffix), query once, record metrics, write time, query latency, disk footprint, then destroy/clean up store.
  - Exclude abstention questions (`_abs`) from retrieval scoring.
- **Measurements & Projections:**
  - Measure turns per question, ingest time, query time, disk footprint, and RAM per question.
  - Project full 500-question wall-clock totals for every tool and class under:
    (a) Sequential execution.
    (b) 2 or 3 parallel workers.
  - Validate parallel worker isolation harness under `benchmarks/work/<worker>/` (gitignored, distinct ports, isolated `HOME`/`APPDATA`/`MESH_HOME`).
- **Target Deliverable:** Deliver **CHECKPOINT B** report and STOP for user time-budget approval before starting STEP 4.

---

## 2. Completed Items & Commits
- `589e2df`: benchmarks: record 3 sequential smoke test runs across Classes A, B, and C at Checkpoint A
- `d3c50d4`: docs(benchmarks): update STATE.md to v1.3.0 at Checkpoint A
- `77a093f`: benchmarks: update STATE.md to v1.2.0 and record ranking analysis findings
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
- None.

---

## 4. Next Action
1. Download official LongMemEval S dataset (`longmemeval_s.json`) if not present, verify SHA-256 and MIT license.
2. Generate seeded 20-question pilot dataset (`benchmarks/data/longmemeval_pilot_20.json`, seed 42) from non-abstention questions.
3. Implement pilot runner with per-question fresh stores and worker isolation validation.
4. Execute pilot run across Classes A, B, and C.
5. Compute per-question averages and 500-question projections for sequential vs parallel workers.
6. Compile CHECKPOINT B report and STOP for user time-budget approval.

---

## 5. Decisions Made
1. **Checkpoint A Approved:** Proceeding to STEP 3 Pilot per brief specifications.
2. **Per-Question Store Lifecycle:** Each question in the pilot provisions a completely fresh, isolated store per tool and class, ingests the question's haystack sessions, runs the query, measures resources, and tears down the store.
3. **Worker Isolation Validation:** Parallel worker directories structured under `benchmarks/work/<worker>/` with distinct ports and environment variables.
