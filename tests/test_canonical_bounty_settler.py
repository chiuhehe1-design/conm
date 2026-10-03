#!/usr/bin/env python3
"""
Unit Tests for Canonical Bounty Settlement Binding Engine
"""

import sqlite3
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.canonical_bounty_settler import (
    CanonicalBountySettler,
    BountySpec,
    SettlementIntent,
    PaymentCandidate,
    SettlementStatus,
    APPROVED_TOKEN_CONTRACTS
)


class TestCanonicalBountySettler(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.settler = CanonicalBountySettler(db_conn=self.conn)

        self.solver_wallet = "0x24A2151Ec787a2C5c81412A888c3a9d9eEc3beEA"
        self.base_usdc = APPROVED_TOKEN_CONTRACTS["BASE_EVM"]["USDC"]

        self.valid_bounty = BountySpec(
            bounty_id="opire#4599",
            repo="claude-builders-bounty/claude-builders-bounty",
            platform="Opire",
            pr_status="PR_MERGED",
            expected_amount=100.0,
            currency="USDC",
            authorized_solver_wallet=self.solver_wallet
        )

        self.valid_intent = SettlementIntent(
            settlement_id="stl_opire_4599",
            bounty_id="opire#4599",
            settlement_reference="claim_4599_sig",
            approved_amount=100.0,
            currency="USDC",
            platform_payout_address="0xOpirePlatformPayoutContract"
        )

        self.valid_candidate = PaymentCandidate(
            network="BASE_EVM",
            tx_hash="0xcanonical_settle_tx_001",
            token_contract=self.base_usdc,
            from_address="0xOpirePlatformPayoutContract",
            to_address=self.solver_wallet,
            amount=100.0,
            currency="USDC",
            block_number=1000,
            current_block_height=1050,  # 50 confirmations > 12
            memo="Payout for claim_4599_sig"
        )

    def tearDown(self):
        self.conn.close()

    def test_happy_path_payment_confirmed(self):
        """All 8 invariants satisfied -> PAYMENT_CONFIRMED."""
        receipt = self.settler.execute_atomic_settlement(
            self.valid_bounty, self.valid_intent, self.valid_candidate
        )
        self.assertEqual(receipt.status, SettlementStatus.PAYMENT_CONFIRMED)
        self.assertTrue(self.settler.is_tx_already_consumed(self.valid_candidate.tx_hash))

    def test_reject_unmerged_pr(self):
        """Cannot confirm payment if PR is not merged yet."""
        unmerged = BountySpec(
            bounty_id="opire#4599",
            repo="claude-builders-bounty",
            platform="Opire",
            pr_status="PR_OPEN",  # Still open!
            expected_amount=100.0,
            currency="USDC",
            authorized_solver_wallet=self.solver_wallet
        )
        receipt = self.settler.execute_atomic_settlement(
            unmerged, self.valid_intent, self.valid_candidate
        )
        self.assertEqual(receipt.status, SettlementStatus.PREREQUISITE_PR_NOT_MERGED)

    def test_reject_wrong_recipient_wallet(self):
        """Candidate sent to hacker or wrong wallet is rejected."""
        wrong_dest = PaymentCandidate(
            network="BASE_EVM",
            tx_hash="0xwrong_dest",
            token_contract=self.base_usdc,
            from_address="0xOpirePlatformPayoutContract",
            to_address="0xAttackerWallet123",
            amount=100.0,
            currency="USDC",
            block_number=1000,
            current_block_height=1050
        )
        receipt = self.settler.execute_atomic_settlement(
            self.valid_bounty, self.valid_intent, wrong_dest
        )
        self.assertEqual(receipt.status, SettlementStatus.DESTINATION_MISMATCH)

    def test_reject_unapproved_token_contract(self):
        """Scam token with same symbol USDC is rejected."""
        scam_token = PaymentCandidate(
            network="BASE_EVM",
            tx_hash="0xscam_token",
            token_contract="0xScamFakeUsdcContractAddress",
            from_address="0xOpirePlatformPayoutContract",
            to_address=self.solver_wallet,
            amount=100.0,
            currency="USDC",
            block_number=1000,
            current_block_height=1050
        )
        receipt = self.settler.execute_atomic_settlement(
            self.valid_bounty, self.valid_intent, scam_token
        )
        self.assertEqual(receipt.status, SettlementStatus.TOKEN_CONTRACT_MISMATCH)

    def test_reject_insufficient_confirmation_depth(self):
        """Candidate with only 2 block confirmations is rejected to prevent reorg attacks."""
        shallow_block = PaymentCandidate(
            network="BASE_EVM",
            tx_hash="0xshallow_tx",
            token_contract=self.base_usdc,
            from_address="0xOpirePlatformPayoutContract",
            to_address=self.solver_wallet,
            amount=100.0,
            currency="USDC",
            block_number=1000,
            current_block_height=1002,  # Only 2 blocks depth! Required: 12
            memo="Payout for claim_4599_sig"
        )
        receipt = self.settler.execute_atomic_settlement(
            self.valid_bounty, self.valid_intent, shallow_block
        )
        self.assertEqual(receipt.status, SettlementStatus.CONFIRMATION_DEPTH_INSUFFICIENT)

    def test_reject_ambiguous_sender_and_missing_memo(self):
        """If incoming tx has unknown sender AND lacks claim reference memo -> FAIL-CLOSED."""
        ambiguous = PaymentCandidate(
            network="BASE_EVM",
            tx_hash="0xrandom_sender",
            token_contract=self.base_usdc,
            from_address="0xRandomStrangerAddress",
            to_address=self.solver_wallet,
            amount=100.0,
            currency="USDC",
            block_number=1000,
            current_block_height=1050,
            memo=None  # No memo!
        )
        receipt = self.settler.execute_atomic_settlement(
            self.valid_bounty, self.valid_intent, ambiguous
        )
        self.assertEqual(receipt.status, SettlementStatus.AMBIGUOUS_HOLD)

    def test_reject_duplicate_tx_hash(self):
        """Once confirmed, tx_hash cannot be used for any other bounty."""
        # Confirm first time
        r1 = self.settler.execute_atomic_settlement(
            self.valid_bounty, self.valid_intent, self.valid_candidate
        )
        self.assertEqual(r1.status, SettlementStatus.PAYMENT_CONFIRMED)

        # Attempt to confirm second bounty with SAME tx
        bounty2 = BountySpec(
            bounty_id="algora#123",
            repo="other/repo",
            platform="Algora",
            pr_status="PR_MERGED",
            expected_amount=100.0,
            currency="USDC",
            authorized_solver_wallet=self.solver_wallet
        )
        intent2 = SettlementIntent(
            settlement_id="stl_algora_123",
            bounty_id="algora#123",
            settlement_reference="claim_4599_sig",
            approved_amount=100.0,
            currency="USDC",
            platform_payout_address="0xOpirePlatformPayoutContract"
        )
        r2 = self.settler.execute_atomic_settlement(
            bounty2, intent2, self.valid_candidate
        )
        self.assertEqual(r2.status, SettlementStatus.DUPLICATE_TX_REJECTED)


if __name__ == "__main__":
    unittest.main()
