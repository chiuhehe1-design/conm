#!/usr/bin/env python3
"""
Unit and integration tests for Network Governor & HTTPS 443 Attribution (P2-01, P2-02, P2-14, P2-15).
Tests:
1. Mapping of 5 canonical tiers to fwmark (0x10 - 0x50) and tc classes (1:10 - 1:50).
2. HTTPS 443 classification based on worker domain/actor identity.
3. Atomic recording of per-job bandwidth usage in SQLite.
4. Quota breach detection and alerting.
5. Per-worker aggregate calculation across multiple jobs.
6. Generation of idempotent Linux tc / iptables shell script.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.network_governor import (
    NetworkGovernor,
    TrafficTier,
    TIER_POLICIES
)


class TestNetworkGovernor(unittest.TestCase):

    def setUp(self):
        self.gov = NetworkGovernor(db_path=":memory:")

    def test_traffic_classification_mapping(self):
        # 1. Control / Payment
        cp = self.gov.classify_traffic("payment_reconciler")
        self.assertEqual(cp.tier, TrafficTier.CONTROL_PAYMENT)
        self.assertEqual(cp.fwmark, 0x10)
        self.assertEqual(cp.tc_class, "1:10")
        self.assertEqual(cp.priority, 1)

        # 2. Production
        prod = self.gov.classify_traffic("production_worker")
        self.assertEqual(prod.tier, TrafficTier.PRODUCTION)
        self.assertEqual(prod.fwmark, 0x20)
        self.assertEqual(prod.tc_class, "1:20")
        self.assertEqual(prod.priority, 2)

        # 3. Coding
        code = self.gov.classify_traffic("bounty_engineer", task_type="code_generation")
        self.assertEqual(code.tier, TrafficTier.CODING)
        self.assertEqual(code.fwmark, 0x30)
        self.assertEqual(code.tc_class, "1:30")
        self.assertEqual(code.priority, 3)

        # 4. Browser
        brow = self.gov.classify_traffic("agent-prd-browser")
        self.assertEqual(brow.tier, TrafficTier.BROWSER)
        self.assertEqual(brow.fwmark, 0x40)
        self.assertEqual(brow.tc_class, "1:40")
        self.assertEqual(brow.priority, 4)

        # 5. Background
        bg = self.gov.classify_traffic("backup_daemon")
        self.assertEqual(bg.tier, TrafficTier.BACKGROUND)
        self.assertEqual(bg.fwmark, 0x50)
        self.assertEqual(bg.tc_class, "1:50")
        self.assertEqual(bg.priority, 5)

    def test_record_usage_and_retrieval(self):
        usage = self.gov.record_usage(
            job_id="job-net-01",
            worker_id="w-eng-01",
            domain_or_actor="engineer",
            bytes_sent=1024,
            bytes_received=4096,
            packets_sent=10,
            packets_received=20,
            quota_bytes=10000
        )
        self.assertEqual(usage.job_id, "job-net-01")
        self.assertEqual(usage.tier, TrafficTier.CODING)
        self.assertFalse(usage.quota_breached)

        retrieved = self.gov.get_job_usage("job-net-01")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.bytes_sent, 1024)
        self.assertEqual(retrieved.bytes_received, 4096)
        self.assertEqual(retrieved.tier, TrafficTier.CODING)

    def test_quota_breach_detection(self):
        # Limit is 5000 bytes, total used is 2000 + 4000 = 6000 bytes
        usage = self.gov.record_usage(
            job_id="job-over-quota",
            worker_id="w-browser-01",
            domain_or_actor="browser",
            bytes_sent=2000,
            bytes_received=4000,
            quota_bytes=5000
        )
        self.assertTrue(usage.quota_breached)

        # Verify persisted status
        retrieved = self.gov.get_job_usage("job-over-quota")
        self.assertTrue(retrieved.quota_breached)

    def test_worker_aggregation(self):
        worker_id = "w-multi-job"
        self.gov.record_usage(
            job_id="job-01",
            worker_id=worker_id,
            domain_or_actor="coding",
            bytes_sent=1000,
            bytes_received=2000
        )
        self.gov.record_usage(
            job_id="job-02",
            worker_id=worker_id,
            domain_or_actor="coding",
            bytes_sent=3000,
            bytes_received=4000,
            quota_bytes=5000  # Will breach (7000 > 5000)
        )

        agg = self.gov.get_worker_aggregate(worker_id)
        self.assertEqual(agg["total_jobs"], 2)
        self.assertEqual(agg["total_bytes_sent"], 4000)
        self.assertEqual(agg["total_bytes_received"], 6000)
        self.assertEqual(agg["total_bytes"], 10000)
        self.assertEqual(agg["quota_breaches"], 1)

    def test_tc_script_generation(self):
        script = self.gov.generate_tc_setup_script("eth0")
        self.assertIn("#!/usr/bin/env bash", script)
        self.assertIn("classid 1:10", script)
        self.assertIn("classid 1:20", script)
        self.assertIn("classid 1:30", script)
        self.assertIn("classid 1:40", script)
        self.assertIn("classid 1:50", script)
        self.assertIn("handle 0x10 fw flowid 1:10", script)
        self.assertIn("handle 0x50 fw flowid 1:50", script)
        self.assertIn("--dport 443", script)


if __name__ == "__main__":
    unittest.main()
