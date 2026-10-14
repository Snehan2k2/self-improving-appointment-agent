"""Minimal OpenRouter chat client (OpenAI-compatible /chat/completions).

- Default model is the free Gemma 4; if the free model is rate limited, the same
  model's paid endpoint is used for the rest of the process.
- Retries on rate limits, server errors and timeouts.
- Returns a clean assistant message dict that can be appended to `messages` as-is.
"""

import os
import time
from pathlib import Path

import requests

URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemma-4-26b-a4b-it:free"
DEFAULT_FALLBACK = "google/gemma-4-26b-a4b-it"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path = ENV_PATH) -> None:
    """Read KEY=VALUE lines from .env into os.environ (existing env vars win)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class LLMError(RuntimeError):
    pass


class OpenRouterLLM:
    def __init__(self, model: str | None = None, fallback_model: str | None = None,
                 reasoning: bool = True, temperature: float | None = None,
                 max_retries: int = 4, timeout: int = 120):
        load_env()
        self.api_key = os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise LLMError("OPENROUTER_API_KEY is not set. Copy .env.example to .env and add your key.")
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self.fallback_model = fallback_model or os.environ.get("LLM_FALLBACK_MODEL", DEFAULT_FALLBACK)
        self.reasoning = reasoning
        self.temperature = temperature
        self.max_retries = max_retries
        self.timeout = timeout
        self.use_fallback = False   # flips to True once the free model is rate limited
        self.calls = []             # one entry per API call: model used + token usage

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        body = {"model": self._current_model(), "messages": messages}
        if tools:
            body["tools"] = tools
        if self.reasoning:
            body["reasoning"] = {"enabled": True}
        if self.temperature is not None:
            body["temperature"] = self.temperature

        last_error = ""
        for attempt in range(self.max_retries + 1):
            body["model"] = self._current_model()
            try:
                r = requests.post(URL, json=body, timeout=self.timeout,
                                  headers={"Authorization": f"Bearer {self.api_key}"})
            except requests.RequestException as e:
                last_error = f"network error: {e}"
                self._sleep(attempt)
                continue

            if r.status_code == 429:
                last_error = "rate limited (429)"
                if body["model"].endswith(":free") and self.fallback_model:
                    self.use_fallback = True        # free quota used up -> paid same model
                    continue
                self._sleep(attempt)
                continue
            if r.status_code >= 500:
                last_error = f"server error {r.status_code}"
                self._sleep(attempt)
                continue
            if r.status_code != 200:
                raise LLMError(f"OpenRouter {r.status_code}: {r.text[:500]}")

            data = r.json()
            if "error" in data or not data.get("choices"):
                last_error = f"provider error: {str(data.get('error', data))[:300]}"
                self._sleep(attempt)
                continue

            self.calls.append({"model": data.get("model", body["model"]), "usage": data.get("usage", {})})
            return _clean_message(data["choices"][0]["message"])

        raise LLMError(f"OpenRouter call failed after {self.max_retries + 1} attempts: {last_error}")

    def _current_model(self) -> str:
        return self.fallback_model if self.use_fallback else self.model

    @staticmethod
    def _sleep(attempt: int) -> None:
        time.sleep(min(2 ** attempt, 30))


def _clean_message(msg: dict) -> dict:
    """Keep only the fields we send back next turn. reasoning_details is passed back unmodified."""
    out = {"role": "assistant", "content": msg.get("content")}
    if msg.get("tool_calls"):
        out["tool_calls"] = msg["tool_calls"]
    if msg.get("reasoning_details"):
        out["reasoning_details"] = msg["reasoning_details"]
    return out
