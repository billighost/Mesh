# Phase 7 Benchmark Plan & Preregistration Protocol

**Document Version:** 1.0.0  
**Mesh Target Commit:** `9a21be1` (`feat/ci-evidence`)  
**Evaluation Date:** October 26, 2026  
**Primary Public Benchmark:** LongMemEval (MIT License)  
**Private Benchmark (Non-Redistributable):** LoCoMo (CC BY-NC 4.0, internal evaluation only)

---

## 1. Competitor Provenance & Evidence (Item B.1)

All competitor tools are evaluated at explicit, immutable versions with verified cryptographic checksums and install logs.

| Tool | Version | Source / Release Page | Retrieval Date | Package Type | SHA-256 Digest |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Mesh** | `0.8.3` | Git commit `9a21be1` | 2026-10-26 | Source / Wheel | Local build |
| **Mem0** | `2.2.1` | [PyPI Release](https://pypi.org/project/mem0ai/2.2.1/) | 2026-10-26 | Wheel (`py3-none-any`) | `fe91bb91ac8926231993a4aa58df00a60c6c74c709e6a338fe399500776eef4d` |
| **Basic Memory** | `0.24.1` | [PyPI Release](https://pypi.org/project/basic-memory/0.24.1/) | 2026-10-26 | Wheel (`py3-none-any`) | `b528fff0a92bc91683e791553f9b10f57f7e3185a1aa15d494f5b8294eaad89e` |
| **Auxly CLI** | `1.5.0` | [GitHub Release](https://github.com/Tzamun-Arabia-IT-Co/auxly-memory-cli/releases/tag/v1.5.0) | 2026-10-26 | Go binary (Windows amd64) | `720feb62b795ddaf027ca35d758caa4d261f3c5f8169eedc4a1c1e2f9bdf9d55` |

### Platform Compatibility Note: Python 3.14 & `litellm`
- `basic-memory 0.24.1` declares dependency `litellm<1.92.0,>=1.91.5`. On PyPI, `litellm 1.91.5` explicitly declares `Requires-Python: <3.14,>=3.10`.
- On Python 3.14.0 GA, standard `pip install basic-memory==0.24.1` fails during resolution with `ERROR: No matching distribution found for litellm<1.92.0,>=1.91.5`.
- For Python 3.14 execution, `basic-memory` core modules (`basic_memory.cli`, `basic_memory.indexing`) are driven with runtime dependencies (`loguru`, `typer`, `alembic`, `pydantic-settings`, `fastembed`, `aiosqlite`, `python-frontmatter`) installed, leaving `litellm` uninvoked since benchmark ingestion and retrieval are local and do not route through cloud LLM gateways.

---

## 2. Ingest Architecture & Model Harmonization (Items B.2, B.3)

### Mem0 Ingest Configuration
- **LLM Extraction OFF:** To ensure an apples-to-apples comparison with Mesh (which performs zero LLM parsing at ingest and zero LLM calls at retrieval), Mem0 is driven with `infer=False` (`Memory.add(..., infer=False)`). No 7B Ollama model or external LLM is invoked during main benchmark ingest.
- **Local Embedder:** Configured with `sentence-transformers/all-MiniLM-L6-v2` via `fastembed`/`sentence_transformers`.
- **Optional Variant:** A small sample (e.g. 5 questions) may optionally be evaluated with LLM extraction enabled, presented in an isolated appendix table with explicit cost and throughput disclosures.

### Embedding Models
- **Mesh:** Local ONNX Runtime `all-MiniLM-L6-v2` (384-dimensional vector space, cosine distance, SIMD-accelerated).
- **Mem0:** Local `all-MiniLM-L6-v2` (384-dimensional vector space, Qdrant / in-memory vector store).
- **Basic Memory:** Local fastembed `all-MiniLM-L6-v2` (384-dimensional vector space, SQLite).
- **Auxly CLI:** **Keyword-only (BM25 / Fuzzy FTS)**. Auxly uses file-based markdown vaults and Ripgrep/Fuzzy lexical ranking; it does not compute neural embeddings. Labeled explicitly as *Keyword-only*.

---

## 3. Evaluation Methodology & Metric Definitions (Item B.4)

### Core Unit Definitions
1. **Memory Unit:** A discrete dialogue turn or factual assertion. In LongMemEval, each turn in a user/assistant multi-session history is ingested as a distinct memory record (`{question_id}_{session_idx}_{turn_idx}`).
2. **Gold Evidence Mapping:** Turns in `haystack_sessions` with `"has_answer": True` represent the gold evidence. A query retrieval is considered a **hit** if any returned memory in top-$k$ matches a gold evidence memory ID or has $\ge 80\%$ token overlap with the gold assertion.
3. **Retrieval Rank ($k$):** Metrics are computed for $k \in \{1, 5, 10\}$.
   - **Recall@k:** $\mathbb{I}(\text{at least one gold memory in top-}k)$.
   - **MRR@k:** $1 / \text{rank}$ of the highest-ranked gold memory in top-$k$ ($0$ if none in top-$k$).
   - **Precision@k:** $(\text{count of gold memories in top-}k) / k$.

### Interface Classes (Reported in Separate Tables)
To avoid conflating IPC/subprocess startup overhead with algorithmic retrieval latency, results are segregated into two interface classes:
- **Class A (In-Process Library API):**
  - Mesh Python SDK (`mesh.Mesh`: `learn()`, `recall()`)
  - Mem0 Python SDK (`mem0.Memory`: `add(..., infer=False)`, `search()`)
- **Class B (CLI Subprocess / External IPC):**
  - Mesh CLI (`mesh learn`, `mesh recall`)
  - Basic Memory CLI (`basic-memory add/write`, `basic-memory grep`)
  - Auxly CLI (`auxly.exe note`, `auxly.exe search`)

---

## 4. Workload Estimates & Seed Subsets (Item B.5)

- **Fixed Deterministic Seed:** `seed = 42` applied across all dataset subsampling, memory ordering, and query replay.
- **Estimated Runtimes:**
  - *Smoke Test (~50 questions, ~1,300 memories):*
    - Mesh: ~15 seconds ingest, ~2 seconds recall.
    - Mem0 (`infer=False`): ~25 seconds ingest, ~4 seconds recall.
    - Auxly: ~10 seconds ingest, ~2 seconds recall.
    - Basic Memory: ~30 seconds ingest, ~5 seconds recall.
    - Total smoke test wall-clock time: < 3 minutes.
  - *Full LongMemEval-S (500 questions, ~15k memories):*
    - Mesh: ~3 minutes ingest, ~30 seconds recall.
    - Mem0 (`infer=False`): ~6 minutes ingest, ~1 minute recall.
    - Auxly: ~2 minutes ingest, ~30 seconds recall.
    - Basic Memory: ~7 minutes ingest, ~1.5 minutes recall.
- **Timeout Policy:** If any tool fails to complete within 4 hours for a given scale size, execution is terminated and reported as `"did not finish within 4 hours"` alongside terminal stderr logs.

---

## 5. Machine Controls & Environmental Parity (Item B.7)

1. **Hardware Power State:** AC mains power connected, Windows High Performance power plan active (`powercfg /s 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c` or equivalent).
2. **Process Isolation:** All background user applications (browsers, IDE dev servers, indexing agents) closed.
3. **File System Exclusions:** Antivirus scanning excluded from repository, virtual environment paths, and temporary test directories (`TEMP`, `MESH_HOME`).
4. **Interpreter Version:** Identical Python 3.14.0 binary across all Python virtual environments.
5. **Warm-up Discard:** First 2 queries per benchmark run discarded as warm-up to ensure ONNX runtime kernels and SQLite page caches reach steady state.
6. **Linux Block:** Dedicated Linux run block executed via Ubuntu 22.04 CI runner / WSL2, explicitly flagged in results as *Noisy shared hardware (cloud virtualization)*.

---

## 6. Data Governance & Licensing (Item B.8)

1. **LongMemEval:** MIT License. Dataset files stored under `benchmarks/data/` for public reproducibility.
2. **LoCoMo:** Creative Commons Attribution-NonCommercial 4.0 International (`CC BY-NC 4.0`).
   - In strict adherence to Section 1(i) and Section 2:
     - No raw LoCoMo dataset files will be committed or redistributed within the Mesh repository.
     - Zero LoCoMo metrics will appear in `README.md`, `pyproject.toml`, PyPI descriptions, or project documentation until explicit authorization is granted.
     - LoCoMo evaluation is restricted to private internal verification scripts.

---

## 7. Maintainer Pre-Publication Review (Item B.9)

Before any benchmark results are published externally (e.g. in `RESULTS.md`, blog posts, or release announcements):
1. The exact invocation script, configuration JSON, and environment manifest will be compiled into a peer review packet.
2. Review packets will be sent to the respective maintainers:
   - Mem0: `hello@mem0.ai` / GitHub repository issues.
   - Basic Memory: `basicmachines-co` GitHub repository issues.
   - Auxly: `Tzamun-Arabia-IT-Co` GitHub repository issues.
3. Maintainers will be given 14 calendar days to review the harness configuration and suggest parity adjustments before public release.

---

## 8. Smoke Test Execution Protocol (Item B.10)

- **Corpus:** 50 LongMemEval questions (`benchmarks/data/longmemeval_smoke_50.json`), comprising 1,319 total memories and 107 gold answers.
- **Scope:**
  - 1,000-memory ingest scale test (throughput, memory RSS delta, storage footprint).
  - 50-question retrieval evaluation (Recall@1, Recall@5, Recall@10, MRR).
- **Execution:** Run via `benchmarks/run_smoke_test.py`.
- **Termination:** STOP immediately following smoke test completion. Do NOT start the full benchmark run. Output raw JSON and summary logs for user review.
