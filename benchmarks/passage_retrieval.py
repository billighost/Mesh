"""CI-only guarded passage retrieval experiment on a frozen public checkpoint.

This runner has no relevance-label inputs and makes no production schema changes.
Short documents keep their existing representation. Long accepted documents are
split at deterministic paragraphs/sentences and oversized sentences use the
existing 224-token/192-stride windows. Each derived passage goes through Mesh's
normal guarded SDK; scores are collapsed to the original revision-bound parent.
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import hashlib
import heapq
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

SHORT_DOCUMENT_TOKENS = 254
PASSAGE_TOKENS = 224
OVERSIZED_SENTENCE_OVERLAP = 32
PARAGRAPH_BREAK = re.compile(r"\r?\n[ \t]*\r?\n")
SENTENCE_END = re.compile(r"[.!?][\"')\]]*(?=\s|$)")
MODEL_TAG = "onnx-minilm-l6-v2"


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _checked_helper_bootstrap(path: Path, *, directory=True) -> Path:
    # Check raw ancestors before any resolver can hide a symlink/reparse target.
    absolute = Path(os.path.abspath(path))
    for ancestor in reversed((absolute, *absolute.parents)):
        try:
            status = ancestor.lstat()
        except FileNotFoundError:
            continue
        if ancestor.is_symlink() or getattr(status, "st_file_attributes", 0) & 0x400:
            raise ValueError("Retrieval helper bootstrap traversed a symlink or reparse point")
    if not (absolute.is_dir() if directory else absolute.is_file()):
        raise ValueError("Retrieval helper path is unavailable")
    return absolute


def _load_checked_helper(name, directory):
    path = _checked_helper_bootstrap(directory / (name + ".py"), directory=False)
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise ValueError("Checked retrieval helper has no Python loader")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def split_passages(content: str, tokenizer) -> list[tuple[int, int]]:
    """Original-text spans; no source characters or paragraphs are truncated."""
    full = tokenizer.encode(content, add_special_tokens=False)
    if len(full.ids) <= SHORT_DOCUMENT_TOKENS:
        return [(0, len(content))] if content.strip() else []
    spans = []
    boundaries = [0]
    for match in PARAGRAPH_BREAK.finditer(content):
        boundaries.extend((match.start(), match.end()))
    boundaries.append(len(content))
    for left, right in zip(boundaries[::2], boundaries[1::2]):
        while left < right and content[left].isspace():
            left += 1
        while right > left and content[right - 1].isspace():
            right -= 1
        if left == right:
            continue
        paragraph = content[left:right]
        encoding = tokenizer.encode(paragraph, add_special_tokens=False)
        if len(encoding.ids) <= PASSAGE_TOKENS:
            spans.append((left, right))
            continue
        offsets = encoding.offsets
        ends = [end for _, end in offsets]
        sentence_ends = sorted({bisect.bisect_right(ends, match.end()) for match in SENTENCE_END.finditer(paragraph)})
        start = 0
        while start < len(offsets):
            ceiling = min(len(offsets), start + PASSAGE_TOKENS)
            position = bisect.bisect_right(sentence_ends, ceiling)
            sentence_end = sentence_ends[position - 1] if position else 0
            natural_boundary = sentence_end > start
            end = sentence_end if natural_boundary else ceiling
            spans.append((left + offsets[start][0], left + offsets[end - 1][1]))
            if end == len(offsets):
                break
            start = end if natural_boundary else max(start + 1, end - OVERSIZED_SENTENCE_OVERLAP)
    return spans


def uncovered_nonspace(content: str, spans: list[tuple[int, int]]) -> int:
    """Explicitly account for tokenizer-ignored characters and guard exclusions."""
    uncovered = 0
    end = 0
    for left, right in sorted(spans):
        if not 0 <= left < right <= len(content):
            raise ValueError("Passage offsets escaped their accepted parent")
        if left > end:
            uncovered += sum(not char.isspace() for char in content[end:left])
        end = max(end, right)
    uncovered += sum(not char.isspace() for char in content[end:])
    return uncovered


class ResourceDiagnostics:
    """Loaded phase diagnostics; an external CI supervisor supplies hard limits."""

    def __init__(self):
        self.started = time.monotonic()
        self.cpu_started = time.process_time()
        self.rss_max = 0
        self.phases = {}
        try:
            import psutil
            self.process = psutil.Process()
        except ImportError:
            self.process = None

    def sample(self, phase):
        if self.process is not None:
            self.rss_max = max(self.rss_max, int(self.process.memory_info().rss))
        elif sys.platform != "win32":
            import resource
            scale = 1 if sys.platform == "darwin" else 1024
            self.rss_max = max(self.rss_max, int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * scale)
        value = {"wall_seconds": time.monotonic() - self.started,
                 "process_cpu_seconds": time.process_time() - self.cpu_started,
                 "sampled_owned_process_rss_bytes_max": self.rss_max}
        self.phases[phase] = value
        return value


class _ParentHeapEntry:
    __slots__ = ("score", "similarity", "identifier", "serial")

    def __init__(self, row, serial):
        self.score = float(row["rank_score"])
        self.similarity = float(row["similarity"])
        self.identifier = row["id"]
        self.serial = serial

    def __lt__(self, other):
        if self.score != other.score:
            return self.score < other.score
        if self.similarity != other.similarity:
            return self.similarity < other.similarity
        return self.identifier > other.identifier


def _same_parent_quality(row):
    return (float(row["rank_score"]), float(row["similarity"]),
            -int(row.get("selected_passage_ordinal", -1)))


class _BoundedParents:
    """Bound live parent rows while scanning shared-passage associations."""

    def __init__(self, limit):
        self.limit = limit
        self.rows = {}
        self.heap = []
        self.serial = 0
        self.peak_rows = 0

    def offer(self, row):
        previous = self.rows.get(row["id"])
        proposed = _ParentHeapEntry(row, self.serial)
        if previous is not None:
            if _same_parent_quality(previous[1]) >= _same_parent_quality(row):
                return
        while self.heap and self.rows.get(self.heap[0].identifier, (None,))[0] != self.heap[0].serial:
            heapq.heappop(self.heap)
        if previous is None and len(self.rows) >= self.limit:
            if not self.heap[0] < proposed:
                return
            worst = heapq.heappop(self.heap)
            self.rows.pop(worst.identifier)
        self.serial += 1
        proposed.serial = self.serial
        self.rows[row["id"]] = (self.serial, row)
        heapq.heappush(self.heap, proposed)
        self.peak_rows = max(self.peak_rows, len(self.rows))
        if len(self.heap) > self.limit * 2:
            self.heap = [_ParentHeapEntry(value, serial) for serial, value in self.rows.values()]
            heapq.heapify(self.heap)


def drain_jobs(mesh, memory_module, resources):
    attempts_without_progress = 0
    while True:
        before = int(mesh.store.embedding_job_stats()["pending_count"])
        if before == 0:
            return
        jobs = mesh.store.list_embedding_jobs(limit=32)
        if not jobs:
            raise RuntimeError("Derived indexing is deferred; partial evidence cannot be scored")
        hints = [memory_module._ReembedHint(
            str(mesh.store.db_path.parent), mesh.namespace, job["memory_id"], int(job["revision"]),
            job["target_model"], int(job["attempts"]), incarnation=job["incarnation"],
            store_generation=job["store_generation"]) for job in jobs]
        memory_module._process_reembed_batch(hints)
        after = int(mesh.store.embedding_job_stats()["pending_count"])
        attempts_without_progress = attempts_without_progress + 1 if after >= before else 0
        resources.sample("passage_indexing")
        if attempts_without_progress >= 3:
            raise RuntimeError("Derived indexing made no progress; every remaining job is accounted as incomplete")


def parent_identity(parent, record):
    accepted = record["acceptance"]
    if (parent is None or parent.get("local_only") or parent.get("quarantined")
            or parent["embedding_model"] != MODEL_TAG
            or int(parent["revision"]) != int(record["current_revision"])
            or parent["incarnation"] != accepted["incarnation"]
            or parent["store_generation"] != accepted["store_generation"]
            or content_hash(parent["content"]) != record["stored_sha256"]):
        raise ValueError("Accepted public parent content, privacy, model or complete identity changed")
    return {"id": parent["id"], "revision": int(parent["revision"]),
            "incarnation": parent["incarnation"], "store_generation": parent["store_generation"],
            "embedding_model": MODEL_TAG}


def verify_indexed_identities(mesh, identities):
    """Readiness must include vectors plus revision/incarnation/generation."""
    identifiers = list(identities)
    generations = {value["store_generation"] for value in identities.values()}
    if len(generations) > 1:
        raise ValueError("One derived namespace contains conflicting store generations")
    for start in range(0, len(identifiers), 128):
        block = identifiers[start:start + 128]
        result = mesh.wait_for_indexed(
            block, timeout=0, include_private=False, target_model=MODEL_TAG,
            expected_revisions={identifier: int(identities[identifier]["revision"]) for identifier in block},
            expected_incarnations={identifier: identities[identifier]["incarnation"] for identifier in block},
            expected_store_generation=next(iter(generations)))
        if result["status"] != "indexed" or len(result["indexed"]) != len(block):
            raise RuntimeError("Derived vector readiness failed its full immutable identity check")
    return len(identifiers)


def ingest_children(parent_mesh, child_mesh, source_records, tokenizer, helpers, memory_module,
                    fidelity_stream, coverage_stream, resources):
    parents, spans_by_child, submissions = {}, defaultdict(list), []
    parent_spans = defaultdict(list)
    counts = Counter()
    pending = []
    pending_bytes = 0

    def submit():
        nonlocal pending, pending_bytes
        if not pending:
            return
        results = child_mesh.learn_many(
            [{"content": item["content"], "memory_type": "fact", "local_only": False, "tags": []}
             for item in pending], atomic=False, deduplicate=False)
        if len(results) != len(pending):
            raise RuntimeError("Guarded child ingestion lost an input outcome")
        for item, result in zip(pending, results):
            record = {key: value for key, value in item.items() if key != "content"}
            record["acceptance"] = result
            submissions.append(record)
        counts["submitted_passages"] += len(pending)
        pending, pending_bytes = [], 0
        resources.sample("passage_acceptance")

    for source in source_records:
        counts["source_submissions"] += 1
        if not source["faithful_public"]:
            counts["excluded_nonpublic_or_unfaithful_sources"] += 1
            continue
        identifier = source["acceptance"]["memory_id"]
        if identifier in parents:
            parents[identifier]["faithful_document_ids"].append(source["document_id"])
            continue
        parent = parent_mesh.get(identifier, include_private=False)
        metadata = parent_identity(parent, source)
        metadata["faithful_document_ids"] = [source["document_id"]]
        metadata["nonspace_characters"] = sum(not char.isspace() for char in parent["content"])
        parents[identifier] = metadata
        spans = split_passages(parent["content"], tokenizer)
        counts["unchanged_short_parent_representations"] += int(spans == [(0, len(parent["content"]))])
        counts["public_parent_memories"] += 1
        counts["planned_passages"] += len(spans)
        counts["split_uncovered_nonspace_characters"] += uncovered_nonspace(parent["content"], spans)
        for ordinal, (start, end) in enumerate(spans):
            content = parent["content"][start:end]
            token_count = len(tokenizer.encode(" ".join(content.split()), add_special_tokens=False).ids)
            if token_count > SHORT_DOCUMENT_TOKENS:
                raise ValueError("A derived span exceeded the existing single-window input limit")
            counts["maximum_passage_tokens"] = max(counts["maximum_passage_tokens"], token_count)
            item = {"parent_id": identifier, "parent_revision": metadata["revision"],
                    "parent_incarnation": metadata["incarnation"], "parent_store_generation": metadata["store_generation"],
                    "passage_ordinal": ordinal, "start": start, "end": end,
                    "input_sha256": content_hash(content.strip()), "input_tokens": token_count, "content": content}
            size = len(content.encode("utf-8"))
            if pending and (len(pending) >= 64 or pending_bytes + size > 512 * 1024):
                submit()
            pending.append(item)
            pending_bytes += size
    submit()
    drain_jobs(child_mesh, memory_module, resources)
    # Validate every accepted derived child after durable model publication.
    for record in submissions:
        acceptance = record["acceptance"]
        identifier = acceptance.get("memory_id")
        child = child_mesh.get(identifier, include_private=False) if identifier else None
        faithful = bool(child is not None and not child.get("local_only") and not child.get("quarantined")
                        and child["embedding_model"] == MODEL_TAG
                        and int(child["revision"]) == int(acceptance["revision"])
                        and child["incarnation"] == acceptance["incarnation"]
                        and child["store_generation"] == acceptance["store_generation"]
                        and content_hash(child["content"]) == record["input_sha256"])
        record.update(faithful_public=faithful, rejected=identifier is None,
                      private=bool(acceptance.get("local_only")), quarantined=bool(acceptance.get("quarantined")),
                      body_or_identity_changed=bool(identifier and not faithful),
                      current_revision=int(child["revision"]) if child else None)
        helpers.append_jsonl(fidelity_stream, record)
        counts["faithful_public_passages"] += int(faithful)
        counts["rejected_passages"] += int(identifier is None)
        counts["excluded_private_or_quarantined_passages"] += int(bool(acceptance.get("local_only") or acceptance.get("quarantined")))
        counts["body_or_identity_changed_passages"] += int(bool(identifier and not faithful))
        if faithful:
            spans_by_child[identifier].append(record)
            parent_spans[record["parent_id"]].append((record["start"], record["end"]))
    # Account for source coverage only from exact, eligible child bodies.
    for identifier, metadata in parents.items():
        current = parent_mesh.get(identifier, include_private=False)
        if current is None or current["incarnation"] != metadata["incarnation"] or current["store_generation"] != metadata["store_generation"] or int(current["revision"]) != metadata["revision"]:
            raise ValueError("Parent changed while building its derived passages")
        missing = uncovered_nonspace(current["content"], parent_spans[identifier])
        coverage = {**metadata, "faithful_passage_count": len(parent_spans[identifier]),
                    "uncovered_nonspace_characters": missing, "full_nonspace_coverage": missing == 0}
        helpers.append_jsonl(coverage_stream, coverage)
        counts["parents_with_full_nonspace_coverage"] += int(missing == 0)
        counts["parents_with_partial_passage_coverage"] += int(missing > 0)
        counts["guarded_uncovered_nonspace_characters"] += missing
    counts["unique_public_child_memories"] = len(spans_by_child)
    counts["identity_bound_indexed_child_memories"] = verify_indexed_identities(
        child_mesh, {identifier: records[0]["acceptance"] for identifier, records in spans_by_child.items()})
    counts["pending_embedding_jobs"] = int(child_mesh.store.embedding_job_stats()["pending_count"])
    return parents, spans_by_child, dict(counts)


def collapse_parents(passages, original, parents, spans_by_child, parent_mesh, parent_cap):
    """One joint score/span per original memory; never combine different spans."""
    selected = _BoundedParents(parent_cap)
    considered = set()
    stale_or_private = 0
    for candidate in original:
        identifier = candidate.get("id")
        if identifier in parents:
            considered.add(identifier)
            selected.offer({**candidate, **parents[identifier], "match_source": "original_parent"})
    for passage in passages:
        if passage.get("local_only") or passage.get("quarantined") or passage.get("embedding_model") != MODEL_TAG:
            stale_or_private += 1
            continue
        for association in spans_by_child.get(passage["id"], []):
            accepted = association["acceptance"]
            if (passage["revision"] != accepted["revision"] or passage["incarnation"] != accepted["incarnation"]
                    or passage["store_generation"] != accepted["store_generation"]):
                stale_or_private += 1
                continue
            identifier = association["parent_id"]
            considered.add(identifier)
            metadata = parents[identifier]
            score = float(passage["rank_score"])
            previous_entry = selected.rows.get(identifier)
            previous = previous_entry[1] if previous_entry is not None else None
            proposed_quality = (score, float(passage["similarity"]), -association["passage_ordinal"])
            if previous is not None and _same_parent_quality(previous) >= proposed_quality:
                continue
            selected.offer({
                **metadata, "similarity": float(passage["similarity"]), "rank_score": score,
                "semantic_similarity": float(passage["semantic_similarity"]),
                "lexical_score": float(passage["lexical_score"]), "bm25_score": float(passage["bm25_score"]),
                "retrieval_mode": passage["retrieval_mode"], "candidate_count": int(passage["candidate_count"]),
                "match_source": "derived_passage", "selected_passage_id": passage["id"],
                "selected_passage_ordinal": association["passage_ordinal"],
                "supporting_spans": [{"memory_id": identifier, "revision": metadata["revision"],
                                      "incarnation": metadata["incarnation"], "store_generation": metadata["store_generation"],
                                      "start": association["start"], "end": association["end"],
                                      "span_content_sha256": association["input_sha256"]}],
            })
    before_validation = len(selected.rows)
    validated = parent_mesh.store.revalidate_query_results(
        [row for _, row in selected.rows.values()], include_private=False, include_quarantined=False,
        include_stale=True, min_confidence=0.0)
    validated.sort(key=lambda row: (-row["rank_score"], -row["similarity"], row["id"]))
    bounded = validated[:parent_cap]
    distinct = len({row["id"] for row in bounded})
    return bounded, {
        "distinct_parents_considered": len(considered),
        "parent_materialization_peak_count": selected.peak_rows,
        "distinct_parents_before_policy_revalidation": before_validation,
        "distinct_parents_after_policy_revalidation": len(validated),
        "distinct_parents_returned": distinct,
        "parent_candidate_cap": parent_cap,
        "parent_cap_violation": selected.peak_rows > parent_cap or len(bounded) > parent_cap or distinct != len(bounded),
        "dropped_stale_or_private_associations": stale_or_private + before_validation - len(validated),
    }


def retrieve_parent_query(query, cap, parent_budget, child_mesh, parent_mesh,
                          parents, associations, original):
    """The same guarded retrieval call for baseline and optional CI observers."""
    parent_cap = min(cap, parent_budget)
    os.environ["MESH_RECALL_CANDIDATES"] = str(cap)
    passages = child_mesh.recall(
        query["query"], n=parent_cap, include_private=False, include_quarantined=False,
        record_access=False, _precomputed_embedding=query["vector"], _precomputed_model_tag=MODEL_TAG)
    baseline = original[cap][query["query_id"]]
    collapsed, budget = collapse_parents(
        passages, baseline["final_sdk_top10"], parents, associations, parent_mesh, parent_cap)
    budget.update(query_id=query["query_id"], candidate_cap=cap,
                  passage_returned_count=len(passages),
                  passage_internal_candidate_count=max((int(row["candidate_count"]) for row in passages), default=0))
    return {"query_id": query["query_id"], "corpus_id": query["corpus_id"], "candidate_cap": cap,
            "query_embedding_model": MODEL_TAG, "original_sdk_top10": baseline["final_sdk_top10"],
            "raw_hybrid_top64": collapsed[:64], "final_sdk_top10": collapsed[:10],
            "candidate_budget": budget, "endpoint": "Original parent evidence IDs only; no answer generator."}


@contextlib.contextmanager
def owned_runtime_directory():
    owned = tempfile.TemporaryDirectory(prefix="mesh-frozen-quality-passages-")
    root = Path(owned.name).resolve(strict=True)
    try:
        yield root
    finally:
        # List exactly the owned files before TemporaryDirectory removes them.
        print(json.dumps({"owned_cleanup_files": sorted(
            item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file())}), flush=True)
        owned.cleanup()


def main(argv=None, experiment=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("source-run", "repository", "dataset-directory", "protocol", "model-directory", "output"):
        parser.add_argument("--" + option, required=True, type=Path)
    parser.add_argument("--dependency-directory", type=Path)
    parser.add_argument("--helper-directory", type=Path, help="Directory containing the reviewed sibling CI retrieval helpers")
    parser.add_argument("--parent-budget", type=int, default=96)
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Full passage experiments run only in GitHub Actions; local native smoke is separate.")
    if not 10 <= args.parent_budget <= 512 or args.output.exists():
        parser.error("parent budget must be 10..512 and output must not already exist")
    raw_helper_directory = args.helper_directory or Path(__file__).absolute().parent
    helper_directory = _checked_helper_bootstrap(raw_helper_directory)
    replay = _load_checked_helper("replay_retrieval", helper_directory)
    helpers = _load_checked_helper("production_retrieval", helper_directory)
    if Path(helpers.__file__).resolve().parent != helper_directory or Path(replay.__file__).resolve().parent != helper_directory:
        raise ValueError("Retrieval imported helpers outside the checked directory")
    from replay_retrieval import (checked_path, digest_file, isolated_artifact_path, isolated_source_home,
                                  read_jsonl, safe_member, verify_exports, write_json)

    repository = checked_path(args.repository, must_exist=True, directory=True)
    helper_directory = checked_path(raw_helper_directory, must_exist=True, directory=True)
    source_run = isolated_artifact_path(args.source_run, repository=repository, must_exist=True)
    model_directory = isolated_artifact_path(args.model_directory, repository=repository, must_exist=True)
    data_directory = isolated_artifact_path(args.dataset_directory, repository=repository, must_exist=True)
    output = isolated_artifact_path(args.output, repository=repository, must_exist=False)
    protocol_path = checked_path(args.protocol, must_exist=True, directory=False)
    dependency_directory = checked_path(args.dependency_directory, must_exist=True, directory=True) if args.dependency_directory else None
    source_path = safe_member(source_run, "run-manifest.json")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    fixture_path = safe_member(data_directory, "manifest.json")
    fixture = helpers.verify_manifest(fixture_path)
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if (source["status"] != "complete" or source["embedding_model"] != "minilm"
            or source["query_model_space_tag"] != MODEL_TAG or protocol["fusion"] != "calibrated"
            or protocol["candidate_caps"] != [64, 96] or source["dataset_manifest_sha256"] != digest_file(fixture_path)
            or source["model_revision"] != protocol["model"]["revision"]
            or source["protocol_sha256"] != digest_file(protocol_path)):
        raise ValueError("Source must be the fully accounted same-model guarded checkpoint")
    verify_exports(source_run, source)
    source_home = isolated_source_home(source["temporary_mesh_home"])
    source_fidelity = safe_member(source_run, "ingestion-fidelity.jsonl")
    source_vectors = safe_member(source_run, "query-vectors.jsonl")
    records = list(read_jsonl(source_fidelity))
    vectors = list(read_jsonl(source_vectors))
    prepared = list(read_jsonl(safe_member(data_directory, "queries-selected.jsonl")))
    expected_queries = {row["id"]: row["query"] for row in prepared}
    if (len(vectors) != len(expected_queries) or len(vectors) != source["query_target"]
            or len(vectors) != fixture["query_count"] or len(records) != fixture["corpus_count"]
            or {row["query_id"]: row["query"] for row in vectors} != expected_queries
            or any(row["embedding_model"] != MODEL_TAG for row in vectors)):
        raise ValueError("Source queries or complete source-corpus accounting changed")
    original = {}
    for cap in (64, 96):
        exported = list(read_jsonl(safe_member(source_run, f"results-C{cap}.jsonl")))
        original[cap] = {row["query_id"]: row for row in exported}
        if len(exported) != len(expected_queries) or set(original[cap]) != set(expected_queries):
            raise ValueError("Frozen original results omit or duplicate a query")
    output.mkdir(parents=True)
    shutil.copyfile(source_fidelity, output / "ingestion-fidelity.jsonl")
    shutil.copyfile(source_vectors, output / "query-vectors.jsonl")
    manifest = {
        "status": "running", "dataset": source["dataset"], "query_target": len(vectors),
        "dataset_manifest": fixture, "dataset_manifest_sha256": digest_file(fixture_path),
        "protocol_sha256": digest_file(protocol_path), "harness_sha256": digest_file(Path(__file__)),
        "source_run_manifest_sha256": digest_file(source_path), "source_output_sha256": source["output_sha256"],
        "baseline_provenance": "Paired guarded-parent versus deterministic guarded-passage experiment; identical source bodies, parent identities and saved neural query vectors.",
        "started_at_utc": datetime.now(timezone.utc).isoformat(), "embedding_model": "minilm",
        "model_revision": protocol["model"]["revision"], "query_model_space_tag": MODEL_TAG,
        "cache_condition_bytes": 256 * 1024 * 1024, "labels_in_ranking_inputs": False,
        "ingestion": source["ingestion"], "passage_ingestion": {}, "cache_observations": {},
        "fixed_parameters": {"short_document_tokens": SHORT_DOCUMENT_TOKENS, "passage_tokens": PASSAGE_TOKENS,
                             "oversized_sentence_stride": PASSAGE_TOKENS - OVERSIZED_SENTENCE_OVERLAP,
                             "boundaries": "blank-line paragraphs, then sentence endings; fixed token fallback",
                             "fusion": "current SDK calibrated per-passage dense/lexical 25%; maximum joint passage per parent",
                             "source_fallback": "original guarded SDK top10 parents remain available with their existing eligibility filters",
                             "derived_deduplicate": False,
                             "derived_identity": "one guarded child per original parent/ordinal; source parent dedup/canonical IDs unchanged",
                             "requested_parent_budget": args.parent_budget, "effective_parent_budget": "min(cell candidate cap, requested budget)",
                             "passage_result_budget": "effective parent budget; actual SDK internal candidate counts recorded",
                             "source_content_limit": "No derived truncation; original accepted content and all omitted nonspace characters accounted"},
        "candidate_budget_observations": [], "saved_query_vectors_used": True,
        "production_schema_changed": False, "new_model_or_provider_call": False,
        "timing_scope": "CI loaded phase diagnostics; external supervisor measures hard resource ceilings; no production SLO claim.",
    }
    write_json(output / "run-manifest.json", manifest)
    resources = ResourceDiagnostics()
    inference = Counter()
    streams = {cap: (output / f"results-C{cap}.jsonl").open("w", encoding="utf-8", newline="\n") for cap in (64, 96)}
    fidelity = (output / "passage-ingestion-fidelity.jsonl").open("w", encoding="utf-8", newline="\n")
    coverage = (output / "parent-passage-coverage.jsonl").open("w", encoding="utf-8", newline="\n")
    try:
        with contextlib.ExitStack() as cleanup:
            root = cleanup.enter_context(owned_runtime_directory())
            if experiment is not None:
                cleanup.callback(experiment.close)
            helpers.install_model(root, model_directory, protocol, query_only=False, repository=repository)
            hashes, audit_module, memory_module, store_module = helpers.import_runtime(
                root, repository, dependency_directory, cleanup)
            if hashes != source["runtime_files"]:
                raise ValueError("Passage candidate and frozen source must use the same production runtime")
            manifest["runtime_files"] = hashes
            manifest["schema_version"] = store_module.SCHEMA_VERSION
            manifest["dependencies"] = {name: importlib.metadata.version(name) for name in ("numpy", "onnxruntime", "tokenizers", "apsw")}
            model, backend = memory_module._get_model()
            if backend != "onnx" or not isinstance(model, memory_module._DirectOnnxEmbedding):
                raise ValueError("The fixed token-window experiment requires the existing local DirectOnnx backend")
            tokenizer = model.window_tokenizer
            encode_original = memory_module._DirectOnnxEmbedding._encode_raw_batches

            def counted_encode(self, texts, batch_size=32):
                inference["derived_document_encoding_calls"] += 1
                inference["derived_document_window_inputs"] += len(texts)
                inference["derived_document_inference_batches"] += (len(texts) + batch_size - 1) // batch_size
                result = encode_original(self, texts, batch_size=batch_size)
                resources.sample("document_inference")
                return result

            memory_module._DirectOnnxEmbedding._encode_raw_batches = counted_encode
            cleanup.callback(setattr, memory_module._DirectOnnxEmbedding, "_encode_raw_batches", encode_original)
            del model
            grouped_queries, grouped_records = defaultdict(list), defaultdict(list)
            for query in vectors:
                grouped_queries[query["namespace"]].append(query)
            for record in records:
                grouped_records[record["corpus_id"]].append(record)
            completed = 0
            for ordinal, (namespace, queries) in enumerate(grouped_queries.items()):
                corpus_id = queries[0]["corpus_id"]
                if any(query["corpus_id"] != corpus_id for query in queries):
                    raise ValueError("Source namespace contains ambiguous corpus identities")
                source_records = grouped_records[corpus_id]
                helpers.copy_guarded_corpus(source_run, source_home, namespace, root, source_records, store_module)
                parent_mesh = memory_module.Mesh(namespace=namespace, agent_id="ci-passage-parent")
                child_mesh = memory_module.Mesh(namespace=f"ci-passages-{ordinal:03d}", agent_id="ci-passage-derived")
                try:
                    manifest["sqlite"] = helpers.verify_sqlite(child_mesh, store_module)
                    helpers.verify_guarded_readiness(parent_mesh, source_records, memory_module)
                    parents, associations, stats = ingest_children(
                        parent_mesh, child_mesh, source_records, tokenizer, helpers,
                        memory_module, fidelity, coverage, resources)
                    manifest["passage_ingestion"][corpus_id] = stats
                    inference_after_ingestion = dict(inference)

                    def retrieve(query, cap):
                        return retrieve_parent_query(
                            query, cap, args.parent_budget, child_mesh, parent_mesh,
                            parents, associations, original)

                    if experiment is not None:
                        experiment.before_namespace(queries, retrieve, parent_mesh, source_records)
                    for query in queries:
                        for cap in (64, 96):
                            result = (experiment.query(query, cap, retrieve) if experiment is not None
                                      else retrieve(query, cap))
                            manifest["candidate_budget_observations"].append(result["candidate_budget"])
                            helpers.append_jsonl(streams[cap], result)
                        completed += 1
                        resources.sample("parent_queries")
                        if completed % 10 == 0:
                            write_json(output / "progress.json", {"completed_queries": completed, "query_target": len(vectors)})
                            print(json.dumps({"phase": "passage-parent-queries", "completed_queries": completed, "query_target": len(vectors)}), flush=True)
                    if experiment is not None:
                        experiment.after_namespace(queries, retrieve)
                    if dict(inference) != inference_after_ingestion:
                        raise ValueError("Saved-query passage retrieval unexpectedly performed query-time model inference")
                    manifest["cache_observations"][corpus_id] = store_module.get_vector_cache_stats()
                finally:
                    helpers.close_namespace(child_mesh, audit_module, memory_module, store_module)
                    helpers.close_namespace(parent_mesh, audit_module, memory_module, store_module)
            manifest["all_queries_accounted"] = completed == len(vectors)
            if not manifest["all_queries_accounted"] or any(row["parent_cap_violation"] for row in manifest["candidate_budget_observations"]):
                raise ValueError("Parent query accounting or candidate cap failed")
            manifest["inference"] = dict(inference, saved_query_vectors_reused=len(vectors), query_time_window_inputs=0)
            if experiment is not None:
                experiment.finish(manifest, output)
            manifest["status"] = "complete"
    except BaseException as exc:
        manifest.update(status="failed", failure_type=type(exc).__name__, inference=dict(inference))
        raise
    finally:
        for stream in (*streams.values(), fidelity, coverage):
            stream.close()
        manifest["resource_diagnostics"] = resources.sample("complete_or_failed")
        manifest["resource_phases"] = resources.phases
        manifest["output_sha256"] = {path.name: digest_file(path) for path in output.glob("*.jsonl")}
        manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "run-manifest.json", manifest)
    print(json.dumps({"status": manifest["status"], "dataset": manifest["dataset"],
                      "queries": len(vectors), "parent_cap_violations": 0}), flush=True)


if __name__ == "__main__":
    main()
