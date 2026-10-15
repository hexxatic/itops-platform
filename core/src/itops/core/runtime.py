"""The live services the core hands to each module when it is loaded."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itops.core.actions import ActionRunner
from itops.core.ai import AIService
from itops.core.areas import Areas
from itops.core.audit import AuditLog
from itops.core.bus import EventBus
from itops.core.config import Settings
from itops.core.connectors import ConnectorRunner, SourceRefs
from itops.core.files import FileStore
from itops.core.forms import FormKeeper
from itops.core.jobs import JobRunner
from itops.core.people import People
from itops.core.registry import ServiceRegistry
from itops.core.search import SearchIndex
from itops.core.suggest import SuggestionRunner
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
    jobs: JobRunner = field(default_factory=JobRunner)
    actions: ActionRunner = field(default_factory=ActionRunner)
    areas: Areas = field(default_factory=Areas)
    people: People = field(default_factory=People)
    suggestions: SuggestionRunner = field(default_factory=SuggestionRunner)
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
