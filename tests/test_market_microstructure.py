"""
Unit Tests: Indian Market Microstructure & Circuit Freezes
"""

import unittest
from execution_router import ExecutionRouter
from ledger import Ledger


class TestMarketMicrostructure(unittest.TestCase):

    def test_upper_circuit_detection(self):
        """Upper circuit occurs when open==high==low==close and price rises >= 4.5% above prev close."""
        router = ExecutionRouter(ledger=None)
        is_frozen = router.is_upper_circuit(prev_close=100.0, open_px=105.0, high_px=105.0, low_px=105.0, close_px=105.0)
        self.assertTrue(is_frozen)

        # Normal trading day
        is_normal = router.is_upper_circuit(prev_close=100.0, open_px=101.0, high_px=106.0, low_px=99.0, close_px=104.0)
        self.assertFalse(is_normal)

    def test_lower_circuit_detection(self):
        """Lower circuit occurs when open==high==low==close and price falls <= -4.5% below prev close."""
        router = ExecutionRouter(ledger=None)
        is_frozen = router.is_lower_circuit(prev_close=100.0, open_px=95.0, high_px=95.0, low_px=95.0, close_px=95.0)
        self.assertTrue(is_frozen)


if __name__ == "__main__":
    unittest.main()
