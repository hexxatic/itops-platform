"""Core file storage (ADR 0003): bytes on disk, metadata in ``itops.files``.

Files are content-addressed by SHA-256, so the same screenshot pasted twice is stored
once. Only an allowlist of types is accepted. SVG is deliberately excluded: it can carry
script, which is exactly what the content sanitizer exists to keep out.

Every file records the permission needed to read it (the owning module's, e.g.
``docs.read``), which the download route checks on every request.

**Attachments** (PDFs, Office files, Visio, Outlook messages, ...) are accepted by file
extension, not by the type the browser claims (often wrong or generic for Visio and
Outlook files), stored under their canonical type, and always served as downloads:
never rendered inside IT-Ops pages.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from itops.core import auth, extract

IMAGE_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


# extension -> canonical type. Served as downloads only (Content-Disposition: attachment).
ATTACHMENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".vsd": "application/vnd.visio",
    ".vsdx": "application/vnd.ms-visio.drawing.main+xml",
    ".drawio": "application/vnd.jgraph.mxfile",  # draw.io diagram source (XML)
    ".msg": "application/vnd.ms-outlook",
    ".eml": "message/rfc822",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".log": "text/plain",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def attachment_type(filename: str) -> tuple[str, str]:
    """(canonical type, extension) for an attachment, or FileRejected."""
    ext = Path(filename).suffix.lower()
    if ext not in ATTACHMENT_TYPES:
        allowed = ", ".join(sorted(e.lstrip(".") for e in ATTACHMENT_TYPES))
        raise FileRejected(
            f"“{Path(filename).name}”: that file type isn't allowed. Allowed: {allowed}."
        )
    return ATTACHMENT_TYPES[ext], ext


def is_image(mime_type: str) -> bool:
    return mime_type in IMAGE_TYPES


class FileRejected(ValueError):
    pass


@dataclass(frozen=True)
class StoredFile:
    id: str
    url: str
    filename: str
    mime_type: str
    size_bytes: int


@dataclass(frozen=True)
class FileRow:
    id: str
    module: str
    read_permission: str
    storage_path: str
    mime_type: str
    filename: str
    entity_type: str | None = None  # what it belongs to, once claimed
    entity_id: str | None = None


VisibleEntity = Callable[..., Awaitable[bool]]
SharedPermission = Callable[..., Awaitable[str | None]]
READER_VERSION = 1  # bump when core/extract.py reads something better: cached text is re-read


async def holds_permission(
    row: FileRow, principal: auth.Principal | None, shared: SharedPermission | None
) -> bool:
    """Whether ``principal`` holds the file's read permission, or the one more permission the
    owning module's ``shared_file_permission`` service opens its entity's files to (Docs:
    everyone, for a how-to published for end users; ADR 0027)."""
    if principal is None:
        return False
    if principal.has(row.read_permission):
        return True
    if shared is None or not (row.entity_type and row.entity_id):
        return False
    extra = await shared(entity_type=row.entity_type, entity_id=row.entity_id)
    return bool(extra) and principal.has(str(extra))


async def may_read(
    row: FileRow,
    principal: auth.Principal | None,
    visible: VisibleEntity | None,
    shared: SharedPermission | None = None,
) -> bool:
    """Whether ``principal`` may read the file: its read permission (or the one its entity is
    shared with), and, if it belongs to an entity, that the owning module's
    ``visible_entity`` service lets them see it (ADR 0012). Downloads (``/files``) and
    copies (``module.files.copy``) both ask this."""
    if not await holds_permission(row, principal, shared):
        return False
    if row.entity_type and row.entity_id and visible is not None:
        return bool(await visible(entity_type=row.entity_type, entity_id=row.entity_id))
    return True


class FileStore:
    def __init__(
        self, root: str | Path, *, max_bytes: int, max_attachment_bytes: int | None = None
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes  # images (screenshots)
        self.max_attachment_bytes = max_attachment_bytes or max_bytes

    def _write(self, data: bytes, ext: str) -> tuple[str, str]:
        digest = hashlib.sha256(data).hexdigest()
        rel = Path(digest[:2]) / f"{digest}{ext}"  # shard: no giant directory
        dest = self.root / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)  # atomic: never a half-written file under the real name
        return digest, rel.as_posix()

    def resolve(self, storage_path: str) -> Path:
        """Absolute path, guarded against traversal out of the files root."""
        root = self.root.resolve()
        full = (root / storage_path).resolve()
        if not full.is_relative_to(root):
            raise FileRejected("file path escapes the storage root")
        return full

    async def save(
        self,
        session: AsyncSession,
        *,
        data: bytes,
        mime_type: str,
        filename: str,
        module: str,
        read_permission: str,
        created_by: str,
        attachment: bool = False,
    ) -> StoredFile:
        """Images (``attachment=False``) are checked by type; attachments by extension and
        get the larger size limit."""
        if attachment:
            mime_type, ext = attachment_type(filename)
            limit = self.max_attachment_bytes
        else:
            if mime_type not in IMAGE_TYPES:
                raise FileRejected(f"file type {mime_type!r} is not allowed")
            ext, limit = IMAGE_TYPES[mime_type], self.max_bytes
        if not data:
            raise FileRejected(f"“{Path(filename).name}” is empty")
        if len(data) > limit:
            raise FileRejected(
                f"“{Path(filename).name}” is larger than {limit // (1024 * 1024)} MB"
            )
        digest, rel = self._write(data, ext)
        file_id = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO itops.files (id, module, read_permission, sha256, storage_path,"
                " mime_type, size_bytes, filename, created_by)"
                " VALUES (:id, :module, :perm, :sha, :path, :mime, :size, :name, :by)"
            ),
            {
                "id": file_id,
                "module": module,
                "perm": read_permission,
                "sha": digest,
                "path": rel,
                "mime": mime_type,
                "size": len(data),
                "name": Path(filename).name[:260] or "file",
                "by": created_by,
            },
        )
        return StoredFile(file_id, f"/files/{file_id}", filename, mime_type, len(data))

    async def claim(
        self,
        session: AsyncSession,
        *,
        module: str,
        ids: list[str],
        entity_type: str,
        entity_id: str,
    ) -> int:
        """Attach unclaimed files (uploaded before their entity was saved) to an entity."""
        claimed = 0
        for fid in ids:
            res = await session.execute(
                text(
                    "UPDATE itops.files SET entity_type = :t, entity_id = :e"
                    " WHERE id = :id AND module = :m AND entity_type IS NULL"
                ),
                {"t": entity_type, "e": entity_id, "id": fid, "m": module},
            )
            rowcount: int = getattr(res, "rowcount", 0) or 0
            claimed += rowcount
        return claimed

    async def copy(
        self,
        session: AsyncSession,
        row: FileRow,
        *,
        module: str,
        read_permission: str,
        created_by: str,
    ) -> StoredFile:
        """A copy of ``row`` owned by ``module``, unclaimed (claim it like an upload). The bytes
        aren't duplicated: storage is by content, so the copy points at the same file."""
        file_id = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO itops.files (id, module, read_permission, sha256, storage_path,"
                " mime_type, size_bytes, filename, created_by)"
                " SELECT :id, :module, :perm, sha256, storage_path, mime_type, size_bytes,"
                " filename, :by FROM itops.files WHERE id = :src"
            ),
            {"id": file_id, "module": module, "perm": read_permission, "by": created_by,
             "src": row.id},
        )  # fmt: skip
        size = (
            await session.execute(
                text("SELECT size_bytes FROM itops.files WHERE id = :id"), {"id": file_id}
            )
        ).scalar_one()
        return StoredFile(file_id, f"/files/{file_id}", row.filename, row.mime_type, int(size))

    async def text(self, session: AsyncSession, row: FileRow) -> list[str]:
        """The file's text by page (core/extract.py), read once per file content and kept in
        ``itops.file_text``; ``[]`` when it has none (a scan, an image, an unreadable file).
        The caller commits (a first read stores the text)."""
        if not extract.readable(row.filename):
            return []
        sha = PurePosixPath(row.storage_path).stem  # stored as <sha[:2]>/<sha256><ext>
        cached = (
            await session.execute(
                text("SELECT pages, reader_version FROM itops.file_text WHERE sha256 = :s"),
                {"s": sha},
            )
        ).first()
        if cached is not None and int(cached[1]) == READER_VERSION:
            stored: list[str] = json.loads(cached[0])
            return stored
        path = self.resolve(row.storage_path)
        data = await asyncio.to_thread(path.read_bytes) if path.exists() else b""
        pages = await asyncio.to_thread(extract.pages, data, row.filename)
        await session.execute(
            text(
                "MERGE itops.file_text AS t USING (SELECT :s AS sha256) AS src"
                " ON t.sha256 = src.sha256"
                " WHEN MATCHED THEN UPDATE SET pages = :p, reader_version = :v,"
                "   extracted_at = sysutcdatetime()"
                " WHEN NOT MATCHED THEN INSERT (sha256, pages, reader_version)"
                "   VALUES (:s, :p, :v);"
            ),
            {"s": sha, "p": json.dumps(pages), "v": READER_VERSION},
        )
        return pages

    async def get(self, session: AsyncSession, file_id: str) -> FileRow | None:
        try:
            uuid.UUID(file_id)
        except ValueError:
            return None
        row = (
            await session.execute(
                text(
                    "SELECT CAST(id AS nvarchar(36)), module, read_permission, storage_path,"
                    " mime_type, filename, entity_type, entity_id FROM itops.files WHERE id = :id"
                ),
                {"id": file_id},
            )
        ).first()
        if row is None:
            return None
        return FileRow(str(row[0]).lower(), row[1], row[2], row[3], row[4], row[5], row[6],
                       row[7])  # fmt: skip
