#!/usr/bin/env python3
"""
Unit and Integration tests for FEATURE-017:
- Financial Analytics & P&L Ledger
- Workforce Dynamic Optimizer & Scoring
- Live Bounty Crawler & Economic Ranking
- Multi-Database Snapshotter & Backup Archive
- Gateway P&L & Rebalance Endpoints
"""

import os
import sys
import time
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "shared") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "shared"))

from shared.financial_analytics import FinancialAnalytics, EntryType, PnLSummary
from shared.workforce_optimizer import WorkforceOptimizer, RebalanceResult
from shared.workforce_lifecycle_scheduler import WorkforceScheduler, WorkerLifecycleState, WorkerProfile
from shared.live_bounty_crawler import LiveBountyCrawler, RankedOpportunity, IngestedBounty, BountySource
from tools.snapshot_company_state import CompanyStateSnapshotter, calculate_sha256


class TestFinancialAnalytics(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp_dir.name) / "test_fin.db")
        self.fa = FinancialAnalytics(db_path=self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_pnl_calculation_and_margins(self):
        # 1. Record revenue
        self.fa.record_entry(
            entry_type=EntryType.REVENUE,
            amount_usd=1000.0,
            reference_id="bounty-01",
            chain="BASE_EVM"
        )
        # 2. Record compute cost
        self.fa.record_entry(
            entry_type=EntryType.COMPUTE_COST,
            amount_usd=5.0,
            reference_id="job-01",
            chain="INTERNAL"
        )
        # 3. Record gas fee
        self.fa.record_entry(
            entry_type=EntryType.GAS_FEE,
            amount_usd=0.01,
            reference_id="0xtx123",
            chain="BASE_EVM"
        )

        pnl = self.fa.get_pnl_summary()
        self.assertEqual(pnl.gross_revenue_usd, 1000.0)
        self.assertEqual(pnl.total_compute_cost_usd, 5.0)
        self.assertEqual(pnl.total_gas_fees_usd, 0.01)
        self.assertEqual(pnl.total_expenses_usd, 5.01)
        self.assertEqual(pnl.net_profit_usd, 994.99)
        self.assertAlmostEqual(pnl.net_profit_margin_pct, 99.5, delta=0.1)
        self.assertGreater(pnl.roi_multiple, 100.0)
        self.assertEqual(pnl.total_transactions, 3)

    def test_record_settlement_and_inference(self):
        self.fa.record_bounty_settlement("opp-test-1", 750.0, "BASE_EVM", "0xtx_base_750")
        self.fa.record_inference_cost("job-inf-1", "auto/best-fast", 0.04)

        pnl = self.fa.get_pnl_summary()
        self.assertEqual(pnl.gross_revenue_usd, 750.0)
        self.assertEqual(pnl.total_compute_cost_usd, 0.04)
        self.assertGreater(pnl.total_gas_fees_usd, 0.0)
        self.assertGreater(pnl.net_profit_usd, 749.0)

        entries = self.fa.get_recent_entries(limit=5)
        self.assertEqual(len(entries), 3)


class TestWorkforceOptimizer(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp_dir.name) / "test_wf.db")
        self.wf = WorkforceScheduler(db_path=self.db_path)
        self.opt = WorkforceOptimizer(scheduler=self.wf, min_acceptable_score=0.75, scale_up_load_threshold=0.70)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_composite_score(self):
        # High quality, high reliability, low latency
        s1 = WorkforceOptimizer.compute_composite_score(quality=1.0, reliability=1.0, latency_ms=400.0)
        self.assertGreater(s1, 0.90)

        # Poor quality, high latency
        s2 = WorkforceOptimizer.compute_composite_score(quality=0.4, reliability=0.6, latency_ms=1900.0)
        self.assertLess(s2, 0.60)

    def test_auto_scale_and_retrain(self):
        # Register a worker with low score
        self.wf.register_worker(WorkerProfile(
            worker_id="w-poor-1",
            worker_name="Lagging Worker",
            domain="engineering",
            lifecycle_state=WorkerLifecycleState.READY,
            skills=["python", "git"],
            max_load=5,
            current_load=4,  # 4/5 = 80% load > 70% threshold
            quality_rate=0.5,
            reliability_rate=0.5,
            avg_latency_ms=1800.0
        ))

        res = self.opt.evaluate_and_rebalance()
        self.assertEqual(res.workers_retrained, 1)
        self.assertGreaterEqual(res.new_workers_spawned, 1)

        # Verify state of w-poor-1 is now RETRAIN
        prof = self.wf.get_worker("w-poor-1")
        self.assertEqual(prof.lifecycle_state, WorkerLifecycleState.RETRAIN)


class TestLiveBountyCrawler(unittest.TestCase):

    def setUp(self):
        self.crawler = LiveBountyCrawler(enable_network=False)

    def test_regex_usd_parser(self):
        self.assertEqual(self.crawler._extract_usd_amount("Bounty: $500 for fixing bug"), 500.0)
        self.assertEqual(self.crawler._extract_usd_amount("Reward: 1,500 USDC on Base"), 1500.0)
        self.assertEqual(self.crawler._extract_usd_amount("Payout: 250 USD via Stripe"), 250.0)
        self.assertEqual(self.crawler._extract_usd_amount("General issue without reward"), 100.0)

    def test_crawl_and_ranking(self):
        bounties = self.crawler.crawl_all(limit_per_source=3)
        self.assertGreater(len(bounties), 0)

        ranked = self.crawler.rank_opportunities(bounties, default_compute_cost=0.05)
        self.assertEqual(len(ranked), len(bounties))
        self.assertTrue(ranked[0].expected_value_usd >= ranked[-1].expected_value_usd)
        self.assertGreater(ranked[0].roi_multiple, 10.0)


class TestCompanyStateSnapshotter(unittest.TestCase):

    def setUp(self):
        self.tmp_data = tempfile.TemporaryDirectory()
        self.tmp_backups = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_data.name)
        self.backup_dir = Path(self.tmp_backups.name)

        # Create dummy SQLite databases
        for db_name in ("db1.db", "db2.db"):
            conn = sqlite3.connect(str(self.data_dir / db_name))
            with conn:
                conn.execute("CREATE TABLE test (id INT, val TEXT);")
                conn.execute("INSERT INTO test VALUES (1, 'alpha');")
            conn.close()

        self.snapshotter = CompanyStateSnapshotter(
            data_dir=self.data_dir,
            backup_dir=self.backup_dir,
            retention_days=7
        )

    def tearDown(self):
        self.tmp_data.cleanup()
        self.tmp_backups.cleanup()

    def test_create_snapshot_manifest_and_tarball(self):
        rep = self.snapshotter.create_snapshot()
        self.assertTrue(os.path.exists(rep["archive_path"]))
        self.assertGreater(rep["size_bytes"], 0)
        self.assertEqual(rep["manifest"]["total_databases"], 2)
        self.assertIn("db1.db", rep["manifest"]["databases"])
        self.assertIn("db2.db", rep["manifest"]["databases"])

        # Integrity verification
        archive_sha = calculate_sha256(Path(rep["archive_path"]))
        self.assertEqual(archive_sha, rep["archive_sha256"])


class TestAutonomousDaemonFinancialIntegration(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)

        from shared.autonomous_revenue_portfolio import RevenuePortfolioEngine
        from shared.task_coordinator import ANTITaskCoordinator
        from shared.canonical_bounty_settler import CanonicalBountySettler
        from shared.self_improvement_governor import SelfImprovementGovernor
        from daemon.autonomous_daemon import AntiAutonomousDaemon

        self.portfolio = RevenuePortfolioEngine(str(self.base_dir / "port.db"))
        self.workforce = WorkforceScheduler(str(self.base_dir / "wf.db"))
        self.coordinator = ANTITaskCoordinator(str(self.base_dir / "tasks.db"))
        self.settler = CanonicalBountySettler(sqlite3.connect(str(self.base_dir / "settle.db")))
        self.governor = SelfImprovementGovernor(str(self.base_dir / "rfcs.db"))
        self.financial = FinancialAnalytics(str(self.base_dir / "fin.db"))
        self.optimizer = WorkforceOptimizer(scheduler=self.workforce)
        self.crawler = LiveBountyCrawler(enable_network=False)

        # Register ready worker
        self.workforce.register_worker(
            worker_id="w-daemon-eng-01",
            worker_name="Daemon Engineer",
            domain="engineering",
            skills=["code_edit", "run_tests", "open_pr"],
            initial_state=WorkerLifecycleState.READY
        )

        self.daemon = AntiAutonomousDaemon(
            db_path=str(self.base_dir / "daemon.db"),
            portfolio_engine=self.portfolio,
            workforce_scheduler=self.workforce,
            task_coordinator=self.coordinator,
            bounty_settler=self.settler,
            self_governor=self.governor,
            financial_analytics=self.financial,
            workforce_optimizer=self.optimizer,
            live_crawler=self.crawler,
            pause_file=str(self.base_dir / "GLOBAL_PAUSE")
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_single_cycle_financial_ledger_recording(self):
        res = self.daemon.run_single_cycle()
        self.assertEqual(res.status, "COMPLETED")
        self.assertGreater(res.scanned_count, 0)

        # Verify financial ledger tracked compute costs
        pnl = self.financial.get_pnl_summary()
        self.assertGreaterEqual(pnl.total_transactions, res.executed_count)
        if res.executed_count > 0:
            self.assertGreater(pnl.total_compute_cost_usd, 0.0)


if __name__ == "__main__":
    unittest.main()

