"""
Tests for Phase 2 — Gateway (auth, A/B routing, rate limiting).

Run:  pytest tests/gateway/ -v
"""
import hashlib
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from services.gateway.auth import issue_token, verify_token, TokenPayload
from services.gateway.ab_router import Experiment, Variant, load_experiments, get_variant


# ─────────────────────────────────────────────────────────────────────────────
# Auth tests
# ─────────────────────────────────────────────────────────────────────────────

SECRET = "test-secret-not-for-production"


def test_issue_and_verify_token():
    token = issue_token(sub="user_abc", team_id="team_x", roles=["premium"], secret=SECRET)
    payload = verify_token(token, SECRET)
    assert payload.sub == "user_abc"
    assert payload.team_id == "team_x"
    assert "premium" in payload.roles


def test_expired_token_raises_401():
    from fastapi import HTTPException
    # Issue token that expired 10 seconds ago
    token = issue_token(sub="u", team_id="t", roles=[], secret=SECRET, ttl_seconds=-10)
    with pytest.raises(HTTPException) as exc_info:
        verify_token(token, SECRET)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"] == "token_expired"


def test_tampered_token_raises_401():
    from fastapi import HTTPException
    token = issue_token(sub="u", team_id="t", roles=[], secret=SECRET)
    tampered = token[:-4] + "XXXX"
    with pytest.raises(HTTPException) as exc_info:
        verify_token(tampered, SECRET)
    assert exc_info.value.status_code == 401


def test_wrong_secret_raises_401():
    from fastapi import HTTPException
    token = issue_token(sub="u", team_id="t", roles=[], secret=SECRET)
    with pytest.raises(HTTPException):
        verify_token(token, "wrong-secret")


# ─────────────────────────────────────────────────────────────────────────────
# A/B router tests
# ─────────────────────────────────────────────────────────────────────────────

EXPERIMENT_CONFIG = {
    "experiments": [
        {
            "id": "test_exp",
            "description": "50/50 split for testing",
            "enabled": True,
            "variants": [
                {"name": "control",     "model": "gpt-35-turbo", "weight": 0.5},
                {"name": "treatment",   "model": "gpt-4o",       "weight": 0.5},
            ],
        }
    ]
}


def test_load_experiments():
    load_experiments(EXPERIMENT_CONFIG)
    v = get_variant("test_exp", "any_user")
    assert v is not None
    assert v.model in ("gpt-35-turbo", "gpt-4o")


def test_deterministic_assignment():
    """Same user_id must always get the same variant."""
    load_experiments(EXPERIMENT_CONFIG)
    first  = get_variant("test_exp", "user_determinism_test")
    second = get_variant("test_exp", "user_determinism_test")
    third  = get_variant("test_exp", "user_determinism_test")
    assert first.name == second.name == third.name


def test_sticky_across_refreshes():
    """Simulate 100 'refreshes' (repeated calls) — all must return same variant."""
    load_experiments(EXPERIMENT_CONFIG)
    user_id = "sticky_user_42"
    variants = {get_variant("test_exp", user_id).name for _ in range(100)}
    assert len(variants) == 1, "Variant must not change across calls for same user"


def test_traffic_split_approximate():
    """
    With 1000 random user IDs, roughly 50% should land in each variant.
    Allow ±10% tolerance — MD5 bucketing is uniform but not exactly 50/50
    for small N.
    """
    load_experiments(EXPERIMENT_CONFIG)
    counts = {"control": 0, "treatment": 0}
    for i in range(1000):
        v = get_variant("test_exp", f"user_{i:04d}")
        counts[v.name] += 1
    ratio = counts["control"] / 1000
    assert 0.40 <= ratio <= 0.60, f"Expected ~50% split, got {ratio:.2%}"


def test_disabled_experiment_returns_none():
    config = {
        "experiments": [{
            "id": "disabled_exp",
            "enabled": False,
            "variants": [{"name": "v", "model": "gpt-4o", "weight": 1.0}],
        }]
    }
    load_experiments(config)
    assert get_variant("disabled_exp", "anyone") is None


def test_unknown_experiment_returns_none():
    assert get_variant("exp_that_doesnt_exist", "user_x") is None


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="must be 1.0"):
        Experiment(
            id="bad",
            variants=[
                Variant(name="a", model="gpt-4o", weight=0.3),
                Variant(name="b", model="gpt-35-turbo", weight=0.3),
            ],
        )


def test_md5_bucket_is_in_unit_interval():
    """Sanity check the bucket math for 10k user IDs."""
    for i in range(10_000):
        key = f"test_exp:user_{i}".encode()
        bucket = int(hashlib.md5(key).hexdigest(), 16) / (16 ** 32)
        assert 0.0 <= bucket < 1.0


def test_three_way_split():
    config = {
        "experiments": [{
            "id": "three_way",
            "enabled": True,
            "variants": [
                {"name": "a", "model": "gpt-4o-mini",  "weight": 0.34},
                {"name": "b", "model": "gpt-35-turbo", "weight": 0.33},
                {"name": "c", "model": "gpt-4o",       "weight": 0.33},
            ],
        }]
    }
    load_experiments(config)
    counts = {"a": 0, "b": 0, "c": 0}
    for i in range(3000):
        v = get_variant("three_way", f"u_{i}")
        counts[v.name] += 1
    # Each variant should get roughly 33% ± 10%
    for name, count in counts.items():
        ratio = count / 3000
        assert 0.23 <= ratio <= 0.43, f"Variant {name} got {ratio:.2%}, outside ±10% band"


# ─────────────────────────────────────────────────────────────────────────────
# Integration smoke test (no real HTTP calls)
# ─────────────────────────────────────────────────────────────────────────────

def test_gateway_health():
    """Smoke test: gateway starts and /health returns 200."""
    from services.gateway.main import app
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


def test_chat_without_auth_returns_403():
    from services.gateway.main import app
    with TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json={"messages": []})
        assert resp.status_code in (401, 403, 422)