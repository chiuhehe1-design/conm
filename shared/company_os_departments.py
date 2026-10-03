#!/usr/bin/env python3
"""
COMPANY OS: AUTONOMOUS 14-DEPARTMENT COORDINATION ENGINE (P3-01 / P3-17)
Orchestrates AI departments into a closed operational loop under ANTI central command.

14 Canonical Departments:
1. REVENUE:      Opportunity ingestion, bounty pipeline, market discovery
2. PRODUCT:      RFCs, specifications, roadmap prioritization, feature gates
3. ENGINEERING:  Code development, bug fixing, automated tests, PR submissions
4. RESEARCH:     Technical audits, paper analysis, model benchmarks, market intel
5. MARKETING:    Announcements, community distribution, growth loops
6. SALES:        Direct client acquisition, service negotiation, deal closing
7. SUPPORT:      Customer inquiry triage, incident responses, FAQ generation
8. FINANCE:      Base USDC reconciliation, treasury ledger, compute cost & ROI
9. SECURITY:     Secret lifecycle, OPA policy audit, break-glass quarantine
10. SRE:         Automated self-healing, QoS traffic control, uptime & reboot drills
11. DATA:        Telemetry aggregation, analytics classification, performance metrics
12. PROCUREMENT: API credit management, proxy allocation, cloud compute sizing
13. HR:          Workforce lifecycle, WorkerScore scheduling, retraining triggers
14. LEGAL:       Open-source license compliance, terms of service validation

ANTI acts as CEO/Chief Orchestrator:
- Translates high-level Owner goals into cross-department dependency graphs.
- Dispatches inter-departmental tasks with SLAs and verifiable acceptance criteria.
"""

import os
import sys
import time
import json
import uuid
import sqlite3
import logging
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("COMPANY_OS")

DEFAULT_COMPANY_DB = os.environ.get("ANTI_COMPANY_DB", str(REPO_ROOT / "data" / "company_os.db"))


class DepartmentType(str, Enum):
    REVENUE = "revenue"
    PRODUCT = "product"
    ENGINEERING = "engineering"
    RESEARCH = "research"
    MARKETING = "marketing"
    SALES = "sales"
    SUPPORT = "support"
    FINANCE = "finance"
    SECURITY = "security"
    SRE = "sre"
    DATA = "data"
    PROCUREMENT = "procurement"
    HR = "hr"
    LEGAL = "legal"


class DeptTaskStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


@dataclass
class DepartmentTask:
    task_id: str
    from_dept: DepartmentType
    to_dept: DepartmentType
    title: str
    description: str
    payload: Dict[str, Any]
    priority: int = 2  # 1 (Highest) to 5 (Lowest)
    sla_seconds: float = 300.0
    status: DeptTaskStatus = DeptTaskStatus.SUBMITTED
    assigned_worker_id: Optional[str] = None
    result_manifest: Optional[Dict[str, Any]] = None
    error_message: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


class CompanyOSOrchestrator:
    """
    Central cross-department operational bus and coordination engine.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_COMPANY_DB
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS department_tasks (
                task_id TEXT PRIMARY KEY,
                from_dept TEXT NOT NULL,
                to_dept TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                payload TEXT NOT NULL,
                priority INTEGER NOT NULL,
                sla_seconds REAL NOT NULL,
                status TEXT NOT NULL,
                assigned_worker_id TEXT,
                result_manifest TEXT,
                error_message TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            );
            """)

    def submit_task(
        self,
        from_dept: DepartmentType,
        to_dept: DepartmentType,
        title: str,
        description: str,
        payload: Optional[Dict[str, Any]] = None,
        priority: int = 2,
        sla_seconds: float = 300.0
    ) -> DepartmentTask:
        task_id = f"dept-{to_dept.value[:3]}-{uuid.uuid4().hex[:8]}"
        task = DepartmentTask(
            task_id=task_id,
            from_dept=from_dept,
            to_dept=to_dept,
            title=title,
            description=description,
            payload=payload or {},
            priority=priority,
            sla_seconds=sla_seconds,
            status=DeptTaskStatus.SUBMITTED,
            created_at=time.time(),
            updated_at=time.time()
        )
        self._save_task(task)
        logger.info(f"Department task created: [{from_dept.value} -> {to_dept.value}] {title} (ID: {task_id})")
        return task

    def _save_task(self, task: DepartmentTask):
        with self.conn:
            self.conn.execute("""
            INSERT OR REPLACE INTO department_tasks
            (task_id, from_dept, to_dept, title, description, payload, priority,
             sla_seconds, status, assigned_worker_id, result_manifest, error_message,
             created_at, updated_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task.task_id, task.from_dept.value, task.to_dept.value, task.title,
                task.description, json.dumps(task.payload), task.priority,
                task.sla_seconds, task.status.value, task.assigned_worker_id,
                json.dumps(task.result_manifest) if task.result_manifest else None,
                task.error_message, task.created_at, task.updated_at, task.completed_at
            ))

    def get_task(self, task_id: str) -> Optional[DepartmentTask]:
        cur = self.conn.cursor()
        cur.execute("""
        SELECT task_id, from_dept, to_dept, title, description, payload, priority,
               sla_seconds, status, assigned_worker_id, result_manifest, error_message,
               created_at, updated_at, completed_at
        FROM department_tasks WHERE task_id = ?
        """, (task_id,))
        row = cur.fetchone()
        if not row:
            return None
        return DepartmentTask(
            task_id=row[0],
            from_dept=DepartmentType(row[1]),
            to_dept=DepartmentType(row[2]),
            title=row[3],
            description=row[4],
            payload=json.loads(row[5]),
            priority=row[6],
            sla_seconds=row[7],
            status=DeptTaskStatus(row[8]),
            assigned_worker_id=row[9],
            result_manifest=json.loads(row[10]) if row[10] else None,
            error_message=row[11],
            created_at=row[12],
            updated_at=row[13],
            completed_at=row[14]
        )

    def claim_task(self, to_dept: DepartmentType, worker_id: str) -> Optional[DepartmentTask]:
        """
        Pulls highest priority SUBMITTED task for a department.
        """
        with self.conn:
            cur = self.conn.cursor()
            cur.execute("""
            SELECT task_id FROM department_tasks
            WHERE to_dept = ? AND status = ?
            ORDER BY priority ASC, created_at ASC LIMIT 1
            """, (to_dept.value, DeptTaskStatus.SUBMITTED.value))
            row = cur.fetchone()
            if not row:
                return None
            task_id = row[0]
            now = time.time()
            cur.execute("""
            UPDATE department_tasks
            SET status = ?, assigned_worker_id = ?, updated_at = ?
            WHERE task_id = ?
            """, (DeptTaskStatus.IN_PROGRESS.value, worker_id, now, task_id))

        return self.get_task(task_id)

    def complete_task(
        self,
        task_id: str,
        result_manifest: Dict[str, Any]
    ) -> bool:
        task = self.get_task(task_id)
        if not task:
            return False
        now = time.time()
        task.status = DeptTaskStatus.COMPLETED
        task.result_manifest = result_manifest
        task.completed_at = now
        task.updated_at = now
        self._save_task(task)
        logger.info(f"Department task COMPLETED: {task_id} [{task.to_dept.value}]")
        return True

    def fail_task(
        self,
        task_id: str,
        error_message: str
    ) -> bool:
        task = self.get_task(task_id)
        if not task:
            return False
        now = time.time()
        task.status = DeptTaskStatus.FAILED
        task.error_message = error_message
        task.updated_at = now
        self._save_task(task)
        logger.warning(f"Department task FAILED: {task_id} [{task.to_dept.value}]: {error_message}")
        return True

    def get_backlog(self, dept: DepartmentType) -> List[DepartmentTask]:
        cur = self.conn.cursor()
        cur.execute("""
        SELECT task_id FROM department_tasks
        WHERE to_dept = ? AND status IN (?, ?)
        ORDER BY priority ASC, created_at ASC
        """, (dept.value, DeptTaskStatus.SUBMITTED.value, DeptTaskStatus.IN_PROGRESS.value))
        return [self.get_task(r[0]) for r in cur.fetchall() if r[0]]

    def get_department_metrics(self) -> Dict[str, Any]:
        """
        Summarizes workload and completion performance across all 14 departments.
        """
        cur = self.conn.cursor()
        cur.execute("""
        SELECT to_dept, status, COUNT(*)
        FROM department_tasks
        GROUP BY to_dept, status
        """)
        metrics: Dict[str, Dict[str, int]] = {d.value: {"total": 0, "completed": 0, "in_progress": 0, "failed": 0} for d in DepartmentType}
        for dept, status, count in cur.fetchall():
            if dept in metrics:
                metrics[dept]["total"] += count
                if status == DeptTaskStatus.COMPLETED.value:
                    metrics[dept]["completed"] += count
                elif status == DeptTaskStatus.IN_PROGRESS.value:
                    metrics[dept]["in_progress"] += count
                elif status == DeptTaskStatus.FAILED.value:
                    metrics[dept]["failed"] += count
        return metrics


if __name__ == "__main__":
    cos = CompanyOSOrchestrator(":memory:")
    t1 = cos.submit_task(
        from_dept=DepartmentType.REVENUE,
        to_dept=DepartmentType.ENGINEERING,
        title="Fix Bounty #104 Tenstorrent Bug",
        description="Fix matrix multiplication assertion error",
        payload={"bounty_id": "b-104", "reward_usd": 750.0}
    )
    print("Submitted:", t1.task_id)
    claimed = cos.claim_task(DepartmentType.ENGINEERING, "worker-eng-01")
    print("Claimed:", claimed.task_id, "by", claimed.assigned_worker_id)
    cos.complete_task(t1.task_id, {"pr_url": "https://github.com/org/repo/pull/1", "tests_passed": True})
    print("Metrics:", cos.get_department_metrics())
