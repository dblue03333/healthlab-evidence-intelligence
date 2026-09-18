"""Typed configuration. Contact requirements apply only to online ingestion."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
            raise ValueError("Set NCBI_EMAIL to a real contact email before online ingestion")


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
            raise ValueError("FPT_BASE_URL must be an HTTPS base URL without credentials/query")
        if not self.api_key.get_secret_value() or self.api_key.get_secret_value().startswith(
            "your_"
        ):
            raise ValueError("Set FPT_API_KEY before extraction")
