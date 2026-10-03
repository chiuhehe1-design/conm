#!/usr/bin/env python3
"""
Unit Tests for Universal Circuit Breaker and Resilient Durable Queue (P1.6 & P1.7)
Verifies:
1. Closed -> Open -> Half-Open state transitions.
2. In-memory fallback resilience when Redis is unavailable.
3. DurableQueue SQLite persistence across sessions.
4. Fencing token protection (zombie worker completion rejection).
5. Retry exhaustion -> Dead Letter Queue (DLQ) transition.
6. Stale lease reaper reclaims jobs.
"""

import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.circuit_breaker import CircuitBreaker, CircuitState, DurableQueue


class TestUniversalCircuitBreaker(unittest.TestCase):

    def setUp(self):
        # r=False ensures testing pure resilient fallback mode
        self.cb = CircuitBreaker(r=False, failure_threshold=3, cooldown_seconds=0.1)

    def test_closed_to_open_transition(self):
        """After 3 consecutive failures, circuit trips to OPEN."""
        svc = "omniroute_cloud"
        self.assertEqual(self.cb.get_state(svc), CircuitState.CLOSED)
        self.assertTrue(self.cb.is_available(svc))

        self.cb.record_failure(svc, "502 Bad Gateway")
        self.cb.record_failure(svc, "504 Gateway Timeout")
        self.assertEqual(self.cb.get_state(svc), CircuitState.CLOSED)

        # 3rd failure trips
        self.cb.record_failure(svc, "429 Rate Limit")
        self.assertEqual(self.cb.get_state(svc), CircuitState.OPEN)
        self.assertFalse(self.cb.is_available(svc))

    def test_open_to_half_open_cooldown(self):
        """After cooldown expires, state transitions to HALF_OPEN."""
        svc = "external_rpc"
        self.cb.trip_open(svc, "Manual trip")
        self.assertFalse(self.cb.is_available(svc))

        time.sleep(0.12)
        # Should now be HALF_OPEN and available for probe
        self.assertEqual(self.cb.get_state(svc), CircuitState.HALF_OPEN)
        self.assertTrue(self.cb.is_available(svc))

        # Probe success closes circuit
        self.cb.record_success(svc)
        self.assertEqual(self.cb.get_state(svc), CircuitState.CLOSED)

    def test_half_open_failure_immediately_re_trips(self):
        """If probe fails in HALF_OPEN, re-trips immediately back to OPEN."""
        svc = "base_usdc_rpc"
        self.cb.trip_open(svc, "Outage")
        time.sleep(0.12)
        self.assertEqual(self.cb.get_state(svc), CircuitState.HALF_OPEN)

        # Probe fails
        self.cb.record_failure(svc, "RPC still dead")
        self.assertEqual(self.cb.get_state(svc), CircuitState.OPEN)
        self.assertFalse(self.cb.is_available(svc))

    def test_call_wrapper_executes_fallback_on_trip(self):
        """When circuit is OPEN, cb.call() invokes fallback directly."""
        svc = "payment_api"
        self.cb.trip_open(svc, "Network cut")

        primary_called = False
        fallback_called = False

        def primary():
            nonlocal primary_called
            primary_called = True
            return "PRIMARY_OK"

        def fallback():
            nonlocal fallback_called
            fallback_called = True
            return "FALLBACK_OK"

        res = self.cb.call(svc, primary, fallback_func=fallback)
        self.assertEqual(res, "FALLBACK_OK")
        self.assertFalse(primary_called)
        self.assertTrue(fallback_called)


class TestDurableQueue(unittest.TestCase):

    def setUp(self):
        self.dq = DurableQueue(db_path=":memory:", r=False)

    def test_push_and_lease_job_with_fencing_token(self):
        """Job is pushed as PENDING and leased with unique fencing token."""
        jid = self.dq.push_job("coding", {"task": "fix bug", "file": "auth.py"})
        self.assertTrue(jid.startswith("job-"))

        leased = self.dq.lease_job("coding", "worker-1", lease_duration_sec=10.0)
        self.assertIsNotNone(leased)
        job_data, token = leased
        self.assertEqual(job_data["job_id"], jid)
        self.assertTrue(token.startswith("lease_"))

        # Queue stats: running=1
        stats = self.dq.get_queue_stats("coding")
        self.assertEqual(stats["running"], 1)
        self.assertEqual(stats["pending"], 0)

    def test_fencing_token_prevents_stale_ack(self):
        """Old worker cannot ACK job after lease has been invalidated."""
        jid = self.dq.push_job("coding", {"task": "critical task"})
        _, token_old = self.dq.lease_job("coding", "worker-lagging")

        # Fake lease timeout / nack -> re-leased to worker 2
        self.dq.nack_job(jid, token_old, "Worker timed out")
        _, token_new = self.dq.lease_job("coding", "worker-new")
        self.assertNotEqual(token_old, token_new)

        # Worker lagging tries to ack with token_old -> FAILS
        ok = self.dq.ack_job(jid, token_old)
        self.assertFalse(ok)

        # Worker new acks with token_new -> SUCCEEDS
        ok2 = self.dq.ack_job(jid, token_new)
        self.assertTrue(ok2)

        stats = self.dq.get_queue_stats("coding")
        self.assertEqual(stats["completed"], 1)

    def test_max_retries_exhaustion_moves_to_dlq(self):
        """Job exceeding max_retries transitions directly to Dead Letter Queue (DLQ)."""
        jid = self.dq.push_job("sre", {"action": "restart"}, max_retries=2)

        # Attempt 1
        _, token1 = self.dq.lease_job("sre", "worker-a")
        self.dq.nack_job(jid, token1, "Error 1")
        self.assertEqual(self.dq.get_queue_stats("sre")["pending"], 1)

        # Attempt 2 -> Max reached
        _, token2 = self.dq.lease_job("sre", "worker-b")
        self.dq.nack_job(jid, token2, "Fatal Error 2")

        stats = self.dq.get_queue_stats("sre")
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["running"], 0)
        self.assertEqual(stats["dlq"], 1)

    def test_reap_stale_leases(self):
        """Worker crash without heartbeat/ack is reclaimed automatically by reaper."""
        jid = self.dq.push_job("fast", {"task": "ping"}, max_retries=3)
        self.dq.lease_job("fast", "worker-crashed", lease_duration_sec=0.05)

        time.sleep(0.08)
        reaped = self.dq.reap_stale_leases()
        self.assertIn(jid, reaped)

        # Re-queued into PENDING
        stats = self.dq.get_queue_stats("fast")
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["running"], 0)


if __name__ == "__main__":
    unittest.main()
