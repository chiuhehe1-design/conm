#!/usr/bin/env python3
"""
Table-Driven OPA & Security Matrix Test for ANTI Control Gateway
"""
import sys
import os
import json
import urllib.request
import urllib.error
from pathlib import Path

OWNER_TOKEN_FILE = Path(__file__).parent / "owner_break_glass.token"
ANTI_TOKEN_FILE = Path(__file__).parent / "anti_control.token"

OWNER_TOKEN = OWNER_TOKEN_FILE.read_text().strip() if OWNER_TOKEN_FILE.exists() else ""
ANTI_TOKEN = ANTI_TOKEN_FILE.read_text().strip() if ANTI_TOKEN_FILE.exists() else ""

HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1:20146")

def call(endpoint, method="GET", data=None, token=None):
    url = f"http://{HOST}{endpoint}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, headers=headers, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = resp.status
            try:
                payload = json.loads(resp.read().decode())
            except Exception:
                payload = None
            return code, payload
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = None
        return e.code, payload
    except Exception as e:
        return 0, {"error": str(e)}

def run_tests():
    print(f"[*] Running OPA & Gateway Security Matrix against {HOST}...")
    results = []

    def check(name, got_code, exp_code, condition=True):
        passed = (got_code == exp_code) and condition
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name:<45} Got: {got_code} (Exp: {exp_code})")
        results.append(passed)

    # 1. Health check (unauthenticated)
    code, res = call("/api/v1/health")
    check("Health check (no auth)", code, 200, res.get("status") == "healthy")

    # 2. Diagnostics without token
    code, res = call("/api/v1/owner/diagnostics")
    check("Diagnostics without auth", code, 401)

    # 3. Diagnostics with bogus token
    code, res = call("/api/v1/owner/diagnostics", token="bad-token-xyz")
    check("Diagnostics with invalid token", code, 401)

    # 4. Diagnostics with ANTI token (should be 403 Forbidden - Owner required)
    code, res = call("/api/v1/owner/diagnostics", token=ANTI_TOKEN)
    check("Diagnostics with ANTI token", code, 403)

    # 5. Diagnostics with OWNER token (should be 200 OK)
    code, res = call("/api/v1/owner/diagnostics", token=OWNER_TOKEN)
    check("Diagnostics with OWNER token", code, 200, res.get("role") == "OWNER")

    # 6. Killswitch with ANTI token (should be 403 Forbidden)
    code, res = call("/api/v1/owner/killswitch", method="POST", token=ANTI_TOKEN)
    check("Killswitch with ANTI token", code, 403)

    # 7. Recover with ANTI token (should be 403 Forbidden)
    code, res = call("/api/v1/owner/recover", method="POST", token=ANTI_TOKEN)
    check("Recover with ANTI token", code, 403)

    # 8. Recover with OWNER token (should be 200 OK)
    code, res = call("/api/v1/owner/recover", method="POST", token=OWNER_TOKEN)
    check("Recover with OWNER token", code, 200, res.get("status") == "recovered")

    # 9. Pause with ANTI token (should be 200 OK)
    code, res = call("/api/v1/system/pause", method="POST", token=ANTI_TOKEN)
    check("System pause with ANTI token", code, 200, res.get("status") == "paused")

    # 10. Resume with ANTI token (should be 200 OK)
    code, res = call("/api/v1/system/resume", method="POST", token=ANTI_TOKEN)
    check("System resume with ANTI token", code, 200, res.get("status") == "resumed")

    # 11. Create sandbox with Path Traversal
    code, res = call("/api/v1/sandbox/create", method="POST", data={"task_id": "../../etc"}, token=ANTI_TOKEN)
    check("Sandbox creation path traversal rejection", code, 400) # Regex rejects ../../etc

    code, res = call("/api/v1/sandbox/create", method="POST", data={"task_id": "test_matrix_sbx_01"}, token=ANTI_TOKEN)
    check("Sandbox creation valid path", code, 200, res.get("status") == "created")
    sbx_path = res.get("sandbox_path", "")

    # 12. Run tests with arbitrary command (RCE attempt)
    code, res = call("/api/v1/sandbox/test", method="POST", data={"sandbox_path": sbx_path, "command": "rm -rf /"}, token=ANTI_TOKEN)
    check("Run tests arbitrary shell attempt rejection", code, 400, "SECURITY REJECT" in res.get("error", ""))

    # 13. Run tests with path traversal sandbox
    code, res = call("/api/v1/sandbox/test", method="POST", data={"sandbox_path": "/etc", "test_profile": "python-pytest"}, token=ANTI_TOKEN)
    check("Run tests outside worktree rejection", code, 403)

    # 14. Run tests with approved profile in valid sandbox
    code, res = call("/api/v1/sandbox/test", method="POST", data={"sandbox_path": sbx_path, "test_profile": "python-unittest"}, token=ANTI_TOKEN)
    check("Run tests approved profile", code, 200, res.get("test_profile") == "python-unittest")

    # 15. Restart worker unauthorized service
    code, res = call("/api/v1/workers/restart", method="POST", data={"worker": "ssh"}, token=ANTI_TOKEN)
    check("Restart non-whitelisted service rejection", code, 403)

    # 16. Restart worker whitelisted supervisor
    code, res = call("/api/v1/workers/restart", method="POST", data={"worker": "anti-supervisor.service"}, token=ANTI_TOKEN)
    check("Restart whitelisted supervisor", code, 200, res.get("status") == "restarted")

    # 17. Forbidden dangerous action
    code, res = call("/api/v1/transfer_funds", method="POST", data={"amount": 100}, token=ANTI_TOKEN)
    check("Forbidden financial action block", code, 403, res.get("policy") == "DENY")

    # 18. Canary deploy stub
    code, res = call("/api/v1/canary/deploy", method="POST", token=OWNER_TOKEN)
    check("Canary deploy stub rejection", code, 501, res.get("status") == "NOT_IMPLEMENTED")

    print(f"\n[Summary] {sum(results)} / {len(results)} tests passed.")
    if all(results):
        print(">>> ALL OPA AND SECURITY MATRIX TESTS PASSED! <<<")
        return 0
    else:
        print(">>> SOME TESTS FAILED! <<<")
        return 1

if __name__ == "__main__":
    sys.exit(run_tests())
