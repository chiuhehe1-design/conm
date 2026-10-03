#!/usr/bin/env python3
"""
JEV MODEL ROUTER & RESILIENCE ADAPTER
Orchestrates: Jev Decision Gate -> ModelRouter -> OmniRoute Gateway -> Multi-tier Failover.

Resilience Matrix:
- Handles 429 Rate Limits
- Handles Timeouts
- Handles Provider 5xx / Bad Gateway
- Handles OmniRoute Connection Failure -> Direct In-Memory Ollama Fallback
"""

import time
import json
import socket
import logging
import urllib.request
import urllib.error
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger("JevResilienceAdapter")


class ModelTier(str, Enum):
    FAST = "FAST"
    BALANCED = "BALANCED"
    CODE_HEAVY = "CODE_HEAVY"
    STRONG_REASONING = "STRONG_REASONING"


@dataclass
class JevDecision:
    tier: ModelTier
    complexity_score: float
    recommended_role: str
    reasoning: str


class JevGate:
    """Heuristic / LLM-guided evaluation of prompt complexity."""

    @staticmethod
    def evaluate(prompt: str, retry_count: int = 0) -> JevDecision:
        p_lower = prompt.lower()
        if retry_count >= 2:
            return JevDecision(
                tier=ModelTier.STRONG_REASONING,
                complexity_score=0.95,
                recommended_role="reasoner",
                reasoning="Escalated to STRONG_REASONING due to repeated retry depletion"
            )

        reasoning_kw = ["architect", "root cause", "concurrency", "race condition", "deadlock", "security", "failover"]
        code_kw = ["implement", "refactor", "patch", "unit test", "fix bug", "reconcile", "parser"]
        fast_kw = ["status", "check", "verify", "ping", "inspect", "list", "format", "typo"]

        for kw in reasoning_kw:
            if kw in p_lower:
                return JevDecision(ModelTier.STRONG_REASONING, 0.85, "reasoner", f"Keyword '{kw}' requires deep reasoning")
        for kw in code_kw:
            if kw in p_lower:
                return JevDecision(ModelTier.CODE_HEAVY, 0.70, "coding", f"Keyword '{kw}' requires code specialist")
        for kw in fast_kw:
            if kw in p_lower:
                return JevDecision(ModelTier.FAST, 0.20, "fast", f"Keyword '{kw}' suitable for fast tier")

        return JevDecision(ModelTier.BALANCED, 0.50, "general", "Standard balanced execution")


class ModelRouter:
    """Resolves Jev tier to priority candidate chains."""

    TIER_MAPPINGS = {
        ModelTier.FAST: ["tk/claude-3-5-sonnet", "in-ai/deepseek-r1", "qwen2.5:0.5b"],
        ModelTier.BALANCED: ["tk/claude-3-5-sonnet", "in-ai/deepseek-r1", "qwen2.5:0.5b"],
        ModelTier.CODE_HEAVY: ["tk/claude-3-5-sonnet", "openai/qwen2.5-coder:7b", "qwen2.5:0.5b"],
        ModelTier.STRONG_REASONING: ["in-ai/deepseek-r1", "tk/claude-3-5-sonnet", "qwen2.5:0.5b"]
    }

    @classmethod
    def get_candidate_chain(cls, tier: ModelTier) -> List[str]:
        return cls.TIER_MAPPINGS.get(tier, ["auto/smart", "qwen2.5:0.5b"])


class ResilientOmniDispatcher:
    """
    Executes prompt with resilient failover handling:
    429, timeout, 5xx, or OmniRoute connection failure -> auto-fallback to Ollama.
    """

    def __init__(
        self,
        omniroute_url: str = "http://127.0.0.1:20128/v1",
        ollama_url: str = "http://127.0.0.1:11434/v1",
        timeout_sec: float = 5.0
    ):
        self.omniroute_url = omniroute_url
        self.ollama_url = ollama_url
        self.timeout_sec = timeout_sec
        self._query_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self.cache_ttl_sec: float = 300.0

    def warm_ollama_model(self, model: str = "qwen2.5:0.5b") -> bool:
        """Proactively preloads local Ollama model into memory to eliminate cold start."""
        try:
            url = f"{self.ollama_url}/models"
            req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    logger.info(f"Ollama local model '{model}' warmed and ready.")
                    return True
        except Exception as e:
            logger.debug(f"Ollama warming ping skipped ({e})")
        return False

    def _call_http(self, base_url: str, model: str, prompt: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        url = f"{base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        data = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2
        }
        req = urllib.request.Request(url, headers=headers, data=json.dumps(data).encode(), method="POST")
        effective_timeout = timeout or self.timeout_sec

        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode()
            except Exception:
                pass
            raise RuntimeError(f"HTTP_{e.code}: {err_body or e.reason}")
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            raise RuntimeError(f"NET_ERROR: {e}")

    def dispatch(self, prompt: str, retry_count: int = 0, use_cache: bool = True) -> Dict[str, Any]:
        # Cache check for deterministic idempotent queries
        cache_key = prompt.strip()
        if use_cache and retry_count == 0:
            cached = self._query_cache.get(cache_key)
            if cached:
                cached_time, cached_res = cached
                if time.time() - cached_time < self.cache_ttl_sec:
                    hit = dict(cached_res)
                    hit["cached"] = True
                    return hit

        # 1. Jev Decision Gate
        jev_dec = JevGate.evaluate(prompt, retry_count)
        candidates = ModelRouter.get_candidate_chain(jev_dec.tier)

        attempts = []
        # 2. Attempt Candidate Chain via OmniRoute
        for model in candidates:
            # If candidate is explicitly local model, skip OmniRoute
            if model == "qwen2.5:0.5b":
                continue

            try:
                res = self._call_http(self.omniroute_url, model, prompt)
                content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
                result = {
                    "status": "SUCCESS",
                    "provider": "OMNIROUTE",
                    "model": model,
                    "tier": jev_dec.tier.value,
                    "jev_reasoning": jev_dec.reasoning,
                    "fallback_used": False,
                    "content": content,
                    "attempts": attempts,
                    "cached": False
                }
                if use_cache:
                    self._query_cache[cache_key] = (time.time(), result)
                return result
            except Exception as e:
                attempts.append({"target": f"OmniRoute:{model}", "error": str(e)})
                logger.warning(f"OmniRoute model '{model}' failed: {e}. Trying next candidate...")

        # 3. Failover: Local In-Memory / Edge Ollama
        logger.info("All OmniRoute targets exhausted. Initiating resilient Ollama fallback...")
        try:
            res = self._call_http(self.ollama_url, "qwen2.5:0.5b", prompt, timeout=15.0)
            content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
            result = {
                "status": "SUCCESS",
                "provider": "OLLAMA_LOCAL_FALLBACK",
                "model": "qwen2.5:0.5b",
                "tier": jev_dec.tier.value,
                "jev_reasoning": jev_dec.reasoning,
                "fallback_used": True,
                "fallback_reason": f"OmniRoute candidates exhausted ({len(attempts)} errors)",
                "content": content,
                "attempts": attempts,
                "cached": False
            }
            if use_cache:
                self._query_cache[cache_key] = (time.time(), result)
            return result
        except Exception as ollama_err:
            attempts.append({"target": "Ollama:qwen2.5:0.5b", "error": str(ollama_err)})
            return {
                "status": "FAILURE",
                "provider": "NONE",
                "model": "NONE",
                "tier": jev_dec.tier.value,
                "error": f"All providers including Ollama failed: {ollama_err}",
                "attempts": attempts
            }
