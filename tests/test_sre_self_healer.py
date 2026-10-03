#!/usr/bin/env python3
"""
Unit and integration tests for Autonomous SRE Self-Healing Engine (P2-04 & P2-05).
Tests:
1. Severity classification (P0, P1, P2)
2. Provider outage -> CircuitBreaker trip & fallback trigger
3. Auth / Security failure -> Fail-closed immediate escalation (P0, no retry)
4. Whitelisted systemd service restart via OPA policy
5. Non-whitelisted service restart rejected by OPA policy
6. Verification failure -> AUTO_RECOVERY_EXHAUSTED -> Owner Alert
7. Persistence & incident retrieval in SQLite
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.sre_self_healer import (
    SRESelfHealingController,
    IncidentSeverity,
    IncidentStatus,
    IncidentReport
)
from shared.circuit_breaker import CircuitBreaker, CircuitState


class TestSRESelfHealer(unittest.TestCase):

    def setUp(self):
        self.cb = CircuitBreaker()
        self.controller = SRESelfHealingController(cb=self.cb, test_mode=True)

    def test_severity_classification(self):
        self.assertEqual(
            self.controller.detect_and_classify_severity("auth", "unauthorized bearer token"),
            IncidentSeverity.P0_CRITICAL
        )
        self.assertEqual(
            self.controller.detect_and_classify_severity("gateway", "gateway down detected"),
            IncidentSeverity.P0_CRITICAL
        )
        self.assertEqual(
            self.controller.detect_and_classify_severity("omniroute", "HTTP 502 Bad Gateway"),
            IncidentSeverity.P1_MAJOR
        )
        self.assertEqual(
            self.controller.detect_and_classify_severity("worker", "transient timeout reading stream"),
            IncidentSeverity.P2_MINOR
        )

    def test_provider_outage_trips_circuit_breaker(self):
        import uuid
        service_name = f"test_provider_{uuid.uuid4().hex[:6]}"
        # Initial state should be CLOSED
        self.assertEqual(self.cb.get_state(service_name), CircuitState.CLOSED)

        report = self.controller.diagnose_and_repair(
            subsystem=service_name,
            error_msg="HTTP 502: Bad Gateway Upstream",
            custom_verifier=lambda: True
        )

        self.assertEqual(report.status, IncidentStatus.VERIFIED_HEALTHY)
        self.assertIn("fallback active", "".join(report.actions_taken))
        # Provider outage should trip the circuit breaker to OPEN
        self.assertEqual(self.cb.get_state(service_name), CircuitState.OPEN)

    def test_auth_security_failure_immediate_escalation(self):
        report = self.controller.diagnose_and_repair(
            subsystem="gateway_auth",
            error_msg="HTTP 401: Unauthorized - forged signature detected",
            custom_verifier=lambda: True  # Should not even be called
        )

        self.assertEqual(report.severity, IncidentSeverity.P0_CRITICAL)
        self.assertEqual(report.status, IncidentStatus.ESCALATED_TO_OWNER)
        self.assertEqual(report.repair_attempts, 1)
        self.assertIn("Engaged security lockdown", "".join(report.actions_taken))

    def test_whitelisted_service_restart_opa_allowed(self):
        report = self.controller.diagnose_and_repair(
            subsystem="anti-supervisor.service",
            error_msg="anti-supervisor.service died unexpectedly with code 137",
            custom_verifier=lambda: True
        )

        self.assertEqual(report.status, IncidentStatus.VERIFIED_HEALTHY)
        self.assertTrue(any("Restarted systemd unit 'anti-supervisor.service' via OPA gate" in act for act in report.actions_taken))

    def test_non_whitelisted_service_restart_opa_denied(self):
        report = self.controller.diagnose_and_repair(
            subsystem="anti-malicious.service",
            error_msg="anti-malicious.service degraded",
            custom_verifier=lambda: False
        )

        self.assertEqual(report.status, IncidentStatus.AUTO_RECOVERY_EXHAUSTED)
        self.assertTrue(any("OPA policy rejected restart" in act for act in report.actions_taken))

    def test_auto_recovery_exhausted_escalation(self):
        attempts = 0
        def always_failing_verifier():
            nonlocal attempts
            attempts += 1
            return False

        report = self.controller.diagnose_and_repair(
            subsystem="worker-subsystem",
            error_msg="Unknown internal worker pipeline failure",
            custom_verifier=always_failing_verifier
        )

        self.assertEqual(report.status, IncidentStatus.AUTO_RECOVERY_EXHAUSTED)
        self.assertEqual(report.repair_attempts, 2)
        self.assertEqual(attempts, 2)
        self.assertIn("Auto-recovery exhausted (2 attempts). Alerting Owner.", report.actions_taken[-1])

    def test_db_persistence_and_query(self):
        self.controller.diagnose_and_repair("sub1", "transient error 1")
        self.controller.diagnose_and_repair("sub2", "unauthorized access attempt")

        incidents = self.controller.list_active_incidents()
        self.assertEqual(len(incidents), 2)
        severities = {inc["severity"] for inc in incidents}
        self.assertIn("P0", severities)
        self.assertIn("P2", severities)


if __name__ == "__main__":
    unittest.main()
