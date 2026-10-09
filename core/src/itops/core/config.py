"""Runtime configuration.

Everything deployment-specific comes from the environment (or a local ``.env``) and is
never hardcoded. That keeps ``core/`` free of company specifics so it can be exported to
the public portfolio repo unchanged.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from itops.core.db import odbc_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ITOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    env: Literal["development", "production"] = "development"
    brand_name: str = "IT-Ops"
    # The address people open in a browser (links in AI tool results, emails).
    # Must match the host in the Entra redirect URI (http is only allowed for localhost).
    public_url: str = "http://localhost:8800"
    # ADR 0013: the public address of the MCP endpoint (a Cloudflare Tunnel hostname), for AI
    # clients in the cloud like ChatGPT. Set it to turn on OAuth for MCP; only /mcp/, the
    # OAuth endpoints and their discovery documents answer on that host. Empty: off.
    mcp_public_url: str = ""
    # Re-check token owners against AD hourly (and at each OAuth refresh): leavers' tokens
    # stop working (core/directory.py). Entra mode on Windows only.
    directory_check: bool = True
    # Rate limits (core/ratelimit.py): the OAuth sign-in endpoints per client address, made-up
    # tokens per address (then held off), and MCP/API calls per token.
    rate_sign_in_per_minute: int = 30
    rate_failed_tokens_per_5_minutes: int = 20
    rate_token_per_minute: int = 600
    # Reverse proxies in front of the local one whose CF-Connecting-IP header is believed
    # (the Cloudflare Tunnel host's address, as the local proxy sees it). Empty: none.
    trusted_proxies: list[str] = Field(default_factory=list)
    # OAuth clients may only redirect to these hosts (and their subdomains).
    oauth_redirect_hosts: list[str] = Field(
        default_factory=lambda: [
            "chatgpt.com",
            "chat.openai.com",
            "claude.ai",
            "claude.com",
            "localhost",
            "127.0.0.1",
        ]
    )

    # "dev": every request acts as a local all-permissions user. Refused in production.
    # "entra": Entra ID sign-in; roles from AD groups synced to Entra (design §7).
    # Company branding (core/theme.py): empty = the one installed theme, "none" = no theme.
    theme: str = ""
    # Run enabled connectors on their schedules (ADR 0010). Off: only manual runs.
    run_connectors: bool = True
    auth_mode: Literal["dev", "entra"] = "dev"
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: SecretStr = SecretStr("")
    # Group (sAMAccountName) per role; each role includes the ones before it.
    role_groups: dict[str, str] = Field(
        default_factory=lambda: {
            "reader": "App-ITOps-Readers",
            "editor": "App-ITOps-Editors",
            "publisher": "App-ITOps-Publishers",
            "admin": "App-ITOps-Admins",
        }
    )
    # Signs the session cookie. Required outside development (dev makes a throwaway one).
    secret_key: SecretStr = SecretStr("")
    session_hours: int = 8

    # SQL Server, Windows authentication (the service's gMSA in production, the
    # developer in dev), so no database password is ever configured.
    # Production: e.g. ITOPS_DB_SERVER=SQLHOST\INSTANCE (or host,port), ITOPS_DB_NAME=ITOps.
    db_server: str = "localhost"
    db_name: str = "itops"
    db_driver: str = "ODBC Driver 18 for SQL Server"
    # Local dev instances use a self-signed certificate; production should verify.
    db_trust_server_certificate: bool = True
    # Full SQLAlchemy URL override (CI uses SQL auth against a container). Read
    # without the ITOPS_ prefix so the conventional DATABASE_URL name works.
    database_url_override: str | None = Field(default=None, validation_alias="DATABASE_URL")
    # Apply pending core + module migrations at startup.
    auto_migrate: bool = True

    # Host names the app answers to (MCP DNS-rebinding protection). Behind IIS in
    # production, add the public name, e.g. ["itops.example.com"].
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    )

    # Folder of module wheels that `itops modules sync` installs.
    modules_dir: str = "./modules"

    # Core file store: bytes on disk (metadata in itops.files). Back it up with the DB.
    files_dir: str = "./data/files"
    max_upload_mb: int = 15  # images (screenshots)
    max_attachment_mb: int = 50  # attachments (PDFs, Office, Visio, ...); IIS allows 55 MB

    # AI tier 2: local embeddings for semantic search (runs on this server; ADR 0004).
    ai_embeddings_provider: Literal["none", "fastembed"] = "fastembed"
    ai_embeddings_model: str = "BAAI/bge-small-en-v1.5"
    # Where the model files live. Downloaded once, then used offline. In production,
    # ship them with the release so the server never needs internet access.
    ai_models_dir: str = "./data/models"
    # AI tier 3: embedded LLM. Off by default; enabled per feature ("helpdesk.triage").
    ai_llm_provider: Literal["none"] = "none"
    ai_llm_features: list[str] = Field(default_factory=list[str])

    @property
    def is_dev(self) -> bool:
        return self.env == "development"

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return odbc_url(
            self.db_server,
            self.db_name,
            driver=self.db_driver,
            trust_server_certificate=self.db_trust_server_certificate,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
