#!/usr/bin/env python3
"""
Unit and integration tests for Company OS, Autonomous Revenue Portfolio, and Self-Improvement Governor (P3).
Tests:
1. Company OS: 14-department dispatch, priority claiming, and inter-department metrics.
2. Revenue Portfolio: 9-stage revenue loop, strict zero-unconfirmed-revenue rule, and Net ROI feedback.
3. Self-Improvement Governor: Governed 6-stage RFC lifecycle, test gate failure rejection, canary SLA breach rejection, and promotion.
"""

import os
import sys
import unittest
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.company_os_departments import (
    CompanyOSOrchestrator,
    DepartmentType,
    DeptTaskStatus
)
from shared.autonomous_revenue_portfolio import (
    RevenuePortfolioEngine,
    RevenueStage
)
from shared.self_improvement_governor import (
    SelfImprovementGovernor,
    RFCStage
)


class TestCompanyOS(unittest.TestCase):

    def setUp(self):
        self.cos = CompanyOSOrchestrator(db_path=":memory:")

    def test_department_task_dispatch_and_claiming(self):
        # 1. Low priority task
        self.cos.submit_task(
            from_dept=DepartmentType.PRODUCT,
            to_dept=DepartmentType.ENGINEERING,
            title="Refactor logger",
            description="Cleanup old log calls",
            priority=4
        )
        # 2. High priority task
        t_high = self.cos.submit_task(
            from_dept=DepartmentType.REVENUE,
            to_dept=DepartmentType.ENGINEERING,
            title="Fix P0 Settlement Bug",
            description="Bounty #750 fix",
            priority=1
        )

        # High priority task must be claimed first
        claimed = self.cos.claim_task(DepartmentType.ENGINEERING, "worker-eng-01")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.task_id, t_high.task_id)
        self.assertEqual(claimed.status, DeptTaskStatus.IN_PROGRESS)
        self.assertEqual(claimed.assigned_worker_id, "worker-eng-01")

        # Complete task
        manifest = {"pr_url": "https://github.com/org/repo/pull/1", "tests_passed": True}
        self.assertTrue(self.cos.complete_task(t_high.task_id, manifest))

        retrieved = self.cos.get_task(t_high.task_id)
        self.assertEqual(retrieved.status, DeptTaskStatus.COMPLETED)
        self.assertEqual(retrieved.result_manifest["pr_url"], "https://github.com/org/repo/pull/1")

    def test_cross_department_metrics(self):
        self.cos.submit_task(DepartmentType.SECURITY, DepartmentType.SRE, "Audit firewall", "Check rules")
        self.cos.submit_task(DepartmentType.FINANCE, DepartmentType.REVENUE, "Sync treasury", "Check USDC")

        metrics = self.cos.get_department_metrics()
        self.assertIn("sre", metrics)
        self.assertIn("revenue", metrics)
        self.assertGreaterEqual(metrics["sre"]["total"], 1)


class TestRevenuePortfolioEngine(unittest.TestCase):

    def setUp(self):
        self.rpe = RevenuePortfolioEngine(db_path=":memory:")

    def test_9_stage_revenue_lifecycle_and_zero_unconfirmed_revenue(self):
        # 1. DISCOVER
        opp = self.rpe.discover_opportunity(
            platform="github",
            target_repo="tenstorrent/tt-metal",
            title="Fix Tensor Memory Leak",
            raw_reward_usd=750.0
        )
        self.assertEqual(opp.stage, RevenueStage.DISCOVER)
        self.assertEqual(opp.confirmed_payout_usd, 0.0)

        # Check financial summary: Confirmed revenue MUST be 0.0
        fin_pre = self.rpe.get_financial_summary()
        self.assertEqual(fin_pre["confirmed_revenue_usd"], 0.0)

        # 2. SCORE
        scored = self.rpe.evaluate_and_score(opp.opp_id, historical_repo_pass_rate=0.8)
        self.assertEqual(scored.stage, RevenueStage.SCORE)
        self.assertEqual(scored.expected_value_usd, 600.0)

        # 3. ELIGIBILITY
        self.assertTrue(self.rpe.verify_eligibility(opp.opp_id, is_license_permissive=True))

        # 4. ASSIGN
        self.assertTrue(self.rpe.assign_worker(opp.opp_id, "worker-coder-01"))

        # 5 & 6. EXECUTE & SUBMIT
        self.assertTrue(self.rpe.submit_deliverable(
            opp.opp_id,
            pr_url="https://github.com/tenstorrent/tt-metal/pull/404",
            compute_cost_usd=0.25
        ))

        # Even after PR is submitted, confirmed revenue MUST still be 0.0!
        fin_mid = self.rpe.get_financial_summary()
        self.assertEqual(fin_mid["confirmed_revenue_usd"], 0.0)
        self.assertGreater(fin_mid["pipeline_potential_usd"], 0.0)

        # 7 & 8. SETTLEMENT_TRACKING & PAYMENT_CONFIRMED
        tx_hash = "0x89abcdef1234567890abcdef1234567890abcdef1234567890abcdef12345678"
        self.assertTrue(self.rpe.confirm_payment_settlement(opp.opp_id, tx_hash, 750.0))

        # Now confirmed revenue MUST be exactly $750.0
        fin_post = self.rpe.get_financial_summary()
        self.assertEqual(fin_post["confirmed_revenue_usd"], 750.0)

        # 9. ROI_FEEDBACK
        settled_opp = self.rpe.get_opportunity(opp.opp_id)
        self.assertEqual(settled_opp.stage, RevenueStage.PAYMENT_CONFIRMED)
        # (750.0 - 0.25) / 0.25 = 2999.0x ROI
        self.assertGreater(settled_opp.net_roi_ratio, 2000.0)

    def test_eligibility_rejection_for_non_permissive(self):
        opp = self.rpe.discover_opportunity("unknown", "closed/repo", "Proprietary task", 50.0)
        self.rpe.evaluate_and_score(opp.opp_id)
        # Reject if license is not permissive
        eligible = self.rpe.verify_eligibility(opp.opp_id, is_license_permissive=False)
        self.assertFalse(eligible)
        updated = self.rpe.get_opportunity(opp.opp_id)
        self.assertEqual(updated.stage, RevenueStage.REJECTED)


class TestSelfImprovementGovernor(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.gov = SelfImprovementGovernor(
            db_path=":memory:",
            docs_dir=self.temp_dir.name
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_rfc_promotion_happy_path(self):
        rfc = self.gov.propose_rfc(
            title="Introduce Adaptive Backoff for OmniRoute 429",
            target_subsystem="shared/jev_resilience_adapter.py",
            problem_analysis="High concurrency leads to rate limit bursts.",
            proposed_solution="Implement Decorrelated Jitter Backoff."
        )
        self.assertEqual(rfc.stage, RFCStage.CREATE_RFC)
        self.assertTrue(Path(self.temp_dir.name, f"{rfc.rfc_id}.md").exists())

        # Attach build candidate
        self.assertTrue(self.gov.attach_candidate_build(rfc.rfc_id, "git_sha_fe1234"))

        # Run test gate (PASS)
        self.assertTrue(self.gov.run_test_gate(rfc.rfc_id, lambda: True))

        # Evaluate Canary within SLA (< 1% error, < 1500ms latency)
        self.assertTrue(self.gov.evaluate_canary(
            rfc.rfc_id,
            observed_error_rate=0.001,
            p99_latency_ms=150.0
        ))

        promoted = self.gov.get_rfc(rfc.rfc_id)
        self.assertEqual(promoted.stage, RFCStage.PROMOTED)

    def test_rfc_rejection_on_test_failure(self):
        rfc = self.gov.propose_rfc(
            title="Faulty refactor",
            target_subsystem="core",
            problem_analysis="Testing test failure",
            proposed_solution="Broken code"
        )
        self.gov.attach_candidate_build(rfc.rfc_id, "git_sha_bad")

        # Test runner returns False
        self.assertFalse(self.gov.run_test_gate(rfc.rfc_id, lambda: False))

        rejected = self.gov.get_rfc(rfc.rfc_id)
        self.assertEqual(rejected.stage, RFCStage.REJECTED)
        self.assertIn("Regression detected", rejected.rejection_reason)

    def test_rfc_rejection_on_canary_sla_breach(self):
        rfc = self.gov.propose_rfc(
            title="High latency change",
            target_subsystem="gateway",
            problem_analysis="Testing canary breach",
            proposed_solution="Heavy filter"
        )
        self.gov.attach_candidate_build(rfc.rfc_id, "git_sha_latency")
        self.gov.run_test_gate(rfc.rfc_id, lambda: True)

        # Observed error rate is 2.5% (> 1.0% SLA limit)
        self.assertFalse(self.gov.evaluate_canary(
            rfc.rfc_id,
            observed_error_rate=0.025,
            p99_latency_ms=200.0
        ))

        rejected = self.gov.get_rfc(rfc.rfc_id)
        self.assertEqual(rejected.stage, RFCStage.REJECTED)
        self.assertIn("Canary SLA breached", rejected.rejection_reason)


if __name__ == "__main__":
    unittest.main()
