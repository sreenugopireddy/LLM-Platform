"""
Inference Router — PTU vs PAYG optimiser.

Strategy:
  1. Try PTU (provisioned throughput) first — it's cheaper at scale
     because you're paying for reserved capacity regardless of usage.
  2. If PTU returns 429 (capacity exceeded) or times out, fall back
     to PAYG (pay-as-you-go) immediately — no user-visible latency spike.
  3. Track which endpoint served each request for cost attribution.

Why this matters:
  PTU costs are fixed (e.g. $3000/month for 300K TPM reserved).
  PAYG costs are per-token (e.g. $0.005/1K tokens for GPT-4o).
  At high traffic, PTU is ~40–60% cheaper. At low traffic, PAYG wins.
  The router maximises PTU utilisation without sacrificing reliability.
"""
import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Optional, Tuple

import httpx
from fastapi import HTTPException

from .cost_tracker import CostTracker, CostRecord
from .config import InferenceSettings

logger = logging.getLogger("inference.router")

# Azure OpenAI errors that mean PTU is at capacity → fall back to PAYG
PTU_FALLBACK_STATUS_CODES = {429, 503}

# Pricing per 1K tokens (input / output) — update as Azure publishes new rates
# https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/
MODEL_PRICING = {
    "gpt-4o":            {"input": 0.005,  "output": 0.015},
    "gpt-4o-mini":       {"input": 0.00015,"output": 0.0006},
    "gpt-35-turbo":      {"input": 0.0005, "output": 0.0015},
    "gpt-4o-2024-11-20": {"input": 0.0025, "output": 0.010},
}
DEFAULT_PRICING = {"input": 0.001, "output": 0.002}


class InferenceRouter:
    def __init__(self, settings: InferenceSettings, cost_tracker: CostTracker):
        self.settings = settings
        self.cost_tracker = cost_tracker
        self._ptu_client: Optional[httpx.AsyncClient] = None
        self._payg_client: Optional[httpx.AsyncClient] = None

    async def startup(self):
        timeout = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
        limits  = httpx.Limits(max_connections=50, max_keepalive_connections=10)

        if self.settings.azure_oai_ptu_endpoint:
            self._ptu_client = httpx.AsyncClient(
                base_url=self.settings.azure_oai_ptu_endpoint,
                headers={"api-key": self.settings.azure_oai_ptu_key},
                timeout=timeout,
                limits=limits,
            )

        if self.settings.azure_oai_payg_endpoint:
            self._payg_client = httpx.AsyncClient(
                base_url=self.settings.azure_oai_payg_endpoint,
                headers={"api-key": self.settings.azure_oai_payg_key},
                timeout=timeout,
                limits=limits,
            )

        if not self._ptu_client and not self._payg_client:
            logger.warning("No Azure OpenAI endpoints configured — using mock mode")

    async def shutdown(self):
        for client in [self._ptu_client, self._payg_client]:
            if client:
                await client.aclose()

    # ── Buffered (non-streaming) ──────────────────────────────────────────────

    async def complete(
        self, body: dict, team_id: str = "unknown", user_id: str = "unknown"
    ) -> Tuple[dict, CostRecord]:
        model = body.get("model", "gpt-4o-mini")
        start = time.monotonic()

        # Try PTU first
        if self._ptu_client:
            try:
                response = await self._ptu_client.post(
                    self._build_path(model), json=self._strip_metadata(body)
                )
                if response.status_code not in PTU_FALLBACK_STATUS_CODES:
                    data = response.json()
                    cost = self._record_cost(data, model, "ptu", team_id, start)
                    logger.info("PTU served | model=%s team=%s tokens=%s cost=$%.5f",
                                model, team_id, data.get("usage", {}).get("total_tokens"), cost.total_usd)
                    return data, cost
                else:
                    logger.warning("PTU capacity exceeded (status=%d) — falling back to PAYG", response.status_code)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                logger.warning("PTU timeout/error (%s) — falling back to PAYG", exc)

        # PAYG fallback
        if self._payg_client:
            response = await self._payg_client.post(
                self._build_path(model), json=self._strip_metadata(body)
            )
            if response.status_code != 200:
                raise HTTPException(response.status_code, detail=response.json())
            data = response.json()
            cost = self._record_cost(data, model, "payg", team_id, start)
            logger.info("PAYG served | model=%s team=%s tokens=%s cost=$%.5f",
                        model, team_id, data.get("usage", {}).get("total_tokens"), cost.total_usd)
            return data, cost

        # Mock mode (no endpoints configured — useful for local dev without Azure creds)
        record = CostRecord(model=model, endpoint="mock", input_tokens=10, output_tokens=20,
                            total_usd=0.0, team_id=team_id, latency_ms=50.0)
        self.cost_tracker.record(record)
        return self._mock_response(body), record

    # ── Streaming ─────────────────────────────────────────────────────────────

    async def stream(
        self, body: dict, team_id: str = "unknown", user_id: str = "unknown"
    ) -> AsyncGenerator[bytes, None]:
        model = body.get("model", "gpt-4o-mini")
        client = self._ptu_client or self._payg_client
        endpoint_type = "ptu" if client is self._ptu_client else "payg"

        if not client:
            # Mock streaming for local dev
            yield b"data: " + json.dumps(self._mock_stream_chunk("Hello from mock!")).encode() + b"\n\n"
            yield b"data: [DONE]\n\n"
            return

        stream_body = {**self._strip_metadata(body), "stream": True}
        start = time.monotonic()
        total_tokens = 0

        try:
            async with client.stream("POST", self._build_path(model), json=stream_body) as response:
                if response.status_code in PTU_FALLBACK_STATUS_CODES and self._payg_client:
                    # PTU capacity exceeded mid-stream — can't transparently fallback
                    # so we yield an error event the client can handle
                    logger.warning("PTU capacity exceeded during stream — sending fallback signal")
                    yield b"data: " + json.dumps({"error": "ptu_capacity", "fallback": "payg"}).encode() + b"\n\n"
                    return

                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            yield b"data: [DONE]\n\n"
                            break
                        try:
                            chunk = json.loads(payload)
                            # Accumulate token count from stream chunks that include usage
                            if "usage" in chunk:
                                total_tokens = chunk["usage"].get("total_tokens", 0)
                            yield f"data: {json.dumps(chunk)}\n\n".encode()
                        except json.JSONDecodeError:
                            continue

            # Record approximate cost (streaming doesn't always return token counts)
            latency_ms = (time.monotonic() - start) * 1000
            if total_tokens > 0:
                pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
                approx_cost = total_tokens / 1000 * (pricing["input"] + pricing["output"]) / 2
                self.cost_tracker.record(CostRecord(
                    model=model, endpoint=endpoint_type,
                    input_tokens=total_tokens // 2, output_tokens=total_tokens // 2,
                    total_usd=approx_cost, team_id=team_id, latency_ms=latency_ms,
                ))

        except httpx.TimeoutException:
            yield b"data: " + json.dumps({"error": "upstream_timeout"}).encode() + b"\n\n"

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_path(self, model: str) -> str:
        """Azure OpenAI uses deployment names in the path."""
        deployment = self.settings.model_deployment_map.get(model, model)
        return f"/openai/deployments/{deployment}/chat/completions?api-version={self.settings.api_version}"

    def _strip_metadata(self, body: dict) -> dict:
        """Remove our internal metadata before sending to Azure OpenAI."""
        clean = {k: v for k, v in body.items() if k not in ("metadata", "experiment_id")}
        return clean

    def _record_cost(
        self, response_data: dict, model: str, endpoint: str, team_id: str, start: float
    ) -> CostRecord:
        usage = response_data.get("usage", {})
        input_tokens  = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
        total_usd = (input_tokens / 1000 * pricing["input"]) + (output_tokens / 1000 * pricing["output"])
        latency_ms = (time.monotonic() - start) * 1000

        record = CostRecord(
            model=model, endpoint=endpoint,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_usd=total_usd, team_id=team_id, latency_ms=latency_ms,
        )
        self.cost_tracker.record(record)
        return record

    def _mock_response(self, body: dict) -> dict:
        """Returns a realistic-looking mock response for local dev without Azure creds."""
        messages = body.get("messages", [])
        last_msg = messages[-1].get("content", "hello") if messages else "hello"
        return {
            "id": "chatcmpl-mock-001",
            "object": "chat.completion",
            "model": body.get("model", "gpt-4o-mini"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": f"[MOCK] Echo: {last_msg}"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

    def _mock_stream_chunk(self, content: str) -> dict:
        return {
            "id": "chatcmpl-mock-001",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }