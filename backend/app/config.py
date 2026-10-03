"""Runtime configuration, read once from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _accounts(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if ":" in pair:
            user, pwd = pair.split(":", 1)
            if user.strip() and pwd.strip():
                out[user.strip()] = pwd.strip()
    return out


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/app.db"))
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "gemini").lower())
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", ""))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", ""))
    llm_timeout_s: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT_SECONDS", "90")))
    llm_max_retries: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_RETRIES", "2")))
    # Comma-separated backup models, tried in order when the main model is busy (429/5xx) or retired (404).
    llm_fallback_models: list[str] = field(default_factory=lambda: [
        m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "").split(",") if m.strip()])
    auth_secret: str = field(default_factory=lambda: os.getenv("AUTH_SECRET", "dev-only-change-me"))
    reviewer_accounts: dict[str, str] = field(
        default_factory=lambda: _accounts(os.getenv("REVIEWER_ACCOUNTS", "reviewer:reviewer-demo"))
    )
    max_runs_per_hour: int = field(default_factory=lambda: int(os.getenv("MAX_ANALYSIS_RUNS_PER_HOUR", "40")))
    allow_failure_simulation: bool = field(default_factory=lambda: _bool("ALLOW_FAILURE_SIMULATION", True))
    seed_samples: bool = field(default_factory=lambda: _bool("SEED_SAMPLE_CASES", True))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    static_dir: str = field(default_factory=lambda: os.getenv("STATIC_DIR", ""))

    @property
    def resolved_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        return {
            "gemini": "gemini-3.8-flash",
            "anthropic": "claude-sonnet-5-5",
            "openai": "gpt-4o-mini",
            "mock": "mock-analyst",
        }.get(self.llm_provider, "")


settings = Settings()
