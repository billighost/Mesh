"""Collect the existing stdlib passage identity, coverage and cap contracts."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "benchmarks"))
import check_passage_contract as contracts
import passage_retrieval as passage
import production_retrieval as production
import retrieval_ci as ci


class PassageContracts(unittest.TestCase):
    def test_complete_source_coverage_and_preserved_short_parents(self):
        contracts.check_coverage()

    def test_bounded_parent_heap_matches_independent_full_sort(self):
        contracts.check_budget()

    def test_final_public_policy_and_full_identity_revalidation(self):
        contracts.check_policy_and_identity()

    def test_repeated_paragraphs_keep_each_parent_association(self):
        contracts.check_repeated_paragraph_associations()

    def test_explicit_absent_ordinal_preserves_parent_ties_and_derived_zero(self):
        parent = {"id": "parent-one", "revision": 1, "incarnation": "a" * 32,
                  "store_generation": "b" * 32, "embedding_model": passage.MODEL_TAG,
                  "local_only": False, "quarantined": False}
        metadata = dict(parent, faithful_document_ids=["source-first"])
        original = production.decorate_results([dict(parent, rank_score=0.5, similarity=0.5)],
                                               {parent["id"]: [{"document_id": "source-first", "faithful_public": True}]})
        self.assertIsNone(original[0]["selected_passage_ordinal"])
        accepted = {"revision": 1, "incarnation": "c" * 32, "store_generation": "d" * 32}
        child = {"id": "child-one", **accepted, "embedding_model": passage.MODEL_TAG,
                 "local_only": False, "quarantined": False, "similarity": 0.5,
                 "semantic_similarity": 0.5, "lexical_score": 0.5, "bm25_score": 1.0,
                 "retrieval_mode": "cached", "candidate_count": 1}
        association = {"parent_id": parent["id"], "acceptance": accepted, "passage_ordinal": 0,
                       "start": 0, "end": 12, "input_sha256": "e" * 64}
        mesh = SimpleNamespace(store=contracts.PublicStore({parent["id"]: parent}))
        for score, expected_source, expected_ordinal in ((0.5, "original_parent", None), (0.6, "derived_passage", 0)):
            rows, stats = passage.collapse_parents([dict(child, rank_score=score)], original,
                {parent["id"]: metadata}, {child["id"]: [association]}, mesh, 64)
            self.assertEqual(rows[0]["match_source"], expected_source)
            self.assertEqual(rows[0]["selected_passage_ordinal"], expected_ordinal)
            self.assertEqual(rows[0]["faithful_document_ids"], ["source-first"])
            self.assertFalse(stats["parent_cap_violation"])

    def test_worker_error_diagnostics_exclude_arguments_paths_and_unknown_fields(self):
        marker = "FAKE_BODY_NOT_EXPORTED C:/Users/EXAMPLE"
        try:
            passage._same_parent_quality({"rank_score": 0.5, "similarity": 0.5, "selected_passage_ordinal": marker})
        except ValueError as exc:
            details = production.worker_failure_details(exc, "passages")
        else:
            self.fail("Malformed ordinal stopped raising ValueError")
        self.assertEqual(details["exception_type"], "ValueError")
        self.assertTrue(any(frame["source"] == "passage_retrieval.py" for frame in details["source_frames"]))
        partial = {"status": "failed", "failure_details": details, "content": marker,
                   "temporary_mesh_home": marker, "failure_message": marker}
        projected = ci.worker_failure_projection(partial, "passages")
        self.assertEqual(projected, details)
        self.assertNotIn(marker, str(projected))
        partial["failure_details"] = {"exception_type": marker, "source_frames": [
            {"source": marker, "line": 1}, {"source": "store.py", "line": True}]}
        self.assertEqual(ci.worker_failure_projection(partial, "passages"),
                         {"stage": "passages", "exception_type": "other", "source_frames": []})


if __name__ == "__main__":
    unittest.main()
