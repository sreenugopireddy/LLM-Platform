from opentelemetry import metrics
from fastapi import Request
import time

meter = metrics.get_meter("llm-platform")
cost_counter = meter.create_counter("llm.cost.usd", description="USD per request")
latency_hist  = meter.create_histogram("llm.latency.ms")

# Pricing (update as Azure publishes new rates)
COST_PER_1K = {"gpt-4o": 0.005, "gpt-35-turbo": 0.0005}

async def cost_middleware(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    latency_ms = (time.monotonic() - start) * 1000
    
    body = request.state.__dict__.get("response_body", {})
    tokens = body.get("usage", {}).get("total_tokens", 0)
    model  = body.get("model", "unknown")
    team   = request.headers.get("x-team-id", "unknown")
    
    cost = tokens / 1000 * COST_PER_1K.get(model, 0.001)
    
    attrs = {"model": model, "team": team}
    cost_counter.add(cost, attrs)
    latency_hist.record(latency_ms, attrs)
    
    return response