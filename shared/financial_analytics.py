#!/usr/bin/env python3
"""
AUTONOMOUS FINANCIAL ANALYTICS & P&L LEDGER (P3-02 / P3-18 / ANTI-017)
Tracks real-time revenue, model compute costs, L2 Base/Solana gas fees,
and calculates net profit margin and ROI velocity for PrimeNode Company OS.
"""

import os
import sys
import time
import sqlite3
import logging
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("FINANCIAL_ANALYTICS")

DEFAULT_FINANCIAL_DB = os.environ.get(
    "ANTI_FINANCIAL_DB",
    str(REPO_ROOT / "data" / "financial_ledger.db")
)


class EntryType(str, Enum):
    REVENUE = "REVENUE"
    COMPUTE_COST = "COMPUTE_COST"
    GAS_FEE = "GAS_FEE"


@dataclass
class FinancialEntry:
    entry_id: str
    entry_type: EntryType
    amount_usd: float
    reference_id: str
    chain: str = "INTERNAL"
    timestamp: float = field(default_factory=time.time)
    description: str = ""


@dataclass
class PnLSummary:
    gross_revenue_usd: float
    total_compute_cost_usd: float
    total_gas_fees_usd: float
    total_expenses_usd: float
    net_profit_usd: float
    net_profit_margin_pct: float
    roi_multiple: float
    total_transactions: int
    last_updated: float


class FinancialAnalytics:
    """
    Manages double-entry financial ledger and P&L analytics for the Autonomous Company.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_FINANCIAL_DB
        data_dir = Path(self.db_path).parent
        data_dir.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS financial_ledger (
                entry_id TEXT PRIMARY KEY,
                entry_type TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                reference_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                timestamp REAL NOT NULL,
                description TEXT NOT NULL
            );
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_fin_type ON financial_ledger(entry_type);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_fin_ts ON financial_ledger(timestamp);")

    def record_entry(
        self,
        entry_type: EntryType,
        amount_usd: float,
        reference_id: str,
        chain: str = "INTERNAL",
        description: str = "",
        entry_id: Optional[str] = None
    ) -> FinancialEntry:
        eid = entry_id or f"fin-{int(time.time()*1000)}-{os.urandom(4).hex()}"
        ts = time.time()
        entry = FinancialEntry(
            entry_id=eid,
            entry_type=entry_type,
            amount_usd=round(amount_usd, 6),
            reference_id=reference_id,
            chain=chain,
            timestamp=ts,
            description=description
        )
        with self.conn:
            self.conn.execute("""
            INSERT OR REPLACE INTO financial_ledger
            (entry_id, entry_type, amount_usd, reference_id, chain, timestamp, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (entry.entry_id, entry.entry_type.value, entry.amount_usd,
                  entry.reference_id, entry.chain, entry.timestamp, entry.description))
        return entry

    def record_bounty_settlement(self, opp_id: str, amount_usd: float, chain: str, tx_hash: str):
        """Records revenue payout and estimated on-chain gas expenditure."""
        # 1. Revenue
        self.record_entry(
            entry_type=EntryType.REVENUE,
            amount_usd=amount_usd,
            reference_id=opp_id,
            chain=chain,
            description=f"Confirmed settlement payout for {opp_id} (Tx: {tx_hash})"
        )
        # 2. Estimated Gas Fee (Base EVM ~ $0.002, Solana ~ $0.0005)
        gas_cost = 0.002 if "BASE" in chain.upper() else 0.0005
        self.record_entry(
            entry_type=EntryType.GAS_FEE,
            amount_usd=gas_cost,
            reference_id=tx_hash,
            chain=chain,
            description=f"Settlement transaction gas fee on {chain}"
        )

    def record_inference_cost(self, job_id: str, model_name: str, cost_usd: float):
        """Records AI model inference cost (OmniRoute / Jev)."""
        self.record_entry(
            entry_type=EntryType.COMPUTE_COST,
            amount_usd=cost_usd,
            reference_id=job_id,
            chain="INTERNAL",
            description=f"Model inference cost ({model_name}) for job {job_id}"
        )

    def get_pnl_summary(self) -> PnLSummary:
        cur = self.conn.cursor()
        cur.execute("""
        SELECT entry_type, SUM(amount_usd), COUNT(*)
        FROM financial_ledger
        GROUP BY entry_type
        """)
        rows = cur.fetchall()

        gross_rev = 0.0
        compute_cost = 0.0
        gas_fee = 0.0
        tx_count = 0

        for etype, amt, count in rows:
            tx_count += count
            if etype == EntryType.REVENUE.value:
                gross_rev += amt
            elif etype == EntryType.COMPUTE_COST.value:
                compute_cost += amt
            elif etype == EntryType.GAS_FEE.value:
                gas_fee += amt

        total_expenses = compute_cost + gas_fee
        net_profit = gross_rev - total_expenses
        margin_pct = (net_profit / gross_rev * 100.0) if gross_rev > 0 else 0.0
        roi = (gross_rev / total_expenses) if total_expenses > 0 else (999.0 if gross_rev > 0 else 0.0)

        return PnLSummary(
            gross_revenue_usd=round(gross_rev, 4),
            total_compute_cost_usd=round(compute_cost, 4),
            total_gas_fees_usd=round(gas_fee, 4),
            total_expenses_usd=round(total_expenses, 4),
            net_profit_usd=round(net_profit, 4),
            net_profit_margin_pct=round(margin_pct, 2),
            roi_multiple=round(roi, 1),
            total_transactions=tx_count,
            last_updated=time.time()
        )

    def get_recent_entries(self, limit: int = 20) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("""
        SELECT entry_id, entry_type, amount_usd, reference_id, chain, timestamp, description
        FROM financial_ledger ORDER BY timestamp DESC LIMIT ?
        """, (limit,))
        return [
            {
                "entry_id": r[0],
                "entry_type": r[1],
                "amount_usd": r[2],
                "reference_id": r[3],
                "chain": r[4],
                "timestamp": r[5],
                "description": r[6]
            } for r in cur.fetchall()
        ]
