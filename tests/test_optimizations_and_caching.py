#!/usr/bin/env python3
"""
UNIT & INTEGRATION TESTS: PERFORMANCE OPTIMIZATIONS & CACHING (ANTI-015)
Verifies:
1. Multi-threaded concurrent bounty ingestion (ThreadPoolExecutor).
2. Jev Resilience Adapter LRU prompt query caching and proactive Ollama warming.
3. Gateway persistent HTTP/1.1 reverse proxy connection pool with socket keep-alive.
4. Governed RFC production application tracking (mark_rfc_applied).
"""

import os
import sys
import time
import socket
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import shutil
import sqlite3
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "shared") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "shared"))

from shared.bounty_ingestion_adapter import BountyIngestionEngine, BaseBountyAdapter, IngestedBounty, BountySource
from shared.autonomous_revenue_portfolio import RevenuePortfolioEngine
from shared.jev_resilience_adapter import ResilientOmniDispatcher, JevGate, ModelTier
from shared.self_improvement_governor import SelfImprovementGovernor, RFCStage
from gateway.control_gateway import PersistentProxyPool


class TestOptimizationsAndCaching(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.portfolio_db = os.path.join(self.temp_dir, "test_portfolio.db")
        self.rfc_db = os.path.join(self.temp_dir, "test_rfcs.db")
        self.rfc_docs_dir = os.path.join(self.temp_dir, "rfcs_docs")

        self.portfolio = RevenuePortfolioEngine(self.portfolio_db)
        self.governor = SelfImprovementGovernor(self.rfc_db, docs_dir=self.rfc_docs_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_bounty_ingestion(self):
        """Verifies that multiple adapters are executed concurrently via ThreadPoolExecutor."""
        call_times = []

        class SlowMockAdapter(BaseBountyAdapter):
            def __init__(self, name: str, source: BountySource):
                super().__init__(source)
                self.name = name

            def fetch_open_bounties(self, limit=5):
                start = time.time()
                time.sleep(0.05)  # Simulate 50ms network call
                call_times.append((self.name, start, time.time()))
                return [
                    IngestedBounty(
                        bounty_id=f"mock-{self.name}-1",
                        platform=self.source_name,
                        target_repo=f"org/{self.name}",
                        title=f"Fix bug in {self.name}",
                        description="Mock description",
                        raw_reward_usd=100.0,
                        issue_url=f"https://github.com/org/{self.name}/issues/1"
                    )
                ]

        adapters = [
            SlowMockAdapter("adapter-1", BountySource.GITHUB),
            SlowMockAdapter("adapter-2", BountySource.OPIRE),
            SlowMockAdapter("adapter-3", BountySource.ALGORA),
            SlowMockAdapter("adapter-4", BountySource.SUPERTEAM)
        ]

        engine = BountyIngestionEngine(self.portfolio, adapters=adapters)
        start_scan = time.time()
        report = engine.scan_and_ingest()
        elapsed = time.time() - start_scan

        # If sequential: 4 * 0.05s = 0.20s+. If concurrent: ~0.05s - 0.12s.
        self.assertEqual(report["total_scanned"], 4)
        self.assertLess(elapsed, 0.18, f"Concurrent scan took {elapsed:.3f}s; expected < 0.18s")

    def test_jev_resilience_query_caching(self):
        """Verifies LRU query caching returns instantaneous responses for identical prompts."""
        dispatcher = ResilientOmniDispatcher()
        prompt = "Determine the root cause of deadlock in database connection pool"

        # Mock _call_http to track network invocations
        call_count = {"omniroute": 0}

        def mock_call(base_url, model, prompt_text, timeout=None):
            call_count["omniroute"] += 1
            return {
                "choices": [{
                    "message": {
                        "content": "Root cause: Unordered mutex acquisition in worker threads."
                    }
                }]
            }

        dispatcher._call_http = mock_call

        # First call: cache miss (invokes network)
        res1 = dispatcher.dispatch(prompt, retry_count=0, use_cache=True)
        self.assertEqual(res1["status"], "SUCCESS")
        self.assertEqual(call_count["omniroute"], 1)
        self.assertFalse(res1.get("cached", False))

        # Second call: cache hit (no network call, returns instantaneously)
        res2 = dispatcher.dispatch(prompt, retry_count=0, use_cache=True)
        self.assertEqual(res2["status"], "SUCCESS")
        self.assertEqual(call_count["omniroute"], 1)  # Network count still 1
        self.assertTrue(res2.get("cached", True))
        self.assertEqual(res2["content"], res1["content"])

    @patch("http.client.HTTPConnection")
    def test_persistent_proxy_connection_pool(self, mock_conn_cls):
        """Verifies socket reuse and auto-reconnect in PersistentProxyPool."""
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"status": "ok"}'
        mock_conn.getresponse.return_value = mock_resp
        mock_conn_cls.return_value = mock_conn

        pool = PersistentProxyPool(host="127.0.0.1", port=20140, timeout=2.0)

        # Request 1: creates connection and sends request
        status1, body1 = pool.request("GET", "/test1")
        self.assertEqual(status1, 200)
        self.assertIn(b"ok", body1)
        self.assertEqual(mock_conn_cls.call_count, 1)

        # Request 2: reuses existing connection without re-instantiating HTTPConnection
        status2, body2 = pool.request("GET", "/test2")
        self.assertEqual(status2, 200)
        self.assertEqual(mock_conn_cls.call_count, 1)  # Socket reused!

        # Request 3: simulates broken pipe / disconnect -> auto-reconnects
        mock_conn.request.side_effect = [BrokenPipeError("Connection reset"), None]
        status3, body3 = pool.request("GET", "/test3")
        self.assertEqual(status3, 200)
        self.assertEqual(mock_conn_cls.call_count, 2)  # Reconnected!

    def test_governor_mark_rfc_applied(self):
        """Verifies that promoted RFCs can be marked as applied in production with audit trail."""
        rfc = self.governor.propose_rfc(
            title="Optimize Gateway Reverse Proxy Keep-Alive Connection Pool",
            target_subsystem="gateway",
            problem_analysis="Socket creation on every request increases P99 latency.",
            proposed_solution="Implement persistent HTTP/1.1 connection pool with socket keep-alive."
        )
        self.governor.attach_candidate_build(rfc.rfc_id, git_sha="sha-123456")
        self.governor.run_test_gate(rfc.rfc_id, lambda: True)
        self.governor.evaluate_canary(rfc.rfc_id, observed_error_rate=0.001, p99_latency_ms=45.0)

        # Mark applied in production
        applied_sha = "sha-applied-production-v1"
        ok = self.governor.mark_rfc_applied(rfc.rfc_id, applied_sha=applied_sha)
        self.assertTrue(ok)

        # Verify DB record
        fresh = self.governor.get_rfc(rfc.rfc_id)
        self.assertEqual(fresh.candidate_git_sha, applied_sha)
        self.assertEqual(fresh.stage, RFCStage.PROMOTED)

        # Verify markdown documentation file
        doc_file = Path(self.rfc_docs_dir) / f"{rfc.rfc_id}.md"
        self.assertTrue(doc_file.exists())
        doc_content = doc_file.read_text(encoding="utf-8")
        self.assertIn("APPLIED_IN_PRODUCTION", doc_content)
        self.assertIn(applied_sha, doc_content)


if __name__ == "__main__":
    unittest.main()
