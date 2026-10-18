"""Folding, tag extraction, highlighting and snippets."""

import pytest

from app import text
from app.text import ELLIPSIS, Segment


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Καλημέρα", "καλημερα"),
        ("ΛΌΓΟΣ", "λογος"),
        ("Straße", "strasse"),
        ("CAFÉ", "cafe"),
        ("e\u0301", "e"),
        ("café", "cafe\u0301"),
        ("İstanbul", "istanbul"),
    ],
)
def test_fold_examples(a: str, b: str) -> None:
    assert text.fold(a) == text.fold(b)


def test_fold_keeps_other_text() -> None:
    assert text.fold("Docker RUN -it") == "docker run -it"
    assert text.fold("emoji 😀 stays") == "emoji 😀 stays"
    assert text.fold("a\nb\r\nc") == "a\nb\r\nc"
    # Astral combining marks (here U+1D167, a musical combining mark) are removed too.
    assert text.fold("x\U0001d167y") == "xy"
    assert text.fold("ת\u05b4") == "ת"


def test_fold_many_distinct_marks() -> None:
    marks = "".join(chr(c) for c in range(0x300, 0x330))
    assert text.fold("a" + marks + "b") == "ab"


def test_fold_with_map_points_to_sources() -> None:
    folded, origin = text.fold_with_map("Aße\u0301😀")
    assert folded == "asse😀"
    assert origin == [0, 1, 1, 2, 4]


TAG_EXAMPLES = [
    ("#docker", ["docker"]),
    ("see #linux-tips.", ["linux-tips"]),
    ("#συνταγές", ["συνταγές"]),
    ("# Heading", []),
    ("##x", []),
    ("issue #42", []),
    ("https://x.org/#top", []),
    ("`#notatag`", []),
]


@pytest.mark.parametrize(("source", "expected"), TAG_EXAMPLES)
def test_tag_examples(source: str, expected: list[str]) -> None:
    assert text.extract_tags(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("a #x, #y; #z: #w! #v? (#u) [#t] {#s}", ["x", "y", "z", "w", "v"]),
        ("(#inparens)", []),
        ("#a)b", ["a"]),
        ("#tag/sub", []),
        ("#tag's", []),
        ("#UPPER and #upper", ["upper"]),
        ("#Ünïcode", ["ünïcode"]),
        ("#2024-plan", ["2024-plan"]),
        ("#_", []),
        ("#a_1-b", ["a_1-b"]),
        ("line\n#second\r\n#third\r\n", ["second", "third"]),
        ("tab\t#tabbed", ["tabbed"]),
        ("word#notag", []),
        ("#ΟΔΟΣ", ["οδος"]),
        ("#café #CAFÉ", ["café"]),
        # A decomposed accent is a combining mark, not a letter, so it ends the body.
        ("#cafe\u0301", []),
    ],
)
def test_tag_rules(source: str, expected: list[str]) -> None:
    assert text.extract_tags(source) == expected


def test_tags_in_fenced_code_are_ignored() -> None:
    source = (
        "#before\n"
        "```bash\n"
        "# comment\n"
        "echo #notag\n"
        "```\n"
        "#middle\n"
        "~~~~\n"
        "#tilde\n"
        "~~~\n"
        "still code #inside\n"
        "~~~~\n"
        "#after\n"
        "  ```\n"
        "#indented-fence\n"
        "  ```\n"
        "````\n"
        "#unclosed\n"
    )
    assert text.extract_tags(source) == ["before", "middle", "after"]


def test_backtick_fence_requires_no_backticks_in_info() -> None:
    assert text.extract_tags("```inline``` #real\n#also") == ["real", "also"]


def test_tags_in_inline_code_are_ignored() -> None:
    source = "use `#a` and ``x #b ` y`` but #c, `unmatched #d"
    assert text.extract_tags(source) == ["c", "d"]


def test_tag_display_is_first_occurrence_lowercased() -> None:
    assert text.extract_tags("#Docker then #DOCKER and #docker") == ["docker"]
    assert text.extract_tags("#ΣΟΦΟΣ") == ["σοφος"]


def test_tag_regex_is_fast_on_pathological_input() -> None:
    assert text.extract_tags("#" + "a" * 200_000 + "@") == []
    assert text.extract_tags(" #a" * 50_000) == ["a"]
    assert text.extract_tags("a " + "`" * 10_000 + " #x") == ["x"]
    assert text.extract_tags("a " + " ".join("`" * k for k in range(1, 400)) + " #x") == ["x"]


def marked(segments: list[Segment]) -> list[str]:
    return [s.text for s in segments if s.mark]


def joined(segments: list[Segment]) -> str:
    return "".join(s.text for s in segments)


def test_highlight_marks_every_match_in_original_text() -> None:
    segments = text.highlight("Café and CAFE and cafe\u0301", (text.fold("cafe"),))
    assert marked(segments) == ["Café", "CAFE", "cafe\u0301"]
    assert joined(segments) == "Café and CAFE and cafe\u0301"


def test_highlight_sharp_s_and_greek() -> None:
    assert marked(text.highlight("Die Straße", ("strasse",))) == ["Straße"]
    assert marked(text.highlight("Die Straße", ("ss",))) == ["ß"]
    assert marked(text.highlight("ΛΌΓΟΣ και λόγος", (text.fold("λογος"),))) == ["ΛΌΓΟΣ", "λόγος"]


def test_highlight_emoji_and_overlaps() -> None:
    segments = text.highlight("😀 docker 😀 dock", ("dock", "docker"))
    assert marked(segments) == ["docker", "dock"]
    assert joined(segments) == "😀 docker 😀 dock"


def test_highlight_without_terms() -> None:
    assert text.highlight("Title", ()) == [Segment("Title", False)]


def test_snippets_document_order_and_limit() -> None:
    content = "intro\nfirst docker line\nnothing\nsecond Docker\r\nthird DOCKER\nfourth docker\n"
    result = text.snippets(content, text.fold(content), ("docker",))
    assert [joined(s) for s in result] == ["first docker line", "second Docker", "third DOCKER"]
    assert [marked(s) for s in result] == [["docker"], ["Docker"], ["DOCKER"]]


def test_snippet_window_on_long_line() -> None:
    words = " ".join(f"word{i}" for i in range(20_000))
    line = words + " Καλημέρα " + words
    result = text.snippets(line, text.fold(line), ("καλημερα",))
    [snippet] = result
    assert snippet[0] == Segment(ELLIPSIS, False)
    assert snippet[-1] == Segment(ELLIPSIS, False)
    assert marked(snippet) == ["Καλημέρα"]
    body = joined(snippet[1:-1])
    assert 140 <= len(body) <= 220
    assert body.startswith(" ") or body[0] == "w"
    assert not body.split()[0].startswith("ord")


def test_snippet_on_line_without_spaces() -> None:
    line = "x" * 100_000 + "needle" + "y" * 100_000
    [snippet] = text.snippets(line, text.fold(line), ("needle",))
    assert marked(snippet) == ["needle"]
    assert len(joined(snippet)) < 250


def test_snippet_with_accents_emoji_and_sharp_s() -> None:
    line = "😀 Grüße aus der Straße, café 😀"
    [snippet] = text.snippets(line, text.fold(line), ("strasse", "cafe", "grusse"))
    assert joined(snippet) == line
    assert marked(snippet) == ["Grüße", "Straße", "café"]


def test_no_snippets_without_terms() -> None:
    assert text.snippets("a\nb", "a\nb", ()) == []
