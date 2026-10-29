# Benchmarks State & Tracking

**Document Version:** 1.9.0  
**Current Step:** CHECKPOINT P1-A REACHED (Step 1 Fixes F1–F7 Complete, Code Frozen, Workflow Ready, Awaiting User Review)  
**Target Commit / Base:** `d3ada72` (`feat/ci-evidence`)  
**Frozen Benchmark Tag:** `benchmark-v1`  
**Timestamp:** 2026-10-30T00:05:00+01:00  

---

## 1. Current Step: CHECKPOINT P1-A REACHED (Pre-CI Validation & Step 1 Fixes Complete)
- **Status:** All STEP 1 prerequisites (F1 through F7) have been thoroughly investigated, implemented, tested, and committed with green test suites. The code freeze is prepared and the Pass 1 GitHub Actions workflow `.github/workflows/bench_pass1.yml` has been authored and pinned to immutable commit SHAs.
- **Rule Enforced:** Main agent stops at CHECKPOINT P1-A and awaits explicit user authorization before launching any CI runs on GitHub.

### Step 1 Resolution & Audit Details (F1 – F7)

#### F1. Guard Parity & MCP/Daemon Accounting
- **Root Cause Analysis:** Investigation of the pilot prompt-injection distractor turn (Q3) confirmed that `mesh/guards.py` and `mesh/mcp_server.py` correctly ran `guard_ingest` on the MCP path and returned `status: "rejected"` with `isError=True` in `CallToolResult`. The harness in `benchmarks/run_pilot.py` was at fault: it caught exceptions via `try...except Exception`, but `call_tool` returns a result object rather than raising an exception. Consequently, rejections were treated as successful tool calls and counted under `ingested`. Similarly, for the daemon path, `_daemon_request` returned `{"_http_error": 400, "detail": ...}` when HTTP 400 was returned by the server, but the harness looked specifically for `resp.get("status") == "error"`, causing guard rejections to fall through to `failed += 1` with an "unknown daemon failure" label.
- **Resolution:**
  1. Added unit test `test_mcp_learn_guard_rejection_isError` in `tests/test_phase1.py` (commit `6b267c4`) confirming that MCP `mesh_learn` tool calls return `isError=True` and JSON `{"status": "rejected", "error": ...}` when content is rejected.
  2. Fixed harness ingestion loop in `benchmarks/run_pilot.py` (commit `9ea16f9`): parses `CallToolResult` JSON payload, inspects `result.isError`, and maps `payload.get("status") == "rejected"` to `rejected` count.
  3. Fixed daemon accounting in `benchmarks/run_pilot.py` (commit `9ea16f9`): inspects `_http_error == 400` and extracts rejection details.

#### F2. Root Cause Analysis: Pilot v1 Artifacts
- **MCP R@1 = 0.90 Artifact:** In Pilot v1, `eval_mesh_mcp_question_async` in `benchmarks/run_pilot.py` (introduced in commit `707933c`) captured raw MCP stdout text and assigned `raw_cands = [("mcp_raw", raw_text)]` at rank 1 instead of parsing individual result items from the response. Because all 10 retrieved candidates were lumped into a single candidate string, any query where the gold turn appeared anywhere in the top 10 was scored as an immediate hit at Rank 1 (yielding R@1 = 0.90, R@5 = 0.90, MRR = 0.90). This was resolved in commit `bc93c4f` by parsing the JSON/markdown block output into distinct candidate tuples.
- **Cold CLI R@1 = 0.40 vs SDK 0.30:** In Pilot v1, pre-flight gate checks (known-item and long-turn queries) were executed against the active namespace prior to the benchmark evaluation queries without database snapshotting. Each pre-flight query modified memory access counters and access timestamps, introducing ranking drift. In addition, `MESH_CLI_FORCE_ONNX=1` was omitted in subprocess calls. Both were resolved in commit `bc93c4f` by taking isolated database snapshots (`snap_ki`, `snap_lt`, `snap_raw`, `snap_kw`) immediately post-ingestion and enforcing `MESH_CLI_FORCE_ONNX=1`.
- **Smoke-Test Table Validity:** Inspection of `benchmarks/run_smoke_test.py` confirmed that `run_class_c_mesh_mcp` had always parsed `data.get("results", [])` individually into separate candidate tuples. The candidate-lumping bug was isolated entirely to the pilot harness in `707933c` and never existed in `run_smoke_test.py`. Therefore, all earlier smoke-test tables remain completely valid.

#### F3. Default-Configuration Check & 500-Question Pre-Scan
- **Pre-Scan Execution:** Executed full pre-scan across all 500 questions in `benchmarks/data/longmemeval_s_cleaned.json` (246,750 total turns) under default configuration (`MESH_MAX_CONTENT_LENGTH=16384`) without ingestion.
- **Results:**
  - Total Questions: 500
  - Total Turns: 246,750
  - Turns > 16,384 characters: 12 turns (distractor turns only).
  - Turns rejected by default guards: 115 turns (82 length rejections, 33 prompt injection/adversarial pattern rejections).
  - Questions containing affected turns: 99 / 500 (19.8%).
  - **Gold Turns Affected:** **EXACTLY 0** (0 / 500). Not a single gold turn exceeds 16,384 characters or triggers an adversarial guard rejection.
  - Pre-scan results saved to `benchmarks/results/prescan_500_default_config.json` (49.5 KB). In PASS 1, Mesh SDK will additionally be evaluated under all-default settings on the 99 affected questions as a separate comparison row.

#### F4. Deterministic Tie-Breaking
- **Root Cause:** Pilot Q11 observed two memories with identical cosine similarities (rounded to 4 decimal places) at rank 10 (`[turn:00395]` and `[turn:00140]`), causing nondeterministic ordering across interfaces depending on SQLite row traversal order.
- **Resolution:** In `mesh/store.py` (commit `5a15c49`), candidate sorting was updated to:
  `scored_candidates.sort(key=lambda x: (-x["similarity"], -x["lexical_score"], -x["semantic_similarity"], x["id"]))`
  Ascending string sort on `x["id"]` deterministically breaks exact ties across all three scoring components without altering non-tied rankings. Verified by `test_score_tie_break_deterministic` and `test_exact_float_tie_break_by_id` in `tests/test_retrieval_quality.py`.

#### F5. Auxly Capabilities, Rules & Variants
- **Local Embedding Audit:** Reverse engineering and binary inspection of `auxly-linux-amd64` and `auxly.exe` (v1.5.0, 20.9 MB) confirmed that Auxly contains no built-in embedding model or native ONNX/FastEmbed runtime. It integrates with external daemons (`Ollama` at `http://localhost:11434`) or cloud APIs (`text-embedding-3-small` via `OPENAI_API_KEY`/`GEMINI_API_KEY`). When neither is configured, Auxly defaults to exact contiguous substring matching over vault markdown files. Because no external local Ollama daemon is provisioned and cloud APIs are prohibited, Auxly is correctly labeled: `"substring search, no embedding model"`.
- **Cloud Rule Citation:** Source of the "Rule 4" constraint prohibiting cloud LLM calls in benchmarks is `AGENTS.md` Ground Rules line 14:
  `4. **Never call a cloud LLM provider** in tests or examples. Do not use --yes or MESH_AUTO_APPROVE=1 where a cloud path is reachable.`
- **Keyword-Query Variant:** Documented deterministic keyword extractor `extract_keywords()` extracts the rarest content words (filtering `STOP_WORDS`) and runs identically across all tools as a separate reporting column alongside raw queries. Auxly agent trust level is set to direct write (`auto`) with `auxly_pending_list` confirmed empty.

#### F6. Turn Counts Reconciliation
- **Reconciliation:** The 9,782 turn count reported in Pilot v2 represents raw dataset haystack turns evaluated across the 20 pilot questions (`sum(q["total_haystack_turns"])`). The 9,822 figure from earlier Checkpoint B counting included the 2 synthetic gate items per question (1 known-item turn + 1 long-turn gate turn × 20 questions = 40 synthetic turns; 9,782 + 40 = 9,822). Gate items are validated independently and excluded from retrieval metrics.

#### F7. Model Safety & ONNX Enforcement
- **Enforcement:** Set `MESH_REQUIRE_MODEL=1` and `MESH_AUTO_DOWNLOAD_MODEL=1` at module level in `benchmarks/run_pilot.py` and in `.github/workflows/bench_pass1.yml`.
- **Assertions:** Fixed `embedding_backend` resolution in `eval_mesh_cli_daemon_question` and `eval_mesh_cli_cold_question` to inspect result candidate metadata, and asserted `assert backend in ("onnx-minilm-l6-v2", "onnx")` on every result line across SDK, Daemon, Cold CLI, and MCP. A silent fallback to `hash-384` immediately halts execution with `RuntimeError`.

---

## 2. Completed Items & Commits
- `d3ada72`: ci(benchmarks): add Pass 1 LongMemEval accuracy benchmark workflow on GitHub Actions
- `9ea16f9`: fix(benchmarks): handle MCP/daemon guard rejections, enforce ONNX model safety, and add dataset pre-scan
- `6b267c4`: test(mcp): verify mesh_learn guard rejection returns isError and status rejected
- `5a15c49`: fix(mesh): tie-break candidate ranking deterministically by memory ID
- `a9dd993`: docs(benchmarks): update STATE.md to v1.8.0 and record Pilot v2 results at Checkpoint B2
- `fedf7b1`: fix(benchmarks): use stable turn identifier for ranked parity and wrap daemon in try/finally
- `bc93c4f`: fix(benchmarks): implement Checkpoint B review fixes for Pilot v2
- `837876c`: docs(benchmarks): update STATE.md to v1.7.1 noting Checkpoint B approval in principle
- `6988c89`: docs(benchmarks): update STATE.md to v1.7.0 and record 20-question pilot results at Checkpoint B

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
- **Full Top-10 Parity**: 20 / 20 questions 100% identical post-F4 deterministic tie-break fix (`5a15c49`).
- **Metrics Parity**: Turn Recall@1 (0.30), Turn Recall@5 (0.75), Turn MRR (0.491) identical across all four Mesh interfaces.

---

## 6. GitHub Actions Workflow Configuration (.github/workflows/bench_pass1.yml)
- **Runner OS:** `ubuntu-latest` (GitHub-hosted only, per Rule 7).
- **Triggers:** `workflow_dispatch` only (manual invocation; no automatic push/PR triggers).
- **Default Permissions:** `contents: read` (strict least-privilege).
- **Action Pins (Full Commit SHAs):**
  - `actions/checkout@11d5960a326750d5838078e36cf38b85af677262` (v4)
  - `actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065` (v5)
  - `actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830` (v4)
  - `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02` (v4)
- **Dataset Source:** Official Hugging Face `xiaowu0162/longmemeval-cleaned` (`longmemeval_s_cleaned.json`, 277,383,467 bytes, SHA-256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`).
- **Auxly Linux Binary:** `auxly-linux-amd64` v1.5.0, SHA-256 `ec452fc983e862f78f4cf23d41d3e25396ebff974508aceb45e8224720759b04`.
- **Pre-Flight Suite:** Runs `ruff check .`, `check_skill.py`, and `check_docs.py` before execution.

---

## 7. Next Action
1. Present CHECKPOINT P1-A report and STOP for user review.
2. Await explicit user authorization before initiating Step 2 CI sample-shard test.
