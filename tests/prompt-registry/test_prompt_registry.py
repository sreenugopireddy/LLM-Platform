"""
Tests for Phase 4 — Prompt Registry + Eval Gate
Run: pytest tests/prompt-registry/ -v
"""
import pytest
from fastapi.testclient import TestClient
from shared.prompt_schema import PromptVersionCreate, PromptVersionDoc


# ── Schema tests ──────────────────────────────────────────────────────────────

def test_valid_semver_accepted():
    p = PromptVersionCreate(version="1.2.3", template="Hello {{name}}")
    assert p.version == "1.2.3"


def test_invalid_semver_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        PromptVersionCreate(version="v1.2", template="Hello")


def test_empty_template_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        PromptVersionCreate(version="1.0.0", template="   ")


def test_template_render():
    doc = PromptVersionDoc(
        id="test:1.0.0", name="test", version="1.0.0",
        template="Hello {{name}}, your issue is {{issue}}."
    )
    rendered = doc.render({"name": "Alice", "issue": "billing"})
    assert rendered == "Hello Alice, your issue is billing."


def test_eval_gate_passes_when_all_scores_above_threshold():
    doc = PromptVersionDoc(
        id="t:1.0.0", name="t", version="1.0.0", template="x",
        eval_scores={"relevance": 0.90, "faithfulness": 0.87}
    )
    assert doc.passes_eval_gate(threshold=0.85) is True


def test_eval_gate_fails_when_any_score_below_threshold():
    doc = PromptVersionDoc(
        id="t:1.0.0", name="t", version="1.0.0", template="x",
        eval_scores={"relevance": 0.90, "faithfulness": 0.80}
    )
    assert doc.passes_eval_gate(threshold=0.85) is False


def test_eval_gate_fails_when_no_scores():
    doc = PromptVersionDoc(id="t:1.0.0", name="t", version="1.0.0", template="x")
    assert doc.passes_eval_gate() is False


# ── API tests ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from services.prompt_registry.main import app
    with TestClient(app) as c:
        yield c


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_create_version(client):
    resp = client.post("/prompts/test-prompt/versions", json={
        "version": "1.0.0",
        "template": "You are a helpful assistant. {{user_message}}",
        "input_schema": {"user_message": "string"},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == "test-prompt:1.0.0"
    assert data["status"] == "draft"


def test_duplicate_version_rejected(client):
    payload = {"version": "2.0.0", "template": "Hello {{name}}"}
    client.post("/prompts/dup-test/versions", json=payload)
    resp = client.post("/prompts/dup-test/versions", json=payload)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "version_exists"


def test_promote_blocked_without_eval_scores(client):
    client.post("/prompts/gate-test/versions", json={"version": "1.0.0", "template": "Hello"})
    resp = client.post("/prompts/gate-test/versions/1.0.0/promote")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "eval_scores_missing"


def test_promote_blocked_with_low_scores(client):
    client.post("/prompts/low-score/versions", json={"version": "1.0.0", "template": "Hello"})
    client.patch("/prompts/low-score/versions/1.0.0/eval-scores",
                 json={"relevance": 0.70, "faithfulness": 0.65})
    resp = client.post("/prompts/low-score/versions/1.0.0/promote")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "eval_gate_failed"


def test_promote_succeeds_with_passing_scores(client):
    client.post("/prompts/good-prompt/versions", json={"version": "1.0.0", "template": "Hello {{x}}"})
    client.patch("/prompts/good-prompt/versions/1.0.0/eval-scores",
                 json={"relevance": 0.92, "faithfulness": 0.88})
    resp = client.post("/prompts/good-prompt/versions/1.0.0/promote")
    assert resp.status_code == 200
    assert resp.json()["promoted"] is True


def test_production_endpoint_returns_latest(client):
    client.post("/prompts/prod-test/versions", json={"version": "1.0.0", "template": "v1"})
    client.patch("/prompts/prod-test/versions/1.0.0/eval-scores",
                 json={"relevance": 0.90, "faithfulness": 0.90})
    client.post("/prompts/prod-test/versions/1.0.0/promote")
    resp = client.get("/prompts/prod-test/production")
    assert resp.status_code == 200
    assert resp.json()["version"] == "1.0.0"
    assert resp.json()["status"] == "production"


def test_delete_draft(client):
    client.post("/prompts/del-test/versions", json={"version": "1.0.0", "template": "bye"})
    resp = client.delete("/prompts/del-test/versions/1.0.0")
    assert resp.status_code == 204


def test_cannot_delete_production(client):
    client.post("/prompts/nodelete/versions", json={"version": "1.0.0", "template": "keep"})
    client.patch("/prompts/nodelete/versions/1.0.0/eval-scores",
                 json={"relevance": 0.95, "faithfulness": 0.93})
    client.post("/prompts/nodelete/versions/1.0.0/promote")
    resp = client.delete("/prompts/nodelete/versions/1.0.0")
    assert resp.status_code == 409