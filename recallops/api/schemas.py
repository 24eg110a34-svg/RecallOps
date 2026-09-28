"""Request/response schemas for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateIncidentRequest(BaseModel):
    scenario_id: str = Field(default="INC-A1", description="Seeded scenario id, e.g. INC-A1")
    incident_id: str | None = None
    memory_enabled: bool = True
    auto_analyze: bool = False
    run_id: str = ""


class AnalyzeRequest(BaseModel):
    memory_enabled: bool = True
    reason: str = ""


class AdvanceRequest(BaseModel):
    analyze: bool = True
    steps: int = Field(default=1, ge=1, le=10)


class ApproveRequest(BaseModel):
    approved_by: str = "oncall"
    note: str = ""


class RejectRequest(BaseModel):
    rejected_by: str = "oncall"
    reason: str = ""


class ExecuteRequest(BaseModel):
    approved_by: str = "oncall"
    what_if_only: bool = False


class ResolveRequest(BaseModel):
    root_cause_id: str | None = None
    resolution: str | None = None
    resolved_by: str = "incident-commander"


class FeedbackRequest(BaseModel):
    target_type: Literal["hypothesis", "action"] = "hypothesis"
    target_id: str
    verdict: Literal["correct", "incorrect", "helpful", "not_helpful"] = "correct"
    comment: str = ""
    corrected_cause: str = ""
    author: str = "oncall"


class WhatIfRequest(BaseModel):
    action: str = Field(description="Action definition id, e.g. rollback_recent_deploy")


class RetainRequest(BaseModel):
    include_diagnostics: bool = True


class DemoStartRequest(BaseModel):
    scenario_id: str = "INC-A1"
    auto_analyze: bool = True
    speed: float = Field(default=4.0, ge=0.25, le=50.0)
    auto_advance: bool = True
    auto_resolve: bool = False


class DemoAdvanceRequest(BaseModel):
    steps: int = Field(default=1, ge=1, le=20)
    analyze: bool = True


class ResetRequest(BaseModel):
    wipe_memory: bool = True
    seed: bool = True
    recreate_incidents: bool = True


class RunDemoRequest(BaseModel):
    scenarios: list[str] = Field(default_factory=lambda: ["INC-A1", "INC-A2"])
    run_comparison: bool = True


class ComparisonRunRequest(BaseModel):
    persist: bool = True


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "error"] = "ok"
    detail: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AnalyzeRequest",
    "ApproveRequest",
    "ComparisonRunRequest",
    "CreateIncidentRequest",
    "DemoAdvanceRequest",
    "DemoStartRequest",
    "ExecuteRequest",
    "FeedbackRequest",
    "HealthResponse",
    "RejectRequest",
    "ResetRequest",
    "ResolveRequest",
    "RetainRequest",
    "RunDemoRequest",
    "WhatIfRequest",
]
