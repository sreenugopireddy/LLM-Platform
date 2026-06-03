import json
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class InferenceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",           # ← ignore unknown fields from .env
        populate_by_name=True,
    )

    # Base URL only — NO path, NO query string
    # Correct:   https://sreenu-openai-2026.openai.azure.com
    # Wrong:     https://sreenu-openai-2026.openai.azure.com/openai/deployments/...
    azure_oai_ptu_endpoint: str  = Field(default="", alias="AZURE_OAI_PTU_ENDPOINT")
    azure_oai_ptu_key: str       = Field(default="", alias="AZURE_OAI_PTU_KEY")
    azure_oai_payg_endpoint: str = Field(default="", alias="AZURE_OAI_PAYG_ENDPOINT")
    azure_oai_payg_key: str      = Field(default="", alias="AZURE_OAI_PAYG_KEY")
    api_version: str             = Field(default="2024-10-21", alias="AZURE_OAI_API_VERSION")
    otel_endpoint: str           = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")

    model_deployment_map_json: str = Field(
        default='{"gpt-4o-mini":"gpt-4o-mini"}',
        alias="MODEL_DEPLOYMENT_MAP",
    )

    @property
    def model_deployment_map(self) -> dict:
        try:
            return json.loads(self.model_deployment_map_json)
        except Exception:
            return {}