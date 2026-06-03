"""
Cost Tracker — per-request cost tracking with OpenTelemetry.

Every inference request records:
  - input_tokens, output_tokens, total_usd
  - model, endpoint (ptu/payg), team_id
  - latency_ms

These flow into:
  1. OTel metrics → Azure Monitor → cost dashboards
  2. In-memory aggregator → /metrics/cost endpoint for the dashboard
  3. (Phase 4) Cosmos DB → durable per-team chargeback records

Why this is FAANG-grade:
  Most demos call OpenAI and ignore the bill.
  Production platforms track cost per team, per model, per day.
  At 1M requests/day × $0.001/request = $1000/day — you need visibility.
"""
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, List, Optional

from opentelemetry import metrics

logger = logging.getLogger("inference.cost")

# ── OTel instruments ──────────────────────────────────────────────────────────
meter = metrics.get_meter("inference.cost")

_cost_counter    = meter.create_counter("llm.cost.usd",          description="Cumulative USD spent on LLM inference")
_token_counter   = meter.create_counter("llm.tokens.total",      description="Total tokens consumed")
_input_counter   = meter.create_counter("llm.tokens.input",      description="Input (prompt) tokens")
_output_counter  = meter.create_counter("llm.tokens.output",     description="Output (completion) tokens")
_latency_hist    = meter.create_histogram("llm.latency.ms",      description="Inference latency in milliseconds")
_request_counter = meter.create_counter("llm.requests.total",    description="Total inference requests")
_ptu_counter     = meter.create_counter("llm.requests.ptu",      description="Requests served by PTU")
_payg_counter    = meter.create_counter("llm.requests.payg",     description="Requests served by PAYG")


@dataclass
class CostRecord:
    model: str
    endpoint: str           # "ptu" | "payg" | "mock"
    input_tokens: int
    output_tokens: int
    total_usd: float
    team_id: str
    latency_ms: float
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class TeamAggregate:
    team_id: str
    total_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_usd: float = 0.0
    ptu_requests: int = 0
    payg_requests: int = 0
    last_request_at: str = ""


class CostTracker:
    """
    Thread-safe in-memory cost aggregator.
    Emits OTel metrics on every record() call.
    Exposes get_summary() for the /metrics/cost dashboard endpoint.
    """

    def __init__(self, max_recent: int = 1000):
        self._lock = Lock()
        self._team_aggregates: Dict[str, TeamAggregate] = defaultdict(
            lambda: TeamAggregate(team_id="unknown")
        )
        self._recent: List[CostRecord] = []   # ring buffer, last N requests
        self._max_recent = max_recent
        self._total_usd = 0.0
        self._total_requests = 0

    def record(self, record: CostRecord) -> None:
        """Call after every inference request. Thread-safe."""
        attrs = {
            "model":    record.model,
            "endpoint": record.endpoint,
            "team":     record.team_id,
        }

        # ── OTel metrics (picked up by Azure Monitor exporter) ────────────────
        _cost_counter.add(record.total_usd, attrs)
        _input_counter.add(record.input_tokens, attrs)
        _output_counter.add(record.output_tokens, attrs)
        _token_counter.add(record.total_tokens, attrs)
        _latency_hist.record(record.latency_ms, attrs)
        _request_counter.add(1, attrs)

        if record.endpoint == "ptu":
            _ptu_counter.add(1, {"team": record.team_id, "model": record.model})
        elif record.endpoint == "payg":
            _payg_counter.add(1, {"team": record.team_id, "model": record.model})

        # ── In-memory aggregation ─────────────────────────────────────────────
        with self._lock:
            agg = self._team_aggregates[record.team_id]
            agg.team_id = record.team_id
            agg.total_requests     += 1
            agg.total_input_tokens += record.input_tokens
            agg.total_output_tokens += record.output_tokens
            agg.total_usd          += record.total_usd
            agg.last_request_at     = record.timestamp
            if record.endpoint == "ptu":
                agg.ptu_requests += 1
            elif record.endpoint == "payg":
                agg.payg_requests += 1

            self._total_usd      += record.total_usd
            self._total_requests += 1

            # Ring buffer
            self._recent.append(record)
            if len(self._recent) > self._max_recent:
                self._recent.pop(0)

        logger.debug(
            "cost_record team=%s model=%s endpoint=%s tokens=%d usd=%.5f latency_ms=%.1f",
            record.team_id, record.model, record.endpoint,
            record.total_tokens, record.total_usd, record.latency_ms,
        )

    def get_summary(self) -> dict:
        """Returns a snapshot for the /metrics/cost endpoint."""
        with self._lock:
            teams = {tid: asdict(agg) for tid, agg in self._team_aggregates.items()}
            recent = [asdict(r) for r in self._recent[-50:]]  # last 50 requests

        return {
            "total_usd":      round(self._total_usd, 6),
            "total_requests": self._total_requests,
            "by_team":        teams,
            "recent_requests": recent,
        }

    def get_team_cost(self, team_id: str) -> Optional[TeamAggregate]:
        with self._lock:
            return self._team_aggregates.get(team_id)