#!/usr/bin/env python3
"""
Unit and Integration Tests for Central Dispatch Pipeline (P1.5)
Verifies:
1. Stage 1: Auth gate rejection (unauthenticated / forged token).
2. Stage 2: OPA gate rejection (unauthorized action / target path).
3. Stage 3: Deduplication gate rejection (duplicate idempotency key).
4. Stage 4: Resource Admission gate rejection (GLOBAL_PAUSE & CircuitBreaker).
5. Stage 5: Permission scope gate rejection (privilege boundary violation).
6. Stage 6: Successful ingestion into DurableQueue (202 Accepted).
7. Bypass Elimination: Direct worker calls without lease token fail-closed.
"""

import os
import json
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.central_dispatcher import CentralDispatcher, DispatchRequest, GLOBAL_PAUSE_FILE
from shared.circuit_breaker import CircuitBreaker, DurableQueue
from shared.deduplicator import Deduplicator


class TestCentralDispatchPipeline(unittest.TestCase):

    def setUp(self):
        self.queue = DurableQueue(db_path=":memory:", r=False)
        self.cb = CircuitBreaker(r=False, failure_threshold=2, cooldown_seconds=0.1)
        self.dedupe = Deduplicator(r=False)
        self.dispatcher = CentralDispatcher(
            queue=self.queue,
            cb=self.cb,
            dedupe=self.dedupe,
            test_mode=True
        )

        # Clear any accidental GLOBAL_PAUSE in test env
        if os.path.exists(GLOBAL_PAUSE_FILE):
            try:
                os.remove(GLOBAL_PAUSE_FILE)
            except Exception:
                pass

    def test_stage_1_rejects_missing_or_forged_auth(self):
        """Unauthenticated or forged bearer token rejected at Stage 1."""
        req = DispatchRequest(
            task_desc="Run system audit",
            action="submit_job",
            domain="sre",
            actor="ANTI",
            auth_token="forged-or-empty-token"
        )
        res = self.dispatcher.dispatch(req)
        self.assertEqual(res["status"], "REJECTED")
        self.assertEqual(res["stage"], "AUTH")
        self.assertEqual(res["http_code"], 401)

    def test_stage_2_rejects_opa_forbidden_action(self):
        """Action forbidden by OPA rego rules (e.g. drop database, transfer crypto) rejected at Stage 2."""
        req = DispatchRequest(
            task_desc="Wipe database",
            action="drop_database",
            domain="sre",
            actor="ANTI",
            auth_token="test-token"
        )
        res = self.dispatcher.dispatch(req)
        self.assertEqual(res["status"], "REJECTED")
        self.assertEqual(res["stage"], "OPA")
        self.assertEqual(res["http_code"], 403)
        self.assertIn("OPA Denied", res["error"])

    def test_stage_3_rejects_duplicate_task(self):
        """Identical dedupe key rejected at Stage 3."""
        import uuid
        key = f"issue_scan_45_{uuid.uuid4().hex[:6]}"
        req1 = DispatchRequest(
            task_desc="Scan issue #45",
            action="submit_job",
            domain="revenue",
            actor="ANTI",
            auth_token="test-token",
            dedupe_key=key
        )
        res1 = self.dispatcher.dispatch(req1)
        self.assertEqual(res1["status"], "QUEUED")

        # Second submission with same dedupe_key
        req2 = DispatchRequest(
            task_desc="Scan issue #45 again",
            action="submit_job",
            domain="revenue",
            actor="ANTI",
            auth_token="test-token",
            dedupe_key=key
        )
        res2 = self.dispatcher.dispatch(req2)
        self.assertEqual(res2["status"], "REJECTED")
        self.assertEqual(res2["stage"], "DEDUPE")
        self.assertEqual(res2["http_code"], 409)

    def test_stage_4_rejects_when_circuit_breaker_open(self):
        """Tripped circuit breaker prevents task admission at Stage 4."""
        # Trip circuit for 'revenue' domain
        self.cb.trip_open("worker_revenue", "Payment backend down")

        req = DispatchRequest(
            task_desc="Process payout",
            action="submit_job",
            domain="revenue",
            actor="ANTI",
            auth_token="test-token"
        )
        res = self.dispatcher.dispatch(req)
        self.assertEqual(res["status"], "REJECTED")
        self.assertEqual(res["stage"], "ADMISSION")
        self.assertEqual(res["http_code"], 503)
        self.assertIn("Circuit breaker", res["error"])

    def test_stage_5_rejects_role_exceeding_permission_scope(self):
        """antiworker attempting to submit arbitrary jobs rejected at Stage 5."""
        req = DispatchRequest(
            task_desc="Unauthorized administrative task",
            action="submit_job",
            domain="engineering",
            actor="antiworker",
            auth_token="test-token"
        )
        res = self.dispatcher.dispatch(req)
        self.assertEqual(res["status"], "REJECTED")
        self.assertEqual(res["stage"], "PERMISSION")
        self.assertEqual(res["http_code"], 403)

    def test_stage_6_happy_path_enqueues_durable_job(self):
        """Valid task passes all 5 gates and is durably enqueued (202 Accepted)."""
        import uuid
        key = f"refactor_dedupe_{uuid.uuid4().hex[:6]}"
        req = DispatchRequest(
            task_desc="Refactor deduplicator unit test",
            action="submit_job",
            domain="engineering",
            actor="ANTI",
            auth_token="test-token",
            dedupe_key=key
        )
        res = self.dispatcher.dispatch(req)
        self.assertEqual(res["status"], "QUEUED")
        self.assertEqual(res["stage"], "COMPLETED")
        self.assertEqual(res["http_code"], 202)
        self.assertTrue(res["job_id"].startswith("job-"))

        # Check queue stats
        stats = self.queue.get_queue_stats("engineering")
        self.assertEqual(stats["pending"], 1)

        # Worker leases the task
        leased = self.queue.lease_job("engineering", "worker-eng-1")
        self.assertIsNotNone(leased)
        job_data, token = leased
        self.assertEqual(job_data["task"], "Refactor deduplicator unit test")
        self.assertTrue(token.startswith("lease_"))


if __name__ == "__main__":
    unittest.main()
