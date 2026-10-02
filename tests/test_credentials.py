"""Tests for credential discovery and the credential-free default path.

The platform's promise is that it runs end to end with **no credentials** while
still being able to use real providers when they are configured. These tests pin
both halves of that promise: detection reports presence (never values), the
offline provider needs nothing, and a credentialed provider without credentials
gets an actionable warning instead of an opaque SDK error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from insilico_trial_mas.config import LLMConfig
from insilico_trial_mas.credentials import (
    CREDENTIAL_SOURCES,
    detect_credentials,
    missing_credentials_hint,
    provider_has_credentials,
)
from insilico_trial_mas.engine.checklist import inspect_environment
from insilico_trial_mas.errors import ConfigurationError, LLMProviderError

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every variable the discovery logic may look at - cleared before each test so
#: the developer's own environment cannot make the suite pass or fail.
CREDENTIAL_VARIABLES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_CONFIG_PROFILE",
    "MLFLOW_TRACKING_URI",
    "MLFLOW_TRACKING_USERNAME",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_nothing_is_detected_in_a_bare_environment() -> None:
    detected = detect_credentials()
    assert detected["aws-bedrock"] is False
    assert detected["openai"] is False
    assert detected["databricks"] is False
    assert detected["mlflow"] is False


def test_static_aws_keys_are_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    assert detect_credentials()["aws-bedrock"] is True


def test_incomplete_aws_keys_are_not_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    assert detect_credentials()["aws-bedrock"] is False


def test_aws_role_and_profile_alternatives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_PROFILE", "insilico")
    assert detect_credentials()["aws-bedrock"] is True
    monkeypatch.delenv("AWS_PROFILE")
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/bedrock")
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/var/run/secrets/token")
    assert detect_credentials()["aws-bedrock"] is True


def test_databricks_requires_a_host_and_one_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://dbc-example.cloud.databricks.com")
    assert detect_credentials()["databricks"] is False
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-example")
    assert detect_credentials()["databricks"] is True
    monkeypatch.delenv("DATABRICKS_TOKEN")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-id")
    assert detect_credentials()["databricks"] is True


def test_mlflow_and_openai_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "databricks")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-example")
    detected = detect_credentials()
    assert detected["mlflow"] is True and detected["openai"] is True


def test_detection_never_returns_values(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "AKIA-SUPER-SECRET-VALUE"
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", secret)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", secret)
    detected = detect_credentials()
    assert all(isinstance(value, bool) for value in detected.values()), "only booleans may be returned"
    assert secret not in repr(detected)


def test_provider_helpers() -> None:
    assert provider_has_credentials("offline") is True
    assert provider_has_credentials("bedrock") is False
    hint = missing_credentials_hint("bedrock")
    assert "AWS_PROFILE" in hint and "offline" in hint
    assert "OPENAI_API_KEY" in missing_credentials_hint("openai")


def test_credential_sources_are_documented_in_the_template() -> None:
    """`.env.example` must mention every variable the discovery logic reads."""
    template = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for _, required, alternatives in CREDENTIAL_SOURCES:
        for name in (*required, *alternatives):
            assert name in template, f"{name} is missing from .env.example"
    for name in ("DATABRICKS_HOST", "MLFLOW_TRACKING_URI", "JAVA_HOME"):
        assert name in template
    # A template must not carry values.
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        assert value == "" or key.startswith("INSILICO_"), f"{key} ships with a value: {value!r}"


def test_env_check_reports_credentials_without_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-appear")
    report = inspect_environment()
    payload = report.as_dict()
    assert payload["credentials"]["openai"] is True
    rendered = report.render()
    assert "credentials" in rendered
    assert "sk-must-not-appear" not in rendered
    assert "presence only" in rendered


def test_env_check_notes_the_credential_free_path() -> None:
    report = inspect_environment()
    assert any("offline" in note for note in report.notes), "the offline fallback must be explained"


def test_factory_warns_before_failing_on_a_credentialed_provider() -> None:
    """A missing credential chain must be named before the provider is constructed.

    The package logger does not propagate to the root logger (executor logs stay
    machine-parseable), so the test attaches its own handler instead of using
    ``caplog``. A warning - not an error - is correct here: on EC2 or Databricks
    the instance role supplies credentials that no environment variable reveals.
    """
    import logging

    from insilico_trial_mas.llm.factory import create_llm_client
    from insilico_trial_mas.logging_utils import get_logger

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = get_logger("llm.factory")
    logger.addHandler(handler)
    try:
        # Without the optional [llm] extra the provider cannot even be built, so the
        # run ends in ConfigurationError (or an SDK error when langchain-aws is
        # installed) - what matters is that the warning was logged first.
        with pytest.raises((ConfigurationError, LLMProviderError, OSError, ValueError)):
            create_llm_client(LLMConfig(provider="bedrock", model="us.anthropic.example", cache_enabled=False))
    finally:
        logger.removeHandler(handler)

    messages = [record.getMessage() for record in records]
    assert any("no credentials were detected" in message for message in messages), messages
    assert any("AWS_PROFILE" in message for message in messages)


def test_offline_provider_needs_no_credentials(tmp_path: Path) -> None:
    from insilico_trial_mas.llm.factory import create_llm_client

    client = create_llm_client(LLMConfig(provider="offline", cache_enabled=False, cache_path=str(tmp_path / "cache.jsonl")))
    assert client.provider == "offline"


def test_error_kind_never_returns_provider_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Log lines and Silver columns get a fixed label, never the provider message.

    Guards CodeQL ``py/clear-text-logging-sensitive-data``: SDK errors can echo the
    prompt or the API key, so ``error_kind`` returns a constant from a closed
    vocabulary and ``debug_error_text`` (exception chaining only) is the sole way to
    see the message.
    """
    from insilico_trial_mas.logging_utils import ERROR_KINDS, debug_error_text, error_kind

    secret = "sk-live-abcdef123456"
    error = TimeoutError(f"Bedrock timed out for key {secret}")

    kind = error_kind(error)
    assert kind == "timeout"
    assert secret not in kind
    assert kind in {label for _, label in ERROR_KINDS}, "the label must come from the closed vocabulary"
    assert error_kind(RuntimeError("boom")) == "unexpected-error"
    assert error_kind(ValueError("bad json")) == "invalid-value"

    # The developer-facing text exists, but is only sanctioned inside an exception.
    assert secret in debug_error_text(error)


def test_patient_agent_does_not_log_provider_text() -> None:
    """The persona agent must log a label and store it in ``llm_error``."""
    source = (REPO_ROOT / "src" / "insilico_trial_mas" / "agents" / "patient_agent.py").read_text(encoding="utf-8")
    assert "error_kind(exc)" in source
    for forbidden in ("{exc}", "{summary}", "debug_error_text"):
        assert forbidden not in source, f"{forbidden} would put provider text into a log line"
