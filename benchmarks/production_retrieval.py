"""Offline, isolated retrieval regressions and current-default query verification.

Existing pinned model files and prepared dataset manifests are required. No
provider, network download, answer generator, or real Mesh configuration is used.
Relevance labels are opened only by the separate score command after retrieval.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from replay_retrieval import (checked_path, compact_results, configure_isolation, digest_file,
                              freeze_runtime, isolated_artifact_path, isolated_source_home, read_jsonl,
                              safe_member, verify_exports, write_json)

MODEL_TAG = "onnx-minilm-l6-v2"
PASSAGE_REPRESENTATION = "paragraph-sentences-v1"
PASSAGE_BATCH_LIMIT = 32
PASSAGE_SNAPSHOT_FOLDER = "mesh-frozen-quality-passage-snapshot"
PASSAGE_SNAPSHOT_CONFIGURATION = "prequery-original-parent-passages-v1"
WORKER_FAILURE_SOURCES = frozenset({"production_retrieval.py", "passage_retrieval.py", "replay_retrieval.py",
                                    "retrieval_c4.py", "retrieval_c4_model.py", "memory.py", "store.py",
                                    "passage_search.py", "passages.py", "runtime_rerank_c4.py", "local_rerank.py"})
WORKER_FAILURE_TYPES = frozenset({"ValueError", "TypeError", "KeyError", "AttributeError", "AssertionError",
                                  "OSError", "FileNotFoundError", "PermissionError", "RuntimeError", "ImportError",
                                  "ModuleNotFoundError", "KeyboardInterrupt", "SystemExit", "TimeoutExpired",
                                  "CacheBudgetExceededError", "StaleStoreError"})


def digest_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def worker_failure_details(exc, stage):
    # Keep only fixed source names and line numbers, never exception arguments.
    frames, trace = [], exc.__traceback__
    while trace is not None:
        source = Path(trace.tb_frame.f_code.co_filename).name
        if source in WORKER_FAILURE_SOURCES:
            frames.append({"source": source, "line": trace.tb_lineno})
        trace = trace.tb_next
    name = type(exc).__name__
    return {"stage": stage, "exception_type": name if name in WORKER_FAILURE_TYPES else "other",
            "source_frames": frames[-4:]}


def validate_runtime_passage_schema(value):
    # Mesh stores its schema constant as text; benchmark manifests use a number.
    if type(value) not in {str, int} or value not in {"6", 6}:
        raise ValueError("Runtime passage evaluation requires the canonical schema-6 runtime")
    return 6


def decorate_results(results, records_by_memory):
    if any(row.get("local_only") or row.get("quarantined") for row in results):
        raise RuntimeError("Private or quarantined candidate in a public export")
    source_ids = {identifier: [record["document_id"] for record in records if record["faithful_public"]]
                  for identifier, records in records_by_memory.items()}
    exported = compact_results(results, source_ids)
    for row, candidate in zip(results, exported):
        candidate.update(incarnation=row.get("incarnation"), store_generation=row.get("store_generation"),
                         local_only=bool(row.get("local_only")), quarantined=bool(row.get("quarantined")),
                         selected_passage_ordinal=row.get("selected_passage_ordinal"),
                         supporting_spans=[{key: span.get(key) for key in (
                             "memory_id", "revision", "incarnation", "store_generation", "start", "end",
                         )} for span in row.get("supporting_spans", [])])
    return exported


def verify_manifest(path: Path) -> dict:
    path = checked_path(path, must_exist=True, directory=False)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for relative, expected in manifest["files"].items():
        expected_hash = expected["sha256"] if isinstance(expected, dict) else expected
        actual = digest_file(safe_member(path.parent, relative))
        if actual != expected_hash:
            raise ValueError(f"Dataset file differs from preparation manifest: {relative}")
    return manifest

def append_jsonl(stream, value) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
    stream.flush()

def documents(path: Path):
    for row in read_jsonl(path):
        if "_id" in row:
            yield {"id": str(row["_id"]), "content": row.get("title", "") + "\n\n" + row["text"]}
        else:
            yield {"id": str(row["id"]), "content": row["content"]}

def bounded_batches(rows):
    batch, byte_count = [], 0
    for row in rows:
        size = len(row["content"].encode("utf-8"))
        if batch and (len(batch) >= 64 or byte_count + size > 512 * 1024):
            yield batch
            batch, byte_count = [], 0
        batch.append(row)
        byte_count += size
    if batch:
        yield batch

def verify_sqlite(mesh, store_module) -> dict:
    with mesh.store._connect() as connection:
        journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        options = {row[0] for row in connection.execute("PRAGMA compile_options").fetchall()}
    from mesh.sqlite_runtime import is_patched_sqlite
    version = store_module.sqlite3.sqlite_version
    if not is_patched_sqlite(version) or journal != "wal" or synchronous != 2:
        raise RuntimeError("Evaluation requires verified patched SQLite, WAL and FULL synchronization")
    if "THREADSAFE=0" in options or "ENABLE_FTS5" not in options:
        raise RuntimeError("Required SQLite thread safety / FTS5 is absent")
    return {"sqlite_version": version, "journal_mode": journal, "synchronous": synchronous,
            "fts5": True, "threadsafe": True}

def ingest_corpus(mesh, path: Path, dataset: str, corpus_id: str, memory_module,
                  expected_tag: str = "onnx-minilm-l6-v2", retarget_jobs=None) -> tuple[list[dict], dict]:
    records, submitted = [], 0
    for batch in bounded_batches(documents(path)):
        inputs = [{"content": row["content"], "memory_type": "fact", "local_only": False, "tags": []}
                  for row in batch]
        results = mesh.learn_many(inputs, atomic=False, deduplicate=True)
        if len(results) != len(batch):
            raise RuntimeError("SDK batch response did not account for every submitted item")
        for row, result in zip(batch, results):
            content = row["content"]
            records.append({
                "dataset": dataset, "corpus_id": corpus_id, "document_id": row["id"],
                "input_sha256": digest_text(content), "boundary_trimmed_sha256": digest_text(content.strip()),
                "input_bytes": len(content.encode("utf-8")), "input_characters": len(content),
                "input_boundary_trimmed": content != content.strip(),
                "acceptance": result,
            })
        submitted += len(batch)
        if submitted % 512 == 0:
            print(json.dumps({"phase": "acceptance", "dataset": dataset, "corpus": corpus_id,
                              "submitted": submitted}), flush=True)
    if retarget_jobs is not None:
        retarget_jobs(mesh, records)
    # Exercise the same durable revision/model-checked worker implementation without
    # a concurrent worker changing the corpus between verification and measurement.
    iterations, no_progress = 0, 0
    while True:
        before = int(mesh.store.embedding_job_stats()["pending_count"])
        if not before:
            break
        jobs = mesh.store.list_embedding_jobs(limit=32)
        if not jobs:
            raise RuntimeError("Neural readiness failed: durable jobs are deferred")
        hints = [memory_module._ReembedHint(str(mesh.store.db_path.parent), mesh.namespace,
                                           job["memory_id"], int(job["revision"]),
                                           job["target_model"], int(job["attempts"]),
                                           incarnation=job["incarnation"], store_generation=job["store_generation"])
                 for job in jobs]
        memory_module._process_reembed_batch(hints)
        after = int(mesh.store.embedding_job_stats()["pending_count"])
        iterations += 1
        no_progress = no_progress + 1 if after >= before else 0
        if no_progress >= 3:
            raise RuntimeError("Neural readiness made no progress; this run cannot be scored as indexed retrieval")
        if iterations % 16 == 0:
            print(json.dumps({"phase": "neural-readiness", "dataset": dataset, "corpus": corpus_id,
                              "jobs_remaining": after}), flush=True)
    accepted_ids = {record["acceptance"]["memory_id"] for record in records
                    if record["acceptance"].get("memory_id")}
    current_rows = {identifier: mesh.get(identifier, include_private=True) for identifier in accepted_ids}
    current_revisions = {identifier: int(row["revision"]) for identifier, row in current_rows.items() if row}
    for offset in range(0, len(current_revisions), 128):
        ids = list(current_revisions)[offset:offset + 128]
        status = mesh.wait_for_indexed(ids, timeout=0, include_private=True,
                                      expected_revisions={identifier: current_revisions[identifier] for identifier in ids},
                                      expected_incarnations={identifier: current_rows[identifier]["incarnation"] for identifier in ids},
                                      expected_store_generation=mesh.store.store_generation,
                                      target_model=expected_tag)
        if status["status"] != "indexed" or len(status["indexed"]) != len(ids):
            raise RuntimeError("Revision/model-bound neural readiness validation failed")
    summary = Counter()
    for record in records:
        result = record["acceptance"]
        identifier = result.get("memory_id")
        row = current_rows.get(identifier)
        flags = {
            "accepted": identifier is not None,
            "rejected": result.get("status") == "rejected",
            "missing": identifier is not None and row is None,
            "private": bool(row and row.get("local_only")),
            "quarantined": bool(row and row.get("quarantined")),
            "body_changed": bool(row and digest_text(row["content"]) != record["boundary_trimmed_sha256"]),
            "revision_advanced": bool(row and int(row["revision"]) != int(result.get("revision", row["revision"]))),
            "neural_indexed": bool(row and row.get("embedding_model") == expected_tag),
        }
        faithful_public = bool(flags["accepted"] and row and flags["neural_indexed"]
                               and not any(flags[key] for key in ("missing", "private", "quarantined", "body_changed")))
        record.update(flags, faithful_public=faithful_public,
                      current_revision=int(row["revision"]) if row else None,
                      current_incarnation=row["incarnation"] if row else None,
                      store_generation=mesh.store.store_generation if row else None,
                      stored_characters=len(row["content"]) if row else None,
                      stored_sha256=digest_text(row["content"]) if row else None)
        for flag, value in flags.items():
            summary[flag] += int(value)
        summary["faithful_public"] += int(faithful_public)
        summary["input_boundary_trimmed"] += int(record["input_boundary_trimmed"])
    return records, dict(summary, submitted=len(records), unique_accepted_memories=len(accepted_ids),
                         pending_embedding_jobs=int(mesh.store.embedding_job_stats()["pending_count"]))

def evaluate_queries(mesh, queries: list[dict], records: list[dict], protocol: dict,
                     memory_module, streams: dict, vector_stream,
                     expected_tag: str = "onnx-minilm-l6-v2", embed_query=None) -> None:
    by_memory = defaultdict(list)
    for record in records:
        identifier = record["acceptance"].get("memory_id")
        if identifier:
            by_memory[identifier].append(record)
    for query_index, query in enumerate(queries):
        vector, model_tag = (embed_query or memory_module._embed_query_with_tag)(query["query"])
        if model_tag != expected_tag:
            raise RuntimeError("The frozen neural query backend is not active")
        append_jsonl(vector_stream, {"query_id": query["id"], "corpus_id": query.get("corpus", "complete"),
                                    "query": query["query"], "embedding_model": model_tag, "vector": vector,
                                    "namespace": mesh.namespace})
        for candidate_cap in protocol["candidate_caps"]:
            os.environ["MESH_RECALL_CANDIDATES"] = str(candidate_cap)
            raw = mesh.store.hybrid_query(query_text=query["query"], embedding=vector,
                                         query_embedding_model=model_tag, n=64,
                                         include_private=False, include_quarantined=False)
            final = mesh.recall(query["query"], n=10, include_private=False, include_quarantined=False,
                                record_access=False, _precomputed_embedding=vector, _precomputed_model_tag=model_tag)
            rerank_input = mesh.recall(query["query"], n=24, include_private=False, include_quarantined=False,
                                       record_access=False, _precomputed_embedding=vector, _precomputed_model_tag=model_tag)
            dense = mesh.store.hybrid_query(query_text="", embedding=vector,
                                           query_embedding_model=model_tag, n=64,
                                           include_private=False, include_quarantined=False)
            payload = {
                "query_id": query["id"], "query": query["query"],
                "corpus_id": query.get("corpus", "complete"), "namespace": mesh.namespace,
                "candidate_cap": candidate_cap, "query_embedding_model": model_tag,
                "raw_hybrid_top64": decorate_results(raw, by_memory),
                "final_sdk_top10": decorate_results(final, by_memory),
                "authorized_sdk_rerank_top24": decorate_results(rerank_input, by_memory),
                "dense_public_top64": decorate_results(dense, by_memory),
                "endpoint": "Evidence retrieval; no answer generator is called.",
            }
            append_jsonl(streams[candidate_cap], payload)
        if (query_index + 1) % 25 == 0:
            print(json.dumps({"phase": "queries", "namespace": mesh.namespace, "completed": query_index + 1}), flush=True)

def candidate_document(row: dict) -> str | None:
    # Choose the first faithfully preserved source document by ingestion order.
    # Relevance labels must never choose which source ID a deduplicated row receives.
    ids = row.get("faithful_document_ids", [])
    return ids[0] if ids else None

def query_metrics(candidates: list[dict], gold: dict[str, int], cutoffs=(1, 5, 10, 64)) -> dict[str, float]:
    documents = [candidate_document(row) for row in candidates]
    metrics = {}
    for cutoff in cutoffs:
        found = {identifier for identifier in documents[:cutoff] if identifier in gold}
        metrics[f"hit_at_{cutoff}"] = float(bool(found))
        metrics[f"document_recall_at_{cutoff}"] = len(found) / len(gold) if gold else 0.0
    first_rank = next((rank for rank, identifier in enumerate(documents[:10], 1) if identifier in gold), None)
    metrics["mrr_at_10"] = 1.0 / first_rank if first_rank else 0.0
    # A source document contributes at most once to DCG, even if it is repeated.
    seen = set()
    dcg = 0.0
    for rank, identifier in enumerate(documents[:10], 1):
        relevance = gold.get(identifier, 0) if identifier not in seen else 0
        seen.add(identifier)
        dcg += (2 ** relevance - 1) / math.log2(rank + 1)
    ideal = sorted(gold.values(), reverse=True)[:10]
    idcg = sum((2 ** relevance - 1) / math.log2(rank + 1) for rank, relevance in enumerate(ideal, 1))
    metrics["ndcg_at_10"] = dcg / idcg if idcg else 0.0
    return metrics

def mean_interval(values: list[float]) -> dict:
    if not values:
        return {"mean": None, "ci95": None, "n": 0}
    vector = np.asarray(values, dtype=np.float64)
    generator = np.random.default_rng(314159)
    sampled_means = vector[generator.integers(0, len(vector), size=(5000, len(vector)))].mean(axis=1)
    low, high = np.quantile(sampled_means, [0.025, 0.975])
    return {"mean": float(vector.mean()), "ci95": [float(low), float(high)], "n": len(vector)}

def summarize(rows: list[dict], field: str, qrels: dict, retained: dict, query_ids: list[str]) -> dict:
    by_query = {row["query_id"]: row for row in rows}
    metrics = {}
    cohorts = {
        "unconditional": [identifier for identifier in query_ids if qrels.get(identifier)],
        "any_gold_survived": [identifier for identifier in query_ids
                              if set(qrels.get(identifier, {})) & retained.get(identifier, set())],
        "all_gold_survived": [identifier for identifier in query_ids
                              if qrels.get(identifier) and set(qrels[identifier]).issubset(retained.get(identifier, set()))],
    }
    for cohort, identifiers in cohorts.items():
        per_query = [query_metrics(by_query[identifier][field], qrels[identifier]) for identifier in identifiers]
        metrics[cohort] = {name: mean_interval([row[name] for row in per_query])
                           for name in query_metrics([], {"placeholder": 1})}
    retained_metrics = [query_metrics(by_query[identifier][field],
                                      {document: grade for document, grade in qrels[identifier].items()
                                       if document in retained.get(identifier, set())})
                        for identifier in cohorts["any_gold_survived"]]
    metrics["retained_gold_denominator"] = {name: mean_interval([row[name] for row in retained_metrics])
                                          for name in query_metrics([], {"placeholder": 1})}
    return metrics

def paired_difference(rows: list[dict], hybrid_field: str, dense_field: str, qrels: dict, query_ids: list[str]) -> dict:
    differences = {}
    for metric in ("hit_at_1", "hit_at_5", "hit_at_10", "mrr_at_10", "ndcg_at_10"):
        values = []
        for row in rows:
            if row["query_id"] in query_ids and qrels.get(row["query_id"]):
                gold = qrels[row["query_id"]]
                values.append(query_metrics(row[hybrid_field], gold)[metric] - query_metrics(row[dense_field], gold)[metric])
        differences[metric] = mean_interval(values)
    return differences

def paired_hit_at_1_counts(rows: list[dict], candidate_field: str, baseline_field: str,
                           qrels: dict, query_ids: list[str]) -> dict[str, int]:
    # Net mean improvements can conceal lost exact-evidence hits on other queries.
    counts = {name: 0 for name in (
        "answered_queries", "baseline_hits", "candidate_hits", "lost_baseline_hits", "gained_hits",
    )}
    selected = set(query_ids)
    for row in rows:
        identifier = row["query_id"]
        gold = qrels.get(identifier)
        if identifier not in selected or not gold:
            continue
        baseline = bool(query_metrics(row[baseline_field], gold, cutoffs=(1,))["hit_at_1"])
        candidate = bool(query_metrics(row[candidate_field], gold, cutoffs=(1,))["hit_at_1"])
        counts["answered_queries"] += 1
        counts["baseline_hits"] += int(baseline)
        counts["candidate_hits"] += int(candidate)
        counts["lost_baseline_hits"] += int(baseline and not candidate)
        counts["gained_hits"] += int(candidate and not baseline)
    return counts


def install_model(root, model_directory, protocol, *, query_only, repository=None):
    configure_isolation(root)
    model_directory = isolated_artifact_path(model_directory, repository, must_exist=True)
    os.environ.pop("MESH_FORCE_HASH_EMBED", None)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    installed = root / "mesh-home/models/all-MiniLM-L6-v2-onnx"
    installed.mkdir(parents=True)
    for filename, key in (("model.onnx", "onnx_sha256"), ("tokenizer.json", "tokenizer_sha256")):
        source = safe_member(model_directory, filename)
        expected = protocol["model"][key]
        if source.is_symlink() or digest_file(source) != expected:
            raise ValueError("Pinned local embedding fixture is missing or changed")
        shutil.copyfile(source, installed / filename)
        if digest_file(installed / filename) != expected:
            raise ValueError("Copied local model fixture changed")
    os.environ["MESH_MODEL_SHA256"] = protocol["model"]["onnx_sha256"]
    os.environ["MESH_TOKENIZER_SHA256"] = protocol["model"]["tokenizer_sha256"]
    if not query_only:
        os.environ["MESH_QUERY_MODE"] = "neural"
        os.environ["MESH_REQUIRE_MODEL"] = "1"
        os.environ["MESH_RECALL_FUSION"] = protocol["fusion"]


def import_runtime(root, repository, dependency_directory, cleanup):
    implementation = root / "implementation"
    hashes = freeze_runtime(repository, implementation)
    if dependency_directory:
        sys.path.insert(0, str(dependency_directory))
    sys.path.insert(0, str(implementation))
    import mesh.audit as audit_module
    import mesh.memory as memory_module
    import mesh.store as store_module
    if Path(memory_module.__file__).resolve().parent != implementation / "mesh":
        raise ValueError("Imported another runtime")
    if store_module.DEFAULT_MAX_VECTOR_CACHE_BYTES != 256 * 1024 * 1024:
        raise ValueError("Production default cache budget changed")

    def close_owned_pools():
        audit_module._close_all_audit_connections()
        store_module.close_store_readers()
        for path in list(store_module._CONNECTION_POOL):
            store_module.close_store_connection(path)
        store_module._cleanup_open_lock_fds()

    cleanup.callback(close_owned_pools)
    memory_module._trigger_background_bootstrap = lambda: None
    model, backend = memory_module._get_model()
    if backend != "onnx" or memory_module._backend_to_model_tag(backend, model) != MODEL_TAG:
        raise ValueError("Pinned local MiniLM backend did not become ready")
    return hashes, audit_module, memory_module, store_module


def close_namespace(mesh, audit_module, memory_module, store_module):
    namespace = mesh.namespace
    mesh.close()
    store_module._evict_vector_cache_for_db_path(mesh.store.db_path)
    store_module.close_store_readers(mesh.store.db_path)
    store_module.close_store_connection(mesh.store.db_path)
    memory_module.evict_memory_namespace_cache(namespace)
    audit_module.evict_audit_cache(namespace)


def copy_guarded_corpus(source_run, source_home, namespace, root, records, store_module):
    namespace = store_module.validate_namespace(namespace)
    database = safe_member(source_home, "mesh_" + namespace + ".db")
    source = store_module.sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    destination = store_module.sqlite3.connect(str(root / "mesh-home" / database.name))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def verify_guarded_readiness(mesh, records, memory_module):
    with mesh.store._read_connect() as connection:
        rows = {row["id"]: row for row in connection.execute("SELECT * FROM memories WHERE namespace=?", (mesh.namespace,))}
    accepted = {record["acceptance"]["memory_id"] for record in records if record["acceptance"].get("memory_id")}
    if set(rows) != accepted:
        raise ValueError("Full accepted-memory accounting changed")
    for record in records:
        identifier = record["acceptance"].get("memory_id")
        if not identifier:
            continue
        row = rows[identifier]
        if (digest_text(row["content"]) != record["stored_sha256"]
                or int(row["revision"]) != record["current_revision"]
                or bool(row["local_only"]) != record["private"]
                or bool(row["quarantined"]) != record["quarantined"]
                or row["embedding_model"] != MODEL_TAG or row["embedding"] is None or not row["incarnation"]
                or record.get("current_incarnation", row["incarnation"]) != row["incarnation"]
                or record.get("store_generation", mesh.store.store_generation) != mesh.store.store_generation):
            raise ValueError("Guarded content, revision, privacy or model provenance changed")
    revisions = {identifier: int(row["revision"]) for identifier, row in rows.items()}
    identifiers = list(revisions)
    for offset in range(0, len(identifiers), 128):
        batch = identifiers[offset:offset + 128]
        status = mesh.wait_for_indexed(batch, timeout=0, include_private=True,
                                      expected_revisions={identifier: revisions[identifier] for identifier in batch},
                                      expected_incarnations={identifier: rows[identifier]["incarnation"] for identifier in batch},
                                      expected_store_generation=mesh.store.store_generation,
                                      target_model=MODEL_TAG)
        if status["status"] != "indexed" or len(status["indexed"]) != len(batch):
            raise ValueError("Revision/model-bound readiness failed")
    if mesh.store.embedding_job_stats()["pending_count"]:
        raise ValueError("Guarded checkpoint has pending document indexing")
    return {"source_submissions": len(records), "unique_memories": len(rows),
            "full_fidelity_preserved": True, "revision_model_bound_indexed_memories": len(rows),
            "incarnation_generation_bound_indexed_memories": len(rows),
            "pending_embedding_jobs": 0}


def runtime_parent_identities(records):
    identities = {}
    for record in records:
        identifier = record["acceptance"].get("memory_id")
        if not identifier:
            continue
        revision, incarnation, generation = (record.get(key) for key in (
            "current_revision", "current_incarnation", "store_generation",
        ))
        characters = record.get("stored_characters")
        if (type(revision) is not int or revision < 1 or type(characters) is not int or characters < 1
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value)
                       for value in (incarnation, generation)) or record.get("neural_indexed") is not True):
            raise ValueError("Runtime passage evaluation requires explicit original parent identity and model provenance")
        identity = {"revision": revision, "incarnation": incarnation, "store_generation": generation,
                    "embedding_model": MODEL_TAG, "stored_characters": characters,
                    "local_only": record["private"], "quarantined": record["quarantined"]}
        previous = identities.setdefault(identifier, dict(identity, faithful_document_ids=[]))
        if any(previous[key] != value for key, value in identity.items()):
            raise ValueError("Original parent identity differs across source submissions")
        if record["faithful_public"]:
            previous["faithful_document_ids"].append(record["document_id"])
    return identities


def validate_runtime_parent_results(results, identities, *, allow_passages=True):
    if (not isinstance(results, list) or len(results) > 10
            or len({row["id"] for row in results}) != len(results)):
        raise ValueError("Runtime passage evaluation requires bounded unique original parents")
    for row in results:
        expected = identities.get(row["id"])
        if (not expected or expected["local_only"] or expected["quarantined"]
                or type(row.get("revision")) is not int
                or any(row.get(key) != expected[key] for key in (
                    "revision", "incarnation", "store_generation", "embedding_model", "faithful_document_ids",
                )) or row.get("local_only") is not False or row.get("quarantined") is not False):
            raise ValueError("Exported parent identity, privacy, source IDs or model changed")
        ordinal, spans = row.get("selected_passage_ordinal"), row.get("supporting_spans")
        if ordinal is not None and (not allow_passages or type(ordinal) is not int or ordinal < 0):
            raise ValueError("Paired default export used passage mode or selected an invalid passage")
        if not isinstance(spans, list) or len(spans) != 1:
            raise ValueError("Original parent supporting span is missing or unbounded")
        for span in spans:
            if (span.get("memory_id") != row["id"] or type(span.get("revision")) is not int
                    or any(span.get(key) != expected[key] for key in ("revision", "incarnation", "store_generation"))
                    or type(span.get("start")) is not int or type(span.get("end")) is not int
                    or not 0 <= span["start"] < span["end"] <= expected["stored_characters"]):
                raise ValueError("Supporting span differs from the original parent identity or bounds")


def load_fresh_default_pair(directory, repository, source_run, source_manifest, queries, records):
    directory = isolated_artifact_path(directory, repository, must_exist=True)
    manifest_path = safe_member(directory, "run-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_exports(directory, manifest)
    observations = manifest.get("default_query_backend_observations", {})
    if (manifest.get("status") != "complete" or manifest.get("all_queries_accounted") is not True
            or manifest.get("query_target") != len(queries) or manifest.get("smoke_limit_cases")
            or manifest.get("passage_index_enabled") is not False
            or manifest.get("labels_in_ranking_inputs") is not False
            or manifest.get("saved_query_vectors_used") is not False
            or manifest.get("document_inference_invoked") is not False
            or manifest.get("query_encoding_mode") != "auto (unset default)"
            or manifest.get("source_run_manifest_sha256") != digest_file(source_run / "run-manifest.json")
            or any(manifest.get(key) != source_manifest[key] for key in (
                "dataset", "dataset_manifest_sha256", "protocol_sha256", "runtime_files", "model_revision",
                "model_file_sha256", "query_model_space_tag", "ingestion",
            ))
            or manifest["output_sha256"].get("ingestion-fidelity.jsonl") != source_manifest["output_sha256"]["ingestion-fidelity.jsonl"]
            or observations.get("normal_sdk_query_encoding_calls") != len(queries)
            or observations.get("all_query_model_tags_match_indexed_model") is not True
            or observations.get("all_query_dimensions_384") is not True
            or observations.get("document_inference_attempts") != 0):
        raise ValueError("Runtime passage pairing requires the complete hash-verified fresh-default condition")
    results_path = safe_member(directory, "results-default.jsonl")
    if manifest["output_sha256"].get(results_path.name) != digest_file(results_path):
        raise ValueError("Fresh-default ranks are not hash-accounted")
    rows = list(read_jsonl(results_path))
    by_query = {row["query_id"]: row for row in rows}
    if len(rows) != len(queries) or len(by_query) != len(queries) or set(by_query) != {query["id"] for query in queries}:
        raise ValueError("Fresh-default pairing omits or duplicates selected queries")
    identities = runtime_parent_identities(records)
    for query in queries:
        row = by_query[query["id"]]
        if (row.get("corpus_id") != query.get("corpus", "complete") or row.get("candidate_cap") is not None
                or row.get("query_embedding_model") != MODEL_TAG or "original_sdk_top10" in row):
            raise ValueError("Fresh-default paired query condition changed")
        validate_runtime_parent_results(row["final_sdk_top10"], identities, allow_passages=False)
    return {identifier: row["final_sdk_top10"] for identifier, row in by_query.items()}, {
        "fresh_default_run_manifest_sha256": digest_file(manifest_path),
        "fresh_default_results_sha256": digest_file(results_path),
    }


class RuntimePassageResources:
    """Content-free loaded diagnostics; CI separately enforces process-tree caps."""
    def __init__(self):
        import psutil
        self.process = psutil.Process()
        self.started, self.cpu_started, self.rss_max = time.monotonic(), time.process_time(), 0
        self.sample()

    def sample(self):
        rss = int(self.process.memory_info().rss)
        self.rss_max = max(self.rss_max, rss)
        return rss

    def summary(self):
        self.sample()
        return {"wall_seconds": time.monotonic() - self.started,
                "process_cpu_seconds": time.process_time() - self.cpu_started,
                "sampled_owned_process_rss_bytes_max": self.rss_max}


def guarded_database_fingerprint(source_home, namespace):
    database = safe_member(source_home, "mesh_" + namespace + ".db")
    wal = safe_member(source_home, database.name + "-wal", must_exist=False)
    return {"database_sha256": digest_file(database), "wal_sha256": digest_file(wal) if wal.exists() else None}


def freeze_runtime_passage_snapshot(mesh, records, snapshot_root, store_module):
    source_home = isolated_source_home(str(mesh.store.db_path.parent))
    snapshot_root = isolated_artifact_path(snapshot_root, must_exist=True)
    home = checked_path(snapshot_root / "mesh-home", root=snapshot_root, must_exist=True, directory=True)
    namespace = store_module.validate_namespace(mesh.namespace)
    if safe_member(home, "mesh_" + namespace + ".db", must_exist=False).exists():
        raise ValueError("Refusing to overwrite a frozen runtime passage snapshot")
    identities = runtime_parent_identities(records)
    if {row["store_generation"] for row in identities.values()} != {mesh.store.store_generation}:
        raise ValueError("Runtime passage snapshot original generation changed")
    copy_guarded_corpus(None, source_home, namespace, snapshot_root, records, store_module)
    database = safe_member(home, "mesh_" + namespace + ".db")
    connection = store_module.sqlite3.connect(database)
    try:
        # A read-only open of a WAL-mode backup can create an empty WAL. Finish
        # this owned snapshot before hashing; query copies restore runtime WAL.
        if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise ValueError("Runtime passage snapshot journal finalization failed")
    finally:
        connection.close()
    fingerprint = guarded_database_fingerprint(home, namespace)
    if fingerprint["wal_sha256"] is not None:
        raise ValueError("Runtime passage snapshot still has a WAL")
    return {"namespace": namespace, "store_generation": mesh.store.store_generation,
            "original_parent_count": len(identities), "fingerprint": fingerprint}


def runtime_passage_snapshot_metadata(manifest, records, source_queries=None):
    snapshot = manifest.get("runtime_passage_snapshot", {})
    if (not isinstance(snapshot, dict) or set(snapshot) != {"configuration", "copy_method", "frozen_before_queries", "immutable", "corpora"}
            or snapshot.get("configuration") != PASSAGE_SNAPSHOT_CONFIGURATION
            or snapshot.get("copy_method") != "sqlite_backup" or snapshot.get("frozen_before_queries") is not True
            or snapshot.get("immutable") is not True or not isinstance(snapshot.get("corpora"), dict)):
        raise ValueError("Runtime passage snapshot is missing or was not frozen before queries")
    identities = runtime_parent_identities(records)
    expected = defaultdict(set)
    for record in records:
        expected[record["corpus_id"]]
        if record["acceptance"].get("memory_id"):
            expected[record["corpus_id"]].add(record["acceptance"]["memory_id"])
    namespaces = defaultdict(set)
    for query in (source_queries or {}).values():
        namespaces[query["corpus_id"]].add(query["namespace"])
    if (set(snapshot["corpora"]) != set(expected)
            or source_queries is not None and set(namespaces) != set(expected)):
        raise ValueError("Runtime passage snapshot corpus accounting changed")
    for corpus, identifiers in expected.items():
        if corpus != "complete" and not re.fullmatch(r"corpus-[0-9]{3}\.jsonl", corpus):
            raise ValueError("Runtime passage snapshot corpus name is invalid")
        item = snapshot["corpora"][corpus]
        generations = {identities[identifier]["store_generation"] for identifier in identifiers}
        if (not isinstance(item, dict) or set(item) != {"namespace", "store_generation", "original_parent_count", "fingerprint"}
                or type(item.get("namespace")) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", item["namespace"])
                or source_queries is not None and namespaces[corpus] != {item["namespace"]}
                or generations != {item.get("store_generation")}
                or type(item.get("original_parent_count")) is not int or item["original_parent_count"] != len(identifiers)):
            raise ValueError("Runtime passage snapshot namespace or original generation changed")
        fingerprint = item["fingerprint"]
        if (not isinstance(fingerprint, dict) or set(fingerprint) != {"database_sha256", "wal_sha256"}
                or any(type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for key, value in fingerprint.items() if key != "wal_sha256" or value is not None)):
            raise ValueError("Runtime passage snapshot fingerprint is incomplete")
    return snapshot


def load_runtime_passage_snapshot(directory, home, repository, source_manifest, source_hash, pairing, records, source_queries):
    directory = isolated_artifact_path(directory, repository, must_exist=True)
    manifest_path = safe_member(directory, "run-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_exports(directory, manifest)
    if (manifest.get("status") != "complete" or manifest.get("all_queries_accounted") is not True
            or manifest.get("passage_index_enabled") is not True or manifest.get("guarded_source_immutable") is not True
            or manifest.get("source_run_manifest_sha256") != source_hash
            or manifest.get("source_fidelity_sha256") != source_manifest["output_sha256"]["ingestion-fidelity.jsonl"]
            or any(manifest.get(key) != source_manifest.get(key) for key in (
                "dataset", "query_target", "dataset_manifest_sha256", "protocol_sha256", "runtime_files",
                "model_revision", "model_file_sha256", "query_model_space_tag"))
            or any(manifest.get("runtime_passage_index", {}).get(key) != value for key, value in pairing.items())):
        raise ValueError("Runtime passage snapshot source or fresh-default pairing changed")
    validate_runtime_passage_schema(manifest.get("schema_version"))
    snapshot = runtime_passage_snapshot_metadata(manifest, records, source_queries)
    if home is None:
        retained = isolated_source_home(manifest["temporary_mesh_home"])
        home = retained.parent / PASSAGE_SNAPSHOT_FOLDER / "mesh-home"
    home = isolated_source_home(str(home))
    for item in snapshot["corpora"].values():
        if guarded_database_fingerprint(home, item["namespace"]) != item["fingerprint"]:
            raise ValueError("Runtime passage snapshot database or WAL changed")
    return {"directory": directory, "home": home, "repository": repository, "manifest": manifest, "metadata": snapshot,
            "run_manifest_sha256": digest_file(manifest_path)}


def runtime_passage_snapshot_reuse_metadata(manifest, records):
    reuse = manifest.get("runtime_passage_snapshot_reuse", {})
    if (not isinstance(reuse, dict) or set(reuse) != {"source_run_manifest_sha256", "snapshot", "source_immutable", "readiness_verified_without_inference"}
            or type(reuse.get("source_run_manifest_sha256")) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", reuse["source_run_manifest_sha256"])
            or reuse.get("source_immutable") is not True or reuse.get("readiness_verified_without_inference") is not True
            or manifest.get("document_inference_invoked") is not False):
        raise ValueError("Runtime passage snapshot reuse provenance or inference accounting changed")
    snapshot = runtime_passage_snapshot_metadata({"runtime_passage_snapshot": reuse["snapshot"]}, records)
    readiness = manifest.get("runtime_passage_readiness", {})
    if set(readiness) != set(snapshot["corpora"]):
        raise ValueError("Runtime passage snapshot reuse readiness is incomplete")
    for corpus, item in snapshot["corpora"].items():
        actual = readiness[corpus]
        if (actual.get("status") != "indexed" or actual.get("target_model") != MODEL_TAG
                or actual.get("target_representation") != PASSAGE_REPRESENTATION
                or actual.get("representation_bound_indexed_memories") != item["original_parent_count"]
                or any(type(actual.get(key)) is not int or actual[key] != 0 for key in (
                    "completed_batches", "scheduled_jobs", "pending_embedding_jobs", "primary_document_inference_attempts",
                    "passage_raw_batch_encoding_calls", "passage_encoded_rows"))):
            raise ValueError("Runtime passage snapshot reuse performed work or failed readiness")
    return reuse


def copy_runtime_passage_snapshot(snapshot, corpus, namespace, root, records, store_module):
    home = isolated_source_home(str(snapshot["home"]))
    root = isolated_artifact_path(root, must_exist=True)
    checked_path(root / "mesh-home", root=root, must_exist=True, directory=True)
    namespace = store_module.validate_namespace(namespace)
    item = snapshot["metadata"]["corpora"][corpus]
    if namespace != item["namespace"] or guarded_database_fingerprint(home, namespace) != item["fingerprint"]:
        raise ValueError("Runtime passage snapshot database, WAL or namespace changed before copying")
    copy_guarded_corpus(snapshot["directory"], home, namespace, root, records, store_module)
    if guarded_database_fingerprint(home, namespace) != item["fingerprint"]:
        raise ValueError("Runtime passage snapshot database or WAL changed while copying")


def verify_runtime_passage_snapshot(snapshot):
    home = isolated_source_home(str(snapshot["home"]))
    directory = isolated_artifact_path(snapshot["directory"], snapshot["repository"], must_exist=True)
    if digest_file(safe_member(directory, "run-manifest.json")) != snapshot["run_manifest_sha256"]:
        raise ValueError("Runtime passage snapshot manifest changed")
    verify_exports(directory, snapshot["manifest"])
    for item in snapshot["metadata"]["corpora"].values():
        if guarded_database_fingerprint(home, item["namespace"]) != item["fingerprint"]:
            raise ValueError("Runtime passage snapshot database or WAL changed")


def verify_runtime_passage_readiness(mesh, records, memory_module):
    identities = runtime_parent_identities(records)
    if {row["store_generation"] for row in identities.values()} != {mesh.store.store_generation}:
        raise ValueError("Original store generation changed before passage readiness verification")
    identifiers = list(identities)
    for offset in range(0, len(identifiers), 128):
        batch = identifiers[offset:offset + 128]
        status = mesh.wait_for_indexed(
            batch, timeout=0, include_private=True, target_model=MODEL_TAG, target_representation=PASSAGE_REPRESENTATION,
            expected_revisions={identifier: identities[identifier]["revision"] for identifier in batch},
            expected_incarnations={identifier: identities[identifier]["incarnation"] for identifier in batch},
            expected_store_generation=mesh.store.store_generation)
        if status["status"] != "indexed" or set(status["indexed"]) != set(batch):
            raise ValueError("Original-parent passage representation/model readiness failed")
    verify_guarded_readiness(mesh, records, memory_module)
    return len(identities)


def reuse_runtime_passages(mesh, records, memory_module, readiness, resources):
    identities = runtime_parent_identities(records)
    model, backend = memory_module._get_model()
    if (backend != "onnx" or not isinstance(model, memory_module._DirectOnnxEmbedding)
            or memory_module._backend_to_model_tag(backend, model) != MODEL_TAG
            or memory_module.PASSAGE_FORMAT_VERSION != PASSAGE_REPRESENTATION
            or os.environ.get("MESH_PASSAGE_INDEX") != "1" or os.environ.get("MESH_BG_ONNX") != "0"):
        raise ValueError("Snapshot passage reuse requires the pinned direct model and manual opt-in")
    readiness.update(status="running", target_model=MODEL_TAG, target_representation=PASSAGE_REPRESENTATION,
                     eligible_original_parent_memories=len(identities), representation_bound_indexed_memories=0,
                     batch_limit=PASSAGE_BATCH_LIMIT, maximum_batches=max(3, len(identities) * 3), completed_batches=0,
                     scheduled_jobs=0, pending_embedding_jobs=0, passage_raw_batch_encoding_calls=0,
                     passage_encoded_rows=0, primary_document_inference_attempts=0)
    started, cpu_started = time.monotonic(), time.process_time()
    encoder, worker, raw = memory_module._encode_texts_with_tag, memory_module._process_reembed_batch, model._encode_raw_batches
    had_raw_override = "_encode_raw_batches" in vars(model)

    def forbid_primary(*_args, **_kwargs):
        readiness["primary_document_inference_attempts"] += 1
        raise RuntimeError("Snapshot passage reuse attempted primary document inference")

    def forbid_passages(*_args, **_kwargs):
        readiness["passage_raw_batch_encoding_calls"] += 1
        raise RuntimeError("Snapshot passage reuse attempted passage reinference")

    memory_module._encode_texts_with_tag = forbid_primary
    memory_module._process_reembed_batch = model._encode_raw_batches = forbid_passages
    try:
        count = verify_runtime_passage_readiness(mesh, records, memory_module)
        readiness.update(status="indexed", representation_bound_indexed_memories=count)
    except BaseException:
        readiness["status"] = "failed"
        raise
    finally:
        memory_module._encode_texts_with_tag, memory_module._process_reembed_batch = encoder, worker
        if had_raw_override:
            model._encode_raw_batches = raw
        else:
            del model._encode_raw_batches
        readiness["resource_diagnostics"] = {"wall_seconds": time.monotonic() - started,
                                              "process_cpu_seconds": time.process_time() - cpu_started,
                                              "sampled_owned_process_rss_bytes_max": resources.summary()["sampled_owned_process_rss_bytes_max"]}


def index_runtime_passages(mesh, records, memory_module, readiness, resources):
    identities = runtime_parent_identities(records)
    if {row["store_generation"] for row in identities.values()} != {mesh.store.store_generation}:
        raise ValueError("Original store generation changed before passage backfill")
    model, backend = memory_module._get_model()
    if (backend != "onnx" or not isinstance(model, memory_module._DirectOnnxEmbedding)
            or memory_module._backend_to_model_tag(backend, model) != MODEL_TAG
            or memory_module.PASSAGE_FORMAT_VERSION != PASSAGE_REPRESENTATION
            or os.environ.get("MESH_PASSAGE_INDEX") != "1" or os.environ.get("MESH_BG_ONNX") != "0"):
        raise ValueError("Runtime passages require the direct pinned local model and an explicit manual opt-in")
    maximum_batches = max(3, len(identities) * 3)
    readiness.update(status="running", target_model=MODEL_TAG, target_representation=PASSAGE_REPRESENTATION,
                     eligible_original_parent_memories=len(identities), representation_bound_indexed_memories=0,
                     batch_limit=PASSAGE_BATCH_LIMIT, maximum_batches=maximum_batches, completed_batches=0,
                     scheduled_jobs=0, pending_embedding_jobs=0, passage_raw_batch_encoding_calls=0,
                     passage_encoded_rows=0, primary_document_inference_attempts=0)
    started, cpu_started = time.monotonic(), time.process_time()
    original_encoder, original_raw = memory_module._encode_texts_with_tag, model._encode_raw_batches
    had_raw_override = "_encode_raw_batches" in vars(model)

    def forbid_primary_inference(*_args, **_kwargs):
        readiness["primary_document_inference_attempts"] += 1
        raise RuntimeError("Runtime passage backfill attempted to replace an original primary embedding")

    def observe_passages(texts, *args, **kwargs):
        readiness["passage_raw_batch_encoding_calls"] += 1
        readiness["passage_encoded_rows"] += len(texts)
        return original_raw(texts, *args, **kwargs)

    memory_module._encode_texts_with_tag, model._encode_raw_batches = forbid_primary_inference, observe_passages
    try:
        no_progress = 0
        for _ in range(maximum_batches):
            readiness["scheduled_jobs"] += mesh.store.backfill_embedding_jobs(
                PASSAGE_BATCH_LIMIT, target_model=MODEL_TAG, target_representation=PASSAGE_REPRESENTATION)
            before = int(mesh.store.embedding_job_stats()["pending_count"])
            readiness["pending_embedding_jobs"] = before
            if not before:
                break
            jobs = mesh.store.list_embedding_jobs(limit=PASSAGE_BATCH_LIMIT)
            if not jobs:
                raise RuntimeError("Runtime passage jobs are deferred; readiness cannot be declared")
            for job in jobs:
                expected = identities.get(job["memory_id"])
                if (not expected or any(job[key] != expected[key] for key in ("revision", "incarnation", "store_generation"))
                        or job["target_model"] != MODEL_TAG or job["target_representation"] != PASSAGE_REPRESENTATION):
                    raise ValueError("Runtime passage job differs from its original parent or representation")
            hints = [memory_module._ReembedHint(str(mesh.store.db_path.parent), mesh.namespace,
                                               job["memory_id"], job["revision"], job["target_model"], job["attempts"],
                                               incarnation=job["incarnation"], store_generation=job["store_generation"],
                                               target_representation=job["target_representation"]) for job in jobs]
            memory_module._process_reembed_batch(hints)
            after = int(mesh.store.embedding_job_stats()["pending_count"])
            readiness.update(completed_batches=readiness["completed_batches"] + 1, pending_embedding_jobs=after)
            resources.sample()
            no_progress = no_progress + 1 if after >= before else 0
            if no_progress >= 3:
                raise RuntimeError("Bounded runtime passage drain made no progress")
        else:
            raise RuntimeError("Runtime passage drain exceeded its original-parent batch bound")
        verify_runtime_passage_readiness(mesh, records, memory_module)
        readiness.update(status="indexed", representation_bound_indexed_memories=len(identities))
    except BaseException:
        readiness["status"] = "failed"
        raise
    finally:
        memory_module._encode_texts_with_tag = original_encoder
        if had_raw_override:
            model._encode_raw_batches = original_raw
        else:
            del model._encode_raw_batches
        readiness["resource_diagnostics"] = {"wall_seconds": time.monotonic() - started,
                                              "process_cpu_seconds": time.process_time() - cpu_started,
                                              "sampled_owned_process_rss_bytes_max": resources.summary()["sampled_owned_process_rss_bytes_max"]}


def fresh_queries(mesh, queries, records, streams, metadata, memory_module, *,
                  original_sdk_top10=None, resource_stream=None, resources=None, experiment=None):
    runtime_passages = original_sdk_top10 is not None
    if runtime_passages and (resource_stream is None or resources is None):
        raise ValueError("Runtime passage queries require resource accounting")
    identities = runtime_parent_identities(records) if runtime_passages else None
    by_memory = defaultdict(list)
    for record in records:
        if record["acceptance"].get("memory_id"):
            by_memory[record["acceptance"]["memory_id"]].append(record)
    if experiment is not None:
        experiment.before_namespace(mesh, queries, records, memory_module)
    original = memory_module._embed_query_with_tag
    original_document_encoder = memory_module._encode_texts_with_tag
    original_worker = memory_module._process_reembed_batch
    observations = []
    document_attempts = []

    def observe_query(query):
        vector, tag = original(query)
        if tag != MODEL_TAG:
            raise RuntimeError("Default query backend did not use the indexed neural model space")
        observations.append({"query_sha256": digest_text(query), "embedding_model": tag, "dimension": len(vector)})
        return vector, tag

    def forbid_document_inference(*_args, **_kwargs):
        document_attempts.append(True)
        raise RuntimeError("Query-only verification attempted document inference")

    memory_module._embed_query_with_tag = observe_query
    memory_module._encode_texts_with_tag = forbid_document_inference
    memory_module._process_reembed_batch = forbid_document_inference
    diagnostics = metadata.setdefault("first_six_gross_diagnostics", [])
    observation_key = "runtime_passage_query_backend_observations" if runtime_passages else "default_query_backend_observations"
    previous = metadata.get(observation_key, {})
    previous_calls = previous.get("normal_sdk_query_encoding_calls", 0)
    try:
        for index, query in enumerate(queries, 1):
            started = time.perf_counter()
            cpu_started = time.process_time()
            before = len(observations)
            results = mesh.recall(query["query"], n=10, include_private=False, include_quarantined=False)
            elapsed_ms = (time.perf_counter() - started) * 1000
            if len(observations) != before + 1:
                raise ValueError("Default SDK call did not account for one normal query encoding")
            exported = decorate_results(results, by_memory)
            if runtime_passages:
                validate_runtime_parent_results(exported, identities)
            payload = {"query_id": query["id"], "corpus_id": query.get("corpus", "complete"),
                       "candidate_cap": None, "query_embedding_model": observations[-1]["embedding_model"],
                       "final_sdk_top10": exported}
            if runtime_passages:
                payload["original_sdk_top10"] = original_sdk_top10[query["id"]]
                append_jsonl(resource_stream, {"query_ordinal": previous_calls + index, "elapsed_ms": elapsed_ms,
                                               "process_cpu_ms": (time.process_time() - cpu_started) * 1000,
                                               "sampled_owned_process_rss_bytes": resources.sample()})
            if experiment is not None:
                experiment.observe(query, results, payload, elapsed_ms, (time.process_time() - cpu_started) * 1000)
            append_jsonl(streams[96], payload)
            if previous_calls + index <= 6:
                diagnostics.append({"query_ordinal": previous_calls + index, "elapsed_ms": elapsed_ms, "result_count": len(results)})
            if index == 6 or index % 10 == 0:
                print(json.dumps({"completed_queries": index, "query_target": len(queries)}), flush=True)
        metadata[observation_key] = {
            "normal_sdk_query_encoding_calls": previous_calls + len(observations),
            "all_query_model_tags_match_indexed_model": previous.get("all_query_model_tags_match_indexed_model", True) and all(row["embedding_model"] == MODEL_TAG for row in observations),
            "all_query_dimensions_384": previous.get("all_query_dimensions_384", True) and all(row["dimension"] == 384 for row in observations),
            "document_inference_attempts": previous.get("document_inference_attempts", 0) + len(document_attempts),
        }
    finally:
        memory_module._embed_query_with_tag = original
        memory_module._encode_texts_with_tag = original_document_encoder
        memory_module._process_reembed_batch = original_worker
    if experiment is not None:
        experiment.after_namespace(mesh, queries, records, memory_module)


def run_retrieval(args, *, experiment=None, passage_snapshot=None):
    runtime_passages = getattr(args, "runtime_passages", False)
    if passage_snapshot is not None and (runtime_passages is not True or experiment is None or args.command != "query-only"):
        raise ValueError("Passage snapshot reuse requires an explicit native passage query experiment")
    if runtime_passages and (args.command != "query-only" or not getattr(args, "fresh_default_run", None)):
        raise ValueError("Runtime passages are a query-only opt-in paired with an explicit fresh-default run")
    if getattr(args, "fresh_default_run", None) and not runtime_passages:
        raise ValueError("A fresh-default pair requires the runtime passage opt-in")
    repository = checked_path(args.repository, must_exist=True, directory=True)
    data = checked_path(args.dataset_directory, must_exist=True, directory=True)
    fixture_path = safe_member(data, "manifest.json")
    fixture = verify_manifest(fixture_path)
    queries = list(read_jsonl(safe_member(data, "queries-selected.jsonl")))
    query_ids = [query["id"] for query in queries]
    if len(queries) != fixture["query_count"] or len(set(query_ids)) != len(queries):
        raise ValueError("Selected query accounting differs from preparation")
    protocol_path = checked_path(args.protocol, must_exist=True, directory=False)
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if protocol["model"]["name"] != "sentence-transformers/all-MiniLM-L6-v2" or protocol["candidate_caps"] != [64, 96]:
        raise ValueError("Unsupported model or undeclared candidate condition")
    if args.dataset == "longmemeval" and (fixture["query_count"] != 6 or fixture["corpus_count"] != 191):
        raise ValueError("LongMemEval requires the inspected six-case / 191-turn fixture")
    query_only = args.command == "query-only"
    source_manifest = None
    records = None
    source_home = None
    source_queries = {}
    original_sdk_top10, pairing = None, None
    snapshot = None
    if query_only:
        args.source_run = isolated_artifact_path(args.source_run, repository, must_exist=True)
        source_path = safe_member(args.source_run, "run-manifest.json")
        source_manifest = json.loads(source_path.read_text(encoding="utf-8"))
        verify_exports(args.source_run, source_manifest)
        if (source_manifest["status"] != "complete" or source_manifest.get("smoke_limit_cases")
                or source_manifest["dataset"] != args.dataset or source_manifest["embedding_model"] != "minilm"
                or source_manifest["query_model_space_tag"] != MODEL_TAG
                or source_manifest["dataset_manifest_sha256"] != digest_file(fixture_path)
                or source_manifest["protocol_sha256"] != digest_file(protocol_path)
                or source_manifest["model_revision"] != protocol["model"]["revision"]
                or source_manifest["model_file_sha256"] != {key: protocol["model"][key] for key in ("onnx_sha256", "tokenizer_sha256")}):
            raise ValueError("Source is not the fully accounted guarded dataset/model checkpoint")
        source_home = isolated_source_home(str(args.source_home or source_manifest["temporary_mesh_home"]))
        fidelity_path = safe_member(args.source_run, "ingestion-fidelity.jsonl")
        if digest_file(fidelity_path) != source_manifest["output_sha256"][fidelity_path.name]:
            raise ValueError("Guarded fidelity export changed")
        records = list(read_jsonl(fidelity_path))
        if len(records) != fixture["corpus_count"]:
            raise ValueError("Query-only verification needs every guarded corpus submission")
        vectors = list(read_jsonl(safe_member(args.source_run, "query-vectors.jsonl")))
        source_queries = {row["query_id"]: row for row in vectors}
        if len(vectors) != len(queries) or len(source_queries) != len(queries) or set(source_queries) != set(query_ids):
            raise ValueError("Source query identities are incomplete or duplicated")
        for query in queries:
            source_query = source_queries[query["id"]]
            if (source_query["query"] != query["query"] or source_query["embedding_model"] != MODEL_TAG
                    or source_query["corpus_id"] != query.get("corpus", "complete")):
                raise ValueError("Fresh query text, corpus or model differs from its baseline")
        if args.dataset != "longmemeval" and not args.source_namespace:
            raise ValueError("An explicit guarded source namespace is required")
        if runtime_passages:
            if source_manifest.get("passage_index_enabled") is not False:
                raise ValueError("Runtime passages require an explicitly default-off guarded source")
            original_sdk_top10, pairing = load_fresh_default_pair(
                args.fresh_default_run, repository, args.source_run, source_manifest, queries, records)
            if passage_snapshot is not None:
                snapshot = load_runtime_passage_snapshot(
                    passage_snapshot["directory"], passage_snapshot.get("home"), repository, source_manifest,
                    digest_file(source_path), pairing, records, source_queries)
    output = isolated_artifact_path(args.output, repository)
    if output.exists():
        raise ValueError("Refusing to overwrite an existing run")
    output.mkdir(parents=True)
    metadata = {
        "status": "running", "dataset": args.dataset, "query_target": len(queries),
        "started_at_utc": datetime.now(timezone.utc).isoformat(), "dataset_manifest_sha256": digest_file(fixture_path),
        "dataset_manifest": fixture, "protocol_sha256": digest_file(protocol_path),
        "harness_sha256": digest_file(Path(__file__)), "embedding_model": "minilm",
        "model_revision": protocol["model"]["revision"], "query_model_space_tag": MODEL_TAG,
        "model_file_sha256": {key: protocol["model"][key] for key in ("onnx_sha256", "tokenizer_sha256")},
        "cache_condition_bytes": 256 * 1024 * 1024, "labels_in_ranking_inputs": False,
        "passage_index_enabled": bool(runtime_passages),
        "scope": "Current-source fresh default query-only verification on existing full guarded corpora." if query_only else ("Current-source complete published corpus; fixed test split; no competitor or performance claim." if args.dataset in {"nfcorpus", "scifact"} else "Current-source inspected bounded regression; no full-dataset generalization claim."),
        "baseline_provenance": "Independent default-SDK fresh-query verification; saved-vector replays and frozen quality are separate." if query_only else "Same-model dense ablation on a bounded fixture; no pre-change or competitor comparison.",
        "saved_query_vectors_used": False,
        "query_encoding_mode": "auto (unset default)" if query_only else "explicit neural",
        "ranking_condition": "SDK defaults, n=10, public only, default access recording enabled" if query_only else "Declared C64/C96; n=10; public only; access recording disabled",
        "timing_scope": "Loaded development diagnostics only; no quiet latency, throughput, p95 or SLO claim.",
        "smoke_limit_cases": None, "inspected_case_count": len(queries) if args.dataset == "longmemeval" else None,
        "ingestion": source_manifest["ingestion"] if query_only else {}, "cache_observations": {},
    }
    if source_manifest:
        metadata["source_run_manifest_sha256"] = digest_file(args.source_run / "run-manifest.json")
        metadata["source_fidelity_sha256"] = source_manifest["output_sha256"]["ingestion-fidelity.jsonl"]
        metadata["document_inference_invoked"] = False
    if runtime_passages:
        metadata.update(scope="Current-source actual runtime passage index on original guarded parents; separate optional quality evaluation.",
                        baseline_provenance="Original-parent runtime passages paired with the hash-verified ordinary fresh-default SDK export.",
                        runtime_passage_index=dict(pairing, representation=PASSAGE_REPRESENTATION,
                                                  backend="direct-onnx", manual_worker=True),
                        runtime_passage_readiness={}, guarded_source_database_fingerprints={}, guarded_source_immutable=False)
        if snapshot is not None:
            metadata["runtime_passage_snapshot_reuse"] = {
                "source_run_manifest_sha256": snapshot["run_manifest_sha256"], "snapshot": snapshot["metadata"],
                "source_immutable": False, "readiness_verified_without_inference": False}
    write_json(output / "run-manifest.json", metadata)
    started = time.monotonic()
    caps = [96] if query_only else protocol["candidate_caps"]
    streams = {cap: (output / ("results-default.jsonl" if query_only else f"results-C{cap}.jsonl")).open("w", encoding="utf-8", newline="\n") for cap in caps}
    fidelity = (output / "ingestion-fidelity.jsonl").open("w", encoding="utf-8", newline="\n")
    vectors = None if query_only else (output / "query-vectors.jsonl").open("w", encoding="utf-8", newline="\n")
    resource_stream = (output / "query-resources.jsonl").open("w", encoding="utf-8", newline="\n") if runtime_passages else None
    resources = None
    try:
        with contextlib.ExitStack() as cleanup:
            if runtime_passages:
                resources = RuntimePassageResources()
            if args.retain_guarded_home:
                root = Path(tempfile.mkdtemp(prefix="mesh-frozen-quality-")).resolve(strict=True)
                metadata["temporary_mesh_home"] = str(root / "mesh-home")
            else:
                root = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="mesh-frozen-quality-"))).resolve(strict=True)
            snapshot_root = None
            if runtime_passages and args.retain_guarded_home and snapshot is None:
                snapshot_root = root / PASSAGE_SNAPSHOT_FOLDER
                (snapshot_root / "mesh-home").mkdir(parents=True)
                metadata["runtime_passage_snapshot"] = {
                    "configuration": PASSAGE_SNAPSHOT_CONFIGURATION, "copy_method": "sqlite_backup",
                    "frozen_before_queries": True, "immutable": False, "corpora": {}}
            install_model(root, args.model_directory, protocol, query_only=query_only, repository=repository)
            if runtime_passages:
                os.environ["MESH_PASSAGE_INDEX"] = "1"
            if experiment is not None:
                experiment.configure()
            hashes, audit_module, memory_module, store_module = import_runtime(
                root, repository, checked_path(args.dependency_directory, must_exist=True, directory=True) if args.dependency_directory else None, cleanup)
            metadata["runtime_files"] = hashes
            if source_manifest and hashes != source_manifest["runtime_files"]:
                raise ValueError("Fresh-default runtime differs from its current guarded baseline")
            metadata["schema_version"] = (validate_runtime_passage_schema(store_module.SCHEMA_VERSION)
                                          if runtime_passages else store_module.SCHEMA_VERSION)
            metadata["dependencies"] = {name: importlib.metadata.version(name) for name in ("numpy", "onnxruntime", "tokenizers", "apsw")}
            groups = [(query["corpus"], [query]) for query in queries] if args.dataset == "longmemeval" else [("complete", queries)]
            records_by_corpus = defaultdict(list)
            if query_only:
                for record in records:
                    records_by_corpus[record["corpus_id"]].append(record)
                if set(records_by_corpus) != {corpus_id for corpus_id, _group in groups}:
                    raise ValueError("Fresh-default corpus accounting differs from its baseline")
                metadata["guarded_readiness"] = {}
            completed = 0
            for index, (corpus_id, group) in enumerate(groups):
                namespace = args.source_namespace if query_only else f"bounded-{args.dataset}-{index:03d}"
                if query_only:
                    expected_namespace = source_queries[group[0]["id"]]["namespace"]
                    if args.dataset == "longmemeval":
                        namespace = expected_namespace
                    if any(source_queries[query["id"]]["namespace"] != namespace for query in group):
                        raise ValueError("Fresh queries were assigned to another guarded namespace")
                    records = records_by_corpus[corpus_id]
                    if runtime_passages:
                        source_fingerprint = guarded_database_fingerprint(source_home, namespace)
                        metadata["guarded_source_database_fingerprints"][corpus_id] = source_fingerprint
                        if snapshot is not None and snapshot["manifest"]["guarded_source_database_fingerprints"].get(corpus_id) != source_fingerprint:
                            raise ValueError("Guarded source fingerprint differs from the qualified passage snapshot")
                    if snapshot is not None:
                        copy_runtime_passage_snapshot(snapshot, corpus_id, namespace, root, records, store_module)
                    else:
                        copy_guarded_corpus(args.source_run, source_home, namespace, root, records, store_module)
                mesh = memory_module.Mesh(namespace=namespace, agent_id="offline-retrieval-verification")
                try:
                    if mesh.store.db_path.parent.resolve() != root / "mesh-home":
                        raise ValueError("Store escaped its isolated home")
                    metadata["sqlite"] = verify_sqlite(mesh, store_module)
                    if query_only:
                        metadata["guarded_readiness"][corpus_id] = verify_guarded_readiness(mesh, records, memory_module)
                        for record in records:
                            append_jsonl(fidelity, record)
                        if runtime_passages:
                            readiness = metadata["runtime_passage_readiness"].setdefault(corpus_id, {})
                            if snapshot is not None:
                                reuse_runtime_passages(mesh, records, memory_module, readiness, resources)
                            else:
                                index_runtime_passages(mesh, records, memory_module, readiness, resources)
                            metadata["document_inference_invoked"] |= bool(readiness["passage_encoded_rows"])
                            if snapshot_root is not None:
                                # Freeze before default recall can update access statistics.
                                metadata["runtime_passage_snapshot"]["corpora"][corpus_id] = freeze_runtime_passage_snapshot(
                                    mesh, records, snapshot_root, store_module)
                        fresh_queries(mesh, group, records, streams, metadata, memory_module,
                                      original_sdk_top10=original_sdk_top10, resource_stream=resource_stream,
                                      resources=resources, experiment=experiment)
                        if runtime_passages and guarded_database_fingerprint(source_home, namespace) != source_fingerprint:
                            raise ValueError("Guarded source database changed during runtime passage evaluation")
                        if snapshot is not None:
                            verify_runtime_passage_snapshot(snapshot)
                        if snapshot_root is not None:
                            expected = metadata["runtime_passage_snapshot"]["corpora"][corpus_id]["fingerprint"]
                            if guarded_database_fingerprint(snapshot_root / "mesh-home", namespace) != expected:
                                raise ValueError("Prequery passage snapshot changed during comparator queries")
                    else:
                        corpus_path = safe_member(data, corpus_id if args.dataset == "longmemeval" else "corpus.jsonl")
                        records, summary = ingest_corpus(mesh, corpus_path, args.dataset, corpus_id, memory_module)
                        for record in records:
                            append_jsonl(fidelity, record)
                        metadata["ingestion"][corpus_id] = summary
                        evaluate_queries(mesh, group, records, protocol, memory_module, streams, vectors)
                    completed += len(group)
                    metadata["cache_observations"][corpus_id] = store_module.get_vector_cache_stats()
                    write_json(output / "progress.json", {"completed_queries": completed, "query_target": len(queries)})
                finally:
                    close_namespace(mesh, audit_module, memory_module, store_module)
            metadata["all_queries_accounted"] = completed == len(queries)
            if not metadata["all_queries_accounted"]:
                raise ValueError("Retrieval omitted selected queries")
            if runtime_passages:
                verify_exports(args.source_run, source_manifest)
                if digest_file(safe_member(args.source_run, "run-manifest.json")) != metadata["source_run_manifest_sha256"]:
                    raise ValueError("Guarded source manifest changed during runtime passage evaluation")
                fresh_path = isolated_artifact_path(args.fresh_default_run, repository, must_exist=True)
                if (digest_file(safe_member(fresh_path, "run-manifest.json")) != pairing["fresh_default_run_manifest_sha256"]
                        or digest_file(safe_member(fresh_path, "results-default.jsonl")) != pairing["fresh_default_results_sha256"]):
                    raise ValueError("Fresh-default pair changed during runtime passage evaluation")
                metadata["guarded_source_immutable"] = True
                if snapshot is not None:
                    verify_runtime_passage_snapshot(snapshot)
                    metadata["runtime_passage_snapshot_reuse"].update(source_immutable=True, readiness_verified_without_inference=True)
                if snapshot_root is not None:
                    metadata["runtime_passage_snapshot"]["immutable"] = True
            if experiment is not None:
                experiment.finish(metadata, output)
            metadata["status"] = "complete"
    except BaseException as exc:
        metadata.update(status="failed", failure_type=type(exc).__name__,
                        failure_details=worker_failure_details(exc, "runtime-passages" if runtime_passages else args.command))
        raise
    finally:
        if experiment is not None:
            experiment.close()
        for stream in (*streams.values(), fidelity, *((vectors,) if vectors else ()), *((resource_stream,) if resource_stream else ())):
            stream.close()
        if resources is not None:
            metadata["runtime_passage_resource_diagnostics"] = resources.summary()
        metadata["output_sha256"] = {path.name: digest_file(path) for path in output.glob("*.jsonl")}
        metadata["diagnostic_loaded_wall_seconds"] = time.monotonic() - started
        metadata["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "run-manifest.json", metadata)
    print(json.dumps({"status": metadata["status"], "dataset": args.dataset, "queries": len(queries)}), flush=True)


def score_run(args):
    global np
    import numpy as np
    run = args.run_directory.resolve()
    data = args.dataset_directory.resolve()
    manifest_path = run / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["status"] != "complete" or digest_file(data / "manifest.json") != manifest["dataset_manifest_sha256"]:
        raise ValueError("Refusing to score an incomplete run or mismatched fixture")
    verify_manifest(data / "manifest.json")
    verify_exports(run, manifest)
    labels = json.loads((data / "labels.json").read_text(encoding="utf-8"))
    queries = list(read_jsonl(data / "queries-selected.jsonl"))
    query_ids = [query["id"] for query in queries]
    if len(query_ids) != manifest["query_target"] or len(set(query_ids)) != len(query_ids):
        raise ValueError("Scoring query accounting mismatch")
    qrels = labels["qrels"]
    unanswered = set(labels.get("unanswered_query_ids", []))
    if set(qrels) | unanswered != set(query_ids):
        raise ValueError("Gold/no-answer accounting differs from selected queries")
    retained_by_corpus = defaultdict(set)
    for record in read_jsonl(run / "ingestion-fidelity.jsonl"):
        if record["faithful_public"]:
            retained_by_corpus[record["corpus_id"]].add(record["document_id"])
    retained = {query["id"]: retained_by_corpus[query.get("corpus", "complete")] for query in queries}
    survival = {
        "answered_queries": sum(bool(qrels.get(identifier)) for identifier in query_ids),
        "any_gold_survived_queries": sum(bool(set(qrels.get(identifier, {})) & retained[identifier]) for identifier in query_ids),
        "all_gold_survived_queries": sum(bool(qrels.get(identifier)) and set(qrels[identifier]).issubset(retained[identifier]) for identifier in query_ids),
        "gold_documents": sum(len(qrels.get(identifier, {})) for identifier in query_ids),
        "retained_gold_documents": sum(len(set(qrels.get(identifier, {})) & retained[identifier]) for identifier in query_ids),
    }
    result = {
        "dataset": manifest["dataset"], "query_count": len(query_ids), "run_manifest_sha256": digest_file(manifest_path),
        "labels_sha256": digest_file(data / "labels.json"), "scorer_sha256": digest_file(Path(__file__)),
        "baseline_provenance": manifest["baseline_provenance"], "ingestion": manifest["ingestion"],
        "gold_survival": survival, "cells": {},
        "definitions": {
            "hit_at_k": "Queries with at least one exact positive evidence document in the first k returned memories.",
            "document_recall_at_k": "Per-query fraction of exact positive source documents retrieved; distinct from any-document hits.",
            "canonical_document": "First faithfully preserved source ID by ingestion order; relevance never selects a deduplicated association.",
            "paired_top1_counts": "Answered exact-evidence queries only; lost baseline hits remain visible even when gains offset them.",
            "bootstrap": "5000 query resamples, seed314159, percentile95% intervals; paired differences share each query.",
            "dense_comparison": "Same-version/model dense ablation, where present; no historical or competitor baseline.",
            "no_answer": "Candidate emission only; no answer generator or wrong-answer endpoint.",
        },
    }
    for path in sorted(run.glob("results-*.jsonl")):
        if "-rerank-" in path.stem:
            continue
        rows = list(read_jsonl(path))
        ids = [row["query_id"] for row in rows]
        if len(ids) != len(query_ids) or len(set(ids)) != len(ids) or set(ids) != set(query_ids):
            raise ValueError("Rank export omits or duplicates selected queries")
        fields = [field for field in ("final_sdk_top10", "raw_hybrid_top64", "dense_public_top64", "original_sdk_top10") if all(field in row for row in rows)]
        cell = {field: summarize(rows, field, qrels, retained, query_ids) for field in fields}
        if "dense_public_top64" in fields:
            cell["sdk_minus_dense_raw_top10"] = paired_difference(rows, "final_sdk_top10", "dense_public_top64", qrels, query_ids)
        if all("original_sdk_top10" in row for row in rows):
            cell["candidate_minus_original_sdk"] = paired_difference(rows, "final_sdk_top10", "original_sdk_top10", qrels, query_ids)
            cell["candidate_hit_at_1_regressions"] = paired_hit_at_1_counts(
                rows, "final_sdk_top10", "original_sdk_top10", qrels, query_ids)
        if manifest["dataset"] == "nfcorpus":
            strong = {identifier: {document: grade for document, grade in values.items() if grade >= 2}
                      for identifier, values in qrels.items()}
            cell["strong_grade_at_least_2"] = {field: summarize(rows, field, strong, retained, query_ids) for field in fields}
        if unanswered:
            no_answer_rows = [row for row in rows if row["query_id"] in unanswered]
            cell["unanswered_candidate_emission"] = {
                "query_count": len(no_answer_rows),
                "sdk_any_candidates": mean_interval([float(bool(row["final_sdk_top10"])) for row in no_answer_rows]),
                "sdk_candidate_count_mean": mean_interval([float(len(row["final_sdk_top10"])) for row in no_answer_rows]),
            }
        if "session_qrels" in labels:
            cell["gold_session_hits"] = {field: summarize(rows, field, labels["session_qrels"], retained, query_ids) for field in fields}
        result["cells"][path.stem] = cell
    if not result["cells"]:
        raise ValueError("No accounted rank cell to score")
    write_json(run / "score-summary.json", result)
    print(json.dumps({"dataset": result["dataset"], "query_count": len(query_ids), "gold_survival": survival,
                      "sdk_hit_at_1": {name: cell["final_sdk_top10"]["unconditional"]["hit_at_1"] for name, cell in result["cells"].items()}}), flush=True)


def report_run(args):
    run = args.run_directory.resolve()
    manifest_path = run / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    score = json.loads((run / "score-summary.json").read_text(encoding="utf-8"))
    if manifest["status"] != "complete" or score["run_manifest_sha256"] != digest_file(manifest_path):
        raise ValueError("Report needs a completed run and matching offline score")
    verify_exports(run, manifest)
    report = {
        "report_version": 1, "status": "complete", "dataset": manifest["dataset"],
        "query_target": manifest["query_target"], "query_count": score["query_count"],
        "runtime_files": manifest["runtime_files"], "run_manifest_sha256": digest_file(manifest_path),
        "score_summary_sha256": digest_file(run / "score-summary.json"), "output_sha256": manifest["output_sha256"],
        "dataset_manifest_sha256": manifest["dataset_manifest_sha256"], "dataset_files": manifest["dataset_manifest"]["files"],
        "labels_sha256": score["labels_sha256"], "scorer_sha256": score.get("scorer_sha256"),
        "model_revision": manifest["model_revision"], "query_model_space_tag": manifest["query_model_space_tag"],
        "model_file_sha256": manifest["model_file_sha256"], "protocol_sha256": manifest["protocol_sha256"],
        "cache_condition_bytes": manifest["cache_condition_bytes"], "dependencies": manifest["dependencies"],
        "sqlite": manifest["sqlite"], "gold_survival": score["gold_survival"], "ingestion": score["ingestion"],
        "metrics": score["cells"], "definitions": score["definitions"],
        "baseline_provenance": manifest["baseline_provenance"],
        "labels_in_ranking_inputs": manifest["labels_in_ranking_inputs"],
        "latency_or_throughput_claim": None,
    }
    for key in ("schema_version", "guarded_readiness", "default_query_backend_observations", "query_encoding_mode",
                "ranking_condition", "saved_query_vectors_used", "document_inference_invoked", "first_six_gross_diagnostics",
                "timing_scope", "passage_condition", "parent_budget", "inference_accounting", "resource_diagnostics",
                "fixed_parameter_provenance", "source_run_manifest_sha256", "passage_index_enabled", "runtime_passage_index",
                "runtime_passage_readiness", "runtime_passage_query_backend_observations", "runtime_passage_resource_diagnostics",
                "guarded_source_immutable", "guarded_source_database_fingerprints"):
        if key in manifest:
            report[key] = manifest[key]

    def validate_public(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"content", "body", "query", "answer", "authorization", "token_file",
                                   "temporary_mesh_home", "mesh_home", "source_home", "configuration", "auth_token"}:
                    raise ValueError("Private field in aggregate report")
                validate_public(child)
        elif isinstance(value, list):
            for child in value:
                validate_public(child)
        elif isinstance(value, str) and re.search(r"(?i)(?:(?<![a-z0-9])[a-z]:[\\/]|/home/|/users/|\\users\\|mesh-frozen-quality-[a-z0-9]{6})", value):
            raise ValueError("Personal or temporary path in aggregate report")

    validate_public(report)
    if args.output.exists():
        raise ValueError("Refusing to overwrite an existing report")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, report)
    print(json.dumps({"report_written": args.output.name, "query_count": report["query_count"],
                      "sha256": digest_file(args.output)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("regression", "query-only"):
        command = commands.add_parser(name, help="Bounded guarded ingestion and neural queries" if name == "regression" else "Default SDK fresh queries on an existing indexed guarded corpus")
        command.add_argument("dataset", choices=("nfcorpus", "scifact", "prospective", "longtext-prospective", "longmemeval"))
        command.add_argument("--repository", required=True, type=Path)
        command.add_argument("--dataset-directory", required=True, type=Path)
        command.add_argument("--protocol", required=True, type=Path)
        command.add_argument("--model-directory", required=True, type=Path)
        command.add_argument("--dependency-directory", type=Path)
        command.add_argument("--output", required=True, type=Path)
        command.add_argument("--retain-guarded-home", action="store_true", help="Explicitly retain the named isolated corpus for later offline replay")
        if name == "query-only":
            command.add_argument("--source-run", required=True, type=Path)
            command.add_argument("--source-namespace", help="Required for a single corpus; inspected LME namespaces come from verified query provenance")
            command.add_argument("--source-home", type=Path, help="Named isolated source home when its manifest is sanitized")
            command.add_argument("--runtime-passages", action="store_true", help="Opt in to the actual schema-6 passage index on a guarded database copy; manually build and verify original-parent readiness")
            command.add_argument("--fresh-default-run", type=Path, help="Required with --runtime-passages: complete hash-verified fresh-default SDK export from the same guarded source")
    score = commands.add_parser("score", help="Read relevance labels only after a completed, hash-verified retrieval export")
    score.add_argument("run_directory", type=Path)
    score.add_argument("--dataset-directory", required=True, type=Path)
    report = commands.add_parser("report", help="Export only path-free aggregate metrics and provenance")
    report.add_argument("run_directory", type=Path)
    report.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "score":
        score_run(args)
    elif args.command == "report":
        report_run(args)
    else:
        run_retrieval(args)


if __name__ == "__main__":
    main()
