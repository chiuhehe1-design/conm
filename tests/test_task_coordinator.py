#!/usr/bin/env python3
"""
Unit Tests for ANTI Task Coordinator, Fencing Tokens, and Acceptance Gate
"""

import os
import time
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.task_coordinator import (
    ANTITaskCoordinator,
    Task,
    TaskStatus,
    TaskDomain,
    ModelTier,
    CompletionManifest
)


class TestANTITaskCoordinatorProduction(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_tasks.db")
        self.coordinator = ANTITaskCoordinator(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_canonical_states_lifecycle(self):
        """WAITING -> QUEUED -> RUNNING -> DONE."""
        t1 = Task(task_id="T1", title="Step 1", description="Implement auth", domain=TaskDomain.ENGINEERING)
        t2 = Task(task_id="T2", title="Step 2", description="Test auth", domain=TaskDomain.ENGINEERING, depends_on=["T1"])

        self.coordinator.submit_goal_plan([t1, t2])

        # T1 has no deps -> QUEUED. T2 has deps -> WAITING
        self.assertEqual(self.coordinator.registry.get_task("T1").status, TaskStatus.QUEUED)
        self.assertEqual(self.coordinator.registry.get_task("T2").status, TaskStatus.WAITING)

        # Lease T1
        leased_t1, token_1 = self.coordinator.lease_task("T1", "worker-1")
        self.assertEqual(leased_t1.status, TaskStatus.RUNNING)
        self.assertTrue(token_1.startswith("lease_"))

        # Submit completion with manifest
        manifest_1 = CompletionManifest(
            task_id="T1",
            worker_id="worker-1",
            lease_token=token_1,
            status="SUCCESS",
            artifacts=["auth.py"]
        )
        ok, reason, unblocked = self.coordinator.submit_completion(manifest_1)
        self.assertTrue(ok)
        self.assertEqual(self.coordinator.registry.get_task("T1").status, TaskStatus.DONE)

        # Auto Next-Task unblocking: T2 should now be QUEUED!
        self.assertEqual(len(unblocked), 1)
        self.assertEqual(unblocked[0].task_id, "T2")
        self.assertEqual(self.coordinator.registry.get_task("T2").status, TaskStatus.QUEUED)

    def test_fencing_token_prevents_split_brain(self):
        """Worker 1 lags -> lease stolen by reaper -> Worker 1 cannot complete task with old token."""
        t = Task(task_id="T_FENCE", title="Fenced Task", description="Critical write", domain=TaskDomain.REVENUE, lease_timeout_sec=0.1)
        self.coordinator.submit_goal_plan([t])

        _, token_1 = self.coordinator.lease_task("T_FENCE", "worker-lagging")

        # Lease expires and task is reaped
        time.sleep(0.15)
        self.coordinator.reap_stale_leases()

        # Task is re-queued, now leased by Worker 2
        _, token_2 = self.coordinator.lease_task("T_FENCE", "worker-fast")
        self.assertNotEqual(token_1, token_2)

        # Old lagging worker attempts completion with expired token_1 -> REJECTED!
        stale_manifest = CompletionManifest(
            task_id="T_FENCE",
            worker_id="worker-lagging",
            lease_token=token_1,
            status="SUCCESS"
        )
        ok, reason, _ = self.coordinator.submit_completion(stale_manifest)
        self.assertFalse(ok)
        self.assertIn("Fencing token mismatch", reason)

        # Fast worker submits with token_2 -> ACCEPTED!
        valid_manifest = CompletionManifest(
            task_id="T_FENCE",
            worker_id="worker-fast",
            lease_token=token_2,
            status="SUCCESS"
        )
        ok2, _, _ = self.coordinator.submit_completion(valid_manifest)
        self.assertTrue(ok2)
        self.assertEqual(self.coordinator.registry.get_task("T_FENCE").status, TaskStatus.DONE)

    def test_acceptance_gate_enforcement(self):
        """Task with criteria fails if required artifacts or assertions are missing."""
        t = Task(
            task_id="T_GATE",
            title="Gate Test",
            description="Produce clean patch",
            domain=TaskDomain.ENGINEERING,
            acceptance_criteria={
                "required_artifacts": ["patch.diff"],
                "assertions": {"tests_passed": True}
            }
        )
        self.coordinator.submit_goal_plan([t])
        _, token = self.coordinator.lease_task("T_GATE", "worker-e")

        # Manifest missing patch.diff -> REJECTED
        bad_manifest = CompletionManifest(
            task_id="T_GATE",
            worker_id="worker-e",
            lease_token=token,
            status="SUCCESS",
            artifacts=["wrong.txt"],
            evidence={"tests_passed": True}
        )
        ok, reason, _ = self.coordinator.submit_completion(bad_manifest)
        self.assertFalse(ok)
        self.assertIn("Missing mandatory artifact", reason)

    def test_deduplication_idempotency(self):
        """Tasks submitted with identical dedupe_key are not duplicated."""
        t1 = Task(task_id="UUID_1", title="Scan PR #4599", description="bounty scan", domain=TaskDomain.REVENUE, dedupe_key="pr_4599_scan")
        t2 = Task(task_id="UUID_2", title="Scan PR #4599 Duplicate", description="bounty scan duplicate", domain=TaskDomain.REVENUE, dedupe_key="pr_4599_scan")

        res1 = self.coordinator.submit_goal_plan([t1])
        res2 = self.coordinator.submit_goal_plan([t2])

        all_tasks = self.coordinator.registry.list_tasks()
        self.assertEqual(len(all_tasks), 1)
        self.assertEqual(all_tasks[0].task_id, "UUID_1")


if __name__ == "__main__":
    unittest.main()
