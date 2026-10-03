#!/usr/bin/env python3
"""
Unit and integration tests for Workforce Lifecycle & WorkerScore Scheduler Engine (P2-06 & P2-07).
Tests:
1. Canonical 10-state lifecycle transitions and rejection of illegal state jumps.
2. Worker registration, persistence and retrieval in SQLite.
3. Multi-factor WorkerScore calculations across skills, domain fit, and latency.
4. Selection ranking preferring higher capability workers over naive round-robin.
5. Overload filtering and canary inclusion logic.
6. Task assign and complete feedback loops updating quality and load.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.workforce_lifecycle_scheduler import (
    WorkforceScheduler,
    WorkerLifecycleState,
    WorkerProfile
)


class TestWorkforceScheduler(unittest.TestCase):

    def setUp(self):
        self.scheduler = WorkforceScheduler(db_path=":memory:")

    def test_lifecycle_transitions(self):
        # Register in PROVISION
        w = self.scheduler.register_worker(
            "w-01", "Worker-1", "engineering", ["python"],
            initial_state=WorkerLifecycleState.PROVISION
        )
        self.assertEqual(w.lifecycle_state, WorkerLifecycleState.PROVISION)

        # PROVISION -> TRAIN: Valid
        self.assertTrue(self.scheduler.transition_lifecycle("w-01", WorkerLifecycleState.TRAIN))
        self.assertEqual(self.scheduler.get_worker("w-01").lifecycle_state, WorkerLifecycleState.TRAIN)

        # TRAIN -> READY: Invalid (must go SHADOW -> CANARY -> READY)
        self.assertFalse(self.scheduler.transition_lifecycle("w-01", WorkerLifecycleState.READY))

        # TRAIN -> SHADOW -> CANARY -> READY: Valid
        self.assertTrue(self.scheduler.transition_lifecycle("w-01", WorkerLifecycleState.SHADOW))
        self.assertTrue(self.scheduler.transition_lifecycle("w-01", WorkerLifecycleState.CANARY))
        self.assertTrue(self.scheduler.transition_lifecycle("w-01", WorkerLifecycleState.READY))

        # READY -> RETRAIN -> TRAIN: Valid
        self.assertTrue(self.scheduler.transition_lifecycle("w-01", WorkerLifecycleState.RETRAIN))
        self.assertTrue(self.scheduler.transition_lifecycle("w-01", WorkerLifecycleState.TRAIN))

    def test_workerscore_calculation_and_ranking(self):
        # Worker A: Full skill match + high quality + engineering domain
        self.scheduler.register_worker(
            "w-eng-senior", "Senior Engineer", "engineering",
            skills=["python", "fastapi", "rego"],
            initial_state=WorkerLifecycleState.READY,
            max_load=5
        )
        # Worker B: Only partial skill match + revenue domain
        self.scheduler.register_worker(
            "w-rev-junior", "Junior Revenue", "revenue",
            skills=["rego"],
            initial_state=WorkerLifecycleState.READY,
            max_load=5
        )

        task_req = {
            "required_skills": ["python", "fastapi"],
            "domain": "engineering"
        }

        best = self.scheduler.select_best_worker(task_req)
        self.assertIsNotNone(best)
        best_worker, score = best
        self.assertEqual(best_worker.worker_id, "w-eng-senior")
        self.assertGreater(score, 80.0)

        # Compare directly with Worker B's score
        wb = self.scheduler.get_worker("w-rev-junior")
        score_b = self.scheduler.calculate_worker_score(wb, task_req)
        self.assertGreater(score, score_b)

    def test_overload_exclusion(self):
        self.scheduler.register_worker(
            "w-busy", "Busy Worker", "engineering",
            skills=["python"],
            initial_state=WorkerLifecycleState.READY,
            max_load=1
        )
        # Assign 1 task -> max_load reached
        self.assertTrue(self.scheduler.assign_task_to_worker("w-busy"))
        self.assertEqual(self.scheduler.get_worker("w-busy").current_load, 1)

        # Should not be selected because current_load == max_load
        task_req = {"required_skills": ["python"]}
        res = self.scheduler.select_best_worker(task_req)
        self.assertIsNone(res)

    def test_canary_selection_flag(self):
        self.scheduler.register_worker(
            "w-canary", "Canary Tester", "engineering",
            skills=["python"],
            initial_state=WorkerLifecycleState.CANARY
        )
        task_req = {"required_skills": ["python"]}

        # Without allow_canary -> None
        self.assertIsNone(self.scheduler.select_best_worker(task_req, allow_canary=False))

        # With allow_canary -> Selected
        best = self.scheduler.select_best_worker(task_req, allow_canary=True)
        self.assertIsNotNone(best)
        self.assertEqual(best[0].worker_id, "w-canary")

    def test_task_completion_feedback_loop(self):
        self.scheduler.register_worker(
            "w-loop", "Loop Worker", "engineering",
            skills=["python"],
            initial_state=WorkerLifecycleState.READY
        )
        self.scheduler.assign_task_to_worker("w-loop")

        # Worker is now WORKING with load 1
        w = self.scheduler.get_worker("w-loop")
        self.assertEqual(w.current_load, 1)
        self.assertEqual(w.lifecycle_state, WorkerLifecycleState.WORKING)

        # Complete task successfully
        self.scheduler.complete_task_for_worker("w-loop", success=True, latency_ms=300.0)
        w = self.scheduler.get_worker("w-loop")
        self.assertEqual(w.current_load, 0)
        self.assertEqual(w.lifecycle_state, WorkerLifecycleState.READY)
        self.assertEqual(w.failure_count, 0)

        # Complete another task with failure
        self.scheduler.assign_task_to_worker("w-loop")
        self.scheduler.complete_task_for_worker("w-loop", success=False, latency_ms=1200.0)
        w = self.scheduler.get_worker("w-loop")
        self.assertEqual(w.failure_count, 1)
        self.assertLess(w.quality_rate, 1.0)


if __name__ == "__main__":
    unittest.main()
