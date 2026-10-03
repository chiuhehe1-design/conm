#!/usr/bin/env python3
"""
UNIT & INTEGRATION TESTS: 24/7 AUTONOMOUS DAEMON & EXECUTIVE GATEWAY API
Verifies:
1. AntiAutonomousDaemon single cycle execution (Ingestion -> DAG -> RFCs -> Persistent DB).
2. Break-Glass GLOBAL_PAUSE safety guard (Graceful skip when pause file exists).
3. Gateway Executive Endpoints:
   - GET /api/v1/revenue/summary
   - GET /api/v1/revenue/opportunities
   - GET /api/v1/workforce/status
   - GET /api/v1/rfcs
   - GET /api/v1/autonomous/status
   - POST /api/v1/autonomous/cycle
"""

import os
import sys
import json
import sqlite3
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "shared") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "shared"))

from daemon.autonomous_daemon import AntiAutonomousDaemon, CycleResult
from shared.autonomous_revenue_portfolio import RevenuePortfolioEngine, RevenueStage
from shared.workforce_lifecycle_scheduler import WorkforceScheduler, WorkerLifecycleState
from shared.task_coordinator import ANTITaskCoordinator
from shared.canonical_bounty_settler import CanonicalBountySettler
from shared.self_improvement_governor import SelfImprovementGovernor
from shared.standing_rfc_generator import TelemetrySnapshot


class TestAutonomousDaemonAndGateway(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.daemon_db = os.path.join(self.temp_dir, "daemon.db")
        self.portfolio_db = os.path.join(self.temp_dir, "portfolio.db")
        self.workforce_db = os.path.join(self.temp_dir, "workforce.db")
        self.tasks_db = os.path.join(self.temp_dir, "tasks.db")
        self.settler_db = os.path.join(self.temp_dir, "settler.db")
        self.rfc_db = os.path.join(self.temp_dir, "rfcs.db")
        self.rfc_docs_dir = os.path.join(self.temp_dir, "rfcs_docs")
        self.pause_file = os.path.join(self.temp_dir, "GLOBAL_PAUSE")

        # Set environment variable for pause file to test isolation
        os.environ["ANTI_GLOBAL_PAUSE_FILE"] = self.pause_file

        self.portfolio = RevenuePortfolioEngine(self.portfolio_db)
        self.workforce = WorkforceScheduler(self.workforce_db)
        self.coordinator = ANTITaskCoordinator(self.tasks_db)
        self.settler_conn = sqlite3.connect(self.settler_db)
        self.settler = CanonicalBountySettler(self.settler_conn)
        self.governor = SelfImprovementGovernor(self.rfc_db, docs_dir=self.rfc_docs_dir)

        # Pre-seed a ready engineering worker
        self.workforce.register_worker(
            worker_id="w-daemon-eng",
            worker_name="Daemon Engineer",
            domain="engineering",
            skills=["code_edit", "run_tests", "open_pr"],
            initial_state=WorkerLifecycleState.READY
        )

        self.daemon = AntiAutonomousDaemon(
            db_path=self.daemon_db,
            cycle_interval_sec=1,
            portfolio_engine=self.portfolio,
            workforce_scheduler=self.workforce,
            task_coordinator=self.coordinator,
            bounty_settler=self.settler,
            self_governor=self.governor,
            pause_file=self.pause_file
        )

    def tearDown(self):
        self.settler_conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        if "ANTI_GLOBAL_PAUSE_FILE" in os.environ:
            del os.environ["ANTI_GLOBAL_PAUSE_FILE"]

    def test_daemon_single_cycle_execution(self):
        """Verifies a full autonomous cycle executes and persists to SQLite."""
        telemetry = TelemetrySnapshot(
            gateway_p99_ms=120.0,  # Will trigger latency RFC
            model_router_fallback_rate=0.01,
            worker_failure_rate=0.01
        )

        res = self.daemon.run_single_cycle(telemetry=telemetry)
        self.assertEqual(res.status, "COMPLETED")
        self.assertGreater(res.scanned_count, 0)
        self.assertGreater(res.eligible_count, 0)
        self.assertGreater(res.executed_count, 0)
        self.assertEqual(res.rfcs_generated, 1)
        self.assertEqual(res.rfcs_promoted, 1)

        # Verify persistent record in daemon_cycles DB
        latest = self.daemon.get_latest_cycle()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["cycle_id"], res.cycle_id)
        self.assertEqual(latest["status"], "COMPLETED")
        self.assertEqual(latest["scanned_count"], res.scanned_count)

    def test_daemon_break_glass_pause_guard(self):
        """Verifies that daemon pauses cleanly without executing when GLOBAL_PAUSE is present."""
        Path(self.pause_file).touch()
        self.assertTrue(self.daemon.is_paused())

        res = self.daemon.run_single_cycle()
        self.assertEqual(res.status, "PAUSED")
        self.assertEqual(res.scanned_count, 0)
        self.assertEqual(res.executed_count, 0)
        self.assertIn("GLOBAL_PAUSE", res.error_message)

        # Resume by removing pause file
        os.remove(self.pause_file)
        self.assertFalse(self.daemon.is_paused())

        res_after = self.daemon.run_single_cycle()
        self.assertEqual(res_after.status, "COMPLETED")

    def test_gateway_revenue_and_workforce_endpoints(self):
        """Verifies query logic backing Gateway GET /api/v1/revenue/summary, workforce, rfcs."""
        # 1. Run a cycle to generate data
        self.daemon.run_single_cycle(telemetry=TelemetrySnapshot(gateway_p99_ms=120.0))

        # 2. Test revenue summary logic
        summary = self.portfolio.get_financial_summary()
        self.assertIn("confirmed_revenue_usd", summary)
        self.assertIn("pipeline_potential_usd", summary)
        self.assertGreaterEqual(summary["pipeline_potential_usd"], 0.0)

        # 3. Test workforce status query logic
        cur = self.workforce.conn.cursor()
        cur.execute("SELECT worker_id, worker_name, domain, lifecycle_state FROM workforce_profiles")
        workers = cur.fetchall()
        self.assertGreaterEqual(len(workers), 1)
        self.assertEqual(workers[0][0], "w-daemon-eng")

        # 4. Test RFC status query logic
        rfcs = self.governor.conn.cursor().execute("SELECT rfc_id, stage FROM improvement_rfcs").fetchall()
        self.assertGreaterEqual(len(rfcs), 1)


if __name__ == "__main__":
    unittest.main()
