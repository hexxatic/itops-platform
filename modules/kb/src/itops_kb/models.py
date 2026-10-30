"""ORM mapping of the kb schema (see migrations/0001_init.up.sql, the source of truth)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Integer, Unicode
from sqlalchemy.orm import Mapped, mapped_column

from itops_kb.app import module

Base = module.Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Indexed(Base):
    """Which version of a how-to its search entries were built from."""

    __tablename__ = "indexed"

    slug: Mapped[str] = mapped_column(Unicode(300), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Unicode(300))
    collection_slug: Mapped[str] = mapped_column(Unicode(100))
    area: Mapped[str | None] = mapped_column(Unicode(200), default=None)  # ADR 0028
    indexed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_slug: Mapped[str] = mapped_column(Unicode(300))
    helpful: Mapped[bool] = mapped_column(Boolean)
    comment: Mapped[str | None] = mapped_column(Unicode(1000), default=None)
    upn: Mapped[str] = mapped_column(Unicode(256))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
