"""Browser tests: the real app in a uvicorn subprocess, driven by Playwright and Chromium."""

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
from playwright.sync_api import Browser, BrowserContext, Locator, Page, expect, sync_playwright

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
    data: Path


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
            yield Server(url, data)
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
    assert [p.name for p in (server.data / note_id).iterdir()] == [f"{renamed.upper()}.md"]
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
    value = page.evaluate(f"localStorage.getItem('plainnotes.draft.{key}')")
    return json.loads(value) if value else None


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
    second.evaluate("localStorage.setItem('plainnotes.draft.20260101-000000-zzzz', '{}')")
    second.get_by_text("Menu").click()
    second.get_by_role("button", name="Log out").click()
    second.locator("dialog[open]").get_by_role("button", name="Discard").click()
    expect(second.get_by_label("Username")).to_be_visible()
    keys = second.evaluate("Object.keys(localStorage)")
    assert not [key for key in keys if key.startswith("plainnotes.draft.")]
    assert_clean(second, problems)


# --- read-only mode and sub-path hosting ----------------------------------------------


def test_read_only_ui(browser: Browser, tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "20260101-000000-read").mkdir(parents=True)
    (data / "20260101-000000-read" / "Read me.md").write_text("# Hello")
    with running_server(data, tmp_path / "server.log", read_only="true") as running:
        context = browser.new_context(base_url=running.url)
        page = context.new_page()
        problems = watched(page)
        page.goto("")
        login_in_browser(page)
        expect(page.get_by_role("link", name="Read me")).to_be_visible()
        expect(page.get_by_role("banner").get_by_role("link", name="New note")).to_be_hidden()
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
