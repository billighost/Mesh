# Benchmarks State & Tracking

**Document Version:** 1.7.0  
**Current Step:** CHECKPOINT B (Official 20-Question LongMemEval-S Pilot Run Complete)  
**Target Commit / Base:** `26204a3` (`feat/ci-evidence`)  
**Timestamp:** 2026-10-28T07:05:00+01:00  

---

## 1. Current Step: CHECKPOINT B (Pilot Evaluation Complete)
- **Status:** STEP 3 (20-question official LongMemEval-S pilot run) completed successfully across all 3 interface classes (Class A, Class B, Class C) and 9 evaluators.
- **Sleep-Proofing Audit:** Zero Kernel-Power sleep/resume events (Event ID 42 / 107) detected across the entire 3-hour run window. Run is 100% exclusive and uncontaminated.
- **Known-Item Gate:** Mesh SDK (20/20), Mem0 SDK (20/20), Mesh CLI Daemon (20/20), Mesh CLI Cold (20/20), Auxly CLI (20/20), Auxly MCP (20/20), Mesh MCP (20/20) all passed Rank 1 known-item check. Basic Memory CLI (0/20) and Basic Memory MCP (0/20) failed known-item tag recovery due to snippet/frontmatter truncation.
- **Long-Turn Tag Gate (3,000+ chars):** Mesh SDK (20/20), Mem0 SDK (20/20), Mesh CLI Daemon (20/20), Mesh CLI Cold (20/20), and Mesh MCP (20/20) preserved tags at start of long turns. Auxly CLI (0/20), Auxly MCP (0/20), Basic Memory CLI (0/20), and Basic Memory MCP (0/20) dropped tags due to snippet truncation.
- **Target Deliverable:** Deliver **CHECKPOINT B REPORT** and STOP for user time-budget and parallel worker configuration approval before starting STEP 4.

---

## 2. Completed Items & Commits
- `26204a3`: fix(benchmarks): handle distractor exceptions gracefully in mem0 worker
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
1. Stop at CHECKPOINT B.
2. Deliver the Checkpoint B report to the user.
3. Wait for the user's explicit time-budget and parallel worker configuration approval before starting STEP 4.
