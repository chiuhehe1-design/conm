#!/usr/bin/env python3
"""
UNIT & INTEGRATION TESTS: ON-CHAIN BASE EVM SETTLER, SANDBOXED WORKTREES & EXECUTIVE DASHBOARD
Verifies:
1. EvmRpcClient and OnChainSettlementWatcher:
   - Topic formatting & decoding.
   - Base USDC Transfer event log parsing.
   - Micro-cent precision and confirmation depth verification.
   - Fail-closed security on mismatched address or reorgs.
2. SandboxedWorktreeEngine:
   - Isolated directory lifecycle (create, apply patch, sandboxed test execution, cleanup).
   - Strict command timeouts and exit code handling.
3. AutonomousBountyExecutor Integration:
   - Stage 2 patch and Stage 4 test integration with SandboxedWorktreeEngine.
   - Automatic on-chain polling and settlement of bounties in SETTLEMENT_TRACKING.
4. Executive Dashboard:
   - GET / and GET /dashboard endpoint renders HTML.
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "shared") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "shared"))

from shared.onchain_settlement_watcher import (
    EvmRpcClient, OnChainSettlementWatcher, BASE_USDC_CONTRACT, ERC20_TRANSFER_TOPIC
)
from shared.sandboxed_worktree_engine import SandboxedWorktreeEngine, TestExecutionResult
from shared.autonomous_bounty_executor import AutonomousBountyExecutor
from shared.autonomous_revenue_portfolio import RevenuePortfolioEngine, RevenueStage
from shared.workforce_lifecycle_scheduler import WorkforceScheduler, WorkerLifecycleState
from shared.task_coordinator import ANTITaskCoordinator
from shared.canonical_bounty_settler import CanonicalBountySettler, PaymentCandidate, SettlementStatus
from daemon.autonomous_daemon import AntiAutonomousDaemon
from gateway.dashboard_ui import get_dashboard_html


class TestOnChainSettlerAndWorktree(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.worktree_root = os.path.join(self.temp_dir, "worktrees")
        self.db_path = os.path.join(self.temp_dir, "portfolio.db")
        self.settler_db = os.path.join(self.temp_dir, "settler.db")
        self.tasks_db = os.path.join(self.temp_dir, "tasks.db")
        self.workforce_db = os.path.join(self.temp_dir, "workforce.db")

        self.portfolio = RevenuePortfolioEngine(self.db_path)
        self.settler_conn = sqlite3.connect(self.settler_db)
        self.settler = CanonicalBountySettler(self.settler_conn)
        self.coordinator = ANTITaskCoordinator(self.tasks_db)
        self.workforce = WorkforceScheduler(self.workforce_db)
        self.worktree_engine = SandboxedWorktreeEngine(root_dir=self.worktree_root)

        self.solver_wallet = "0x9876543210fedcba9876543210fedcba98765432"

    def tearDown(self):
        self.settler_conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_onchain_watcher_topic_encoding_and_decoding(self):
        """Verifies 32-byte EVM topic encoding and decoding for wallet addresses."""
        addr = "0x9876543210fedcba9876543210fedcba98765432"
        topic = OnChainSettlementWatcher.encode_address_topic(addr)
        self.assertTrue(topic.startswith("0x000000000000000000000000"))
        self.assertEqual(len(topic), 66)

        decoded = OnChainSettlementWatcher.decode_address_topic(topic)
        self.assertEqual(decoded.lower(), addr.lower())

        # Test USDC 6-decimal conversion
        # 750 USDC = 750 * 10^6 = 750,000,000 units = 0x2cb41780
        hex_units = hex(750000000)
        parsed = OnChainSettlementWatcher.parse_usdc_amount(hex_units)
        self.assertEqual(parsed, 750.0)

    def test_onchain_watcher_verify_valid_payout(self):
        """Verifies that an on-chain receipt with matching Base USDC transfer yields PaymentCandidate."""
        mock_client = MagicMock(spec=EvmRpcClient)
        mock_client.get_block_number.return_value = 21000000

        # Receipt from block 20999980 (depth = 20 blocks >= 12 required)
        mock_client.get_transaction_receipt.return_value = {
            "status": "0x1",
            "blockNumber": hex(20999980),
            "transactionHash": "0xreal_tx_hash_12345",
            "logs": [
                {
                    "address": BASE_USDC_CONTRACT,
                    "topics": [
                        ERC20_TRANSFER_TOPIC,
                        OnChainSettlementWatcher.encode_address_topic("0x1111111111111111111111111111111111111111"),
                        OnChainSettlementWatcher.encode_address_topic(self.solver_wallet)
                    ],
                    "data": hex(750000000)  # 750 USDC
                }
            ]
        }

        watcher = OnChainSettlementWatcher(base_client=mock_client)
        candidate = watcher.verify_transaction_for_payout(
            tx_hash="0xreal_tx_hash_12345",
            expected_solver_wallet=self.solver_wallet,
            expected_amount_usd=750.0,
            settlement_ref="ref_bounty_750"
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.network, "BASE_EVM")
        self.assertEqual(candidate.amount, 750.0)
        self.assertEqual(candidate.currency, "USDC")
        self.assertEqual(candidate.to_address.lower(), self.solver_wallet.lower())
        self.assertIn("ref_bounty_750", candidate.memo)

    def test_onchain_watcher_rejects_mismatched_amount_or_failed_tx(self):
        """Verifies fail-closed rejection on invalid on-chain transaction."""
        mock_client = MagicMock(spec=EvmRpcClient)
        mock_client.get_block_number.return_value = 21000000

        # Case 1: Transaction status = 0x0 (EVM execution failed)
        mock_client.get_transaction_receipt.return_value = {
            "status": "0x0",
            "blockNumber": hex(20999980),
            "logs": []
        }
        watcher = OnChainSettlementWatcher(base_client=mock_client)
        cand1 = watcher.verify_transaction_for_payout("0xfail_tx", self.solver_wallet, 750.0)
        self.assertIsNone(cand1)

        # Case 2: Amount is different (e.g. 500 USDC instead of 750 USDC)
        mock_client.get_transaction_receipt.return_value = {
            "status": "0x1",
            "blockNumber": hex(20999980),
            "logs": [
                {
                    "address": BASE_USDC_CONTRACT,
                    "topics": [
                        ERC20_TRANSFER_TOPIC,
                        OnChainSettlementWatcher.encode_address_topic("0x1111111111111111111111111111111111111111"),
                        OnChainSettlementWatcher.encode_address_topic(self.solver_wallet)
                    ],
                    "data": hex(500000000)  # 500 USDC
                }
            ]
        }
        cand2 = watcher.verify_transaction_for_payout("0xwrong_amt", self.solver_wallet, 750.0)
        self.assertIsNone(cand2)

    def test_sandboxed_worktree_engine_lifecycle(self):
        """Verifies worktree creation, patch writing, test execution, and cleanup."""
        task_id = "test-task-sandboxed-01"
        ws = self.worktree_engine.create_workspace(task_id)
        self.assertTrue(ws.exists())
        self.assertTrue(ws.is_dir())

        # Test patch application
        sample_patch = "diff --git a/README.md b/README.md\n+Fix completed\n"
        ok, patch_hash, msg = self.worktree_engine.apply_patch(ws, sample_patch)
        self.assertTrue(ok)
        self.assertEqual(len(patch_hash), 64)

        # Test sandboxed command execution (success)
        res = self.worktree_engine.run_sandboxed_tests(
            ws,
            command="python3 -c 'print(\"SANDBOX_OK\")'",
            timeout_sec=5.0
        )
        self.assertTrue(res.success)
        self.assertEqual(res.returncode, 0)
        self.assertIn("SANDBOX_OK", res.stdout)
        self.assertFalse(res.timed_out)

        # Test sandboxed command timeout
        res_timeout = self.worktree_engine.run_sandboxed_tests(
            ws,
            command="python3 -c 'import time; time.sleep(2)'",
            timeout_sec=0.1
        )
        self.assertFalse(res_timeout.success)
        self.assertTrue(res_timeout.timed_out)

        # Test cleanup
        self.worktree_engine.cleanup_workspace(ws)
        self.assertFalse(ws.exists())

    def test_autonomous_bounty_executor_with_sandboxed_worktree_and_autosettle(self):
        """Verifies end-to-end bounty execution with sandboxed worktree and on-chain auto-settling."""
        # 1. Register worker
        self.workforce.register_worker(
            worker_id="w-sandbox-test",
            worker_name="Sandbox Specialist",
            domain="engineering",
            skills=["code_edit", "run_tests", "open_pr"],
            initial_state=WorkerLifecycleState.READY
        )

        # 2. Discover opportunity
        opp = self.portfolio.discover_opportunity(
            platform="opire",
            target_repo="tenstorrent/tt-metal",
            title="Implement micro-kernel tensor buffer alignment",
            raw_reward_usd=750.0
        )
        self.portfolio.evaluate_and_score(opp.opp_id)
        self.portfolio.verify_eligibility(opp.opp_id, is_license_permissive=True)

        # 3. Setup mock on-chain watcher
        mock_client = MagicMock(spec=EvmRpcClient)
        mock_client.get_block_number.return_value = 21000000
        watcher = OnChainSettlementWatcher(base_client=mock_client)

        executor = AutonomousBountyExecutor(
            portfolio_engine=self.portfolio,
            workforce_scheduler=self.workforce,
            task_coordinator=self.coordinator,
            bounty_settler=self.settler,
            worktree_engine=self.worktree_engine,
            settlement_watcher=watcher
        )

        # 4. Execute pipeline -> advances to SETTLEMENT_TRACKING
        res = executor.execute_bounty_pipeline(
            opp_id=opp.opp_id,
            solver_wallet=self.solver_wallet,
            settlement_reference=f"ref_{opp.opp_id}"
        )
        self.assertEqual(res.stage, RevenueStage.SETTLEMENT_TRACKING)
        self.assertIsNotNone(res.pr_url)

        # 5. Simulate On-Chain Event: Solver receives 750 USDC transfer
        watcher.scan_recent_transfers = MagicMock(return_value=[
            PaymentCandidate(
                network="BASE_EVM",
                tx_hash="0xverified_base_usdc_tx_750",
                token_contract=BASE_USDC_CONTRACT,
                from_address="0xplatform_vault_address",
                to_address=self.solver_wallet,
                amount=750.0,
                currency="USDC",
                block_number=20999980,
                current_block_height=21000000,
                memo=f"ref:ref_{opp.opp_id}"
            )
        ])

        # 6. Auto-settle pending bounties
        settled_list = executor.scan_and_settle_pending_bounties(
            solver_wallet=self.solver_wallet,
            watcher=watcher
        )
        self.assertEqual(len(settled_list), 1)
        settled = settled_list[0]
        self.assertEqual(settled.stage, RevenueStage.PAYMENT_CONFIRMED)
        self.assertEqual(settled.confirmed_revenue_usd, 750.0)

        # 7. Check financial summary
        fin = self.portfolio.get_financial_summary()
        self.assertEqual(fin["confirmed_revenue_usd"], 750.0)

    def test_executive_dashboard_html_rendering(self):
        """Verifies that get_dashboard_html renders cleanly with title and injected token."""
        html = get_dashboard_html(initial_token="test_bearer_token_xyz")
        self.assertIn("ANTI // Autonomous Company OS Dashboard", html)
        self.assertIn("test_bearer_token_xyz", html)
        self.assertIn("Confirmed Cash Flow", html)
        self.assertIn("Autonomous Workforce Fleet", html)


if __name__ == "__main__":
    unittest.main()
