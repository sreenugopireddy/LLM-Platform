"""
Shared prompt schema — used by prompt-registry and eval-harness.

A prompt version document in Cosmos DB looks like:
{
  "id": "customer-support:2.1.0",
  "name": "customer-support",
  "version": "2.1.0",
  "template": "You are a helpful support agent. {{user_message}}",
  "input_schema": {"user_message": "string"},
  "status": "draft",          # draft → staging → production
  "eval_scores": {},          # filled by eval harness before promotion
  "created_at": "2024-01-01T00:00:00Z",
  "promoted_at": null,
  "promoted_by": null
}
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field, field_validator
from semver import VersionInfo


class PromptVersionCreate(BaseModel):
    version: str
    template: str
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    description: str = ""

    @field_validator("version")
    @classmethod
    def validate_semver(cls, v: str) -> str:
        try:
            VersionInfo.parse(v)
        except ValueError:
            raise ValueError(f"version must be valid semver (e.g. 1.0.0), got: {v!r}")
        return v

    @field_validator("template")
    @classmethod
    def validate_template_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("template must not be empty")
        return v


class PromptVersionDoc(BaseModel):
    """Full Cosmos DB document shape."""
    id: str                              # "{name}:{version}"
    name: str
    version: str
    template: str
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    status: str = "draft"               # draft | staging | production
    eval_scores: Dict[str, float] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    promoted_at: Optional[str] = None
    promoted_by: Optional[str] = None

    def render(self, variables: Dict[str, str]) -> str:
        """Render the template with provided variables."""
        result = self.template
        for key, value in variables.items():
            result = result.replace(f"{{{{{key}}}}}", value)
        return result

    def passes_eval_gate(self, threshold: float = 0.85) -> bool:
        """Returns True if all eval scores meet the threshold."""
        if not self.eval_scores:
            return False
        return all(score >= threshold for score in self.eval_scores.values())