from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List
import json


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",           # ← ignore unknown fields from .env
        populate_by_name=True,
    )

    jwt_secret: str                  = Field(default="changeme", alias="JWT_SECRET")
    inference_service_url: str       = Field(default="http://localhost:8001", alias="INFERENCE_SERVICE_URL")
    prompt_registry_url: str         = Field(default="http://localhost:8002", alias="PROMPT_REGISTRY_URL")
    default_rpm: int                 = Field(default=60,      alias="DEFAULT_RPM")
    default_tpm: int                 = Field(default=100000,  alias="DEFAULT_TPM")
    redis_url: str                   = Field(default="",      alias="REDIS_URL")
    default_experiment: str          = Field(default="",      alias="DEFAULT_EXPERIMENT")
    experiments_json: str            = Field(default="{}",    alias="EXPERIMENTS_JSON")
    otel_endpoint: str               = Field(default="",      alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    allowed_origins: List[str]       = Field(
        default=["http://localhost:3000"],
        alias="ALLOWED_ORIGINS",
    )

    @property
    def experiments_config(self) -> dict:
        try:
            return json.loads(self.experiments_json)
        except Exception:
            return {}