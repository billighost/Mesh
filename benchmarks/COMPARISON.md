# Matched competitor retrieval comparison

[competitor_comparison.py](competitor_comparison.py) runs Mesh 0.9.0 candidate,
Mem0 2.2.1, Basic Memory 0.24.1 and Auxly 1.5.0 against the same original parent
documents, fixed queries and positive relevance judgments. No new numerical
results are claimed until the source-bound Actions artifacts are reviewed and
published in [RESULTS.md](RESULTS.md). Older Mesh 0.8.3 comparisons remain
historical evidence.

The [comparison workflow](../.github/workflows/competitor-comparison.yml) has
twenty independent cells: four tools and five datasets. Public NFCorpus retains
all 3,633 source parents, 323 test queries and 12,334 positive judgments; SciFact
retains all 5,183 parents, 300 test queries and 339 positive judgments. The
direct-fact and positional long-text fixtures are previously inspected bounded
regressions. LongMemEval is the previously inspected six-query, 191-turn subset;
it does not repeat the historical 470-answerable-question evaluation.

| Tool | Interface and retrieval profile |
| --- | --- |
| Mesh 0.9.0 candidate | Guarded SDK `learn` and production default `recall`, pinned MiniLM, neural indexing required, optional passages and reranker off |
| Mem0 2.2.1 | Native SDK local Qdrant hybrid search, `infer=False`, reranker off; FastEmbed 0.9.1 supplies native BM25, and a local ONNX adapter uses the identical pinned MiniLM bytes and truncates at 256 tokens |
| Basic Memory 0.24.1 | Persistent MCP stdio `search_notes`, native local text search; optional `sqlite-vec` distribution absent, automatic update and reranker disabled |
| Auxly 1.5.0 | Persistent MCP stdio `auxly_memory_search`, native exact substring search; service self-repair disabled |

Mesh executes in the source-qualified host environment. Official Mem0 and Basic
Memory distributions execute in separate virtual environments because Basic
Memory requires MCP 2 while Mesh requires MCP 1, and Mem0 requires protobuf below
7 while the qualified Mesh host has protobuf 7. Each competitor environment
keeps its own supported dependencies and complete frozen installed manifest.
The native Basic Memory client and server both execute inside its MCP 2
environment; the supervising host communicates with the worker through files.
Mem0's NumPy, ONNX Runtime, tokenizer and resource sampler versions must match
the qualified Mesh host. Its native BM25 implementation is FastEmbed 0.9.1.

These are explicitly configured local profiles. They compare the same retrieval
problem across distinct implementations. Mesh's production encoder pools
overlapping windows for long documents; the Mem0 adapter truncates. Basic Memory
and Auxly do not use embeddings in these profiles. SDK and MCP latency belong to
separate interface strata. Ingestion includes Mesh acknowledgement/readiness,
Mem0 SDK storage, or native note-file materialization/indexing; those boundaries
are described separately and are not interchangeable storage throughput claims.

Source identity comes from verified on-disk parent bodies and returned native
IDs or exact note paths. Public parents join title and text with a newline;
surrounding whitespace is stripped identically for every tool. Mesh guards,
default size limits, privacy and deduplication remain active. Rejected or
unfaithful parents remain in the unconditional relevance denominator. The first
faithfully preserved source is canonical by input order when a tool coalesces
duplicate parents to one native identity. An unfaithful
returned slot stays at its original rank and receives no relevance credit.

Scoring opens qrels only after worker shutdown and ranked-export closure. It
preserves graded judgments and reports Hit@1/@5/@10, document Recall@1/@5/@10,
MRR@10 and nDCG@1/@5/@10 for ranked tools, with exact turn and session hits
separated on LongMemEval. Auxly iterates an unordered Go map and returns uncapped
matches; only the first ten distinct returned matches receive subset hit/recall
measurements. Its rank metrics are unavailable. Missing, duplicate or malformed
query exports cannot score; complete cells retain every selected query.

Only static preparation can fetch pinned distribution archives, public datasets
and allowlisted model assets. Mem0 BM25 preparation verifies 25,242 bytes of
configuration and stopwords at immutable `Qdrant/bm25` revision
`22b8d2af71a76161e18dd432d2cee0eefa66e412`, then copies them into each worker's
isolated native FastEmbed cache. The unchanged native sparse constructor must
load offline. Every accepted parent must produce a sparse write, and every
query must execute native sparse search; fallback or incomplete sparse
accounting makes the cell unscored. Measured Python and Go descendants run in a
disconnected Linux network namespace with isolated home/configuration/data
directories. Mem0's LLM factory raises on inference, and all cloud credentials
are removed from workers. Automatic Mesh daemons, synchronization, downloads
and updates are disabled. The measured process group is stopped before cleanup.
Any missing binary/model, network-isolation failure, native exception, incomplete
accounting or resource rejection stays visible and unscored.

Each cell has a 45-minute native wall limit and a 4 GiB sampled worker-tree RSS
limit. Reports retain sampled CPU/RSS, single-pass query latency, original corpus
and query counts, installed package versions, source hashes, model pins, dataset
and ranked-export digests. Sampling can miss peaks or child CPU after exit and
summed RSS can count shared pages twice. Single-pass observations do not establish
repeatability, general superiority or generated-answer accuracy.

Dispatch requires a completed production run containing successful primary and
minimum full-suite artifacts for byte-identical Mesh runtime sources and package
metadata. The Mesh host installation uses the primary run's full frozen
dependency constraints; every installed shared host distribution must match its
qualified version, including preinstalled tooling. This check remains separate
from the official competitors' environments. Each child environment records its
Python, ABI, platform, full installed versions, official wheel digest and copied
interpreter bytes before measurement. Mem0 also verifies the matching encoder
versions. Dependency consistency must pass before Basic Memory's vector package
is deliberately removed for keyword search; the subsequent check permits only
that declared missing package. The worker inventory, interpreter and entrypoint
bytes must remain unchanged through measurement. Missing native packages or
Linux wheel incompatibility remain unscored.

The artifacts retain both source-qualified Mesh runtime manifests, the complete
host and competitor inventories, and the distinct dependency scopes.
Qualification used Python 3.14 and 3.10 on Windows; native comparison uses Python
3.12 on Linux, with different platform wheel bytes. Source and selected encoder
version equality do not establish identical platforms or full qualification of
the competitors' dependencies. The unchanged Mesh package is qualified by the
production artifacts; the separate comparator overlay runs its model-free
contract tests before native execution. Its source archive is not claimed to
have passed the core archive gate. Full native execution is Actions-only; do not
run models, public corpora or competitors on the constrained development machine.

Pushes to `ci/mesh-comparison-*` use production qualification run `43906485048`;
the source hash checks still reject a different runtime. Manual or reusable
dispatches require an explicit qualification run ID. Up to ten independent
hosted workers run concurrently; each cell retains its serial query order.

```bash
gh workflow run competitor-comparison.yml --ref YOUR_COMPARISON_BRANCH \
  -f qualification_run_id=YOUR_COMPLETED_QUALIFICATION_RUN_ID
```

Successful measurement artifacts contain `summary.json`, content-free query and
document references in `ranked.jsonl`, and progress accounting. Private worker
logs, parent bodies, raw queries and labels are excluded from uploaded results.
Every failed or unrun cell still uploads its initialized summary. Review all
twenty cells and matching input hashes before publishing a comparison table.
