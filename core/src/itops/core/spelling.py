"""Typo tolerance for search: a vocabulary of the terms IT-Ops actually holds.

The vocabulary is built from the search index itself (titles, section labels, text), so it
knows technical names no dictionary has: hostnames, product names, error codes. A query
word that isn't in it is matched to the closest known terms (Damerau-Levenshtein, i.e.
swapped letters count as one edit): one edit for words of 4-7 characters, two for longer
ones. Candidates come from shared three-letter fragments, so a lookup is fast even with
hundreds of thousands of terms.

Guard rails: words of three characters or fewer and anything that's only digits and
punctuation (IPs, dates, versions) are never corrected; a correction may never change a
word's digits (a serial or hostname number stays as typed); a word that's a known term plus
an inflection ending (plurals, -ed, -ing) isn't treated as a typo, since full-text search
already handles those (a *missing* ending is: "librenm" -> "librenms").

The vocabulary only rewrites queries. Results still come from the index, filtered by what
the searcher may read, so nothing hidden leaks through a correction.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable

_TOKEN_RE = re.compile(r"[^\W_][\w.\-]*[^\W_]|[^\W_]", re.UNICODE)
_PARTS_RE = re.compile(r"[._\-]+")
_ONLY_NUMBERS_RE = re.compile(r"^[\d.\-:/]+$")
_DIGITS_RE = re.compile(r"\d")
_ENDINGS = ("s", "es", "ed", "ing", "er", "ly")
MIN_LENGTH = 4
MAX_TERM = 40
CANDIDATES = 300  # best fragment overlaps checked per lookup


def tokens(text: str) -> list[str]:
    """Lowercased terms of ``text``; hyphenated/dotted names also give their parts."""
    out: list[str] = []
    for t in _TOKEN_RE.findall(text.lower()):
        if len(t) > MAX_TERM:
            continue
        out.append(t)
        parts = [p for p in _PARTS_RE.split(t) if p]
        if len(parts) > 1:
            out += parts
    return out


def _grams(word: str) -> set[str]:
    padded = f"^{word}$"
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def distance(a: str, b: str, limit: int) -> int:
    """Optimal-string-alignment distance (a transposition is one edit); returns
    ``limit + 1`` as soon as it's known to exceed ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def max_edits(word: str) -> int:
    return 0 if len(word) < MIN_LENGTH else 1 if len(word) < 8 else 2


def correctable(word: str) -> bool:
    return len(word) >= MIN_LENGTH and not _ONLY_NUMBERS_RE.match(word)


def _inflection(word: str, term: str) -> bool:
    """``word`` is ``term`` plus an inflection ending ("spoolers" for "spooler"): full-text
    search already finds those. The other way round ("librenm" for "librenms") is a typo."""
    return word.startswith(term) and word[len(term) :] in _ENDINGS


class Vocabulary:
    """Known terms with how often they occur, and a fragment index for lookups."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self._index: defaultdict[str, set[str]] = defaultdict(set)

    def __len__(self) -> int:
        return len(self.counts)

    def __contains__(self, word: str) -> bool:
        return word in self.counts

    def add(self, text: str) -> None:
        for t in tokens(text):
            if t not in self.counts and len(t) >= MIN_LENGTH - 1:
                for g in _grams(t):
                    self._index[g].add(t)
            self.counts[t] += 1

    def add_all(self, texts: Iterable[str]) -> None:
        for t in texts:
            self.add(t)

    def correct(self, word: str) -> str | None:
        """The closest known term for an unknown ``word``, or None (known, guarded, or
        nothing close enough)."""
        word = word.lower()
        if word in self.counts or not correctable(word):
            return None
        limit = max_edits(word)
        overlap: Counter[str] = Counter()
        for g in _grams(word):
            for term in self._index.get(g, ()):
                overlap[term] += 1
        digits = _DIGITS_RE.findall(word)
        best: tuple[int, int, int, str] | None = None
        for term, _shared in overlap.most_common(CANDIDATES):
            if _DIGITS_RE.findall(term) != digits or _inflection(word, term):
                continue
            d = distance(word, term, limit)
            if d > limit:
                continue
            # Ties: people rarely get the first and last letters wrong; then the commoner term.
            ends = (term[0] != word[0]) + (term[-1] != word[-1])
            rank = (d, ends, -self.counts[term], term)
            if best is None or rank < best:
                best = rank
        return best[3] if best else None

    def correct_query(self, words: Iterable[str]) -> dict[str, str]:
        """Unknown words of a query -> their corrections (only the ones that have one).
        A hyphenated word with no correction of its own is tried part by part."""
        out: dict[str, str] = {}
        for w in words:
            if w in self.counts:
                continue
            fixed = self.correct(w)
            if fixed is None and _PARTS_RE.search(w):
                parts = [p for p in _PARTS_RE.split(w) if p]
                fixes = [self.correct(p) or p for p in parts]
                if fixes != parts:
                    seps = _PARTS_RE.findall(w)
                    fixed = fixes[0] + "".join(s + f for s, f in zip(seps, fixes[1:], strict=False))
            if fixed:
                out[w] = fixed
        return out
