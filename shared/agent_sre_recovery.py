#!/usr/bin/env python3
"""
FAILURE RECOVERY AGENT (agent-sre-recovery)
Classifies failures (transient, provider, code, auth, infrastructure).
Applies self-healing:
- Transient -> Exponential backoff
- Provider -> Circuit Breaker trip & model fallback
- Code -> Feeds test errors back into prompt (max 3 rounds)
- Auth / Financial -> Freezes subsystem, alerts Telegram
- DLQ -> Inspects dead letter queue, summarizes root causes
"""

import os
import sys
import json
import time
import logging
from typing import Dict, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [AGENT_SRE_RECOVERY] %(message)s")
logger = logging.getLogger("AGENT_SRE_RECOVERY")

class SREFailureClassifier:
    @staticmethod
    def classify_error(err_str: str) -> Dict[str, Any]:
        err_lower = str(err_str).lower()

        if any(kw in err_lower for kw in ["429", "rate limit", "too many requests", "quota exceeded"]):
            return {
                "category": "transient",
                "action": "backoff_and_switch_model",
                "backoff_seconds": 15,
                "can_retry": True
            }
        elif any(kw in err_lower for kw in ["502", "503", "bad gateway", "connection refused", "econnrefused"]):
            return {
                "category": "provider",
                "action": "trip_circuit_breaker_and_fallback_ollama",
                "can_retry": True
            }
        elif any(kw in err_lower for kw in ["assertionerror", "test failed", "pytest", "failed test"]):
            return {
                "category": "code",
                "action": "feed_test_log_to_coder",
                "max_fix_rounds": 3,
                "can_retry": True
            }
        elif any(kw in err_lower for kw in ["401", "403", "unauthorized", "invalid_api_key", "no active credentials"]):
            return {
                "category": "auth",
                "action": "freeze_subsystem_and_escalate",
                "can_retry": False
            }
        else:
            return {
                "category": "unknown",
                "action": "log_and_retry_once",
                "can_retry": True
            }

if __name__ == "__main__":
    c1 = SREFailureClassifier.classify_error("HTTP 429: Too Many Requests")
    print("Classify 429:", c1)
    c2 = SREFailureClassifier.classify_error("AssertionError: assert multiply(2,2) == 4 failed")
    print("Classify test error:", c2)
    c3 = SREFailureClassifier.classify_error("HTTP 401: Invalid API key")
    print("Classify auth error:", c3)
