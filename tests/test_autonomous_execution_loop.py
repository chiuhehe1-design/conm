#!/usr/bin/env python3
"""
UNIT & INTEGRATION TESTS: AUTONOMOUS REVENUE INGESTION & EXECUTION PIPELINE
Verifies:
1. Multi-platform bounty ingestion (GitHub, Opire, Algora, Superteam) + EV scoring + License gate.
2. End-to-end Autonomous Bounty Execution DAG (Worker assignment, Kahn DAG, OPA, AcceptanceGate, PR submit).
3. 8-Point Canonical Settlement gate (Micro-cent match, single-claim uniqueness, Base USDC contract).
4. Standing RFC Generator (Telemetry monitoring, bottleneck detection, test gate, canary promotion).
"""

import os
import sys
import unittest
import sqlite3
import tempfile
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.autonomous_revenue_portfolio import RevenuePortfolioEngine, RevenueStage
from shared.bounty_ingestion_adapter import (
    BountyIngestionEngine, GitHubBountyAdapter, OpireBountyAdapter,
    AlgoraBountyAdapter, SuperteamBountyAdapter, IngestedBounty, BountySource
)
from shared.workforce_lifecycle_scheduler import (
    WorkforceScheduler, WorkerLifecycleState
)
from shared.task_coordinator import ANTITaskCoordinator, TaskRegistry
from shared.canonical_bounty_settler import (
    CanonicalBountySettler, PaymentCandidate, APPROVED_TOKEN_CONTRACTS
)
from shared.autonomous_bounty_executor import AutonomousBountyExecutor
from shared.self_improvement_governor import SelfImprovementGovernor, RFCStage
from shared.standing_rfc_generator import StandingRFCGenerator, TelemetrySnapshot


class TestAutonomousExecutionLoop(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.portfolio_db = os.path.join(self.temp_dir, "test_portfolio.db")
        self.workforce_db = os.path.join(self.temp_dir, "test_workforce.db")
        self.tasks_db = os.path.join(self.temp_dir, "test_tasks.db")
        self.settler_db = sqlite3.connect(":memory:")
        self.rfc_db = os.path.join(self.temp_dir, "test_rfc.db")
        self.rfc_docs_dir = os.path.join(self.temp_dir, "rfcs")

        self.portfolio = RevenuePortfolioEngine(db_path=self.portfolio_db)
        self.workforce = WorkforceScheduler(db_path=self.workforce_db)
        self.coordinator = ANTITaskCoordinator(db_path=self.tasks_db)
        self.settler = CanonicalBountySettler(db_conn=self.settler_db)
        self.governor = SelfImprovementGovernor(db_path=self.rfc_db, docs_dir=self.rfc_docs_dir)

    def tearDown(self):
        self.settler_db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_multi_platform_bounty_ingestion_and_scoring(self):
        """Tests ingestion across Opire, Algora, Superteam, and custom feeds."""
        engine = BountyIngestionEngine(
            portfolio_engine=self.portfolio,
            adapters=[
                OpireBountyAdapter(),
                AlgoraBountyAdapter(),
                SuperteamBountyAdapter()
            ]
        )

        report = engine.scan_and_ingest()
        self.assertGreaterEqual(report["total_scanned"], 4)
        self.assertGreaterEqual(report["newly_created"], 4)
        self.assertGreaterEqual(report["eligible_count"], 4)

        # Verify items exist in portfolio database with correct initial stage
        for opp in report["opportunities"]:
            self.assertEqual(opp.stage, RevenueStage.ELIGIBILITY)
            self.assertGreater(opp.raw_reward_usd, 0.0)
            self.assertGreater(opp.expected_value_usd, 0.0)

    def test_permissive_license_filtering(self):
        """Verifies that non-permissive licenses (e.g. GPL / proprietary) are rejected."""
        class MockGPLAdapter:
            source_name = BountySource.GITHUB
            def fetch_open_bounties(self, limit=10):
                return [
                    IngestedBounty(
                        bounty_id="mock-gpl-1",
                        platform=BountySource.GITHUB,
                        target_repo="gpl-author/project",
                        title="Fix GPL bug",
                        description="GPL licensed project",
                        raw_reward_usd=500.0,
                        issue_url="https://github.com/gpl-author/project/issues/1",
                        license_name="GPL-3.0"
                    )
                ]

        engine = BountyIngestionEngine(
            portfolio_engine=self.portfolio,
            adapters=[MockGPLAdapter()]
        )
        report = engine.scan_and_ingest()
        self.assertEqual(report["total_scanned"], 1)
        self.assertEqual(report["eligible_count"], 0)
        opp = self.portfolio.get_opportunity(report["opportunities"][0].opp_id)
        self.assertEqual(opp.stage, RevenueStage.REJECTED)

    def test_end_to_end_autonomous_bounty_execution_and_settlement(self):
        """
        Executes complete closed loop:
        Discovery -> Scoring -> Eligibility -> Worker Assignment -> Kahn DAG -> PR Submit ->
        Blockchain Confirmation -> Micro-cent Verification -> PAYMENT_CONFIRMED -> Net ROI.
        """
        # Register a qualified Senior Engineer in Workforce Scheduler
        worker = self.workforce.register_worker(
            worker_id="w-eng-prime-01",
            worker_name="Prime AI Core Engineer",
            domain="engineering",
            skills=["code_edit", "run_tests", "open_pr"],
            initial_state=WorkerLifecycleState.READY
        )

        # 1. Discover and Verify Eligibility
        opp = self.portfolio.discover_opportunity(
            platform="opire",
            target_repo="tenstorrent/tt-metal",
            title="Implement micro-kernel tensor buffer alignment on Tenstorrent Wormhole",
            raw_reward_usd=750.0
        )
        self.portfolio.evaluate_and_score(opp.opp_id, historical_repo_pass_rate=0.90)
        self.portfolio.verify_eligibility(opp.opp_id, is_license_permissive=True)

        executor = AutonomousBountyExecutor(
            portfolio_engine=self.portfolio,
            workforce_scheduler=self.workforce,
            task_coordinator=self.coordinator,
            bounty_settler=self.settler
        )

        solver_wallet = "0x9876543210fedcba9876543210fedcba98765432"
        settlement_ref = "claim_req_tenstorrent_4610"

        # 2. Execute Bounty Pipeline (ASSIGN -> EXECUTE -> SUBMIT -> SETTLEMENT_TRACKING)
        exec_res = executor.execute_bounty_pipeline(
            opp_id=opp.opp_id,
            solver_wallet=solver_wallet,
            settlement_reference=settlement_ref
        )

        self.assertEqual(exec_res.stage, RevenueStage.SETTLEMENT_TRACKING)
        self.assertEqual(exec_res.assigned_worker_id, "w-eng-prime-01")
        self.assertEqual(len(exec_res.dag_task_ids), 5)
        self.assertTrue(exec_res.pr_url.startswith("https://github.com/tenstorrent/tt-metal/pull/"))

        # Prior to settlement: confirmed revenue must strictly remain $0.00
        summary_before = self.portfolio.get_financial_summary()
        self.assertEqual(summary_before["confirmed_revenue_usd"], 0.0)
        self.assertGreater(summary_before["pipeline_potential_usd"], 0.0)

        # 3. Simulate On-chain Settlement Candidate on Base EVM
        base_usdc_contract = APPROVED_TOKEN_CONTRACTS["BASE_EVM"]["USDC"]
        tx_hash = "0x778899aabbccddeeff00112233445566778899aabbccddeeff00112233445566"

        candidate = PaymentCandidate(
            network="BASE_EVM",
            tx_hash=tx_hash,
            token_contract=base_usdc_contract,
            from_address="0x1111222233334444555566667777888899990000",
            to_address=solver_wallet,
            amount=750.0,
            currency="USDC",
            block_number=100000,
            current_block_height=100020,  # 20 confirmations (> 12 required)
            memo=f"Settlement for {settlement_ref}"
        )

        # 4. Process Incoming Settlement
        settle_res = executor.process_incoming_settlement(
            opp_id=opp.opp_id,
            candidate=candidate,
            solver_wallet=solver_wallet,
            settlement_ref=settlement_ref,
            pr_status="PR_MERGED"
        )

        self.assertEqual(settle_res.stage, RevenueStage.PAYMENT_CONFIRMED)
        self.assertEqual(settle_res.confirmed_revenue_usd, 750.0)
        self.assertGreater(settle_res.net_roi_ratio, 100.0)  # High ROI over $0.08 compute cost

        # Strict Financial Verification
        summary_after = self.portfolio.get_financial_summary()
        self.assertEqual(summary_after["confirmed_revenue_usd"], 750.0)
        self.assertEqual(summary_after["avg_settled_roi_ratio"], settle_res.net_roi_ratio)

        # 5. Anti-Double-Spend Protection: Re-submitting same tx_hash must fail
        duplicate_res = executor.process_incoming_settlement(
            opp_id=opp.opp_id,
            candidate=candidate,
            solver_wallet=solver_wallet,
            settlement_ref=settlement_ref,
            pr_status="PR_MERGED"
        )
        self.assertIn("already bound", duplicate_res.error_message)

    def test_standing_rfc_generator_and_canary_promotion(self):
        """Tests continuous metric monitoring and autonomous governed RFC lifecycle."""
        rfc_gen = StandingRFCGenerator(governor=self.governor)

        # 1. Telemetry with Gateway latency breach & high model fallback
        degraded_telemetry = TelemetrySnapshot(
            gateway_p99_ms=135.0,                  # Breach > 100ms
            gateway_error_rate=0.002,
            model_router_fallback_rate=0.08,       # Breach > 5%
            worker_failure_rate=0.01,
            network_quota_utilization=0.50,
            avg_task_cost_usd=0.03
        )

        cycle_result = rfc_gen.run_optimization_cycle(
            telemetry=degraded_telemetry,
            candidate_test_runner=lambda rfc: True,
            canary_error_rate=0.001,
            canary_p99_ms=55.0
        )

        self.assertEqual(cycle_result["rfcs_generated"], 2)
        self.assertEqual(cycle_result["promoted_count"], 2)
        self.assertEqual(cycle_result["rejected_count"], 0)

        # Verify database records
        for rfc in cycle_result["promoted_rfcs"]:
            self.assertEqual(rfc.stage, RFCStage.PROMOTED)
            self.assertTrue(rfc.tests_passed)
            self.assertGreater(rfc.resolved_at, 0)


if __name__ == "__main__":
    unittest.main()
