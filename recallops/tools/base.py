"""Tool layer.

The agent can only reach the world through these tools, and the backend
validates every call: unknown tool, bad parameters or a missing incident is a
structured error, never a traceback. Read-only tools read simulator/DB state;
write tools record rows. Nothing here talks to real infrastructure - actions are
executed by the deterministic simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from recallops.security import redact_obj


class ToolError(Exception):
    """A tool call that failed validation or execution."""

    def __init__(self, tool: str, message: str, *, code: str = "tool_error", detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.tool = tool
        self.message = message
        self.code = code
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "code": self.code, "message": self.message, "detail": self.detail}


class ToolResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool: str
    ok: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- params


class GetIncidentContextParams(BaseModel):
    incident_id: str


class GetRecentDeploymentsParams(BaseModel):
    incident_id: str
    hours: int = Field(default=24, ge=1, le=168)


class QueryLogsParams(BaseModel):
    incident_id: str
    pattern: str = ""
    limit: int = Field(default=20, ge=1, le=200)
    level: str | None = None


class GetServiceHealthParams(BaseModel):
    incident_id: str
    include_dependencies: bool = True


class GetMetricsParams(BaseModel):
    incident_id: str
    metrics: list[str] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=200)


class GetDependenciesParams(BaseModel):
    incident_id: str


class RecallMemoryParams(BaseModel):
    incident_id: str
    query: str = ""
    limit: int = Field(default=10, ge=1, le=50)
    exclude_current_incident: bool = True


class RecordActionParams(BaseModel):
    incident_id: str
    action_id: str
    status: str = "proposed"
    decision_reason: str = ""
    decided_by: str = ""


class ExecuteDemoActionParams(BaseModel):
    incident_id: str
    action_id: str
    approved_by: str = ""
    what_if_only: bool = False


class RecordFeedbackParams(BaseModel):
    incident_id: str
    target_type: str
    target_id: str
    verdict: str
    comment: str = ""
    corrected_cause: str = ""
    author: str = "oncall"


class RetainMemoryParams(BaseModel):
    incident_id: str
    include_diagnostics: bool = True


# --------------------------------------------------------------------------- registry


@dataclass
class ToolSpec:
    name: str
    description: str
    params_model: type[BaseModel]
    handler: Callable[..., dict[str, Any]]
    read_only: bool = False
    requires_approval: bool = False
    tags: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "read_only": self.read_only,
            "requires_approval": self.requires_approval,
            "params": sorted(self.params_model.model_fields.keys()),
            "tags": self.tags,
        }


class ToolRegistry:
    """Holds the tool table and dispatches validated calls."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        *,
        description: str,
        params_model: type[BaseModel],
        handler: Callable[..., dict[str, Any]],
        read_only: bool = False,
        requires_approval: bool = False,
        tags: Iterable[str] = (),
    ) -> None:
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            params_model=params_model,
            handler=handler,
            read_only=read_only,
            requires_approval=requires_approval,
            tags=list(tags),
        )

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(name, f"unknown tool '{name}'", code="unknown_tool", detail={"available": self.names()})
        return spec

    def describe(self) -> list[dict[str, Any]]:
        return [self._tools[n].describe() for n in self.names()]

    def call(self, name: str, **kwargs: Any) -> ToolResult:
        spec = self.get(name)
        try:
            params = spec.params_model.model_validate(kwargs)
        except ValidationError as exc:
            raise ToolError(name, f"invalid parameters for {name}", code="invalid_params", detail={"errors": exc.errors(include_url=False)[:5]}) from exc
        try:
            data = spec.handler(params)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - tools must never crash the API
            from recallops.security import scrub_exception

            return ToolResult(tool=name, ok=False, error={"code": "tool_failed", "message": scrub_exception(exc)})
        return ToolResult(tool=name, ok=True, data=redact_obj(data) if isinstance(data, dict) else {"value": data})


__all__ = [
    "ExecuteDemoActionParams",
    "GetDependenciesParams",
    "GetIncidentContextParams",
    "GetMetricsParams",
    "GetRecentDeploymentsParams",
    "GetServiceHealthParams",
    "QueryLogsParams",
    "RecallMemoryParams",
    "RecordActionParams",
    "RecordFeedbackParams",
    "RetainMemoryParams",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
]
