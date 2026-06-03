"""
LLM Platform — Inference Service
Wraps Azure OpenAI with:
  - PTU vs PAYG routing (cost optimiser)
  - Per-request cost tracking (OTel)
  - Streaming SSE support
  - Automatic retry + fallback on PTU capacity errors
  - Request/response logging with token counts
"""
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from .router import InferenceRouter
from .cost_tracker import CostTracker
from .config import InferenceSettings
from .metrics_router import build_cost_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("inference")

settings = InferenceSettings()
cost_tracker = CostTracker()
router = InferenceRouter(settings, cost_tracker)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await router.startup()
    logger.info("Inference service started")
    logger.info("  PTU  endpoint : %s", settings.azure_oai_ptu_endpoint or "not configured (mock mode)")
    logger.info("  PAYG endpoint : %s", settings.azure_oai_payg_endpoint or "not configured (mock mode)")
    yield
    await router.shutdown()


app = FastAPI(title="LLM Platform Inference", version="1.0.0", lifespan=lifespan)

# Mount cost metrics routes at /metrics/cost and /metrics/cost/teams/{team_id}
app.include_router(build_cost_router(cost_tracker))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "inference",
        "mode": "live" if (settings.azure_oai_ptu_endpoint or settings.azure_oai_payg_endpoint) else "mock",
        "ptu_configured": bool(settings.azure_oai_ptu_endpoint),
        "payg_configured": bool(settings.azure_oai_payg_endpoint),
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    if "messages" not in body:
        raise HTTPException(400, detail={"error": "messages field required"})

    stream  = body.get("stream", False)
    team_id = body.get("metadata", {}).get("team_id", "unknown")
    user_id = body.get("metadata", {}).get("user_id", "unknown")

    if stream:
        return StreamingResponse(
            router.stream(body, team_id=team_id, user_id=user_id),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    response, cost_record = await router.complete(body, team_id=team_id, user_id=user_id)

    # Attach cost info to response headers for the gateway to log
    return JSONResponse(
        content=response,
        headers={
            "X-Cost-USD":       f"{cost_record.total_usd:.6f}",
            "X-Tokens-Total":   str(cost_record.total_tokens),
            "X-Endpoint-Type":  cost_record.endpoint,
        },
    )