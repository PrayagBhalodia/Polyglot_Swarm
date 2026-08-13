"""Tests for the stdlib Groq client — request shape, retries, parsing.

The HTTP transport is injected, so these run with ZERO network access and no API
key; only real deployments reach api.groq.com.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from collections.abc import Mapping

from config.settings import GroqConfig
from services.groq_client import Completion, GroqClient, GroqError


def _config() -> GroqConfig:
    return GroqConfig(
        model="llama-3.3-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
        temperature=0.2,
        max_tokens=1024,
        request_timeout_seconds=30,
        api_key="test-key",
    )


def _ok_body(text: str = "hello", tokens: int = 42) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": text}}], "usage": {"total_tokens": tokens}}
    ).encode("utf-8")


class RequestShapeTests(unittest.TestCase):
    def test_builds_authorized_chat_request(self) -> None:
        captured: dict[str, object] = {}

        def transport(url, data, headers, timeout):  # type: ignore[no-untyped-def]
            captured["url"] = url
            captured["headers"] = dict(headers)
            captured["payload"] = json.loads(data)
            captured["timeout"] = timeout
            return 200, _ok_body("def f(): pass", 7)

        client = GroqClient(_config(), transport=transport)
        result = client.complete(system="sys", user="usr", model="m1")

        self.assertEqual(result, Completion(text="def f(): pass", tokens=7))
        self.assertEqual(
            captured["url"], "https://api.groq.com/openai/v1/chat/completions"
        )
        headers = captured["headers"]
        assert isinstance(headers, Mapping)
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["model"], "m1")
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["content"], "usr")
        self.assertEqual(captured["timeout"], 30.0)

    def test_defaults_model_from_config(self) -> None:
        def transport(url, data, headers, timeout):  # type: ignore[no-untyped-def]
            return 200, _ok_body()

        client = GroqClient(_config(), transport=transport)
        # No model override => config model is used (asserted via a capture).
        seen: dict[str, str] = {}

        def capturing(url, data, headers, timeout):  # type: ignore[no-untyped-def]
            seen["model"] = json.loads(data)["model"]
            return 200, _ok_body()

        GroqClient(_config(), transport=capturing).complete(system="s", user="u")
        self.assertEqual(seen["model"], "llama-3.3-70b-versatile")


class RetryTests(unittest.TestCase):
    def test_retries_then_succeeds_on_429(self) -> None:
        calls = {"n": 0}

        def transport(url, data, headers, timeout):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                return 429, b'{"error": "rate limited"}'
            return 200, _ok_body("ok", 3)

        client = GroqClient(_config(), transport=transport, sleep=lambda _s: None)
        result = client.complete(system="s", user="u")
        self.assertEqual(result.tokens, 3)
        self.assertEqual(calls["n"], 2)

    def test_non_retryable_status_raises(self) -> None:
        def transport(url, data, headers, timeout):  # type: ignore[no-untyped-def]
            return 400, b'{"error": "bad request"}'

        client = GroqClient(_config(), transport=transport, sleep=lambda _s: None)
        with self.assertRaises(GroqError):
            client.complete(system="s", user="u")

    def test_network_error_exhausts_retries(self) -> None:
        calls = {"n": 0}

        def transport(url, data, headers, timeout):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            raise urllib.error.URLError("connection refused")

        client = GroqClient(_config(), transport=transport, sleep=lambda _s: None)
        with self.assertRaises(GroqError):
            client.complete(system="s", user="u")
        self.assertEqual(calls["n"], 4)  # 1 try + 3 retries


class ParseTests(unittest.TestCase):
    def test_malformed_body_raises(self) -> None:
        def transport(url, data, headers, timeout):  # type: ignore[no-untyped-def]
            return 200, b"not json"

        client = GroqClient(_config(), transport=transport)
        with self.assertRaises(GroqError):
            client.complete(system="s", user="u")

    def test_missing_api_key_fails_fast(self) -> None:
        from core.errors import ConfigError

        no_key = GroqConfig(
            model="m", base_url="https://x/v1", temperature=0.0,
            max_tokens=1, request_timeout_seconds=1, api_key=None,
        )
        with self.assertRaises(ConfigError):
            GroqClient(no_key)


if __name__ == "__main__":
    unittest.main()
