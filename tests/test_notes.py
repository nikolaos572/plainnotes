"""Note IDs, titles, and the notes service working directly on a data folder."""

import logging
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import notes
from app.fsutil import DataDir

ID = "20261018-143205-k3f9"


@pytest.mark.parametrize(
    "title",
    [
        "Docker commands",
        "Καλημέρα",
        "a",
        "café ☕",
        "x" * 252,
        "é" * 126,
        "name.with.dots",
        "trailing.",
        "#hashtag",
        "semi;colon & ampersand",
    ],
)
def test_valid_titles(title: str) -> None:
    assert notes.clean_title(title) == title


@pytest.mark.parametrize(
    "title",
    [
        "",
        "   ",
        ".hidden",
        "x" * 253,
        "é" * 127,
        "a/b",
        "a\\b",
        "a:b",
        "a*b",
        "a?b",
        'a"b',
        "a<b",
        "a>b",
        "a|b",
        "tab\there",
        "new\nline",
        "nul\x00",
        "del\x7f",
        "\x1f",
        "lone \udc80 surrogate",
    ],
)
def test_invalid_titles(title: str) -> None:
    with pytest.raises(notes.InvalidTitle):
        notes.clean_title(title)


def test_title_is_trimmed_but_otherwise_unchanged() -> None:
    assert notes.clean_title("  Mixed Case  \t") == "Mixed Case"
    assert notes.clean_title("\u00a0x\u00a0") == "x"


def test_title_key_ignores_case_and_normalization() -> None:
    assert notes.title_key("Docker") == notes.title_key("DOCKER")
    assert notes.title_key("café") == notes.title_key("CAFE\u0301")
    assert notes.title_key("Straße") == notes.title_key("STRASSE")


def test_id_format_and_regeneration_on_collision() -> None:
    moment = datetime(2026, 10, 18, 14, 32, 5, tzinfo=UTC)
    first = notes.new_id(moment, lambda candidate: False)
    assert notes.NOTE_ID_RE.fullmatch(first)
    assert first.startswith("20261018-143205-")
    seen: list[str] = []

    def taken(candidate: str) -> bool:
        seen.append(candidate)
        return len(seen) < 3

    result = notes.new_id(moment, taken)
    assert len(seen) == 3
    assert result == seen[-1]


@pytest.mark.parametrize(
    "name",
    [ID, "20261018-143205-0000"],
)
def test_is_note_id_accepts(name: str) -> None:
    assert notes.is_note_id(name)


@pytest.mark.parametrize(
    "name",
    [
        "20261018-143205-K3F9",
        "20261018-143205-k3f",
        "20261018-143205-k3f9\n",
        "٢٠٢٦١٠١٨-143205-k3f9",
    ],
)
def test_is_note_id_rejects(name: str) -> None:
    assert not notes.is_note_id(name)


def test_create_and_read(data_dir: DataDir) -> None:
    note = notes.create_note(data_dir, "  First note ", "hello\n")
    assert note.ref.title == "First note"
    assert notes.NOTE_ID_RE.fullmatch(note.ref.id)
    path = data_dir.root / note.ref.id / "First note.md"
    assert path.read_bytes() == b"hello\n"
    again = notes.read_note(data_dir, note.ref.id)
    assert again.content == "hello\n"
    assert again.revision == note.revision == notes.revision_of(b"hello\n")
    assert [p.name for p in data_dir.root.iterdir()] == [note.ref.id]


def test_create_rejects_duplicate_title(data_dir: DataDir) -> None:
    notes.create_note(data_dir, "Docker", "")
    with pytest.raises(notes.TitleExists):
        notes.create_note(data_dir, "DOCKER", "")


def test_create_too_large(data_dir: DataDir) -> None:
    notes.create_note(data_dir, "max", "x" * notes.MAX_CONTENT_BYTES)
    with pytest.raises(notes.ContentTooLarge):
        notes.create_note(data_dir, "big", "x" * (notes.MAX_CONTENT_BYTES + 1))


def test_update_content_and_rename(data_dir: DataDir) -> None:
    note = notes.create_note(data_dir, "Old", "one")
    updated = notes.update_note(data_dir, note.ref.id, "New", "two", note.revision)
    assert updated.ref.title == "New"
    folder = data_dir.root / note.ref.id
    assert [p.name for p in folder.iterdir()] == ["New.md"]
    assert (folder / "New.md").read_bytes() == b"two"


def test_case_only_rename(data_dir: DataDir) -> None:
    note = notes.create_note(data_dir, "docker", "x")
    updated = notes.update_note(data_dir, note.ref.id, "Docker", "x", note.revision)
    assert updated.ref.title == "Docker"


def test_rename_conflict(data_dir: DataDir) -> None:
    notes.create_note(data_dir, "Taken", "")
    note = notes.create_note(data_dir, "Mine", "")
    with pytest.raises(notes.TitleExists):
        notes.update_note(data_dir, note.ref.id, "taken", "", note.revision)


def test_revision_conflict_carries_current(data_dir: DataDir) -> None:
    note = notes.create_note(data_dir, "Note", "v1")
    note.ref.path.write_bytes(b"changed on disk")
    with pytest.raises(notes.RevisionConflict) as info:
        notes.update_note(data_dir, note.ref.id, "Note", "v2", note.revision)
    assert info.value.current.content == "changed on disk"
    assert note.ref.path.read_bytes() == b"changed on disk"


def test_unchanged_update_writes_nothing(data_dir: DataDir) -> None:
    note = notes.create_note(data_dir, "Note", "same")
    os.utime(note.ref.path, (1_000_000, 1_000_000))
    result = notes.update_note(data_dir, note.ref.id, "Note", "same", note.revision)
    assert result.ref.mtime == 1_000_000
    assert note.ref.path.stat().st_mtime == 1_000_000


def test_keeping_an_externally_invalid_title(data_dir: DataDir, make_note: Callable) -> None:
    make_note("a: b", "x", note_id=ID)
    note = notes.read_note(data_dir, ID)
    updated = notes.update_note(data_dir, ID, "a: b", "y", note.revision)
    assert updated.ref.title == "a: b"
    with pytest.raises(notes.InvalidTitle):
        notes.update_note(data_dir, ID, "c: d", "y", updated.revision)


def test_not_found(data_dir: DataDir) -> None:
    for bad in [ID, "..", "", "x", "../" + ID]:
        with pytest.raises(notes.NotFound):
            notes.read_note(data_dir, bad)
    with pytest.raises(notes.NotFound):
        notes.update_note(data_dir, ID, "t", "c", "r")


def test_not_utf8(data_dir: DataDir, make_note: Callable) -> None:
    make_note("Latin1", "caf\xe9".encode("latin-1"), note_id=ID)
    assert [r.title for r in notes.list_notes(data_dir)] == ["Latin1"]
    with pytest.raises(notes.NotUtf8):
        notes.read_note(data_dir, ID)


def test_bom_is_content(data_dir: DataDir, make_note: Callable) -> None:
    make_note("Bom", b"\xef\xbb\xbfhello", note_id=ID)
    assert notes.read_note(data_dir, ID).content == "\ufeffhello"


def test_listing_rules(
    data_dir: DataDir, make_note: Callable, caplog: pytest.LogCaptureFixture
) -> None:
    make_note("Good", "x", note_id=ID)
    root = data_dir.root
    (root / "20261018-143205-zero").mkdir()
    two = root / "20261018-143205-twoo"
    two.mkdir()
    (two / "a.md").write_text("a")
    (two / "b.md").write_text("b")
    (root / "not-a-note").mkdir()
    (root / "not-a-note" / "x.md").write_text("x")
    (root / ".hidden-20261018-143205-hide").mkdir()
    link_target = root.parent / "elsewhere"
    link_target.mkdir()
    (link_target / "Linked.md").write_text("x")
    (root / "20261018-143205-link").symlink_to(link_target)
    with_link = root / "20261018-143205-lnkf"
    with_link.mkdir()
    (with_link / "Symlinked.md").symlink_to(link_target / "Linked.md")
    with_dotfile = root / "20261018-143205-dotf"
    with_dotfile.mkdir()
    (with_dotfile / "Real.md").write_text("x")
    (with_dotfile / "._Real.md").write_text("mac metadata")
    with caplog.at_level(logging.WARNING):
        titles = sorted(r.title for r in notes.list_notes(data_dir))
        notes.list_notes(data_dir)
    assert titles == ["Good", "Real"]
    warnings = [r.getMessage() for r in caplog.records]
    assert sum("20261018-143205-twoo" in w for w in warnings) == 1
    assert sum("20261018-143205-zero" in w for w in warnings) == 1
    assert not any("not-a-note" in w for w in warnings)


def test_external_edits_are_visible(data_dir: DataDir, make_note: Callable) -> None:
    path = make_note("Title", "one", note_id=ID)
    assert notes.read_note(data_dir, ID).content == "one"
    path.write_text("two")
    assert notes.read_note(data_dir, ID).content == "two"
    path.rename(path.with_name("Renamed.md"))
    assert notes.read_note(data_dir, ID).ref.title == "Renamed"


def test_created_from_id(data_dir: DataDir, make_note: Callable) -> None:
    make_note("T", "", note_id=ID)
    assert notes.find_note(data_dir, ID).created == datetime(2026, 10, 18, 14, 32, 5, tzinfo=UTC)
    make_note("Impossible", "", note_id="20261399-999999-zzzz", mtime=1_000_000)
    ref = notes.find_note(data_dir, "20261399-999999-zzzz")
    assert ref.created == datetime.fromtimestamp(1_000_000, UTC)


def test_delete_moves_folder_to_trash(data_dir: DataDir, make_note: Callable) -> None:
    path = make_note("Gone", "x", note_id=ID)
    (path.parent / "attachments").mkdir()
    (path.parent / "attachments" / "a.png").write_bytes(b"png")
    moment = datetime(2026, 10, 19, 10, 15, 0, tzinfo=UTC)
    trash_id = notes.delete_note(data_dir, ID, now=moment)
    assert trash_id == f"20261019-101500_{ID}"
    trash = data_dir.root / ".trash" / trash_id
    assert (trash / "Gone.md").read_bytes() == b"x"
    assert (trash / "attachments" / "a.png").exists()
    assert notes.list_notes(data_dir) == []
    with pytest.raises(notes.NotFound):
        notes.delete_note(data_dir, ID)


def test_delete_twice_in_same_second(data_dir: DataDir) -> None:
    moment = datetime(2026, 10, 19, 10, 15, 0, tzinfo=UTC)
    note = notes.create_note(data_dir, "Again", "")
    first = notes.delete_note(data_dir, note.ref.id, now=moment)
    shutil.copytree(data_dir.root / ".trash" / first, data_dir.root / note.ref.id)
    second = notes.delete_note(data_dir, note.ref.id, now=moment)
    assert first != second
    assert second.startswith("20261019-101501_")


def test_cleanup_temp_files(data_dir: DataDir, make_note: Callable) -> None:
    root = data_dir.root
    path = make_note("Keep", "x", note_id=ID)
    (root / ".plainnotes-tmp-abc").mkdir()
    (root / ".plainnotes-tmp-abc" / "Half.md").write_text("half")
    (root / ".plainnotes-tmp-file").write_text("x")
    (path.parent / ".plainnotes-tmp-123").write_text("x")
    attachments = path.parent / "attachments"
    attachments.mkdir()
    (attachments / ".plainnotes-tmp-up").write_text("partial upload")
    (attachments / "real.txt").write_text("keep")
    other = root / "unrelated"
    other.mkdir()
    (other / ".plainnotes-tmp-x").write_text("not ours to touch")
    notes.cleanup_temp_files(data_dir)
    assert sorted(p.name for p in root.iterdir()) == [ID, "unrelated"]
    assert sorted(p.name for p in path.parent.iterdir()) == ["Keep.md", "attachments"]
    assert [p.name for p in attachments.iterdir()] == ["real.txt"]
    assert (other / ".plainnotes-tmp-x").exists()


def test_import_loose_file(data_dir: DataDir) -> None:
    loose = data_dir.root / "Some file.md"
    loose.write_bytes(b"line one\r\nline two")
    mtime = datetime(2020, 5, 6, 7, 8, 9, tzinfo=UTC).timestamp()
    os.utime(loose, (mtime, mtime))
    notes.import_loose_files(data_dir)
    [ref] = notes.list_notes(data_dir)
    assert ref.title == "Some file"
    assert ref.id.startswith("20200506-070809-")
    assert ref.path.read_bytes() == b"line one\r\nline two"
    assert ref.path.stat().st_mtime == mtime
    assert not loose.exists()


def test_import_leaves_invalid_and_duplicate_titles(
    data_dir: DataDir, make_note: Callable, caplog: pytest.LogCaptureFixture
) -> None:
    make_note("Existing", "x", note_id=ID)
    root = data_dir.root
    (root / "existing.md").write_text("duplicate")
    (root / ".hidden.md").write_text("hidden")
    (root / "a:b.md").write_text("invalid")
    (root / "Same.md").write_text("first")
    (root / "same.md").write_text("second")
    outside = root.parent / "outside.md"
    outside.write_text("outside")
    (root / "Link.md").symlink_to(outside)
    (root / "folder.md").mkdir()
    with caplog.at_level(logging.WARNING):
        notes.import_loose_files(data_dir)
        notes.import_loose_files(data_dir)
    titles = sorted(r.title for r in notes.list_notes(data_dir))
    assert titles == ["Existing", "Same"]
    left = sorted(p.name for p in root.iterdir() if not notes.is_note_id(p.name))
    assert left == [".hidden.md", "Link.md", "a:b.md", "existing.md", "folder.md", "same.md"]
    warnings = [r.getMessage() for r in caplog.records]
    assert sum("existing.md" in w for w in warnings) == 1
    assert sum("a:b.md" in w for w in warnings) == 1


def test_import_ignores_trash(data_dir: DataDir) -> None:
    (data_dir.root / ".trash").mkdir()
    (data_dir.root / ".trash" / "Inside.md").write_text("x")
    notes.import_loose_files(data_dir)
    assert notes.list_notes(data_dir) == []


def test_paths_never_leave_data_folder(data_dir: DataDir, tmp_path: Path) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("secret")
    for bad in ["../secret.md", "/etc/passwd", "..", "%2e%2e"]:
        with pytest.raises(notes.NotFound):
            notes.read_note(data_dir, bad)
