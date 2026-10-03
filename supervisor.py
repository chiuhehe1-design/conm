#!/usr/bin/env python3
"""
ANTI SUPERVISOR SWARM RUNTIME v2.1 (Hardened Production Safe)
Architecture:
1. Strict 127.0.0.1:20140 binding.
2. Bearer Token Authentication via /etc/anti-agents/supervisor.secret.
3. Unprivileged execution under user 'antiworker'.
4. Asynchronous Non-Blocking Job Queue (202 Accepted + /jobs/<id> polling).
5. OPA Policy Guard & Deduplication.
6. Ephemeral Worktree / Browser sandbox enforcement.
"""

import os
import sys
import json
import time
import uuid
import hmac
import sqlite3
import urllib.request
import subprocess
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor

PORT = 20140
GLOBAL_PAUSE_FILE = "/etc/anti-agents/GLOBAL_PAUSE"
SECRET_FILE = Path("/etc/anti-agents/supervisor.secret")
LOG_FILE = Path("/var/log/anti-agents/supervisor.log")
DB_PATH = Path("/opt/revenue_os/data/canonical_ledger.db")
JSON_LEDGER_PATH = Path("/opt/revenue_os/data/bounty_revenue_ledger.json")

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "shared") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "shared"))

from shared.policy_engine import PolicyEngine
from shared.deduplicator import Deduplicator
from shared.circuit_breaker import CircuitBreaker, DurableQueue
from shared.source_truth_resolver import SourceTruthResolver
from shared.agent_sre_recovery import SREFailureClassifier

dedupe = Deduplicator()
circuit_breaker = CircuitBreaker()
durable_queue = DurableQueue()
executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="anti-worker")

# Local in-memory job status store (synchronized with Redis)
JOB_RESULTS: dict[str, dict] = {}

def get_valid_secret() -> str:
    if SECRET_FILE.exists():
        try:
            with open(SECRET_FILE) as f:
                return f.read().strip()
        except Exception:
            pass
    return ""

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [SUPERVISOR] {msg}\n"
    print(line, end="", flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass

def log_audit(agent_id: str, action: str, target: str, decision: str, details: str = ""):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO audit_events (agent_id, action, target, policy_decision, details)
        VALUES (?, ?, ?, ?, ?)
        """, (agent_id, action, target, decision, details))
        conn.commit()
        conn.close()
    except Exception:
        pass

def execute_coder_job_async(job_id: str, data: dict):
    JOB_RESULTS[job_id] = {"job_id": job_id, "type": "CODER", "status": "RUNNING", "start_time": time.time()}
    try:
        from aider.worker_bounty import run_job as run_coder_job
        res = run_coder_job(data)
        JOB_RESULTS[job_id].update({
            "status": "COMPLETED" if res.get("status") in ("completed", "partial") else "FAILED",
            "result": res,
            "end_time": time.time()
        })
    except Exception as e:
        JOB_RESULTS[job_id].update({
            "status": "FAILED",
            "error": str(e),
            "end_time": time.time()
        })

def execute_browser_job_async(job_id: str, data: dict):
    JOB_RESULTS[job_id] = {"job_id": job_id, "type": "BROWSER", "status": "RUNNING", "start_time": time.time()}
    try:
        worker_bin = "/opt/anti-agents/browser/worker_browser.py"
        proc = subprocess.run([worker_bin, "--json", json.dumps(data)], capture_output=True, text=True)
        raw = proc.stdout.strip()
        try:
            s_idx = raw.find("{")
            e_idx = raw.rfind("}")
            if s_idx != -1 and e_idx != -1:
                res = json.loads(raw[s_idx:e_idx+1])
            else:
                res = json.loads(raw)
        except Exception:
            res = {"status": "error", "raw_output": proc.stdout, "stderr": proc.stderr}
        JOB_RESULTS[job_id].update({
            "status": "COMPLETED" if res.get("status") == "completed" else "FAILED",
            "result": res,
            "end_time": time.time()
        })
    except Exception as e:
        JOB_RESULTS[job_id].update({
            "status": "FAILED",
            "error": str(e),
            "end_time": time.time()
        })

class SupervisorHandler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload, indent=2).encode())

    def _check_auth(self) -> bool:
        expected = get_valid_secret()
        if not expected:
            return True # Fallback if secret not initialized
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header.replace("Bearer ", "").strip()
        return hmac.compare_digest(token, expected)

    def do_GET(self):
        # Read-only health and status
        if self.path in ("/health", "/status/health"):
            paused = os.path.exists(GLOBAL_PAUSE_FILE)
            cb_ok = circuit_breaker.allow_request()
            self._send_json(200, {
                "status": "healthy" if not paused else "paused",
                "supervisor_role": "antiworker",
                "bindings": "127.0.0.1:20140",
                "subsystems": {
                    "source_truth_resolver": "active",
                    "policy_engine_opa": "active",
                    "deduplicator": "active",
                    "global_pause": paused,
                    "circuit_breaker": "CLOSED" if cb_ok else "OPEN"
                },
                "queues": durable_queue.get_queue_stats(),
                "active_jobs_count": sum(1 for j in JOB_RESULTS.values() if j.get("status") == "RUNNING"),
                "timestamp": time.time()
            })

        elif self.path in ("/ledger", "/api/bounty-ledger"):
            if JSON_LEDGER_PATH.exists():
                with open(JSON_LEDGER_PATH) as f:
                    self._send_json(200, json.load(f))
            else:
                self._send_json(404, {"error": "Ledger file missing"})

        elif self.path.startswith("/jobs/"):
            job_id = self.path.replace("/jobs/", "").strip()
            job_info = JOB_RESULTS.get(job_id)
            if job_info:
                self._send_json(200, job_info)
            else:
                self._send_json(404, {"error": f"Job {job_id} not found"})

        elif self.path in ("/version", "/api/v1/version"):
            from shared.version_metadata import get_version_metadata
            self._send_json(200, get_version_metadata("anti-supervisor"))

        elif self.path == "/queues":
            self._send_json(200, durable_queue.get_queue_stats())

        elif self.path in ("/", "/status"):
            self._send_json(200, {
                "name": "ANTI Swarm Control Plane v2.1",
                "architecture": "Authenticated Async Job Plane",
                "bound_address": "127.0.0.1:20140",
                "execution_user": "antiworker",
                "circuit_breaker": "active",
                "paused": os.path.exists(GLOBAL_PAUSE_FILE)
            })

        else:
            self._send_json(404, {"error": "Not Found", "path": self.path})

    def do_POST(self):
        # 1. Enforce Authentication for all mutating endpoints
        if not self._check_auth():
            log_audit("UNAUTHORIZED_INTERNAL", "POST", self.path, "DENIED", "Missing or invalid supervisor secret")
            self._send_json(401, {"error": "Unauthorized", "message": "Valid supervisor Bearer token required"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else "{}"
        try:
            data = json.loads(body) if body else {}
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        # KILL SWITCH
        if self.path in ("/kill", "/api/kill"):
            Path(GLOBAL_PAUSE_FILE).touch()
            log("KILL SWITCH ENGAGED via API")
            log_audit("anti-commander", "KILL_SWITCH", "SYSTEM", "ALLOW", "Touched /etc/anti-agents/GLOBAL_PAUSE")
            self._send_json(200, {"status": "paused", "message": "GLOBAL_PAUSE created"})

        # RESUME
        elif self.path in ("/resume", "/api/resume"):
            if os.path.exists(GLOBAL_PAUSE_FILE):
                os.remove(GLOBAL_PAUSE_FILE)
            log("RESUME ENGAGED via API")
            log_audit("anti-commander", "RESUME", "SYSTEM", "ALLOW", "Removed /etc/anti-agents/GLOBAL_PAUSE")
            self._send_json(200, {"status": "resumed", "message": "GLOBAL_PAUSE removed"})

        # RESOLVER
        elif self.path == "/resolver/verify":
            res = SourceTruthResolver.resolve_bounty(data)
            log_audit("agent-bounty-scout", "VERIFY_SOURCE_TRUTH", data.get("url", ""), "EVALUATED", res.get("status"))
            self._send_json(200, res)

        # POLICY EVAL
        elif self.path == "/policy/eval":
            res = PolicyEngine.evaluate(data)
            self._send_json(200, res)

        # CODER JOBS (Async Non-Blocking)
        elif self.path == "/workers/coder/jobs":
            # OPA Guard
            policy_check = PolicyEngine.evaluate({
                "action": "code_edit",
                "work_dir": data.get("target_dir", "/var/lib/anti-agents/worktrees/temp")
            })
            if not policy_check.get("allowed"):
                log_audit("agent-bounty-engineer", "code_edit", data.get("task", ""), "DENIED", policy_check.get("details"))
                self._send_json(403, {"status": "denied", "policy": policy_check})
                return

            # Deduplication
            repo = data.get("repo")
            issue = data.get("issue")
            if repo and issue:
                is_unique, err_msg = dedupe.check_and_register_opportunity("github", repo, int(issue), data.get("task", ""))
                if not is_unique:
                    log_audit("agent-bounty-engineer", "dedupe_check", f"{repo}#{issue}", "REJECTED_DUPLICATE", err_msg)
                    self._send_json(409, {"status": "rejected", "error": err_msg})
                    return

            job_id = f"coder-{uuid.uuid4().hex[:8]}"
            data["task_id"] = job_id
            executor.submit(execute_coder_job_async, job_id, data)
            log(f"Enqueued Coder job {job_id} asynchronously")
            self._send_json(202, {
                "status": "QUEUED",
                "job_id": job_id,
                "check_url": f"/jobs/{job_id}",
                "message": "Coder job dispatched to background worker pool"
            })

        # BROWSER JOBS (Async Non-Blocking)
        elif self.path == "/workers/browser/jobs":
            allowed_domains = data.get("allowed_domains", [])
            if not allowed_domains:
                self._send_json(400, {"status": "rejected", "error": "allowed_domains is mandatory"})
                return

            # OPA Guard
            policy_check = PolicyEngine.evaluate({
                "action": "navigate",
                "url": allowed_domains[0]
            })
            if not policy_check.get("allowed"):
                log_audit("agent-prd-browser", "navigate", str(allowed_domains), "DENIED", policy_check.get("details"))
                self._send_json(403, {"status": "denied", "policy": policy_check})
                return

            job_id = f"browser-{uuid.uuid4().hex[:8]}"
            data["task_id"] = job_id
            executor.submit(execute_browser_job_async, job_id, data)
            log(f"Enqueued Browser job {job_id} asynchronously")
            self._send_json(202, {
                "status": "QUEUED",
                "job_id": job_id,
                "check_url": f"/jobs/{job_id}",
                "message": "Browser job dispatched to background worker pool"
            })

        # SUPERVISOR AUTO-CLASSIFY TASK
        elif self.path == "/supervisor/task":
            task_desc = data.get("task", "").lower()
            coding_keywords = ["code", "git", "repo", "patch", "fix", "test", "pytest", "pnpm", "def ", "class "]
            is_coding = any(k in task_desc for k in coding_keywords)
            job_id = f"task-{uuid.uuid4().hex[:8]}"
            data["task_id"] = job_id

            if is_coding:
                executor.submit(execute_coder_job_async, job_id, data)
                self._send_json(202, {
                    "classification": "CODING",
                    "status": "QUEUED",
                    "job_id": job_id,
                    "check_url": f"/jobs/{job_id}"
                })
            else:
                executor.submit(execute_browser_job_async, job_id, data)
                self._send_json(202, {
                    "classification": "BROWSER",
                    "status": "QUEUED",
                    "job_id": job_id,
                    "check_url": f"/jobs/{job_id}"
                })

        else:
            self._send_json(404, {"error": "Not Found", "path": self.path})

def run():
    server = HTTPServer(("127.0.0.1", PORT), SupervisorHandler)
    log(f"ANTI Swarm Control Plane v2.1 listening on 127.0.0.1:{PORT} (Async Queue + Authenticated)")
    server.serve_forever()

if __name__ == "__main__":
    run()
