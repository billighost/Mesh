# Benchmarks State & Tracking

**Document Version:** 1.6.0  
**Current Step:** STEP 3 (Running 20-Question Pilot across Classes A, B, C)  
**Target Commit / Base:** `707933c` (`feat/ci-evidence`)  
**Timestamp:** 2026-10-28T03:17:00+01:00  

---

## 1. Current Step: STEP 3 (Pilot Run - 20 Seeded Questions)
- **Status:** Checkpoint A approved by user. Active execution of STEP 3.
- **Protocol:** Official LongMemEval-S per-question haystack protocol:
  - 20 seeded questions (`seed=42`) sampled from 470 non-abstention questions in official `longmemeval_s_cleaned.json`.
  - Fresh store provisioned per question, per tool, per interface class.
  - Ingest that question's haystack turns (one memory per turn with opaque `[turn:<idx>]` at START of memory text), query once with raw and keyword variants, record metrics, write time, query latency, per-file disk footprint, and process-tree peak RAM via psutil, then destroy/clean up store.
  - Abstention questions (`_abs`) excluded from retrieval scoring.
  - Measure turns per question, ingest time, query time, disk footprint (data vs model/asset breakdown), and process-tree peak RAM per question.
  - Project full 500-question wall-clock totals for every tool and class under:
    (a) Sequential execution.
    (b) 2 or 3 parallel workers.
  - Validate parallel worker isolation harness under `benchmarks/work/<worker>/` (distinct ports, isolated `HOME`/`APPDATA`/`MESH_HOME`).
- **Target Deliverable:** Deliver **CHECKPOINT B** report and STOP for user time-budget approval before starting STEP 4.

---

## 2. Completed Items & Commits
- `707933c`: feat(benchmarks): implement official 20-question LongMemEval-S pilot harness
- `1cc9eba`: test(benchmarks): add parallel worker directory and environment isolation test
- `2f0bcb7`: feat(benchmarks): add official LongMemEval-S 20-question pilot sampler
- `7769597`: feat(benchmarks): implement sleep-proofing guard, power assertions, and Kernel-Power event auditor
- `a619acd`: feat(daemon): add /ready probe, startup model warmup wait, and cache isolation
- `0c7723f`: benchmarks: ignore raw LongMemEval datasets, pilot subsets, and worker directories
- `48cfdfb`: docs(benchmarks): update STATE.md to v1.4.0 for STEP 3 pilot execution
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

---

## 3. Running Subagents & Worktrees
- None.

---

## 4. Next Action
1. Implement `benchmarks/run_pilot.py` incorporating:
   - Per-question fresh store lifecycle.
   - Tag placement at start of turn text (`[turn:<idx>] <content>`).
   - 3,000+ char long-turn snippet check.
   - Symmetric keyword extraction query variant (`extract_keywords`).
   - Auxly 1 file per turn layout for turn-level comparability.
   - Per-file disk footprint breakdown (data vs shared model cache).
   - Process-tree peak RAM monitoring via `psutil`.
   - DualTimer wall vs monotonic tracking and Kernel-Power sleep event audit.
2. Execute pilot across Classes A, B, and C.
3. Compute per-question averages and 500-question projections for sequential vs parallel workers.
4. Compile CHECKPOINT B report and STOP for user time-budget approval.

---

## 5. Decisions Made
1. **Sleep Contamination Root Cause Confirmed & Corrected:** Windows System Event Viewer Kernel-Power log confirmed event ID 42 (Sleep) and 107 (Resume) during smoke run 3. The 46,774 s Basic Memory CLI write figure was caused by user closing laptop lid and laptop sleeping, NOT tool or harness instability. Run 1 write speed (~0.23 s/write) stands as the uncontaminated valid measurement. Smoke run 2 cold CLI p50 of 20.9 s was contaminated by disk cleanup overlap and is discarded.
2. **Sleep-Proofing Protocol Active:** `benchmarks/sleep_guard.py` enforces AC line check, AC sleep timeout = 0s check, `ExecutionStateGuard(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`, `DualTimer`, and post-run Kernel-Power event log verification.
3. **Daemon Readiness Probe Implemented:** Daemon preloads model on start and exposes `GET /ready` (returns 503 while warming, 200 when ready). `start_daemon(wait=True)` polls readiness probe up to `MESH_DAEMON_READY_TIMEOUT` (30s) and returns `startup_duration_seconds`.
4. **Official Dataset Verified:** Downloaded official `longmemeval_s_cleaned.json` (SHA-256: `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`). Sampled 20 seeded non-abstention questions (`seed=42`, total 9,782 turns, mean 489.1 turns/question).
5. **Worker Isolation Verified:** Parallel worker directory isolation verified under `benchmarks/work/<worker>/` via `tests/test_worker_isolation.py`.
