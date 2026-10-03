#!/usr/bin/env python3
"""
Unit Tests for Canonical Fail-Closed Payment Reconciler
Verifies fix for P0 Duplicate Receipt and Loose Fuzzing Vulnerability.
"""

import sqlite3
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.payment_reconciler_canonical import (
    CanonicalPaymentReconciler,
    PaymentReceipt,
    PendingBounty,
    ReconciliationVerdict
)


class TestPaymentReconcilerFailClosed(unittest.TestCase):

    def setUp(self):
        # In-memory SQLite for atomic testing
        self.conn = sqlite3.connect(":memory:")
        self.reconciler = CanonicalPaymentReconciler(db_conn=self.conn)

    def tearDown(self):
        self.conn.close()

    def test_single_bounty_exact_match(self):
        """1:1 exact match settles cleanly."""
        b = PendingBounty(
            canonical_id="BOUNTY_101",
            title="Fix async race condition",
            reward_amount=150.0,
            currency="USDC",
            platform="GitHub",
            stage="PR_MERGED"
        )
        r = PaymentReceipt(
            network="BASE_EVM",
            currency="USDC",
            amount=150.0,
            tx_hash="0xabc123456789",
            recipient="0x24A2151Ec787a2C5c81412A888c3a9d9eEc3beEA"
        )

        res = self.reconciler.reconcile_bounties([b], [r])
        self.assertEqual(res["BOUNTY_101"]["verdict"], ReconciliationVerdict.MATCH_SETTLED)
        self.assertEqual(res["BOUNTY_101"]["tx_hash"], "0xabc123456789")

        # Verify persisted in database
        self.assertTrue(self.reconciler.is_tx_already_settled("0xabc123456789"))

    def test_prevent_single_receipt_claiming_multiple_bounties(self):
        """
        CRITICAL P0 TEST:
        Two bounties of $99 each. One receipt of $99 arrives.
        Old buggy code confirmed BOTH bounties.
        Fail-closed code MUST detect ambiguity and hold both without memo!
        """
        b1 = PendingBounty(
            canonical_id="BOUNTY_A",
            title="Task A",
            reward_amount=99.0,
            currency="USDC",
            platform="Algora",
            stage="PR_MERGED"
        )
        b2 = PendingBounty(
            canonical_id="BOUNTY_B",
            title="Task B",
            reward_amount=99.0,
            currency="USDC",
            platform="Opire",
            stage="PR_MERGED"
        )
        r = PaymentReceipt(
            network="BASE_EVM",
            currency="USDC",
            amount=99.0,
            tx_hash="0xsingle_receipt_hash",
            recipient="0x24A2151Ec787a2C5c81412A888c3a9d9eEc3beEA"
        )

        res = self.reconciler.reconcile_bounties([b1, b2], [r])

        # Both MUST NOT be settled! Must fail-closed to AMBIGUOUS_HOLD
        self.assertEqual(res["BOUNTY_A"]["verdict"], ReconciliationVerdict.AMBIGUOUS_HOLD)
        self.assertEqual(res["BOUNTY_B"]["verdict"], ReconciliationVerdict.AMBIGUOUS_HOLD)

        # Database must NOT record any settlement
        self.assertFalse(self.reconciler.is_tx_already_settled("0xsingle_receipt_hash"))

    def test_explicit_memo_resolves_identical_amount_ambiguity(self):
        """When multiple bounties share identical amount, explicit memo resolves 1:1 safely."""
        b1 = PendingBounty(canonical_id="BOUNTY_A", title="Task A", reward_amount=99.0, currency="USDC", platform="Algora", stage="PR_MERGED")
        b2 = PendingBounty(canonical_id="BOUNTY_B", title="Task B", reward_amount=99.0, currency="USDC", platform="Opire", stage="PR_MERGED")

        # Receipt explicitly references BOUNTY_B
        r = PaymentReceipt(
            network="BASE_EVM",
            currency="USDC",
            amount=99.0,
            tx_hash="0xb_receipt",
            recipient="0x24A2151Ec787a2C5c81412A888c3a9d9eEc3beEA",
            memo="Payout for BOUNTY_B solver"
        )

        res = self.reconciler.reconcile_bounties([b1, b2], [r])
        # Only BOUNTY_B is settled!
        self.assertEqual(res["BOUNTY_B"]["verdict"], ReconciliationVerdict.MATCH_SETTLED)
        # BOUNTY_A remains NO_MATCH
        self.assertEqual(res["BOUNTY_A"]["verdict"], ReconciliationVerdict.NO_MATCH)

    def test_duplicate_tx_hash_already_in_db_rejected(self):
        """If a receipt tx_hash was previously settled in DB, subsequent reconciliations reject it."""
        # Pre-populate DB with settled tx
        with self.conn:
            self.conn.execute("""
            INSERT INTO settled_payments (canonical_id, tx_hash, network, currency, amount, recipient_wallet)
            VALUES ('OLD_BOUNTY', '0xalready_used_tx', 'BASE_EVM', 'USDC', 100.0, '0xwallet')
            """)

        b = PendingBounty(canonical_id="NEW_BOUNTY", title="New", reward_amount=100.0, currency="USDC", platform="GitHub", stage="PR_MERGED")
        r = PaymentReceipt(network="BASE_EVM", currency="USDC", amount=100.0, tx_hash="0xalready_used_tx", recipient="0xwallet")

        res = self.reconciler.reconcile_bounties([b], [r])
        self.assertEqual(res["NEW_BOUNTY"]["verdict"], ReconciliationVerdict.NO_MATCH)

    def test_strict_amount_no_fuzzing(self):
        """Eliminates ±0.10 USD fuzzing: $99.00 does NOT match $99.05 or $98.95."""
        b = PendingBounty(canonical_id="BOUNTY_EXACT", title="Exact", reward_amount=99.00, currency="USDC", platform="GitHub", stage="PR_MERGED")
        # Receipt is slightly off by $0.05
        r = PaymentReceipt(network="BASE_EVM", currency="USDC", amount=99.05, tx_hash="0xfuzz_tx", recipient="0xwallet")

        res = self.reconciler.reconcile_bounties([b], [r])
        self.assertEqual(res["BOUNTY_EXACT"]["verdict"], ReconciliationVerdict.NO_MATCH)


if __name__ == "__main__":
    unittest.main()
