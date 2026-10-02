"""Deterministic offline LLM provider.

This is the default provider: it needs no credentials and no network, so the
full 10,000-agent pipeline runs in CI, in a Databricks Community Edition
notebook, or on a laptop. It emits the *same JSON contract* as a real model
(:func:`insilico_trial_mas.llm.prompts.heuristic_symptom_response`), so switching
to Bedrock changes the content of the qualitative reports but not a single line
of downstream code.
"""

from __future__ import annotations

import time

from .base import BaseLLMClient, LLMRequest, LLMResponse
from .prompts import heuristic_symptom_response


class OfflineLLMClient(BaseLLMClient):
    """Rule-based persona narrator used for reproducibility and tests."""

    provider = "offline"
    model = "offline-heuristic-v1"
    offline = True

    def __init__(self, *, simulated_latency_ms: float = 0.0) -> None:
        self.simulated_latency_ms = simulated_latency_ms

    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        start = time.perf_counter()
        context = request.context or {}
        ae_probabilities = {k: float(v) for k, v in (context.get("ae_probabilities") or {}).items()}
        worst_grade = int(context.get("worst_grade", 1) or 1)
        text = heuristic_symptom_response(
            ae_probabilities,
            epoch=int(context.get("epoch", 0) or 0),
            worst_grade=worst_grade,
        )
        if self.simulated_latency_ms:
            # Only used by load tests that need realistic latency accounting.
            time.sleep(self.simulated_latency_ms / 1000.0)
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            tokens_in=self.estimate_tokens(request.prompt),
            tokens_out=self.estimate_tokens(text),
            latency_ms=self._timed(start),
            attempts=1,
        )


class EchoLLMClient(BaseLLMClient):
    """Test double that returns a fixed answer and records the requests it saw."""

    provider = "echo"
    model = "echo-v1"
    offline = True

    def __init__(self, response_text: str = '{"symptoms": []}') -> None:
        self.response_text = response_text
        self.requests: list[LLMRequest] = []

    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            text=self.response_text,
            provider=self.provider,
            model=self.model,
            tokens_in=self.estimate_tokens(request.prompt),
            tokens_out=self.estimate_tokens(self.response_text),
        )
