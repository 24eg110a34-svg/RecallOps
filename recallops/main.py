"""FastAPI application factory.

    uvicorn recallops.main:app --reload
    # or, from the repo root:
    uvicorn apps.api.main:app
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from recallops import __version__
from recallops.agent.orchestrator import ActionNotFound, IncidentNotFound, InvalidTransition
from recallops.api.deps import get_container
from recallops.api.routes_demo import router as demo_router
from recallops.api.routes_health import router as health_router
from recallops.api.routes_incidents import router as incidents_router
from recallops.api.routes_insights import router as insights_router
from recallops.config import get_settings
from recallops.security import scrub_exception
from recallops.services.resilience import ProviderError
from recallops.tools.base import ToolError

logger = logging.getLogger("recallops")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

DESCRIPTION = """
**RecallOps** - AI incident intelligence with Hindsight persistent memory.

Incident -> Evidence -> Memory recall -> Hypotheses -> Root cause -> Planned action ->
Safety gate -> Human approval -> Simulated outcome -> Postmortem -> Durable memory.

The demo is driven by a deterministic simulator (`scenarios/`), so every number in
the UI and in the Memory OFF/ON comparison is measured, not staged.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    container = get_container()
    settings = container.settings
    logger.info("RecallOps %s starting (env=%s, demo=%s)", __version__, settings.environment, settings.demo_mode)
    memory_health = container.memory.health()
    logger.info("Memory layer: %s (%s) - %s", memory_health.state, memory_health.mode.value, memory_health.detail)
    logger.info("LLM provider: %s", container.llm.describe())
    logger.info("Scenarios: %s", ", ".join(container.orchestrator.scenarios.ids()))

    if settings.demo_mode and settings.demo_seed_on_start:
        from recallops.services.seed_story import is_empty, seed_story

        if is_empty(container.orchestrator):
            logger.info("Empty database detected - playing the demo story once so the console opens on a real investigation...")
            try:
                # Seeding is synchronous I/O; keep it off the event loop.
                import anyio

                summary = await anyio.to_thread.run_sync(
                    lambda: seed_story(orchestrator=container.orchestrator, memory=container.memory)
                )
                logger.info(
                    "Demo story ready: %s incident(s), %s memories, comparison off/on = %s",
                    len(summary.get("incidents", [])),
                    summary.get("memories_total"),
                    summary.get("comparison"),
                )
            except Exception as exc:  # noqa: BLE001 - never block startup on seeding
                logger.warning("Demo seeding skipped: %s", exc)
    yield
    logger.info("RecallOps shutting down")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="RecallOps - AI Incident Response Agent",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Memory-Mode"],
    )

    app.include_router(health_router)
    app.include_router(incidents_router)
    app.include_router(demo_router)
    app.include_router(insights_router)

    @app.exception_handler(IncidentNotFound)
    async def _incident_not_found(_request: Request, exc: IncidentNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": scrub_exception(exc)})

    @app.exception_handler(ActionNotFound)
    async def _action_not_found(_request: Request, exc: ActionNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": scrub_exception(exc)})

    @app.exception_handler(InvalidTransition)
    async def _invalid_transition(_request: Request, exc: InvalidTransition) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": scrub_exception(exc)})

    @app.exception_handler(ProviderError)
    async def _provider_error(_request: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": exc.to_dict()})

    @app.exception_handler(ToolError)
    async def _tool_error(_request: Request, exc: ToolError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": exc.to_dict()})

    @app.get("/", tags=["meta"])
    def root() -> dict[str, Any]:
        container = get_container()
        return {
            "app": "RecallOps",
            "version": __version__,
            "docs": "/docs",
            "scenarios": container.orchestrator.scenarios.ids(),
            "endpoints": {
                "health": "/health",
                "create_incident": "POST /api/incidents",
                "analyze": "POST /api/incidents/{id}/analyze",
                "stream": "GET /api/incidents/{id}/stream",
                "approve": "POST /api/incidents/{id}/actions/{action_id}/approve",
                "resolve": "POST /api/incidents/{id}/resolve",
                "demo_run": "POST /api/demo/run",
                "demo_reset": "POST /api/demo/reset",
                "comparison": "POST /api/comparison/{scenario}/run",
                "analytics": "GET /api/analytics",
            },
        }

    return app


app = create_app()

__all__ = ["app", "create_app"]
