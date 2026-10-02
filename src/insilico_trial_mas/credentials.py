"""Provider credential discovery - presence only, never values.

The platform talks to external providers (Amazon Bedrock, OpenAI, the Databricks
API, an MLflow tracking server) but it must also run with **no credentials at
all**: CI, a laptop, a Databricks Community Edition notebook and the offline demo
all work because the default LLM provider is deterministic and local.

This module answers one question for the rest of the code: *is a credential chain
configured for this provider?* It deliberately reports booleans, so the answer can
be printed in ``insilico-trial env-check``, embedded in a run manifest or pasted
into a bug report without leaking a secret. Nothing here reads, logs or returns a
credential value.

Credential sources follow each SDK's own resolution order, which is why the
platform does not ship a loader of its own:

* **AWS / Bedrock** - ``AWS_ACCESS_KEY_ID`` + ``AWS_SECRET_ACCESS_KEY``,
  ``AWS_PROFILE``, ``AWS_ROLE_ARN``, ``AWS_WEB_IDENTITY_TOKEN_FILE``, or the
  instance/container role that boto3 picks up automatically.
* **OpenAI** - ``OPENAI_API_KEY`` (optionally ``OPENAI_BASE_URL``).
* **Databricks** - ``DATABRICKS_HOST`` plus ``DATABRICKS_TOKEN`` or
  ``DATABRICKS_CLIENT_ID``/``DATABRICKS_CLIENT_SECRET`` (OAuth M2M).
* **MLflow** - ``MLFLOW_TRACKING_URI`` (optionally with basic-auth variables) or
  ``mlflow.set_tracking_uri`` through the simulation configuration.
"""

from __future__ import annotations

import os

#: ``(label, required env vars, alternative env vars)``. The label is what appears
#: in ``env-check`` output and in the run manifest.
CREDENTIAL_SOURCES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "aws-bedrock",
        ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        ("AWS_PROFILE", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE"),
    ),
    ("openai", ("OPENAI_API_KEY",), ("OPENAI_BASE_URL",)),
)

#: Provider names accepted by ``llm.provider`` that need credentials, mapped to
#: the label used by :func:`detect_credentials` (the label is not always the
#: provider name: Bedrock authenticates with the AWS chain).
PROVIDER_CREDENTIAL_LABEL: dict[str, str] = {"bedrock": "aws-bedrock", "openai": "openai"}


def detect_credentials() -> dict[str, bool]:
    """Report which provider credential chains are configured in this process."""
    detected: dict[str, bool] = {}
    for label, required, alternatives in CREDENTIAL_SOURCES:
        if all(os.environ.get(name) for name in required):
            detected[label] = True
        else:
            detected[label] = any(os.environ.get(name) for name in alternatives)

    detected["databricks"] = bool(os.environ.get("DATABRICKS_HOST")) and any(
        os.environ.get(name) for name in ("DATABRICKS_TOKEN", "DATABRICKS_CLIENT_ID", "DATABRICKS_CONFIG_PROFILE")
    )
    detected["mlflow"] = bool(os.environ.get("MLFLOW_TRACKING_URI"))
    detected["bedrock_region"] = bool(os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    return detected


def credential_sources_for(provider: str) -> tuple[str, ...]:
    """Human-readable list of what a provider accepts, for error messages."""
    if provider == "bedrock":
        return (
            "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY",
            "AWS_PROFILE (a named profile in ~/.aws/credentials)",
            "AWS_ROLE_ARN (+ AWS_WEB_IDENTITY_TOKEN_FILE) for IRSA/EKS",
            "the EC2 instance profile or Databricks cluster IAM role",
        )
    if provider == "openai":
        return ("OPENAI_API_KEY", "OPENAI_API_KEY + OPENAI_BASE_URL for an Azure/proxy endpoint")
    if provider == "langchain":
        return ("options.chat_model: a pre-built LangChain chat model instance",)
    return ("no credentials required for the offline provider",)


def provider_has_credentials(provider: str) -> bool:
    """True when ``provider`` needs no credentials or its chain is configured."""
    label = PROVIDER_CREDENTIAL_LABEL.get(provider)
    if label is None:
        return True
    return bool(detect_credentials().get(label))


def missing_credentials_hint(provider: str) -> str:
    """Actionable message for a provider whose credentials are absent.

    It names the accepted environment variables *and* the way out (the offline
    provider), because the most common support question is "why did my persona
    narration silently degrade?".
    """
    sources = ", ".join(credential_sources_for(provider))
    return (
        f"llm.provider={provider!r} is configured but no credentials were detected. "
        f"Provide one of: {sources}. Alternatively set llm.provider=offline for a "
        f"deterministic, credential-free run with the same JSON contract."
    )
