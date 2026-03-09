"""LLM provider abstraction module with lazy imports for heavy provider clients."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanobot.providers.base import LLMProvider, LLMResponse

if TYPE_CHECKING:
    from nanobot.providers.azure_openai_provider import AzureOpenAIProvider
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "LiteLLMProvider",
    "OpenAICodexProvider",
    "AzureOpenAIProvider",
]


def __getattr__(name: str):
    """Load heavy provider implementations only when they are actually used."""
    if name == "LiteLLMProvider":
        from nanobot.providers.litellm_provider import LiteLLMProvider

        return LiteLLMProvider
    if name == "OpenAICodexProvider":
        from nanobot.providers.openai_codex_provider import OpenAICodexProvider

        return OpenAICodexProvider
    if name == "AzureOpenAIProvider":
        from nanobot.providers.azure_openai_provider import AzureOpenAIProvider

        return AzureOpenAIProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
