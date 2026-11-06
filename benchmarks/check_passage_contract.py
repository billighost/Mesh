"""Tiny stdlib contract checks: source coverage, policy, identity and budgets.

No Mesh instance, model inference, provider, home directory or daemon is used.
"""
from __future__ import annotations

import random
import re
import io
import json
from types import SimpleNamespace

from passage_retrieval import (MODEL_TAG, _BoundedParents, collapse_parents, content_hash,
                               ingest_children, split_passages, uncovered_nonspace)


class WordTokenizer:
    def encode(self, text, add_special_tokens=False):
        assert add_special_tokens is False
        offsets = [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]
        return SimpleNamespace(ids=list(range(len(offsets))), offsets=offsets)


class PublicStore:
    def __init__(self, states):
        self.states = states

    def revalidate_query_results(self, results, **policy):
        assert policy["include_private"] is False
        assert policy["include_quarantined"] is False
        accepted = []
        for row in results:
            current = self.states.get(row["id"])
            if current is None or current.get("local_only") or current.get("quarantined"):
                continue
            if all(row[key] == current[key] for key in ("revision", "incarnation", "store_generation")):
                accepted.append(row)
        return accepted


def check_coverage():
    tokenizer = WordTokenizer()
    short = "A short title.\n\nA short body with punctuation and accented café text."
    assert split_passages(short, tokenizer) == [(0, len(short))]
    filler = " ".join(f"word{index}" for index in range(600))
    fact = "Calibration records remain available for forty-five days."
    for text in (fact + "\n\n" + filler, filler + "\n\n" + fact):
        spans = split_passages(text, tokenizer)
        assert uncovered_nonspace(text, spans) == 0
        assert any(text[start:end] == fact for start, end in spans)
        assert all(len(tokenizer.encode(text[start:end]).ids) <= 224 for start, end in spans)
    # Multiple blank lines, mixed line endings and long sentences retain all data.
    text = (" ".join(f"unit{index}." if index % 37 == 36 else f"unit{index}" for index in range(480))
            + "\r\n \r\n\n" + filler)
    assert uncovered_nonspace(text, split_passages(text, tokenizer)) == 0
    assert uncovered_nonspace("visible omitted", [(0, 7)]) == 7


def check_budget():
    random_source = random.Random(7201)
    inputs = [{"id": f"parent-{index % 193:03d}", "rank_score": round(random_source.random(), 2),
               "similarity": round(random_source.random(), 2)} for index in range(3000)]
    for limit in (64, 96):
        bounded = _BoundedParents(limit)
        oracle = {}
        for row in inputs:
            bounded.offer(row)
            previous = oracle.get(row["id"])
            if previous is None or (previous["rank_score"], previous["similarity"]) < (row["rank_score"], row["similarity"]):
                oracle[row["id"]] = row
            assert len(bounded.rows) <= limit
            assert len(bounded.heap) <= limit * 2
        order = lambda row: (-row["rank_score"], -row["similarity"], row["id"])
        expected = sorted(oracle.values(), key=order)[:limit]
        actual = sorted([row for _, row in bounded.rows.values()], key=order)
        assert actual == expected


def check_policy_and_identity():
    parent = {"id": "parent-one", "revision": 1, "incarnation": "a" * 32,
              "store_generation": "b" * 32, "embedding_model": MODEL_TAG,
              "faithful_document_ids": ["source-first", "source-alias"]}
    accepted = {"revision": 1, "incarnation": "c" * 32, "store_generation": "d" * 32}
    child = {"id": "child-one", **accepted, "embedding_model": MODEL_TAG, "local_only": False,
             "quarantined": False, "rank_score": 0.92, "similarity": 0.92,
             "semantic_similarity": 0.9, "lexical_score": 0.8, "bm25_score": 1.0,
             "retrieval_mode": "cached", "candidate_count": 12}
    association = {"parent_id": parent["id"], "acceptance": accepted, "passage_ordinal": 0,
                   "start": 400, "end": 490, "input_sha256": "e" * 64}
    original = {"id": parent["id"], "rank_score": 0.5, "similarity": 0.5}

    def invoke(candidate, state):
        mesh = SimpleNamespace(store=PublicStore({parent["id"]: state} if state else {}))
        return collapse_parents([candidate], [original], {parent["id"]: parent},
                                {child["id"]: [association]}, mesh, 64)

    rows, stats = invoke(child, parent)
    assert len(rows) == 1 and rows[0]["rank_score"] == 0.92
    assert rows[0]["faithful_document_ids"] == ["source-first", "source-alias"]
    assert rows[0]["supporting_spans"][0]["start"] == 400
    assert stats["parent_cap_violation"] is False
    tie = {**child, "similarity": 0.93}
    mesh = SimpleNamespace(store=PublicStore({parent["id"]: parent}))
    tie_rows, _ = collapse_parents([child, tie], [original], {parent["id"]: parent},
                                  {child["id"]: [association]}, mesh, 64)
    assert tie_rows[0]["similarity"] == 0.93
    for excluded in ({**child, "local_only": True}, {**child, "quarantined": True},
                     {**child, "incarnation": "f" * 32}, {**child, "store_generation": "f" * 32},
                     {**child, "revision": 2}, {**child, "embedding_model": "hash-384"}):
        rows, stats = invoke(excluded, parent)
        assert rows[0]["match_source"] == "original_parent"
        assert stats["dropped_stale_or_private_associations"] == 1
    for changed in ({**parent, "local_only": True}, {**parent, "quarantined": True},
                    {**parent, "revision": 2}, {**parent, "incarnation": "f" * 32},
                    {**parent, "store_generation": "f" * 32}, None):
        rows, stats = invoke(child, changed)
        assert rows == []
        assert stats["dropped_stale_or_private_associations"] == 1


def check_repeated_paragraph_associations():
    common = " ".join(f"shared{index}" for index in range(400))
    parents = {
        "parent-a": {"id": "parent-a", "content": "Harbor calibration records remain available.\n\n" + common,
                     "revision": 1, "incarnation": "a" * 32, "store_generation": "b" * 32,
                     "embedding_model": MODEL_TAG, "local_only": False, "quarantined": False},
        "parent-b": {"id": "parent-b", "content": "Lantern telemetry uploads metrics regularly.\n\n" + common,
                     "revision": 1, "incarnation": "c" * 32, "store_generation": "b" * 32,
                     "embedding_model": MODEL_TAG, "local_only": False, "quarantined": False},
    }
    sources = [{"document_id": f"source-{index}", "faithful_public": True,
                "acceptance": {"memory_id": parent["id"], "revision": 1,
                               "incarnation": parent["incarnation"], "store_generation": parent["store_generation"]},
                "current_revision": 1, "stored_sha256": content_hash(parent["content"])}
               for index, parent in enumerate(parents.values())]

    class GuardedChildren:
        def __init__(self):
            self.rows = {}
            self.store = SimpleNamespace(embedding_job_stats=lambda: {"pending_count": 0})

        def learn_many(self, inputs, *, atomic, deduplicate):
            assert atomic is False and deduplicate is False
            outcomes = []
            for item in inputs:
                identifier = f"derived-{len(self.rows)}"
                row = {"id": identifier, "content": item["content"], "revision": 1,
                       "incarnation": "d" * 32, "store_generation": "e" * 32,
                       "embedding_model": MODEL_TAG, "local_only": False, "quarantined": False}
                self.rows[identifier] = row
                outcomes.append({"memory_id": identifier, "revision": 1, "incarnation": row["incarnation"],
                                 "store_generation": row["store_generation"], "local_only": False, "quarantined": False})
            return outcomes

        def get(self, identifier, *, include_private):
            assert include_private is False
            return self.rows.get(identifier)

        def wait_for_indexed(self, identifiers, *, timeout, include_private, target_model,
                             expected_revisions, expected_incarnations, expected_store_generation):
            assert timeout == 0 and include_private is False and target_model == MODEL_TAG
            assert expected_store_generation == "e" * 32
            assert all(expected_revisions[identifier] == self.rows[identifier]["revision"] for identifier in identifiers)
            assert all(expected_incarnations[identifier] == self.rows[identifier]["incarnation"] for identifier in identifiers)
            return {"status": "indexed", "indexed": identifiers}

    child_mesh = GuardedChildren()
    parent_mesh = SimpleNamespace(get=lambda identifier, include_private: parents[identifier])
    helpers = SimpleNamespace(append_jsonl=lambda stream, value: stream.write(json.dumps(value) + "\n"))
    resources = SimpleNamespace(sample=lambda _phase: None)
    fidelity, coverage = io.StringIO(), io.StringIO()
    _, associations, counts = ingest_children(
        parent_mesh, child_mesh, sources, WordTokenizer(), helpers, None, fidelity, coverage, resources)
    by_body = {}
    for identifier, row in child_mesh.rows.items():
        by_body.setdefault(row["content"], []).append(identifier)
    repeated = [identifiers for identifiers in by_body.values() if len(identifiers) == 2]
    assert repeated
    for identifiers in repeated:
        attached = {record["parent_id"] for identifier in identifiers for record in associations[identifier]}
        assert attached == {"parent-a", "parent-b"}
    coverage_rows = [json.loads(line) for line in coverage.getvalue().splitlines()]
    assert len(coverage_rows) == 2 and all(row["full_nonspace_coverage"] for row in coverage_rows)
    assert counts["guarded_uncovered_nonspace_characters"] == 0
    assert counts["parents_with_full_nonspace_coverage"] == 2
    assert counts["identity_bound_indexed_child_memories"] == len(child_mesh.rows)
if __name__ == "__main__":
    check_coverage()
    check_budget()
    check_policy_and_identity()
    check_repeated_paragraph_associations()
    print("Passed tiny passage coverage, repeated-parent associations, C64/C96 heap, public policy and complete identity checks.")
