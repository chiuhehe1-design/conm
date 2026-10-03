#!/usr/bin/env python3
"""
Unit and Integration Tests for Jev Decision Gate & OmniRoute Resilience Matrix
Verifies: strong model, balanced model, free model, 429, timeout, provider down, OmniRoute down, Ollama fallback.
"""

import time
import json
import socket
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import threading
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.jev_resilience_adapter import (
    JevGate,
    ModelTier,
    ModelRouter,
    ResilientOmniDispatcher
)


class MockOmniHandler(BaseHTTPRequestHandler):
    mode = "OK"  # "OK", "429", "502", "TIMEOUT"

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len > 0:
            self.rfile.read(content_len)

        if self.mode == "429":
            body = b'{"error": {"message": "Rate limit exceeded", "code": "rate_limit_exceeded"}}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        elif self.mode == "502":
            body = b'{"error": {"message": "Bad Gateway / Provider down"}}'
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        elif self.mode == "TIMEOUT":
            time.sleep(0.3)
            body = b'{"status": "timeout"}'
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            body = json.dumps({"choices": [{"message": {"content": "MOCK_OMNI_SUCCESS"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()


class MockOllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len > 0:
            self.rfile.read(content_len)

        body = json.dumps({"choices": [{"message": {"content": "MOCK_OLLAMA_FALLBACK_SUCCESS"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


class TestJevOmniResilience(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Start ThreadingHTTPServer for OmniRoute
        cls.mock_omni = ThreadingHTTPServer(("127.0.0.1", 0), MockOmniHandler)
        cls.omni_port = cls.mock_omni.server_port
        cls.omni_thread = threading.Thread(target=cls.mock_omni.serve_forever, daemon=True)
        cls.omni_thread.start()

        # Start ThreadingHTTPServer for Ollama (Always available fallback)
        cls.mock_ollama = ThreadingHTTPServer(("127.0.0.1", 0), MockOllamaHandler)
        cls.ollama_port = cls.mock_ollama.server_port
        cls.ollama_thread = threading.Thread(target=cls.mock_ollama.serve_forever, daemon=True)
        cls.ollama_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.mock_omni.shutdown()
        cls.mock_ollama.shutdown()

    def setUp(self):
        MockOmniHandler.mode = "OK"
        self.dispatcher = ResilientOmniDispatcher(
            omniroute_url=f"http://127.0.0.1:{self.omni_port}/v1",
            ollama_url=f"http://127.0.0.1:{self.ollama_port}/v1",
            timeout_sec=0.1
        )

    def test_1_strong_model_routing(self):
        """Deep architectural or concurrency task routes to STRONG_REASONING."""
        prompt = "Architect a non-blocking deadlock prevention algorithm for distributed workers"
        dec = JevGate.evaluate(prompt)
        self.assertEqual(dec.tier, ModelTier.STRONG_REASONING)
        chain = ModelRouter.get_candidate_chain(dec.tier)
        self.assertIn("auto/best-reasoning", chain)

    def test_2_balanced_model_routing(self):
        """General standard task routes to BALANCED."""
        prompt = "Coordinate telemetry reporting across gateway nodes"
        dec = JevGate.evaluate(prompt)
        self.assertEqual(dec.tier, ModelTier.BALANCED)
        chain = ModelRouter.get_candidate_chain(dec.tier)
        self.assertIn("auto/best-coding", chain)

    def test_3_free_model_routing(self):
        """Lightweight format or typo fix routes to FAST."""
        prompt = "Fix typo in README and format markdown list"
        dec = JevGate.evaluate(prompt)
        self.assertEqual(dec.tier, ModelTier.FAST)
        chain = ModelRouter.get_candidate_chain(dec.tier)
        self.assertIn("auto/best-fast", chain)

    def test_4_rate_limit_429_failover(self):
        """When OmniRoute returns HTTP 429, dispatcher fails over to Ollama."""
        MockOmniHandler.mode = "429"
        res = self.dispatcher.dispatch("Check status")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res["fallback_used"])
        self.assertEqual(res["provider"], "OLLAMA_LOCAL_FALLBACK")
        self.assertEqual(res["content"], "MOCK_OLLAMA_FALLBACK_SUCCESS")

    def test_5_timeout_failover(self):
        """When OmniRoute hangs, client timeout catches it and fails over to Ollama."""
        MockOmniHandler.mode = "TIMEOUT"
        res = self.dispatcher.dispatch("Check status")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res["fallback_used"])
        self.assertEqual(res["provider"], "OLLAMA_LOCAL_FALLBACK")
        self.assertEqual(res["content"], "MOCK_OLLAMA_FALLBACK_SUCCESS")

    def test_6_provider_down_502_failover(self):
        """When OmniRoute returns 502 Bad Gateway, fails over to Ollama."""
        MockOmniHandler.mode = "502"
        res = self.dispatcher.dispatch("Check status")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res["fallback_used"])
        self.assertEqual(res["provider"], "OLLAMA_LOCAL_FALLBACK")
        self.assertEqual(res["content"], "MOCK_OLLAMA_FALLBACK_SUCCESS")

    def test_7_omniroute_completely_down_failover(self):
        """When OmniRoute port is unreachable, catches NET_ERROR and fails over to Ollama."""
        dead_dispatcher = ResilientOmniDispatcher(
            omniroute_url="http://127.0.0.1:59999/v1",  # Unused port
            ollama_url=f"http://127.0.0.1:{self.ollama_port}/v1",
            timeout_sec=0.1
        )
        res = dead_dispatcher.dispatch("Check status")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res["fallback_used"])
        self.assertEqual(res["provider"], "OLLAMA_LOCAL_FALLBACK")
        self.assertEqual(res["content"], "MOCK_OLLAMA_FALLBACK_SUCCESS")


if __name__ == "__main__":
    unittest.main()
