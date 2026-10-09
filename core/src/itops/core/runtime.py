"""The live services the core hands to each module when it is loaded."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core.actions import ActionRunner
from itops.core.ai import AIService
from itops.core.audit import AuditLog
from itops.core.bus import EventBus
from itops.core.config import Settings
from itops.core.connectors import ConnectorRunner, SourceRefs
from itops.core.files import FileStore
from itops.core.forms import FormKeeper
from itops.core.registry import ServiceRegistry
from itops.core.search import SearchIndex
from itops.core.types import TypeStore


@dataclass
class Runtime:
    settings: Settings
    bus: EventBus
    registry: ServiceRegistry
    ai: AIService
    files: FileStore
    search: SearchIndex = field(default_factory=SearchIndex)
    audit: AuditLog = field(default_factory=AuditLog)
    types: TypeStore = field(default_factory=TypeStore)
    forms: FormKeeper = field(default_factory=FormKeeper)
    refs: SourceRefs = field(default_factory=SourceRefs)
    connectors: ConnectorRunner = field(default_factory=ConnectorRunner)
    actions: ActionRunner = field(default_factory=ActionRunner)
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
