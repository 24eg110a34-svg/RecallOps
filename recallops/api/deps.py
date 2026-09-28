"""FastAPI dependencies: the app container and per-request accessors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from recallops.agent.orchestrator import IncidentOrchestrator
from recallops.agent.safety import SafetyGate
from recallops.config import Settings, get_settings
from recallops.memory.factory import get_memory_adapter
from recallops.memory.port import MemoryPort
from recallops.persistence.db import get_sessionmaker, init_db
from recallops.services.llm.base import LLMProvider
from recallops.services.llm.factory import get_llm_provider
from recallops.tools.base import ToolRegistry
from recallops.tools.registry import ToolContext, build_registry


@dataclass
class AppContainer:
    """Wiring created once at startup and reused by every request."""

    settings: Settings
    orchestrator: IncidentOrchestrator
    memory: MemoryPort
    llm: LLMProvider
    tools: ToolRegistry
    safety: SafetyGate
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def session_factory(self) -> Any:
        return get_sessionmaker


def build_container() -> AppContainer:
    settings = get_settings()
    init_db()
    memory = get_memory_adapter()
    llm = get_llm_provider()
    safety = SafetyGate()
    orchestrator = IncidentOrchestrator(
        session_factory=get_sessionmaker,
        settings=settings,
        memory=memory,
        llm=llm,
        safety=safety,
    )
    tools = build_registry(
        ToolContext(
            orchestrator=orchestrator,
            session_factory=get_sessionmaker,
            memory=memory,
            scenarios=orchestrator.scenarios,
        )
    )
    return AppContainer(settings=settings, orchestrator=orchestrator, memory=memory, llm=llm, tools=tools, safety=safety)


_container: AppContainer | None = None


def get_container() -> AppContainer:
    global _container
    if _container is None:
        _container = build_container()
    return _container


def set_container(container: AppContainer | None) -> None:
    global _container
    _container = container


__all__ = ["AppContainer", "build_container", "get_container", "set_container"]
