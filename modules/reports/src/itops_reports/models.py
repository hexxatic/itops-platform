"""ORM mapping of the reports schema (see migrations/0001_init.up.sql, the source of truth)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Unicode,
    UnicodeText,
)
from sqlalchemy.orm import Mapped, mapped_column

from itops_reports.app import module

Base = module.Base


def utcnow() -> dt.datetime:
    # Stored as naive UTC (datetime2 has no offset).
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column("key", Unicode(60))
    title: Mapped[str] = mapped_column(Unicode(200))
    description: Mapped[str] = mapped_column(Unicode(1000), default="")
    type_key: Mapped[str] = mapped_column(Unicode(40))
    mode: Mapped[str] = mapped_column(Unicode(10))  # synced | manual
    time_field: Mapped[str | None] = mapped_column(Unicode(40), default=None)
    retention_days: Mapped[int | None] = mapped_column(Integer, default=None)
    source: Mapped[str] = mapped_column(Unicode(200), default="")
    created_by: Mapped[str] = mapped_column(Unicode(200))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    updated_by: Mapped[str] = mapped_column(Unicode(200))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())


class Row(Base):
    __tablename__ = "rows"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("reports.datasets.id"))
    source_key: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    fingerprint: Mapped[str | None] = mapped_column(String(64), default=None)
    row_time: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    values: Mapped[str] = mapped_column("values", UnicodeText)  # JSON
    created_by: Mapped[str] = mapped_column(Unicode(200))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    updated_by: Mapped[str] = mapped_column(Unicode(200))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())


class Definition(Base):
    __tablename__ = "definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column("key", Unicode(60))
    title: Mapped[str] = mapped_column(Unicode(200))
    description: Mapped[str] = mapped_column(Unicode(1000), default="")
    dataset_id: Mapped[int] = mapped_column(ForeignKey("reports.datasets.id"))
    config: Mapped[str | None] = mapped_column(UnicodeText, default=None)  # JSON, published
    draft_config: Mapped[str | None] = mapped_column(UnicodeText, default=None)  # JSON
    draft_by: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    draft_via_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(Unicode(200))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    updated_by: Mapped[str] = mapped_column(Unicode(200))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    published_by: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)


class Proposal(Base):
    """Rows AI proposed for a typed-in dataset; added only when a person approves them."""

    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("reports.datasets.id"))
    # Set while the rows wait for a proposed dataset (dataset_id is None until it's created).
    dataset_proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("reports.dataset_proposals.id"), default=None
    )
    rows: Mapped[str] = mapped_column("rows", UnicodeText)  # JSON: a list of row values
    row_count: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Unicode(1000), default="")
    status: Mapped[str] = mapped_column(Unicode(20), default="pending")
    proposed_by: Mapped[str] = mapped_column(Unicode(200))
    proposed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    decided_by: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)


class DatasetProposal(Base):
    """A typed-in dataset AI proposed; created only when a person with reports.publish
    approves it."""

    __tablename__ = "dataset_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column("key", Unicode(60))
    spec: Mapped[str] = mapped_column(UnicodeText)  # JSON: DatasetInput fields
    note: Mapped[str] = mapped_column(Unicode(1000), default="")
    status: Mapped[str] = mapped_column(Unicode(20), default="pending")
    proposed_by: Mapped[str] = mapped_column(Unicode(200))
    proposed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    decided_by: Mapped[str | None] = mapped_column(Unicode(200), default=None)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
