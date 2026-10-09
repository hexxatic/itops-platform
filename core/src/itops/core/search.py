"""Core search (ADR 0003, ADR 0004): one index for every module, searched platform-wide
or per module, by keyword **and** by meaning.

Modules add **published** content as citable passages (``index``); archived content
stays in, flagged, and only shows when a search asks for it; deleted content is taken
out (``remove``). Drafts never go in.

Ranking blends two signals (reciprocal rank fusion):

* **Keyword**: SQL Server Full-Text where installed (stemming: "restarting" finds
  "restart"), plain word matching where not (the CI container).
* **Meaning**: each passage is embedded with the local model (AI tier 2) and stored in
  a native ``VECTOR`` column; a query is embedded and compared by cosine distance.
  Meaning-only matches must be close enough (``MEANING_MAX_DISTANCE``) so nonsense
  queries don't return random documents. Without the model, search is keyword-only.

Results are filtered to what the searcher may read and collapsed to the best passage per
entity.

**Typos** (``spelling``): query words IT-Ops has never seen are matched to the closest
terms in its own vocabulary (built from this index, so technical names count). The
corrected query is searched too and fused in; hits only found that way carry
``corrected`` (the query that found them) so pages can say "also showing results for".
``fuzzy=False`` searches exactly what was typed.
"""

from __future__ import annotations

import asyncio
import html as htmllib
import json
import logging
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from itops.core import auth
from itops.core.ai import AIService, AIUnavailable
from itops.core.content import Passage
from itops.core.spelling import Vocabulary

log = logging.getLogger("itops.search")

MAX_QUERY = 200
CHUNK_CHARS = 900  # longer passages are split into overlapping windows
CHUNK_OVERLAP = 150
RRF_K = 60  # standard reciprocal-rank-fusion constant
MEANING_MAX_DISTANCE = 0.35  # meaning-only cut-off; calibrated on real data (ADR 0004)
VOCABULARY_REFRESH = 900.0  # seconds; picks up terms other worker processes indexed
_WORD_RE = re.compile(r"[\w][\w.\-]*", re.UNICODE)

Match = Literal["keyword", "meaning", "both"]


@dataclass(frozen=True)
class Hit:
    module: str
    kind: str
    entity_id: str
    title: str
    url: str  # includes #anchor when the passage has one
    label: str  # "Failover > Step 3"
    snippet: str  # HTML: escaped text with the query terms in <mark>
    score: float
    archived: bool = False
    match: Match = "keyword"  # how it was found
    corrected: str = ""  # found only by the typo-corrected query (this one)

    def as_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "kind": self.kind,
            "id": self.entity_id,
            "title": self.title,
            "url": self.url,
            "where": self.label,
            # Plain text for AI tools and the API: no <mark>, no HTML escapes.
            "snippet": htmllib.unescape(re.sub(r"</?mark>", "", self.snippet)),
            "archived": self.archived,
            "match": self.match,
            **({"corrected_query": self.corrected} if self.corrected else {}),
        }


def terms(query: str) -> list[str]:
    """The words of a query (for fallback matching and highlighting)."""
    return [t for t in _WORD_RE.findall(query.lower()) if len(t) > 1][:8]


def snippet(passage: str, words: Sequence[str], width: int = 110) -> str:
    """A short excerpt around the first matching word, terms wrapped in <mark>."""
    lower = passage.lower()
    hits = [i for i in (lower.find(w) for w in words) if i >= 0]
    start = max(0, min(hits) - width // 2) if hits else 0
    end = min(len(passage), start + width * 2)
    # Cut on word boundaries, not mid-word.
    if start > 0 and (space := passage.find(" ", start, start + 25)) != -1:
        start = space + 1
    if end < len(passage) and (space := passage.rfind(" ", end - 25, end)) != -1:
        end = space
    piece = passage[start:end]
    out = htmllib.escape(piece)
    for w in sorted(set(words), key=len, reverse=True):
        out = re.sub(
            f"({re.escape(htmllib.escape(w))})",
            r"<mark>\1</mark>",
            out,
            flags=re.IGNORECASE,
        )
    return ("…" if start > 0 else "") + out + ("…" if end < len(passage) else "")


def chunk(passage: Passage) -> list[Passage]:
    """Split a long passage into overlapping windows on word boundaries (embedding
    models only read the first ~512 tokens; snippets stay focused too)."""
    body = passage.text
    if len(body) <= CHUNK_CHARS:
        return [passage]
    out: list[Passage] = []
    start = 0
    while start < len(body):
        end = min(len(body), start + CHUNK_CHARS)
        if end < len(body) and (space := body.rfind(" ", start + CHUNK_CHARS // 2, end)) != -1:
            end = space
        out.append(passage.model_copy(update={"text": body[start:end].strip()}))
        if end >= len(body):
            break
        next_start = end - CHUNK_OVERLAP
        if (space := body.find(" ", next_start, end)) != -1:
            next_start = space + 1
        start = max(next_start, start + 1)
    return [p for p in out if p.text]


def _like(term: str) -> str:
    return "%" + re.sub(r"([\[%_])", r"[\1]", term) + "%"


def _vector(values: Sequence[float]) -> str:
    return json.dumps([round(float(v), 6) for v in values])


def correct_query(q: str, fixes: dict[str, str]) -> str:
    """``q`` with each misspelt word replaced by its correction (whole words only)."""
    for wrong, right in fixes.items():
        q = re.sub(rf"(?<![\w.\-]){re.escape(wrong)}(?![\w\-])", right, q, flags=re.IGNORECASE)
    return q


class SearchIndex:
    def __init__(self, ai: AIService | None = None) -> None:
        self._ai = ai
        self._fulltext: bool | None = None
        self._vectors: bool | None = None
        self._vocabulary: Vocabulary | None = None
        self._vocabulary_at = 0.0
        self._vocabulary_lock = asyncio.Lock()

    async def vocabulary(self, s: AsyncSession) -> Vocabulary:
        """Every term in the index (archived and restricted content included: the
        vocabulary only rewrites queries; results are still filtered). Loaded on first
        use, grown as content is indexed, reloaded now and then."""
        if self._vocabulary is None or time.monotonic() - self._vocabulary_at > VOCABULARY_REFRESH:
            async with self._vocabulary_lock:
                stale = time.monotonic() - self._vocabulary_at > VOCABULARY_REFRESH
                if self._vocabulary is None or stale:
                    rows = await s.execute(
                        text("SELECT title, label, text FROM itops.search_passages")
                    )
                    vocabulary = Vocabulary()
                    vocabulary.add_all(f"{r[0]} {r[1]} {r[2]}" for r in rows)
                    self._vocabulary, self._vocabulary_at = vocabulary, time.monotonic()
        return self._vocabulary

    async def has_fulltext(self, s: AsyncSession) -> bool:
        if self._fulltext is None:
            row = await s.execute(
                text(
                    "SELECT COUNT(*) FROM sys.fulltext_indexes"
                    " WHERE object_id = OBJECT_ID(N'itops.search_passages')"
                )
            )
            self._fulltext = bool(row.scalar())
        return self._fulltext

    async def has_vectors(self, s: AsyncSession) -> bool:
        """Semantic search is on: a model is configured and the index has a column that
        matches its size."""
        if self._vectors is None:
            dim = self._ai.embedding_dim if self._ai else None
            if dim is None:
                self._vectors = False
            else:
                row = await s.execute(
                    text(
                        "SELECT c.vector_dimensions FROM sys.columns c"
                        " WHERE c.object_id = OBJECT_ID(N'itops.search_passages')"
                        " AND c.name = N'embedding'"
                    )
                )
                have = row.scalar()
                self._vectors = have == dim
                if have is not None and have != dim:
                    log.warning(
                        "search index vectors are %s-d but the model makes %s-d; "
                        "semantic search is off until the index matches the model",
                        have,
                        dim,
                    )
        return bool(self._vectors)

    @property
    def _dim(self) -> int:
        assert self._ai is not None and self._ai.embedding_dim is not None
        return self._ai.embedding_dim

    # ---- writing ---------------------------------------------------------------------

    async def remove(self, s: AsyncSession, *, module: str, kind: str, entity_id: str) -> None:
        await s.execute(
            text(
                "DELETE FROM itops.search_passages"
                " WHERE module = :m AND kind = :k AND entity_id = :e"
            ),
            {"m": module, "k": kind, "e": entity_id},
        )

    async def replace(
        self,
        s: AsyncSession,
        *,
        module: str,
        kind: str,
        entity_id: str,
        title: str,
        url: str,
        read_permission: str,
        passages: Iterable[Passage],
        archived: bool = False,
    ) -> int:
        """Replace everything indexed for one entity. Returns the passage count."""
        await self.remove(s, module=module, kind=kind, entity_id=entity_id)
        rows = [c for p in passages if p.text.strip() for c in chunk(p)] or [
            Passage(label="", anchor=None, text=title)  # e.g. a doc with only headings
        ]
        vectors: list[list[float]] | None = None
        if await self.has_vectors(s):
            assert self._ai is not None
            try:
                # The title and section label give short passages their context.
                vectors = await self._ai.embed(
                    [
                        f"{title}. {p.label}. {p.text}" if p.label else f"{title}. {p.text}"
                        for p in rows
                    ]
                )
            except AIUnavailable as exc:
                log.warning("indexing %s/%s without meaning vectors: %s", module, entity_id, exc)
        for i, p in enumerate(rows):
            params: dict[str, Any] = {
                "m": module,
                "k": kind,
                "e": entity_id,
                "t": title[:300],
                "u": url[:500],
                "l": p.label[:300],
                "a": (p.anchor or None),
                "x": p.text,
                "p": read_permission,
                "ar": archived,
            }
            cols = "module, kind, entity_id, title, url, label, anchor, text, "
            cols += "read_permission, archived"
            vals = ":m, :k, :e, :t, :u, :l, :a, :x, :p, :ar"
            if vectors is not None:
                assert self._ai is not None
                params["v"] = _vector(vectors[i])
                params["em"] = self._ai.embedding_model
                cols += ", embedding, embedding_model"
                vals += f", CAST(CAST(:v AS nvarchar(max)) AS VECTOR({self._dim})), :em"
            await s.execute(
                text(f"INSERT INTO itops.search_passages ({cols}) VALUES ({vals})"), params
            )
        if self._vocabulary is not None:
            self._vocabulary.add_all(f"{title} {p.label} {p.text}" for p in rows)
        return len(rows)

    # ---- reading ---------------------------------------------------------------------

    async def query(
        self,
        s: AsyncSession,
        q: str,
        *,
        principal: auth.Principal | None,
        module: str | None = None,
        limit: int = 20,
        include_archived: bool = False,
        fuzzy: bool = True,
    ) -> list[Hit]:
        """``fuzzy``: also search the typo-corrected query (see the module docstring)."""
        q = " ".join(q.split())[:MAX_QUERY]
        words = terms(q)
        if principal is None or not words:
            return []
        params: dict[str, Any] = {"m": module, "n": max(1, min(limit, 50)) * 5}
        filters = "(:m IS NULL OR p.module = :m)"
        if auth.ALL not in principal.permissions:
            held = sorted(principal.permissions)
            if not held:
                return []
            names = [f"p{i}" for i in range(len(held))]
            params.update(dict(zip(names, held, strict=True)))
            filters += " AND p.read_permission IN (" + ", ".join(":" + n for n in names) + ")"
        if not include_archived:
            filters += " AND p.archived = 0"
        cols = "p.module, p.kind, p.entity_id, p.title, p.url, p.label, p.anchor, p.text"
        cols += ", p.archived"

        lists = [
            ("keyword", await self._keyword(s, q, words, cols, filters, dict(params))),
            ("meaning", await self._meaning(s, q, cols, filters, dict(params))),
        ]
        corrected = ""
        if fuzzy:
            fixes = (await self.vocabulary(s)).correct_query(w.strip(".-") for w in words)
            if fixes:
                corrected = correct_query(q, fixes)
                fixed_words = terms(corrected)
                lists += [
                    ("keyword", await self._keyword(s, corrected, fixed_words, cols, filters,
                                                    dict(params))),
                    ("meaning", await self._meaning(s, corrected, cols, filters, dict(params))),
                ]  # fmt: skip
                words = list(dict.fromkeys(words + fixed_words))  # highlight both
        return self._fuse(lists, words, limit, corrected)

    async def _keyword(
        self,
        s: AsyncSession,
        q: str,
        words: list[str],
        cols: str,
        filters: str,
        params: dict[str, Any],
    ) -> list[Any]:
        if await self.has_fulltext(s):
            params["q"] = q
            sql = (
                f"SELECT TOP (:n) {cols}"
                " FROM FREETEXTTABLE(itops.search_passages, (title, label, text), :q) k"
                " JOIN itops.search_passages p ON p.id = k.[KEY]"
                f" WHERE {filters} ORDER BY k.RANK DESC, p.updated_at DESC"
            )
        else:
            likes: list[str] = []
            for i, w in enumerate(words):
                params[f"w{i}"] = _like(w)
                likes.append(f"(p.title + N' ' + p.label + N' ' + p.text) LIKE :w{i}")
            title_hits = " + ".join(
                f"CASE WHEN p.title LIKE :w{i} THEN 2 ELSE 0 END" for i in range(len(words))
            )
            sql = (
                f"SELECT TOP (:n) {cols} FROM itops.search_passages p"
                f" WHERE {filters} AND {' AND '.join(likes)}"
                f" ORDER BY ({title_hits}) DESC, p.updated_at DESC"
            )
        return list((await s.execute(text(sql), params)).all())

    async def _meaning(
        self, s: AsyncSession, q: str, cols: str, filters: str, params: dict[str, Any]
    ) -> list[Any]:
        if not await self.has_vectors(s):
            return []
        assert self._ai is not None
        try:
            params["qv"] = _vector(await self._ai.embed_query(q))
        except AIUnavailable as exc:
            log.warning("keyword-only search: %s", exc)
            return []
        params["em"] = self._ai.embedding_model
        params["maxd"] = MEANING_MAX_DISTANCE
        # Long parameters arrive as ntext, which can't convert to VECTOR directly.
        qv = f"CAST(CAST(:qv AS nvarchar(max)) AS VECTOR({self._dim}))"
        dist = f"VECTOR_DISTANCE('cosine', p.embedding, {qv})"
        sql = (
            f"SELECT TOP (:n) {cols}, {dist} AS dist FROM itops.search_passages p"
            f" WHERE {filters} AND p.embedding IS NOT NULL AND p.embedding_model = :em"
            f" AND {dist} <= :maxd ORDER BY dist"
        )
        return list((await s.execute(text(sql), params)).all())

    @staticmethod
    def _fuse(
        lists: list[tuple[str, list[Any]]], words: list[str], limit: int, corrected: str = ""
    ) -> list[Hit]:
        """Reciprocal rank fusion per entity over ranked lists ("keyword"/"meaning"; the
        first two are for the query as typed, any after them for the corrected one),
        keeping each entity's best passage (the keyword one when there is one, so the
        snippet shows the matched words). A hit is ``corrected`` when only the corrected
        spelling matched its words."""
        score: dict[tuple[str, str, str], float] = {}
        passage: dict[tuple[str, str, str], Any] = {}
        found: dict[tuple[str, str, str], set[str]] = {}
        keyword_passage: dict[tuple[str, str, str], Any] = {}
        # Words matched as typed vs only by the corrected spelling (meaning matches don't
        # decide it: the embedding is a little typo-tolerant on its own).
        typed_words: set[tuple[str, str, str]] = set()
        fixed_words: set[tuple[str, str, str]] = set()
        for n, (source, rows) in enumerate(lists):
            rank = 0
            counted: set[tuple[str, str, str]] = set()
            for row in rows:
                key = (row[0], row[1], row[2])
                if key in counted:
                    continue  # only an entity's best passage per list counts
                counted.add(key)
                rank += 1
                score[key] = score.get(key, 0.0) + 1.0 / (RRF_K + rank)
                found.setdefault(key, set()).add(source)
                if source == "keyword":
                    (typed_words if n < 2 else fixed_words).add(key)
                    keyword_passage.setdefault(key, row)
                passage.setdefault(key, row)
        passage.update(keyword_passage)  # a keyword passage shows the matched words
        ordered = sorted(score, key=lambda k: score[k], reverse=True)[:limit]
        hits: list[Hit] = []
        for key in ordered:
            mod, kind, eid, title, url, label, anchor, body, archived = passage[key][:9]
            how = found[key]
            hits.append(
                Hit(
                    module=mod,
                    kind=kind,
                    entity_id=eid,
                    title=title,
                    url=url + (f"#{anchor}" if anchor else ""),
                    label=label,
                    snippet=snippet(body, words),
                    score=score[key],
                    archived=bool(archived),
                    match="both"
                    if len(how) == 2
                    else ("keyword" if "keyword" in how else "meaning"),
                    corrected=corrected if key in fixed_words and key not in typed_words else "",
                )
            )
        return hits

    async def wait_until_current(self, s: AsyncSession, timeout: float = 15.0) -> None:
        """Full-Text indexes update asynchronously (~2-3 s). Tests and bulk rebuilds wait
        here until no changes are pending. No-op without Full-Text.

        (The catalog's PopulateStatus sits at 9, "change tracking", in steady state, so
        it's not a "busy" signal; pending changes are.)"""
        if not await self.has_fulltext(s):
            return
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            pending = (
                await s.execute(
                    text(
                        "SELECT CAST(OBJECTPROPERTYEX(OBJECT_ID(N'itops.search_passages'),"
                        " N'TableFulltextPendingChanges') AS int)"
                    )
                )
            ).scalar()
            if not pending:
                return
            if asyncio.get_running_loop().time() > deadline:
                return
            await asyncio.sleep(0.3)
