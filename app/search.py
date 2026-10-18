"""Search: query parsing, matching, scoring, sorting, and the tag list."""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from app import notes, text
from app.fsutil import DataDir
from app.notes import NoteRef
from app.text import Segment

MAX_QUERY_CHARS = 500
MAX_TERMS = 20
SNIPPET_RESULTS = 100
SORTS = ("relevance", "modified", "title")
ORDERS = ("asc", "desc")


class BadQuery(Exception):
    """The query or its sort options are invalid."""


@dataclass(frozen=True)
class Query:
    """A parsed query: folded substring terms and folded tag filters."""

    terms: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True if the query matches every note."""
        return not self.terms and not self.tags

    @property
    def title_text(self) -> str:
        """The folded query text without its tag terms, compared with whole titles."""
        return " ".join(self.terms)


@dataclass
class Result:
    """One matching note with its score and highlights."""

    ref: NoteRef
    tags: list[str]
    score: int = 0
    title_segments: list[Segment] = field(default_factory=list)
    snippets: list[list[Segment]] = field(default_factory=list)


def _split(query: str) -> list[tuple[str, bool]]:
    """Split on whitespace; "double-quoted text" is one term; an unclosed quote runs to the end."""
    terms: list[tuple[str, bool]] = []
    word: list[str] = []

    def flush() -> None:
        if word:
            terms.append(("".join(word), False))
            word.clear()

    position = 0
    while position < len(query):
        char = query[position]
        if char == '"':
            flush()
            end = query.find('"', position + 1)
            end = len(query) if end == -1 else end
            if end > position + 1:
                terms.append((query[position + 1 : end], True))
            position = end + 1
            continue
        if char.isspace():
            flush()
        else:
            word.append(char)
        position += 1
    flush()
    return terms


def parse_query(query: str) -> Query:
    """Parse the query syntax; raise BadQuery if it is too long or has too many terms."""
    if len(query) > MAX_QUERY_CHARS:
        raise BadQuery(f"Queries can be at most {MAX_QUERY_CHARS} characters long.")
    raw_terms = _split(query)
    if len(raw_terms) > MAX_TERMS:
        raise BadQuery(f"Queries can have at most {MAX_TERMS} terms.")
    if query.strip() == "*":
        return Query()
    terms: list[str] = []
    tags: list[str] = []
    for raw, quoted in raw_terms:
        if raw.startswith("#") and not quoted:
            tags.append(text.fold(raw[1:]))
        elif folded := text.fold(raw):
            terms.append(folded)
    return Query(terms=tuple(terms), tags=tuple(tags))


def _resolve_sort(query: Query, sort: str | None, order: str | None) -> tuple[str, str]:
    sort = sort or ("modified" if query.is_empty else "relevance")
    if sort not in SORTS:
        raise BadQuery("sort must be relevance, modified or title.")
    order = order or ("asc" if sort == "title" else "desc")
    if order not in ORDERS:
        raise BadQuery("order must be asc or desc.")
    return sort, order


def _score(query: Query, ref: NoteRef, folded_content: str) -> int | None:
    """The note's score, or None if any term does not match."""
    folded_title = text.fold(ref.title)
    score = 5 * len(query.tags)
    for term in query.terms:
        in_title = term in folded_title
        count = folded_content.count(term)
        if not in_title and not count:
            return None
        score += (10 if in_title else 0) + min(count, 10)
    if query.terms and folded_title == query.title_text:
        score += 20
    return score


def _sort_key(sort: str) -> Callable[[Result], tuple]:
    if sort == "relevance":
        return lambda r: (r.score, r.ref.mtime, r.ref.id)
    if sort == "modified":
        return lambda r: (r.ref.mtime, r.ref.id)
    return lambda r: (text.fold(r.ref.title), r.ref.title, r.ref.id)


def search(
    data_dir: DataDir, query_text: str, sort: str | None = None, order: str | None = None
) -> list[Result]:
    """All notes matching the query, sorted, with highlights for the first SNIPPET_RESULTS."""
    query = parse_query(query_text)
    sort, order = _resolve_sort(query, sort, order)
    results: list[Result] = []
    contents: dict[str, tuple[str, str]] = {}
    for ref in notes.list_notes(data_dir):
        content = notes.read_content(ref)
        tags = text.extract_tags(content) if content else []
        if query.tags:
            folded_tags = {text.fold(tag) for tag in tags}
            if not all(tag in folded_tags for tag in query.tags):
                continue
        folded_content = ""
        if query.terms and content:
            folded_content = text.fold(content)
            contents[ref.id] = (content, folded_content)
        score = _score(query, ref, folded_content)
        if score is not None:
            results.append(Result(ref=ref, tags=tags, score=score))
    results.sort(key=_sort_key(sort), reverse=order == "desc")
    for rank, result in enumerate(results):
        result.title_segments = text.highlight(result.ref.title, query.terms)
        if rank < SNIPPET_RESULTS and result.ref.id in contents:
            result.snippets = text.snippets(*contents[result.ref.id], query.terms)
    return results


def list_tags(data_dir: DataDir) -> list[tuple[str, int]]:
    """Every tag with the number of notes that use it, sorted by tag."""
    display: dict[str, str] = {}
    counts: Counter[str] = Counter()
    # Newest notes first, so each tag is shown the way it was first written in the newest note.
    refs = sorted(notes.list_notes(data_dir), key=lambda r: (r.mtime, r.id), reverse=True)
    for ref in refs:
        content = notes.read_content(ref)
        for tag in text.extract_tags(content) if content else []:
            identity = text.tag_identity(tag)
            display.setdefault(identity, tag)
            counts[identity] += 1
    return sorted(
        ((display[identity], count) for identity, count in counts.items()),
        key=lambda item: (text.fold(item[0]), item[0]),
    )
