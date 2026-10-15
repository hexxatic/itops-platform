"""ORM mapping of the news schema (see migrations/0001_init.up.sql, the source of truth)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, Unicode, UnicodeText, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from itops_news.app import module

Base = module.Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(Unicode(200))
    title: Mapped[str] = mapped_column(Unicode(300))
    category: Mapped[str] = mapped_column(Unicode(20))
    content: Mapped[str] = mapped_column(UnicodeText)
    status: Mapped[str] = mapped_column(Unicode(20), default="draft")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(Unicode(200))
    created_via_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(Unicode(200))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    published_by: Mapped[str | None] = mapped_column(Unicode(200), nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
