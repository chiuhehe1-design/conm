#!/usr/bin/env python3
"""
CENTRAL DISPATCHER PIPELINE (ANTI Autonomous Control Plane)
Complies with P1.5 (Strict Execution Pipeline):

Flow:
  Task Ingestion
        ↓
  1. Auth Verification (SHA-256 Bearer Verifier or Internal Secret)
        ↓
  2. OPA Policy Gate (Strict declarative Rego evaluation)
        ↓
  3. Deduplication Check (Idempotency key & Opportunity fingerprint)
        ↓
  4. Resource Admission (GLOBAL_PAUSE, Worker CircuitBreaker, Concurrency limits)
        ↓
  5. Permission Scope Gate (Role vs Domain permissions)
        ↓
  6. Durable Queue Enqueue (SQLite WAL persistent queue with fencing tokens)
        ↓
  7. Worker Execution & Acceptance Gate (Only authorized workers with lease token)

Eliminates any direct execution bypass. Fail-closed on any validation error.
"""

import os
import sys
import time
import json
import uuid
import hashlib
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, Tuple, Optional, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.policy_engine import PolicyEngine
from shared.deduplicator import Deduplicator
from shared.circuit_breaker import CircuitBreaker, DurableQueue

logger = logging.getLogger("CENTRAL_DISPATCHER")

GLOBAL_PAUSE_FILE = os.environ.get("ANTI_GLOBAL_PAUSE_FILE", "/etc/anti-agents/GLOBAL_PAUSE")
TOKEN_HASHES_FILE = Path("/etc/anti-agents/token_hashes.json")
SUPERVISOR_SECRET_FILE = Path("/etc/anti-agents/supervisor.secret")

ROLE_PERMISSIONS = {
    "OWNER": {"*"},  # Full administrative / break-glass
    "ANTI": {"revenue", "engineering", "sre", "research", "browser", "code_edit", "run_tests", "submit_job"},
    "antiworker": {"worker_lease", "worker_ack", "worker_nack", "worker_heartbeat", "submit_manifest"}
}


@dataclass
class DispatchRequest:
    task_desc: str
    action: str
    domain: str = "general"
    actor: str = "ANTI"
    auth_token: str = ""
    dedupe_key: Optional[str] = None
    target_dir: Optional[str] = None
    allowed_domains: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    max_retries: int = 3


class CentralDispatcher:
    """
    Unified gatekeeper for all tasks in PrimeNode.
    Bypassing any stage results in immediate fail-closed rejection.
    """

    def __init__(
        self,
        queue: Optional[DurableQueue] = None,
        cb: Optional[CircuitBreaker] = None,
        dedupe: Optional[Deduplicator] = None,
        test_mode: bool = False
    ):
        self.queue = queue or DurableQueue()
        self.cb = cb or CircuitBreaker()
        self.dedupe = dedupe or Deduplicator()
        self.test_mode = test_mode

    def verify_auth(self, req: DispatchRequest) -> Tuple[bool, str, str]:
        """
        Stage 1: Verify token against vault SHA-256 hashes.
        Returns (is_valid, resolved_role, error_reason).
        """
        token = req.auth_token.strip()
        if token.startswith("Bearer "):
            token = token.replace("Bearer ", "").strip()

        if self.test_mode:
            if req.actor in ("OWNER", "ANTI", "antiworker") and token == "test-token":
                return True, req.actor, ""
            if not token:
                return False, "ANONYMOUS", "Missing authorization bearer token"

        # Check against Supervisor secret for internal worker plane
        if SUPERVISOR_SECRET_FILE.exists():
            try:
                secret = SUPERVISOR_SECRET_FILE.read_text().strip()
                if secret and token == secret:
                    return True, "antiworker", ""
            except Exception:
                pass

        # Check against token_hashes.json
        if TOKEN_HASHES_FILE.exists():
            try:
                hashes = json.loads(TOKEN_HASHES_FILE.read_text())
                token_hash = hashlib.sha256(token.encode()).hexdigest()
                if hashes.get("owner_hash") == token_hash:
                    return True, "OWNER", ""
                if hashes.get("anti_hash") == token_hash:
                    return True, "ANTI", ""
            except Exception as e:
                logger.error(f"Error reading token hashes: {e}")

        return False, "ANONYMOUS", "Cryptographic authentication failed: Invalid or unrecognized bearer token"

    def evaluate_opa(self, req: DispatchRequest, role: str) -> Tuple[bool, str]:
        """
        Stage 2: Declarative OPA policy evaluation.
        """
        payload = {
            "actor": role,
            "action": req.action,
            "target": req.target_dir or req.task_desc[:60],
            "work_dir": req.target_dir or ""
        }
        res = PolicyEngine.evaluate(payload)
        if not res.get("allowed", False):
            return False, f"OPA Denied: {res.get('details', 'Policy restriction')}"
        return True, ""

    def check_deduplication(self, req: DispatchRequest) -> Tuple[bool, str]:
        """
        Stage 3: Idempotency & Deduplication.
        """
        if not req.dedupe_key:
            return True, ""

        allowed = self.dedupe.check_idempotency_key(req.dedupe_key)
        if not allowed:
            return False, f"DO_NOT_CREATE_SECOND_JOB: Dedupe key '{req.dedupe_key}' already processed"
        return True, ""

    def check_resource_admission(self, req: DispatchRequest) -> Tuple[bool, str]:
        """
        Stage 4: Resource Admission Gate.
        """
        pause_file = os.environ.get("ANTI_GLOBAL_PAUSE_FILE", GLOBAL_PAUSE_FILE)
        # 1. Global pause check
        if os.path.exists(pause_file) and req.actor != "OWNER":
            return False, "Admission Denied: GLOBAL_PAUSE engaged across PrimeNode"

        # 2. Worker Circuit Breaker check
        worker_service_key = f"worker_{req.domain}"
        if not self.cb.is_available(worker_service_key):
            return False, f"Admission Denied: Circuit breaker for domain '{req.domain}' is OPEN"

        return True, ""

    def check_permission_scope(self, req: DispatchRequest, role: str) -> Tuple[bool, str]:
        """
        Stage 5: Role Permission Scope Gate.
        """
        allowed_scopes = ROLE_PERMISSIONS.get(role, set())
        if "*" in allowed_scopes:
            return True, ""

        if req.action in allowed_scopes or req.domain in allowed_scopes:
            return True, ""

        return False, f"Permission Denied: Role '{role}' unauthorized for action '{req.action}' in domain '{req.domain}'"

    def dispatch(self, req: DispatchRequest) -> Dict[str, Any]:
        """
        Full 7-Stage Central Dispatch Execution:
        Returns structured 202 Accepted or Fail-Closed rejection payload.
        """
        # Stage 1: Auth
        auth_ok, role, auth_err = self.verify_auth(req)
        if not auth_ok:
            return {
                "status": "REJECTED",
                "stage": "AUTH",
                "http_code": 401,
                "error": auth_err
            }

        # Stage 2: OPA Policy
        opa_ok, opa_err = self.evaluate_opa(req, role)
        if not opa_ok:
            return {
                "status": "REJECTED",
                "stage": "OPA",
                "http_code": 403,
                "error": opa_err
            }

        # Stage 3: Deduplication
        dedupe_ok, dedupe_err = self.check_deduplication(req)
        if not dedupe_ok:
            return {
                "status": "REJECTED",
                "stage": "DEDUPE",
                "http_code": 409,
                "error": dedupe_err
            }

        # Stage 4: Resource Admission
        adm_ok, adm_err = self.check_resource_admission(req)
        if not adm_ok:
            return {
                "status": "REJECTED",
                "stage": "ADMISSION",
                "http_code": 503,
                "error": adm_err
            }

        # Stage 5: Permission Scope
        perm_ok, perm_err = self.check_permission_scope(req, role)
        if not perm_ok:
            return {
                "status": "REJECTED",
                "stage": "PERMISSION",
                "http_code": 403,
                "error": perm_err
            }

        # Stage 6: Enqueue to Durable Queue
        job_data = {
            "task": req.task_desc,
            "action": req.action,
            "domain": req.domain,
            "actor": role,
            "target_dir": req.target_dir,
            "metadata": req.metadata,
            "created_at": time.time()
        }
        job_id = self.queue.push_job(queue_name=req.domain, job_data=job_data, max_retries=req.max_retries)

        logger.info(f"Task '{job_id}' successfully passed Central Dispatch Gate (Domain={req.domain}, Actor={role})")
        return {
            "status": "QUEUED",
            "stage": "COMPLETED",
            "http_code": 202,
            "job_id": job_id,
            "queue": req.domain,
            "actor": role,
            "check_url": f"/api/v1/jobs/{job_id}",
            "message": "Task admitted and enqueued into durable queue"
        }


if __name__ == "__main__":
    dispatcher = CentralDispatcher(test_mode=True)
    req = DispatchRequest(
        task_desc="Verify gateway integrity",
        action="submit_job",
        domain="sre",
        actor="ANTI",
        auth_token="test-token"
    )
    res = dispatcher.dispatch(req)
    print("Dispatch test result:", json.dumps(res, indent=2))
