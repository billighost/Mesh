# Benchmarks State & Tracking

**Document Version:** 1.7.1  
**Current Step:** CHECKPOINT B REVIEWED (Awaiting Pilot v2 Issue List)  
**Target Commit / Base:** `6988c89` (`feat/ci-evidence`)  
**Timestamp:** 2026-10-28T10:38:00+01:00  

---

## 1. Current Step: CHECKPOINT B REVIEWED -> PILOT v2
- **Status:** Checkpoint B reviewed by user. Time budget and worker configuration approved in principle (2 workers; PASS 1 ~6.7 h, PASS 2 ~1.2 h).
- **Directive:** Do NOT start STEP 4. Fix specified issues and execute PILOT v2 (same 20 questions, same seed=42) and stop at CHECKPOINT B2.
- **Rule:** No pilot number may be quoted anywhere outside `STATE.md`.
- **Awaiting:** User issue list for PILOT v2 (prompt received ended after "First fix the issues below and run PILOT v2...").

---

## 2. Completed Items & Commits
- `6988c89`: docs(benchmarks): update STATE.md to v1.7.0 and record 20-question pilot results at Checkpoint B
- `6751daa`: fix(benchmarks): handle guard rejection of adversarial distractor turns in LongMemEval
- `b464306`: fix(benchmarks): set MESH_MAX_CONTENT_LENGTH to 131072 for long-turn LongMemEval questions
- `707933c`: feat(benchmarks): implement official 20-question LongMemEval-S pilot harness
- `8a7d4ad`: feat(benchmarks): add process cleanup and exclusivity assertions to sleep_guard
- `8893e01`: docs(benchmarks): update STATE.md to v1.5.0 for STEP 3 pilot execution
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
- `0f944c0`: merge task/daemon-fix: eliminate CLI tail latency, timeouts, and process tracking on Windows

---

## 3. Running Subagents & Worktrees
- None.

---

## 4. Measured Pilot Summary (20 Official LongMemEval-S Questions, 9,782 turns)

| Tool | Class | Write Time (ms/turn) | Raw Query p50 (ms) | Turn Recall@1 (95% CI) | Turn Recall@5 (95% CI) | Session Recall@5 (95% CI) | Peak RSS (MB) | Data Size (MB) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Mesh SDK** | A | 118.2 ms | 30.8 ms | 0.30 [0.10, 0.50] | 0.75 [0.55, 0.95] | 1.00 [1.00, 1.00] | 273.3 MB | 3.42 MB |
| **Mem0 SDK** | A | 73.0 ms | 83.6 ms | 0.35 [0.15, 0.55] | 0.80 [0.60, 0.95] | 0.90 [0.75, 1.00] | 332.5 MB | 3.54 MB |
| **Mesh CLI (Daemon)** | B | 114.7 ms | 55.2 ms | 0.30 [0.10, 0.50] | 0.75 [0.55, 0.95] | 1.00 [1.00, 1.00] | 426.1 MB | 3.43 MB |
| **Mesh CLI (Cold)** | B | 109.9 ms | 4,246.1 ms | 0.40 [0.20, 0.60] | 0.80 [0.65, 0.95] | 0.90 [0.75, 1.00] | 348.5 MB | 3.43 MB |
| **Auxly CLI** | B | 1.1 ms | 990.2 ms | 0.00 [0.00, 0.00] | 0.00 [0.00, 0.00] | 0.00 [0.00, 0.00] | 332.1 MB | 0.48 MB |
| **Basic Memory CLI**| B | 46.8 ms | 13,343.2 ms | 0.00 [0.00, 0.00] | 0.00 [0.00, 0.00] | 0.00 [0.00, 0.00] | 435.4 MB | 0.82 MB |
| **Mesh MCP** | C | 138.1 ms | 57.9 ms | 0.90 [0.75, 1.00] | 0.90 [0.75, 1.00] | 1.00 [1.00, 1.00] | 441.9 MB | 3.43 MB |
| **Auxly MCP** | C | 1.1 ms | 1,021.3 ms | 0.00 [0.00, 0.00] | 0.00 [0.00, 0.00] | 0.00 [0.00, 0.00] | 256.4 MB | 0.49 MB |
| **Basic Memory MCP**| C | 1.1 ms | 273.3 ms | 0.00 [0.00, 0.00] | 0.00 [0.00, 0.00] | 0.00 [0.00, 0.00] | 692.8 MB | 70.55 MB |

---

## 5. 500-Question Wall-Clock Projections

### Individual Tool Projections (All 500 Questions)
| Evaluator | Class | Mean sec/q | Sequential (1w) | 2 Workers | 3 Workers |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Mesh SDK** | Class A | 57.87 s | 8.04 h (482.3 m) | 4.02 h (241.1 m) | 2.68 h (160.8 m) |
| **Mem0 SDK** | Class A | 35.77 s | 4.97 h (298.1 m) | 2.48 h (149.0 m) | 1.66 h (99.4 m) |
| **Mesh CLI (Daemon)**| Class B | 56.17 s | 7.80 h (468.1 m) | 3.90 h (234.0 m) | 2.60 h (156.0 m) |
| **Mesh CLI (Cold)** | Class B | 58.02 s | 8.06 h (483.5 m) | 4.03 h (241.8 m) | 2.69 h (161.2 m) |
| **Auxly CLI** | Class B | 1.53 s | 0.21 h (12.8 m) | 0.11 h (6.4 m) | 0.07 h (4.3 m) |
| **Basic Memory CLI**| Class B | 36.66 s | 5.09 h (305.5 m) | 2.55 h (152.7 m) | 1.70 h (101.8 m) |
| **Mesh MCP** | Class C | 67.61 s | 9.39 h (563.4 m) | 4.70 h (281.7 m) | 3.13 h (187.8 m) |
| **Auxly MCP** | Class C | 1.59 s | 0.22 h (13.3 m) | 0.11 h (6.6 m) | 0.07 h (4.4 m) |
| **Basic Memory MCP**| Class C | 0.91 s | 0.13 h (7.6 m) | 0.06 h (3.8 m) | 0.04 h (2.5 m) |

### STEP 4 PASS 1 Reduced Matrix (1 interface per tool: Mesh SDK + Mem0 SDK + Basic Memory MCP + Auxly MCP)
- **Sequential (1 worker):** 13.36 hours (~801.4 minutes)
- **2 Parallel Workers:** 6.67 hours (~400.7 minutes)
- **3 Parallel Workers:** 4.45 hours (~267.1 minutes)

---

## 6. Next Action
1. Receive and log user issue list for PILOT v2 under Decisions Made.
2. Implement fixes in dedicated commits with tests.
3. Execute PILOT v2 (same 20 questions, same seed=42) under exclusive sleep-proofing.
4. Update `benchmarks/STATE.md` with Pilot v2 results and projections.
5. Deliver CHECKPOINT B2 report and STOP for user approval.
