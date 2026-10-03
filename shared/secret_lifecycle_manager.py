#!/usr/bin/env python3
"""
SECRET & CREDENTIAL LIFECYCLE CONTROLLER (P1.12 & P1.13 / P1-06)
Standardizes:
Generate -> Store Verifier -> Load -> Rotate -> Revoke -> Audit

Security Principles:
1. Zero Plaintext on Disk: VPS stores strictly SHA-256 verifiers.
2. Custody Separation: Private keys and bearer tokens reside solely on Owner/Client device.
3. Atomic Rotation: Seamless transition with previous token revocation.
4. Comprehensive Audit Trail: Every credential state mutation recorded in audit ledger.
"""

import os
import sys
import json
import time
import secrets
import hashlib
import sqlite3
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Any, Tuple, Optional, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("SECRET_LIFECYCLE")

DEFAULT_VAULT_FILE = Path("/etc/anti-agents/token_hashes.json")


class SecretState:
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    ROTATING = "ROTATING"


class SecretLifecycleManager:
    """
    Cryptographic Secret Lifecycle & Rotation Controller for PrimeNode.
    """

    def __init__(self, vault_path: Optional[Path] = None, audit_db_conn: Optional[sqlite3.Connection] = None):
        self.vault_path = vault_path or DEFAULT_VAULT_FILE
        self.audit_conn = audit_db_conn or sqlite3.connect(":memory:")
        self._init_audit_table()

    def _init_audit_table(self):
        with self.audit_conn:
            self.audit_conn.execute("""
            CREATE TABLE IF NOT EXISTS credential_audit_ledger (
                credential_id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                revoked_at REAL,
                details TEXT
            );
            """)

    def generate_bearer_credential(self, role: str) -> Tuple[str, str]:
        """
        Generates a new cryptographic bearer token and its SHA-256 verifier.
        Returns: (plaintext_token, sha256_verifier)
        The plaintext token is given ONLY to the client/caller.
        The verifier is what gets recorded in the vault.
        """
        token = f"anti_{role.lower()}_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        return token, token_hash

    def load_vault(self) -> Dict[str, str]:
        if self.vault_path.exists():
            try:
                return json.loads(self.vault_path.read_text())
            except Exception:
                return {}
        return {}

    def rotate_role_credential(self, role: str) -> Tuple[str, str]:
        """
        Rotates credential for role ('owner' or 'anti'):
        1. Generates new bearer token & hash verifier.
        2. Revokes previous credential in audit ledger.
        3. Updates vault JSON atomically.
        4. Records new credential in audit ledger.
        Returns (new_plaintext_token, new_token_hash).
        """
        norm_role = role.lower()
        if norm_role not in ("owner", "anti"):
            raise ValueError(f"Invalid role: {role}. Allowed: 'owner', 'anti'")

        vault = self.load_vault()
        old_hash_key = f"{norm_role}_hash"
        old_hash = vault.get(old_hash_key)

        now = time.time()
        # Revoke old hash if present
        if old_hash:
            with self.audit_conn:
                self.audit_conn.execute("""
                UPDATE credential_audit_ledger
                SET state = 'REVOKED', revoked_at = ?
                WHERE role = ? AND token_hash = ? AND state = 'ACTIVE'
                """, (now, role.upper(), old_hash))

        # Generate new
        new_token, new_hash = self.generate_bearer_credential(role)
        vault[old_hash_key] = new_hash

        # Atomic file write
        if self.vault_path.parent.exists() or not self.vault_path.is_absolute():
            os.makedirs(self.vault_path.parent, exist_ok=True)
            temp_file = self.vault_path.with_suffix(".tmp")
            temp_file.write_text(json.dumps(vault, indent=2))
            os.replace(temp_file, self.vault_path)

        # Audit new
        cred_id = f"cred_{role.lower()}_{new_hash[:8]}"
        with self.audit_conn:
            self.audit_conn.execute("""
            INSERT INTO credential_audit_ledger
            (credential_id, role, token_hash, state, created_at, details)
            VALUES (?, ?, ?, 'ACTIVE', ?, 'Rotated via SecretLifecycleManager')
            """, (cred_id, role.upper(), new_hash, now))

        logger.info(f"Credential for role '{role}' successfully rotated (ID={cred_id}, Hash={new_hash[:12]}...)")
        return new_token, new_hash

    def revoke_role_credential(self, role: str) -> bool:
        """Immediately revokes role credential, removing it from active vault."""
        norm_role = role.lower()
        vault = self.load_vault()
        hash_key = f"{norm_role}_hash"
        old_hash = vault.pop(hash_key, None)

        if not old_hash:
            return False

        now = time.time()
        if self.vault_path.exists():
            temp_file = self.vault_path.with_suffix(".tmp")
            temp_file.write_text(json.dumps(vault, indent=2))
            os.replace(temp_file, self.vault_path)

        with self.audit_conn:
            self.audit_conn.execute("""
            UPDATE credential_audit_ledger
            SET state = 'REVOKED', revoked_at = ?
            WHERE role = ? AND token_hash = ?
            """, (now, role.upper(), old_hash))

        logger.warning(f"Credential for role '{role}' REVOKED immediately!")
        return True

    def get_audit_summary(self) -> List[Dict[str, Any]]:
        with self.audit_conn:
            cur = self.audit_conn.cursor()
            cur.execute("SELECT credential_id, role, token_hash, state, created_at, revoked_at FROM credential_audit_ledger ORDER BY created_at DESC")
            rows = cur.fetchall()
            return [
                {
                    "credential_id": r[0],
                    "role": r[1],
                    "token_hash": r[2],
                    "state": r[3],
                    "created_at": r[4],
                    "revoked_at": r[5]
                }
                for r in rows
            ]


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "token_hashes.json"
        mgr = SecretLifecycleManager(vault_path=vault)
        t, h = mgr.rotate_role_credential("ANTI")
        print("Generated token:", t[:16] + "...")
        print("Vault content:", vault.read_text())
        print("Audit entries:", mgr.get_audit_summary())
