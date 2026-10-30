"""ORM mapping of the assets schema (see migrations/0001_init.up.sql, the source of truth)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Unicode,
    UnicodeText,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from itops_assets.app import module

Base = module.Base


def utcnow() -> dt.datetime:
    # Stored as naive UTC (datetime2 has no offset).
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Record(Base):
    __tablename__ = "records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type_key: Mapped[str] = mapped_column(Unicode(60))
    name: Mapped[str] = mapped_column(Unicode(300))
    fields: Mapped[str | None] = mapped_column(UnicodeText, default=None)  # JSON values
    created_by: Mapped[str] = mapped_column(Unicode(200))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    updated_by: Mapped[str] = mapped_column(Unicode(200))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())


class RecordField(Base):
    """One value of a record's field (queryable; rebuilt on every change)."""

    __tablename__ = "record_fields"

    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.records.id"), primary_key=True)
    field_key: Mapped[str] = mapped_column(Unicode(40), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(Unicode(20))
    value_text: Mapped[str | None] = mapped_column(Unicode(450), default=None)
    value_date: Mapped[dt.date | None] = mapped_column(Date, default=None)
    value_number: Mapped[float | None] = mapped_column(Float, default=None)


class Identity(Base):
    """A normalised serial, MAC or host name of a record (ADR 0017; rebuilt with it)."""

    __tablename__ = "identities"

    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.records.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(Unicode(10), primary_key=True)
    value: Mapped[str] = mapped_column(Unicode(200), primary_key=True)


class AcceptedFinding(Base):
    """A disagreement a person accepted as expected (evidence.Finding), while its facts hold."""

    __tablename__ = "accepted_findings"

    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.records.id"), primary_key=True)
    code: Mapped[str] = mapped_column(Unicode(40), primary_key=True)
    facts: Mapped[str] = mapped_column(Unicode(400))
    reason: Mapped[str] = mapped_column(Unicode(500))
    accepted_by: Mapped[str] = mapped_column(Unicode(200))
    accepted_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())


class FilledField(Base):
    """A field a connector owns that a person (or a merge) filled while the source had no
    value: kept on sync until the source sends one (ADR 0017)."""

    __tablename__ = "filled_fields"

    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.records.id"), primary_key=True)
    field_key: Mapped[str] = mapped_column(Unicode(40), primary_key=True)


class Proposal(Base):
    """An AI-drafted new record, change or merge, waiting for a person (assets.publish)."""

    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    merge_into: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)  # merge it here
    type_key: Mapped[str] = mapped_column(Unicode(60))
    name: Mapped[str] = mapped_column(Unicode(300))
    fields: Mapped[str | None] = mapped_column(UnicodeText, default=None)  # a new record's
    changes: Mapped[str | None] = mapped_column(UnicodeText, default=None)  # a change: JSON delta
    note: Mapped[str] = mapped_column(Unicode(500), default="")
    status: Mapped[str] = mapped_column(Unicode(20), default="pending")
    proposed_by: Mapped[str] = mapped_column(Unicode(200))
    proposed_via_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    decided_by: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)


class OsTarget(Base):
    """An operating system devices are checked against, with its hardware requirements
    (ADR 0019; see ``readiness.py``). Kept by admins (``assets.targets``)."""

    __tablename__ = "os_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Unicode(100))
    upgrades_from: Mapped[str] = mapped_column(UnicodeText)  # JSON list of system names
    processors: Mapped[str] = mapped_column(UnicodeText, default="")  # one pattern a line
    min_memory_gb: Mapped[float | None] = mapped_column(Float, default=None)
    min_storage_gb: Mapped[float | None] = mapped_column(Float, default=None)
    min_tpm: Mapped[str | None] = mapped_column(Unicode(10), default=None)
    source: Mapped[str] = mapped_column(Unicode(500), default="")
    notes: Mapped[str] = mapped_column(Unicode(2000), default="")
    updated_by: Mapped[str] = mapped_column(Unicode(200))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())


class ComputerClass(Base):
    """The model patterns that put a computer in a tier (Engineering or Standard), kept by
    admins (``assets.classes``; see ``classes.py``). A missing row means the preset."""

    __tablename__ = "computer_classes"

    tier: Mapped[str] = mapped_column(Unicode(20), primary_key=True)
    models: Mapped[str] = mapped_column(UnicodeText, default="")
    updated_by: Mapped[str] = mapped_column(Unicode(200))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
