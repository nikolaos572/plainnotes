"""Query parsing, matching, scoring and sorting."""

import os
from collections.abc import Callable

import pytest

from app import search
from app.fsutil import DataDir
from app.search import BadQuery, Query, parse_query


def test_parse_words_phrases_and_tags() -> None:
    query = parse_query('Docker "run -it"  #Linux café')
    assert query.terms == ("docker", "run -it", "cafe")
    assert query.tags == ("linux",)


def test_parse_unclosed_quote_makes_rest_one_phrase() -> None:
    assert parse_query('a "b c  d').terms == ("a", "b c  d")


def test_parse_quoted_hash_is_a_phrase() -> None:
    query = parse_query('"#include <x>"')
    assert query.terms == ("#include <x>",)
    assert query.tags == ()


def test_parse_empty_and_star() -> None:
    assert parse_query("") == Query()
    assert parse_query("   ") == Query()
    assert parse_query(" * ") == Query()
    assert parse_query('""').is_empty


def test_parse_limits() -> None:
    parse_query("x" * 500)
    with pytest.raises(BadQuery):
        parse_query("x" * 501)
    parse_query(" ".join(["a"] * 20))
    with pytest.raises(BadQuery):
        parse_query(" ".join(["a"] * 21))


def test_title_text_excludes_tags() -> None:
    assert parse_query("Docker #linux commands").title_text == "docker commands"


@pytest.fixture
def corpus(data_dir: DataDir, make_note: Callable) -> DataDir:
    make_note("Docker commands", "docker run\ndocker ps\n#linux", note_id="20260101-000001-aaaa")
    make_note("Kubernetes", "kubectl, and docker once. #linux #k8s", note_id="20260101-000002-bbbb")
    make_note("Shopping list", "milk\ncafé beans\n#home", note_id="20260101-000003-cccc")
    make_note("Καλημέρα", "Ελληνικό κείμενο για docker", note_id="20260101-000004-dddd")
    make_note("Binary", b"docker \xff\xfe", note_id="20260101-000005-eeee")
    for i, note_id in enumerate(sorted(p.name for p in data_dir.root.iterdir())):
        mtime = 1_700_000_000 + i * 100
        md = next((data_dir.root / note_id).glob("*.md"))
        os.utime(md, (mtime, mtime))
    return data_dir


def titles(results: list[search.Result]) -> list[str]:
    return [r.ref.title for r in results]


def test_empty_query_lists_newest_first(corpus: DataDir) -> None:
    results = search.search(corpus, "")
    expected = ["Binary", "Καλημέρα", "Shopping list", "Kubernetes", "Docker commands"]
    assert titles(results) == expected
    assert all(r.snippets == [] for r in results)
    assert results[0].tags == []


def test_and_semantics(corpus: DataDir) -> None:
    assert titles(search.search(corpus, "docker kubectl")) == ["Kubernetes"]
    assert titles(search.search(corpus, "docker nothing-like-this")) == []


def test_accent_insensitive(corpus: DataDir) -> None:
    assert titles(search.search(corpus, "CAFE")) == ["Shopping list"]
    assert titles(search.search(corpus, "καλημερα")) == ["Καλημέρα"]
    assert titles(search.search(corpus, "ΕΛΛΗΝΙΚΟ")) == ["Καλημέρα"]


def test_phrases(corpus: DataDir) -> None:
    assert titles(search.search(corpus, '"docker run"')) == ["Docker commands"]
    assert titles(search.search(corpus, '"run docker"')) == []


def test_tag_filters_are_exact(corpus: DataDir) -> None:
    assert sorted(titles(search.search(corpus, "#linux"))) == ["Docker commands", "Kubernetes"]
    assert titles(search.search(corpus, "#lin")) == []
    assert titles(search.search(corpus, "#LINUX #k8s")) == ["Kubernetes"]
    assert titles(search.search(corpus, "#linux kubectl")) == ["Kubernetes"]


def test_non_utf8_notes_match_by_title_only(corpus: DataDir) -> None:
    assert titles(search.search(corpus, "binary")) == ["Binary"]
    assert "Binary" not in titles(search.search(corpus, "docker"))


def test_scoring_order(corpus: DataDir) -> None:
    results = search.search(corpus, "docker")
    assert titles(results) == ["Docker commands", "Καλημέρα", "Kubernetes"]
    # Title +10, two content occurrences +2.
    assert results[0].score == 12
    assert [r.score for r in results[1:]] == [1, 1]


def test_exact_title_bonus(corpus: DataDir) -> None:
    [result] = search.search(corpus, "docker commands")
    assert result.score == 10 + 2 + 10 + 0 + 20
    [phrase] = search.search(corpus, '"docker commands"')
    assert phrase.score == 10 + 0 + 20


def test_tag_score(corpus: DataDir) -> None:
    [result] = search.search(corpus, "#k8s")
    assert result.score == 5


def test_occurrences_are_capped(data_dir: DataDir, make_note: Callable) -> None:
    make_note("Many", "word " * 50)
    [result] = search.search(data_dir, "word")
    assert result.score == 10


def test_sorts_and_orders(corpus: DataDir) -> None:
    by_title = titles(search.search(corpus, "", sort="title"))
    assert by_title == ["Binary", "Docker commands", "Kubernetes", "Shopping list", "Καλημέρα"]
    assert titles(search.search(corpus, "", sort="title", order="desc")) == by_title[::-1]
    newest = titles(search.search(corpus, "", sort="modified"))
    assert titles(search.search(corpus, "", sort="modified", order="asc")) == newest[::-1]
    relevance = titles(search.search(corpus, "docker", sort="relevance", order="asc"))
    assert relevance == ["Kubernetes", "Καλημέρα", "Docker commands"]
    assert titles(search.search(corpus, "docker", sort="modified")) == [
        "Καλημέρα",
        "Kubernetes",
        "Docker commands",
    ]


@pytest.mark.parametrize(("sort", "order"), [("size", None), (None, "up"), ("TITLE", None)])
def test_bad_sort_options(corpus: DataDir, sort: str | None, order: str | None) -> None:
    with pytest.raises(BadQuery):
        search.search(corpus, "", sort=sort, order=order)


def test_highlights(corpus: DataDir) -> None:
    [result] = search.search(corpus, "beans cafe")
    assert [s.text for s in result.title_segments] == ["Shopping list"]
    [snippet] = result.snippets
    assert [(s.text, s.mark) for s in snippet] == [("café", True), (" ", False), ("beans", True)]


def test_snippets_only_for_first_100(data_dir: DataDir, make_note: Callable) -> None:
    for i in range(105):
        make_note(f"Note {i:03d}", "match here")
    results = search.search(data_dir, "match", sort="title")
    assert all(r.snippets for r in results[:100])
    assert not any(r.snippets for r in results[100:])


def test_trash_is_excluded(corpus: DataDir) -> None:
    from app import notes

    notes.delete_note(corpus, "20260101-000001-aaaa")
    assert "Docker commands" not in titles(search.search(corpus, "docker"))
    assert all(tag != "k8s" or count == 1 for tag, count in search.list_tags(corpus))


def test_list_tags(corpus: DataDir, make_note: Callable) -> None:
    make_note("Another", "#Linux and #Ünï", note_id="20260101-000000-ffff")
    assert search.list_tags(corpus) == [("home", 1), ("k8s", 1), ("linux", 3), ("ünï", 1)]
