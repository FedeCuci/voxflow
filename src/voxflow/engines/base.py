"""Transcription backends.

Both supported providers expose the same OpenAI-compatible
/audio/transcriptions endpoint, so one implementation covers them; the
subclasses only supply a URL, a default model, and which secret to read.
Keeping this behind an interface leaves room for a local whisper.cpp or
faster-whisper backend later without touching the daemon.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class EngineError(RuntimeError):
    pass


class Engine:
    name = "engine"
    default_model = ""
    secret_field = ""
    signup_url = ""

    def transcribe(
        self,
        wav: bytes,
        api_key: str,
        model: str = "",
        language: str = "",
        prompt: str = "",
        timeout: float = 60.0,
    ) -> str:
        raise NotImplementedError


class OpenAICompatible(Engine):
    url = ""

    def transcribe(
        self,
        wav: bytes,
        api_key: str,
        model: str = "",
        language: str = "",
        prompt: str = "",
        timeout: float = 60.0,
    ) -> str:
        if not api_key:
            raise EngineError(
                f"no API key for {self.name}. Set it in `voxflow settings` "
                f"or {self.secret_field} in secrets.toml."
            )
        data = {
            "model": model or self.default_model,
            "response_format": "json",
            "temperature": "0",
        }
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt

        try:
            response = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("dictation.wav", wav, "audio/wav")},
                data=data,
                timeout=timeout,
            )
        except requests.Timeout as exc:
            raise EngineError(f"{self.name} timed out after {timeout:.0f}s") from exc
        except requests.RequestException as exc:
            raise EngineError(f"could not reach {self.name}: {exc}") from exc

        if response.status_code != 200:
            raise EngineError(self._error_message(response))
        try:
            payload = response.json()
        except ValueError as exc:
            raise EngineError(f"{self.name} returned invalid JSON") from exc
        return str(payload.get("text", "")).strip()

    def _error_message(self, response: requests.Response) -> str:
        detail = ""
        try:
            body = response.json()
            error = body.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            elif isinstance(error, str):
                detail = error
        except ValueError:
            detail = response.text[:200]
        if response.status_code == 401:
            detail = detail or "invalid API key"
        elif response.status_code == 429:
            detail = detail or "rate limited"
        return f"{self.name} error {response.status_code}: {detail or 'unknown'}"
