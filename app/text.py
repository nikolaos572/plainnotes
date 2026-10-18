"""Text folding for search, tag extraction, and highlighted snippets."""

import re
import sys
import unicodedata
from dataclasses import dataclass


def _bmp_mark_class() -> str:
    """A regex character class of every BMP code point in Unicode category Mn."""
    ranges: list[tuple[int, int]] = []
    for code in range(0x10000):
        if unicodedata.category(chr(code)) != "Mn":
            continue
        if ranges and ranges[-1][1] == code - 1:
            ranges[-1] = (ranges[-1][0], code)
        else:
            ranges.append((code, code))
    return "".join(f"\\u{a:04x}-\\u{b:04x}" if a != b else f"\\u{a:04x}" for a, b in ranges)


# One class holding every BMP mark plus the whole astral range. Listing BMP marks only lets re
# compile them to a fast bitmap; the rare astral matches are checked one by one.
_MARK_CANDIDATE_RE = re.compile(f"[{_bmp_mark_class()}\\U00010000-\\U{sys.maxunicode:08x}]")
_MAX_DISTINCT_MARKS = 16

# Starts with a literal "#" so re can jump between candidates instead of testing every position.
_TAG_RE = re.compile(r"#(?:(?<=^#)|(?<=\s#))([\w-]++)(?=[\s.,;:!?)\]}]|$)", re.MULTILINE)
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$", re.MULTILINE)
_BACKTICKS_RE = re.compile(r"`+")

SNIPPET_WIDTH = 160
_BOUNDARY_REACH = 20
ELLIPSIS = "…"


def _is_mark(char: str) -> bool:
    return char < "\U00010000" or unicodedata.category(char) == "Mn"


def _drop_mark(match: re.Match[str]) -> str:
    return "" if _is_mark(match.group()) else match.group()


def _strip_marks(text: str) -> str:
    # Text usually holds only a few distinct marks, so deleting each one with str.replace is
    # much faster than re.sub, whose cost grows with the number of matches.
    distinct = 0
    match = _MARK_CANDIDATE_RE.search(text)
    while match:
        char = match.group()
        if not _is_mark(char):
            match = _MARK_CANDIDATE_RE.search(text, match.end())
            continue
        distinct += 1
        if distinct > _MAX_DISTINCT_MARKS:
            return _MARK_CANDIDATE_RE.sub(_drop_mark, text)
        text = text.replace(char, "")
        match = _MARK_CANDIDATE_RE.search(text, match.start())
    return text


def fold(text: str) -> str:
    """NFD-normalize, remove combining marks (Mn), then casefold."""
    if text.isascii():
        return text.lower()
    return _strip_marks(unicodedata.normalize("NFD", text)).casefold()


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """Fold text and return, for each folded character, the index of its source character."""
    if text.isascii():
        return text.lower(), list(range(len(text)))
    pieces: list[str] = []
    origin: list[int] = []
    folded_chars: dict[str, str] = {}
    for index, char in enumerate(text):
        folded = folded_chars.get(char)
        if folded is None:
            folded = folded_chars[char] = fold(char)
        if folded:
            pieces.append(folded)
            origin.extend([index] * len(folded))
    return "".join(pieces), origin


def tag_identity(body: str) -> str:
    """The identity used to group tags: NFC-normalized and case-folded."""
    return unicodedata.normalize("NFC", body).casefold()


def _without_fenced_code(text: str) -> str:
    if "```" not in text and "~~~" not in text:
        return text
    parts = []
    position = 0
    opening: str | None = None
    for match in _FENCE_RE.finditer(text):
        fence, rest = match.group(1), match.group(2)
        if opening is None:
            if fence[0] == "`" and "`" in rest:
                continue
            parts.append(text[position : match.start()])
            opening = fence
        elif fence[0] == opening[0] and len(fence) >= len(opening) and not rest.strip():
            opening = None
            position = match.end()
    if opening is None:
        parts.append(text[position:])
    return "\n".join(parts)


def _blank_code_spans(line: str) -> str:
    runs = list(_BACKTICKS_RE.finditer(line))
    next_same: list[int | None] = [None] * len(runs)
    last_seen: dict[int, int] = {}
    for i in range(len(runs) - 1, -1, -1):
        length = len(runs[i].group())
        next_same[i] = last_seen.get(length)
        last_seen[length] = i
    chars = list(line)
    i = 0
    while i < len(runs):
        j = next_same[i]
        if j is None:
            i += 1
            continue
        start, end = runs[i].start(), runs[j].end()
        chars[start:end] = " " * (end - start)
        i = j + 1
    return "".join(chars)


def _without_inline_code(text: str) -> str:
    if "`" not in text:
        return text
    parts = []
    position = 0
    tick = text.find("`")
    while tick != -1:
        line_start = text.rfind("\n", 0, tick) + 1
        line_end = text.find("\n", tick)
        if line_end == -1:
            line_end = len(text)
        parts.append(text[position:line_start])
        parts.append(_blank_code_spans(text[line_start:line_end]))
        position = line_end
        tick = text.find("`", line_end)
    parts.append(text[position:])
    return "".join(parts)


def extract_tags(text: str) -> list[str]:
    """Tags in text, as display forms (lowercase), unique by identity, in order of appearance."""
    if "#" not in text:
        return []
    prose = _without_inline_code(_without_fenced_code(text))
    seen: set[str] = set()
    tags = []
    for match in _TAG_RE.finditer(prose):
        body = match.group(1)
        if not any(char.isalpha() for char in body):
            continue
        identity = tag_identity(body)
        if identity not in seen:
            seen.add(identity)
            tags.append(body.lower())
    return tags


@dataclass(frozen=True)
class Segment:
    """A piece of plain text; mark=True means it should be highlighted."""

    text: str
    mark: bool


def _spans(
    folded: str, origin: list[int], length: int, terms: tuple[str, ...], stop: int | None = None
) -> list[tuple[int, int]]:
    """Original-text spans of every match of every term, merged; stop early at index stop."""
    found = []
    for term in terms:
        start = folded.find(term)
        while start != -1:
            if stop is not None and origin[start] >= stop:
                break
            end = start + len(term)
            # The end covers the whole last source character plus any marks that follow it.
            after = origin[end] if end < len(origin) else length
            found.append((origin[start], max(origin[end - 1] + 1, after)))
            start = folded.find(term, end)
    found.sort()
    merged: list[tuple[int, int]] = []
    for start, end in found:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _segments(text: str, spans: list[tuple[int, int]], start: int, end: int) -> list[Segment]:
    segments = []
    position = start
    for span_start, span_end in spans:
        span_start, span_end = max(span_start, start), min(span_end, end)
        if span_start >= span_end:
            continue
        if span_start > position:
            segments.append(Segment(text[position:span_start], False))
        segments.append(Segment(text[span_start:span_end], True))
        position = span_end
    if position < end:
        segments.append(Segment(text[position:end], False))
    return segments


def highlight(text: str, terms: tuple[str, ...]) -> list[Segment]:
    """Split text into segments, marking every match of the (folded) terms."""
    if not terms or not text:
        return [Segment(text, False)]
    folded, origin = fold_with_map(text)
    return _segments(text, _spans(folded, origin, len(text), terms), 0, len(text))


def _window(line: str, match_start: int, match_end: int) -> tuple[int, int]:
    width = max(SNIPPET_WIDTH, match_end - match_start)
    start = max(0, match_start - (width - (match_end - match_start)) // 2)
    end = min(len(line), start + width)
    start = max(0, end - width)
    reach = 0
    while start > 0 and not line[start - 1].isspace() and reach < _BOUNDARY_REACH:
        start -= 1
        reach += 1
    reach = 0
    while end < len(line) and not line[end].isspace() and reach < _BOUNDARY_REACH:
        end += 1
        reach += 1
    return start, end


def _snippet(line: str, terms: tuple[str, ...]) -> list[Segment] | None:
    folded, origin = fold_with_map(line)
    first = min((p for p in (folded.find(t) for t in terms) if p != -1), default=-1)
    if first == -1:
        return None
    first_spans = _spans(folded, origin, len(line), terms, stop=origin[first] + 1)
    start, end = _window(line, *first_spans[0])
    segments = _segments(line, _spans(folded, origin, len(line), terms, stop=end), start, end)
    if start > 0:
        segments.insert(0, Segment(ELLIPSIS, False))
    if end < len(line):
        segments.append(Segment(ELLIPSIS, False))
    return segments


def snippets(
    content: str, folded_content: str, terms: tuple[str, ...], limit: int = 3
) -> list[list[Segment]]:
    """Up to limit snippets, each from one line containing a match, in document order."""
    if not terms:
        return []
    found = []
    # Folding never moves or removes "\n", so folded lines line up with the original lines.
    lines = content.split("\n")
    for number, folded_line in enumerate(folded_content.split("\n")):
        if number >= len(lines) or not any(term in folded_line for term in terms):
            continue
        snippet = _snippet(lines[number].removesuffix("\r"), terms)
        if snippet:
            found.append(snippet)
            if len(found) >= limit:
                break
    return found
