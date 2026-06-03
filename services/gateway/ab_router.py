"""
A/B Router — deterministic sticky variant assignment.

Key design:
  bucket = int(MD5(experiment_id + ":" + user_id), 16) / 16^32
  → always in [0.0, 1.0)
  → same user_id always lands in the same bucket
  → no session storage, no database lookup, O(1)

This matches how YouTube, Bing, and DoorDash run A/B experiments in
production. Random assignment breaks sticky sessions on refresh;
deterministic hashing gives them for free.

Experiments are loaded from env / config at startup.
Hot-reload is supported — call reload_experiments() without restarting.
"""
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("ab_router")


@dataclass
class Variant:
    name: str               # e.g. "control", "treatment_a"
    model: str              # e.g. "gpt-4o", "gpt-35-turbo"
    weight: float           # fraction of traffic, sum across variants == 1.0
    prompt_version: str = ""  # optional — pin a specific prompt semver


@dataclass
class Experiment:
    id: str
    variants: List[Variant]
    enabled: bool = True
    description: str = ""

    def __post_init__(self):
        total = sum(v.weight for v in self.variants)
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Experiment {self.id}: variant weights sum to {total:.3f}, must be 1.0")
        # Build cumulative breakpoints for O(n) lookup
        self._breakpoints: List[float] = []
        cumulative = 0.0
        for v in self.variants:
            cumulative += v.weight
            self._breakpoints.append(cumulative)

    def assign(self, user_id: str) -> Variant:
        """Return a deterministically assigned variant for this user."""
        key = f"{self.id}:{user_id}".encode()
        bucket = int(hashlib.md5(key).hexdigest(), 16) / (16 ** 32)
        for variant, breakpoint in zip(self.variants, self._breakpoints):
            if bucket < breakpoint:
                return variant
        return self.variants[-1]  # float rounding safety net


# ── Experiment registry ────────────────────────────────────────────────────────

_registry: Dict[str, Experiment] = {}


def load_experiments(config: dict) -> None:
    """
    Populate the registry from a dict.
    Call at startup from Settings, and again to hot-reload.

    Expected shape:
    {
      "experiments": [
        {
          "id": "exp_gpt4o_vs_35",
          "description": "Compare GPT-4o vs GPT-3.5 on customer support",
          "enabled": true,
          "variants": [
            {"name": "control",     "model": "gpt-35-turbo", "weight": 0.5},
            {"name": "treatment_a", "model": "gpt-4o",       "weight": 0.5}
          ]
        }
      ]
    }
    """
    global _registry
    new_registry = {}
    for exp_conf in config.get("experiments", []):
        variants = [Variant(**v) for v in exp_conf["variants"]]
        exp = Experiment(
            id=exp_conf["id"],
            variants=variants,
            enabled=exp_conf.get("enabled", True),
            description=exp_conf.get("description", ""),
        )
        new_registry[exp.id] = exp
        logger.info("Loaded experiment '%s' (%d variants)", exp.id, len(variants))
    _registry = new_registry


def get_variant(experiment_id: str, user_id: str) -> Optional[Variant]:
    """
    Returns the assigned variant, or None if the experiment doesn't exist
    or is disabled. Callers treat None as "no experiment active — use default".
    """
    exp = _registry.get(experiment_id)
    if not exp or not exp.enabled:
        return None
    variant = exp.assign(user_id)
    logger.debug("A/B assign exp=%s user=%s → variant=%s model=%s", experiment_id, user_id, variant.name, variant.model)
    return variant


def reload_experiments() -> None:
    """Hot-reload experiments from the EXPERIMENTS_JSON env var."""
    raw = os.getenv("EXPERIMENTS_JSON", "{}")
    try:
        config = json.loads(raw)
        load_experiments(config)
        logger.info("Experiments reloaded — %d active", len(_registry))
    except Exception as exc:
        logger.error("Failed to reload experiments: %s", exc)


# Initialise at import time so the module is usable without calling load_experiments explicitly.
reload_experiments()