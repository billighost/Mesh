"""Collect the existing stdlib passage identity, coverage and cap contracts."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "benchmarks"))
import check_passage_contract as contracts


class PassageContracts(unittest.TestCase):
    def test_complete_source_coverage_and_preserved_short_parents(self):
        contracts.check_coverage()

    def test_bounded_parent_heap_matches_independent_full_sort(self):
        contracts.check_budget()

    def test_final_public_policy_and_full_identity_revalidation(self):
        contracts.check_policy_and_identity()

    def test_repeated_paragraphs_keep_each_parent_association(self):
        contracts.check_repeated_paragraph_associations()


if __name__ == "__main__":
    unittest.main()
