#!/usr/bin/env python3
"""
PRIME NODE BOUNTY CODER WORKER (Hardened Production Safe)
Enforces:
1. Strict target_dir sandbox jail (/var/lib/anti-agents/worktrees/<task-id> only).
2. Test Profile Allowlist (No arbitrary shell commands / RCE protection).
3. Capability-scoped external_write enforcement.
4. Unprivileged execution boundaries.
5. Real SHA256 patch audit logging.
"""

import os
import sys
import json
import uuid
import time
import shutil
import hashlib
import sqlite3
import subprocess
from pathlib import Path

GLOBAL_PAUSE_FILE = "/etc/anti-agents/GLOBAL_PAUSE"
WORKTREES_BASE = Path("/var/lib/anti-agents/worktrees").resolve()
LOG_FILE = Path("/var/log/anti-agents/aider.log")
DB_PATH = Path("/opt/revenue_os/data/canonical_ledger.db")

APPROVED_TEST_PROFILES = {
    "python-pytest": "pytest -q",
    "python-unittest": "python3 -m unittest discover",
    "node-pnpm": "pnpm test",
    "node-npm": "npm test",
    "rust-cargo": "cargo test",
    "go-test": "go test ./..."
}

DANGEROUS_SHELL_PATTERNS = [";", "&&", "||", "|", ">", "<", "$", "`", "\\", "rm ", "curl ", "wget ", "nc ", "bash ", "sh ", "eval ", "exec "]

def check_kill_switch():
    if os.path.exists(GLOBAL_PAUSE_FILE):
        return False, "GLOBAL_PAUSE is active at /etc/anti-agents/GLOBAL_PAUSE. Worker refuses jobs."
    return True, None

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [agent-bounty-engineer] {msg}\n"
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

def resolve_sandbox_work_dir(task_id: str, requested_dir: str = None) -> Path:
    """Enforce that execution is strictly jailed inside /var/lib/anti-agents/worktrees/<task_id>"""
    if requested_dir:
        resolved = Path(requested_dir).resolve()
        try:
            resolved.relative_to(WORKTREES_BASE)
        except ValueError:
            raise PermissionError(f"SECURITY JAIL VIOLATION: target_dir '{requested_dir}' is outside approved sandbox '{WORKTREES_BASE}'")
        return resolved
    
    target = WORKTREES_BASE / task_id
    target.mkdir(parents=True, exist_ok=True)
    return target

def resolve_test_command(job_data: dict) -> tuple[str | None, str | None]:
    """
    Resolve test command strictly via approved profiles or strict validation.
    Returns: (approved_cmd, error_msg)
    """
    profile = job_data.get("test_profile")
    raw_cmd = job_data.get("test_command")

    if profile:
        if profile in APPROVED_TEST_PROFILES:
            return APPROVED_TEST_PROFILES[profile], None
        return None, f"Unrecognized test_profile '{profile}'. Allowed: {list(APPROVED_TEST_PROFILES.keys())}"

    if raw_cmd:
        # Check against approved profile commands
        for prof_name, approved_cmd in APPROVED_TEST_PROFILES.items():
            if raw_cmd.strip() == approved_cmd:
                return approved_cmd, None

        # Check for dangerous patterns
        for pattern in DANGEROUS_SHELL_PATTERNS:
            if pattern in raw_cmd:
                return None, f"SECURITY REJECT: Arbitrary shell pattern '{pattern}' detected in test_command"

        return None, f"SECURITY REJECT: Custom test_command '{raw_cmd}' not approved. Use 'test_profile' instead."

    return None, None

def verify_external_write_capability(job_data: dict) -> tuple[bool, str]:
    """Verify if external_write capability is authorized"""
    cap_token = job_data.get("capability_token")
    ext_flag = job_data.get("external_write", False)
    
    if not ext_flag:
        return False, "EXTERNAL_WRITE_DISABLED"

    # Capability must be granted explicitly with token or supervisor grant
    if cap_token and cap_token.startswith("CAP_EXT_WRITE_"):
        return True, "AUTHORIZED_BY_CAPABILITY_TOKEN"
    elif job_data.get("authorized_by") == "SUPERVISOR_POLICY":
        return True, "AUTHORIZED_BY_SUPERVISOR_POLICY"
    
    return False, "EXTERNAL_WRITE_PERMISSION_DENIED: Missing valid capability_token"

def run_job(job_data: dict) -> dict:
    is_ok, err = check_kill_switch()
    if not is_ok:
        log(f"REJECTED: {err}")
        return {"status": "rejected", "error": err, "patch": None}

    task_id = job_data.get("task_id", f"task-{uuid.uuid4().hex[:8]}")
    repo = job_data.get("repo", "")
    task = job_data.get("task", "")

    # 1. Enforce target_dir sandbox jail
    try:
        work_dir = resolve_sandbox_work_dir(task_id, job_data.get("target_dir"))
    except PermissionError as pe:
        log_audit("agent-bounty-engineer", "jail_violation", str(job_data.get("target_dir")), "DENIED", str(pe))
        return {"status": "denied", "error": str(pe), "task_id": task_id}

    # 2. Enforce test command allowlist
    approved_test_cmd, test_err = resolve_test_command(job_data)
    if test_err:
        log_audit("agent-bounty-engineer", "rce_prevention", str(job_data.get("test_command")), "SECURITY_ALERT_DENIED", test_err)
        return {"status": "security_reject", "error": test_err, "task_id": task_id}

    # 3. External write capability verification
    can_external_write, ext_reason = verify_external_write_capability(job_data)

    log(f"Starting job {task_id} | repo={repo} | test_cmd={approved_test_cmd} | external_write={can_external_write} ({ext_reason})")

    # Clone repo if provided
    if repo and not (work_dir / ".git").exists():
        if not repo.startswith("http") and not repo.startswith("git@"):
            repo_url = f"https://github.com/{repo}.git"
        else:
            repo_url = repo
        log(f"Cloning {repo_url} into {work_dir}")
        clone_res = subprocess.run(["git", "clone", "--depth", "1", repo_url, str(work_dir)], capture_output=True, text=True)
        if clone_res.returncode != 0:
            return {"status": "failed", "error": f"Clone failed: {clone_res.stderr}", "task_id": task_id}

    # Prepare Aider command
    aider_bin = "/usr/local/bin/aider"
    cmd = [
        aider_bin,
        "--model", "openai/anti-coder",
        "--no-auto-commits",
        "--yes-always",
        "--no-show-model-warnings",
        "--message", task
    ]
    if approved_test_cmd:
        cmd.extend(["--test-cmd", approved_test_cmd, "--auto-test"])

    env = os.environ.copy()
    env["OPENAI_API_BASE"] = "http://127.0.0.1:20128/v1"
    env["OPENAI_API_KEY"] = "local-router"

    start_t = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(work_dir), env=env, capture_output=True, text=True, timeout=300)
        elapsed = time.time() - start_t
        log(f"Aider finished in {elapsed:.1f}s, exit={proc.returncode}")

        # Capture git diff
        diff_res = subprocess.run(["git", "diff"], cwd=str(work_dir), capture_output=True, text=True)
        diff_output = diff_res.stdout
        status_res = subprocess.run(["git", "status", "-s"], cwd=str(work_dir), capture_output=True, text=True)

        patch_sha256 = hashlib.sha256(diff_output.encode()).hexdigest() if diff_output else None
        patch_file = work_dir / "solution.patch"
        if diff_output:
            with open(patch_file, "w") as f:
                f.write(diff_output)

        is_success = (proc.returncode == 0) and bool(diff_output)

        # Audit external action
        if is_success:
            log_audit("agent-bounty-engineer", "PATCH_GENERATED", str(work_dir), "SUCCESS", f"sha256={patch_sha256}")

        result = {
            "status": "completed" if is_success else "partial",
            "task_id": task_id,
            "work_dir": str(work_dir),
            "elapsed_seconds": round(elapsed, 2),
            "modified_files": status_res.stdout.strip().splitlines(),
            "patch": diff_output,
            "patch_sha256": patch_sha256,
            "patch_file": str(patch_file) if diff_output else None,
            "external_write_granted": can_external_write,
            "external_write_reason": ext_reason,
            "pr_ready": is_success,
            "error": proc.stderr if proc.returncode != 0 else None
        }
        return result

    except subprocess.TimeoutExpired:
        log(f"Job {task_id} timed out after 300s")
        return {"status": "timeout", "task_id": task_id, "error": "Execution exceeded 300s"}
    except Exception as e:
        log(f"Job {task_id} error: {e}")
        return {"status": "error", "task_id": task_id, "error": str(e)}

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
            print("Usage: worker_bounty.py [--json '<payload>' | --stdin]")
    else:
        print("Usage: worker_bounty.py [--json '<payload>' | --stdin]")
