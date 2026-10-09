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


class Proposal(Base):
    """An AI-drafted new record or change, waiting for a person (assets.publish)."""

    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    type_key: Mapped[str] = mapped_column(Unicode(60))
    name: Mapped[str] = mapped_column(Unicode(300))
    fields: Mapped[str | None] = mapped_column(UnicodeText, default=None)
    note: Mapped[str] = mapped_column(Unicode(500), default="")
    status: Mapped[str] = mapped_column(Unicode(20), default="pending")
    proposed_by: Mapped[str] = mapped_column(Unicode(200))
    proposed_via_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    decided_by: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
