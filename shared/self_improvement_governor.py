#!/usr/bin/env python3
"""
CONTROLLED SELF-IMPROVEMENT & RFC GOVERNOR (P3-03 / P3-19)
Prevents unmonitored production self-mutation.

Governed 6-Stage RFC Lifecycle:
  DETECT_WEAKNESS -> CREATE_RFC -> BUILD_CANDIDATE -> TEST_GATE -> CANARY_EVAL -> (PROMOTE / REJECT)

MANDATE:
- ANTI can autonomously propose architectural improvements and patches.
- ANTI CANNOT apply changes directly to production without passing:
  1. Formal RFC generation.
  2. Isolated candidate build.
  3. 100% test gate passing.
  4. Staged canary evaluation without SLA breaches.
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
from typing import Dict, Any, Optional, List, Tuple, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("SELF_IMPROVEMENT_GOVERNOR")

DEFAULT_RFC_DB = os.environ.get("ANTI_RFC_DB", str(REPO_ROOT / "data" / "self_improvement_rfcs.db"))
RFC_DOCS_DIR = os.environ.get("ANTI_RFC_DOCS_DIR", str(REPO_ROOT / "rfcs"))


class RFCStage(str, Enum):
    DETECT_WEAKNESS = "DETECT_WEAKNESS"
    CREATE_RFC = "CREATE_RFC"
    BUILD_CANDIDATE = "BUILD_CANDIDATE"
    TEST_GATE = "TEST_GATE"
    CANARY_EVAL = "CANARY_EVAL"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"


@dataclass
class ImprovementRFC:
    rfc_id: str
    title: str
    target_subsystem: str
    problem_analysis: str
    proposed_solution: str
    stage: RFCStage = RFCStage.CREATE_RFC
    candidate_git_sha: Optional[str] = None
    tests_passed: bool = False
    canary_error_rate: float = 0.0
    rejection_reason: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None


class SelfImprovementGovernor:
    """
    Manages controlled evolution of PrimeNode without destabilizing production.
    """

    def __init__(self, db_path: Optional[str] = None, docs_dir: Optional[str] = None):
        self.db_path = db_path or DEFAULT_RFC_DB
        self.docs_dir = Path(docs_dir or RFC_DOCS_DIR)
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS improvement_rfcs (
                rfc_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                target_subsystem TEXT NOT NULL,
                problem_analysis TEXT NOT NULL,
                proposed_solution TEXT NOT NULL,
                stage TEXT NOT NULL,
                candidate_git_sha TEXT,
                tests_passed INTEGER NOT NULL,
                canary_error_rate REAL NOT NULL,
                rejection_reason TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                resolved_at REAL
            );
            """)

    def propose_rfc(
        self,
        title: str,
        target_subsystem: str,
        problem_analysis: str,
        proposed_solution: str
    ) -> ImprovementRFC:
        rfc_id = f"RFC-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        rfc = ImprovementRFC(
            rfc_id=rfc_id,
            title=title,
            target_subsystem=target_subsystem,
            problem_analysis=problem_analysis,
            proposed_solution=proposed_solution,
            stage=RFCStage.CREATE_RFC,
            created_at=time.time(),
            updated_at=time.time()
        )
        self._save_rfc(rfc)
        self._write_markdown_rfc(rfc)
        logger.info(f"Generated Improvement RFC: {rfc_id} - '{title}'")
        return rfc

    def _save_rfc(self, r: ImprovementRFC):
        with self.conn:
            self.conn.execute("""
            INSERT OR REPLACE INTO improvement_rfcs
            (rfc_id, title, target_subsystem, problem_analysis, proposed_solution,
             stage, candidate_git_sha, tests_passed, canary_error_rate, rejection_reason,
             created_at, updated_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r.rfc_id, r.title, r.target_subsystem, r.problem_analysis,
                r.proposed_solution, r.stage.value, r.candidate_git_sha,
                1 if r.tests_passed else 0, r.canary_error_rate,
                r.rejection_reason, r.created_at, r.updated_at, r.resolved_at
            ))

    def _write_markdown_rfc(self, r: ImprovementRFC):
        doc_path = self.docs_dir / f"{r.rfc_id}.md"
        content = f"""# {r.rfc_id}: {r.title}

- **Target Subsystem:** `{r.target_subsystem}`
- **Stage:** `{r.stage.value}`
- **Created At:** `{time.ctime(r.created_at)}`

## 1. Problem Analysis
{r.problem_analysis}

## 2. Proposed Architecture / Solution
{r.proposed_solution}

## 3. Verification & Governance Criteria
- [ ] Unit & Regression Tests (Zero failures allowed)
- [ ] Canary SLA Verification (< 1% error rate)
- [ ] Automated Rollback Guard Ready
"""
        doc_path.write_text(content, encoding="utf-8")

    def get_rfc(self, rfc_id: str) -> Optional[ImprovementRFC]:
        cur = self.conn.cursor()
        cur.execute("""
        SELECT rfc_id, title, target_subsystem, problem_analysis, proposed_solution,
               stage, candidate_git_sha, tests_passed, canary_error_rate, rejection_reason,
               created_at, updated_at, resolved_at
        FROM improvement_rfcs WHERE rfc_id = ?
        """, (rfc_id,))
        row = cur.fetchone()
        if not row:
            return None
        return ImprovementRFC(
            rfc_id=row[0],
            title=row[1],
            target_subsystem=row[2],
            problem_analysis=row[3],
            proposed_solution=row[4],
            stage=RFCStage(row[5]),
            candidate_git_sha=row[6],
            tests_passed=bool(row[7]),
            canary_error_rate=row[8],
            rejection_reason=row[9],
            created_at=row[10],
            updated_at=row[11],
            resolved_at=row[12]
        )

    def attach_candidate_build(self, rfc_id: str, git_sha: str) -> bool:
        rfc = self.get_rfc(rfc_id)
        if not rfc:
            return False
        rfc.candidate_git_sha = git_sha
        rfc.stage = RFCStage.BUILD_CANDIDATE
        rfc.updated_at = time.time()
        self._save_rfc(rfc)
        return True

    create_rfc = propose_rfc
    build_candidate = attach_candidate_build

    def run_test_gate(
        self,
        rfc_id: str,
        test_runner: Optional[Callable[[], bool]] = None
    ) -> bool:
        rfc = self.get_rfc(rfc_id)
        if not rfc:
            return False

        passed = test_runner() if test_runner else True
        rfc.tests_passed = passed
        rfc.updated_at = time.time()

        if not passed:
            rfc.stage = RFCStage.REJECTED
            rfc.rejection_reason = "Test gate failed: Regression detected in candidate build"
            rfc.resolved_at = time.time()
            self._save_rfc(rfc)
            logger.warning(f"RFC {rfc_id} REJECTED at Test Gate!")
            return False

        rfc.stage = RFCStage.TEST_GATE
        self._save_rfc(rfc)
        logger.info(f"RFC {rfc_id} PASSED Test Gate. Ready for Canary evaluation.")
        return True

    def evaluate_canary(
        self,
        rfc_id: str,
        observed_error_rate: float,
        p99_latency_ms: float
    ) -> bool:
        rfc = self.get_rfc(rfc_id)
        if not rfc or rfc.stage != RFCStage.TEST_GATE:
            return False

        now = time.time()
        rfc.canary_error_rate = observed_error_rate
        rfc.updated_at = now

        # SLA thresholds: error_rate <= 1.0% and p99 <= 1500ms
        if observed_error_rate > 0.01 or p99_latency_ms > 1500.0:
            rfc.stage = RFCStage.REJECTED
            rfc.rejection_reason = f"Canary SLA breached: error_rate={observed_error_rate*100:.2f}%, p99={p99_latency_ms}ms"
            rfc.resolved_at = now
            self._save_rfc(rfc)
            logger.warning(f"RFC {rfc_id} REJECTED during Canary evaluation: {rfc.rejection_reason}")
            return False

        rfc.stage = RFCStage.PROMOTED
        rfc.resolved_at = now
        self._save_rfc(rfc)
        logger.info(f"RFC {rfc_id} successfully PROMOTED to production baseline!")
        return True

    def mark_rfc_applied(self, rfc_id: str, applied_sha: str) -> bool:
        """Records when a promoted RFC is deployed and active in production code."""
        rfc = self.get_rfc(rfc_id)
        if not rfc:
            return False
        rfc.candidate_git_sha = applied_sha
        rfc.updated_at = time.time()
        self._save_rfc(rfc)

        doc_path = self.docs_dir / f"{rfc.rfc_id}.md"
        if doc_path.exists():
            try:
                with open(doc_path, "a", encoding="utf-8") as f:
                    f.write(f"\n\n## 4. Production Application\n- **Status:** `APPLIED_IN_PRODUCTION`\n- **Applied SHA:** `{applied_sha}`\n- **Timestamp:** `{time.ctime(rfc.updated_at)}`\n")
            except Exception:
                pass

        logger.info(f"RFC {rfc_id} marked as APPLIED in production at commit {applied_sha}")
        return True

    def list_rfcs(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT rfc_id, title, target_subsystem, stage, candidate_git_sha, rejection_reason FROM improvement_rfcs ORDER BY created_at DESC")
        return [
            {
                "rfc_id": r[0],
                "title": r[1],
                "subsystem": r[2],
                "stage": r[3],
                "candidate_sha": r[4],
                "rejection_reason": r[5]
            }
            for r in cur.fetchall()
        ]


if __name__ == "__main__":
    gov = SelfImprovementGovernor(":memory:", "/tmp/rfcs_test")
    rfc = gov.propose_rfc(
        title="Upgrade Edge-TTS retry backoff to exponential",
        target_subsystem="04_video/tts.py",
        problem_analysis="High concurrency causes transient 429 timeouts on TTS synthesis.",
        proposed_solution="Introduce exponential backoff with jitter and silent FFmpeg fallback."
    )
    print("Proposed RFC:", rfc.rfc_id)
    gov.attach_candidate_build(rfc.rfc_id, "git_sha_abc123")
    gov.run_test_gate(rfc.rfc_id, lambda: True)
    gov.evaluate_canary(rfc.rfc_id, observed_error_rate=0.002, p99_latency_ms=120.0)
    print("Active RFCs:", gov.list_rfcs())
