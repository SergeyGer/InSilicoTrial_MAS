"""LangChain-backed chat providers (Amazon Bedrock / OpenAI / any LangChain chat model).

The specification's blueprint pinned ``langchain_community.chat_models.BedrockChat``
with ``beta_use_converse_api=True`` and the model id
``anthropic.claude-3-sonnet-20240229-v1:0``. Three problems with that:

* ``langchain_community`` chat models are deprecated in favour of
  ``langchain_aws`` (and ``beta_use_converse_api`` no longer exists);
* that Claude 3 Sonnet model id has reached end of life on Bedrock, and
  cross-region inference now requires an inference-profile id
  (``us.anthropic.claude-3-5-sonnet-20241022-v2:0``);
* the client was constructed per row instead of per partition.

Here the model is built once per worker partition, the model id is configuration
(never hardcoded) and the LCEL chain is exposed through :meth:`build_chain` for
inspection/tracing while :meth:`acomplete` calls the model directly so that token
usage metadata is preserved.
"""

from __future__ import annotations

import time
from typing import Any

from ..errors import ConfigurationError, LLMProviderError
from ..logging_utils import get_logger
from .base import BaseLLMClient, LLMRequest, LLMResponse

logger = get_logger("llm.langchain")

DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def _require(module: str, hint: str) -> Any:
    try:
        return __import__(module, fromlist=["*"])
    except ImportError as exc:
        raise ConfigurationError(
            f"provider requires the optional dependency {module!r} ({hint}); "
            f"install it with: pip install 'insilico-trial-mas[llm]'"
        ) from exc


def resolve_chat_model(config) -> tuple[Any, str, str]:
    """Build a LangChain chat model for the configured provider.

    Returns ``(chat_model, provider_name, model_id)``.
    """
    provider = config.provider
    if provider in {"bedrock", "langchain"} and not config.model:
        config.model = DEFAULT_BEDROCK_MODEL
    if provider == "openai" and not config.model:
        config.model = DEFAULT_OPENAI_MODEL

    if provider == "bedrock":
        langchain_aws = _require("langchain_aws", "pip install langchain-aws boto3")
        options: dict[str, Any] = {
            "model_id": config.model,
            "region_name": config.region,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        options.update(config.options or {})
        options = {k: v for k, v in options.items() if v is not None}
        return langchain_aws.ChatBedrock(**options), "bedrock", config.model

    if provider == "openai":
        langchain_openai = _require("langchain_openai", "pip install langchain-openai")
        options = {
            "model": config.model,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "timeout": config.timeout_seconds,
        }
        if config.endpoint_url:
            options["base_url"] = config.endpoint_url
        options.update(config.options or {})
        options = {k: v for k, v in options.items() if v is not None}
        return langchain_openai.ChatOpenAI(**options), "openai", config.model

    if provider == "langchain":
        # Bring-your-own chat model: `options.chat_model` is a LangChain Runnable.
        custom = (config.options or {}).get("chat_model")
        if custom is None:
            raise ConfigurationError("provider 'langchain' requires options.chat_model (a LangChain chat model)")
        model_id = getattr(custom, "model_name", "") or getattr(custom, "model", "") or "custom"
        return custom, "langchain", str(model_id)

    raise ConfigurationError(f"unsupported LangChain provider: {provider!r}")


class LangChainChatClient(BaseLLMClient):
    """Thin async wrapper over a LangChain chat model."""

    def __init__(self, chat_model: Any, *, provider: str, model: str, temperature: float = 0.2) -> None:
        self._model = chat_model
        self.provider = provider
        self.model = model
        self.temperature = temperature

    @classmethod
    def from_config(cls, config) -> LangChainChatClient:
        model, provider, model_id = resolve_chat_model(config)
        return cls(model, provider=provider, model=model_id, temperature=config.temperature)

    def build_chain(self, prompt_template: Any):
        """Return ``prompt | model | StrOutputParser()`` (LCEL, as in the spec).

        Exposed for notebooks, tracing and unit tests; the hot path uses
        :meth:`acomplete` so that token usage metadata is not lost.
        """
        from langchain_core.output_parsers import StrOutputParser

        return prompt_template | self._model | StrOutputParser()

    async def acomplete(self, request: LLMRequest) -> LLMResponse:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages: list[Any] = []
        if request.system:
            messages.append(SystemMessage(content=request.system))
        messages.append(HumanMessage(content=request.prompt))
        start = time.perf_counter()
        try:
            message = await self._model.ainvoke(messages)
        except Exception as exc:
            raise LLMProviderError(f"{self.provider} invocation failed: {exc}") from exc

        text = self._content_to_text(message)
        usage = getattr(message, "usage_metadata", None) or {}
        response_meta = getattr(message, "response_metadata", None) or {}
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=str(response_meta.get("model_id") or response_meta.get("model_name") or self.model),
            tokens_in=int(usage.get("input_tokens", 0) or self.estimate_tokens(request.prompt)),
            tokens_out=int(usage.get("output_tokens", 0) or self.estimate_tokens(text)),
            latency_ms=self._timed(start),
            raw={"prompt_hash": request.context.get("prompt_hash", ""), "response_metadata": _jsonable(response_meta)},
        )

    @staticmethod
    def _content_to_text(message: Any) -> str:
        """Normalise LangChain message content (str or list of content blocks)."""
        content = getattr(message, "content", message)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    if isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    elif block.get("type") == "text" and isinstance(block.get("content"), str):
                        parts.append(block["content"])
            return "".join(parts)
        return str(content)


def _jsonable(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(k): (v if isinstance(v, (str, int, float, bool, type(None))) else str(v)) for k, v in value.items()}
