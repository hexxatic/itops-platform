"""Services for core's types pages: when an admin retires a document type, what happens to
the documents that use it (keep them, move them to another type, or archive them)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from itops.sdk import Principal
from itops_docs import service
from itops_docs.app import module
from itops_docs.models import Document
from itops_docs.service import DocsError


def _who(permission: str) -> Principal:
    who = module.current_principal()
    if who is None or not who.has(permission) or who.via_ai:
        raise DocsError(f"Only a person with {permission} can do this.")
    return who


@module.service("type_usage")
async def type_usage(type_key: str) -> int:
    """How many documents (archived ones included) have this type."""
    async with module.session() as s:
        query = select(func.count()).select_from(Document).where(Document.type_key == type_key)
        return int(await s.scalar(query) or 0)


@module.service("move_type")
async def move_type(type_key: str, to: str) -> dict[str, Any]:
    """Give every document of ``type_key`` the type ``to``. Their stored values are kept;
    fields both types share keep showing. Documents a connector syncs stay (the connector
    sets their type; moving them would break its next run). Returns {moved, kept}."""
    _who("docs.write")
    if to not in {t.key for t in await service.available_types()}:
        raise DocsError(f"{to!r} isn't an available document type (unknown or retired).")
    async with module.session() as s:
        found = (await s.execute(select(Document).where(Document.type_key == type_key))).scalars()
        moved = kept = 0
        for doc in list(found):
            if await module.refs.links("document", str(doc.id)):
                kept += 1
                continue
            await service.change_type(s, doc, to)
            moved += 1
        await s.commit()
    return {"moved": moved, "kept": kept}


@module.service("archive_type")
async def archive_type(type_key: str) -> dict[str, Any]:
    """Archive every document of ``type_key`` that isn't archived yet. Returns {archived}."""
    who = _who("docs.publish")
    async with module.session() as s:
        found = await s.execute(
            select(Document).where(Document.type_key == type_key, Document.status != "archived")
        )
        docs = list(found.scalars())
        for doc in docs:
            await service.archive(s, who, doc)
        await s.commit()
    return {"archived": len(docs)}
