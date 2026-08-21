from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Protocol


class LLMProvider(Protocol):
    debug_log: list[dict[str, Any]]

    def parse_intent(self, text: str, schema: dict[str, Any]) -> dict[str, Any]:
        ...

    def phrase(self, source: dict[str, Any]) -> str:
        ...


SECRET_PATTERNS = [re.compile(r"AIza[0-9A-Za-z_-]+"), re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")]


class GroqLLM:
    def __init__(self, model: str | None = None, api_key: str | None = None, endpoint: str | None = None):
        self.model = model or os.environ.get("INSTANTIATION_AGENT_GROQ_MODEL", "llama-3.3-70b-versatile")
        self.api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        self.endpoint = endpoint or "https://api.groq.com/openai/v1/chat/completions"
        self.debug_log: list[dict[str, Any]] = []

    def parse_intent(self, text: str, schema: dict[str, Any]) -> dict[str, Any]:
        self._assert_no_secret(text)
        prompt = "Return only strict JSON matching this schema. Do not include prose.\nSchema:\n"
        prompt += json.dumps(schema, sort_keys=True) + "\nUser request:\n" + text
        content = self._chat(prompt)
        return strict_json_from_text(content)

    def phrase(self, source: dict[str, Any]) -> str:
        self._assert_no_secret(json.dumps(source, sort_keys=True))
        prompt = "Phrase this result for a user without changing any numbers, ids, URLs, roles, or field values:\n"
        content = self._chat(prompt + json.dumps(source, sort_keys=True))
        for value in _must_preserve_values(source):
            if str(value) not in content:
                return deterministic_phrase(source)
        return content

    def _chat(self, prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is required for live LLM calls")
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
        self.debug_log.append({"request": _redact(body)})
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "curl/8.4.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            self.debug_log.append({"response": {"error": f"HTTP {exc.code}", "body": _redact_text(detail)}})
            raise RuntimeError(f"Groq request failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            self.debug_log.append({"response": {"error": _redact_text(str(exc))}})
            raise RuntimeError(f"Groq request failed: {exc.reason}") from exc
        self.debug_log.append({"response": _redact(payload)})
        return payload["choices"][0]["message"]["content"].strip()

    def _assert_no_secret(self, text: str) -> None:
        if self.api_key and self.api_key in text:
            raise ValueError("secret value attempted to enter LLM context")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            raise ValueError("credential-looking value attempted to enter LLM context")


def deterministic_phrase(source: dict[str, Any]) -> str:
    return json.dumps(source, indent=2, sort_keys=True)


def strict_json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM did not return a JSON object")
    return value


def _must_preserve_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        result: list[Any] = []
        for child in value.values():
            result.extend(_must_preserve_values(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_must_preserve_values(child))
        return result
    if isinstance(value, (str, int, float)) and str(value):
        return [value]
    return []


def _redact(value: Any) -> Any:
    return json.loads(_redact_text(json.dumps(value, sort_keys=True)))


def _redact_text(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = re.sub(r"Bearer [A-Za-z0-9._-]+", "Bearer [REDACTED]", text)
    return text
