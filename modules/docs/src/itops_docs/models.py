"""ORM mapping of the docs schema (see migrations/0001_init.up.sql, the source of truth)."""

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from itops_docs.app import module

Base = module.Base


class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(Unicode(100))
    name: Mapped[str] = mapped_column(Unicode(200))
    description: Mapped[str] = mapped_column(Unicode(1000), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    collection_id: Mapped[int] = mapped_column(ForeignKey("docs.collections.id"))
    title: Mapped[str] = mapped_column(Unicode(300))
    slug: Mapped[str] = mapped_column(Unicode(300))
    status: Mapped[str] = mapped_column(Unicode(20), default="draft")
    type_key: Mapped[str | None] = mapped_column(Unicode(60), default=None)  # ADR 0008
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("docs.versions.id", use_alter=True), default=None
    )
    review_interval_days: Mapped[int] = mapped_column(Integer, default=365)
    next_review_at: Mapped[dt.date | None] = mapped_column(Date, default=None)
    created_by: Mapped[str] = mapped_column(Unicode(200))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())

    collection: Mapped[Collection] = relationship(lazy="joined")


class Version(Base):
    __tablename__ = "versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("docs.documents.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(UnicodeText)
    fields: Mapped[str | None] = mapped_column(UnicodeText, default=None)  # JSON values
    attachments: Mapped[str | None] = mapped_column(UnicodeText, default=None)  # JSON list
    change_note: Mapped[str] = mapped_column(Unicode(500), default="")
    author_user: Mapped[str] = mapped_column(Unicode(200))
    author_via_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    author_client: Mapped[str] = mapped_column(Unicode(50))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=lambda: utcnow())
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)


class DocumentField(Base):
    """A published field value, one row per value (queryable; rebuilt on publish)."""

    __tablename__ = "document_fields"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("docs.documents.id"), primary_key=True
    )
    field_key: Mapped[str] = mapped_column(Unicode(40), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(Unicode(20))
    value_text: Mapped[str | None] = mapped_column(Unicode(450), default=None)
    value_date: Mapped[dt.date | None] = mapped_column(Date, default=None)
    value_number: Mapped[float | None] = mapped_column(Float, default=None)


class DocumentTag(Base):
    __tablename__ = "document_tags"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("docs.documents.id"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(Unicode(100), primary_key=True)


def utcnow() -> dt.datetime:
    # Stored as naive UTC (datetime2 has no offset).
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)
