"""ORM mapping of the contracts schema (see migrations/0001_init.up.sql, the source of truth)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Unicode,
    UnicodeText,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from itops_contracts.app import module

Base = module.Base


def utcnow() -> dt.datetime:
    # Stored as naive UTC (datetime2 has no offset).
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Contract(Base):
    __tablename__ = "contracts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Unicode(300))
    vendor: Mapped[str] = mapped_column(Unicode(200), default="")
    category: Mapped[str] = mapped_column(Unicode(40))
    owner_upn: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    description: Mapped[str | None] = mapped_column(UnicodeText, default=None)
    auto_renews: Mapped[bool] = mapped_column(Boolean, default=False)
    notice_days: Mapped[int] = mapped_column(Integer, default=0)
    links: Mapped[str | None] = mapped_column(UnicodeText, default=None)  # JSON
    status: Mapped[str] = mapped_column(Unicode(20), default="active")
    created_by: Mapped[str] = mapped_column(Unicode(200))
    created_via_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    updated_by: Mapped[str] = mapped_column(Unicode(200))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())


class Term(Base):
    __tablename__ = "terms"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.contracts.id"))
    starts_on: Mapped[dt.date] = mapped_column(Date)
    ends_on: Mapped[dt.date] = mapped_column(Date)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), default=None)
    cost_period: Mapped[str] = mapped_column(Unicode(10), default="year")
    po_number: Mapped[str] = mapped_column(Unicode(100), default="")
    seats_bought: Mapped[int | None] = mapped_column(Integer, default=None)
    seats_in_use: Mapped[int | None] = mapped_column(Integer, default=None)
    state: Mapped[str] = mapped_column(Unicode(20), default="active")
    attachments: Mapped[str | None] = mapped_column(UnicodeText, default=None)  # JSON list
    decided_by: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    decision_note: Mapped[str] = mapped_column(Unicode(500), default="")
    notified: Mapped[str] = mapped_column(Unicode(100), default="")
    overdue_notified_on: Mapped[dt.date | None] = mapped_column(Date, default=None)
    created_by: Mapped[str] = mapped_column(Unicode(200))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())


class Rate(Base):
    """What a term charges for each one used (a B&W page), and how many a month it includes."""

    __tablename__ = "rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    term_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.terms.id"))
    position: Mapped[int] = mapped_column(Integer)
    item: Mapped[str] = mapped_column(Unicode(200))
    price: Mapped[Decimal] = mapped_column(Numeric(15, 5))
    included_per_month: Mapped[int | None] = mapped_column(Integer, default=None)


class Proposal(Base):
    """A term's cost and rates as proposed (by AI, or someone who can't see costs), until a
    person who can see costs applies or rejects it."""

    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    term_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.terms.id"))
    data: Mapped[str] = mapped_column(UnicodeText)  # JSON {cost, cost_period, rates}
    note: Mapped[str] = mapped_column(Unicode(1000), default="")
    status: Mapped[str] = mapped_column(Unicode(20), default="pending")
    proposed_by: Mapped[str] = mapped_column(Unicode(200))
    proposed_via_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    proposed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    decided_by: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
