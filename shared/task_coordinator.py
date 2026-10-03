#!/usr/bin/env python3
"""
ANTI TASK COORDINATOR & CLOSED-LOOP ORCHESTRATION ENGINE
Core Production-Grade Implementation for PrimeNode.

Canonical Lifecycle States:
  WAITING  -> Upstream DAG dependencies not yet completed.
  QUEUED   -> Dependencies met, queued for worker claim/lease.
  RUNNING  -> Leased by a worker with active heartbeat & fencing token.
  DONE     -> Verified by Acceptance Gate and finalized.
  FAILED   -> Retry budget exhausted or unrecoverable failure.
  BLOCKED  -> Upstream prerequisite failed or administrative hold.

Features:
1. Deduplication & Idempotency: dedupe_key prevents redundant task generation.
2. Fencing Token Protection: Prevents split-brain completions from lagging/stale workers.
3. Acceptance Gate: Formal acceptance verification before transitioning to DONE.
4. Auto Next-Task Dispatch: Evaluates DAG on every completion, auto-queuing ready tasks.
5. Jev Gate 1 Integration: Automatically assigns optimal intelligence tier.
"""

import time
import json
import uuid
import sqlite3
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Set, Any, Tuple


class TaskStatus(str, Enum):
    WAITING = "WAITING"    # Waiting for prerequisite dependencies
    QUEUED = "QUEUED"      # All prerequisites completed; ready for dispatch
    RUNNING = "RUNNING"    # Leased by an active worker
    DONE = "DONE"          # Verified by acceptance gate and finished
    FAILED = "FAILED"      # Retry budget exhausted or fatal error
    BLOCKED = "BLOCKED"    # Upstream prerequisite failed


class TaskDomain(str, Enum):
    REVENUE = "REVENUE"
    GATEWAY = "GATEWAY"
    SRE = "SRE"
    ENGINEERING = "ENGINEERING"
    RESEARCH = "RESEARCH"
    WORKER = "WORKER"


class ModelTier(str, Enum):
    FAST = "FAST"
    BALANCED = "BALANCED"
    CODE_HEAVY = "CODE_HEAVY"
    STRONG_REASONING = "STRONG_REASONING"


@dataclass
class CompletionManifest:
    task_id: str
    worker_id: str
    lease_token: str
    status: str  # "SUCCESS" | "FAILURE"
    artifacts: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None


@dataclass
class Task:
    task_id: str
    title: str
    description: str
    domain: TaskDomain
    depends_on: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.WAITING
    dedupe_key: Optional[str] = None
    assigned_worker: Optional[str] = None
    lease_token: Optional[str] = None
    jev_tier: ModelTier = ModelTier.BALANCED
    retry_count: int = 0
    max_retries: int = 3
    lease_timeout_sec: float = 60.0
    heartbeat_at: Optional[float] = None
    acceptance_criteria: Dict[str, Any] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["domain"] = self.domain.value
        d["jev_tier"] = self.jev_tier.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        d = dict(data)
        d["status"] = TaskStatus(d["status"])
        d["domain"] = TaskDomain(d["domain"])
        d["jev_tier"] = ModelTier(d.get("jev_tier", "BALANCED"))
        return cls(**d)


class TaskRegistry:
    """Persistent SQLite & memory task catalog with atomic transactions and deduplication."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else None
        self._tasks: Dict[str, Task] = {}
        self._dedupe_index: Dict[str, str] = {} # dedupe_key -> task_id
        if self.db_path:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
            self._load_from_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                domain TEXT NOT NULL,
                status TEXT NOT NULL,
                dedupe_key TEXT UNIQUE,
                assigned_worker TEXT,
                lease_token TEXT,
                jev_tier TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                lease_timeout_sec REAL DEFAULT 60.0,
                heartbeat_at REAL,
                created_at REAL NOT NULL,
                started_at REAL,
                completed_at REAL,
                data_json TEXT NOT NULL
            )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_domain ON tasks(domain);")
            conn.commit()

    def _load_from_db(self):
        if not self.db_path or not self.db_path.exists():
            return
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT data_json FROM tasks")
            for (data_json,) in cur.fetchall():
                try:
                    t = Task.from_dict(json.loads(data_json))
                    self._tasks[t.task_id] = t
                    if t.dedupe_key:
                        self._dedupe_index[t.dedupe_key] = t.task_id
                except Exception:
                    pass

    def _save_task(self, task: Task):
        self._tasks[task.task_id] = task
        if task.dedupe_key:
            self._dedupe_index[task.dedupe_key] = task.task_id
        if self.db_path:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                INSERT OR REPLACE INTO tasks (
                    task_id, title, domain, status, dedupe_key, assigned_worker, lease_token,
                    jev_tier, retry_count, max_retries, lease_timeout_sec, heartbeat_at,
                    created_at, started_at, completed_at, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    task.task_id, task.title, task.domain.value, task.status.value,
                    task.dedupe_key, task.assigned_worker, task.lease_token,
                    task.jev_tier.value, task.retry_count, task.max_retries,
                    task.lease_timeout_sec, task.heartbeat_at, task.created_at,
                    task.started_at, task.completed_at, json.dumps(task.to_dict())
                ))
                conn.commit()

    def register_task(self, task: Task) -> Tuple[Task, bool]:
        """
        Registers a task. If dedupe_key already exists, returns (existing_task, False).
        Otherwise saves task and returns (task, True).
        """
        if task.dedupe_key and task.dedupe_key in self._dedupe_index:
            existing_id = self._dedupe_index[task.dedupe_key]
            existing_task = self._tasks.get(existing_id)
            if existing_task:
                return existing_task, False

        self._save_task(task)
        return task, True

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self, status: Optional[TaskStatus] = None, domain: Optional[TaskDomain] = None) -> List[Task]:
        res = list(self._tasks.values())
        if status:
            res = [t for t in res if t.status == status]
        if domain:
            res = [t for t in res if t.domain == domain]
        return res

    def record_heartbeat(self, task_id: str, worker_id: str, lease_token: str) -> bool:
        """Validates worker and fencing lease_token before extending heartbeat."""
        task = self.get_task(task_id)
        if not task or task.status != TaskStatus.RUNNING:
            return False
        if task.assigned_worker != worker_id or task.lease_token != lease_token:
            return False  # Fencing token mismatch (lease was already stolen/reclaimed)
        task.heartbeat_at = time.time()
        self._save_task(task)
        return True


class DependencyDAG:
    """Evaluates DAG dependencies and detects cycles via topological sorting."""

    @staticmethod
    def validate_no_cycles(tasks: List[Task]) -> bool:
        adj: Dict[str, Set[str]] = {t.task_id: set(t.depends_on) for t in tasks}
        visited: Set[str] = set()
        visiting: Set[str] = set()

        def dfs(node: str) -> bool:
            visiting.add(node)
            for dep in adj.get(node, set()):
                if dep in visiting:
                    return False
                if dep not in visited:
                    if not dfs(dep):
                        return False
            visiting.remove(node)
            visited.add(node)
            return True

        for tid in adj:
            if tid not in visited:
                if not dfs(tid):
                    return False
        return True

    @staticmethod
    def evaluate_readiness(registry: TaskRegistry) -> List[Task]:
        """
        Scans all WAITING tasks:
          - If all depends_on tasks are DONE -> transitions to QUEUED.
          - If any depends_on task is FAILED or BLOCKED -> transitions to BLOCKED.
        Returns newly QUEUED tasks.
        """
        newly_queued = []
        waiting_tasks = registry.list_tasks(status=TaskStatus.WAITING)

        for task in waiting_tasks:
            deps = task.depends_on
            if not deps:
                task.status = TaskStatus.QUEUED
                registry._save_task(task)
                newly_queued.append(task)
                continue

            all_done = True
            any_failed = False
            for dep_id in deps:
                dep_task = registry.get_task(dep_id)
                if not dep_task or dep_task.status != TaskStatus.DONE:
                    all_done = False
                if dep_task and dep_task.status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
                    any_failed = True

            if any_failed:
                task.status = TaskStatus.BLOCKED
                task.error_message = "Prerequisite upstream task failed or blocked"
                registry._save_task(task)
            elif all_done:
                task.status = TaskStatus.QUEUED
                registry._save_task(task)
                newly_queued.append(task)

        return newly_queued


class AcceptanceGate:
    """Verifies worker completion evidence before allowing transition to DONE."""

    @staticmethod
    def verify(task: Task, manifest: CompletionManifest) -> Tuple[bool, str]:
        if manifest.status != "SUCCESS":
            return False, f"Worker reported explicit failure: {manifest.error_message}"

        crit = task.acceptance_criteria or {}

        # 1. Required artifacts verification
        required_artifacts = crit.get("required_artifacts", [])
        for art in required_artifacts:
            if art not in manifest.artifacts:
                return False, f"Missing mandatory artifact: {art}"

        # 2. Minimum exit code / assertions
        expected_assertions = crit.get("assertions", {})
        for k, v in expected_assertions.items():
            actual = manifest.evidence.get(k)
            if actual != v:
                return False, f"Assertion failed: {k} expected {v}, got {actual}"

        return True, "All acceptance criteria verified successfully"


class JevClassifier:
    """Heuristic model tier evaluator (Gate 1 before model routing)."""

    @staticmethod
    def classify(task_description: str, retry_count: int = 0) -> Tuple[ModelTier, float, str]:
        desc_lower = task_description.lower()

        if retry_count >= 2:
            return ModelTier.STRONG_REASONING, 0.95, "Escalated to STRONG_REASONING due to retry budget depletion"

        reasoning_kw = ["architect", "root cause", "concurrency", "race condition", "security", "failover", "deadlock"]
        code_kw = ["implement", "refactor", "patch", "pytest", "unit test", "fix bug", "reconcile", "parser", "compile"]
        fast_kw = ["status", "check", "verify", "ping", "inspect", "list", "format", "heartbeat"]

        for kw in reasoning_kw:
            if kw in desc_lower:
                return ModelTier.STRONG_REASONING, 0.85, f"Keyword '{kw}' requires deep reasoning"
        for kw in code_kw:
            if kw in desc_lower:
                return ModelTier.CODE_HEAVY, 0.70, f"Keyword '{kw}' requires coding specialist"
        for kw in fast_kw:
            if kw in desc_lower:
                return ModelTier.FAST, 0.20, f"Keyword '{kw}' suitable for fast tier"

        return ModelTier.BALANCED, 0.50, "Default general balanced tier"


class ANTITaskCoordinator:
    """
    Central Autonomous Task Orchestrator for PrimeNode:
    - Ingests Goal Plans & Validates DAGs.
    - Manages Leases with Fencing Tokens.
    - Acceptance Gate Verification before DONE.
    - Auto Next-Task Dispatch loop.
    - Stale Lease Reaping & Retry Escalation.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.registry = TaskRegistry(db_path=db_path)
        self.dag = DependencyDAG()
        self.acceptance_gate = AcceptanceGate()
        self.jev = JevClassifier()

    def submit_goal_plan(self, plan_tasks: List[Task]) -> List[Task]:
        """Validates DAG, registers tasks idempotently, and triggers initial evaluation."""
        if not self.dag.validate_no_cycles(plan_tasks):
            raise ValueError("Dependency DAG contains circular references!")

        registered_tasks = []
        for t in plan_tasks:
            tier, score, reason = self.jev.classify(t.description, t.retry_count)
            t.jev_tier = tier
            task_obj, _ = self.registry.register_task(t)
            registered_tasks.append(task_obj)

        self.dag.evaluate_readiness(self.registry)
        return registered_tasks

    def get_queued_tasks(self, domain: Optional[TaskDomain] = None) -> List[Task]:
        """Returns all QUEUED tasks ready for worker execution."""
        self.dag.evaluate_readiness(self.registry)
        return self.registry.list_tasks(status=TaskStatus.QUEUED, domain=domain)

    def lease_task(self, task_id: str, worker_id: str) -> Optional[Tuple[Task, str]]:
        """
        Atomically leases a QUEUED task to a worker.
        Generates a unique fencing lease_token.
        Returns (Task, lease_token) or None.
        """
        task = self.registry.get_task(task_id)
        if not task or task.status != TaskStatus.QUEUED:
            return None

        lease_token = f"lease_{uuid.uuid4().hex[:12]}"
        task.assigned_worker = worker_id
        task.lease_token = lease_token
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        task.heartbeat_at = time.time()
        self.registry._save_task(task)
        return task, lease_token

    def heartbeat(self, task_id: str, worker_id: str, lease_token: str) -> bool:
        """Records keep-alive heartbeat with fencing token verification."""
        return self.registry.record_heartbeat(task_id, worker_id, lease_token)

    def submit_completion(self, manifest: CompletionManifest) -> Tuple[bool, str, List[Task]]:
        """
        Worker submits completion manifest.
        1. Verifies worker and lease fencing token (fencing protection).
        2. Validates against Acceptance Gate.
        3. If accepted -> DONE -> Evaluates DAG -> Returns newly QUEUED tasks.
        4. If rejected -> Triggers fail/retry.
        """
        task = self.registry.get_task(manifest.task_id)
        if not task:
            return False, "Task not found", []

        if task.status != TaskStatus.RUNNING:
            return False, f"Task status is {task.status.value}, expected RUNNING", []

        if task.assigned_worker != manifest.worker_id or task.lease_token != manifest.lease_token:
            return False, "Fencing token mismatch: lease expired or reassigned", []

        # Acceptance verification
        passed, reason = self.acceptance_gate.verify(task, manifest)
        if not passed:
            self.fail_task(task.task_id, f"Acceptance Gate Rejected: {reason}")
            return False, reason, []

        # Mark DONE
        task.status = TaskStatus.DONE
        task.completed_at = time.time()
        task.result = {
            "artifacts": manifest.artifacts,
            "metrics": manifest.metrics,
            "evidence": manifest.evidence
        }
        self.registry._save_task(task)

        # Auto Next-Task Dispatch
        newly_queued = self.dag.evaluate_readiness(self.registry)
        return True, "Accepted and finalized to DONE", newly_queued

    def fail_task(self, task_id: str, error_msg: str) -> Optional[Task]:
        """Handles task failure: retries with Jev escalation or transitions to FAILED."""
        task = self.registry.get_task(task_id)
        if not task:
            return None

        task.retry_count += 1
        task.error_message = error_msg

        if task.retry_count < task.max_retries:
            task.jev_tier, _, _ = self.jev.classify(task.description, task.retry_count)
            task.status = TaskStatus.QUEUED
            task.assigned_worker = None
            task.lease_token = None
            task.heartbeat_at = None
        else:
            task.status = TaskStatus.FAILED
            task.completed_at = time.time()

        self.registry._save_task(task)
        self.dag.evaluate_readiness(self.registry)
        return task

    def reap_stale_leases(self) -> List[Task]:
        """Finds running tasks that stopped emitting heartbeats and reclaims them."""
        now = time.time()
        reaped = []
        running = self.registry.list_tasks(status=TaskStatus.RUNNING)

        for t in running:
            last_hb = t.heartbeat_at or t.started_at or t.created_at
            if (now - last_hb) > t.lease_timeout_sec:
                self.fail_task(t.task_id, f"Heartbeat lease expired (>{t.lease_timeout_sec}s without check-in)")
                reaped.append(t)

        return reaped
