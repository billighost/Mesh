"""Exact-evidence regression counts use stdlib fixtures without loading a model."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "benchmarks"))
import production_retrieval as scoring


def candidate(*documents):
    return {"faithful_document_ids": list(documents)}


class PairedScoringContracts(unittest.TestCase):
    def test_equal_average_does_not_hide_a_lost_top_one_hit(self):
        rows = [
            {"query_id": "lost", "original_sdk_top10": [candidate("gold-lost")],
             "final_sdk_top10": [candidate("distractor")]},
            {"query_id": "gained", "original_sdk_top10": [candidate("distractor")],
             "final_sdk_top10": [candidate("gold-gained")]},
            {"query_id": "answerless", "original_sdk_top10": [], "final_sdk_top10": [candidate("distractor")]},
        ]
        qrels = {"lost": {"gold-lost": 1}, "gained": {"gold-gained": 1}, "answerless": {}}
        counts = scoring.paired_hit_at_1_counts(rows, "final_sdk_top10", "original_sdk_top10", qrels,
                                               ["lost", "gained", "answerless"])
        self.assertEqual(counts, {"answered_queries": 2, "baseline_hits": 1, "candidate_hits": 1,
                                  "lost_baseline_hits": 1, "gained_hits": 1})

    def test_labels_cannot_select_a_later_deduplicated_source_alias(self):
        rows = [{"query_id": "exact", "original_sdk_top10": [candidate("first-accepted", "gold")],
                 "final_sdk_top10": [candidate("gold")]}]
        counts = scoring.paired_hit_at_1_counts(rows, "final_sdk_top10", "original_sdk_top10",
                                               {"exact": {"gold": 1}}, ["exact"])
        self.assertEqual(counts["baseline_hits"], 0)
        self.assertEqual(counts["candidate_hits"], 1)
        self.assertEqual(counts["gained_hits"], 1)


if __name__ == "__main__":
    unittest.main()
