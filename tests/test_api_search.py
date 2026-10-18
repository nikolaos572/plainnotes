"""Search and tags API."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def seeded(client: TestClient, data_root: Path) -> TestClient:
    samples = [
        ("Docker commands", "docker run -it alpine\ndocker ps\n#linux #docker"),
        ("Shopping", "milk, café, bread #home"),
        ("Greek", "Καλημέρα κόσμε #συνταγές"),
        ("Cluster", "kubectl get pods; docker once #linux"),
    ]
    for i, (title, content) in enumerate(samples):
        note = client.post("/api/notes", json={"title": title, "content": content}).json()
        mtime = 1_700_000_000 + i * 1000
        os.utime(data_root / note["id"] / f"{title}.md", (mtime, mtime))
    return client


def titles(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [r["title"] for r in response.json()]


def test_result_shape(seeded: TestClient) -> None:
    [result] = seeded.get("/api/search", params={"q": "kubectl"}).json()
    assert set(result) == {
        "id",
        "title",
        "modified",
        "score",
        "tags",
        "title_segments",
        "snippets",
    }
    assert result["title_segments"] == [{"text": "Cluster", "mark": False}]
    assert result["snippets"] == [
        [
            {"text": "kubectl", "mark": True},
            {"text": " get pods; docker once #linux", "mark": False},
        ]
    ]


def test_every_sort_and_order(seeded: TestClient) -> None:
    get = lambda **params: titles(seeded.get("/api/search", params=params))  # noqa: E731
    assert get() == ["Cluster", "Greek", "Shopping", "Docker commands"]
    assert get(sort="modified", order="asc") == ["Docker commands", "Shopping", "Greek", "Cluster"]
    assert get(sort="title") == ["Cluster", "Docker commands", "Greek", "Shopping"]
    assert get(sort="title", order="desc") == ["Shopping", "Greek", "Docker commands", "Cluster"]
    assert get(q="docker") == ["Docker commands", "Cluster"]
    assert get(q="docker", order="asc") == ["Cluster", "Docker commands"]
    assert get(q="docker", sort="modified") == ["Cluster", "Docker commands"]
    assert get(q="docker", sort="title", order="desc") == ["Docker commands", "Cluster"]
    assert get(q="*") == get()


def test_and_phrases_tags_and_accents(seeded: TestClient) -> None:
    get = lambda q: titles(seeded.get("/api/search", params={"q": q}))  # noqa: E731
    assert get("docker kubectl") == ["Cluster"]
    assert get('"docker run"') == ["Docker commands"]
    assert get('"run docker"') == []
    assert get("#linux") == ["Cluster", "Docker commands"]
    assert get("#linux #docker") == ["Docker commands"]
    assert get("#lin") == []
    assert get("CAFE") == ["Shopping"]
    assert get("καλημερα") == ["Greek"]
    assert get("#ΣΥΝΤΑΓΕΣ") == ["Greek"]


def test_trashed_notes_are_excluded(seeded: TestClient) -> None:
    [note] = seeded.get("/api/search", params={"q": "kubectl"}).json()
    seeded.delete(f"/api/notes/{note['id']}")
    assert titles(seeded.get("/api/search", params={"q": "kubectl"})) == []
    tags = seeded.get("/api/tags").json()
    assert {"tag": "linux", "count": 1} in tags


@pytest.mark.parametrize(
    "params",
    [
        {"q": "x" * 501},
        {"q": " ".join(["a"] * 21)},
        {"sort": "size"},
        {"order": "sideways"},
    ],
)
def test_bad_query(seeded: TestClient, params: dict) -> None:
    response = seeded.get("/api/search", params=params)
    assert response.status_code == 400
    assert response.json()["code"] == "bad_query"


def test_tags_sorted_with_counts(seeded: TestClient, make_note: Callable) -> None:
    make_note("Upper", "#Linux and #Zebra", note_id="20200101-000000-uppr")
    assert seeded.get("/api/tags").json() == [
        {"tag": "docker", "count": 1},
        {"tag": "home", "count": 1},
        {"tag": "linux", "count": 3},
        {"tag": "zebra", "count": 1},
        {"tag": "συνταγές", "count": 1},
    ]


def test_search_terms_are_not_logged(seeded: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    seeded.get("/api/search", params={"q": "very-secret-term"})
    assert "very-secret-term" not in caplog.text
