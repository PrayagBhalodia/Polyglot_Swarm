"""A stdlib-only Groq client (OpenAI-compatible chat completions).

No third-party SDK: requests go over :mod:`urllib` so the project stays
dependency-free. The HTTP transport is injectable, so request-building, retry
handling, and response-parsing are all tested without a network or API key —
only real runs reach ``api.groq.com``.

Rate limits are per Groq *account*, not per key, so many agents sharing one key
is expected; ``429`` (and transient ``5xx``/network errors) are retried with
exponential backoff before surfacing as a :class:`GroqError`.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from config.settings import GroqConfig
from core.errors import PolyglotSwarmError

# (url, body, headers, timeout) -> (status, response_body)
Transport = Callable[[str, bytes, Mapping[str, str], float], tuple[int, bytes]]

_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 3
_BACKOFF_CAP_SECONDS = 8.0


class GroqError(PolyglotSwarmError):
    """Raised when a Groq API call fails after exhausting retries."""


@dataclass(frozen=True, slots=True)
class Completion:
    """One chat completion: the model's text plus tokens billed."""

    text: str
    tokens: int


class CompletionClient(Protocol):
    """The narrow surface the Brain seams depend on (eases faking in tests)."""

    def complete(
        self, *, system: str, user: str, model: str | None = None
    ) -> Completion: ...


class GroqClient:
    """Calls Groq's ``/chat/completions`` endpoint over stdlib ``urllib``."""

    def __init__(
        self,
        config: GroqConfig,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._api_key = config.require_api_key()  # fails fast if unset
        self._transport = transport or _urllib_transport
        self._sleep = sleep

    def complete(
        self, *, system: str, user: str, model: str | None = None
    ) -> Completion:
        """Run one chat completion, retrying transient failures."""
        url = self._config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model or self._config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = float(self._config.request_timeout_seconds)

        last_error = "unknown error"
        for attempt in range(_MAX_RETRIES + 1):
            try:
                status, body = self._transport(url, data, headers, timeout)
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = f"network error: {exc}"
                if attempt < _MAX_RETRIES:
                    self._sleep(_backoff(attempt))
                    continue
                raise GroqError(last_error) from exc

            if status == 200:
                return _parse_completion(body)

            last_error = _error_detail(status, body)
            if status in _RETRYABLE and attempt < _MAX_RETRIES:
                self._sleep(_backoff(attempt))
                continue
            raise GroqError(last_error)

        raise GroqError(last_error)  # pragma: no cover - loop always returns/raises


def _backoff(attempt: int) -> float:
    return min(0.5 * float(2**attempt), _BACKOFF_CAP_SECONDS)


def _urllib_transport(
    url: str, data: bytes, headers: Mapping[str, str], timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=data, headers=dict(headers), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        # Return the status so complete() can decide whether to retry.
        return int(exc.code), exc.read()


def _parse_completion(body: bytes) -> Completion:
    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        tokens = int(usage.get("total_tokens", 0))
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise GroqError(f"malformed Groq response: {exc}") from exc
    if not isinstance(content, str):
        raise GroqError("Groq response content was not a string")
    return Completion(text=content, tokens=tokens)


def _error_detail(status: int, body: bytes) -> str:
    message = body.decode("utf-8", "replace")[:300]
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return f"Groq API returned {status}: {message}"
    return f"Groq API returned {status}: {str(parsed.get('error', parsed))[:300]}"
