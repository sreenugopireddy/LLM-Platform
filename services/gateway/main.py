"""
LLM Platform — API Gateway
"""
import time
import logging
import httpx
from contextlib import asynccontextmanager
from typing import List, Optional, Any, Dict
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from opentelemetry import metrics, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    _OTLP_AVAILABLE = True
except ImportError:
    _OTLP_AVAILABLE = False

from .auth import verify_token, TokenPayload
from .ab_router import get_variant
from .rate_limiter import RateLimiter
from .config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("gateway")
settings = Settings()

provider = TracerProvider()
if settings.otel_endpoint and _OTLP_AVAILABLE:
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint)))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("gateway")

meter_provider = MeterProvider()
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("gateway")
request_counter    = meter.create_counter("gateway.requests.total")
latency_hist       = meter.create_histogram("gateway.latency.ms")
rate_limit_counter = meter.create_counter("gateway.rate_limited.total")

http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        base_url=settings.inference_service_url,
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    logger.info("Gateway started — inference: %s", settings.inference_service_url)
    yield
    await http_client.aclose()


app = FastAPI(title="LLM Platform Gateway", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

security = HTTPBearer()
rate_limiter = RateLimiter(
    redis_url=settings.redis_url,
    default_rpm=settings.default_rpm,
    default_tpm=settings.default_tpm,
)


# ── Pydantic models — makes Swagger UI show request body ─────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    model: str = "gpt-4o-mini"
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stream: Optional[bool] = False
    experiment_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = {"json_schema_extra": {"example": {
        "messages": [{"role": "user", "content": "Hello!"}],
        "model": "gpt-4o-mini"
    }}}


# ── Middleware ────────────────────────────────────────────────────────────────

@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    start = time.monotonic()
    request_id = request.headers.get("X-Request-ID", "")
    with tracer.start_as_current_span(f"{request.method} {request.url.path}") as span:
        try:
            response = await call_next(request)
            elapsed_ms = (time.monotonic() - start) * 1000
            team = request.headers.get("X-Team-ID", "unknown")
            attrs = {"path": request.url.path, "status": str(response.status_code), "team": team}
            request_counter.add(1, attrs)
            latency_hist.record(elapsed_ms, attrs)
            span.set_attribute("http.status_code", response.status_code)
            return response
        except Exception as exc:
            span.record_exception(exc)
            raise


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"service": "LLM Platform Gateway", "version": "1.0.0",
            "status": "running", "docs": "/docs", "health": "/health"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "gateway", "version": app.version}


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatRequest,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    token_payload: TokenPayload = verify_token(credentials.credentials, settings.jwt_secret)
    team_id = token_payload.team_id
    user_id = token_payload.sub

    body_dict = body.model_dump(exclude_none=True)

    allowed, headers = await rate_limiter.check(team_id, estimated_tokens=body.max_tokens or 1000)
    if not allowed:
        rate_limit_counter.add(1, {"team": team_id})
        raise HTTPException(status_code=429,
                            detail={"error": "rate_limit_exceeded", "team": team_id},
                            headers=headers)

    _check_model_permission(token_payload, body.model)

    experiment_id = body.experiment_id or settings.default_experiment
    if experiment_id:
        variant = get_variant(experiment_id, user_id)
        if variant:
            body_dict["model"] = variant.model
            body_dict.setdefault("metadata", {})["ab_variant"] = variant.name
            body_dict["metadata"]["experiment_id"] = experiment_id

    body_dict.setdefault("metadata", {})["team_id"] = team_id
    body_dict["metadata"]["user_id"] = user_id

    if body.stream:
        return await _stream_forward(body_dict)
    return await _buffered_forward(body_dict)


def _check_model_permission(payload: TokenPayload, model: str):
    gpt4_models = {"gpt-4o", "gpt-4o-2024-11-20"}
    if model in gpt4_models and "premium" not in payload.roles:
        raise HTTPException(403, detail={"error": "model_not_permitted",
                                         "model": model, "required_role": "premium"})


async def _buffered_forward(body: dict) -> JSONResponse:
    resp = await http_client.post("/v1/chat/completions", json=body)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


async def _stream_forward(body: dict) -> StreamingResponse:
    async def generate():
        async with http_client.stream("POST", "/v1/chat/completions", json=body) as r:
            async for chunk in r.aiter_bytes():
                yield chunk
    return StreamingResponse(generate(), media_type="text/event-stream")