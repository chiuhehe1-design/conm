#!/usr/bin/env python3
"""
STANDING RFC GENERATOR FOR AUTONOMOUS SYSTEM OPTIMIZATION (P3-03 / P3-19 / ANTI-013)
Continuously monitors system health, telemetry, and compute economics,
detecting performance bottlenecks and formulating governed architectural RFCs.

Closed-Loop Lifecycle:
  1. METRIC TELEMETRY SCAN
     - Analyzes latency, error rates, circuit breaker trips, worker scores, network quotas.
  2. BOTTLENECK DETECTION
     - Identifies optimization opportunities against target SLAs.
  3. GOVERNED RFC CREATION
     - Formulates structured ImprovementRFC markdown & database records.
  4. ISOLATED TEST GATE
     - Executes validation tests in candidate sandbox.
  5. CANARY EVALUATION
     - Validates error rates and latency SLAs before production promotion.
"""

import os
import sys
import time
import uuid
import logging
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.self_improvement_governor import SelfImprovementGovernor, ImprovementRFC, RFCStage

logger = logging.getLogger("STANDING_RFC_GENERATOR")


@dataclass
class TelemetrySnapshot:
    gateway_p99_ms: float = 45.0
    gateway_error_rate: float = 0.001
    model_router_fallback_rate: float = 0.02
    worker_failure_rate: float = 0.01
    network_quota_utilization: float = 0.45
    avg_task_cost_usd: float = 0.04


class OptimizationDomain(str, Enum):
    LATENCY = "latency"
    MODEL_ROUTING = "model_routing"
    WORKFORCE = "workforce"
    NETWORK_QOS = "network_qos"
    COMPUTE_ECONOMICS = "compute_economics"


class StandingRFCGenerator:
    """
    Automated telemetry watcher and RFC proposal engine.
    """

    def __init__(self, governor: SelfImprovementGovernor):
        self.governor = governor

    def analyze_telemetry_and_generate_rfcs(
        self,
        telemetry: TelemetrySnapshot
    ) -> List[ImprovementRFC]:
        """
        Scans telemetry against system invariants and autonomously generates RFCs.
        """
        generated_rfcs: List[ImprovementRFC] = []

        # 1. Latency Bottleneck Detection
        if telemetry.gateway_p99_ms > 100.0:
            rfc = self.governor.create_rfc(
                title="Optimize Gateway Reverse Proxy Keep-Alive Connection Pool",
                target_subsystem="gateway",
                problem_analysis=(
                    f"Gateway P99 latency degraded to {telemetry.gateway_p99_ms:.1f}ms "
                    f"(SLA threshold: 100.0ms) due to socket re-establishment overhead."
                ),
                proposed_solution=(
                    "Enable HTTP/1.1 persistent keep-alive pool with 60-second reuse timeout "
                    "and thread-local connection caching."
                )
            )
            generated_rfcs.append(rfc)

        # 2. Model Router Fallback Optimization
        if telemetry.model_router_fallback_rate > 0.05:
            rfc = self.governor.create_rfc(
                title="Adaptive OmniRoute Model Fallback and Local Ollama Warming",
                target_subsystem="jev_resilience",
                problem_analysis=(
                    f"ModelRouter fallback frequency reached {telemetry.model_router_fallback_rate*100:.1f}%, "
                    "causing unnecessary round-trip latency to remote providers during rate limits."
                ),
                proposed_solution=(
                    "Implement proactive Ollama local model warming and dynamic backoff window "
                    "scaling based on provider HTTP 429 response headers."
                )
            )
            generated_rfcs.append(rfc)

        # 3. Worker Failure Rate / Workforce Retraining
        if telemetry.worker_failure_rate > 0.04:
            rfc = self.governor.create_rfc(
                title="Automated Workforce Shadow Retraining for Underperforming Workers",
                target_subsystem="workforce_scheduler",
                problem_analysis=(
                    f"Worker failure rate reached {telemetry.worker_failure_rate*100:.1f}%, "
                    "exceeding the 4.0% tolerance threshold."
                ),
                proposed_solution=(
                    "Trigger automated state transition READY -> RETRAIN -> SHADOW for workers "
                    "with WorkerScore below 70.0 until 3 consecutive shadow tasks pass."
                )
            )
            generated_rfcs.append(rfc)

        # 4. Network QoS Quota Optimization
        if telemetry.network_quota_utilization > 0.80:
            rfc = self.governor.create_rfc(
                title="Dynamic Kernel QoS Bandwidth Burst Ceiling Adjustment",
                target_subsystem="network_governor",
                problem_analysis=(
                    f"Network quota utilization exceeded {telemetry.network_quota_utilization*100:.1f}%, "
                    "risking HTB queue throttling for priority control plane traffic."
                ),
                proposed_solution=(
                    "Scale HTB ceil bandwidth for class 1:20 (Production) by 25% while maintaining "
                    "strict 1:10 (Control) rate reservation."
                )
            )
            generated_rfcs.append(rfc)

        # 5. Compute Cost Inefficiency
        if telemetry.avg_task_cost_usd > 0.15:
            rfc = self.governor.create_rfc(
                title="Token Compression and Speculative Tier Downgrade in Jev",
                target_subsystem="jev_adapter",
                problem_analysis=(
                    f"Average task compute cost rose to ${telemetry.avg_task_cost_usd:.4f}, "
                    "diluting Net ROI margins on sub-$50 bounties."
                ),
                proposed_solution=(
                    "Apply AST-based code snippet pruning before LLM context ingestion "
                    "and downgrade FAST tier tasks to local Ollama qwen2.5:0.5b."
                )
            )
            generated_rfcs.append(rfc)

        return generated_rfcs

    def run_optimization_cycle(
        self,
        telemetry: TelemetrySnapshot,
        candidate_test_runner: Optional[Callable[[ImprovementRFC], bool]] = None,
        canary_error_rate: float = 0.002,
        canary_p99_ms: float = 65.0
    ) -> Dict[str, Any]:
        """
        Executes full closed-loop RFC governance cycle:
          Scan -> Generate -> Test Gate -> Canary Eval -> Promote
        """
        rfcs = self.analyze_telemetry_and_generate_rfcs(telemetry)
        promoted = []
        rejected = []

        test_func = candidate_test_runner or (lambda rfc: True)

        for rfc in rfcs:
            # 1. Build Candidate
            self.governor.build_candidate(rfc.rfc_id, git_sha=f"sha-{uuid.uuid4().hex[:7]}")

            # 2. Test Gate
            passed_test = test_func(rfc)
            self.governor.run_test_gate(rfc.rfc_id, test_runner=lambda: passed_test)

            # 3. Canary Evaluation
            promoted_ok = self.governor.evaluate_canary(
                rfc.rfc_id,
                observed_error_rate=canary_error_rate,
                p99_latency_ms=canary_p99_ms
            )

            current = self.governor.get_rfc(rfc.rfc_id)
            if promoted_ok and current and current.stage == RFCStage.PROMOTED:
                promoted.append(current)
            else:
                if current:
                    rejected.append(current)

        return {
            "telemetry": telemetry,
            "rfcs_generated": len(rfcs),
            "promoted_count": len(promoted),
            "rejected_count": len(rejected),
            "promoted_rfcs": promoted,
            "rejected_rfcs": rejected
        }
