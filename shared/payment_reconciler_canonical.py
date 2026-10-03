#!/usr/bin/env python3
"""
CANONICAL PAYMENT RECONCILER (Fail-Closed Settlement Engine)
Fixes P0 Vulnerability:
  1. Prevents duplicate receipt reuse (Strict tx_hash uniqueness).
  2. Eliminates loose ±0.10 USD fuzzing (Micro-cent exact matching).
  3. Fails-closed on ambiguous matches (Multiple bounties with same amount without distinct memo).
  4. Idempotent and atomic settlement with rollback.
"""

import sqlite3
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("CanonicalPaymentReconciler")


class ReconciliationVerdict:
    MATCH_SETTLED = "MATCH_SETTLED"
    AMBIGUOUS_HOLD = "AMBIGUOUS_HOLD"
    NO_MATCH = "NO_MATCH"
    DUPLICATE_TX_REJECTED = "DUPLICATE_TX_REJECTED"


@dataclass
class PaymentReceipt:
    network: str              # BASE_EVM, SOLANA, PAYPAL
    currency: str             # USDC, SOL, USD
    amount: float             # Exact amount received
    tx_hash: str              # Unique blockchain transaction hash or payment ID
    recipient: str            # Wallet address or account receiving funds
    sender: Optional[str] = None
    memo: Optional[str] = None # Optional memo referencing bounty/canonical_id
    verified_on_chain: bool = True


@dataclass
class PendingBounty:
    canonical_id: str
    title: str
    reward_amount: float
    currency: str
    platform: str
    stage: str
    expected_wallet: Optional[str] = None
    solver_ref: Optional[str] = None


class CanonicalPaymentReconciler:
    """
    Fail-closed settlement engine for bounties and incoming payments.
    """

    def __init__(self, db_conn: Optional[sqlite3.Connection] = None):
        self.conn = db_conn
        if self.conn:
            self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS settled_payments (
                payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_id TEXT NOT NULL,
                tx_hash TEXT NOT NULL UNIQUE,
                network TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount REAL NOT NULL,
                recipient_wallet TEXT NOT NULL,
                settled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS reconciliation_audits (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_id TEXT,
                tx_hash TEXT,
                verdict TEXT NOT NULL,
                reason TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

    def is_tx_already_settled(self, tx_hash: str) -> bool:
        """Checks if a tx_hash has already been used in any previous settlement."""
        if not self.conn:
            return False
        cur = self.conn.cursor()
        cur.execute("SELECT canonical_id FROM settled_payments WHERE tx_hash = ?", (tx_hash,))
        row = cur.fetchone()
        return row is not None

    def reconcile_bounties(
        self,
        bounties: List[PendingBounty],
        receipts: List[PaymentReceipt]
    ) -> Dict[str, Dict[str, any]]:
        """
        Executes strict, fail-closed reconciliation.
        Returns: {canonical_id: {"verdict": ..., "tx_hash": ..., "details": ...}}
        """
        results = {}
        consumed_tx_hashes: Set[str] = set()

        # Step 1: Filter out already settled tx_hashes (DB level)
        valid_receipts: List[PaymentReceipt] = []
        for r in receipts:
            if not r.tx_hash:
                continue
            if self.is_tx_already_settled(r.tx_hash):
                logger.warning(f"Rejecting receipt: tx_hash={r.tx_hash} already settled in database!")
                continue
            valid_receipts.append(r)

        # Step 2: Index pending bounties by (currency, exact_amount)
        amount_to_bounties: Dict[Tuple[str, float], List[PendingBounty]] = {}
        for b in bounties:
            key = (b.currency.upper(), round(b.reward_amount, 4))
            amount_to_bounties.setdefault(key, []).append(b)

        # Step 3: Match receipts to bounties
        for b in bounties:
            results[b.canonical_id] = {
                "verdict": ReconciliationVerdict.NO_MATCH,
                "tx_hash": None,
                "details": "No matching payment receipt found."
            }

        for r in valid_receipts:
            if r.tx_hash in consumed_tx_hashes:
                continue

            r_key = (r.currency.upper(), round(r.amount, 4))
            candidate_bounties = amount_to_bounties.get(r_key, [])

            if not candidate_bounties:
                continue

            # Case A: Receipt explicitly includes memo/reference to canonical_id
            memo_match = None
            if r.memo:
                for b in candidate_bounties:
                    if b.canonical_id in r.memo or (b.solver_ref and b.solver_ref in r.memo):
                        memo_match = b
                        break

            if memo_match:
                # 1:1 Explicit match
                self._settle(memo_match, r, results, consumed_tx_hashes)
                candidate_bounties.remove(memo_match)
                continue

            # Case B: Only one bounty exists for this exact amount -> Safe 1:1 match
            if len(candidate_bounties) == 1:
                b = candidate_bounties[0]
                self._settle(b, r, results, consumed_tx_hashes)
                candidate_bounties.remove(b)
                continue

            # Case C: Multiple bounties have the EXACT same amount and NO distinguishing memo!
            # FAIL-CLOSED: Refuse to guess! Mark as AMBIGUOUS_HOLD
            logger.error(
                f"FAIL-CLOSED: Multiple bounties {[b.canonical_id for b in candidate_bounties]} "
                f"share amount {r.amount} {r.currency}. Tx {r.tx_hash} lacks distinguishing memo!"
            )
            for b in candidate_bounties:
                results[b.canonical_id] = {
                    "verdict": ReconciliationVerdict.AMBIGUOUS_HOLD,
                    "tx_hash": r.tx_hash,
                    "details": (
                        f"Ambiguous match: {len(candidate_bounties)} bounties share reward {r.amount} {r.currency}. "
                        f"Requires explicit memo or manual solver settlement verification."
                    )
                }
                self._log_audit(b.canonical_id, r.tx_hash, ReconciliationVerdict.AMBIGUOUS_HOLD, "Ambiguous amount match")

        return results

    def _settle(
        self,
        bounty: PendingBounty,
        receipt: PaymentReceipt,
        results: Dict[str, any],
        consumed_tx_hashes: Set[str]
    ):
        """Atomically settles a verified 1:1 bounty match."""
        # 1. Mark tx_hash as consumed in memory to prevent reuse
        consumed_tx_hashes.add(receipt.tx_hash)

        # 2. Persist to DB if connection available
        if self.conn:
            try:
                with self.conn:
                    self.conn.execute("""
                    INSERT INTO settled_payments (
                        canonical_id, tx_hash, network, currency, amount, recipient_wallet
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        bounty.canonical_id, receipt.tx_hash, receipt.network,
                        receipt.currency, receipt.amount, receipt.recipient
                    ))
                    self.conn.execute("""
                    INSERT INTO reconciliation_audits (
                        canonical_id, tx_hash, verdict, reason
                    ) VALUES (?, ?, ?, ?)
                    """, (
                        bounty.canonical_id, receipt.tx_hash,
                        ReconciliationVerdict.MATCH_SETTLED, "1:1 exact verification confirmed"
                    ))
            except sqlite3.IntegrityError:
                # Duplicate tx_hash caught by DB unique constraint!
                results[bounty.canonical_id] = {
                    "verdict": ReconciliationVerdict.DUPLICATE_TX_REJECTED,
                    "tx_hash": receipt.tx_hash,
                    "details": "Integrity violation: tx_hash was already settled in database!"
                }
                return

        results[bounty.canonical_id] = {
            "verdict": ReconciliationVerdict.MATCH_SETTLED,
            "tx_hash": receipt.tx_hash,
            "details": f"Verified on {receipt.network} for {receipt.amount} {receipt.currency}"
        }

    def _log_audit(self, cid: str, tx: str, verdict: str, reason: str):
        if self.conn:
            with self.conn:
                self.conn.execute("""
                INSERT INTO reconciliation_audits (canonical_id, tx_hash, verdict, reason)
                VALUES (?, ?, ?, ?)
                """, (cid, tx, verdict, reason))
