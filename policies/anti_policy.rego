package anti.policy

import rego.v1

default allow = false
default decision = "DENY"
default require_approval = false

# 1. READ actions are always allowed
allow if input.action in ["READ", "read", "navigate", "extract", "search", "screenshot", "get_status"]

decision = "ALLOW" if allow

# 2. CODE_EDIT, TEST, LINT allowed strictly in isolated worktrees
allow if {
    input.action in ["CODE_EDIT", "code_edit", "TEST", "test", "lint"]
    startswith(input.work_dir, "/var/lib/anti-agents/worktrees")
}

# 3. COMMIT allowed strictly in isolated worktrees
allow if {
    input.action in ["COMMIT", "commit"]
    startswith(input.work_dir, "/var/lib/anti-agents/worktrees")
}

# 4. OPEN_PR requires passed tests and canonical eligibility
allow if {
    input.action in ["OPEN_PR", "open_pr", "create_pr"]
    input.tests_passed == true
    input.canonical_eligible == true
}

# 5. GITHUB COMMENT allowed only if not duplicate
allow if {
    input.action in ["COMMENT", "comment", "github.comment"]
    input.is_duplicate == false
}

# 6. LOGIN / FORM SUBMISSION require approval
require_approval if {
    input.action in ["LOGIN", "login", "submit_form", "change_account_settings"]
}

# 7. FINANCIAL, TRANSFER, DELETE strictly denied by default
deny_strict if {
    input.action in ["TRANSFER_CRYPTO", "transfer_funds", "purchase", "delete_account", "drop_database", "reveal_credentials"]
}

# 8. Control plane actions:
allow if {
    input.action in ["create_sandbox", "run_tests", "test"]
    startswith(input.target, "/var/lib/anti-agents/worktrees")
}

allow if {
    input.action in ["submit_job", "submit_task"]
    input.actor in ["OWNER", "ANTI", "antiworker", "agent-bounty-engineer", "agent-prd-browser"]
}

allow if {
    input.action == "restart_worker"
    input.target in ["supervisor", "sentinel", "anti-supervisor.service", "antigravity-bounty-sentinel.service"]
}

allow if input.actor == "OWNER"
