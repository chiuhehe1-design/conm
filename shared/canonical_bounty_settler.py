#!/usr/bin/env python3
"""
CANONICAL BOUNTY SETTLEMENT BINDING ENGINE
Strict 8-Stage Financial Verification Gate for PrimeNode.

Pipeline:
  Bounty Specification (PR_MERGED only)
   ↓
  Settlement Intent (Explicit platform reference & approved amount)
   ↓
  Payment Candidate (Blockchain event / receipt)
   ↓
  8-Point Verification Gate:
    1. Destination Wallet match (Strictly authorized solver wallet)
    2. Currency & Token Contract match (Verified Base USDC, etc.)
    3. Exact Amount match (Zero fuzzing; micro-cent precision)
    4. Globally Unique Tx Hash (Zero duplicate receipt reuse)
    5. Confirmation Depth verification (Reorg protection)
    6. Bounty/Settlement Binding (Platform contract or explicit memo)
    7. Ambiguity Resolution (Identical amounts without memo -> FAIL-CLOSED)
    8. Atomic 3-Way Persistence (bounty_id <-> settlement_id <-> tx_hash)
   ↓
  PAYMENT_CONFIRMED (Strict Canonical Revenue Record)
"""

import sqlite3
import logging
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("CanonicalBountySettler")


class SettlementStatus(str, Enum):
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    AMBIGUOUS_HOLD = "AMBIGUOUS_HOLD"
    CONFIRMATION_DEPTH_INSUFFICIENT = "CONFIRMATION_DEPTH_INSUFFICIENT"
    DESTINATION_MISMATCH = "DESTINATION_MISMATCH"
    TOKEN_CONTRACT_MISMATCH = "TOKEN_CONTRACT_MISMATCH"
    PREREQUISITE_PR_NOT_MERGED = "PREREQUISITE_PR_NOT_MERGED"
    DUPLICATE_TX_REJECTED = "DUPLICATE_TX_REJECTED"
    NO_MATCH = "NO_MATCH"


# Official Known Token Contracts
APPROVED_TOKEN_CONTRACTS = {
    "BASE_EVM": {
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower()
    },
    "ARBITRUM_EVM": {
        "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower()
    },
    "OPTIMISM_EVM": {
        "USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85".lower()
    },
    "SOLANA": {
        "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    }
}

# Required confirmation depths
MINIMUM_CONFIRMATIONS = {
    "BASE_EVM": 12,   # ~24 seconds on Base
    "ARBITRUM_EVM": 20, # Arbitrum fast blocks
    "OPTIMISM_EVM": 12, # Optimism ~24s
    "SOLANA": 32      # finalized commitment
}


@dataclass
class BountySpec:
    bounty_id: str                   # e.g. "opire#4599"
    repo: str                        # "claude-builders-bounty/claude-builders-bounty"
    platform: str                    # "Opire" | "Algora" | "GitHub"
    pr_status: str                   # "PR_OPEN", "PR_MERGED", "CLOSED"
    expected_amount: float
    currency: str                    # "USDC"
    authorized_solver_wallet: str


@dataclass
class SettlementIntent:
    settlement_id: str               # "stl_opire_4599"
    bounty_id: str                   # "opire#4599"
    settlement_reference: str        # e.g. "claim_req_9981"
    approved_amount: float
    currency: str
    platform_payout_address: Optional[str] = None


@dataclass
class PaymentCandidate:
    network: str                     # "BASE_EVM"
    tx_hash: str
    token_contract: str
    from_address: str
    to_address: str
    amount: float
    currency: str
    block_number: int
    current_block_height: int
    memo: Optional[str] = None


@dataclass
class SettlementReceipt:
    bounty_id: str
    settlement_id: str
    tx_hash: str
    amount: float
    currency: str
    network: str
    status: SettlementStatus
    reason: str


class CanonicalBountySettler:
    """
    Production-grade settlement reconciler binding Bounties to Transactions.
    """

    def __init__(self, db_conn: Optional[sqlite3.Connection] = None):
        self.conn = db_conn
        if self.conn:
            self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS confirmed_settlements (
                bounty_id TEXT PRIMARY KEY,
                settlement_id TEXT NOT NULL UNIQUE,
                tx_hash TEXT NOT NULL UNIQUE,
                network TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount REAL NOT NULL,
                confirmed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_settlements_tx ON confirmed_settlements(tx_hash);
            """)

    def is_tx_already_consumed(self, tx_hash: str) -> bool:
        if not self.conn:
            return False
        cur = self.conn.cursor()
        cur.execute("SELECT bounty_id FROM confirmed_settlements WHERE tx_hash = ?", (tx_hash,))
        return cur.fetchone() is not None

    def evaluate_settlement(
        self,
        bounty: BountySpec,
        settlement: SettlementIntent,
        candidate: PaymentCandidate
    ) -> Tuple[SettlementStatus, str]:
        """
        Executes the 8-Point Verification Gate.
        """
        # Invariant 1: PR must be MERGED
        if bounty.pr_status != "PR_MERGED":
            return (
                SettlementStatus.PREREQUISITE_PR_NOT_MERGED,
                f"Bounty PR status is {bounty.pr_status}; payout cannot settle until PR is merged"
            )

        # Invariant 2: Destination Wallet Match
        if candidate.to_address.lower() != bounty.authorized_solver_wallet.lower():
            return (
                SettlementStatus.DESTINATION_MISMATCH,
                f"Candidate recipient {candidate.to_address} does not match solver wallet {bounty.authorized_solver_wallet}"
            )

        # Invariant 3: Token Contract Verification
        approved = APPROVED_TOKEN_CONTRACTS.get(candidate.network, {}).get(candidate.currency.upper())
        if not approved or approved.lower() != candidate.token_contract.lower():
            return (
                SettlementStatus.TOKEN_CONTRACT_MISMATCH,
                f"Unapproved token contract {candidate.token_contract} for {candidate.network} {candidate.currency}"
            )

        # Invariant 4: Micro-cent Exact Amount Match (Zero Fuzzing)
        if abs(candidate.amount - settlement.approved_amount) > 0.000001:
            return (
                SettlementStatus.NO_MATCH,
                f"Amount mismatch: received {candidate.amount}, expected {settlement.approved_amount}"
            )

        # Invariant 5: Globally Unique Tx Hash (Single-Claim Rule)
        if self.is_tx_already_consumed(candidate.tx_hash):
            return (
                SettlementStatus.DUPLICATE_TX_REJECTED,
                f"Transaction {candidate.tx_hash} is already bound to a prior settlement"
            )

        # Invariant 6: Confirmation Depth (Reorg Protection)
        min_depth = MINIMUM_CONFIRMATIONS.get(candidate.network, 12)
        actual_depth = candidate.current_block_height - candidate.block_number
        if actual_depth < min_depth:
            return (
                SettlementStatus.CONFIRMATION_DEPTH_INSUFFICIENT,
                f"Confirmation depth {actual_depth} is less than required {min_depth} blocks"
            )

        # Invariant 7: Bounty/Settlement 1:1 Binding
        # Either candidate memo matches settlement_reference OR candidate from_address matches platform contract
        memo_match = candidate.memo and settlement.settlement_reference in candidate.memo
        sender_match = (
            settlement.platform_payout_address and
            candidate.from_address.lower() == settlement.platform_payout_address.lower()
        )

        if not (memo_match or sender_match):
            # If neither memo nor verified platform sender is present, we cannot bind unambiguously
            return (
                SettlementStatus.AMBIGUOUS_HOLD,
                f"Tx lacks explicit settlement_reference memo '{settlement.settlement_reference}' and sender is not verified platform contract"
            )

        return (SettlementStatus.PAYMENT_CONFIRMED, "All 8 verification invariants satisfied")

    def execute_atomic_settlement(
        self,
        bounty: BountySpec,
        settlement: SettlementIntent,
        candidate: PaymentCandidate
    ) -> SettlementReceipt:
        """Evaluates gate and atomically persists 3-way binding to database."""
        status, reason = self.evaluate_settlement(bounty, settlement, candidate)

        if status != SettlementStatus.PAYMENT_CONFIRMED:
            return SettlementReceipt(
                bounty_id=bounty.bounty_id,
                settlement_id=settlement.settlement_id,
                tx_hash=candidate.tx_hash,
                amount=candidate.amount,
                currency=candidate.currency,
                network=candidate.network,
                status=status,
                reason=reason
            )

        # Atomic 3-way binding
        if self.conn:
            try:
                with self.conn:
                    self.conn.execute("""
                    INSERT INTO confirmed_settlements (
                        bounty_id, settlement_id, tx_hash, network, currency, amount
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        bounty.bounty_id, settlement.settlement_id, candidate.tx_hash,
                        candidate.network, candidate.currency, candidate.amount
                    ))
            except sqlite3.IntegrityError as e:
                return SettlementReceipt(
                    bounty_id=bounty.bounty_id,
                    settlement_id=settlement.settlement_id,
                    tx_hash=candidate.tx_hash,
                    amount=candidate.amount,
                    currency=candidate.currency,
                    network=candidate.network,
                    status=SettlementStatus.DUPLICATE_TX_REJECTED,
                    reason=f"Database integrity violation: {e}"
                )

        return SettlementReceipt(
            bounty_id=bounty.bounty_id,
            settlement_id=settlement.settlement_id,
            tx_hash=candidate.tx_hash,
            amount=candidate.amount,
            currency=candidate.currency,
            network=candidate.network,
            status=SettlementStatus.PAYMENT_CONFIRMED,
            reason="Settlement confirmed and bound to canonical ledger"
        )
