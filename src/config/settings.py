"""Typed settings loaded from layered sources.

Layering (lowest precedence first):

1. ``default.toml`` shipped with this package,
2. an optional user TOML file,
3. environment variables.

Secrets are read *only* from the environment (``GROQ_API_KEY``); they never
appear in any TOML file. ``load_settings`` returns a fully validated, immutable
:class:`Settings` object so the rest of the app can rely on the contract.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.errors import ConfigError
from models.enums import Language

_DEFAULT_TOML = Path(__file__).with_name("default.toml")


@dataclass(frozen=True, slots=True)
class GroqConfig:
    """Everything an agent needs to reach the Groq API.

    ``api_key`` is optional at load time so Track A code (and its tests) can run
    without credentials; :meth:`require_api_key` enforces presence at the point
    an actual network call would be made.
    """

    model: str
    base_url: str
    temperature: float
    max_tokens: int
    request_timeout_seconds: int
    api_key: str | None = None

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigError(
                "GROQ_API_KEY is not set; export it before running the swarm"
            )
        return self.api_key


@dataclass(frozen=True, slots=True)
class Settings:
    """The whole application configuration contract."""

    agent_count: int
    default_target_language: Language
    max_lines_per_unit: int
    overlap_lines: int
    database_path: str
    groq: GroqConfig

    def __post_init__(self) -> None:
        if self.agent_count < 1:
            raise ConfigError("swarm.agent_count must be >= 1")
        if self.max_lines_per_unit < 1:
            raise ConfigError("chunking.max_lines_per_unit must be >= 1")
        if self.overlap_lines < 0 or self.overlap_lines >= self.max_lines_per_unit:
            raise ConfigError(
                "chunking.overlap_lines must be in [0, max_lines_per_unit)"
            )
        if self.groq.temperature < 0:
            raise ConfigError("groq.temperature must be >= 0")
        if self.groq.max_tokens < 1:
            raise ConfigError("groq.max_tokens must be >= 1")


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` (override wins)."""
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _env_str(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _env_int(name: str) -> int | None:
    raw = _env_str(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"environment variable {name} must be an integer") from exc


def load_settings(user_config: str | Path | None = None) -> Settings:
    """Build a :class:`Settings` from defaults, an optional file, and env vars.

    Parameters
    ----------
    user_config:
        Optional path to a TOML file whose values override the packaged
        defaults. Environment variables override both.
    """
    data = _read_toml(_DEFAULT_TOML)
    if user_config is not None:
        data = _deep_merge(data, _read_toml(Path(user_config)))

    swarm = data.get("swarm", {})
    chunking = data.get("chunking", {})
    groq = data.get("groq", {})
    database = data.get("database", {})

    # Environment overrides (POLYGLOT_* namespace, plus the standard GROQ_API_KEY).
    agent_count = _env_int("POLYGLOT_AGENT_COUNT") or int(swarm.get("agent_count", 8))
    max_lines = _env_int("POLYGLOT_MAX_LINES_PER_UNIT") or int(
        chunking.get("max_lines_per_unit", 200)
    )
    overlap_env = _env_int("POLYGLOT_OVERLAP_LINES")
    overlap = overlap_env if overlap_env is not None else int(
        chunking.get("overlap_lines", 0)
    )
    db_path = _env_str("POLYGLOT_DB_PATH") or str(
        database.get("path", "polyglot_swarm.db")
    )
    target_raw = _env_str("POLYGLOT_TARGET_LANGUAGE") or str(
        swarm.get("default_target_language", "python")
    )

    groq_config = GroqConfig(
        model=_env_str("POLYGLOT_GROQ_MODEL")
        or str(groq.get("model", "llama-3.3-70b-versatile")),
        base_url=str(groq.get("base_url", "https://api.groq.com/openai/v1")),
        temperature=float(groq.get("temperature", 0.2)),
        max_tokens=int(groq.get("max_tokens", 8192)),
        request_timeout_seconds=int(groq.get("request_timeout_seconds", 60)),
        api_key=_env_str("GROQ_API_KEY"),
    )

    return Settings(
        agent_count=agent_count,
        default_target_language=Language.from_value(target_raw),
        max_lines_per_unit=max_lines,
        overlap_lines=overlap,
        database_path=db_path,
        groq=groq_config,
    )
