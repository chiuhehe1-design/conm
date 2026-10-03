#!/usr/bin/env python3
"""
Unit Tests for Secret Lifecycle Controller & Rotation (P1.12 & P1.13)
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.secret_lifecycle_manager import SecretLifecycleManager, SecretState


class TestSecretLifecycleManager(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault_file = Path(self.temp_dir.name) / "token_hashes.json"
        self.conn = sqlite3.connect(":memory:")
        self.mgr = SecretLifecycleManager(vault_path=self.vault_file, audit_db_conn=self.conn)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_rotate_generates_new_and_writes_hash_only(self):
        """Rotating credential generates new bearer token, writes SHA-256 verifier to vault, zero plaintext."""
        token, token_hash = self.mgr.rotate_role_credential("ANTI")
        self.assertTrue(token.startswith("anti_anti_"))
        self.assertEqual(len(token_hash), 64)

        # Verify file content on disk has ZERO plaintext
        disk_content = self.vault_file.read_text()
        self.assertNotIn(token, disk_content)
        vault_data = json.loads(disk_content)
        self.assertEqual(vault_data["anti_hash"], token_hash)

        # Audit ledger has ACTIVE entry
        entries = self.mgr.get_audit_summary()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["state"], "ACTIVE")
        self.assertEqual(entries[0]["role"], "ANTI")

    def test_consecutive_rotation_revokes_previous(self):
        """Second rotation marks first token as REVOKED in audit ledger."""
        t1, h1 = self.mgr.rotate_role_credential("OWNER")
        t2, h2 = self.mgr.rotate_role_credential("OWNER")

        self.assertNotEqual(t1, t2)
        self.assertNotEqual(h1, h2)

        # Vault only holds newest hash
        vault_data = json.loads(self.vault_file.read_text())
        self.assertEqual(vault_data["owner_hash"], h2)

        # Audit ledger shows h1 REVOKED, h2 ACTIVE
        entries = self.mgr.get_audit_summary()
        self.assertEqual(len(entries), 2)
        states = {e["token_hash"]: e["state"] for e in entries}
        self.assertEqual(states[h1], "REVOKED")
        self.assertEqual(states[h2], "ACTIVE")

    def test_revocation_removes_from_vault(self):
        """Revoking role credential removes hash from vault and records REVOKED."""
        t, h = self.mgr.rotate_role_credential("ANTI")
        ok = self.mgr.revoke_role_credential("ANTI")
        self.assertTrue(ok)

        # Vault no longer has anti_hash
        vault_data = json.loads(self.vault_file.read_text())
        self.assertNotIn("anti_hash", vault_data)

        # Audit ledger shows REVOKED
        entries = self.mgr.get_audit_summary()
        self.assertEqual(entries[0]["state"], "REVOKED")
        self.assertIsNotNone(entries[0]["revoked_at"])


if __name__ == "__main__":
    unittest.main()
