"""OpenAI transcription."""

from __future__ import annotations

from .base import OpenAICompatible


class OpenAIEngine(OpenAICompatible):
    name = "openai"
    url = "https://api.openai.com/v1/audio/transcriptions"
    # whisper-1 is the broadly-available default; gpt-4o-transcribe and
    # gpt-4o-mini-transcribe also work if your account has them.
    default_model = "whisper-1"
    secret_field = "openai_api_key"
    signup_url = "https://platform.openai.com/api-keys"
