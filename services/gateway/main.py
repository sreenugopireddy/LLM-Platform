"""
LLM Platform — API Gateway
Handles: JWT auth, RBAC, A/B routing, rate limiting, request forwarding.
All requests flow through here before hitting the inference service.
"""
import os
import time
import logging
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from opentelemetry import metrics, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# OTel OTLP exporter is optional — only needed when you wire up Azure Monitor
try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    _OTLP_AVAILABLE = True
except ImportError:
    _OTLP_AVAILABLE = False

from .auth import verify_token, TokenPayload
from .ab_router import get_variant
from .rate_limiter import RateLimiter
from .config import Settings

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("gateway")

settings = Settings()

# ── OpenTelemetry setup ───────────────────────────────────────────────────────
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

# ── HTTP client (shared, connection-pooled) ───────────────────────────────────
http_client: httpx.AsyncClient | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        base_url=settings.inference_service_url,
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    logger.info("Gateway started — inference backend: %s", settings.inference_service_url)
    yield
    await http_client.aclose()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="LLM Platform Gateway", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["POST", "GET"],
    allow_headers=["Authorization", "Content-Type", "X-Team-ID", "X-Request-ID"],
)

security = HTTPBearer()
rate_limiter = RateLimiter(
    redis_url=settings.redis_url,
    default_rpm=settings.default_rpm,
    default_tpm=settings.default_tpm,
)


# ── Middleware: request/response logging + OTel ────────────────────────────────
@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    start = time.monotonic()
    request_id = request.headers.get("X-Request-ID", "")

    with tracer.start_as_current_span(
        f"{request.method} {request.url.path}",
        attributes={"http.method": request.method, "http.path": request.url.path, "request_id": request_id},
    ) as span:
        try:
            response = await call_next(request)
            elapsed_ms = (time.monotonic() - start) * 1000

            team = request.headers.get("X-Team-ID", "unknown")
            attrs = {"path": request.url.path, "status": str(response.status_code), "team": team}
            request_counter.add(1, attrs)
            latency_hist.record(elapsed_ms, attrs)
            span.set_attribute("http.status_code", response.status_code)
            span.set_attribute("latency_ms", round(elapsed_ms, 1))

            logger.info(
                "req_id=%s method=%s path=%s status=%d latency_ms=%.1f team=%s",
                request_id, request.method, request.url.path, response.status_code, elapsed_ms, team
            )
            return response
        except Exception as exc:
            span.record_exception(exc)
            logger.exception("Unhandled error: %s", exc)
            raise


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "gateway", "version": app.version}


# ── Chat completions endpoint ─────────────────────────────────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    # 1. Auth — validate JWT and extract claims
    token_payload: TokenPayload = verify_token(credentials.credentials, settings.jwt_secret)
    team_id = token_payload.team_id
    user_id = token_payload.sub

    # 2. Rate limiting — token-bucket per team
    body = await request.json()
    allowed, headers = await rate_limiter.check(team_id, estimated_tokens=body.get("max_tokens", 1000))
    if not allowed:
        rate_limit_counter.add(1, {"team": team_id})
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limit_exceeded", "team": team_id},
            headers=headers,
        )

    # 3. RBAC — check team has permission for the requested model tier
    requested_model = body.get("model", "gpt-4o-mini")
    _check_model_permission(token_payload, requested_model)

    # 4. A/B routing — deterministic sticky variant assignment
    experiment_id = body.get("experiment_id") or settings.default_experiment
    if experiment_id:
        variant = get_variant(experiment_id, user_id)
        if variant:
            body["model"] = variant.model
            body.setdefault("metadata", {})["ab_variant"] = variant.name
            body["metadata"]["experiment_id"] = experiment_id

    # 5. Inject team chargeback tag for cost tracking downstream
    body.setdefault("metadata", {})["team_id"] = team_id
    body["metadata"]["user_id"] = user_id

    # 6. Forward to inference service (streaming or buffered)
    stream = body.get("stream", False)
    if stream:
        return await _stream_forward(body, request.headers)
    return await _buffered_forward(body, request.headers)


def _check_model_permission(payload: TokenPayload, model: str):
    """RBAC: only 'premium' tier teams can use GPT-4o."""
    gpt4_models = {"gpt-4o", "gpt-4o-2024-11-20"}
    if model in gpt4_models and "premium" not in payload.roles:
        raise HTTPException(
            403,
            detail={"error": "model_not_permitted", "model": model, "required_role": "premium"},
        )


async def _buffered_forward(body: dict, incoming_headers) -> JSONResponse:
    forward_headers = _build_forward_headers(incoming_headers)
    resp = await http_client.post("/v1/chat/completions", json=body, headers=forward_headers)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


async def _stream_forward(body: dict, incoming_headers) -> StreamingResponse:
    forward_headers = _build_forward_headers(incoming_headers)

    async def generate():
        async with http_client.stream("POST", "/v1/chat/completions", json=body, headers=forward_headers) as r:
            async for chunk in r.aiter_bytes():
                yield chunk

    return StreamingResponse(generate(), media_type="text/event-stream")


def _build_forward_headers(incoming_headers) -> dict:
    """Pass through tracing and team headers; strip the original auth token."""
    passthrough = {"X-Request-ID", "X-Team-ID", "X-B3-TraceId", "X-B3-SpanId"}
    return {k: v for k, v in incoming_headers.items() if k in passthrough}


# ── Auth endpoint — users call this to get a token ───────────────────────────

class TokenRequest(BaseModel):
    username: str
    team_id: str
    roles: List[str] = ["basic"]
    api_key: str  # shared API key to gate token issuance

    model_config = {"json_schema_extra": {"example": {
        "username": "user_001",
        "team_id": "team_acme",
        "roles": ["premium"],
        "api_key": "your-platform-api-key"
    }}}

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 86400

@app.post("/auth/token", response_model=TokenResponse, tags=["auth"])
async def get_token(body: TokenRequest):
    """
    Exchange an API key for a JWT token.
    Users call this once, then use the returned token for all API calls.
    """
    if body.api_key != settings.platform_api_key:
        raise HTTPException(401, detail={"error": "invalid_api_key"})
    from .auth import issue_token
    token = issue_token(
        sub=body.username,
        team_id=body.team_id,
        roles=body.roles,
        secret=settings.jwt_secret,
        ttl_seconds=86400,
    )
    return TokenResponse(access_token=token)