"""RecallOps API routes."""

from recallops.api.routes_demo import router as demo_router
from recallops.api.routes_health import router as health_router
from recallops.api.routes_incidents import router as incidents_router
from recallops.api.routes_insights import router as insights_router

__all__ = ["demo_router", "health_router", "incidents_router", "insights_router"]
