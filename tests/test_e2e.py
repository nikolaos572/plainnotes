"""Browser tests: the real app in a uvicorn subprocess, driven by Playwright and Chromium."""

import base64
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Route,
    expect,
    sync_playwright,
)

from tests.conftest import PASSWORD, SECRET_KEY, USERNAME, free_port

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parent.parent

# Records CSP violations and JS dialogs, so tests can assert nothing ever tried to run.
WATCH_SCRIPT = """
window.__violations = [];
document.addEventListener("securitypolicyviolation", (e) => {
  window.__violations.push(e.violatedDirective + " " + e.blockedURI);
});
"""


@dataclass
class Server:
    url: str
    data: Path  # the notes folder inside the configured data folder


@contextmanager
def running_server(data: Path, log: Path, **settings: str) -> Iterator[Server]:
    """Run `python -m app` in a subprocess until the block ends; settings are PLAINNOTES_*."""
    port = free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PLAINNOTES_USERNAME": USERNAME,
        "PLAINNOTES_PASSWORD": PASSWORD,
        "PLAINNOTES_SECRET_KEY": SECRET_KEY,
        "PLAINNOTES_DATA_DIR": str(data),
        "PLAINNOTES_COOKIE_SECURE": "false",
        "PLAINNOTES_HOST": "127.0.0.1",
        "PLAINNOTES_PORT": str(port),
        **{f"PLAINNOTES_{name.upper()}": value for name, value in settings.items()},
    }
    url = f"http://127.0.0.1:{port}{settings.get('base_path', '')}/"
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "app"], cwd=ROOT, env=env, stdout=output, stderr=output
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    with urllib.request.urlopen(url + "health", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(f"server did not start:\n{log.read_text()}") from None
                    time.sleep(0.1)
            yield Server(url, data / "notes")
        finally:
            process.terminate()
            process.wait(timeout=10)


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    log_folder = tmp_path_factory.mktemp("e2e-log")
    with running_server(tmp_path_factory.mktemp("e2e-data"), log_folder / "server.log") as running:
        yield running


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def context(browser: Browser, server: Server) -> Iterator[BrowserContext]:
    context = browser.new_context(
        base_url=server.url, permissions=["clipboard-read", "clipboard-write"]
    )
    context.add_init_script(WATCH_SCRIPT)
    yield context
    context.close()


def watched(page: Page) -> list[str]:
    """Collect JavaScript dialogs and page errors; neither should ever happen."""
    problems: list[str] = []
    page.on(
        "dialog", lambda dialog: (problems.append(f"dialog: {dialog.message}"), dialog.dismiss())
    )
    page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
    return problems


def assert_clean(page: Page, problems: list[str]) -> None:
    assert problems == []
    if not page.is_closed():
        assert page.evaluate("window.__violations ?? []") == []


@pytest.fixture
def page(context: BrowserContext, server: Server) -> Iterator[Page]:
    """A logged-in page that fails the test on JavaScript dialogs, page errors or CSP reports."""
    page = context.new_page()
    problems = watched(page)
    response = context.request.post(
        server.url + "api/login",
        data=json.dumps({"username": USERNAME, "password": PASSWORD}),
        headers={"Content-Type": "application/json", "X-Requested-With": "plainnotes"},
    )
    assert response.status == 204
    yield page
    assert_clean(page, problems)


def login_in_browser(page: Page) -> None:
    page.get_by_label("Username").fill(USERNAME)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()
    page.locator(".app-header").wait_for()


def unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def api(page: Page, method: str, path: str, body: object = None) -> dict | list | None:
    response = page.context.request.fetch(
        path,
        method=method,
        data=json.dumps(body) if body is not None else None,
        headers={"Content-Type": "application/json", "X-Requested-With": "plainnotes"},
    )
    assert response.ok, response.text()
    return response.json() if response.status != 204 else None


def create_note(page: Page, title: str, content: str) -> dict:
    return api(page, "POST", "api/notes", {"title": title, "content": content})


def note_file(server: Server, note: dict, title: str | None = None) -> Path:
    return server.data / note["id"] / f"{title or note['title']}.md"


def png_bytes(width: int = 24, height: int = 16) -> bytes:
    raw = b"".join(b"\x00" + b"\x30\x80\xd0" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def open_markdown(page: Page, note: dict) -> None:
    page.goto(f"#/note/{note['id']}")
    page.get_by_role("button", name="Markdown").click()
    page.locator(".markdown").wait_for()


def assert_nothing_executable(page: Page) -> None:
    """No script elements beyond the page's own four, no on* attributes, no dangerous URLs."""
    report = page.evaluate(
        """() => {
        const scripts = [...document.querySelectorAll("script")].map((s) => s.getAttribute("src"));
        const onAttributes = [];
        for (const element of document.querySelectorAll("*")) {
          for (const attribute of element.attributes) {
            if (attribute.name.toLowerCase().startsWith("on")) {
              onAttributes.push(element.tagName + " " + attribute.name);
            }
          }
        }
        const urls = [...document.querySelectorAll("[href], [src], [action], [formaction]")]
          .map((e) => e.getAttribute("href") ?? e.getAttribute("src") ?? e.getAttribute("action")
            ?? e.getAttribute("formaction"))
          .filter((url) => /^\\s*(javascript|vbscript|data:text)/i.test(url));
        // The only SVG allowed is a button icon from icons.js, placed before the button's label.
        const embedded = [...document.querySelectorAll(
          "#app :is(iframe, object, embed, svg, math, style, form:not(.search, .login-form))"
        )].filter((e) => !e.matches(":is(.button, .menu-item) > svg.icon:first-child")).length;
        return { scripts, onAttributes, urls, embedded };
    }"""
    )
    assert report["scripts"] == [
        "static/js/theme.js",
        "static/vendor/markdown-it.min.js",
        "static/vendor/purify.min.js",
        "static/js/main.js",
    ]
    assert report["onAttributes"] == []
    assert report["urls"] == []
    assert report["embedded"] == 0


# --- login and logout -----------------------------------------------------------------


def test_login_and_logout(context: BrowserContext, server: Server) -> None:
    page = context.new_page()
    page.goto("#/tags")
    expect(page).to_have_url(server.url + "#/login?next=%23%2Ftags")
    heading = page.get_by_role("heading", name="plainnotes", exact=True)
    expect(heading.locator("img.brand-logo")).to_be_visible()
    page.get_by_label("Username").fill(USERNAME)
    page.get_by_label("Password").fill("wrong password")
    page.get_by_role("button", name="Log in").click()
    expect(page.locator(".form-error")).to_have_text("Wrong username or password.")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()
    expect(page).to_have_url(server.url + "#/tags")
    expect(page.get_by_role("heading", name="Tags")).to_be_visible()
    page.get_by_text("Menu").click()
    page.get_by_role("button", name="Log out").click()
    page.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(page.get_by_label("Username")).to_be_visible()
    assert page.request.get(server.url + "api/session").status == 401


def test_login_explains_a_refused_cookie(context: BrowserContext) -> None:
    page = context.new_page()
    problems = watched(page)
    # The password is accepted but no cookie arrives, which is what a browser does with a
    # Secure cookie on a plain-HTTP page (other than localhost).
    page.route("**/api/login", lambda route: route.fulfill(status=204))
    page.goto("#/login")
    page.get_by_label("Username").fill(USERNAME)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()
    expect(page.locator(".form-error")).to_contain_text(
        "refused the login cookie because this page uses plain HTTP"
    )
    expect(page.get_by_label("Password")).to_be_visible()
    assert_clean(page, problems)


# --- editing --------------------------------------------------------------------------


def test_create_edit_save_and_rename(page: Page, server: Server) -> None:
    title = unique("Created in browser")
    page.goto("#/")
    page.get_by_role("banner").get_by_role("link", name="New note").click()
    page.get_by_label("Title").fill(title)
    page.get_by_label("Note text").fill("first line\n")
    page.get_by_role("button", name="Save").click()
    expect(page.locator(".toast").last).to_have_text("Saved")
    expect(page).to_have_url(f"{server.url}#/note/{page.url.split('/note/')[1]}")
    note_id = page.url.split("#/note/")[1].removesuffix("/edit")
    path = server.data / note_id / f"{title}.md"
    assert path.read_bytes() == b"first line\n"
    renamed = title + " (renamed)"
    page.get_by_label("Title").fill(renamed)
    page.get_by_label("Note text").press("Control+End")
    page.keyboard.type("second line")
    page.get_by_role("button", name="Save").click()
    expect(page.locator(".dirty-dot")).to_be_hidden()
    assert (server.data / note_id / f"{renamed}.md").read_bytes() == b"first line\nsecond line"
    assert not path.exists()
    page.get_by_label("Title").fill(renamed.upper())
    page.keyboard.press("Control+s")
    expect(page.locator(".dirty-dot")).to_be_hidden()
    assert sorted(p.name for p in (server.data / note_id).iterdir()) == [
        ".history",
        f"{renamed.upper()}.md",
    ]
    page.get_by_role("button", name="Close").click()
    expect(page.get_by_role("heading", name=renamed.upper())).to_be_visible()
    expect(page).to_have_title(f"{renamed.upper()} – plainnotes")


def test_tab_and_save_shortcuts(page: Page, server: Server) -> None:
    note = create_note(page, unique("Shortcuts"), "")
    page.goto(f"#/note/{note['id']}/edit")
    editor = page.get_by_label("Note text")
    editor.click()
    page.keyboard.type("a")
    page.keyboard.press("Tab")
    page.keyboard.type("b")
    assert editor.input_value() == "a\tb"
    expect(editor).to_be_focused()
    expect(page).to_have_title(f"• {note['title']} – plainnotes")
    page.keyboard.press("Control+Enter")
    expect(page).to_have_title(f"{note['title']} – plainnotes")
    assert note_file(server, note).read_bytes() == b"a\tb"
    page.keyboard.type("\tc")
    page.keyboard.press("Control+s")
    expect(page).to_have_title(f"{note['title']} – plainnotes")
    assert note_file(server, note).read_bytes() == b"a\tb\tc"
    page.keyboard.press("Shift+Tab")
    expect(editor).not_to_be_focused()


def test_editor_opens_at_the_top(page: Page) -> None:
    content = "first line\n" + "\n".join(f"line {i}" for i in range(300))
    note = create_note(page, unique("Long note"), content)
    page.goto(f"#/note/{note['id']}/edit")
    editor = page.get_by_label("Note text")
    expect(editor).to_be_focused()
    assert editor.evaluate("field => field.scrollHeight > field.clientHeight")
    assert editor.evaluate("field => [field.scrollTop, field.selectionStart]") == [0, 0]


def test_escape_asks_about_unsaved_changes(page: Page, server: Server) -> None:
    note = create_note(page, unique("Unsaved"), "kept")
    page.goto(f"#/note/{note['id']}/edit")
    page.get_by_label("Note text").press("End")
    page.keyboard.type(" plus more")
    page.keyboard.press("Escape")
    dialog = page.locator("dialog[open]")
    expect(dialog.get_by_role("heading")).to_have_text("Save changes?")
    dialog.get_by_role("button", name="Cancel").click()
    expect(page.get_by_label("Note text")).to_have_value("kept plus more")
    page.keyboard.press("Escape")
    page.locator("dialog[open]").get_by_role("button", name="Discard").click()
    expect(page.get_by_role("heading", name=note["title"])).to_be_visible()
    assert note_file(server, note).read_text() == "kept"


def test_crlf_files_keep_their_line_endings(page: Page, server: Server) -> None:
    note = create_note(page, unique("Windows"), "one\r\ntwo\r\n")
    page.goto(f"#/note/{note['id']}/edit")
    page.get_by_label("Note text").press("Control+End")
    page.keyboard.type("three")
    page.keyboard.press("Control+s")
    expect(page.locator(".dirty-dot")).to_be_hidden()
    assert note_file(server, note).read_bytes() == b"one\r\ntwo\r\nthree"


def test_conflict_dialog(page: Page, server: Server) -> None:
    note = create_note(page, unique("Conflicted"), "original")
    page.goto(f"#/note/{note['id']}/edit")
    expect(page.get_by_label("Note text")).to_have_value("original")
    note_file(server, note).write_text("changed on disk")
    page.get_by_label("Note text").press("End")
    page.keyboard.type(" mine")
    page.keyboard.press("Control+s")
    dialog = page.locator("dialog[open]")
    expect(dialog.get_by_role("heading")).to_have_text(
        "This note was changed somewhere else since you opened it"
    )
    dialog.get_by_role("button", name="Cancel").click()
    expect(page.get_by_label("Note text")).to_have_value("original mine")
    # The first save finishes when the dialog's close event arrives; then Save is enabled again.
    expect(page.locator(".editor-actions .primary")).to_be_enabled()
    page.keyboard.press("Control+s")
    page.locator("dialog[open]").get_by_role("button", name="Overwrite").click()
    expect(page.locator(".dirty-dot")).to_be_hidden()
    assert note_file(server, note).read_text() == "original mine"


# --- viewing and copying --------------------------------------------------------------

EXACT = "tabs\there  \nback\\slash $HOME `tick`\r\nemoji 😀 Καλημέρα\n\nno final newline"


def test_text_mode_and_copy_all(page: Page) -> None:
    note = create_note(page, unique("Exact"), EXACT)
    page.goto(f"#/note/{note['id']}")
    page.get_by_role("button", name="Text").click()
    text = page.locator("pre.note-text")
    assert text.evaluate("element => element.textContent") == EXACT
    page.get_by_role("button", name="Copy all").click()
    expect(page.locator(".toast").last).to_have_text("Copied")
    assert page.evaluate("navigator.clipboard.readText()") == EXACT


def test_code_block_copy_button(page: Page) -> None:
    block = "docker run -it --rm \\\n  --name test \\\n  alpine sh\n"
    note = create_note(page, unique("Code"), f"Run this:\n\n```bash\n{block}```\n")
    open_markdown(page, note)
    page.locator(".code-block").get_by_role("button", name="Copy").click()
    expect(page.locator(".toast").last).to_have_text("Copied")
    assert page.evaluate("navigator.clipboard.readText()") == block


def test_view_mode_is_remembered(page: Page) -> None:
    first = create_note(page, unique("Mode one"), "# One")
    second = create_note(page, unique("Mode two"), "# Two")
    open_markdown(page, first)
    page.goto(f"#/note/{second['id']}")
    expect(page.locator(".markdown h1")).to_have_text("Two")
    page.get_by_role("button", name="Text").click()


def test_tag_links(page: Page) -> None:
    content = "\n\n".join(
        [
            "#docker",
            "see #linux-tips.",
            "#συνταγές",
            "# Heading",
            "##x",
            "issue #42",
            "https://x.org/#top",
            "`#notatag`",
        ]
    )
    note = create_note(page, unique("Tags"), content)
    assert note["tags"] == ["docker", "linux-tips", "συνταγές"]
    open_markdown(page, note)
    links = page.locator(".markdown a.tag")
    expect(links).to_have_text(["#docker", "#linux-tips", "#συνταγές"])
    hrefs = links.evaluate_all("links => links.map((a) => a.getAttribute('href'))")
    assert hrefs == [
        "#/?q=%23docker",
        "#/?q=%23linux-tips",
        "#/?q=%23%CF%83%CF%85%CE%BD%CF%84%CE%B1%CE%B3%CE%AD%CF%82",
    ]
    links.first.click()
    expect(page.locator(".note-row").filter(has_text=note["title"])).to_be_visible()


def test_heading_links_scroll_without_changing_the_route(page: Page) -> None:
    filler = "\n\n".join(f"Paragraph {i}" for i in range(80))
    note = create_note(
        page, unique("Anchors"), f"[jump](#Last-section)\n\n{filler}\n\n## Last section\n"
    )
    open_markdown(page, note)
    page.get_by_role("link", name="jump").click()
    expect(page.locator("#h-last-section")).to_be_in_viewport()
    assert page.url.endswith(f"#/note/{note['id']}")


def test_theme_persists_and_auto_follows_the_system(page: Page) -> None:
    page.emulate_media(color_scheme="light")
    page.goto("#/")
    page.get_by_text("Menu").click()
    page.get_by_role("button", name="Dark").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.get_by_text("Menu").click()
    page.get_by_role("button", name="Auto").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.get_by_text("Menu").click()
    page.get_by_role("button", name="Light").click()
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")


def test_keyboard_shortcuts(page: Page) -> None:
    note = create_note(page, unique("Keys"), "content")
    page.goto(f"#/note/{note['id']}")
    page.get_by_role("heading", name=note["title"]).wait_for()
    page.keyboard.press("e")
    expect(page.get_by_label("Note text")).to_be_visible()
    page.keyboard.press("Escape")
    page.get_by_role("heading", name=note["title"]).wait_for()
    page.keyboard.press("/")
    search = page.get_by_label("Search notes")
    expect(search).to_be_focused()
    page.keyboard.type("Keys and notes")  # "e" and "n" are typed, not taken as shortcuts
    expect(search).to_have_value("Keys and notes")
    page.keyboard.press("Escape")
    expect(search).to_have_value("")
    expect(search).not_to_be_focused()
    page.keyboard.press("/")
    page.keyboard.type(note["title"])
    page.keyboard.press("Enter")
    expect(page.locator(".note-row mark").first).to_be_visible()
    page.keyboard.press("n")
    expect(page).to_have_url(re.compile(r"#/new$"))
    expect(page.get_by_label("Title")).to_be_focused()


# --- undo, line wrapping, pinned notes and printing -----------------------------------


def test_undo_after_moving_a_note_to_the_trash(page: Page, server: Server) -> None:
    note = create_note(page, unique("Undo me"), "keep me\n")
    page.goto(f"#/note/{note['id']}")
    page.get_by_role("button", name="Delete").click()
    page.get_by_role("dialog").get_by_role("button", name="Move to trash").click()
    expect(page).to_have_url(server.url + "#/")
    assert not note_file(server, note).exists()
    toast = page.locator(".toast").filter(has_text="Moved to trash")
    expect(toast.get_by_role("button", name="Undo", exact=True).locator("svg.icon")).to_be_visible()
    toast.get_by_role("button", name="Undo").click()
    expect(toast).to_have_count(0)
    expect(page).to_have_url(f"{server.url}#/note/{note['id']}")
    expect(page.get_by_role("heading", name=note["title"])).to_be_visible()
    assert note_file(server, note).read_text() == "keep me\n"
    assert all(entry["id"] != note["id"] for entry in api(page, "GET", "api/trash"))


def test_emptying_the_trash_deletes_only_the_notes_it_showed(page: Page, server: Server) -> None:
    shown = create_note(page, unique("Shown in the trash"), "junk\n")
    api(page, "DELETE", f"api/notes/{shown['id']}")
    page.goto("#/trash")
    expect(page.locator(".trash-list")).to_contain_text(shown["title"])
    count = page.locator(".trash-list li").count()
    # Moved to the trash in another tab after this page loaded: the dialog never counted it.
    later = create_note(page, unique("Trashed meanwhile"), "only copy\n")
    api(page, "DELETE", f"api/notes/{later['id']}")
    page.get_by_role("button", name="Empty trash").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text(f"{count} deleted note")
    dialog.get_by_role("button", name="Empty trash").click()
    expect(page.locator(".toast").last).to_have_text("Trash emptied")
    expect(page.locator(".trash-list li")).to_have_count(1)
    expect(page.locator(".trash-list")).to_contain_text(later["title"])
    [entry] = api(page, "GET", "api/trash")
    assert entry["id"] == later["id"]
    assert not (server.data / shown["id"]).exists()
    api(page, "POST", f"api/trash/{entry['trash_id']}/restore")
    assert note_file(server, later).read_text() == "only copy\n"


def test_a_held_enter_does_not_confirm_a_permanent_delete(page: Page, server: Server) -> None:
    note = create_note(page, unique("Held Enter"), "keep\n")
    api(page, "DELETE", f"api/notes/{note['id']}")
    page.goto("#/trash")
    delete = (
        page.locator(".trash-list li")
        .filter(has_text=note["title"])
        .get_by_role("button", name="Delete permanently")
    )
    delete.focus()
    page.keyboard.down("Enter")  # opens the confirmation
    page.keyboard.down("Enter")  # the key repeats while it is held
    page.keyboard.up("Enter")
    page.wait_for_load_state("networkidle")
    assert any(entry["id"] == note["id"] for entry in api(page, "GET", "api/trash"))
    expect(page.locator(".trash-list")).to_contain_text(note["title"])
    # Every destructive confirmation starts on Cancel.
    delete.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    assert any(entry["id"] == note["id"] for entry in api(page, "GET", "api/trash"))


def overflows(element: Locator) -> bool:
    """True if the element's content is wider than the element (it scrolls sideways)."""
    return element.evaluate("element => element.scrollWidth > element.clientWidth")


def test_wrap_lines_toggle(page: Page) -> None:
    command = "docker run " + " ".join(f"--option-{i}=value" for i in range(60))
    note = create_note(page, unique("Long line"), command + "\n")
    page.goto(f"#/note/{note['id']}")
    page.get_by_role("button", name="Text").click()
    text = page.locator("pre.note-text")
    wrap = page.get_by_label("Wrap lines")
    expect(wrap).to_be_checked()
    assert not overflows(text)
    wrap.uncheck()
    expect(text).to_have_class(re.compile(r"\bnowrap\b"))
    assert overflows(text)
    assert text.evaluate("element => element.textContent") == command + "\n"
    page.reload()
    expect(page.get_by_label("Wrap lines")).not_to_be_checked()
    expect(page.locator("pre.note-text")).to_have_class(re.compile(r"\bnowrap\b"))
    page.get_by_role("link", name="Edit").click()
    editor = page.get_by_label("Note text")
    expect(editor).to_have_attribute("wrap", "off")
    expect(editor).to_be_focused()
    assert editor.evaluate("field => [field.scrollLeft, field.selectionStart]") == [0, 0]
    assert overflows(editor)
    page.get_by_label("Wrap lines").check()
    expect(editor).to_have_attribute("wrap", "soft")
    assert not overflows(editor)


def test_pinned_notes_come_first(page: Page) -> None:
    pinned = create_note(page, unique("Pinned note"), "Important #Pinned\n")
    newer = create_note(page, unique("Newer note"), "Not pinned\n")
    page.goto("#/")
    sections = page.locator(".note-sections > section")
    expect(sections).to_have_count(2)
    expect(sections.nth(0).get_by_role("heading")).to_have_text("Pinned")
    expect(sections.nth(0).locator(".note-link")).to_have_text([pinned["title"]])
    expect(sections.nth(1).get_by_role("heading")).to_have_text("Other notes")
    expect(sections.nth(1).locator(".note-link").first).to_have_text(newer["title"])
    page.goto("#/?q=%23pinned")
    expect(page.locator(".note-link")).to_have_text([pinned["title"]])
    expect(page.locator(".note-sections")).to_have_count(0)


def test_print_layout_shows_only_the_note(page: Page) -> None:
    note = create_note(page, unique("Printable"), "printed line\n")
    page.emulate_media(color_scheme="dark")
    page.goto(f"#/note/{note['id']}")
    expect(page.get_by_role("heading", name=note["title"])).to_be_visible()
    page.emulate_media(media="print")
    expect(page.locator(".app-header")).to_be_hidden()
    expect(page.locator(".toolbar")).to_be_hidden()
    expect(page.get_by_role("heading", name=note["title"])).to_be_visible()
    expect(page.locator(".note-text")).to_contain_text("printed line")
    # Black on white even though the dark theme is active.
    assert page.locator("html").get_attribute("data-theme") == "dark"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
    page.emulate_media(media="screen")
    expect(page.locator(".app-header")).to_be_visible()


def test_upload_message_fits_a_phone_screen(page: Page) -> None:
    # A name without spaces gives the message no place to wrap unless it may wrap anywhere.
    name = "Screenshot_20261114_104533_com.android.chrome.jpg"
    page.set_viewport_size({"width": 390, "height": 844})
    note = create_note(page, unique("Long name"), "")
    page.goto(f"#/note/{note['id']}/edit")
    page.locator("input[type=file]").set_input_files(
        files=[{"name": name, "mimeType": "image/jpeg", "buffer": b"jpeg"}]
    )
    toast = page.locator(".toast")
    expect(toast).to_have_text(f"Uploaded {name}")
    box = toast.bounding_box()
    assert box is not None
    assert box["x"] >= 0
    assert box["x"] + box["width"] <= 390


def test_upload_image_and_see_it_in_markdown(page: Page, server: Server, tmp_path: Path) -> None:
    note = create_note(page, unique("Picture"), "Screenshot:\n")
    image = tmp_path / "screen shot (1).png"
    image.write_bytes(png_bytes())
    page.goto(f"#/note/{note['id']}/edit")
    page.locator("input[type=file]").set_input_files(image)
    row = page.locator(".attachment-rows li").filter(has_text="screen shot (1).png")
    expect(row).to_be_visible()
    page.get_by_label("Note text").press("Control+End")
    row.get_by_role("button", name="Insert link").click()
    expected = "Screenshot:\n![screen shot (1).png](attachments/screen%20shot%20%281%29.png)"
    expect(page.get_by_label("Note text")).to_have_value(expected)
    page.keyboard.press("Control+s")
    expect(page.locator(".dirty-dot")).to_be_hidden()
    assert (server.data / note["id"] / "attachments" / "screen shot (1).png").exists()
    open_markdown(page, note)
    image_element = page.locator(".markdown img")
    expect(image_element).to_have_attribute(
        "src", f"api/notes/{note['id']}/attachments/screen%20shot%20%281%29.png"
    )
    expect(image_element).to_have_js_property("naturalWidth", 24)


# --- button icons ---------------------------------------------------------------------


def test_buttons_show_icons_and_keep_their_names(page: Page) -> None:
    note = create_note(page, unique("Icons"), "```\necho hi\n```\n")
    open_markdown(page, note)
    controls = [
        page.get_by_role("link", name="New note", exact=True),
        page.get_by_role("link", name="Edit", exact=True),
        page.get_by_role("button", name="Delete", exact=True),
        page.get_by_role("button", name="Copy", exact=True),
    ]
    for control in controls:
        icon = control.locator("svg.icon")
        expect(icon).to_have_count(1)
        expect(icon).to_have_attribute("aria-hidden", "true")
        assert icon.bounding_box()["width"] > 0
    controls[2].click()
    dialog = page.get_by_role("dialog")
    for name in ["Move to trash", "Cancel"]:
        button = dialog.get_by_role("button", name=name, exact=True)
        expect(button.locator("svg.icon")).to_be_visible()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)
    logo = page.get_by_role("link", name="plainnotes", exact=True).locator("img.brand-logo")
    expect(logo).to_have_attribute("alt", "")
    assert logo.evaluate("image => image.complete && image.naturalWidth > 0")
    controls[1].click()
    expect(page.get_by_role("button", name="Save", exact=True).locator("svg.icon")).to_be_visible()


# --- the XSS corpus -------------------------------------------------------------------

XSS = [
    "<script>alert('script')</script>",
    "<img src=x onerror=alert('img')>",
    "<svg onload=alert('svg')><circle r=10 /></svg>",
    "<iframe src=\"javascript:alert('iframe')\"></iframe>",
    "<a href=\"javascript:alert('html-a')\">html link</a>",
    "<details open ontoggle=alert('details')><summary>x</summary></details>",
    "<math><mtext><table><mglyph><style><img src=x onerror=alert('mxss')>",
    "[md](javascript:alert('md'))",
    "[case](JaVaScRiPt:alert('case'))",
    "[vb](vbscript:msgbox('vb'))",
    "[data](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "![image](javascript:alert('image'))",
    "![svgdata](data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+)",
    "<javascript:alert('autolink')>",
    "<https://example.com/\"onmouseover=\"alert('autolink2')>",
    "[entity](&#106;avascript:alert('entity'))",
    "[hex](&#x6A;avascript:alert('hex'))",
    "[tab](java&#x09;script:alert('tab'))",
    "[newline](java\nscript:alert('newline'))",
    "[percent](%6A%61vascript:alert('percent'))",
    "[space]( javascript:alert('space'))",
    "&lt;script&gt;alert('escaped')&lt;/script&gt;",
    "javascript:alert('plain text')",
    "#tag<img src=x onerror=alert('tag')>",
    "[[<img src=x onerror=alert('wiki')>]]",
    '<a href="#" onclick="alert(\'onclick\')">click</a>',
    "<form action=\"javascript:alert('form')\"><button>go</button></form>",
    "[ref]\n\n[ref]: javascript:alert('reference')",
]
XSS_TITLE = "<img src=x onerror=alert('title')> & \"quotes\" 'single'"


@pytest.fixture
def xss_note(page: Page, server: Server) -> dict:
    note = create_note(page, unique("XSS corpus"), "\n\n".join(XSS) + "\n\n#safe-tag")
    upload = page.context.request.fetch(
        f"api/notes/{note['id']}/attachments/{urllib.request.quote('<svg onload=alert(1)>.png')}",
        method="PUT",
        data=png_bytes(),
        headers={"Content-Type": "application/octet-stream", "X-Requested-With": "plainnotes"},
    )
    assert upload.ok, upload.text()
    return note


def test_xss_corpus_in_markdown_view(page: Page, xss_note: dict) -> None:
    open_markdown(page, xss_note)
    expect(page.locator(".markdown a.tag")).to_have_text(["#safe-tag"])
    assert_nothing_executable(page)
    protocols = page.evaluate(
        """() => [...document.querySelectorAll(".markdown [href], .markdown [src]")]
            .map((e) => new URL(e.getAttribute("href") ?? e.getAttribute("src"), document.baseURI))
            .map((url) => url.protocol)"""
    )
    assert protocols
    assert set(protocols) <= {"http:", "https:"}
    page.get_by_role("button", name="Text").click()
    assert page.locator("pre.note-text").evaluate("e => e.textContent").startswith("<script>")
    assert_nothing_executable(page)


def test_xss_in_titles_attachment_names_and_search(
    page: Page, server: Server, xss_note: dict
) -> None:
    folder = server.data / "20260101-000000-xss1"
    folder.mkdir()
    (folder / f"{XSS_TITLE}.md").write_text("<script>alert('content')</script> #x")
    for route in [
        "#/",
        "#/note/20260101-000000-xss1",
        "#/note/20260101-000000-xss1/edit",
        f"#/note/{xss_note['id']}",
        f"#/note/{xss_note['id']}/edit",
        "#/tags",
        "#/?q=" + urllib.request.quote("<img src=x onerror=alert('q')> script"),
        "#/?q=" + urllib.request.quote('"<script>alert(1)</script>"'),
        "#/?q=onerror",
    ]:
        page.goto(route)
        page.locator("main > *").first.wait_for()
        page.wait_for_load_state("networkidle")
        assert_nothing_executable(page)
    page.goto("#/?q=onerror")
    expect(page.locator(".note-row").filter(has_text="XSS corpus").first).to_be_visible()
    expect(page.locator(".note-link").filter(has_text=XSS_TITLE)).to_be_visible()
    page.goto(f"#/note/{xss_note['id']}")
    expect(page.locator(".attachments")).to_contain_text("_svg onload=alert(1)_.png")
    api(page, "DELETE", "api/notes/20260101-000000-xss1")
    page.goto("#/trash")
    expect(page.locator(".trash-list")).to_contain_text(XSS_TITLE)
    assert_nothing_executable(page)


# --- drafts ---------------------------------------------------------------------------


def draft(page: Page, key: str) -> dict | None:
    return page.evaluate(
        """async (key) => {
            const drafts = await import("./static/js/drafts.js");
            return (await drafts.loadDrafts(key === "new" ? null : key))[0] ?? null;
        }""",
        key,
    )


def eventually(check: Callable[[], object], failure: str, timeout: float = 5) -> object:
    """Poll check() until it is truthy (wait_for_function polls with eval(), which CSP blocks)."""
    deadline = time.monotonic() + timeout
    while not (result := check()):
        assert time.monotonic() < deadline, failure
        time.sleep(0.05)
    return result


def wait_for_draft(page: Page, key: str) -> dict:
    return eventually(lambda: draft(page, key), f"no draft for {key}")


def wait_for_no_draft(page: Page, key: str) -> None:
    # The editor removes a draft after the dialog's close event, a task after the click.
    eventually(lambda: draft(page, key) is None, f"draft for {key} was not removed")


def reopen(context: BrowserContext, page: Page, route: str) -> tuple[Page, list[str]]:
    """Close the tab without saving (like a crash) and open the route in a new one."""
    page.close()
    second = context.new_page()
    problems = watched(second)
    second.goto(route)
    return second, problems


def test_draft_survives_a_closed_tab(context: BrowserContext, page: Page, server: Server) -> None:
    note = create_note(page, unique("Draft"), "server text")
    page.goto(f"#/note/{note['id']}/edit")
    editor = page.get_by_label("Note text")
    expect(editor).to_have_value("server text")
    editor.press("End")
    page.keyboard.type(" plus unsaved")
    saved = wait_for_draft(page, note["id"])
    assert (saved["title"], saved["content"]) == (note["title"], "server text plus unsaved")
    assert saved["base_revision"] == note["revision"]
    assert saved["saved_at"].endswith("Z")
    second, problems = reopen(context, page, f"#/note/{note['id']}/edit")
    dialog = second.locator("dialog[open]")
    expect(dialog.get_by_role("heading")).to_contain_text("Restore unsaved draft from")
    dialog.get_by_role("button", name="Restore").click()
    expect(second.get_by_label("Note text")).to_have_value("server text plus unsaved")
    expect(second).to_have_title(f"• {note['title']} – plainnotes")
    second.keyboard.press("Control+s")
    expect(second.locator(".dirty-dot")).to_be_hidden()
    assert note_file(server, note).read_text() == "server text plus unsaved"
    wait_for_no_draft(second, note["id"])
    assert_clean(second, problems)


def test_restored_draft_keeps_its_base_revision(
    context: BrowserContext, page: Page, server: Server
) -> None:
    note = create_note(page, unique("Stale draft"), "v1")
    page.goto(f"#/note/{note['id']}/edit")
    expect(page.get_by_label("Note text")).to_have_value("v1")
    page.get_by_label("Note text").press("End")
    page.keyboard.type(" mine")
    wait_for_draft(page, note["id"])
    note_file(server, note).write_text("v2 from elsewhere")
    second, problems = reopen(context, page, f"#/note/{note['id']}/edit")
    second.locator("dialog[open]").get_by_role("button", name="Restore").click()
    expect(second.get_by_label("Note text")).to_have_value("v1 mine")
    second.keyboard.press("Control+s")
    expect(second.locator("dialog[open]").get_by_role("heading")).to_have_text(
        "This note was changed somewhere else since you opened it"
    )
    assert note_file(server, note).read_text() == "v2 from elsewhere"
    assert_clean(second, problems)


def test_discarding_a_draft(context: BrowserContext, page: Page) -> None:
    note = create_note(page, unique("Discarded draft"), "kept")
    page.goto(f"#/note/{note['id']}/edit")
    expect(page.get_by_label("Note text")).to_have_value("kept")
    page.get_by_label("Note text").press("End")
    page.keyboard.type(" thrown away")
    wait_for_draft(page, note["id"])
    second, problems = reopen(context, page, f"#/note/{note['id']}/edit")
    second.locator("dialog[open]").get_by_role("button", name="Discard").click()
    expect(second.get_by_label("Note text")).to_have_value("kept")
    wait_for_no_draft(second, note["id"])
    assert_clean(second, problems)


def test_draft_of_a_new_note_and_logout(context: BrowserContext, page: Page) -> None:
    title = unique("Never saved")
    page.goto("#/new")
    page.get_by_label("Title").fill(title)
    page.get_by_label("Note text").fill("typed before the crash")
    wait_for_draft(page, "new")
    second, problems = reopen(context, page, "#/new")
    second.locator("dialog[open]").get_by_role("button", name="Restore").click()
    expect(second.get_by_label("Title")).to_have_value(title)
    expect(second.get_by_label("Note text")).to_have_value("typed before the crash")
    wait_for_draft(second, "new")
    second.get_by_text("Menu").click()
    second.get_by_role("button", name="Log out").click()
    second.locator("dialog[open]").get_by_role("button", name="Discard").click()
    second.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(second.get_by_label("Username")).to_be_visible()
    wait_for_no_draft(second, "new")
    assert_clean(second, problems)


# --- read-only mode and sub-path hosting ----------------------------------------------


def test_read_only_ui(browser: Browser, tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "notes" / "20260101-000000-read").mkdir(parents=True)
    (data / "notes" / "20260101-000000-read" / "Read me.md").write_text("# Hello")
    with running_server(data, tmp_path / "server.log", read_only="true") as running:
        context = browser.new_context(base_url=running.url)
        page = context.new_page()
        problems = watched(page)
        page.goto("")
        login_in_browser(page)
        expect(page.get_by_role("link", name="Read me")).to_be_visible()
        expect(page.get_by_role("banner").get_by_role("link", name="New note")).to_be_hidden()
        expect(page.locator(".storage-notice")).to_be_hidden()  # read-only by choice
        page.get_by_text("Menu").click()
        expect(page.get_by_role("link", name="Trash")).to_be_hidden()
        page.goto("#/note/20260101-000000-read")
        expect(page.get_by_role("heading", name="Read me")).to_be_visible()
        expect(page.get_by_role("link", name="Edit")).to_have_count(0)
        expect(page.get_by_role("button", name="Delete")).to_have_count(0)
        page.keyboard.press("e")
        expect(page).to_have_url(running.url + "#/note/20260101-000000-read")
        page.keyboard.press("n")
        expect(page).to_have_url(running.url + "#/note/20260101-000000-read")
        page.goto("#/note/20260101-000000-read/edit")
        expect(page).to_have_url(running.url + "#/note/20260101-000000-read")
        expect(page.get_by_label("Note text")).to_have_count(0)
        page.goto("#/new")
        expect(page).to_have_url(running.url + "#/")
        assert_clean(page, problems)
        context.close()


def test_storage_notice_explains_why_saving_is_off(page: Page) -> None:
    reason = "Saving is turned off: the storage that holds your notes is not supported."

    def session(route: Route) -> None:
        route.fulfill(json={"username": USERNAME, "read_only": True, "read_only_reason": reason})

    page.route("**/api/session", session)
    page.goto("")
    notice = page.locator(".storage-notice")
    expect(notice).to_have_text(reason)
    expect(notice).to_have_attribute("role", "alert")
    expect(page.get_by_role("banner").get_by_role("link", name="New note")).to_be_hidden()
    page.goto("#/tags")
    expect(notice).to_be_visible()
    page.goto("#/login")
    expect(page.get_by_role("button", name="Log in")).to_be_visible()
    expect(notice).to_be_hidden()


def test_sub_path_hosting(browser: Browser, tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with running_server(data, tmp_path / "server.log", base_path="/notes") as running:
        assert running.url.endswith("/notes/")
        context = browser.new_context()
        page = context.new_page()
        problems = watched(page)
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        page.goto(running.url.removesuffix("/"))
        expect(page).to_have_url(running.url + "#/login?next=%23%2F")
        login_in_browser(page)
        page.get_by_role("banner").get_by_role("link", name="New note").click()
        page.get_by_label("Title").fill("Under a prefix")
        page.get_by_label("Note text").fill("#prefixed")
        page.keyboard.press("Control+s")
        expect(page.locator(".dirty-dot")).to_be_hidden()
        page.locator("input[type=file]").set_input_files(
            files=[{"name": "dot.png", "mimeType": "image/png", "buffer": png_bytes()}]
        )
        expect(page.locator(".attachment-rows li")).to_have_count(1)
        page.keyboard.press("Escape")
        page.get_by_role("button", name="Markdown").click()
        page.locator(".markdown a.tag").click()
        expect(page.get_by_role("link", name="Under a prefix")).to_be_visible()
        assert requests[0] == running.url.removesuffix("/")
        assert all(url.startswith(running.url) for url in requests[1:]), requests
        assert {cookie["path"] for cookie in context.cookies()} == {"/notes/"}
        assert_clean(page, problems)
        context.close()


# --- upload SHA-256 check (PLAINNOTES_CHECK_ATTACHMENTS_HASH) -------------------------

SHA256_LENGTHS = [0, 1, 3, 55, 56, 57, 63, 64, 65, 119, 120, 127, 128, 1000, 70_000]
BIG_FILE_BYTES = 2 * 1024 * 1024 + 17  # three 1 MiB reads, the last one short
# Makes the page a browser from before 2022, which cannot compute the SHA-256 of a file.
OLD_BROWSER = "delete AbortSignal.prototype.throwIfAborted;"


def pattern_bytes(length: int) -> bytes:
    return bytes((index * 31 + 7) & 255 for index in range(length))


def content_digest(body: bytes) -> str:
    return f"sha-256=:{base64.b64encode(hashlib.sha256(body).digest()).decode()}:"


def binary_file(name: str, body: bytes) -> dict[str, object]:
    return {"name": name, "mimeType": "application/octet-stream", "buffer": body}


def test_sha256_module_matches_hashlib(page: Page) -> None:
    page.goto("")
    result = page.evaluate(
        """async ([lengths, bigLength]) => {
        const url = new URL("static/js/sha256.js", document.baseURI).href;
        const { Sha256, blobSha256 } = await import(url);
        const pattern = (length) => Uint8Array.from({ length }, (_, i) => (i * 31 + 7) & 255);
        const hex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
        const whole = lengths.map((length) => hex(new Sha256().update(pattern(length)).digest()));
        // The same bytes fed in uneven pieces, so partial blocks must be joined correctly.
        const pieces = lengths.map((length) => {
          const bytes = pattern(length);
          const hash = new Sha256();
          for (let at = 0, step = 1; at < length; at += step, step = ((step * 7) % 97) + 1) {
            hash.update(bytes.subarray(at, at + step));
          }
          return hex(hash.digest());
        });
        const reported = [];
        const big = new Blob([pattern(bigLength)]);
        const file = hex(await blobSha256(big, { onProgress: (share) => reported.push(share) }));
        const finished = new Sha256();
        finished.digest();
        let reused = "";
        try {
          finished.update(new Uint8Array(1));
        } catch (error) {
          reused = error.message;
        }
        const stop = new AbortController();
        stop.abort();
        const aborted = await blobSha256(big, { signal: stop.signal }).catch((error) => error.name);
        return { whole, pieces, file, reported, reused, aborted };
    }""",
        [SHA256_LENGTHS, BIG_FILE_BYTES],
    )
    expected = [hashlib.sha256(pattern_bytes(length)).hexdigest() for length in SHA256_LENGTHS]
    assert result["whole"] == expected
    assert result["pieces"] == expected
    assert result["file"] == hashlib.sha256(pattern_bytes(BIG_FILE_BYTES)).hexdigest()
    mib = 1024 * 1024
    assert result["reported"] == [mib / BIG_FILE_BYTES, 2 * mib / BIG_FILE_BYTES, 1]
    assert "finished" in result["reused"]
    assert result["aborted"] == "AbortError"


def test_upload_sends_no_hash_by_default(page: Page) -> None:
    note = create_note(page, unique("Unchecked"), "")
    sent: list[dict[str, str]] = []
    page.on("request", lambda request: request.method == "PUT" and sent.append(request.headers))
    page.goto(f"#/note/{note['id']}/edit")
    page.locator("input[type=file]").set_input_files(
        files=[{"name": "plain.txt", "mimeType": "text/plain", "buffer": b"plain"}]
    )
    expect(page.locator(".toast").filter(has_text="Uploaded plain.txt")).to_have_text(
        "Uploaded plain.txt"
    )
    assert len(sent) == 1
    assert "content-digest" not in sent[0]


def test_old_browser_uploads_without_the_check(page: Page, server: Server) -> None:
    page.add_init_script(OLD_BROWSER)
    note = create_note(page, unique("Old browser"), "")
    page.goto(f"#/note/{note['id']}/edit")
    page.locator("input[type=file]").set_input_files(files=[binary_file("old.bin", b"old")])
    expect(page.locator(".toast")).to_have_text("Uploaded old.bin")
    assert (server.data / note["id"] / "attachments" / "old.bin").read_bytes() == b"old"


def test_checked_uploads_in_the_editor(browser: Browser, tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with running_server(data, tmp_path / "server.log", check_attachments_hash="true") as running:
        context = browser.new_context(base_url=running.url)
        page = context.new_page()
        problems = watched(page)
        page.goto("")
        login_in_browser(page)
        note = create_note(page, "Checked uploads", "")
        attachments = running.data / note["id"] / "attachments"
        sent: list[dict[str, str]] = []
        page.on("request", lambda request: request.method == "PUT" and sent.append(request.headers))
        picker = page.locator("input[type=file]")
        body = os.urandom(BIG_FILE_BYTES)
        page.goto(f"#/note/{note['id']}/edit")

        # The browser sends the SHA-256; the server checks it and saves the file.
        picker.set_input_files(files=[binary_file("data.bin", body)])
        expect(page.locator(".toast")).to_contain_text("Uploaded data.bin, SHA-256 checked")
        expect(page.locator(".attachment-rows li")).to_have_count(1)
        assert sent[-1]["content-digest"] == content_digest(body)
        assert (attachments / "data.bin").read_bytes() == body

        # A file that does not match its SHA-256 is refused, and nothing is saved.
        def wrong_digest(route: Route) -> None:
            headers = route.request.all_headers()
            headers["content-digest"] = content_digest(b"other bytes")
            route.continue_(headers=headers)

        page.route("**/attachments/damaged.bin", wrong_digest)
        picker.set_input_files(files=[binary_file("damaged.bin", body)])
        expect(page.locator(".toast.error")).to_contain_text("The file arrived damaged")
        expect(page.locator(".upload-row")).to_have_count(0)

        # Cancel while the SHA-256 is being computed: nothing is sent and no error appears.
        page.evaluate(
            """() => {
            document.getElementById("toasts").replaceChildren();
            const read = Blob.prototype.arrayBuffer;
            Blob.prototype.arrayBuffer = function () {
              return new Promise((done) => setTimeout(done, 400)).then(() => read.call(this));
            };
        }"""
        )
        uploads = len(sent)
        picker.set_input_files(files=[binary_file("cancelled.bin", body)])
        row = page.locator(".upload-row")
        expect(row).to_contain_text("Computing SHA-256…")
        row.get_by_role("button", name="Cancel").click()
        expect(row).to_have_count(0)
        page.wait_for_timeout(1500)  # Longer than the slowed-down hashing would take.
        assert len(sent) == uploads
        expect(page.locator(".toast.error")).to_have_count(0)

        # A browser too old to compute the SHA-256 says so and sends nothing.
        old = context.new_page()
        old.add_init_script(OLD_BROWSER)
        old_problems = watched(old)
        old_requests: list[str] = []
        old.on("request", lambda request: old_requests.append(request.method))
        old.goto(f"#/note/{note['id']}/edit")
        old.locator("input[type=file]").set_input_files(files=[binary_file("old.bin", body)])
        expect(old.locator(".toast.error")).to_contain_text("Your browser is not supported")
        expect(old.locator(".upload-row")).to_have_count(0)
        assert "PUT" not in old_requests

        assert sorted(path.name for path in attachments.iterdir()) == ["data.bin"]
        assert_clean(old, old_problems)
        assert_clean(page, problems)
        context.close()


# --- trusted proxy --------------------------------------------------------------------


def failed_login_status(server: Server, client_ip: str) -> int:
    request = urllib.request.Request(
        server.url + "api/login",
        data=json.dumps({"username": USERNAME, "password": "wrong password"}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Requested-With": "plainnotes",
            "X-Forwarded-For": client_ip,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_trusted_proxy_network_limits_each_client_separately(tmp_path: Path) -> None:
    # The README trusts Docker's bridge network (a CIDR range); here the proxy is 127.0.0.1.
    data = tmp_path / "data"
    data.mkdir()
    with running_server(data, tmp_path / "server.log", forwarded_allow_ips="127.0.0.0/8") as server:
        statuses = [failed_login_status(server, "203.0.113.7") for _ in range(6)]
        assert statuses == [401] * 5 + [429]
        assert failed_login_status(server, "198.51.100.9") == 401


# --- wikilinks ------------------------------------------------------------------------


def test_wikilinks(page: Page) -> None:
    target = create_note(page, unique("Wiki target"), "")
    missing = unique("Missing page")
    note = create_note(
        page,
        unique("Wiki source"),
        f"See [[{target['title'].upper()}]] and [[{missing}]].\n\n`[[not a link]]`",
    )
    open_markdown(page, note)
    links = page.locator(".markdown a.wikilink")
    expect(links).to_have_count(2)
    expect(links.first).to_have_text(target["title"].upper())
    expect(links.first).to_have_attribute("href", f"#/note/{target['id']}")
    expect(links.nth(1)).to_have_class("wikilink missing")
    expect(links.nth(1)).to_have_attribute(
        "href", "#/new?title=" + urllib.request.quote(missing, safe="")
    )
    expect(page.locator(".markdown code")).to_have_text("[[not a link]]")
    links.nth(1).click()
    expect(page.get_by_label("Title")).to_have_value(missing)
    page.keyboard.press("Control+s")
    expect(page.locator(".dirty-dot")).to_be_hidden()
    open_markdown(page, note)
    expect(page.locator(".markdown a.wikilink.missing")).to_have_count(0)
    links.first.click()
    expect(page.get_by_role("heading", name=target["title"])).to_be_visible()


def test_malformed_and_unknown_routes(page: Page) -> None:
    page.goto("#/note/%E0%A4%A")
    expect(page.get_by_role("heading", name="Not found")).to_be_visible()
    page.goto("#/no/such/page")
    expect(page.get_by_role("heading", name="Not found")).to_be_visible()
    page.get_by_role("link", name="Back to all notes").click()
    expect(page.get_by_role("heading", name="All notes")).to_be_visible()


def all_drafts(page, note_id):
    return page.evaluate(
        """async (id) => (await import("./static/js/drafts.js")).loadDrafts(id)""",
        note_id,
    )


def test_saving_other_tab_keeps_independent_draft(context, page):
    note = create_note(page, unique("Independent tabs"), "original")
    route = f"#/note/{note['id']}/edit"
    page.goto(route)
    other = context.new_page()
    other.goto(route)
    expect(page.get_by_label("Note text")).to_have_value("original")
    expect(other.get_by_label("Note text")).to_have_value("original")
    page.get_by_label("Note text").fill("A unsaved")
    wait_for_draft(page, note["id"])
    other.get_by_label("Note text").fill("B saved")
    other.keyboard.press("Control+s")
    expect(other.locator(".dirty-dot")).to_be_hidden()
    # "Saved" appears only after the saved text's drafts are removed.
    expect(other.locator(".toast").last).to_have_text("Saved")
    assert [d["content"] for d in all_drafts(other, note["id"])] == ["A unsaved"]
    page.close()
    other.close()
    recovered = context.new_page()
    recovered.goto(route)
    recovered.locator("dialog[open]").get_by_role("button", name="Restore", exact=True).click()
    expect(recovered.get_by_label("Note text")).to_have_value("A unsaved")
    recovered.keyboard.press("Control+s")
    expect(recovered.locator("dialog[open]")).to_contain_text("changed somewhere else")


def test_two_new_notes_have_distinct_drafts(context, page):
    page.goto("#/new")
    other = context.new_page()
    other.goto("#/new")
    expect(page.get_by_label("Note text")).to_be_visible()
    expect(other.get_by_label("Note text")).to_be_visible()
    page.get_by_label("Title").fill(unique("First"))
    page.get_by_label("Note text").fill("first draft")
    other.get_by_label("Title").fill(unique("Second"))
    other.get_by_label("Note text").fill("second draft")
    eventually(lambda: len(all_drafts(page, None)) == 2, "two independent new-note drafts missing")
    other.keyboard.press("Control+s")
    expect(other.locator(".dirty-dot")).to_be_hidden()
    expect(other.locator(".toast").last).to_have_text("Saved")
    assert [d["content"] for d in all_drafts(page, None)] == ["first draft"]


def test_large_draft_survives_reload(context, page):
    note = create_note(page, unique("Large draft"), "original")
    page.goto(f"#/note/{note['id']}/edit")
    text = "z" * (6 * 1024 * 1024)
    page.get_by_label("Note text").fill(text)
    assert wait_for_draft(page, note["id"])["content"] == text
    recovered, _ = reopen(context, page, f"#/note/{note['id']}/edit")
    recovered.locator("dialog[open]").get_by_role("button", name="Restore", exact=True).click()
    expect(recovered.get_by_label("Note text")).to_have_value(text)


def test_draft_storage_failure_is_visible_and_server_save_still_works(page, server):
    page.add_init_script("""IDBObjectStore.prototype.put = function() {
        throw new DOMException("Injected quota", "QuotaExceededError");
    };""")
    note = create_note(page, unique("Quota failure"), "original")
    page.goto(f"#/note/{note['id']}/edit")
    page.get_by_label("Note text").fill("must not silently lose this")
    expect(page.locator("#draft-warning")).to_be_visible()
    expect(page.locator("#draft-warning")).to_contain_text("could not be stored")
    page.keyboard.press("Control+s")
    expect(page.locator(".dirty-dot")).to_be_hidden()
    assert note_file(server, note).read_text() == "must not silently lose this"


def test_orphaned_draft_can_be_saved_as_new(context, page):
    note = create_note(page, unique("Orphan"), "original")
    route = f"#/note/{note['id']}/edit"
    page.goto(route)
    page.get_by_label("Note text").fill("recover this")
    wait_for_draft(page, note["id"])
    api(page, "DELETE", f"api/notes/{note['id']}")
    recovered, _ = reopen(context, page, route)
    recovered.locator("dialog[open]").get_by_role("button", name="Restore", exact=True).click()
    expect(recovered.get_by_label("Note text")).to_have_value("recover this")
    recovered.keyboard.press("Control+s")
    recovered.locator("dialog[open]").get_by_role("button", name="Save as new note").click()
    expect(recovered.locator(".dirty-dot")).to_be_hidden()
    # "Saved" appears only after the drafts kept under the old ID are removed.
    expect(recovered.locator(".toast").last).to_have_text("Saved")
    assert note["id"] not in recovered.url
    assert all_drafts(recovered, note["id"]) == []
    new_id = recovered.url.split("/note/")[1].split("/")[0]
    assert api(recovered, "GET", f"api/notes/{new_id}")["content"] == "recover this"


def test_logout_epoch_blocks_stale_writes(context, page):
    page.goto("#/")
    other = context.new_page()
    other.goto("#/")
    other.evaluate("""async () => {
        const drafts = await import("./static/js/drafts.js");
        window.auditOwner = await drafts.beginEditor();
        await drafts.saveDraft(null, {title: "draft", content: "text"}, window.auditOwner);
    }""")
    page.evaluate("""async () => (await import("./static/js/drafts.js")).removeAllDrafts()""")
    expect(other.get_by_label("Username")).to_be_visible()
    assert other.evaluate("""async () => {
        const drafts = await import("./static/js/drafts.js");
        try {
            const value = {title: "stale", content: "must not reappear"};
            await drafts.saveDraft(null, value, window.auditOwner);
            return false;
        } catch { return true; }
    }""")
    assert all_drafts(other, None) == []


def test_logout_still_works_when_draft_storage_is_unavailable(page, server):
    page.add_init_script("""indexedDB.open = function() {
        throw new DOMException("Storage unavailable", "SecurityError");
    };""")
    page.goto("#/")
    page.get_by_text("Menu").click()
    page.get_by_role("button", name="Log out", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(page.get_by_label("Username")).to_be_visible()
    assert page.request.get(server.url + "api/session").status == 401
    expect(page.locator(".toast.error")).to_contain_text("could not be erased")


def test_draft_installation_namespaces(page):
    page.goto("#/")
    result = page.evaluate("""async () => {
        const url = new URL("./static/js/drafts.js", location.href).href;
        const first = await import(url);
        const owner = await first.beginEditor();
        await first.saveDraft(null, {title: "root", content: "keep"}, owner);
        history.replaceState(null, "", "/second/");
        const second = await import(url + "?second-installation");
        const isolated = (await second.loadDrafts(null)).length === 0;
        await second.removeAllDrafts();
        const retained = (await first.loadDrafts(null))[0].content === "keep";
        history.replaceState(null, "", "/");
        return isolated && retained;
    }""")
    assert result


@pytest.mark.parametrize("content", ["a\rb\r", "a\r\nb\n", "a\nb\n"])
def test_title_only_edit_preserves_exact_content(page, server, content):
    note = create_note(page, unique("Raw bytes"), content)
    page.goto(f"#/note/{note['id']}/edit")
    title = note["title"] + " renamed"
    page.get_by_label("Title").fill(title)
    page.keyboard.press("Control+s")
    expect(page.locator(".dirty-dot")).to_be_hidden()
    assert (server.data / note["id"] / (title + ".md")).read_bytes() == content.encode()


def test_lone_cr_edits_and_draft_recovery_preserve_convention(context, page, server):
    note = create_note(page, unique("CR recovery"), "a\rb\r")
    route = f"#/note/{note['id']}/edit"
    page.goto(route)
    page.get_by_label("Note text").fill("a\nb\nc\n")
    wait_for_draft(page, note["id"])
    recovered, _ = reopen(context, page, route)
    recovered.locator("dialog[open]").get_by_role("button", name="Restore", exact=True).click()
    expect(recovered.get_by_label("Note text")).to_have_value("a\nb\nc\n")
    recovered.keyboard.press("Control+s")
    expect(recovered.locator(".dirty-dot")).to_be_hidden()
    assert note_file(server, note).read_bytes() == b"a\rb\rc\r"


def test_discarding_restored_copy_preserves_original_tab(context, page):
    note = create_note(page, unique("Original draft"), "server text")
    route = f"#/note/{note['id']}/edit"
    page.goto(route)
    page.get_by_label("Note text").fill("A has never saved this")
    wait_for_draft(page, note["id"])
    second = context.new_page()
    second.goto(route)
    second.locator("dialog[open]").get_by_role("button", name="Restore", exact=True).click()
    expect(second.get_by_label("Note text")).to_have_value("A has never saved this")
    eventually(lambda: len(all_drafts(page, note["id"])) == 2, "second draft missing")
    second.get_by_label("Note text").fill("B changed the restored copy")
    eventually(
        lambda: any(
            d["content"] == "B changed the restored copy" for d in all_drafts(page, note["id"])
        ),
        "B edit not persisted",
    )
    second.get_by_role("button", name="Close", exact=True).click()
    second.locator("dialog[open]").get_by_role("button", name="Discard", exact=True).click()
    expect(second.get_by_role("heading", name=note["title"], exact=True)).to_be_visible()
    expect(page.get_by_label("Note text")).to_have_value("A has never saved this")
    remaining = all_drafts(page, note["id"])
    assert any(d["content"] == "A has never saved this" for d in remaining), remaining
    page.close()
    recovered = context.new_page()
    recovered.goto(route)
    recovered.locator("dialog[open]").get_by_role("button", name="Restore", exact=True).click()
    expect(recovered.get_by_label("Note text")).to_have_value("A has never saved this")


def test_cancelled_logout_leaves_current_editor_recoverable(page):
    note = create_note(page, unique("Cancelled logout"), "server")
    page.goto(f"#/note/{note['id']}/edit")
    page.get_by_label("Note text").fill("still displayed after cancelling logout")
    wait_for_draft(page, note["id"])
    page.get_by_text("Menu", exact=True).click()
    page.get_by_role("button", name="Log out", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Discard", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_label("Note text")).to_have_value("still displayed after cancelling logout")
    assert all_drafts(page, note["id"]), "dirty editor retained but recovery copy removed"


def test_failed_logout_does_not_destroy_subsequent_edits(page):
    note = create_note(page, unique("Failed logout"), "server")
    page.goto(f"#/note/{note['id']}/edit")
    expect(page.get_by_label("Note text")).to_be_visible()
    page.route(
        "**/api/logout",
        lambda route: route.fulfill(
            status=500,
            content_type="application/json",
            body='{"code":"internal","detail":"Injected logout failure"}',
        ),
    )
    page.get_by_text("Menu", exact=True).click()
    page.get_by_role("button", name="Log out", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(page.locator(".toast.error")).to_contain_text("Injected logout failure")
    page.get_by_label("Note text").fill("new work AFTER the failed logout")
    page.wait_for_timeout(1000)
    assert page.locator("#note-content").count() == 1, {
        "url": page.url,
        "drafts": all_drafts(page, note["id"]),
        "session_status": page.request.get("api/session").status,
    }


@pytest.mark.parametrize("choice", ["Save", "Discard", "Cancel"])
def test_logout_choices_preserve_work_until_committed(page, choice):
    note = create_note(page, unique("Logout choices"), "server")
    page.goto(f"#/note/{note['id']}/edit")
    page.get_by_label("Note text").fill("unsaved choice text")
    wait_for_draft(page, note["id"])
    page.get_by_text("Menu", exact=True).click()
    page.get_by_role("button", name="Log out", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name=choice, exact=True).click()
    if choice != "Cancel":
        page.locator("dialog[open]").get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_label("Note text")).to_have_value("unsaved choice text")
    assert page.request.get("api/session").status == 200
    if choice == "Save":
        assert api(page, "GET", f"api/notes/{note['id']}")["content"] == "unsaved choice text"
    else:
        assert all_drafts(page, note["id"])[0]["content"] == "unsaved choice text"


def test_network_logout_failure_preserves_both_tabs_and_retry(context, page):
    note = create_note(page, unique("Network logout"), "server")
    route = f"#/note/{note['id']}/edit"
    page.goto(route)
    other = context.new_page()
    other.goto(route)
    expect(other.get_by_label("Note text")).to_be_visible()
    page.get_by_label("Note text").fill("first draft")
    other.get_by_label("Note text").fill("second draft")
    eventually(lambda: len(all_drafts(page, note["id"])) == 2, "both drafts missing")
    page.route("**/api/logout", lambda request: request.abort())
    page.get_by_text("Menu", exact=True).click()
    page.get_by_role("button", name="Log out", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Discard", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(page.locator(".toast.error")).to_contain_text("Cannot reach")
    page.get_by_label("Note text").fill("still editable")
    eventually(
        lambda: (
            {d["content"] for d in all_drafts(page, note["id"])}
            == {"still editable", "second draft"}
        ),
        "drafts not preserved after failure",
    )
    expect(other.get_by_label("Note text")).to_have_value("second draft")
    page.unroute("**/api/logout")
    page.get_by_text("Menu", exact=True).click()
    page.get_by_role("button", name="Log out", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Discard", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(page.get_by_label("Username")).to_be_visible()
    expect(other.get_by_label("Username")).to_be_visible()
    assert all_drafts(page, note["id"]) == []


def test_pending_logout_blocks_edits_and_recovers_on_failure(page):
    note = create_note(page, unique("Pending logout"), "server")
    route = f"#/note/{note['id']}/edit"
    page.goto(route)
    expect(page.get_by_label("Note text")).to_be_visible()
    held = []
    page.route("**/api/logout", lambda route: held.append(route))
    page.get_by_text("Menu", exact=True).click()
    page.get_by_role("button", name="Log out", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(page.locator("main")).to_have_js_property("inert", True)
    page.keyboard.type("should not be inserted")
    expect(page.get_by_label("Note text")).to_have_value("server")
    page.evaluate("location.hash = '#/tags'")
    expect(page).to_have_url(re.compile(re.escape(route) + "$"))
    assert len(held) == 1
    held[0].fulfill(
        status=500, content_type="application/json", body='{"code":"internal","detail":"Try again"}'
    )
    expect(page.locator("main")).to_have_js_property("inert", False)
    page.get_by_label("Note text").fill("recovered editing")
    assert wait_for_draft(page, note["id"])["content"] == "recovered editing"


def test_cross_tab_logout_closes_recovery_dialog(context, page):
    note = create_note(page, unique("Dialog logout"), "server")
    page.goto(f"#/note/{note['id']}/edit")
    page.get_by_label("Note text").fill("draft")
    wait_for_draft(page, note["id"])
    other = context.new_page()
    other.goto(f"#/note/{note['id']}/edit")
    expect(other.locator("dialog[open]")).to_be_visible()
    page.get_by_text("Menu", exact=True).click()
    page.get_by_role("button", name="Log out", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Discard", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(other.get_by_label("Username")).to_be_visible()
    expect(other.locator("dialog")).to_have_count(0)
    assert all_drafts(other, note["id"]) == []


def test_hung_logout_times_out_without_losing_drafts(page):
    page.clock.install()
    note = create_note(page, unique("Logout timeout"), "server")
    page.goto(f"#/note/{note['id']}/edit")
    expect(page.get_by_label("Note text")).to_be_visible()
    page.route("**/api/logout", lambda route: None)
    page.get_by_text("Menu", exact=True).click()
    page.get_by_role("button", name="Log out", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(page.locator("main")).to_have_js_property("inert", True)
    page.clock.fast_forward(16_000)
    expect(page.locator("main")).to_have_js_property("inert", False)
    expect(page.locator(".toast.error")).to_contain_text("Cannot reach")
    page.get_by_label("Note text").fill("after timeout")
    page.clock.fast_forward(300)
    assert wait_for_draft(page, note["id"])["content"] == "after timeout"


def test_late_save_response_does_not_update_logged_out_page(context, page):
    note = create_note(page, unique("Private editor title"), "original")
    path = f"api/notes/{note['id']}"
    page.goto(f"#/note/{note['id']}/edit")
    page.get_by_label("Note text").fill("saved just before logout")
    held = []

    def hold_response(route):
        held.append((route, route.fetch()))

    page.route("**/" + path, hold_response)
    page.keyboard.press("Control+s")

    def response_ready():
        page.wait_for_timeout(20)
        return held

    eventually(response_ready, "save response not captured")
    other = context.new_page()
    other.goto("#/")
    other.get_by_text("Menu", exact=True).click()
    other.get_by_role("button", name="Log out", exact=True).click()
    other.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(page.get_by_label("Username")).to_be_visible()
    held[0][0].fulfill(response=held[0][1])
    page.wait_for_timeout(300)
    expect(page).to_have_title("plainnotes")
    expect(page.locator(".toast").filter(has_text=re.compile("^Saved$"))).to_have_count(0)
    assert all_drafts(page, note["id"]) == []


def test_successful_local_logout_closes_late_dialog(page):
    page.goto("#/")
    held = []
    page.route("**/api/logout", lambda route: held.append(route))
    page.get_by_text("Menu", exact=True).click()
    page.get_by_role("button", name="Log out", exact=True).click()
    page.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(page.locator("main")).to_have_js_property("inert", True)
    page.evaluate("""async () => {
        const ui = await import("./static/js/ui.js");
        ui.showDialog({title: "Delayed recovery prompt", buttons: [{label: "Keep", value: null}]});
    }""")
    expect(page.locator("dialog[open]")).to_be_visible()
    held[0].fulfill(response=held[0].fetch())
    expect(page.get_by_label("Username")).to_be_visible()
    expect(page.locator("dialog")).to_have_count(0)


@pytest.mark.parametrize("status", [404, 409])
def test_late_save_error_does_not_open_dialog_after_logout(context, page, status):
    note = create_note(page, unique("Late error"), "original")
    page.goto(f"#/note/{note['id']}/edit")
    page.get_by_label("Note text").fill("pending")
    held = []
    page.route(f"**/api/notes/{note['id']}", lambda route: held.append(route))
    page.keyboard.press("Control+s")
    expect(page.get_by_role("button", name="Save", exact=False)).to_be_disabled()
    other = context.new_page()
    other.goto("#/")
    other.get_by_text("Menu", exact=True).click()
    other.get_by_role("button", name="Log out", exact=True).click()
    other.locator("dialog[open]").get_by_role("button", name="Log out", exact=True).click()
    expect(page.get_by_label("Username")).to_be_visible()
    body = {"code": "not_found", "detail": "Missing"}
    if status == 409:
        body = {"code": "revision_conflict", "detail": "Changed", "current": note}
    held[0].fulfill(status=status, content_type="application/json", body=json.dumps(body))
    page.wait_for_timeout(300)
    expect(page.locator("dialog")).to_have_count(0)
    expect(page).to_have_title("plainnotes")


def test_new_note_save_keeps_old_draft_if_migration_fails(page, server):
    title = unique("Migration")
    page.goto("#/new")
    page.get_by_label("Title").fill(title)
    page.get_by_label("Note text").fill("sent text")
    wait_for_draft(page, "new")
    held = []
    page.route("**/api/notes", lambda route: held.append((route, route.fetch())))
    page.keyboard.press("Control+s")
    expect(page.get_by_role("button", name="Save", exact=False)).to_be_disabled()
    page.get_by_label("Note text").fill("sent text plus newer unsaved text")
    eventually(
        lambda: (draft(page, "new") or {}).get("content") == "sent text plus newer unsaved text",
        "newest draft not written",
    )
    page.evaluate("""() => {
        const original = IDBObjectStore.prototype.put;
        window.restoreDraftWrites = () => { IDBObjectStore.prototype.put = original; };
        IDBObjectStore.prototype.put = function(value, ...args) {
            if (value.kind === "draft" && value.noteId !== null) {
                throw new DOMException("Injected migration quota", "QuotaExceededError");
            }
            return original.call(this, value, ...args);
        };
    }""")
    held[0][0].fulfill(response=held[0][1])
    expect(page.locator("#draft-warning")).to_be_visible()
    expect(page.get_by_role("button", name="Save", exact=False)).to_be_enabled()
    preserved = draft(page, "new")
    assert preserved and preserved["content"] == "sent text plus newer unsaved text"
    assert next(server.data.glob(f"*/{title}.md")).read_text() == "sent text"
    page.evaluate("window.restoreDraftWrites()")
    page.keyboard.press("Control+s")
    expect(page.locator(".dirty-dot")).to_be_hidden()
    wait_for_no_draft(page, "new")
    assert (
        next(server.data.glob(f"*/{title}.md")).read_text() == "sent text plus newer unsaved text"
    )
