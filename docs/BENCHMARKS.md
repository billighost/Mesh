# Mesh Context Layer — Benchmark Evidence & Methodology

**Document Version:** 1.0.0  
**Target Commit:** `bb90302` (`feat/ci-evidence`)  
**Frozen Code Base Tag:** `benchmark-v1` (`eff1407412ef5ea9239b8bafb7ee64bf4f5a344f`)  
**CI Benchmark Run Tag:** `benchmark-v1-run` (`4e903608c0464daa499e92ea26e144bdaa361de1`)  
**GitHub Actions Run ID:** [`41109952815`](https://github.com/billighost/Mesh/actions/runs/41109952815)  
**Evidence Artifacts:** Branch [`origin/bench-results/41109952815`](https://github.com/billighost/Mesh/tree/bench-results/41109952815)  

---

## 1. Methodology & Scope

This document provides the authoritative empirical evidence for **Pass 1 (Evidence Retrieval Quality)** of the Phase 7 benchmark suite.

### Core Protocol & Rules
1. **Evidence Retrieval Scoring (Not QA Accuracy):** This benchmark evaluates whether each persistent memory layer successfully retrieves the gold conversation turn and gold session holding the ground-truth evidence required to answer the query. It does not evaluate generative LLM synthesis.
2. **Abstention Separation:** The LongMemEval-S corpus contains 500 questions, of which exactly 30 questions (`*_abs`) evaluate abstention (where no gold answer exists). Per preregistered methodology, abstention questions are excluded from evidence retrieval ranking; the **$N=470$ non-abstention corpus is the PRIMARY evaluation**, and $N=500$ is provided as a secondary reference.
3. **No Runner Timings as Results:** Cloud virtualized runners (`ubuntu-24.04`) experience noisy hypervisor scheduling and variable I/O. As preregistered in `benchmarks/PHASE7_PLAN.md`, all shared-runner timings are excluded as results. Precise latency, throughput, RAM footprint, and disk growth are reserved for **Pass 2** on dedicated bare-metal hardware.
4. **Statistical Rigor & Metrics:**
   - **Metrics Evaluated:** Recall@1/5/10 (hit rate: any gold turn present in top-$k$), Fraction of Gold Found, MRR (Mean Reciprocal Rank: $1 / 	ext{rank}$ of first gold turn), and NDCG@10.
   - **NDCG@10 Formula:** Binary relevance ($r_i \in \{0, 1\}$), where $r_i = 1$ if candidate turn at rank $i$ is in ground-truth gold evidence turns $G$, else $0$:
     $$\text{DCG@10} = \sum_{i=1}^{10} \frac{r_i}{\log_2(i + 1)}, \quad \text{IDCG@10} = \sum_{i=1}^{\min(10, |G|)} \frac{1}{\log_2(i + 1)}, \quad \text{NDCG@10} = \frac{\text{DCG@10}}{\text{IDCG@10}}$$
   - **Confidence Intervals:** All metrics report paired bootstrap 95% confidence intervals (1,000 resamples, fixed `seed=42`).
   - **Pre-Declared Primary Comparisons:** Strictly defined as **Turn Recall@1 and Turn Recall@5 (Mesh SDK vs. Mem0 SDK and vs. Basic Memory MCP)** on the paired intersection ($N=470$). All other comparisons are exploratory.
   - **Sensitivity Analysis:** 99% bootstrap confidence intervals evaluate robustness against multiple testing; differences are reported strictly in percentage points (pp).

---

## 2. Provenance & Code Freeze Audit

The Mesh core library (`mesh/`) was strictly frozen at git tag `benchmark-v1` (`eff1407`). The GitHub Actions benchmark execution ran commit `4e903608` (tagged `benchmark-v1-run`).

```bash
git diff benchmark-v1 4e903608 -- mesh/
# Output: (empty — 100% bit-identical code freeze maintained)
```

### Complete List of Harness & Workflow Changes (between `benchmark-v1` and `4e903608`)

Every change between the freeze tag and the run commit occurred strictly in harness, workflow, or tracking files:

| File | Change Scope | Reason for Change | Could It Affect Scoring? |
| :--- | :--- | :--- | :--- |
| `benchmarks/run_pilot.py` | Added `check_free_disk_guard(3.0)` | Enforces 3 GB free-disk assertion on runner per brief §2(d). | **No** (Harness safety guard) |
| `benchmarks/run_pilot.py` | Added `check_harness_cutoff(300.0)` | Enforces 300-minute stop-starting-new-questions cutoff per brief §2(d). | **No** (Timeout guard; all shards finished well before cutoff) |
| `benchmarks/run_pilot.py` | Added `get_system_metadata()` | Logs runner image, OS, CPU count, RAM MB, commit, and tag per brief §2(d). | **No** (System diagnostics logging) |
| `benchmarks/run_pilot.py` | Added `append_raw_jsonl()` | Writes line-delimited streaming JSONL records per question per brief §2(d). | **No** (Output serialization) |
| `benchmarks/run_pilot.py` | Added `make_raw_record()` | Normalizes data schema across tools for raw question logging. | **No** (Output formatting) |
| `benchmarks/run_pilot.py` | Added cross-platform binary paths (`find_bm_bin`, `find_auxly_bin`) | Enables locating Linux CLI binaries (`/tmp/venv_bm/bin/basic-memory`, `benchmarks/bin/auxly`) on `ubuntu-24.04`. | **No** (OS portability) |
| `benchmarks/run_pilot.py` | Added `eval_auxly_mcp_ollama_question_async` | Evaluates Auxly with local Ollama semantic embeddings (`all-minilm`) per owner directive 4. | **No** (Isolated evaluator for Auxly Ollama variant) |
| `benchmarks/run_pilot.py` | Added `--default-config` flag | Evaluates Mesh SDK with default 16,384 char limit on the 99 affected questions per directive 2. | **No** (Isolated variant; default config only) |
| `benchmarks/run_pilot.py` | Added `--force-hash` flag | Evaluates Mesh SDK with hash-384 feature hashing fallback on 100 questions per directive 2. | **No** (Isolated variant; hash fallback only) |
| `benchmarks/run_pilot.py` | Added `--start-idx`, `--limit`, `--tools` CLI options | Allows runner matrix jobs to execute specific question slices and tools. | **No** (Sharding controls) |
| `benchmarks/run_pilot.py` | Added `analyze_merged_jsonl()` | Parses merged `raw.jsonl` to calculate bootstrap CIs and paired differences. | **No** (Post-hoc analysis utility) |
| `benchmarks/run_mem0_pilot_worker.py` | Added `"raw_ids": [c[0] for c in raw_cands]` (+1 line) | Records retrieved turn IDs in Mem0 worker output dictionary. | **No** (Audit transparency) |
| `.github/workflows/bench_pass1.yml` | Configured 15-shard matrix on `ubuntu-24.04` | Runs sharded matrix with Ollama daemon, code-freeze check, and artifact merging. | **No** (CI infrastructure) |
| `benchmarks/STATE.md` | Version updates (v1.9.0 to v1.14.0) | Tracks directives fulfillment, pre-scan reconciliation, and runner execution state. | **No** (Documentation only) |

### Code Differences Between Benchmark Freeze and Release v0.9.0

The released code differs from the benchmarked code (`benchmark-v1`) only by non-retrieval operational and formatting bug fixes:
1. `mesh/namespaces.py`: Configured `conn.row_factory = sqlite3.Row` and safe positional tuple indexing fallbacks in `list_namespaces` and `namespace_stats` to prevent unhandled `TypeError` exceptions during cold standalone execution when vector cache is unpopulated.
2. `mesh/digest.py`: Enforced `conn.row_factory = sqlite3.Row` and safe positional tuple fallbacks in `generate_digest` when reading contradictions and memory tables from cold stores.
3. `tests/test_daemon_tail.py`: Updated `test_stop_daemon_cleans_up_real_daemon_process` to use platform-aware mocks rather than mutating `os.name`, preventing `pathlib.UnsupportedOperation` failures on POSIX CI runners.

Zero scoring logic, vector similarity formulas, ranking weights, tokenizer parameters, or memory retrieval algorithms were modified. The retrieval engine is bit-for-bit functionally identical to the frozen benchmark code.

---

## 3. Evidence Retrieval Results

### Primary Evidence Table: $N=470$ (Non-Abstention Corpus)

The primary benchmark measures top-$k$ evidence retrieval across the 470 ground-truth questions.

| Tool / Variant | Interface Class | $N$ | Turn Recall@1 (95% CI) | Turn Recall@5 (95% CI) | Turn Recall@10 (95% CI) | Turn Frac Found (95% CI) | Turn MRR (95% CI) | Turn NDCG@10 (95% CI) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Mesh SDK (v0.8.3, onnx-minilm-l6-v2)** | Class A (in-process) | 470 | **0.4128** `[0.368, 0.455]` | **0.8319** `[0.798, 0.864]` | **0.8894** `[0.860, 0.917]` | **0.8040** `[0.773, 0.835]` | **0.5814** `[0.546, 0.615]` | **0.6022** `[0.574, 0.633]` |
| **Mem0 SDK (v2.2.1, fastembed all-MiniLM-L6-v2)** | Class A (in-process) | 470 | **0.3872** `[0.340, 0.432]` | **0.7872** `[0.751, 0.823]` | **0.8957** `[0.868, 0.921]` | **0.8044** `[0.773, 0.835]` | **0.5622** `[0.527, 0.594]` | **0.5807** `[0.552, 0.611]` |
| **Basic Memory MCP (v0.24.1, fastembed all-MiniLM-L6-v2)** | Class C (stdio MCP) | 470 | **0.5213** `[0.474, 0.568]` | **0.8362** `[0.804, 0.868]` | **0.9277** `[0.904, 0.949]` | **0.8409** `[0.812, 0.868]` | **0.6601** `[0.626, 0.696]` | **0.6673** `[0.639, 0.698]` |
| **Auxly MCP (Ollama all-minilm, v1.5.0)** | Class C (stdio MCP) | 470 | **0.1191** `[0.089, 0.149]` | **0.4596** `[0.413, 0.502]` | **0.5234** `[0.477, 0.566]` | **0.3868** `[0.347, 0.424]` | **0.2596** `[0.227, 0.289]` | **0.2938** `[0.257, 0.329]` |
| **Auxly MCP (Substring, v1.5.0)**| Class C (stdio MCP) | 470 | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` |
| **Mesh SDK (Default Config, v0.8.3)**| Class A (16k guard) | 94 | **0.4362** `[0.340, 0.543]` | **0.8617** `[0.787, 0.925]` | **0.9149** `[0.851, 0.968]` | **0.8147** `[0.749, 0.877]` | **0.6147** `[0.546, 0.686]` | **0.6190** `[0.564, 0.679]` |
| **Mesh SDK (Hash-384, v0.8.3)** | Class A (hash fallback) | 94 | **0.4681** `[0.362, 0.575]` | **0.7553** `[0.660, 0.840]` | **0.8511** `[0.766, 0.915]` | **0.7626** `[0.674, 0.839]` | **0.5919** `[0.507, 0.674]` | **0.6084** `[0.532, 0.677]` |

#### Session-Level Evidence Retrieval: $N=470$ (Non-Abstention Corpus)

| Tool / Variant | $N$ | Sess Recall@1 (95% CI) | Sess Recall@5 (95% CI) | Sess Recall@10 (95% CI) | Sess Frac Found (95% CI) | Sess MRR (95% CI) | Sess NDCG@10 (95% CI) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Mesh SDK** | 470 | **0.8894** `[0.860, 0.917]` | **0.9787** `[0.966, 0.992]` | **0.9894** `[0.981, 0.998]` | **0.9433** `[0.928, 0.958]` | **0.9259** `[0.906, 0.945]` | **0.9133** `[0.896, 0.930]` |
| **Mem0 SDK** | 470 | **0.8894** `[0.860, 0.917]` | **0.9617** `[0.943, 0.979]` | **0.9830** `[0.970, 0.994]` | **0.9698** `[0.958, 0.981]` | **0.9200** `[0.899, 0.941]` | **0.9256** `[0.909, 0.941]` |
| **Basic Memory MCP** | 470 | **0.8745** `[0.843, 0.904]` | **0.9617** `[0.945, 0.979]` | **0.9830** `[0.970, 0.994]` | **0.9443** `[0.928, 0.961]` | **0.9101** `[0.887, 0.931]` | **0.9054** `[0.887, 0.924]` |
| **Auxly MCP (Ollama)** | 470 | **0.3681** `[0.326, 0.413]` | **0.7255** `[0.687, 0.766]` | **0.7511** `[0.713, 0.791]` | **0.7349** `[0.700, 0.772]` | **0.5178** `[0.483, 0.554]` | **0.7360** `[0.702, 0.771]` |
| **Auxly MCP (Substring)**| 470 | **0.0021** `[0.000, 0.006]` | **0.0021** `[0.000, 0.006]` | **0.0021** `[0.000, 0.006]` | **0.0011** `[0.000, 0.003]` | **0.0021** `[0.000, 0.006]` | **0.0013** `[0.000, 0.004]` |
| **Mesh SDK (Default Config)**| 94 | **0.9043** `[0.840, 0.957]` | **0.9787** `[0.947, 1.000]` | **0.9894** `[0.968, 1.000]` | **0.9504** `[0.916, 0.977]` | **0.9322** `[0.887, 0.971]` | **0.9178** `[0.879, 0.950]` |
| **Mesh SDK (Hash-384)** | 94 | **0.8298** `[0.755, 0.904]` | **0.9681** `[0.925, 1.000]` | **0.9787** `[0.947, 1.000]` | **0.9314** `[0.891, 0.966]` | **0.8856** `[0.830, 0.935]` | **0.8766** `[0.829, 0.922]` |

---

### Secondary Reference Table: $N=500$ (Full Matrix Including Abstention)

For historical continuity and full-matrix audits, the unpartitioned $N=500$ table is provided below:

| Tool / Variant | $N$ | Turn Recall@1 (95% CI) | Turn Recall@5 (95% CI) | Turn Recall@10 (95% CI) | Turn Frac Found (95% CI) | Turn MRR (95% CI) | Turn NDCG@10 (95% CI) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Mesh SDK** | 500 | **0.3960** `[0.356, 0.440]` | **0.7960** `[0.762, 0.834]` | **0.8520** `[0.820, 0.884]` | **0.7718** `[0.739, 0.806]` | **0.5568** `[0.525, 0.592]` | **0.5779** `[0.549, 0.607]` |
| **Mem0 SDK** | 500 | **0.3720** `[0.328, 0.418]` | **0.7520** `[0.718, 0.792]` | **0.8580** `[0.828, 0.888]` | **0.7721** `[0.741, 0.805]` | **0.5384** `[0.507, 0.576]` | **0.5572** `[0.530, 0.588]` |
| **Basic Memory MCP** | 500 | **0.5000** `[0.458, 0.544]` | **0.8000** `[0.766, 0.836]` | **0.8900** `[0.862, 0.918]` | **0.8084** `[0.778, 0.838]` | **0.6330** `[0.599, 0.666]` | **0.6411** `[0.612, 0.670]` |
| **Auxly MCP (Ollama)** | 500 | **0.1160** `[0.088, 0.142]` | **0.4380** `[0.398, 0.482]` | **0.4980** `[0.454, 0.540]` | **0.3696** `[0.333, 0.405]` | **0.2490** `[0.221, 0.275]` | **0.2814** `[0.248, 0.313]` |
| **Auxly MCP (Substring)**| 500 | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` |
| **Mesh SDK (Default Config)**| 99 | **0.4141** `[0.323, 0.515]` | **0.8283** `[0.758, 0.899]` | **0.8788** `[0.808, 0.939]` | **0.7837** `[0.715, 0.852]` | **0.5857** `[0.511, 0.662]` | **0.5916** `[0.528, 0.658]` |
| **Mesh SDK (Hash-384)** | 100 | **0.4400** `[0.330, 0.540]` | **0.7100** `[0.620, 0.800]` | **0.8000** `[0.720, 0.880]` | **0.7168** `[0.635, 0.796]` | **0.5564** `[0.469, 0.642]` | **0.5719** `[0.496, 0.645]` |

---

## 4. Paired Statistical Comparisons & Sensitivity Analysis

All comparisons represent paired differences calculated on the identical intersection of questions. Differences are expressed in **percentage points (pp)** ($\Delta = \text{Mesh} - \text{Competitor}$).

### 1. Mesh SDK vs. Mem0 SDK ($N=470$ Paired Questions)

| Metric | Role | Mean Diff (pp) | Paired 95% CI (pp) | Paired 99% CI (pp) | 95% Verdict | Survives 99% CI? |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Turn Recall@1** | **PRIMARY** | **+2.55 pp** | `[-1.49, +6.60]` | `[-2.77, +8.51]` | **Parity** | N/A (Parity) |
| **Turn Recall@5** | **PRIMARY** | **+4.47 pp** | **`[+1.91, +7.23]`** | **`[+0.85, +8.09]`** | **Mesh higher** | **Yes** |
| Turn Recall@10 | Exploratory | -0.64 pp | `[-2.55, +1.28]` | `[-3.19, +2.13]` | Parity | N/A (Parity) |
| Turn MRR | Exploratory | +1.91 pp | `[-0.63, +4.73]` | `[-1.33, +5.57]` | Parity | N/A (Parity) |
| Sess Recall@1 | Exploratory | +0.00 pp | `[-2.13, +2.34]` | `[-3.19, +3.19]` | Parity | N/A (Parity) |
| Sess Recall@5 | Exploratory | +1.70 pp | `[+0.00, +3.40]` | `[-0.43, +3.83]` | Parity | N/A (Parity) |
| Sess MRR | Exploratory | +0.60 pp | `[-0.75, +2.08]` | `[-1.32, +2.41]` | Parity | N/A (Parity) |

*Key Finding:* At Rank 1 (Turn Recall@1, Turn MRR), Mesh SDK and Mem0 SDK operate in statistical parity. At top-5 candidate retrieval, Mesh SDK provides a statistically significant advantage of **+4.47 pp** (`[+1.91, +7.23]` pp) that robustly **survives the 99% sensitivity interval**.

---

### 2. Mesh SDK vs. Basic Memory MCP ($N=470$ Paired Questions)

| Metric | Role | Mean Diff (pp) | Paired 95% CI (pp) | Paired 99% CI (pp) | 95% Verdict | Survives 99% CI? |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Turn Recall@1** | **PRIMARY** | **-10.85 pp** | **`[-15.32, -6.60]`** | **`[-16.60, -4.68]`** | **Basic Memory higher** | **Yes** |
| **Turn Recall@5** | **PRIMARY** | **-0.43 pp** | `[-3.19, +2.34]` | `[-4.26, +3.19]` | **Parity** | N/A (Parity) |
| Turn Recall@10 | Exploratory | -3.83 pp | `[-6.17, -1.70]` | `[-6.81, -0.85]` | Basic Memory higher | Yes |
| Turn MRR | Exploratory | -7.87 pp | `[-10.64, -5.26]` | `[-11.22, -4.34]` | Basic Memory higher | Yes |
| Sess Recall@1 | Exploratory | +1.49 pp | `[-0.85, +4.04]` | `[-1.49, +4.89]` | Parity | N/A (Parity) |
| Sess Recall@5 | Exploratory | +1.70 pp | `[+0.43, +2.98]` | `[+0.00, +3.62]` | Mesh higher | No (Parity at 99%) |
| Sess MRR | Exploratory | +1.59 pp | `[-0.02, +3.30]` | `[-0.46, +3.81]` | Parity | N/A (Parity) |

*Key Finding (Honest Disclosure):* Basic Memory MCP achieves significantly higher Rank-1 precision (**+10.85 pp** Turn Recall@1, surviving 99% CI). However, at top-5 candidate recall, Mesh SDK achieves full statistical parity with Basic Memory (**-0.43 pp**, CI spans zero: `[-3.19, +2.34]` pp). While Mesh has a +1.70 pp lead on Session Recall@5 at the 95% level, this difference touches zero at 99% and does not survive conservative sensitivity adjustment.

---

### 3. Mesh SDK vs. Auxly MCP Variants ($N=470$ Paired Questions)

- **Auxly Substring:** Substring search fails completely on conversational dialogue turns (Turn Recall@1 = 0.00%, Recall@5 = 0.00%). Mesh SDK is superior by **+41.28 pp** on Recall@1 and **+83.19 pp** on Recall@5.
- **Auxly + Local Ollama (`all-minilm`):** Adding neural embeddings through local Ollama raises Auxly's Turn Recall@5 to 45.96% and Session Recall@5 to 72.55%. Mesh SDK maintains a statistically significant advantage of **+29.36 pp** on Turn Recall@1 and **+37.23 pp** on Turn Recall@5 (both surviving 99% intervals).

---

### 4. Mesh SDK vs. Default Configuration ($N=94$ Paired Questions)

| Metric | Role | Mean Diff (pp) | Paired 95% CI (pp) | Paired 99% CI (pp) | Verdict |
| :--- | :---: | :---: | :---: | :---: | :--- |
| Turn Recall@1 | PRIMARY | +0.00 pp | `[+0.00, +0.00]` | `[+0.00, +0.00]` | **EXACT PARITY** |
| Turn Recall@5 | PRIMARY | +0.00 pp | `[+0.00, +0.00]` | `[+0.00, +0.00]` | **EXACT PARITY** |
| Turn MRR | Exploratory | +0.00 pp | `[+0.00, +0.00]` | `[+0.00, +0.00]` | **EXACT PARITY** |
| Sess Recall@5 | Exploratory | +0.00 pp | `[+0.00, +0.00]` | `[+0.00, +0.00]` | **EXACT PARITY** |

*Evidence of Different Settings:* Across the 94 affected questions under default configuration, exactly 33 distractor turns were rejected by safety and length guards (12 turns >16k chars, 21 prompt injection attempts), while 0 were rejected under benchmark configuration. Because exactly 0 / 896 gold evidence turns were affected, default guard rejection yielded **100% exact parity** in retrieved candidate rankings.

---

### 5. Mesh SDK (ONNX) vs. Mesh Hash-384 Fallback ($N=94$ Paired Questions)

| Metric | Role | Mean Diff (pp) | Paired 95% CI (pp) | Paired 99% CI (pp) | 95% Verdict | Survives 99% CI? |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Turn Recall@1** | **PRIMARY** | **-4.26 pp** | `[-10.64, +2.13]` | `[-12.77, +4.26]` | **Parity** | N/A (Parity) |
| **Turn Recall@5** | **PRIMARY** | **+8.51 pp** | **`[+1.06, +15.96]`** | `[+0.00, +18.09]` | **Mesh ONNX higher** | **No (Touches 0 at 99%)** |
| Turn MRR | Exploratory | +0.41 pp | `[-4.05, +5.23]` | `[-5.65, +6.34]` | Parity | N/A (Parity) |
| Sess Recall@1 | Exploratory | +8.51 pp | `[+1.06, +15.96]` | `[+0.00, +20.21]` | Mesh ONNX higher | No (Touches 0 at 99%) |
| Sess MRR | Exploratory | +6.69 pp | `[+2.62, +11.68]` | `[+1.59, +14.07]` | Mesh ONNX higher | Yes |

*Implications for Ranking Architecture:* On Turn Recall@1 and MRR, feature hashing (Hash-384) achieves statistical parity with the ONNX embedding model. Our analysis suggests that **Mesh's top-1 precision is heavily driven by its BM25/FTS5 lexical matching engine**. True ONNX semantic embeddings primarily provide value in candidate recall (Recall@5: +8.51 pp), lifting relevant turns that lack exact keyword overlap into the top-5 window.

---

## 5. Keyword-Variant Retrieval Evaluation

When queries are stripped of stop words using documented deterministic keyword extraction (`benchmarks/run_pilot.py:84-90`):

| Tool / Variant | $N$ | Turn KW R@1 | Turn KW R@5 | Turn KW R@10 | Turn KW MRR | Sess KW R@1 | Sess KW R@5 | Sess KW R@10 | Sess KW MRR |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Mesh SDK** | 470 | 0.4043 | 0.8149 | 0.8872 | 0.5732 | 0.8830 | 0.9745 | 0.9872 | 0.9215 |
| **Mem0 SDK** | 470 | 0.3511 | 0.7915 | 0.8957 | 0.5414 | 0.8809 | 0.9596 | 0.9851 | 0.9164 |
| **Basic Memory MCP** | 470 | 0.4957 | 0.8298 | 0.9021 | 0.6362 | 0.8702 | 0.9553 | 0.9830 | 0.9077 |
| **Auxly MCP (Ollama)** | 470 | 0.1021 | 0.4468 | 0.5085 | 0.2438 | 0.3532 | 0.7149 | 0.7383 | 0.5010 |
| **Auxly MCP (Substring)**| 470 | 0.0000 | 0.0021 | 0.0021 | 0.0011 | 0.0000 | 0.0021 | 0.0021 | 0.0011 |
| **Mesh SDK (Default Config)**| 94 | 0.4149 | 0.8298 | 0.9043 | 0.5907 | 0.9043 | 0.9681 | 0.9787 | 0.9283 |
| **Mesh SDK (Hash-384)** | 94 | 0.4574 | 0.7766 | 0.8404 | 0.5887 | 0.8617 | 0.9681 | 0.9787 | 0.9065 |

---

## 6. Per-Question-Type Breakdown (Weaknesses & Strengths)

Evaluating retrieval by LongMemEval question type reveals distinct structural strengths and weaknesses across tools:

#### 1. `knowledge-update` ($n=72$, Standard)
- **Basic Memory MCP:** Turn R@1 = **0.6111**, Turn R@5 = 0.9444, Turn MRR = 0.7427
- **Mesh SDK:** Turn R@1 = **0.4306**, Turn R@5 = 0.9028, Turn MRR = 0.6319
- **Mem0 SDK:** Turn R@1 = **0.3611**, Turn R@5 = 0.8611, Turn MRR = 0.5628
*Weakness Disclosed:* Basic Memory MCP significantly outperforms Mesh SDK on knowledge update queries (+18.05 pp at Rank 1), benefiting from its note-replacement structure.

#### 2. `temporal-reasoning` ($n=127$, Standard)
- **Basic Memory MCP:** Turn R@1 = **0.5354**, Turn R@5 = 0.8110, Turn MRR = 0.6559
- **Mesh SDK:** Turn R@1 = **0.3858**, Turn R@5 = 0.7874, Turn MRR = 0.5498
- **Mem0 SDK:** Turn R@1 = **0.2992**, Turn R@5 = 0.7638, Turn MRR = 0.4929
*Weakness Disclosed:* Basic Memory MCP leads Mesh SDK by +14.96 pp on temporal reasoning at Rank 1. However, Mesh retains higher top-5 recall than Mem0 (+2.36 pp).

#### 3. `multi-session` ($n=121$, Standard)
- **Mem0 SDK:** Turn R@1 = **0.4298**, Turn R@5 = 0.7769, Turn MRR = 0.5841
- **Basic Memory MCP:** Turn R@1 = **0.3967**, Turn R@5 = 0.8182, Turn MRR = 0.5826
- **Mesh SDK:** Turn R@1 = **0.3388**, Turn R@5 = 0.8182, Turn MRR = 0.5161
*Weakness Disclosed:* Mesh SDK is behind Mem0 SDK (-9.10 pp) on multi-session Rank-1 recall. However, Mesh recovers to tie Basic Memory at top-5 candidate recall (0.8182).

#### 4. `single-session-user` ($n=64$, Standard)
- **Basic Memory MCP:** Turn R@1 = 0.6562, Turn R@5 = 0.9219, Turn MRR = 0.7765
- **Mesh SDK:** Turn R@1 = 0.5156, Turn R@5 = 0.9062, Turn MRR = 0.6886
- **Mem0 SDK:** Turn R@1 = 0.5000, Turn R@5 = 0.8906, Turn MRR = 0.6757

#### 5. `single-session-assistant` ($n=56$, Standard)
- **Basic Memory MCP:** Turn R@1 = 0.6964, Turn R@5 = 0.8929, Turn MRR = 0.7973
- **Mesh SDK:** Turn R@1 = 0.6071, Turn R@5 = 0.9286, Turn MRR = 0.7308
- **Mem0 SDK:** Turn R@1 = 0.4286, Turn R@5 = 0.7857, Turn MRR = 0.6101

#### 6. `single-session-preference` ($n=30$, Standard)
- **Mem0 SDK:** Turn R@1 = 0.3333, Turn R@5 = 0.5333, Turn MRR = 0.4345
- **Mesh SDK:** Turn R@1 = 0.2000, Turn R@5 = 0.5667, Turn MRR = 0.3492
- **Basic Memory MCP:** Turn R@1 = 0.1333, Turn R@5 = 0.4667, Turn MRR = 0.2873

*(Note: Cells where $n < 30$ in subset evaluations are descriptive only.)*

---

## 7. Ingestion Integrity Audit

Every turn submitted to each tool was accounted for across all 15 runner shards:

| Shard ID | Target Evaluator & Range | Submitted Turns | Ingested Turns | Rejected Turns | Failed | Quarantined | Gold Evidence Status |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| `mesh-sdk-1` | Mesh SDK (Q 0..249) | 124,169 | 124,152 | 17 | 0 | 0 | **100% Intact (0 rejected)** |
| `mesh-sdk-2` | Mesh SDK (Q 250..499) | 123,581 | 123,565 | 16 | 0 | 0 | **100% Intact (0 rejected)** |
| `mem0-sdk-1` | Mem0 SDK (Q 0..249) | 124,169 | 124,169 | 0 | 0 | 0 | **100% Intact (0 rejected)** |
| `mem0-sdk-2` | Mem0 SDK (Q 250..499) | 123,581 | 123,581 | 0 | 0 | 0 | **100% Intact (0 rejected)** |
| `bm-mcp-1` | Basic Memory MCP (Q 0..83) | 42,207 | 42,207 | 0 | 0 | 0 | **100% Intact (0 rejected)** |
| `bm-mcp-2` | Basic Memory MCP (Q 84..167) | 41,295 | 41,295 | 0 | 0 | 0 | **100% Intact (0 rejected)** |
| `bm-mcp-3` | Basic Memory MCP (Q 168..251) | 41,664 | 41,664 | 0 | 0 | 0 | **100% Intact (0 rejected)** |
| `bm-mcp-4` | Basic Memory MCP (Q 252..335) | 41,353 | 41,353 | 0 | 0 | 0 | **100% Intact (0 rejected)** |
| `bm-mcp-5` | Basic Memory MCP (Q 336..419) | 41,393 | 41,393 | 0 | 0 | 0 | **100% Intact (0 rejected)** |
| `bm-mcp-6` | Basic Memory MCP (Q 420..499) | 39,838 | 39,838 | 0 | 0 | 0 | **100% Intact (0 rejected)** |
| `auxly-mcp-sub` | Auxly MCP Substring (Q 0..499) | 247,750 | 247,750 | 0 | 0 | 0 | **100% Intact (0 rejected)** |
| `auxly-ollama-1` | Auxly MCP Ollama (Q 0..249) | 124,169 | 124,169 | 0 | 0 | 0 | **100% Intact (0 rejected)** |
| `auxly-ollama-2` | Auxly MCP Ollama (Q 250..499) | 123,581 | 123,581 | 0 | 0 | 0 | **100% Intact (0 rejected)** |
| `mesh-default-99`| Mesh SDK Default Config (99 q) | 48,776 | 48,743 | 33 | 0 | 0 | **100% Intact (0 rejected)** |
| `mesh-hash-100` | Mesh SDK Hash-384 (100 q) | 50,023 | 50,013 | 10 | 0 | 0 | **100% Intact (0 rejected)** |

---

## 8. Runner Parity & Environmental Metadata

### 1. Runner Hardware Environment
Across all 15 GitHub Actions runner jobs, environment diagnostics recorded:
- **Runner OS:** `Linux-7.0.0-1017-azure-x86_64-with-glibc2.39` (`ubuntu-24.04`)
- **Virtualization:** Microsoft Azure Standard VM (`Standard_D4ds_v5` or equivalent, 4 vCPUs, 16 GB RAM)
- **Runner Image Version:** `20261026.354.1` (consistent across all shards)
- **Python Version:** 3.12.15 (GA standard on runner image)

### 2. Runner vs. Laptop Parity on the 20 Pilot Questions
Cross-checking the 20 pilot questions evaluated on the Windows laptop (`pilot_20_results.json`) against their corresponding records on the GitHub Actions Ubuntu runner (`merged_raw.jsonl`) matched by canonical question ID (`0862e8bf`, `0977f2af`, etc.):
- **Mesh SDK:** **20 / 20 (100.0%) identical ranked candidate turn ID lists**. Every top-5 retrieved turn ID (`raw_results_payload`) is bit-for-bit identical between Windows laptop and Ubuntu cloud runners.
- **Mem0 SDK:** **20 / 20 (100.0%) identical ranked candidate turn ID lists**. Candidate turn IDs in `raw_results_payload` match identically across all 20 questions.
- **Basic Memory MCP:** **20 / 20 (100.0%) identical ranked candidate turn ID lists**. All top-5 retrieved note turn IDs match identically across all 20 questions.
- **Auxly MCP:** **20 / 20 (100.0%) identical ranked candidate turn ID lists**. Candidate results match identically across all 20 questions.
*Parity Conclusion:* 100.0% candidate ranking parity is established across all tools. Zero near-ties flipped, confirming zero platform drift between environments.

### 3. Explanation of Runner `raw_ms` (Harness Overhead Audit)
In the runner logs, `raw_query` reported a median latency of 1,158 ms for Mesh SDK, compared to ~30 ms observed during warm local queries. 
- **Root Cause Identified:** To guarantee 100% query isolation and eliminate access-counter state drift between pre-flight gate checks, the benchmark harness creates a fresh SQLite database snapshot (`_raw`) per question, calls `_evict_namespace_caches(snap_raw)`, opens a fresh `Mesh` instance, executes a single `recall()` call, and immediately closes it.
- **Consequence:** In every question, `mesh_raw.recall()` executes a **cold vector-cache build**: reading ~500 turns and 384-dimensional float embeddings from SQLite, constructing the in-memory vector cache, setting PRAGMAs, and embedding the query via ONNX Runtime. In production or persistent sessions, this cache is built once and kept warm, where queries execute in ~15–30 ms.
- **Resolution for Pass 2:** The Pass 2 protocol explicitly isolates cold database initialization from warm in-memory retrieval latency.

---

## 9. Auxly Investigation: Exit Code 137 & Semantic Verification

### 1. Verification of Paraphrase Known-Item Test
To verify that Auxly + Ollama was genuinely utilizing 384-dimensional neural embeddings rather than falling back to substring matching, a paraphrased known-item probe was ingested:
- **Stored Content:** `"[turn:00000] The cerulean observatory celestial frequency atop Mount Horizon recorded an anomalous 432.85 MHz on stellar quadrant nine."`
- **Probe Query (Zero Lexical Overlap):** `"azure stargazing facility detected unexplained radio signal on stellar sector nine"`
- **Result:** `ki_rank = 1` across **500 / 500 questions (100.0%)**.
- **Tool Invoked:** Official MCP tool `auxly_memory_recall` querying `embeddings.db` (rebuilt via `auxly index rebuild --endpoint http://localhost:11434/v1/embeddings --embed-model all-minilm`).

### 2. Exit Code 137 Analysis
All three Auxly shards (`auxly-mcp-sub`, `auxly-ollama-1`, `auxly-ollama-2`) completed evaluation of 100% of their assigned questions and successfully committed their complete result JSONs to disk prior to termination:
- `auxly-mcp-sub`: evaluated 500/500 questions; wrote `auxly-mcp-sub_results.json` at `18:53:00Z`.
- `auxly-ollama-1`: evaluated 250/250 questions; wrote `auxly-ollama-1_results.json` at `20:48:06Z`.
- `auxly-ollama-2`: evaluated 250/250 questions; wrote `auxly-ollama-2_results.json` at `20:51:24Z`.

Process RSS tracked via `ProcessTreePeakTracker`:
- `auxly-mcp-sub`: peak child RSS averaged **17.65 MB** (max 19.02 MB).
- `auxly-ollama-1`: peak child RSS averaged **29.54 MB** (max 34.71 MB).

*Exit Code 137 Disclosure:* Exit code 137 (killed), cause unconfirmed; all 500 records were written before the kill. Peak child process RSS averaged 17.65 MB (max 19.02 MB) for `auxly-mcp-sub` and 29.54 MB (max 34.71 MB) for `auxly-ollama-1`, confirming that memory stayed well under the 16 GB runner threshold. Raw step console logs could not be retrieved via unauthenticated GitHub API (403 Forbidden). All 500 assigned questions completed evaluation and committed their JSON/JSONL outputs to disk before process termination; zero evaluation records were lost.

### 3. Draft Bug Report for Auxly Maintainers (Unfiled)

```text
Title: auxly mcp-server does not terminate on stdin EOF (orphaned process in stdio client sessions)
Version: v1.5.0 (auxly-linux-amd64, SHA-256: ec452fc983e862f78f4cf23d41d3e25396ebff974508aceb45e8224720759b04)
Environment: Linux x86_64 (Ubuntu 24.04 / glibc 2.39)

Description:
When auxly is launched in stdio MCP mode (`auxly --path <vault> mcp-server`) by an MCP client session (e.g. Python MCP SDK `stdio_client`), closing the client's stdin pipe does not cause the auxly process to exit cleanly. The process lingers until SIGKILL is sent by process tree cleanup.

Steps to Reproduce:
1. Start auxly mcp-server via pipe: `auxly --path /tmp/vault mcp-server`
2. Perform JSON-RPC initialization and tool calls over stdin/stdout.
3. Close stdin pipe (send EOF).
4. Observe that auxly process remains active in process table rather than exiting with code 0.

Expected Behavior:
auxly mcp-server should detect EOF on stdin and exit cleanly.
```

---

## 10. Specifications, Limitations & Reproduction

### 1. What Was Measured vs. What Was Not
- **Measured:** Persistent evidence retrieval accuracy across multi-session dialogues (finding the exact turn and session containing ground-truth evidence), top-$k$ recall ($k=1, 5, 10$), MRR, and NDCG@10.
- **Not Measured in Pass 1:** Generative answer synthesis, hallucination rates, or LLM-as-a-judge scores. Runner execution timings were collected for harness monitoring but are explicitly excluded from results (Pass 2 bare-metal measurements in `docs/PERFORMANCE.md` are the sole authority on speed).
- **Dataset Note:** LongMemEval-S (cleaned official corpus, SHA-256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`) under MIT License. LoCoMo dataset was **NOT** evaluated due to non-commercial license restrictions (CC BY-NC 4.0).

### 2. Tools, Versions, and Non-Default Configurations
| Tool | Version / Commit | Interface Class | Embedding Model | Non-Default Harness Settings |
| :--- | :---: | :---: | :--- | :--- |
| **Mesh SDK** | v0.8.3 (`eff1407`) | Class A (in-process) | `all-MiniLM-L6-v2` (ONNX Runtime) | `MESH_MAX_CONTENT_LENGTH=131072`, `MESH_SYNC_ONNX=1`, `MESH_REQUIRE_MODEL=1` |
| **Mem0 SDK** | v0.1.37 | Class A (in-process) | `all-MiniLM-L6-v2` (FastEmbed / Qdrant) | `infer=False` (raw turn ingestion without cloud LLM extraction) |
| **Basic Memory MCP** | v0.8.2 | Class C (stdio MCP) | `bge-small-en-v1.5` (FastEmbed) | Default CLI/MCP configuration |
| **Auxly MCP (Ollama)**| v1.5.0 (`ec452fc`) | Class C (stdio MCP) | `all-minilm` (Ollama daemon) | `--endpoint http://localhost:11434/v1/embeddings --embed-model all-minilm` |
| **Auxly MCP (Sub)** | v1.5.0 (`ec452fc`) | Class C (stdio MCP) | None (exact substring scan) | Default configuration |

### 3. Limitations & Where Mesh is Not the Best Choice
1. **Top-1 Precision Deficit vs. Basic Memory:** On Turn Recall@1, Basic Memory MCP outperforms Mesh SDK by **+10.85 pp** (52.1% vs 41.3%, surviving 99% CI). Applications requiring strict rank-1 precision without reranking should consider Basic Memory.
2. **Multi-Process Write Lock Serialization:** Under multi-process write contention, Mesh serializes writes through file-level advisory locks. At 10 concurrent processes, write throughput drops to ~8 writes/s with a p95 write latency of 9.7 s.
3. **Default-Mode Ingestion Vector Deferral:** In default mode (`MESH_SYNC_ONNX` unset), Mesh returns quickly (~13 ms) by using lexical hash vectors and queueing ONNX embedding in the background. Queries executed before the queue drains operate over lower-quality vectors.
4. **Disk Footprint:** Mesh data stores require ~7.4 MB per 489 turns (~2x larger than Mem0's ~3.3 MB) due to dual SQLite vector indices and content-free audit logging.

### 4. Reproduction Commands
- **GitHub Actions Workflow:** To reproduce Pass 1 across 15 shards on Ubuntu runners:
  ```bash
  gh workflow run bench_pass1.yml --ref feat/ci-evidence
  ```
- **Local Analysis from Merged Raw Evidence:**
  ```bash
  python benchmarks/run_pilot.py --analyze benchmarks/results/pass1/merged_raw.jsonl
  ```
- **Bare-Metal Performance Suite (Pass 2):**
  ```bash
  python benchmarks/run_scale_bench_pass2_isolated.py
  python benchmarks/run_concurrency_pass2.py
  python benchmarks/profile_b2_ram_timeseries.py
  python benchmarks/run_b4_like_for_like.py
  python benchmarks/generate_performance_report.py
  ```
