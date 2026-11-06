"""One previously frozen optional fixture; this is not a production model catalog."""

MODEL = {
    "repository": "Xenova/ms-marco-MiniLM-L-6-v2",
    "revision": "a09144355adeed5f58c8ed011d209bf8ee5a1fec",
    "model_file": "model.onnx", "model_remote": "onnx/model_quantized.onnx",
    "model_bytes": 23143499,
    "model_sha256": "e9d8ebf845c413e981c175bfe49a3bfa9b3dcce2a3ba54875ee5df5a58639fbe",
    "tokenizer_file": "tokenizer.json", "tokenizer_remote": "tokenizer.json", "tokenizer_bytes": 711396,
    "tokenizer_sha256": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    "tokenizer_verified": True,
}

LIMITS = {
    "parent_cap": 4, "maximum_passage_tokens": 224, "maximum_query_tokens": 29,
    "maximum_pair_tokens": 256, "batch_items": 4, "intra_op_threads": 1, "inter_op_threads": 1,
    "intra_op_spinning": False, "inter_op_spinning": False, "provider": "CPUExecutionProvider",
    "warmup_queries_per_namespace": 5, "minimum_resource_samples_per_cell": 100,
}

RESOURCE_MAXIMA = {"total_rss_mib": 768, "delta_rss_mib": 128, "delta_p95_ms": 50,
                   "delta_p99_ms": 100, "delta_cpu_ms": 50}

POLICY = {
    "input": "First four distinct authorized parent candidates in immutable no-reranker passage order.",
    "span": "Exact selected source span, verbatim, at most 224 CE tokens; oversized selected spans fail visibly.",
    "body_fallback": "Accepted original body prefix at 224 CE tokens; record original/kept tokens and characters.",
    "query": "Original query prefix at 29 CE tokens; no passage truncation by pair tokenizer.",
    "pair": "BERT pair special tokens plus independently bounded query/passage; no silent pair truncation.",
    "ranking": "Finite uncalibrated relevance logit descending; ties retain original rank; tail order unchanged.",
    "quality": "Separate scorer only after fixed exports: zero paired loss in public parent hit@1/@5/@10, MRR/nDCG; zero new bounded exact-evidence hit@1 regressions.",
    "baseline": "Fresh matched subprocess rebuild, same source/runtime/vectors/cache, fixed warmups before one CE session.",
    "latency": "Worst C64/C96 paired C4 minus no-CE warm-query p95/p99, including guards; nearest-rank percentiles.",
    "resource_repetitions": "max(1, ceil(100/frozen query count)) fixed passes; one quality export per query, repetitions recorded separately.",
    "rss": "Sampled process-tree peak over each complete matched rebuild; include orchestration process; 10ms sampling.",
    "cpu": "Worst C64/C96 mean paired process CPU per query including guards/inference; cold indexing/loading excluded.",
    "labels_in_ranking_inputs": False, "production_default_enabled": False,
    "historical_evidence": "Existing C64 MiniLM resource elimination and TinyBERT quality findings remain unchanged.",
}
