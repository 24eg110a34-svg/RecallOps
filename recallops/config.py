"""Runtime configuration for RecallOps.

All secrets come from the environment (or a local .env file). Nothing in this
module ever returns a secret to a caller that renders it to a browser: use
:func:`Settings.public_snapshot` for anything the frontend may see.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("RECALLOPS_ENV_FILE", str(REPO_ROOT / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- app ---------------------------------------------------------------
    app_name: str = "RecallOps"
    app_version: str = "1.0.0"
    environment: str = "local"
    demo_mode: bool = True
    demo_seed_on_start: bool = True
    database_url: str = "sqlite:///./recallops.db"
    scenarios_dir: str = "./scenarios"
    cors_origins: str = "http://localhost:4321,http://127.0.0.1:4321"
    log_level: str = "info"
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    web_port: int = 4321

    # --- hindsight ---------------------------------------------------------
    hindsight_base_url: str | None = None
    hindsight_api_key: str | None = None
    hindsight_bank_id: str = "recallops-org"
    hindsight_timeout: float = 6.0
    # auto | 1/true | 0/false
    hindsight_enabled: str = "auto"
    hindsight_recall_budget: str = "mid"
    hindsight_recall_max_tokens: int = 2048

    # --- llm ---------------------------------------------------------------
    llm_provider: str = "local_heuristic"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_timeout: float = 45.0
    llm_temperature: float = 0.0

    # --- resilience --------------------------------------------------------
    provider_max_attempts: int = 3
    provider_backoff_base: float = 0.35
    provider_backoff_max: float = 4.0
    provider_breaker_threshold: int = 3
    provider_breaker_cooldown: float = 20.0
    network_probe_timeout: float = 4.0

    # ------------------------------------------------------------------ utils
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def scenarios_path(self) -> Path:
        p = Path(self.scenarios_dir)
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()

    @property
    def sqlalchemy_url(self) -> str:
        """Absolute SQLite URL so the DB never depends on the process CWD."""
        url = self.database_url
        if url.startswith("sqlite:///"):
            tail = url[len("sqlite:///") :]
            if tail in ("", ":memory:"):
                return "sqlite:///:memory:"
            p = Path(tail)
            if not p.is_absolute():
                p = (REPO_ROOT / p).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{p.as_posix()}"
        return url

    @property
    def is_sqlite_memory(self) -> bool:
        return ":memory:" in self.sqlalchemy_url

    @property
    def hindsight_forced_off(self) -> bool:
        return str(self.hindsight_enabled).strip().lower() in {"0", "false", "off", "no", "disabled"}

    @property
    def hindsight_forced_on(self) -> bool:
        return str(self.hindsight_enabled).strip().lower() in {"1", "true", "on", "yes", "enabled"}

    @property
    def hindsight_endpoint(self) -> str | None:
        if not self.hindsight_base_url:
            return None
        url = self.hindsight_base_url.strip()
        if not url:
            return None
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        return url.rstrip("/")

    def host_of(self, url: str | None) -> str | None:
        if not url:
            return None
        try:
            return urlparse(url).hostname
        except ValueError:  # pragma: no cover - defensive
            return None

    def public_snapshot(self) -> dict[str, object]:
        """Config safe to expose over HTTP: no key material, ever."""
        return {
            "app_name": self.app_name,
            "app_version": self.app_version,
            "environment": self.environment,
            "demo_mode": self.demo_mode,
            "llm": {
                "provider": self.llm_provider,
                "model": self.llm_model,
                "base_url": self.llm_base_url if self.llm_provider != "local_heuristic" else None,
                "api_key_configured": bool(self.llm_api_key),
            },
            "hindsight": {
                "enabled_mode": self.hindsight_enabled,
                "base_url": self.hindsight_endpoint,
                "bank_id": self.hindsight_bank_id,
                "api_key_configured": bool(self.hindsight_api_key),
            },
            "database": {"url_scheme": self.sqlalchemy_url.split(":", 1)[0], "sqlite": self.is_sqlite_memory or self.sqlalchemy_url.startswith("sqlite")},
            "scenarios_dir": str(self.scenarios_path),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
