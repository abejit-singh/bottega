"""Application settings, loaded once from environment variables / a `.env` file.

Design decision: we keep *all* tunable knobs in one frozen dataclass instead of
reading `os.environ` all over the codebase. That gives us:
  * one place to see every setting and its default,
  * type conversion + validation at startup (fail fast on a typo),
  * easy testing: tests just build `Settings(...)` directly.

The API key is deliberately NOT stored here. The `anthropic` SDK reads
`ANTHROPIC_API_KEY` from the environment on its own, so the key never has to
pass through our code (and can't accidentally end up in a log line).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Effort controls how much the model "thinks" before answering. Lower effort is
# faster and cheaper; higher effort is more thorough.
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """All runtime configuration for Bottega."""

    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    max_tokens: int = 32_000
    # Safety valve: maximum model calls an agent may make for one task.
    max_turns: int = 25
    # Safety valve: stop a project once it has spent this many dollars.
    max_cost_usd: float = 1.00
    # How many times QA may send one task back before it must be accepted.
    max_revisions: int = 2
    # How many agents may work at the same time.
    max_parallel: int = 3
    # Automatic retries inside the SDK for rate limits and server errors.
    api_retries: int = 4
    # How many times a task is retried if it still fails with a temporary error.
    task_retries: int = 1
    output_dir: Path = Path("output")
    # Company memory: an Obsidian vault that persists across projects.
    memory_dir: Path = Path("memory")
    # When Claude's safety classifiers decline a request, let the API retry it
    # on a fallback model inside the same call instead of failing the run.
    refusal_fallback: bool = True

    def __post_init__(self) -> None:
        if self.effort not in VALID_EFFORTS:
            raise ValueError(f"BOTTEGA_EFFORT must be one of {VALID_EFFORTS}, got {self.effort!r}")
        if self.max_tokens <= 0 or self.max_turns <= 0:
            raise ValueError("BOTTEGA_MAX_TOKENS and BOTTEGA_MAX_TURNS must be positive")
        if self.max_cost_usd <= 0:
            raise ValueError("BOTTEGA_MAX_COST_USD must be positive")
        if self.max_parallel <= 0:
            raise ValueError("BOTTEGA_MAX_PARALLEL must be positive")
        if min(self.max_revisions, self.api_retries, self.task_retries) < 0:
            raise ValueError("Revision and retry limits must not be negative")

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the process environment (after loading `.env`)."""
        load_dotenv()  # no-op if there is no .env file; never overrides real env vars
        return cls(
            model=os.getenv("BOTTEGA_MODEL", cls.model),
            effort=os.getenv("BOTTEGA_EFFORT", cls.effort),
            max_tokens=_env_int("BOTTEGA_MAX_TOKENS", cls.max_tokens),
            max_turns=_env_int("BOTTEGA_MAX_TURNS", cls.max_turns),
            max_cost_usd=_env_float("BOTTEGA_MAX_COST_USD", cls.max_cost_usd),
            max_revisions=_env_int("BOTTEGA_MAX_REVISIONS", cls.max_revisions),
            max_parallel=_env_int("BOTTEGA_MAX_PARALLEL", cls.max_parallel),
            api_retries=_env_int("BOTTEGA_API_RETRIES", cls.api_retries),
            task_retries=_env_int("BOTTEGA_TASK_RETRIES", cls.task_retries),
            output_dir=Path(os.getenv("BOTTEGA_OUTPUT_DIR", str(cls.output_dir))),
            memory_dir=Path(os.getenv("BOTTEGA_MEMORY_DIR", str(cls.memory_dir))),
            refusal_fallback=_env_bool("BOTTEGA_REFUSAL_FALLBACK", cls.refusal_fallback),
        )
