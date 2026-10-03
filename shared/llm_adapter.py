#!/usr/bin/env python3
"""
PrimeNode Execution Plane Browser LLM Adapter
Architecture:
1. Resilient JSON parsing & markdown fence stripping without monkey-patching.
2. Circuit Breaker & Automatic Failover:
   - Primary: OmniRoute (http://127.0.0.1:20128/v1)
   - Fallback: Local Ollama (http://127.0.0.1:11434/v1)
"""

import os
import re
import json
import time
import logging
from typing import Any, TypeVar, Optional
from pydantic import BaseModel
from browser_use.llm.openai.chat import ChatOpenAI
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.llm.exceptions import ModelProviderError

logger = logging.getLogger("LLM_ADAPTER")

T = TypeVar('T', bound=BaseModel)

OLLAMA_FALLBACK_URL = "http://127.0.0.1:11434/v1"
OLLAMA_FALLBACK_MODEL = "qwen2.5:0.5b"

class PrimeNodeBrowserLLMAdapter(ChatOpenAI):
    """
    Adapter layer isolating browser-use from upstream model quirks.
    Handles markdown backticks, raw JSON strings, and automatic failover to Ollama.
    """

    async def ainvoke(
        self, messages: list[Any], output_format: Optional[type[T]] = None, **kwargs: Any
    ) -> ChatInvokeCompletion[Any]:
        # Attempt Primary (OmniRoute)
        try:
            return await self._invoke_internal(messages, output_format=output_format, **kwargs)
        except Exception as primary_err:
            logger.warning(f"Primary LLM endpoint failed ({primary_err}). Initiating CircuitBreaker Failover to local Ollama...")
            # Fallback to Local Ollama
            orig_base_url = self.base_url
            orig_model = self.model
            try:
                self.base_url = OLLAMA_FALLBACK_URL
                self.model = OLLAMA_FALLBACK_MODEL
                self.dont_force_structured_output = True
                res = await self._invoke_internal(messages, output_format=output_format, **kwargs)
                logger.info("Local Ollama failover SUCCEEDED.")
                return res
            except Exception as fallback_err:
                logger.error(f"Both primary and local fallback failed: {fallback_err}")
                raise primary_err
            finally:
                self.base_url = orig_base_url
                self.model = orig_model

    async def _invoke_internal(
        self, messages: list[Any], output_format: Optional[type[T]] = None, **kwargs: Any
    ) -> ChatInvokeCompletion[Any]:
        if output_format is None:
            return await super().ainvoke(messages, output_format=None, **kwargs)

        try:
            return await super().ainvoke(messages, output_format=output_format, **kwargs)
        except Exception as primary_err:
            orig_flag = self.dont_force_structured_output
            try:
                self.dont_force_structured_output = True
                raw_res = await super().ainvoke(messages, output_format=None, **kwargs)
                raw_text = raw_res.completion if hasattr(raw_res, 'completion') else str(raw_res)

                clean_text = raw_text.strip()
                if clean_text.startswith("```"):
                    lines = clean_text.splitlines()
                    if lines and lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    clean_text = "\n".join(lines).strip()

                parsed = None
                try:
                    parsed = output_format.model_validate_json(clean_text)
                except Exception:
                    match = re.search(r"\{.*\}", clean_text, re.DOTALL)
                    if match:
                        try:
                            parsed = output_format.model_validate_json(match.group(0))
                        except Exception:
                            pass

                if parsed is not None:
                    return ChatInvokeCompletion(
                        completion=parsed,
                        usage=getattr(raw_res, 'usage', None),
                        stop_reason=getattr(raw_res, 'stop_reason', None)
                    )

                raise primary_err
            finally:
                self.dont_force_structured_output = orig_flag
