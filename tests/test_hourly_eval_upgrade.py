#!/usr/bin/env python3
"""
Unit and Integration tests for Hourly Test, Evaluation & Self-Upgrade Engine (FEATURE-019)
"""

import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "shared") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "shared"))

from tools.run_hourly_eval_upgrade import HourlyEvalUpgradeEngine, HourlyCycleReport


class TestHourlyEvalUpgrade(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.engine = HourlyEvalUpgradeEngine(repo_root=Path(self.tmp_dir.name))

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_hourly_engine_success_cycle(self):
        with patch.object(self.engine, "run_test_suite", return_value=(True, "108 tests OK")):
            report = self.engine.run_hourly_cycle()
            self.assertEqual(report.status, "SUCCESS")
            self.assertTrue(report.tests_passed)
            self.assertEqual(len(report.certificate_sha256), 64)
            self.assertGreater(report.duration_sec, 0.0)
            self.assertIn("portfolio", report.wal_status)

    def test_hourly_engine_fail_closed_on_test_failure(self):
        with patch.object(self.engine, "run_test_suite", return_value=(False, "FAILED (failures=1)")):
            report = self.engine.run_hourly_cycle()
            self.assertEqual(report.status, "TEST_GATE_FAILED")
            self.assertFalse(report.tests_passed)
            self.assertEqual(report.rfcs_promoted, 0)
            self.assertEqual(len(report.workforce_actions), 0)
            self.assertIn("Test gate failed", report.error_message)

    def test_hourly_engine_paused(self):
        pause_file = Path(self.tmp_dir.name) / "GLOBAL_PAUSE"
        pause_file.touch()

        with patch("tools.run_hourly_eval_upgrade.GLOBAL_PAUSE_FILE", str(pause_file)):
            report = self.engine.run_hourly_cycle()
            self.assertEqual(report.status, "SKIPPED_PAUSED")
            self.assertIn("GLOBAL_PAUSE", report.error_message)


if __name__ == "__main__":
    unittest.main()
