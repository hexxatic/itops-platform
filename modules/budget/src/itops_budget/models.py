"""ORM mapping of the budget schema (see migrations/0001_init.up.sql, the source of truth)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric, Unicode, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column

from itops_budget.app import module

Base = module.Base


def utcnow() -> dt.datetime:
    # Stored as naive UTC (datetime2 has no offset).
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Unicode(60), primary_key=True)
    value: Mapped[str] = mapped_column(UnicodeText)  # JSON
    updated_by: Mapped[str] = mapped_column(Unicode(200))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())


class Price(Base):
    __tablename__ = "prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item: Mapped[str] = mapped_column(Unicode(200))
    category: Mapped[str] = mapped_column(Unicode(20))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    source: Mapped[str] = mapped_column(Unicode(300), default="")
    quoted_on: Mapped[dt.date | None] = mapped_column(Date, default=None)
    updated_by: Mapped[str] = mapped_column(Unicode(200))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())


class Version(Base):
    __tablename__ = "versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Unicode(200))
    status: Mapped[str] = mapped_column(Unicode(20), default="saved")
    total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), default=None)
    snapshot: Mapped[str] = mapped_column(UnicodeText)  # JSON {assumptions, prices, forecast}
    created_by: Mapped[str] = mapped_column(Unicode(200))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    adopted_by: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    adopted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)


class Exclusion(Base):
    __tablename__ = "exclusions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[str] = mapped_column(Unicode(100))
    name: Mapped[str] = mapped_column(Unicode(200))
    reason: Mapped[str] = mapped_column(Unicode(500))
    excluded_by: Mapped[str] = mapped_column(Unicode(200))
    excluded_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())


class Skip(Base):
    __tablename__ = "skips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[str] = mapped_column(Unicode(100))
    name: Mapped[str] = mapped_column(Unicode(200))
    year: Mapped[int] = mapped_column(Integer)  # the budget year it was skipped from
    reason: Mapped[str] = mapped_column(Unicode(500))
    due: Mapped[str] = mapped_column(Unicode(500))  # why it was on the list then
    skipped_by: Mapped[str] = mapped_column(Unicode(200))
    skipped_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(
        Unicode(20)
    )  # price_add|price_change|price_remove|assumptions
    price_id: Mapped[int | None] = mapped_column(Integer, default=None)
    data: Mapped[str] = mapped_column(UnicodeText)  # JSON: the proposed values
    note: Mapped[str] = mapped_column(Unicode(1000), default="")
    status: Mapped[str] = mapped_column(Unicode(20), default="pending")
    proposed_by: Mapped[str] = mapped_column(Unicode(200))
    proposed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    decided_by: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
