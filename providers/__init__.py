"""Provider LLM. Saat ini hanya OpenAI-compatible (MIDAS, Keton, Vikey, dll)."""

from .open_ai_compatible import Completion, OpenAICompatibleProvider, ProviderError

__all__ = ["Completion", "OpenAICompatibleProvider", "ProviderError", "build_provider"]


def build_provider(config) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        host=config.host,
        key=config.key,
        model=config.model,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        timeout=config.timeout,
    )
