"""
Unit Tests: Double-Entry SQLite Portfolio Ledger & Yield Accrual
"""

import os
import sqlite3
import tempfile
import unittest
from ledger import Ledger


class TestLedgerAccounting(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.ledger = Ledger(db_path=self.temp_db.name)
        self.ledger.initialize_portfolio(initial_capital=100000.0, inception_date="2026-09-01", reset=True)

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            try:
                os.remove(self.temp_db.name)
            except OSError:
                pass

    def test_initial_deposit_balance(self):
        """Verifies initial deposit creates a valid cash ledger entry."""
        cash = self.ledger.get_state()["current_cash"]
        self.assertEqual(cash, 100000.0)

    def test_cash_yield_accrual_in_mark_to_market(self):
        """Verifies daily risk-free interest accrues on idle cash during mark-to-market."""
        start_cash = self.ledger.get_state()["current_cash"]
        res = self.ledger.mark_to_market(
            date_str="2026-09-02",
            closing_prices={},
            is_risk_on=True
        )
        self.assertGreater(res["yield_accrued"], 0.0)
        new_cash = self.ledger.get_state()["current_cash"]
        self.assertAlmostEqual(new_cash, start_cash + res["yield_accrued"], places=2)

    def test_buy_trade_cash_deduction(self):
        """Verifies buy execution deducts gross cost, slippage, and updates positions."""
        total_deducted = self.ledger.execute_buy(
            date_str="2026-09-01",
            symbol="TATASTEEL",
            shares=100,
            price=150.0,
            reason="TEST_ENTRY"
        )
        expected_cash = 100000.0 - total_deducted
        self.assertAlmostEqual(self.ledger.get_state()["current_cash"], expected_cash, places=2)

        positions = self.ledger.get_positions()
        self.assertIn("TATASTEEL", positions)
        self.assertEqual(positions["TATASTEEL"]["shares"], 100)


if __name__ == "__main__":
    unittest.main()
