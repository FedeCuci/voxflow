"""Transcription engine registry."""

from __future__ import annotations

from .base import Engine, EngineError, OpenAICompatible
from .groq import GroqEngine
from .openai import OpenAIEngine

ENGINES: dict[str, type[Engine]] = {
    GroqEngine.name: GroqEngine,
    OpenAIEngine.name: OpenAIEngine,
}


def get_engine(provider: str) -> Engine:
    try:
        return ENGINES[provider.strip().lower()]()
    except KeyError as exc:
        known = ", ".join(sorted(ENGINES))
        raise EngineError(f"unknown provider {provider!r}; expected one of: {known}") from exc


__all__ = [
    "ENGINES",
    "Engine",
    "EngineError",
    "GroqEngine",
    "OpenAICompatible",
    "OpenAIEngine",
    "get_engine",
]
