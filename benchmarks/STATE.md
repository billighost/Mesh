# Benchmarks State & Tracking

**Document Version:** 1.16.0  
**Current Step:** CHECKPOINT C1-FIXED COMPLETE — Pass 1 Full Accuracy Benchmark (N=470 Primary, N=500 Secondary) Reconciled & Documented  
**Target Commit / Base:** `feat/ci-evidence`  
**Frozen Benchmark Tag:** `benchmark-v1` (`eff1407412ef5ea9239b8bafb7ee64bf4f5a344f`)  
**CI Benchmark Run Tag:** `benchmark-v1-run` (`4e903608c0464daa499e92ea26e144bdaa361de1`)  
**Run ID:** `41109952815` (Branch: `origin/bench-results/41109952815`)  
**Timestamp:** 2026-11-02T01:30:00+01:00  

---

## 1. Current Step: CHECKPOINT C1-FIXED Review & Directives Fulfillment

Following review of Checkpoint C1, all owner directives have been executed with complete rigor:

1. **Abstention Partitioning ($N=470$ Primary, $N=500$ Secondary):**
   - Exactly 30 `_abs` questions were identified and separated from evidence retrieval scoring.
   - Headline metrics at $N=470$ independently match the owner's cross-check:
     - **Mesh SDK:** Turn R@1 = **0.4128** (~0.413), Turn R@5 = **0.8319** (~0.832), Turn MRR = **0.5814** (~0.581), Sess R@5 = **0.9787** (~0.979).
     - **Mem0 SDK:** Turn R@1 = **0.3872** (~0.387), Turn R@5 = **0.7872** (~0.787), Turn MRR = **0.5622** (~0.562), Sess R@5 = **0.9617** (~0.962).
     - **Basic Memory MCP:** Turn R@1 = **0.5213** (~0.521), Turn R@5 = **0.8362** (~0.836), Turn MRR = **0.6601** (~0.660), Sess R@5 = **0.9617** (~0.962).
   - Reconciled difference: 0.000 (exact match).

2. **Runner Latency Removed & Mesh SDK `raw_ms` Root Cause Explained:**
   - Deleted all "Ingestion Latency" and "Query Latency (p50)" columns from runner results, and deleted all "6.4x faster" throughput claims (per brief §5, shared-runner timings are not results).
   - *Harness Overhead Audit:* Mesh SDK's 1,158 ms median query time on runners was caused by per-question query snapshot isolation (`_evict_namespace_caches(snap_raw)`). On every question, `mesh_raw.recall()` built a cold vector cache from SQLite (reading ~500 turns and float embeddings, loading numpy/ONNX structures), whereas production usage keeps the cache warm (~15–30 ms). Pass 2 isolates cold initialization from warm retrieval.

3. **Complete Tables Generated:**
   - Primary table ($N=470$) and Secondary table ($N=500$) include Recall@1/5/10, Fraction of Gold Found, MRR, and NDCG@10 with bootstrap 95% CIs.
   - Paired differences vs Mesh SDK computed on exact question intersections.
   - Keyword-variant tables generated from `kw_scores`.
   - Per-question-type breakdown generated with cell sample sizes ($n$) and small-$n$ ($<30$) descriptive labels.
   - Ingestion integrity table accounts for all 247,750 turns across all 15 shards.
   - System metadata recorded: Azure `Standard_D4ds_v5` runner image `20261026.354.1`, 4 vCPUs, 16 GB RAM.
   - 20-pilot parity cross-check: Mesh SDK achieved **20/20 (100.0%) bit-identical top-5 ranked turn ID matches** between laptop and runner.

4. **Statistical Language Disciplined:**
   - Removed all `p < 0.001` phrasing.
   - All differences expressed in percentage points (`pp`).
   - Primary comparisons established: **Turn Recall@1 and Turn Recall@5 (Mesh SDK vs Mem0 SDK and vs Basic Memory MCP)**. All other comparisons labeled exploratory.
   - Sensitivity analysis with 99% bootstrap CIs added: session Recall@5 differences (+1.70 pp vs Mem0 and Basic Memory) do not survive 99% intervals and are marked as inconclusive/parity under conservative thresholds.
   - "Evidence retrieval" terminology used throughout.

5. **Auxly OOM Evidence & Verification:**
   - Verified that all 3 Auxly shards evaluated 100% of questions and wrote complete result JSONs prior to process teardown (`auxly-mcp-sub`: 500/500 q; `auxly-ollama-1`: 250/250 q; `auxly-ollama-2`: 250/250 q).
   - Peak child RSS was low: ~17.65 MB average for substring MCP, ~29.54 MB for Ollama MCP.
   - Exit code 137 occurred during Python process termination under bash `set -eo pipefail` because `auxly-linux-amd64` does not exit cleanly when stdio stdin closes.
   - Unfiled bug report drafted for Auxly maintainers.
   - Paraphrased known-item query with zero lexical overlap (`"azure stargazing facility detected unexplained radio signal on stellar sector nine"`) passed at Rank 1 on 500/500 questions, verifying active 384-d semantic embedding index rebuild.

6. **Provenance & Methodology Documentation:**
   - Tag `benchmark-v1-run` created on exact run commit `4e903608`.
   - Complete itemized list of harness/workflow changes between `benchmark-v1` and `4e903608` recorded in `docs/BENCHMARKS.md`.
   - Zero changes to `mesh/` code (`git diff benchmark-v1 4e903608 -- mesh/` is empty).

7. **Default-Config Parity Evidence:**
   - Recorded that across the 94 affected questions under default configuration, exactly 33 distractor turns were rejected by safety and length guards (12 >16k chars, 21 prompt injections), with 0 gold evidence turns affected, proving exact candidate ranking parity is genuine.

8. **Honest Takeaways & Architectural Insights:**
   - Acknowledged per-type weaknesses: Mesh behind Mem0 on multi-session Recall@1 (-9.10 pp) and behind Basic Memory on temporal (-14.96 pp) and knowledge-update (-18.05 pp) Recall@1.
   - Acknowledged that Hash-384 fallback achieves statistical parity with ONNX on Recall@1 and MRR, indicating Mesh's top-1 precision is heavily driven by its BM25/FTS5 lexical engine, with ONNX embeddings providing value primarily in candidate recall (Recall@5: +8.51 pp).

---

## 2. Complete Benchmark Evidence Tables

Authoritative tables are maintained in [`docs/BENCHMARKS.md`](file:///c:/Users/bb201/Desktop/Mesh/docs/BENCHMARKS.md). Key summary tables are reproduced below:

### Primary Table: $N=470$ (Non-Abstention Corpus)

| Tool / Variant | $N$ | Turn Recall@1 (95% CI) | Turn Recall@5 (95% CI) | Turn Recall@10 (95% CI) | Turn Frac Found (95% CI) | Turn MRR (95% CI) | Turn NDCG@10 (95% CI) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Mesh SDK** | 470 | **0.4128** `[0.368, 0.455]` | **0.8319** `[0.798, 0.864]` | **0.8894** `[0.860, 0.917]` | **0.8040** `[0.773, 0.835]` | **0.5814** `[0.546, 0.615]` | **0.6022** `[0.574, 0.633]` |
| **Mem0 SDK** | 470 | **0.3872** `[0.340, 0.432]` | **0.7872** `[0.751, 0.823]` | **0.8957** `[0.868, 0.921]` | **0.8044** `[0.773, 0.835]` | **0.5622** `[0.527, 0.594]` | **0.5807** `[0.552, 0.611]` |
| **Basic Memory MCP** | 470 | **0.5213** `[0.474, 0.568]` | **0.8362** `[0.804, 0.868]` | **0.9277** `[0.904, 0.949]` | **0.8409** `[0.812, 0.868]` | **0.6601** `[0.626, 0.696]` | **0.6673** `[0.639, 0.698]` |
| **Auxly MCP (Ollama)**| 470 | **0.1191** `[0.089, 0.149]` | **0.4596** `[0.413, 0.502]` | **0.5234** `[0.477, 0.566]` | **0.3868** `[0.347, 0.424]` | **0.2596** `[0.227, 0.289]` | **0.2938** `[0.257, 0.329]` |
| **Auxly MCP (Substring)**| 470 | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` | **0.0000** `[0.000, 0.000]` |
| **Mesh SDK (Default Config)**| 94 | **0.4362** `[0.340, 0.543]` | **0.8617** `[0.787, 0.925]` | **0.9149** `[0.851, 0.968]` | **0.8147** `[0.749, 0.877]` | **0.6147** `[0.546, 0.686]` | **0.6190** `[0.564, 0.679]` |
| **Mesh SDK (Hash-384)**| 94 | **0.4681** `[0.362, 0.575]` | **0.7553** `[0.660, 0.840]` | **0.8511** `[0.766, 0.915]` | **0.7626** `[0.674, 0.839]` | **0.5919** `[0.507, 0.674]` | **0.6084** `[0.532, 0.677]` |

### Paired Differences vs. Mesh SDK ($N=470$ Intersection)

| Comparison | Metric | Role | Mean Diff (pp) | Paired 95% CI (pp) | Paired 99% CI (pp) | 95% Verdict | Survives 99% CI? |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Mesh SDK vs Mem0 SDK** | Turn Recall@1 | **PRIMARY** | +2.55 pp | `[-1.49, +6.60]` | `[-2.77, +8.51]` | Parity | N/A (Parity) |
| | Turn Recall@5 | **PRIMARY** | **+4.47 pp** | **`[+1.91, +7.23]`** | **`[+0.85, +8.09]`** | **Mesh higher** | **Yes** |
| | Turn MRR | Exploratory | +1.91 pp | `[-0.63, +4.73]` | `[-1.33, +5.57]` | Parity | N/A (Parity) |
| | Sess Recall@5 | Exploratory | +1.70 pp | `[+0.00, +3.40]` | `[-0.43, +3.83]` | Parity | N/A (Parity) |
| **Mesh SDK vs Basic Memory MCP** | Turn Recall@1 | **PRIMARY** | **-10.85 pp** | **`[-15.32, -6.60]`** | **`[-16.60, -4.68]`** | **Basic Memory higher** | **Yes** |
| | Turn Recall@5 | **PRIMARY** | -0.43 pp | `[-3.19, +2.34]` | `[-4.26, +3.19]` | Parity | N/A (Parity) |
| | Turn MRR | Exploratory | -7.87 pp | `[-10.64, -5.26]` | `[-11.22, -4.34]` | Basic Memory higher | Yes |
| | Sess Recall@5 | Exploratory | +1.70 pp | `[+0.43, +2.98]` | `[+0.00, +3.62]` | Mesh higher | No (Touches 0 at 99%) |
| **Mesh SDK vs Mesh Hash-384** | Turn Recall@1 | **PRIMARY** | -4.26 pp | `[-10.64, +2.13]` | `[-12.77, +4.26]` | Parity | N/A (Parity) |
| | Turn Recall@5 | **PRIMARY** | **+8.51 pp** | **`[+1.06, +15.96]`** | `[+0.00, +18.09]` | **Mesh higher** | No (Touches 0 at 99%) |
| | Turn MRR | Exploratory | +0.41 pp | `[-4.05, +5.23]` | `[-5.65, +6.34]` | Parity | N/A (Parity) |

---

## 3. Next Action

1. **STOP AT CHECKPOINT C1-FIXED** per protocol.
2. Await owner's review and sign-off on the reconciled evidence report before beginning PASS 2 (laptop performance and resource efficiency benchmark).
