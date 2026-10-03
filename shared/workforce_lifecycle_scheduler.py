#!/usr/bin/env python3
"""
WORKFORCE LIFECYCLE & WORKERSCORE SCHEDULER ENGINE (P2-06 & P2-07)
Production-grade Workforce Management for PrimeNode:

1. Canonical 10-State Workforce Lifecycle:
   NEEDED -> PROVISION -> TRAIN -> SHADOW -> CANARY -> READY -> ASSIGNED -> WORKING -> REVIEW -> (PROMOTE / RETRAIN / PAUSE)

2. Multi-Factor WorkerScore Scheduler (Replaces naive round-robin):
   Evaluates:
     - SkillMatch: Task skill requirements vs worker skill capabilities (weight 0.25)
     - Quality: Historical success rate & review score (weight 0.20)
     - Reliability: Uptime & crash resilience (weight 0.15)
     - Availability: Available capacity (1 - current_load / max_load) (weight 0.15)
     - Cost: Normalized cost efficiency (weight 0.10)
     - Speed: Execution latency score (weight 0.10)
     - HistoricalFit: Domain specialization affinity (weight 0.05)
     - FailurePenalty: Dynamic deduction from recent consecutive failures

3. Thread-safe SQLite persistence with WAL mode.
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

logger = logging.getLogger("WORKFORCE_SCHEDULER")

DEFAULT_WORKFORCE_DB = os.environ.get("ANTI_WORKFORCE_DB", str(REPO_ROOT / "data" / "workforce.db"))


class WorkerLifecycleState(str, Enum):
    NEEDED = "NEEDED"
    PROVISION = "PROVISION"
    TRAIN = "TRAIN"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    WORKING = "WORKING"
    REVIEW = "REVIEW"
    PAUSE = "PAUSE"
    RETRAIN = "RETRAIN"


@dataclass
class WorkerProfile:
    worker_id: str
    worker_name: str
    domain: str
    lifecycle_state: WorkerLifecycleState
    skills: List[str]
    max_load: int = 5
    current_load: int = 0
    quality_rate: float = 1.0         # 0.0 - 1.0 (historical success)
    reliability_rate: float = 1.0     # 0.0 - 1.0 (low crash rate)
    avg_latency_ms: float = 500.0     # Target ~500ms
    cost_per_task: float = 0.01       # USD per task
    failure_count: int = 0
    updated_at: float = field(default_factory=time.time)


class WorkforceScheduler:
    """
    Autonomous Lifecycle State Machine and Intelligent WorkerScore Dispatcher.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_WORKFORCE_DB
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS workforce_profiles (
                worker_id TEXT PRIMARY KEY,
                worker_name TEXT NOT NULL,
                domain TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                skills TEXT NOT NULL,
                max_load INTEGER NOT NULL,
                current_load INTEGER NOT NULL,
                quality_rate REAL NOT NULL,
                reliability_rate REAL NOT NULL,
                avg_latency_ms REAL NOT NULL,
                cost_per_task REAL NOT NULL,
                failure_count INTEGER NOT NULL,
                updated_at REAL NOT NULL
            );
            """)

    def register_worker(
        self,
        worker_id: Any,
        worker_name: Optional[str] = None,
        domain: Optional[str] = None,
        skills: Optional[List[str]] = None,
        initial_state: WorkerLifecycleState = WorkerLifecycleState.PROVISION,
        max_load: int = 5
    ) -> WorkerProfile:
        if isinstance(worker_id, WorkerProfile):
            profile = worker_id
            self.save_worker(profile)
            logger.info(f"Registered worker {profile.worker_id} ({profile.worker_name}) in domain {profile.domain} with state {profile.lifecycle_state.value}")
            return profile

        profile = WorkerProfile(
            worker_id=worker_id,
            worker_name=worker_name or "Worker",
            domain=domain or "engineering",
            lifecycle_state=initial_state,
            skills=skills or [],
            max_load=max_load,
            current_load=0,
            updated_at=time.time()
        )
        self.save_worker(profile)
        logger.info(f"Registered worker {worker_id} ({profile.worker_name}) in domain {profile.domain} with state {initial_state.value}")
        return profile

    def save_worker(self, p: WorkerProfile):
        with self.conn:
            self.conn.execute("""
            INSERT OR REPLACE INTO workforce_profiles
            (worker_id, worker_name, domain, lifecycle_state, skills, max_load, current_load,
             quality_rate, reliability_rate, avg_latency_ms, cost_per_task, failure_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p.worker_id, p.worker_name, p.domain, p.lifecycle_state.value,
                json.dumps(p.skills), p.max_load, p.current_load,
                p.quality_rate, p.reliability_rate, p.avg_latency_ms,
                p.cost_per_task, p.failure_count, p.updated_at
            ))

    def get_worker(self, worker_id: str) -> Optional[WorkerProfile]:
        cur = self.conn.cursor()
        cur.execute("SELECT worker_id, worker_name, domain, lifecycle_state, skills, max_load, current_load, quality_rate, reliability_rate, avg_latency_ms, cost_per_task, failure_count, updated_at FROM workforce_profiles WHERE worker_id = ?", (worker_id,))
        row = cur.fetchone()
        if not row:
            return None
        return WorkerProfile(
            worker_id=row[0],
            worker_name=row[1],
            domain=row[2],
            lifecycle_state=WorkerLifecycleState(row[3]),
            skills=json.loads(row[4]),
            max_load=row[5],
            current_load=row[6],
            quality_rate=row[7],
            reliability_rate=row[8],
            avg_latency_ms=row[9],
            cost_per_task=row[10],
            failure_count=row[11],
            updated_at=row[12]
        )

    def transition_lifecycle(self, worker_id: str, new_state: WorkerLifecycleState, reason: str = "") -> bool:
        """
        Enforces canonical transitions between the 10 lifecycle states.
        """
        worker = self.get_worker(worker_id)
        if not worker:
            return False

        current = worker.lifecycle_state

        # Define valid transitions
        valid_transitions = {
            WorkerLifecycleState.NEEDED: {WorkerLifecycleState.PROVISION, WorkerLifecycleState.PAUSE},
            WorkerLifecycleState.PROVISION: {WorkerLifecycleState.TRAIN, WorkerLifecycleState.PAUSE},
            WorkerLifecycleState.TRAIN: {WorkerLifecycleState.SHADOW, WorkerLifecycleState.RETRAIN, WorkerLifecycleState.PAUSE},
            WorkerLifecycleState.SHADOW: {WorkerLifecycleState.CANARY, WorkerLifecycleState.RETRAIN, WorkerLifecycleState.PAUSE},
            WorkerLifecycleState.CANARY: {WorkerLifecycleState.READY, WorkerLifecycleState.RETRAIN, WorkerLifecycleState.PAUSE},
            WorkerLifecycleState.READY: {WorkerLifecycleState.ASSIGNED, WorkerLifecycleState.PAUSE, WorkerLifecycleState.RETRAIN},
            WorkerLifecycleState.ASSIGNED: {WorkerLifecycleState.WORKING, WorkerLifecycleState.READY, WorkerLifecycleState.PAUSE},
            WorkerLifecycleState.WORKING: {WorkerLifecycleState.REVIEW, WorkerLifecycleState.READY, WorkerLifecycleState.PAUSE},
            WorkerLifecycleState.REVIEW: {WorkerLifecycleState.READY, WorkerLifecycleState.RETRAIN, WorkerLifecycleState.PAUSE},
            WorkerLifecycleState.RETRAIN: {WorkerLifecycleState.TRAIN, WorkerLifecycleState.PAUSE},
            WorkerLifecycleState.PAUSE: {WorkerLifecycleState.PROVISION, WorkerLifecycleState.READY}
        }

        if new_state not in valid_transitions.get(current, set()):
            logger.warning(f"Invalid workforce transition for {worker_id}: {current.value} -> {new_state.value}")
            return False

        worker.lifecycle_state = new_state
        worker.updated_at = time.time()
        self.save_worker(worker)
        logger.info(f"Worker {worker_id} transitioned: {current.value} -> {new_state.value} ({reason})")
        return True

    def calculate_worker_score(
        self,
        worker: WorkerProfile,
        task_requirements: Dict[str, Any]
    ) -> float:
        """
        Calculates normalized WorkerScore (0.0 to 100.0) based on 7 factors + failure penalty.
        Weights:
          - SkillMatch: 25%
          - Quality: 20%
          - Reliability: 15%
          - Availability: 15%
          - CostEfficiency: 10%
          - Speed: 10%
          - HistoricalFit: 5%
        """
        # 1. SkillMatch (25%)
        required_skills = set(task_requirements.get("required_skills", []))
        if required_skills:
            matched = len(required_skills.intersection(set(worker.skills)))
            skill_score = (matched / len(required_skills)) * 25.0
        else:
            skill_score = 25.0

        # 2. Quality (20%)
        quality_score = max(0.0, min(1.0, worker.quality_rate)) * 20.0

        # 3. Reliability (15%)
        reliability_score = max(0.0, min(1.0, worker.reliability_rate)) * 15.0

        # 4. Availability (15%)
        avail_ratio = max(0.0, (worker.max_load - worker.current_load) / max(1, worker.max_load))
        availability_score = avail_ratio * 15.0

        # 5. Cost Efficiency (10%)
        # Lower cost is better; base line $0.05 per task
        cost_ratio = max(0.0, min(1.0, (0.05 - worker.cost_per_task) / 0.05)) if worker.cost_per_task < 0.05 else 0.1
        cost_score = cost_ratio * 10.0

        # 6. Speed (10%)
        # Baseline latency target 1000ms; lower is better
        speed_ratio = max(0.0, min(1.0, (1500.0 - worker.avg_latency_ms) / 1500.0))
        speed_score = speed_ratio * 10.0

        # 7. Historical Domain Fit (5%)
        target_domain = task_requirements.get("domain", "")
        fit_score = 5.0 if target_domain and worker.domain.lower() == target_domain.lower() else 2.5

        # 8. Failure Penalty (Deduction)
        penalty = min(30.0, worker.failure_count * 5.0)

        raw_score = (
            skill_score +
            quality_score +
            reliability_score +
            availability_score +
            cost_score +
            speed_score +
            fit_score
        ) - penalty

        return round(max(0.0, min(100.0, raw_score)), 2)

    def select_best_worker(
        self,
        task_requirements: Dict[str, Any],
        allow_canary: bool = False
    ) -> Optional[Tuple[WorkerProfile, float]]:
        """
        Dispatches the optimal worker based on WorkerScore ranking.
        Excludes workers that are overloaded or not in READY (or CANARY if enabled).
        """
        cur = self.conn.cursor()
        allowed_states = [WorkerLifecycleState.READY.value]
        if allow_canary:
            allowed_states.append(WorkerLifecycleState.CANARY.value)

        placeholders = ",".join("?" * len(allowed_states))
        cur.execute(f"""
        SELECT worker_id FROM workforce_profiles
        WHERE lifecycle_state IN ({placeholders}) AND current_load < max_load
        """, allowed_states)

        candidate_ids = [r[0] for r in cur.fetchall()]
        if not candidate_ids:
            return None

        candidates_with_scores: List[Tuple[WorkerProfile, float]] = []
        for wid in candidate_ids:
            worker = self.get_worker(wid)
            if worker:
                score = self.calculate_worker_score(worker, task_requirements)
                candidates_with_scores.append((worker, score))

        # Sort descending by WorkerScore
        candidates_with_scores.sort(key=lambda x: x[1], reverse=True)

        if not candidates_with_scores:
            return None

        best_worker, best_score = candidates_with_scores[0]
        return best_worker, best_score

    def assign_task_to_worker(self, worker_id: str) -> bool:
        worker = self.get_worker(worker_id)
        if not worker or worker.current_load >= worker.max_load:
            return False
        worker.current_load += 1
        if worker.lifecycle_state == WorkerLifecycleState.READY:
            worker.lifecycle_state = WorkerLifecycleState.WORKING
        worker.updated_at = time.time()
        self.save_worker(worker)
        return True

    def complete_task_for_worker(self, worker_id: str, success: bool, latency_ms: float = 500.0) -> bool:
        worker = self.get_worker(worker_id)
        if not worker:
            return False

        worker.current_load = max(0, worker.current_load - 1)
        if success:
            worker.quality_rate = min(1.0, worker.quality_rate * 0.95 + 1.0 * 0.05)
            worker.failure_count = max(0, worker.failure_count - 1)
        else:
            worker.quality_rate = max(0.0, worker.quality_rate * 0.9)
            worker.failure_count += 1

        # Exponential moving average for latency
        worker.avg_latency_ms = worker.avg_latency_ms * 0.8 + latency_ms * 0.2

        if worker.current_load == 0 and worker.lifecycle_state == WorkerLifecycleState.WORKING:
            worker.lifecycle_state = WorkerLifecycleState.READY

        worker.updated_at = time.time()
        self.save_worker(worker)
        return True


if __name__ == "__main__":
    scheduler = WorkforceScheduler(":memory:")
    w1 = scheduler.register_worker("w-eng-01", "CoderAlpha", "engineering", ["python", "rego"], WorkerLifecycleState.READY)
    w2 = scheduler.register_worker("w-rev-01", "RevenueBot", "revenue", ["crypto", "reconciliation"], WorkerLifecycleState.READY)

    task = {"required_skills": ["python"], "domain": "engineering"}
    best, score = scheduler.select_best_worker(task)
    print(f"Selected: {best.worker_name} ({best.worker_id}) with Score: {score}")
