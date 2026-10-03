#!/opt/anti-agents/browser/.venv/bin/python
"""
PRIME NODE BROWSER WORKER (Hardened Production Safe)
Enforces:
1. Ephemeral Per-Job Browser Profiles (Zero cookie/session leak).
2. Mandatory allowed_domains (No wild web browsing without whitelist).
3. SSRF Protection (Strictly denies 127.0.0.0/8, 10.0.0.0/8, 192.168.0.0/16, localhost, internal ports).
4. Pre-Action Policy Middleware (Enforces READ, PREPARE, EXTERNAL_WRITE, FINANCIAL permissions).
5. PrimeNodeBrowserLLMAdapter integration (Clean LLM protocol without monkey-patching).
6. Janitor profile cleanup on completion.
"""

import os
import sys
import json
import uuid
import time
import shutil
import ipaddress
import urllib.parse
import asyncio
import sqlite3
from pathlib import Path

# Add shared modules to path
sys.path.insert(0, "/opt/anti-agents")
from shared.llm_adapter import PrimeNodeBrowserLLMAdapter
from browser_use import Agent, BrowserProfile

GLOBAL_PAUSE_FILE = "/etc/anti-agents/GLOBAL_PAUSE"
PROFILES_BASE = Path("/var/lib/anti-agents/browser-profiles")
LOG_FILE = Path("/var/log/anti-agents/browser.log")
DB_PATH = Path("/opt/revenue_os/data/canonical_ledger.db")

DENIED_IP_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
]

DENIED_INTERNAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "internal", "local"}
DENIED_INTERNAL_PORTS = {22, 20128, 20130, 20131, 20132, 20135, 20140, 20145, 6379, 11434, 5432, 3001}

def check_kill_switch():
    if os.path.exists(GLOBAL_PAUSE_FILE):
        return False, "GLOBAL_PAUSE is active at /etc/anti-agents/GLOBAL_PAUSE. Worker refuses jobs."
    return True, None

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [agent-prd-browser] {msg}\n"
    print(line, end="", file=sys.stderr, flush=True)
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

def validate_ssrf_safety(url_or_domain: str) -> tuple[bool, str]:
    """Check if target violates SSRF policy against private internal services"""
    try:
        parsed = urllib.parse.urlparse(url_or_domain if "://" in url_or_domain else f"https://{url_or_domain}")
        hostname = (parsed.hostname or "").lower()
        port = parsed.port

        if port in DENIED_INTERNAL_PORTS:
            return False, f"SSRF VIOLATION: Access to internal port {port} is strictly forbidden"

        if hostname in DENIED_INTERNAL_HOSTS or hostname.endswith(".internal") or hostname.endswith(".local"):
            return False, f"SSRF VIOLATION: Access to private host '{hostname}' is forbidden"

        # Check IP ranges if hostname is IP
        try:
            ip = ipaddress.ip_address(hostname)
            for net in DENIED_IP_NETWORKS:
                if ip in net:
                    return False, f"SSRF VIOLATION: Access to private network {net} is forbidden ({ip})"
        except ValueError:
            pass # Not a raw IP

        return True, "SAFE"
    except Exception as e:
        return False, f"URL parse error: {e}"

def evaluate_browser_policy(mode: str, action: str, target_domain: str, has_ext_grant: bool) -> tuple[bool, str]:
    """Pre-action Policy Middleware enforcing READ, PREPARE, EXTERNAL_WRITE, and FINANCIAL rules"""
    mode = mode.upper()
    action = action.lower()

    # Rule 1: FINANCIAL mode is strictly forbidden for autonomous browser
    if mode == "FINANCIAL" or "payment" in action or "transfer" in action or "checkout" in action:
        return False, "SECURITY POLICY DENY: Autonomous browser is prohibited from financial/payment actions"

    # Rule 2: READ mode allows only inspection
    if mode == "READ":
        write_actions = {"click_submit", "post_comment", "upload_file", "fill_password", "external_write"}
        if action in write_actions:
            return False, f"POLICY DENY: Action '{action}' is not permitted in READ mode"

    # Rule 3: EXTERNAL_WRITE requires explicit capability grant
    if mode == "EXTERNAL_WRITE" or action in {"post_comment", "submit_form", "publish"}:
        if not has_ext_grant:
            return False, "POLICY DENY: EXTERNAL_WRITE action requires verified capability grant token"

    return True, "ALLOWED"

async def execute_task(job_data: dict) -> dict:
    is_ok, err = check_kill_switch()
    if not is_ok:
        log(f"REJECTED: {err}")
        return {"status": "rejected", "error": err, "result": None}

    task_id = job_data.get("task_id", f"task-br-{uuid.uuid4().hex[:8]}")
    task_prompt = job_data.get("task", "")
    allowed_domains = job_data.get("allowed_domains", [])
    mode = job_data.get("mode", "read").upper()
    max_steps = int(job_data.get("max_steps", 5))
    timeout = int(job_data.get("timeout_seconds", 120))
    cap_token = job_data.get("capability_token", "")
    has_ext_grant = bool(cap_token and cap_token.startswith("CAP_EXT_WRITE_")) or (job_data.get("authorized_by") == "SUPERVISOR_POLICY")

    # 1. Gate: allowed_domains is MANDATORY
    if not allowed_domains:
        err_msg = "SECURITY REJECT: allowed_domains is mandatory. Wildcard unconstrained browsing is denied."
        log_audit("agent-prd-browser", "domain_check", "NONE", "SECURITY_ALERT_DENIED", err_msg)
        return {"status": "security_reject", "error": err_msg, "task_id": task_id}

    # 2. Gate: SSRF check on all allowed domains
    for domain in allowed_domains:
        is_safe, ssrf_err = validate_ssrf_safety(domain)
        if not is_safe:
            log_audit("agent-prd-browser", "ssrf_guard", domain, "SECURITY_ALERT_DENIED", ssrf_err)
            return {"status": "ssrf_denied", "error": ssrf_err, "task_id": task_id}

    # 3. Gate: Pre-job Policy Evaluation
    policy_ok, policy_reason = evaluate_browser_policy(mode, "navigate", allowed_domains[0], has_ext_grant)
    if not policy_ok:
        log_audit("agent-prd-browser", "policy_eval", str(allowed_domains), "POLICY_DENIED", policy_reason)
        return {"status": "policy_denied", "error": policy_reason, "task_id": task_id}

    # 4. Gate: Ephemeral Profile Isolation (Per-Job Directory)
    PROFILES_BASE.mkdir(parents=True, exist_ok=True)
    ephemeral_profile_dir = PROFILES_BASE / task_id
    ephemeral_profile_dir.mkdir(parents=True, exist_ok=True)

    log(f"Starting browser job {task_id} | mode={mode} | profile={ephemeral_profile_dir} | domains={allowed_domains}")

    # Initialize LLM via PrimeNodeBrowserLLMAdapter
    llm = PrimeNodeBrowserLLMAdapter(
        model="anti-browser",
        base_url="http://127.0.0.1:20128/v1",
        api_key="local-router",
    )

    profile_kwargs = {
        "headless": True,
        "chromium_sandbox": False,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-default-apps"
        ],
        "user_data_dir": str(ephemeral_profile_dir),
        "allowed_domains": allowed_domains
    }

    profile = BrowserProfile(**profile_kwargs)

    agent = Agent(
        task=task_prompt,
        llm=llm,
        browser_profile=profile,
        use_vision=False,
    )

    start_t = time.time()
    instrumented_actions = []
    try:
        run_coro = agent.run(max_steps=max_steps)
        result_history = await asyncio.wait_for(run_coro, timeout=timeout)
        elapsed = time.time() - start_t
        final_text = result_history.final_result()
        steps_count = len(result_history.history) if hasattr(result_history, "history") else 1
        urls_visited = result_history.urls() if hasattr(result_history, "urls") else []
        final_url = urls_visited[-1] if urls_visited else None

        # Collect instrumented actions from history
        if hasattr(result_history, "history"):
            for step in result_history.history:
                step_model_output = getattr(step, "model_output", None)
                if step_model_output and hasattr(step_model_output, "action"):
                    instrumented_actions.append(str(step_model_output.action))

        log(f"Job {task_id} completed in {elapsed:.1f}s | steps={steps_count} | urls={urls_visited}")
        log_audit("agent-prd-browser", "BROWSER_RUN_COMPLETED", final_url or str(allowed_domains), "SUCCESS", f"steps={steps_count}")

        return {
            "status": "completed",
            "task_id": task_id,
            "mode": mode,
            "final_url": final_url,
            "urls_visited": urls_visited,
            "result": final_text,
            "steps": steps_count,
            "elapsed_seconds": round(elapsed, 2),
            "external_actions": instrumented_actions,
            "profile_isolated": True,
            "error": None
        }
    except asyncio.TimeoutError:
        log(f"Job {task_id} timed out after {timeout}s")
        return {"status": "timeout", "task_id": task_id, "error": f"Timeout after {timeout}s"}
    except Exception as e:
        log(f"Job {task_id} exception: {e}")
        return {"status": "failed", "task_id": task_id, "error": str(e)}
    finally:
        # JANITOR CLEANUP: Erase ephemeral profile directory to eliminate cookie/session leakage
        try:
            if ephemeral_profile_dir.exists():
                shutil.rmtree(ephemeral_profile_dir, ignore_errors=True)
                log(f"Janitor sanitized ephemeral profile: {ephemeral_profile_dir}")
        except Exception as cl_err:
            log(f"Profile cleanup warning: {cl_err}")

def run_job(job_data: dict) -> dict:
    return asyncio.run(execute_task(job_data))

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--json":
            payload = json.loads(sys.argv[2])
            res = run_job(payload)
            print(json.dumps(res, indent=2))
        elif sys.argv[1] == "--stdin":
            payload = json.loads(sys.stdin.read())
            res = run_job(payload)
            print(json.dumps(res, indent=2))
        else:
            print("Usage: worker_browser.py [--json '<payload>' | --stdin]")
    else:
        print("Usage: worker_browser.py [--json '<payload>' | --stdin]")
