"""Tiny stdlib-only C4 contracts. No Mesh instance, native library or model is loaded."""
from __future__ import annotations

from collections import Counter
import contextlib
import copy
import io
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "benchmarks"))
import passage_retrieval as passage
import retrieval_c4 as c4
from retrieval_c4_model import LIMITS, MODEL, RESOURCE_MAXIMA


class WordTokenizer:
    def encode(self, text, add_special_tokens=False):
        if add_special_tokens is not False:
            raise AssertionError("Unexpected special tokens")
        offsets = [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]
        return SimpleNamespace(ids=list(range(len(offsets))), offsets=offsets)


def fixtures(count=6):
    rows, bodies, records = [], {}, {}
    for index in range(count):
        identifier = f"parent-{index:02d}"
        body = {"id": identifier, "namespace": "fixture", "revision": 1, "incarnation": "a" * 32,
                "store_generation": "b" * 32, "embedding_model": passage.MODEL_TAG,
                "local_only": False, "quarantined": False, "content": f"Opaque item {index} has three slots."}
        row = {key: value for key, value in body.items() if key != "content"}
        row.update(match_source="original_parent", faithful_document_ids=[f"document-{index}"],
                   rank_score=0.9 - index / 100, similarity=0.8 - index / 100, candidate_count=64)
        rows.append(row)
        bodies[identifier] = body
        records[identifier] = {"faithful_public": True, "stored_sha256": passage.content_hash(body["content"]),
            "current_revision": 1, "acceptance": {"memory_id": identifier, "revision": 1,
                "incarnation": body["incarnation"], "store_generation": body["store_generation"]}}
    return rows, bodies, records


class PublicParents:
    namespace = "fixture"

    def __init__(self, bodies):
        self.bodies = bodies

    def get(self, identifier, include_private=False):
        if include_private is not False:
            raise AssertionError("Private access attempted")
        row = self.bodies.get(identifier)
        if row is None or row["local_only"] or row["quarantined"]:
            return None
        return copy.deepcopy(row)


class Contracts(unittest.TestCase):
    def test_actions_guard_before_loading_helpers_or_native_code(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(c4, "checked_helpers") as helper:
            with self.assertRaisesRegex(c4.C4Unavailable, "RemoteActionsRequired"):
                c4.main(["freeze", "--repository", "fixture", "--base-protocol", "fixture", "--output", "fixture"])
            helper.assert_not_called()

    def test_first_four_only_and_tail_unchanged(self):
        rows, _, _ = fixtures()
        ranked = c4.reorder_c4(rows, [0.1, 0.4, 0.3, 0.2])
        self.assertEqual([row["id"] for row in ranked[:4]], [rows[index]["id"] for index in (1, 2, 3, 0)])
        self.assertEqual(ranked[4:], rows[4:])
        for row in ranked[:4]:
            original = next(item for item in rows if item["id"] == row["id"])
            self.assertEqual({key: row[key] for key in original}, original)
        self.assertFalse(any("ce_score" in row for row in ranked[4:]))

    def test_logit_ties_keep_original_order_without_answerability_threshold(self):
        rows, _, _ = fixtures(4)
        ranked = c4.reorder_c4(rows, [-3.0, -3.0, -4.0, -5.0])
        self.assertEqual([row["id"] for row in ranked], [row["id"] for row in rows])

    def test_inference_rejects_more_than_four_pairs_before_model_use(self):
        encoder = c4.FrozenCrossEncoder.__new__(c4.FrozenCrossEncoder)
        with self.assertRaisesRegex(c4.C4Failure, "C4PairCapExceeded"):
            encoder.score("query", [("body", "accepted_original_body")] * 5)

    def test_nonfinite_or_incomplete_logits_rejected(self):
        rows, _, _ = fixtures(4)
        for scores in ([1.0], [1.0, 2.0, 3.0, float("nan")], [1.0, 2.0, 3.0, float("inf")]):
            with self.subTest(scores=scores), self.assertRaises(c4.C4Failure):
                c4.reorder_c4(rows, scores)

    def test_parent_order_and_selected_span_identity_are_frozen(self):
        rows, _, _ = fixtures(4)
        for actual in ([*reversed(rows)], [rows[0], rows[0], *rows[2:]]):
            with self.assertRaisesRegex(c4.C4Failure, "ImmutableParentOrderOrSpanChanged"):
                c4.assert_same_parents(actual, rows)
        derived = dict(rows[0], match_source="derived_passage", selected_passage_id="ephemeral-a",
                       selected_passage_ordinal=2, supporting_spans=[{"memory_id": rows[0]["id"], "revision": 1,
                           "incarnation": "a" * 32, "store_generation": "b" * 32, "start": 2, "end": 7,
                           "span_content_sha256": "c" * 64}])
        recreated = dict(derived, selected_passage_id="ephemeral-b")
        c4.assert_same_parents([recreated], [derived])
        changed = copy.deepcopy(recreated)
        changed["supporting_spans"][0]["end"] = 8
        with self.assertRaises(c4.C4Failure):
            c4.assert_same_parents([changed], [derived])

    def test_verified_exact_span_and_offset_boolean_rejection(self):
        rows, bodies, records = fixtures(1)
        body = bodies[rows[0]["id"]]["content"]
        row = dict(rows[0], match_source="derived_passage", supporting_spans=[{
            "memory_id": rows[0]["id"], "revision": 1, "incarnation": "a" * 32, "store_generation": "b" * 32,
            "start": 0, "end": len(body), "span_content_sha256": passage.content_hash(body)}])
        self.assertEqual(c4.verified_inputs([row], PublicParents(bodies), records, "fixture", passage),
                         [(body, "exact_selected_source_span")])
        row["supporting_spans"][0]["start"] = False
        with self.assertRaisesRegex(c4.C4Failure, "SupportingSpanBytesChanged"):
            c4.verified_inputs([row], PublicParents(bodies), records, "fixture", passage)

    def test_query_and_body_prefix_bounds_are_independent(self):
        text = " ".join(f"word{index}" for index in range(300))
        for maximum in (29, 224):
            prefix, original, kept = c4.bounded_prefix(text, WordTokenizer(), maximum)
            self.assertEqual((original, kept), (300, maximum))
            self.assertTrue(text.startswith(prefix))

    def test_selected_span_cannot_be_silently_truncated(self):
        encoder = c4.FrozenCrossEncoder.__new__(c4.FrozenCrossEncoder)
        encoder.tokenizer = WordTokenizer()
        with self.assertRaisesRegex(c4.C4Failure, "SelectedPassageExceedsTokenLimit"):
            encoder.score("query", [(" ".join(["word"] * 225), "exact_selected_source_span")])

    def test_source_body_and_every_identity_fence_are_enforced(self):
        for key, value in (("revision", 2), ("incarnation", "c" * 32), ("store_generation", "d" * 32),
                           ("local_only", True), ("quarantined", True), ("content", "Changed source.")):
            rows, bodies, records = fixtures(1)
            bodies[rows[0]["id"]][key] = value
            with self.subTest(key=key), self.assertRaises((c4.C4Failure, ValueError)):
                c4.verified_inputs(rows, PublicParents(bodies), records, "fixture", passage)

    def test_source_namespace_and_span_hash_fences(self):
        rows, bodies, records = fixtures(1)
        with self.assertRaisesRegex(c4.C4Failure, "SourceNamespaceChanged"):
            c4.verified_inputs(rows, PublicParents(bodies), records, "wrong", passage)
        rows[0].update(match_source="derived_passage", supporting_spans=[{"memory_id": rows[0]["id"], "revision": 1,
            "incarnation": "a" * 32, "store_generation": "b" * 32, "start": 0, "end": 6,
            "span_content_sha256": "f" * 64}])
        with self.assertRaisesRegex(c4.C4Failure, "SupportingSpanBytesChanged"):
            c4.verified_inputs(rows, PublicParents(bodies), records, "fixture", passage)

    def test_post_inference_change_emits_no_result(self):
        for key, value in (("revision", 2), ("incarnation", "e" * 32), ("store_generation", "f" * 32),
                           ("local_only", True), ("quarantined", True), ("content", "Changed during inference.")):
            rows, bodies, records = fixtures(4)
            result = {"query_id": "query-a", "corpus_id": "corpus", "candidate_cap": 64,
                      "raw_hybrid_top64": rows, "final_sdk_top10": rows,
                      "candidate_budget": {"parent_cap_violation": False, "parent_materialization_peak_count": 4,
                                           "parent_candidate_cap": 64}}
            observer = c4.PassageObserver.__new__(c4.PassageObserver)
            observer.frozen = {64: {"query-a": copy.deepcopy(result)}}
            observer.parent_mesh, observer.records, observer.passage = PublicParents(bodies), records, passage
            observer.measurements, observer.counts = [], Counter()

            def score(query, inputs):
                bodies[rows[0]["id"]][key] = value
                return [1.0] * len(inputs), {"scored_pairs": len(inputs), "inference_batches": 1, "input_accounting": []}

            observer.encoder = SimpleNamespace(score=score)
            with self.subTest(key=key), self.assertRaises((c4.C4Failure, ValueError)):
                observer.evaluate({"query_id": "query-a", "query": "How many slots?", "namespace": "fixture"},
                                  64, lambda query, cap: copy.deepcopy(result))
            self.assertEqual(observer.measurements, [])
            self.assertEqual(observer.counts["measured_scored_pairs"], 4)

    def test_ce_warmup_does_not_add_retrieval_calls_and_loads_one_session(self):
        rows, bodies, records = fixtures(4)
        observer = c4.PassageObserver.__new__(c4.PassageObserver)
        observer.frozen, observer.counts, observer.encoder = {}, Counter(), None
        observer.args = SimpleNamespace(phase="c4", model_directory=Path("fixture"))
        observer.helpers, observer.passage = None, passage
        results = {}
        for cap in (64, 96):
            result = {"query_id": "query-a", "corpus_id": "corpus", "candidate_cap": cap,
                      "raw_hybrid_top64": rows, "final_sdk_top10": rows,
                      "candidate_budget": {"parent_cap_violation": False, "parent_materialization_peak_count": 4,
                                           "parent_candidate_cap": cap}}
            results[cap] = result
            observer.frozen[cap] = {"query-a": copy.deepcopy(result)}
        retrieve = mock.Mock(side_effect=lambda query, cap: copy.deepcopy(results[cap]))
        score = mock.Mock(return_value=([1.0] * 4, {"scored_pairs": 4, "inference_batches": 1, "input_accounting": []}))
        session = SimpleNamespace(score=score)
        query = {"query_id": "query-a", "query": "How many slots?", "namespace": "fixture"}
        with mock.patch.object(c4, "FrozenCrossEncoder", return_value=session) as loader:
            observer.before_namespace([query], retrieve, PublicParents(bodies), list(records.values()))
            observer.before_namespace([query], retrieve, PublicParents(bodies), list(records.values()))
        loader.assert_called_once()
        self.assertEqual(retrieve.call_count, 4)
        self.assertEqual(score.call_count, 1)
        self.assertEqual(observer.counts["session_load_count"], 1)

    def test_resource_repetitions_do_not_repeat_quality_exports(self):
        rows, bodies, records = fixtures(4)
        observer = c4.PassageObserver.__new__(c4.PassageObserver)
        observer.frozen, observer.counts, observer.encoder, observer.measurements = {}, Counter(), None, []
        observer.args, observer.resource_passes = SimpleNamespace(phase="baseline"), 2
        observer.parent_mesh, observer.records, observer.passage = PublicParents(bodies), records, passage
        results = {}
        for cap in (64, 96):
            result = {"query_id": "query-a", "corpus_id": "corpus", "candidate_cap": cap,
                      "raw_hybrid_top64": rows, "final_sdk_top10": rows,
                      "candidate_budget": {"parent_cap_violation": False, "parent_materialization_peak_count": 4,
                                           "parent_candidate_cap": cap}}
            results[cap] = result
            observer.frozen[cap] = {"query-a": copy.deepcopy(result)}
        retrieve = lambda query, cap: copy.deepcopy(results[cap])
        query = {"query_id": "query-a", "query": "How many slots?", "namespace": "fixture"}
        for cap in (64, 96):
            observer.query(query, cap, retrieve)
        observer.after_namespace([query], retrieve)
        with contextlib.redirect_stdout(io.StringIO()), passage.owned_runtime_directory() as root:
            manifest = {}
            observer.finish(manifest, root)
            self.assertEqual(manifest["quality_export_query_cells"], 2)
            self.assertEqual(len(observer.measurements), 4)
            self.assertEqual(len((root / "query-resources.jsonl").read_text(encoding="utf-8").splitlines()), 4)

    def test_fixed_resource_gates_qualify_matching_samples(self):
        baseline = [{"query_id": f"query-{index}", "candidate_cap": cap, "wall_ms": 10.0, "process_cpu_ms": 4.0}
                    for index in range(100) for cap in (64, 96)]
        candidate = [dict(row, wall_ms=30.0, process_cpu_ms=14.0) for row in baseline]
        gates, qualified, enough = c4.resource_gates(baseline, candidate, 300 * 1024**2, 400 * 1024**2)
        self.assertTrue(qualified and enough)
        self.assertEqual({name: row["maximum"] for name, row in gates.items()}, RESOURCE_MAXIMA)
        self.assertEqual(gates["delta_p99_ms"]["observed"], 20.0)

    def test_small_samples_are_explicitly_unqualified(self):
        samples = [{"query_id": "query-a", "candidate_cap": cap, "wall_ms": 10.0, "process_cpu_ms": 4.0} for cap in (64, 96)]
        _, qualified, enough = c4.resource_gates(samples, samples, 1, 1)
        self.assertFalse(qualified or enough)

    def test_unpaired_duplicate_or_nonfinite_resource_samples_fail(self):
        samples = [{"query_id": "query-a", "candidate_cap": 64, "wall_ms": 10.0, "process_cpu_ms": 4.0}]
        for candidate in ([], samples * 2, [dict(samples[0], wall_ms=float("nan"))]):
            with self.subTest(candidate=candidate), self.assertRaises(c4.C4Failure):
                c4.resource_gates(samples, candidate, 1, 1)

    def test_fixture_hash_and_size_are_checked_without_model_reads(self):
        class Helpers:
            def safe_member(self, directory, filename):
                kind = "model" if filename == MODEL["model_file"] else "tokenizer"
                return SimpleNamespace(kind=kind, stat=lambda: SimpleNamespace(st_size=MODEL[kind + "_bytes"]))

            def digest_file(self, path):
                return MODEL[path.kind + "_sha256"]

        helpers = Helpers()
        c4.verify_fixture(Path("fixture"), helpers)
        with mock.patch.object(helpers, "digest_file", return_value="0" * 64):
            with self.assertRaisesRegex(c4.C4Unavailable, "PinnedFixtureHashMismatch"):
                c4.verify_fixture(Path("fixture"), helpers)

    def test_hook_failure_removes_only_owned_runtime_directory(self):
        root = None
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "HookFailure"):
            with passage.owned_runtime_directory() as root:
                (root / "owned-marker.txt").write_text("fixture", encoding="utf-8")
                raise RuntimeError("HookFailure")
        self.assertIsNotNone(root)
        self.assertFalse(root.exists())

    def test_frozen_limits_fit_the_pair_without_passage_truncation(self):
        self.assertEqual(LIMITS["maximum_query_tokens"] + LIMITS["maximum_passage_tokens"] + 3,
                         LIMITS["maximum_pair_tokens"])
        self.assertEqual(LIMITS["parent_cap"], 4)
        self.assertEqual(LIMITS["batch_items"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=1)
