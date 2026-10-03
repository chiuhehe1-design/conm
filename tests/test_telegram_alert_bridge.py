#!/usr/bin/env python3
"""
Unit tests for Telegram Live Alert & Escalation Bridge (FEATURE-020).
Verifies payload serialization, message_thread_id routing, token resolution,
graceful error handling, and domain-specific notifications.
"""

import os
import sys
import json
import time
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.telegram_alert_bridge import (
    TelegramAlertBridge,
    resolve_telegram_bot_token,
    get_telegram_notifier
)


class TestTelegramAlertBridge(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.test_path = Path(self.test_dir.name)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_token_resolution_from_env(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123456:TEST_TOKEN_FROM_ENV"}):
            tok = resolve_telegram_bot_token()
            self.assertEqual(tok, "123456:TEST_TOKEN_FROM_ENV")

    def test_token_resolution_from_token_file(self):
        token_file = self.test_path / "telegram.token"
        token_file.write_text("654321:FILE_TOKEN\n")
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_TOKEN_FILE": str(token_file)}):
            with patch("shared.telegram_alert_bridge.TOKEN_FILE", token_file):
                tok = resolve_telegram_bot_token()
                self.assertEqual(tok, "654321:FILE_TOKEN")

    def test_token_resolution_from_revenue_env(self):
        revenue_env = self.test_path / "revenue.env"
        revenue_env.write_text('FOO=bar\nTELEGRAM_BOT_TOKEN="987654:REVENUE_ENV_TOKEN"\nBAZ=1\n')
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            with patch("shared.telegram_alert_bridge.TOKEN_FILE", self.test_path / "nonexistent.token"):
                with patch("shared.telegram_alert_bridge.REVENUE_OS_ENV", revenue_env):
                    tok = resolve_telegram_bot_token()
                    self.assertEqual(tok, "987654:REVENUE_ENV_TOKEN")

    def test_dispatch_skipped_without_token(self):
        outbox = self.test_path / "outbox.log"
        with patch("shared.telegram_alert_bridge.OUTBOX_LOG", outbox):
            bridge = TelegramAlertBridge(bot_token="", chat_id="-100999", thread_id=2)
            result = bridge._dispatch_raw("Test message without token")
            self.assertFalse(result)
            self.assertTrue(outbox.exists())
            log_content = outbox.read_text()
            self.assertIn("SKIPPED_NO_TOKEN", log_content)

    @patch("urllib.request.urlopen")
    def test_dispatch_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        outbox = self.test_path / "outbox.log"
        with patch("shared.telegram_alert_bridge.OUTBOX_LOG", outbox):
            bridge = TelegramAlertBridge(bot_token="test_token_123", chat_id="-1003879582297", thread_id=2)
            res = bridge._dispatch_raw("Hello Boardroom!")
            self.assertTrue(res)

            # Verify request
            args, kwargs = mock_urlopen.call_args
            req = args[0]
            self.assertIn("/bottest_token_123/sendMessage", req.full_url)
            body = json.loads(req.data.decode("utf-8"))
            self.assertEqual(body["chat_id"], "-1003879582297")
            self.assertEqual(body["message_thread_id"], 2)
            self.assertEqual(body["text"], "Hello Boardroom!")
            self.assertEqual(body["parse_mode"], "Markdown")

            # Outbox log
            self.assertTrue(outbox.exists())
            self.assertIn("SENT_OK", outbox.read_text())

    @patch("urllib.request.urlopen")
    def test_dispatch_network_error_fail_soft(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Connection timed out")
        outbox = self.test_path / "outbox.log"
        with patch("shared.telegram_alert_bridge.OUTBOX_LOG", outbox):
            bridge = TelegramAlertBridge(bot_token="test_token", chat_id="-1003879582297", thread_id=2)
            res = bridge._dispatch_raw("Should fail gracefully")
            self.assertFalse(res)
            self.assertTrue(outbox.exists())
            self.assertIn("FAILED: Connection timed out", outbox.read_text())

    @patch.object(TelegramAlertBridge, "_dispatch_raw")
    def test_async_dispatch(self, mock_raw):
        bridge = TelegramAlertBridge(bot_token="test_token", chat_id="-100", thread_id=2)
        bridge.send_async("Async test message")
        # Allow async thread to run
        time.sleep(0.1)
        mock_raw.assert_called_once_with("Async test message")

    @patch.object(TelegramAlertBridge, "send_async")
    def test_domain_notification_methods(self, mock_send_async):
        bridge = TelegramAlertBridge(bot_token="test_token", chat_id="-100", thread_id=2)

        # 1. Bounty settlement
        bridge.notify_bounty_settlement(
            opp_id="opp-test-123",
            amount_usd=250.0,
            chain="Base-Mainnet",
            tx_hash="0xabcdef1234567890abcdef1234567890",
            roi_multiple=4.2
        )
        self.assertEqual(mock_send_async.call_count, 1)
        msg1 = mock_send_async.call_args[0][0]
        self.assertIn("ON-CHAIN BOUNTY SETTLED", msg1)
        self.assertIn("250.00 USDC", msg1)
        self.assertIn("Base-Mainnet", msg1)
        self.assertIn("4.2x", msg1)

        # 2. Hourly upgrade cycle
        bridge.notify_hourly_upgrade_cycle(
            cycle_id="hourly-001",
            status="SUCCESS",
            tests_passed=True,
            duration_sec=7.89,
            rfcs_promoted=3,
            avg_worker_score=0.98,
            cert_hash="a1b2c3d4e5f6071829"
        )
        self.assertEqual(mock_send_async.call_count, 2)
        msg2 = mock_send_async.call_args[0][0]
        self.assertIn("HOURLY EVAL & UPGRADE CYCLE", msg2)
        self.assertIn("PASS (120/120 Tests)", msg2)
        self.assertIn("0.98 / 1.00", msg2)
        self.assertIn("7.89s", msg2)

        # 3. P0 incident
        bridge.notify_p0_incident(
            incident_id="INC-999",
            component="WorkerOptimizer",
            error_detail="Segmentation fault in optimizer cgroup",
            action_taken="Worker restarted in isolated namespace"
        )
        self.assertEqual(mock_send_async.call_count, 3)
        msg3 = mock_send_async.call_args[0][0]
        self.assertIn("P0 INCIDENT ESCALATION", msg3)
        self.assertIn("INC-999", msg3)
        self.assertIn("WorkerOptimizer", msg3)

        # 4. DR drill
        bridge.notify_dr_drill(
            drill_id="DR-007",
            status="HEALTHY",
            duration_ms=45.2,
            cert_hash="fedcba987654321"
        )
        self.assertEqual(mock_send_async.call_count, 4)
        msg4 = mock_send_async.call_args[0][0]
        self.assertIn("DISASTER RECOVERY DRILL COMPLETE", msg4)
        self.assertIn("DR-007", msg4)

    def test_singleton_get_telegram_notifier(self):
        n1 = get_telegram_notifier()
        n2 = get_telegram_notifier()
        self.assertIs(n1, n2)


if __name__ == "__main__":
    unittest.main()
