#!/usr/bin/env python3
"""
Unit Tests for Canary Release Manager & Automatic Rollback (P1.11 / P1-08)
"""

import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.canary_release_manager import CanaryReleaseManager, CanaryState


class TestCanaryReleaseManager(unittest.TestCase):

    def setUp(self):
        self.mgr = CanaryReleaseManager(test_mode=True)

    def test_canary_lifecycle_promote_happy_path(self):
        """Happy path: test gate passes -> canary staged -> SLA verified -> promoted to 100%."""
        manifest = self.mgr.initiate_canary_release("c4d3a2b", canary_fraction=0.10)
        self.assertEqual(manifest.state, CanaryState.CANARY_ACTIVE)
        self.assertEqual(manifest.canary_fraction, 0.10)
        self.assertEqual(manifest.target_git_sha, "c4d3a2b")

        # Healthcheck within SLAs
        healthy, reason = self.mgr.verify_canary_health(error_rate=0.001, p99_latency_ms=120.0)
        self.assertTrue(healthy)
        self.assertEqual(self.mgr.current_release.state, CanaryState.HEALTHY)

        # Promotion
        promoted = self.mgr.promote_to_production()
        self.assertEqual(promoted.state, CanaryState.PROMOTED)
        self.assertEqual(promoted.canary_fraction, 1.0)

    def test_canary_sla_breach_triggers_rollback(self):
        """Error rate breach (>1%) triggers health failure and allows immediate automated rollback."""
        manifest = self.mgr.initiate_canary_release("bad_sha_01", canary_fraction=0.10)
        self.assertEqual(manifest.state, CanaryState.CANARY_ACTIVE)

        # SLA breach: error rate 2.5%
        healthy, reason = self.mgr.verify_canary_health(error_rate=0.025, p99_latency_ms=500.0)
        self.assertFalse(healthy)
        self.assertIn("Canary SLA Breached", reason)

        # Automated rollback
        rolled_back = self.mgr.trigger_automatic_rollback("SLA error rate breach")
        self.assertEqual(rolled_back.state, CanaryState.ROLLED_BACK)
        self.assertIn("SLA error rate breach", rolled_back.error_message)


if __name__ == "__main__":
    unittest.main()
