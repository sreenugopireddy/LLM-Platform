"""
Tests for Phase 3 — Inference service (router, cost tracker, metrics endpoint).
Run: pytest tests/inference/ -v
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from services.inference.cost_tracker import CostRecord, CostTracker
from services.inference.config import InferenceSettings
from services.inference.router import InferenceRouter, MODEL_PRICING


# ─────────────────────────────────────────────────────────────────────────────
# Cost tracker tests
# ─────────────────────────────────────────────────────────────────────────────

def make_record(**kwargs) -> CostRecord:
    defaults = dict(
        model="gpt-4o-mini", endpoint="payg",
        input_tokens=100, output_tokens=50,
        total_usd=0.000045, team_id="team_a", latency_ms=320.0,
    )
    defaults.update(kwargs)
    return CostRecord(**defaults)


def test_cost_tracker_records_and_aggregates():
    tracker = CostTracker()
    tracker.record(make_record(team_id="team_a", total_usd=0.001, input_tokens=100, output_tokens=50))
    tracker.record(make_record(team_id="team_a", total_usd=0.002, input_tokens=200, output_tokens=100))
    tracker.record(make_record(team_id="team_b", total_usd=0.005, input_tokens=500, output_tokens=200))

    summary = tracker.get_summary()
    assert summary["total_requests"] == 3
    assert abs(summary["total_usd"] - 0.008) < 1e-9

    team_a = summary["by_team"]["team_a"]
    assert team_a["total_requests"] == 2
    assert team_a["total_input_tokens"] == 300
    assert team_a["total_output_tokens"] == 150

    team_b = summary["by_team"]["team_b"]
    assert team_b["total_requests"] == 1


def test_ptu_payg_split_tracking():
    tracker = CostTracker()
    tracker.record(make_record(endpoint="ptu",  team_id="t1"))
    tracker.record(make_record(endpoint="ptu",  team_id="t1"))
    tracker.record(make_record(endpoint="payg", team_id="t1"))

    agg = tracker.get_team_cost("t1")
    assert agg.ptu_requests == 2
    assert agg.payg_requests == 1


def test_recent_ring_buffer():
    tracker = CostTracker(max_recent=5)
    for i in range(10):
        tracker.record(make_record(team_id=f"t{i}"))
    summary = tracker.get_summary()
    assert len(summary["recent_requests"]) == 5  # only last 5 kept


def test_unknown_team_returns_none():
    tracker = CostTracker()
    assert tracker.get_team_cost("nonexistent_team") is None


def test_thread_safety():
    """Concurrent records from multiple threads must not corrupt the aggregate."""
    import threading
    tracker = CostTracker()
    errors = []

    def worker(team_id):
        try:
            for _ in range(100):
                tracker.record(make_record(team_id=team_id, total_usd=0.001))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(f"team_{i}",)) for i in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors
    assert tracker.get_summary()["total_requests"] == 500


# ─────────────────────────────────────────────────────────────────────────────
# Router tests (mock mode — no Azure creds needed)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_router():
    settings = InferenceSettings(
        azure_oai_ptu_endpoint="",
        azure_oai_payg_endpoint="",
    )
    tracker = CostTracker()
    r = InferenceRouter(settings, tracker)
    return r, tracker


@pytest.mark.asyncio
async def test_mock_mode_returns_response(mock_router):
    r, tracker = mock_router
    await r.startup()
    body = {"messages": [{"role": "user", "content": "hello"}], "model": "gpt-4o-mini"}
    response, cost = await r.complete(body, team_id="team_test")
    assert "choices" in response
    assert response["choices"][0]["message"]["role"] == "assistant"
    assert cost.endpoint == "mock"
    await r.shutdown()


@pytest.mark.asyncio
async def test_mock_mode_records_cost(mock_router):
    r, tracker = mock_router
    await r.startup()
    body = {"messages": [{"role": "user", "content": "test"}], "model": "gpt-4o-mini"}
    await r.complete(body, team_id="team_cost_test")
    # Mock records a zero-cost record but still tracks the request
    summary = tracker.get_summary()
    assert summary["total_requests"] == 1
    await r.shutdown()


@pytest.mark.asyncio
async def test_ptu_fallback_to_payg():
    """When PTU returns 429, router should fall back to PAYG."""
    settings = InferenceSettings(
        azure_oai_ptu_endpoint="http://fake-ptu",
        azure_oai_ptu_key="fake-key",
        azure_oai_payg_endpoint="http://fake-payg",
        azure_oai_payg_key="fake-key",
    )
    tracker = CostTracker()
    r = InferenceRouter(settings, tracker)

    # Mock PTU returns 429, PAYG returns 200
    ptu_response = MagicMock()
    ptu_response.status_code = 429

    payg_response = MagicMock()
    payg_response.status_code = 200
    payg_response.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop", "index": 0}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        "model": "gpt-4o-mini",
    }

    ptu_client = AsyncMock()
    ptu_client.post = AsyncMock(return_value=ptu_response)
    payg_client = AsyncMock()
    payg_client.post = AsyncMock(return_value=payg_response)

    r._ptu_client  = ptu_client
    r._payg_client = payg_client

    body = {"messages": [{"role": "user", "content": "hi"}], "model": "gpt-4o-mini"}
    response, cost = await r.complete(body, team_id="t1")

    assert response["choices"][0]["message"]["content"] == "ok"
    assert cost.endpoint == "payg"  # confirmed fallback happened
    ptu_client.post.assert_called_once()
    payg_client.post.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Pricing sanity checks
# ─────────────────────────────────────────────────────────────────────────────

def test_gpt4o_more_expensive_than_gpt35():
    assert MODEL_PRICING["gpt-4o"]["input"] > MODEL_PRICING["gpt-35-turbo"]["input"]
    assert MODEL_PRICING["gpt-4o"]["output"] > MODEL_PRICING["gpt-35-turbo"]["output"]


def test_cost_calculation_correct():
    """1000 input + 500 output tokens at gpt-4o pricing."""
    pricing = MODEL_PRICING["gpt-4o"]
    cost = (1000 / 1000 * pricing["input"]) + (500 / 1000 * pricing["output"])
    # $0.005 input + $0.0075 output = $0.0125
    assert abs(cost - 0.0125) < 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# HTTP endpoint smoke tests
# ─────────────────────────────────────────────────────────────────────────────

def test_inference_health_endpoint():
    from services.inference.main import app
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["mode"] == "mock"  # no Azure creds in test env


def test_cost_metrics_endpoint():
    from services.inference.main import app
    with TestClient(app) as client:
        resp = client.get("/metrics/cost")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_usd" in data
        assert "total_requests" in data
        assert "by_team" in data


def test_chat_completions_mock():
    from services.inference.main import app
    with TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hello"}],
            "model": "gpt-4o-mini",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "choices" in data
        # Cost headers should be present
        assert "X-Cost-USD" in resp.headers
        assert "X-Tokens-Total" in resp.headers
        assert "X-Endpoint-Type" in resp.headers


def test_chat_completions_missing_messages():
    from services.inference.main import app
    with TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json={"model": "gpt-4o-mini"})
        assert resp.status_code == 400