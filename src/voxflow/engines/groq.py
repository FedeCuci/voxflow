"""Groq transcription - whisper-large-v3-turbo, the fastest hosted option."""

from __future__ import annotations

from .base import OpenAICompatible


class GroqEngine(OpenAICompatible):
    name = "groq"
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    default_model = "whisper-large-v3-turbo"
    secret_field = "groq_api_key"
    signup_url = "https://console.groq.com/keys"
