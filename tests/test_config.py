"""Tests for the layered settings loader."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config.settings import load_settings
from core.errors import ConfigError
from models.enums import ChunkStrategy, Language


class DefaultsTests(unittest.TestCase):
    def test_loads_packaged_defaults(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = load_settings()
        self.assertEqual(settings.agent_count, 8)
        self.assertEqual(settings.default_target_language, Language.PYTHON)
        self.assertEqual(settings.max_lines_per_unit, 200)
        self.assertIsNone(settings.groq.api_key)

    def test_require_api_key_raises_when_absent(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = load_settings()
        with self.assertRaises(ConfigError):
            settings.groq.require_api_key()


class OverrideTests(unittest.TestCase):
    def test_env_overrides_defaults(self) -> None:
        env = {
            "POLYGLOT_AGENT_COUNT": "16",
            "POLYGLOT_TARGET_LANGUAGE": "rust",
            "GROQ_API_KEY": "secret-key",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.agent_count, 16)
        self.assertEqual(settings.default_target_language, Language.RUST)
        self.assertEqual(settings.groq.require_api_key(), "secret-key")

    def test_user_toml_overrides_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "user.toml"
            cfg.write_text(
                "[chunking]\nmax_lines_per_unit = 50\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                settings = load_settings(cfg)
        self.assertEqual(settings.max_lines_per_unit, 50)

    def test_chunk_strategy_defaults_to_structural(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                load_settings().chunk_strategy, ChunkStrategy.STRUCTURAL
            )

    def test_chunk_strategy_env_override(self) -> None:
        with mock.patch.dict(
            os.environ, {"POLYGLOT_CHUNK_STRATEGY": "lines"}, clear=True
        ):
            self.assertEqual(load_settings().chunk_strategy, ChunkStrategy.LINES)

    def test_unknown_chunk_strategy_rejected(self) -> None:
        with mock.patch.dict(
            os.environ, {"POLYGLOT_CHUNK_STRATEGY": "vibes"}, clear=True
        ):
            with self.assertRaises(ConfigError):
                load_settings()

    def test_invalid_env_int_rejected(self) -> None:
        with mock.patch.dict(
            os.environ, {"POLYGLOT_AGENT_COUNT": "lots"}, clear=True
        ):
            with self.assertRaises(ConfigError):
                load_settings()


if __name__ == "__main__":
    unittest.main()
