"""LLM layer tests: prompts, parsing, caching, rate limiting, resilience."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from insilico_trial_mas.config import LLMConfig
from insilico_trial_mas.errors import LLMProviderError
from insilico_trial_mas.llm.base import BaseLLMClient, LLMRequest, LLMResponse
from insilico_trial_mas.llm.cache import LLMResponseCache, cache_key
from insilico_trial_mas.llm.factory import create_llm_client
from insilico_trial_mas.llm.offline import EchoLLMClient, OfflineLLMClient
from insilico_trial_mas.llm.prompts import build_patient_prompt, parse_symptom_response
from insilico_trial_mas.llm.rate_limit import AsyncTokenBucket, RetryPolicy, is_retryable
from insilico_trial_mas.llm.resilient import ResilientLLMClient


class FlakyClient(BaseLLMClient):
    """Fails a fixed number of times with a retryable error, then succeeds."""

    provider = "flaky"
    model = "flaky-v1"

    def __init__(self, failures: int, message: str = "ThrottlingException: rate exceeded") -> None:
        self.failures = failures
        self.calls = 0
        self.message = message

    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(self.message)
        return LLMResponse(text='{"symptoms": []}', provider=self.provider, model=self.model)


class SlowClient(BaseLLMClient):
    provider = "slow"
    model = "slow-v1"

    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        await asyncio.sleep(self.delay)
        return LLMResponse(text="ok", provider=self.provider, model=self.model)


def test_prompt_contains_persona_and_schema(cohort, protocol) -> None:
    profile = cohort[0]
    prompt = build_patient_prompt(
        profile,
        arm_label="INS-201 150 mg BID",
        dose_mg=150.0,
        epoch=3,
        sbp=128.0,
        dbp=78.0,
        hr=70.0,
        qtc_ms=415.0,
        alt_u_l=30.0,
        ae_probabilities={"headache": 0.2, "dizziness": 0.05},
    )
    assert "JSON" in prompt.user
    assert "ctcae_grade" in prompt.user
    assert "headache" in prompt.user
    assert prompt.prompt_hash == build_patient_prompt(
        profile,
        arm_label="INS-201 150 mg BID",
        dose_mg=150.0,
        epoch=3,
        sbp=128.0,
        dbp=78.0,
        hr=70.0,
        qtc_ms=415.0,
        alt_u_l=30.0,
        ae_probabilities={"headache": 0.2, "dizziness": 0.05},
    ).prompt_hash


def test_parse_plain_json() -> None:
    parsed = parse_symptom_response('{"symptoms": [{"term": "headache", "ctcae_grade": 2, "verbatim": "throbbing"}]}')
    assert parsed.ok
    assert parsed.symptoms[0].term == "headache"
    assert parsed.symptoms[0].ctcae_grade == 2


def test_parse_markdown_fenced_json() -> None:
    text = 'Here is my answer:\n```json\n{"symptoms": [{"term": "nausea", "ctcae_grade": "moderate"}]}\n```'
    parsed = parse_symptom_response(text)
    assert parsed.ok
    assert parsed.symptoms[0].ctcae_grade == 2


def test_parse_repairs_trailing_commas_and_single_quotes() -> None:
    text = "{'symptoms': [{'term': 'fatigue', 'ctcae_grade': 1,},],}"
    parsed = parse_symptom_response(text)
    assert parsed.ok
    assert parsed.symptoms[0].term == "fatigue"


def test_parse_bare_array_and_extra_prose() -> None:
    parsed = parse_symptom_response('Sure! [{"term": "cough", "grade": 1}] — hope that helps.')
    assert parsed.ok
    assert parsed.symptoms[0].term == "cough"


def test_parse_rejects_medical_advice() -> None:
    parsed = parse_symptom_response('{"symptoms": [], "note": "You should take ibuprofen and rest"}')
    assert not parsed.ok
    assert "safety filter" in parsed.parse_error
    assert parsed.symptoms == []


def test_parse_reports_unparseable_output() -> None:
    parsed = parse_symptom_response("I feel a bit dizzy today.")
    assert not parsed.ok
    assert parsed.symptoms == []
    assert "unparseable" in parsed.parse_error


def test_parse_empty_response() -> None:
    parsed = parse_symptom_response("")
    assert not parsed.ok
    assert parsed.parse_error == "empty response"


def test_parse_clamps_grade_and_limits_count() -> None:
    payload = {"symptoms": [{"term": f"term{i}", "ctcae_grade": 99} for i in range(9)]}
    parsed = parse_symptom_response(json.dumps(payload))
    assert len(parsed.symptoms) == 5
    assert all(symptom.ctcae_grade == 5 for symptom in parsed.symptoms)


def test_offline_provider_emits_valid_json(cohort) -> None:
    client = OfflineLLMClient()
    request = LLMRequest(
        prompt="ignored",
        context={"ae_probabilities": {"headache": 0.4, "dizziness": 0.2}, "epoch": 3, "worst_grade": 2},
    )
    response = asyncio.run(client.acomplete(request))
    parsed = parse_symptom_response(response.text)
    assert parsed.ok
    assert {s.term for s in parsed.symptoms} == {"headache", "dizziness"}


def test_cache_round_trip(tmp_path) -> None:
    path = tmp_path / "cache.jsonl"
    cache = LLMResponseCache(path)
    key = cache_key(provider="offline", model="m", temperature=0.2, prompt="hello")
    from insilico_trial_mas.llm.cache import CacheEntry

    cache.put(CacheEntry(key=key, text="answer", provider="offline", model="m"))
    assert cache.get(key) is not None
    assert cache.get(key).text == "answer"
    reloaded = LLMResponseCache(path)
    assert reloaded.get(key).text == "answer"
    assert reloaded.stats()["hits"] == 1


def test_cache_disabled_returns_nothing(tmp_path) -> None:
    cache = LLMResponseCache(tmp_path / "x.jsonl", enabled=False)
    from insilico_trial_mas.llm.cache import CacheEntry

    cache.put(CacheEntry(key="k", text="t", provider="p", model="m"))
    assert cache.get("k") is None


def test_token_bucket_throttles() -> None:
    async def scenario() -> float:
        bucket = AsyncTokenBucket(rate_per_second=50.0, burst=1)
        start = time.perf_counter()
        for _ in range(3):
            await bucket.acquire()
        return time.perf_counter() - start

    elapsed = asyncio.run(scenario())
    assert elapsed >= 0.02, "the bucket must delay requests beyond the burst"


def test_token_bucket_try_acquire() -> None:
    bucket = AsyncTokenBucket(rate_per_second=1.0, burst=1)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_retry_policy_backoff_is_capped() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=1.0, max_delay_seconds=4.0, jitter=False)
    assert policy.delay_for(1) == 1.0
    assert policy.delay_for(2) == 2.0
    assert policy.delay_for(10) == 4.0


def test_retryable_classification() -> None:
    assert is_retryable(RuntimeError("ThrottlingException: rate exceeded"))
    assert is_retryable(TimeoutError("timed out"))
    assert not is_retryable(RuntimeError("ValidationException: bad model id"))
    assert not is_retryable(RuntimeError("AccessDeniedException"))


def test_resilient_client_retries_then_succeeds() -> None:
    inner = FlakyClient(failures=2)
    client = ResilientLLMClient(inner, retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.001), limiter_enabled=False)
    response = asyncio.run(client.acomplete(LLMRequest(prompt="x")))
    assert inner.calls == 3
    assert response.attempts == 3
    assert client.stats.retries == 2


def test_resilient_client_uses_cache(tmp_path) -> None:
    inner = FlakyClient(failures=0)
    cache = LLMResponseCache(tmp_path / "c.jsonl")
    client = ResilientLLMClient(inner, cache=cache, limiter_enabled=False)
    first = asyncio.run(client.acomplete(LLMRequest(prompt="same")))
    second = asyncio.run(client.acomplete(LLMRequest(prompt="same")))
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert inner.calls == 1


def test_resilient_client_falls_back_to_offline_provider() -> None:
    inner = FlakyClient(failures=99, message="ValidationException: unknown model")
    fallback = OfflineLLMClient()
    client = ResilientLLMClient(
        inner, retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0.001), fallback=fallback, limiter_enabled=False
    )
    response = asyncio.run(
        client.acomplete(LLMRequest(prompt="x", context={"ae_probabilities": {"headache": 0.5}, "worst_grade": 1}))
    )
    assert response.provider == "offline"
    assert "fallback" in response.error
    assert client.stats.fallbacks == 1


def test_resilient_client_raises_without_fallback() -> None:
    client = ResilientLLMClient(
        FlakyClient(failures=99, message="AccessDeniedException"),
        retry_policy=RetryPolicy(max_attempts=1),
        limiter_enabled=False,
    )
    with pytest.raises(LLMProviderError):
        asyncio.run(client.acomplete(LLMRequest(prompt="x")))
    assert client.stats.failures == 1


def test_resilient_client_times_out() -> None:
    client = ResilientLLMClient(
        SlowClient(delay=0.5),
        retry_policy=RetryPolicy(max_attempts=1),
        timeout_seconds=0.05,
        limiter_enabled=False,
    )
    with pytest.raises(LLMProviderError):
        asyncio.run(client.acomplete(LLMRequest(prompt="x")))


def test_factory_returns_offline_client_for_offline_provider() -> None:
    client = create_llm_client(LLMConfig(provider="offline", cache_enabled=False))
    assert client.provider == "offline"
    assert client.limiter_enabled is False


def test_factory_supports_echo_provider() -> None:
    client = create_llm_client(LLMConfig(provider="echo", cache_enabled=False))
    assert isinstance(client.inner, EchoLLMClient)


def test_factory_requires_optional_dependency_for_bedrock() -> None:
    config = LLMConfig(provider="bedrock", model="us.anthropic.claude-3-5-sonnet-20241022-v2:0")
    try:
        create_llm_client(config, cache=LLMResponseCache(None))
    except Exception as exc:  # pragma: no cover - depends on optional extras being installed
        from insilico_trial_mas.errors import ConfigurationError

        assert isinstance(exc, ConfigurationError)
        assert "langchain" in str(exc) or "boto3" in str(exc)
