#!/usr/bin/env python3
"""
POLICY ENGINE (OPA Integration)
Evaluates any incoming agent action against declarative Rego rules.
Separates policy enforcement from agent code.
"""

import os
import sys
import json
import subprocess
from pathlib import Path
from typing import Dict, Any, Tuple

POLICY_PATH = Path("/etc/anti-agents/policies/anti_policy.rego")
OPA_BIN = "/usr/local/bin/opa"

class PolicyEngine:
    POLICY_PATH = Path(os.environ.get("ANTI_POLICY_PATH", "/etc/anti-agents/policies/anti_policy.rego"))
    if not POLICY_PATH.exists():
        fallback_path = Path(__file__).resolve().parent.parent / "policies" / "anti_policy.rego"
        if fallback_path.exists():
            POLICY_PATH = fallback_path

    OPA_BIN = os.environ.get("ANTI_OPA_BIN", "/usr/local/bin/opa")

    @classmethod
    def _evaluate_local(cls, action_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Declarative Python fallback matching anti_policy.rego rules 1:1."""
        raw_act = str(action_payload.get("action", ""))
        action = raw_act.lower()
        actor = str(action_payload.get("actor", ""))
        work_dir = str(action_payload.get("work_dir", ""))
        target = str(action_payload.get("target", ""))

        if actor == "OWNER":
            return {"allow": True, "decision": "ALLOW"}

        forbidden = {"transfer_crypto", "transfer_funds", "purchase", "delete_account", "drop_database", "reveal_credentials"}
        if action in forbidden or raw_act in ["TRANSFER_CRYPTO", "transfer_funds", "drop_database"]:
            return {"allow": False, "decision": "DENY", "deny_strict": True, "details": f"Strictly forbidden: {raw_act}"}

        if action in ["login", "submit_form", "change_account_settings"]:
            return {"allow": False, "decision": "REQUIRE_APPROVAL", "require_approval": True, "details": "Requires manual approval"}

        if action in ["read", "navigate", "extract", "search", "screenshot", "get_status"]:
            return {"allow": True, "decision": "ALLOW"}

        if action in ["code_edit", "test", "lint", "commit", "create_sandbox", "run_tests"]:
            if work_dir.startswith("/var/lib/anti-agents/worktrees") or target.startswith("/var/lib/anti-agents/worktrees"):
                return {"allow": True, "decision": "ALLOW"}
            return {"allow": False, "decision": "DENY", "details": "Path not in /var/lib/anti-agents/worktrees"}

        if action in ["open_pr", "create_pr"]:
            if action_payload.get("tests_passed") is True and action_payload.get("canonical_eligible") is True:
                return {"allow": True, "decision": "ALLOW"}
            return {"allow": False, "decision": "DENY", "details": "Prerequisites not met for OPEN_PR"}

        if action in ["comment", "github.comment"]:
            if action_payload.get("is_duplicate") is False:
                return {"allow": True, "decision": "ALLOW"}
            return {"allow": False, "decision": "DENY", "details": "Duplicate comment rejected"}

        if action in ["submit_job", "submit_task"]:
            if actor in ["OWNER", "ANTI", "antiworker", "agent-bounty-engineer", "agent-prd-browser"]:
                return {"allow": True, "decision": "ALLOW"}
            return {"allow": False, "decision": "DENY", "details": f"Unauthorized actor '{actor}' for submit_job"}

        if action == "restart_worker":
            if target in ["supervisor", "sentinel", "anti-supervisor.service", "antigravity-bounty-sentinel.service"]:
                return {"allow": True, "decision": "ALLOW"}
            return {"allow": False, "decision": "DENY", "details": f"Service '{target}' not whitelisted"}

        return {"allow": False, "decision": "DENY", "details": "Default deny"}

    @classmethod
    def evaluate(cls, action_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluates input against anti.policy
        Returns {
            allowed: bool,
            decision: ALLOW | DENY | REQUIRE_APPROVAL,
            action: action_payload.get(action),
            details: str
        }
        """
        # If OPA binary not present, use faithful declarative Python fallback
        if not cls.POLICY_PATH.exists() or not os.path.exists(cls.OPA_BIN):
            local_res = cls._evaluate_local(action_payload)
            return {
                "allowed": local_res.get("allow", False),
                "decision": local_res.get("decision", "DENY"),
                "action": action_payload.get("action"),
                "require_approval": local_res.get("require_approval", False),
                "deny_strict": local_res.get("deny_strict", False),
                "details": local_res.get("details", f"Policy decision: {local_res.get('decision')}")
            }

        cmd = [
            cls.OPA_BIN, "eval",
            "-d", str(cls.POLICY_PATH),
            "-I",  # Read input from stdin
            "data.anti.policy"
        ]

        try:
            proc = subprocess.run(cmd, input=json.dumps(action_payload), capture_output=True, text=True, timeout=5)
            if proc.returncode != 0:
                return {"allowed": False, "decision": "DENY", "details": f"OPA eval error: {proc.stderr}"}

            raw = json.loads(proc.stdout)
            result = raw.get("result", [{}])[0].get("expressions", [{}])[0].get("value", {})

            allow = bool(result.get("allow", False))
            require_approval = bool(result.get("require_approval", False))
            deny_strict = bool(result.get("deny_strict", False))

            if deny_strict:
                decision = "DENY"
                allow = False
            elif require_approval:
                decision = "REQUIRE_APPROVAL"
                allow = False
            elif allow:
                decision = "ALLOW"
            else:
                decision = "DENY"

            return {
                "allowed": allow,
                "decision": decision,
                "action": action_payload.get("action"),
                "require_approval": require_approval,
                "deny_strict": deny_strict,
                "details": f"Policy decision: {decision}"
            }
        except Exception as e:
            return {"allowed": False, "decision": "DENY", "details": str(e)}

if __name__ == "__main__":
    # Test 1: READ action -> should ALLOW
    r1 = PolicyEngine.evaluate({"action": "navigate", "url": "https://example.com"})
    print("Test 1 (navigate):", r1)

    # Test 2: CODE_EDIT in /var/lib/anti-agents/worktrees/123 -> should ALLOW
    r2 = PolicyEngine.evaluate({"action": "code_edit", "work_dir": "/var/lib/anti-agents/worktrees/task-123"})
    print("Test 2 (code_edit in worktree):", r2)

    # Test 3: CODE_EDIT in /root -> should DENY
    r3 = PolicyEngine.evaluate({"action": "code_edit", "work_dir": "/root/something"})
    print("Test 3 (code_edit in /root):", r3)

    # Test 4: TRANSFER_CRYPTO -> should DENY
    r4 = PolicyEngine.evaluate({"action": "TRANSFER_CRYPTO", "amount": 100})
    print("Test 4 (transfer crypto):", r4)
