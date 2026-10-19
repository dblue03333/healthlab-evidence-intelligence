"""Typed configuration. Contact requirements apply only to online ingestion."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from healthlab.models import ConfigurationError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )
    store_dir: Path = Field(default=Path("outputs"), validation_alias="HEALTHLAB_STORE_DIR")
    email: str = Field(default="", validation_alias="NCBI_EMAIL")
    api_key: SecretStr = Field(default=SecretStr(""), validation_alias="NCBI_API_KEY")
    tool: str = "healthlab_evidence_intelligence"
    timeout: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=5)

    def validate_online(self) -> None:
        address = self.email.strip()
        if (
            "@" not in address
            or "." not in address.rsplit("@", 1)[-1]
            or any(c.isspace() for c in address)
            or address.startswith(("your_email", "your-email"))
            or address.endswith("@example.com")
        ):
            raise ConfigurationError(
                "config_ncbi_email",
                "Set NCBI_EMAIL to a real contact email before online ingestion",
            )


class FPTSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )
    api_key: SecretStr = Field(default=SecretStr(""), validation_alias="FPT_API_KEY")
    base_url: str = Field(
        default="https://mkp-api.fptcloud.com/v1", validation_alias="FPT_BASE_URL"
    )
    model: str = Field(default="gpt-oss-120b", validation_alias="FPT_MODEL_NAME", min_length=1)
    timeout: float = Field(default=60, gt=0, le=120, validation_alias="FPT_TIMEOUT")
    max_tokens: int = Field(default=4096, ge=256, le=16384, validation_alias="FPT_MAX_TOKENS")

    def validate_online(self):
        from urllib.parse import urlsplit

        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError(
                "config_fpt_base_url",
                "FPT_BASE_URL must be an HTTPS base URL without credentials/query",
            )
        if not self.api_key.get_secret_value() or self.api_key.get_secret_value().startswith(
            "your_"
        ):
            raise ConfigurationError("config_fpt_api_key", "Set FPT_API_KEY before extraction")


class OpenRouterSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, hide_input_in_errors=True
    )
    api_key: SecretStr = Field(default=SecretStr(""), validation_alias="OPENROUTER_API_KEY")
    model: str = Field(default="", validation_alias="OPENROUTER_MODEL")
    provider_slug: str = Field(default="", validation_alias="OPENROUTER_PROVIDER")
    timeout: float = Field(default=60, gt=0, le=120, validation_alias="OPENROUTER_TIMEOUT")
    max_tokens: int = Field(
        default=4096, ge=256, le=16384, validation_alias="OPENROUTER_MAX_TOKENS"
    )

    def validate_identity(self):
        if (
            not self.model.strip()
            or self.model != self.model.strip()
            or "/" not in self.model
            or self.model in {"openrouter/auto", "openrouter/free"}
            or ":" in self.model
        ):
            raise ConfigurationError(
                "config_openrouter_model",
                "Set OPENROUTER_MODEL to an explicit model ID without routing suffixes",
            )
        if not self.provider_slug.strip() or self.provider_slug != self.provider_slug.strip():
            raise ConfigurationError(
                "config_openrouter_provider",
                "Set OPENROUTER_PROVIDER to a single provider endpoint slug",
            )

    def validate_online(self):
        self.validate_identity()
        if not self.api_key.get_secret_value() or self.api_key.get_secret_value().startswith(
            "your_"
        ):
            raise ConfigurationError(
                "config_openrouter_api_key", "Set OPENROUTER_API_KEY before OpenRouter calls"
            )


class GeminiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, hide_input_in_errors=True
    )
    api_key: SecretStr = Field(default=SecretStr(""), validation_alias="GEMINI_API_KEY")
    model: Literal["gemini-3.8-flash", "gemini-3.1-pro-preview"] = Field(
        default="gemini-3.8-flash", validation_alias="GEMINI_MODEL_NAME"
    )
    timeout: float = Field(default=90, gt=0, le=120, validation_alias="GEMINI_TIMEOUT")
    max_tokens: int = Field(default=8192, ge=256, le=16384, validation_alias="GEMINI_MAX_TOKENS")
    temperature: float = Field(default=1.0, ge=0, le=2, validation_alias="GEMINI_TEMPERATURE")
    thinking_level: Literal["low", "medium", "high"] = Field(
        default="low", validation_alias="GEMINI_THINKING_LEVEL"
    )

    def validate_online(self):
        key = self.api_key.get_secret_value()
        if not key.strip() or key != key.strip() or key.startswith("your_"):
            raise ConfigurationError(
                "config_gemini_api_key", "Set GEMINI_API_KEY before Gemini calls"
            )
