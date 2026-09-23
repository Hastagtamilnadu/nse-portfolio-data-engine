"""
Unit Tests: Corporate Action Heuristics & Mathematical Adjustments
"""

import os
import unittest
from corporate_action_watcher import CorporateActionWatcher


class TestCorporateActions(unittest.TestCase):

    def setUp(self):
        ref_csv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reference_corporate_actions.csv")
        self.watcher = CorporateActionWatcher(reference_csv_path=ref_csv)

    def test_known_stock_splits(self):
        """Verifies confirmed splits from reference dataset."""
        factor, action, src = self.watcher.check_and_resolve_action("TATASTEEL", "2022-07-28", prev_close=960.0, open_px=96.0)
        self.assertEqual(factor, 10.0)
        self.assertEqual(action, "SPLIT")
        self.assertEqual(src, "REFERENCE_TABLE")

        factor, action, src = self.watcher.check_and_resolve_action("DIXON", "2021-03-18", prev_close=20000.0, open_px=4000.0)
        self.assertEqual(factor, 5.0)
        self.assertEqual(action, "SPLIT")

    def test_demerger_factor_neutrality(self):
        """Verifies demerger parent drops do not trigger false artificial split boosts."""
        factor, action, src = self.watcher.check_and_resolve_action("STAR", "2024-12-06", prev_close=1350.0, open_px=650.0)
        self.assertEqual(factor, 1.0)
        self.assertEqual(action, "DEMERGER")

    def test_heuristic_fallback(self):
        """Verifies mathematical ratio detection for unlisted splits within 1.70 tolerance."""
        factor, action, src = self.watcher.check_and_resolve_action("XYZ", "2026-01-15", prev_close=195.0, open_px=100.0)
        self.assertEqual(factor, 2.0)
        self.assertEqual(action, "SPLIT_OR_BONUS_2X")


if __name__ == "__main__":
    unittest.main()
