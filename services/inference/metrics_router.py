"""
Cost metrics API routes.
Mounted on the inference service at /metrics/cost.

These endpoints power the cost dashboard (Phase 5).
In production, protect these with internal-only RBAC.
"""
from fastapi import APIRouter, HTTPException
from .cost_tracker import CostTracker

# cost_tracker instance is injected when this router is mounted
_tracker: CostTracker = None


def build_cost_router(tracker: CostTracker) -> APIRouter:
    global _tracker
    _tracker = tracker
    router = APIRouter(prefix="/metrics", tags=["cost"])

    @router.get("/cost")
    async def get_cost_summary():
        """Full cost breakdown — total, by team, recent requests."""
        return _tracker.get_summary()

    @router.get("/cost/teams/{team_id}")
    async def get_team_cost(team_id: str):
        """Per-team cost aggregate for chargeback."""
        agg = _tracker.get_team_cost(team_id)
        if not agg:
            raise HTTPException(404, detail={"error": "team_not_found", "team_id": team_id})
        from dataclasses import asdict
        return asdict(agg)

    return router