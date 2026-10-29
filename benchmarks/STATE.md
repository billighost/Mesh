# Benchmarks State & Tracking

**Document Version:** 1.8.0  
**Current Step:** CHECKPOINT B2 COMPLETED (Pilot v2 Evaluated, Awaiting User Review)  
**Target Commit / Base:** `fedf7b1` (`feat/ci-evidence`)  
**Timestamp:** 2026-10-29T09:05:00+01:00  

---

## 1. Current Step: CHECKPOINT B2 COMPLETED -> AWAITING STEP 4 APPROVAL
- **Status:** PILOT v2 (20 questions, `seed=42`) completed successfully across all 9 evaluators (Classes A, B, and C).
- **Directives Followed:**
  1. Resolved Mesh MCP 0.90 artifact (restored genuine candidate ranking; scores 0.30 / 0.75 / 0.491 identical to SDK).
  2. Snapshotting stores after ingestion eliminated access-counter drift across queries.
  3. Forced cold CLI ONNX model via `MESH_CLI_FORCE_ONNX=1` (eliminated hash fallback).
  4. 4-way Mesh interface parity verified (SDK == Daemon == Cold == MCP).
  5. Implemented primary ID-based turn extraction (`[turn:XXXXX]`) with tag fallback.
  6. Basic Memory full vector reindexing (`bge-small-en-v1.5`) active; gates pass 20/20 (100%).
  7. Auxly search semantics documented (exact substring matching offline fallback); gates pass 20/20 (100%).
  8. Harness baseline RAM subtracted; true tool peak RSS measured.
  9. Disk categorized into DATA vs MODEL/ASSET roles.
  10. Sleep-proofing asserted throughout; 0 Kernel-Power sleep/resume events detected in 8+ hour run.
- **Rule Enforced:** No pilot numbers quoted outside `STATE.md` or Checkpoint B2 report.

---

## 2. Completed Items & Commits
- `fedf7b1`: fix(benchmarks): use stable turn identifier for ranked parity and wrap daemon in try/finally
- `bc93c4f`: fix(benchmarks): implement Checkpoint B review fixes for Pilot v2
- `837876c`: docs(benchmarks): update STATE.md to v1.7.1 noting Checkpoint B approval in principle
- `6988c89`: docs(benchmarks): update STATE.md to v1.7.0 and record 20-question pilot results at Checkpoint B
- `6751daa`: fix(benchmarks): handle guard rejection of adversarial distractor turns in LongMemEval
- `b464306`: fix(benchmarks): set MESH_MAX_CONTENT_LENGTH to 131072 for long-turn LongMemEval questions
- `707933c`: feat(benchmarks): implement official 20-question LongMemEval-S pilot harness

---

## 3. Running Subagents & Worktrees
- None.

---

## 4. Measured Pilot v2 Summary (20 Official LongMemEval-S Questions, 9,782 turns)

| Tool / Interface | Class | Write Time (s/q) | Write Time (ms/turn) | Raw Query (ms) | Turn Recall@1 (95% CI) | Turn Recall@5 (95% CI) | Turn MRR (95% CI) | KI Gate Pass | LT Gate Pass | True Peak RAM | Data Disk | Model Disk |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Mesh SDK** | A | 80.80 s | 165.2 ms | 3,974.9 ms | 0.30 [0.10, 0.50] | 0.75 [0.55, 0.95] | 0.491 [0.338, 0.658] | 20/20 (100%) | 20/20 (100%) | **13.5 MB** | 17.1 MB | 195.7 MB |
| **Mem0 SDK** | A | 41.87 s | 85.6 ms | 533.5 ms | 0.35 [0.15, 0.55] | 0.80 [0.60, 0.95] | 0.514 [0.356, 0.672] | 20/20 (100%) | 20/20 (100%) | **112.2 MB** | 17.7 MB | 487.5 MB |
| **Mesh CLI (Daemon)**| B | 65.26 s | 133.4 ms | 2,917.0 ms | 0.30 [0.10, 0.50] | 0.75 [0.55, 0.95] | 0.491 [0.338, 0.658] | 20/20 (100%) | 20/20 (100%) | **434.7 MB** *(srv)*| 17.3 MB | 195.7 MB |
| **Mesh CLI (Cold)** | B | 66.80 s | 136.6 ms | 5,989.9 ms | 0.30 [0.10, 0.50] | 0.75 [0.55, 0.95] | 0.491 [0.338, 0.658] | 20/20 (100%) | 20/20 (100%) | **201.0 MB** | 17.2 MB | 195.7 MB |
| **Auxly CLI** | B | 0.67 s | 1.3 ms | 1,293.3 ms | 0.00 [0.00, 0.00] | 0.00 [0.00, 0.00] | 0.000 [0.000, 0.000] | 20/20 (100%) | 20/20 (100%) | **15.5 MB** | 0.5 MB | 0.0 MB |
| **Basic Memory CLI**| B | 463.63 s | 948.0 ms | 19,828.1 ms| 0.50 [0.30, 0.70] | 0.90 [0.75, 1.00] | 0.656 [0.498, 0.812] | 20/20 (100%) | 20/20 (100%) | **462.9 MB** | 13.8 MB | 451.6 MB |
| **Mesh MCP** | C | 79.57 s | 162.7 ms | 3,315.0 ms | 0.30 [0.10, 0.50] | 0.75 [0.55, 0.95] | 0.491 [0.338, 0.658] | 20/20 (100%) | 20/20 (100%) | **432.1 MB** | 17.1 MB | 195.7 MB |
| **Auxly MCP** | C | 0.61 s | 1.2 ms | 1,165.6 ms | 0.00 [0.00, 0.00] | 0.00 [0.00, 0.00] | 0.000 [0.000, 0.000] | 20/20 (100%) | 20/20 (100%) | **94.1 MB** | 0.5 MB | 0.0 MB |
| **Basic Memory MCP**| C | 407.89 s | 834.0 ms | 304.0 ms | 0.50 [0.30, 0.70] | 0.90 [0.75, 1.00] | 0.658 [0.500, 0.812] | 20/20 (100%) | 20/20 (100%) | **504.1 MB** | 13.8 MB | 451.6 MB |

---

## 5. Parity Tables Summary

### Mesh 4-Way Interface Parity
- **SDK vs CLI Daemon**: **20 / 20 questions identical (100.0%)** on top-5 ranked ID lists.
- **SDK vs CLI Cold**: **20 / 20 questions identical (100.0%)** on top-5 ranked ID lists.
- **SDK vs MCP**: **20 / 20 questions identical (100.0%)** on top-5 ranked ID lists.
- **Full Top-10 Parity**: 19 / 20 questions 100% identical. Question 11 exhibits a tie at rank 10 between `[turn:00395]` and `[turn:00140]` due to identical cosine scores; top 9 are identical across all 4 interfaces.
- **Metrics Parity**: Turn Recall@1 (0.30), Turn Recall@5 (0.75), Turn MRR (0.491) identical across all four Mesh interfaces.

### Basic Memory CLI vs MCP Parity
- **Turn Recall@1**: 0.50 (both CLI and MCP)
- **Turn Recall@5**: 0.90 (both CLI and MCP)
- **Turn MRR**: 0.656 (CLI) vs 0.658 (MCP)
- **Ranked Titles**: 11 / 20 questions byte-for-byte identical on full 10-item lists; the remaining 9 differ solely by internal rowid tie-swapping between items sharing identical reciprocal rank scores.

---

## 6. 500-Question Wall-Clock Projections (Pilot v2 Calibrated)

### Individual Tool Full 500-Question Matrix
| Evaluator | Class | Mean sec/q | Sequential (1w) | 2 Workers | 3 Workers |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Mesh SDK** | Class A | 84.78 s | 11.77 h (706.5 m) | 5.89 h (353.2 m) | 3.92 h (235.5 m) |
| **Mem0 SDK** | Class A | 42.41 s | 5.89 h (353.4 m) | 2.94 h (176.7 m) | 1.96 h (117.8 m) |
| **Mesh CLI (Daemon)**| Class B | 68.18 s | 9.47 h (568.1 m) | 4.73 h (284.1 m) | 3.16 h (189.4 m) |
| **Mesh CLI (Cold)** | Class B | 72.79 s | 10.11 h (606.6 m) | 5.05 h (303.3 m) | 3.37 h (202.2 m) |
| **Auxly CLI** | Class B | 1.96 s | 0.27 h (16.3 m) | 0.14 h (8.2 m) | 0.09 h (5.4 m) |
| **Basic Memory CLI**| Class B | 483.46 s | 67.15 h (4028.8 m)| 33.57 h (2014.4 m)| 22.38 h (1342.9 m)|
| **Mesh MCP** | Class C | 82.88 s | 11.51 h (690.7 m) | 5.76 h (345.3 m) | 3.84 h (230.2 m) |
| **Auxly MCP** | Class C | 1.78 s | 0.25 h (14.8 m) | 0.12 h (7.4 m) | 0.08 h (4.9 m) |
| **Basic Memory MCP**| Class C | 408.19 s | 56.69 h (3401.6 m)| 28.35 h (1700.8 m)| 18.90 h (1133.9 m)|

### STEP 4 Reduced Matrix (PASS 1 & PASS 2 Options)
- **PASS 1 Primary 4-Tool Matrix** (Mesh SDK + Mem0 SDK + Auxly MCP + Basic Memory MCP):
  - *Sequential (1 worker):* 74.60 hours (~4,476 minutes) — dominated by Basic Memory MCP (56.7 h).
  - *2 Parallel Workers:* **37.30 hours** (~2,238 minutes).
- **Fast 3-Tool Matrix (Excluding Basic Memory bulk reindex)** (Mesh SDK + Mem0 SDK + Auxly MCP):
  - *Sequential (1 worker):* 17.91 hours (~1,074 minutes).
  - *2 Parallel Workers:* **8.95 hours** (~537 minutes).
  - *3 Parallel Workers:* **5.97 hours** (~358 minutes).

---

## 7. Next Action
1. Deliver CHECKPOINT B2 report and STOP for user review.
2. Await explicit user authorization before starting STEP 4.
