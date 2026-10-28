"""Typed application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT_DIR / "content"
_EPHEMERAL_SECRET: bytes | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT_DIR / ".env"), env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    api_host: str = "127.0.0.1"
    api_port: int = 8010
    ui_port: int = 5174
    demo_mode: bool = True
    clinic_tz: str = "Asia/Kolkata"
    tenant_id: str = "sunrise-clinic"  # documents of other tenants are never retrievable by this deployment's users

    llm_provider: str = "openai"  # "openai" (any OpenAI-compatible endpoint) or "mock"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen2.5:3b"
    llm_fallback_model: str = "qwen2.5:1.5b"
    llm_fast_model: str = "qwen2.5:1.5b"  # short JSON classifications (router fallback, safety 2nd layer); empty = use LLM_MODEL
    llm_num_predict: int = 300
    llm_temperature: float = 0.1
    llm_keep_alive: str = "60m"
    llm_timeout_s: float = 120.0

    rag_mode: str = "bm25"  # vectorless: "bm25" (+ deterministic tree navigation) or "tree" (+ LLM table-of-contents navigation)
    rag_top_k: int = 3

    db_path: str = "data/careharbor.db"
    checkpoint_db_path: str = "data/checkpoints.db"
    index_dir: str = "data/index"
    trace_dir: str = "data/traces"
    database_url: str = ""
    # Data minimisation: saved conversations (and their agent checkpoints) older than this are deleted at start-up. 0 = keep.
    chat_retention_days: int = 30
    # A verified chat session expires after this many minutes; records then need phone + DOB + OTP again. 0 = never.
    verified_ttl_minutes: int = 120

    staff_username: str = "staff"
    staff_password: str = "change-me"
    # Signs chat session ids, staff cookies, OTP hashes and memory keys. setup.ps1 generates a random one.
    session_secret: str = ""
    rag_pipeline: str = "advanced"  # "advanced" (multi-query + RRF + rerank + corrective retry) or "basic" (single BM25 pass)

    telegram_bot_token: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    @property
    def secret(self) -> bytes:
        """The signing secret. Without SESSION_SECRET a random per-process secret is used (sessions reset on restart)."""
        global _EPHEMERAL_SECRET
        if self.session_secret and self.session_secret != "change-me":
            return self.session_secret.encode()
        if _EPHEMERAL_SECRET is None:
            import secrets as _s

            _EPHEMERAL_SECRET = _s.token_bytes(32)
        return _EPHEMERAL_SECRET

    def resolve(self, rel: str) -> Path:
        """Resolve a configured path relative to the project root."""
        p = Path(rel)
        return p if p.is_absolute() else ROOT_DIR / p

    @property
    def db_file(self) -> Path:
        return self.resolve(self.db_path)

    @property
    def checkpoint_file(self) -> Path:
        return self.resolve(self.checkpoint_db_path)

    @property
    def index_path(self) -> Path:
        return self.resolve(self.index_dir)

    @property
    def trace_path(self) -> Path:
        return self.resolve(self.trace_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()
