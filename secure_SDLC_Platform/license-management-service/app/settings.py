from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HORIZON_LICENSE_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./license-management.db"
    auto_create_tables: bool = True

    issuer: str = "Horizon Relevance"
    signing_mode: Literal["local-hmac", "aws-kms"] = "local-hmac"
    signing_secret: str = Field(default="dev-only-change-me")
    kms_key_id: str = ""
    kms_region: str = "us-east-1"
    kms_signing_algorithm: str = "RSASSA_PKCS1_V1_5_SHA_256"

    token_pepper: str = Field(default="dev-only-token-pepper")
    admin_api_key: str = Field(default="dev-admin-key")

    default_license_ttl_days: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()

