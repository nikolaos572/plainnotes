"""Static files: headers and types, plus source checks that enforce the frontend rules."""

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_web import assert_security_headers

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
JS_FILES = sorted(STATIC.glob("js/**/*.js"))
VENDOR_SHA256 = {
    "markdown-it.min.js": "494ba53db1aac9f676317dda7062bd9545b5f0bbe3f12b0ab1ab4f813825cfb0",
    "purify.min.js": "cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31",
}
FORBIDDEN_JS = {
    "innerHTML": r"\binnerHTML\b",
    "outerHTML": r"\bouterHTML\b",
    "insertAdjacentHTML": r"\binsertAdjacentHTML\b",
    "document.write": r"\bdocument\.write",
    "eval": r"\beval\s*\(",
    "Function constructor": r"\bFunction\s*\(",
    "string timer": r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]",
    "on* property": r"\.on[a-z]+\s*=[^=]",
    "on* attribute": r"setAttribute\(\s*[\"'`]on",
    "style attribute": r"setAttribute\(\s*[\"'`]style",
    "contenteditable": r"contenteditable",
    "DOMParser": r"\bDOMParser\b",
    "createContextualFragment": r"createContextualFragment",
    "srcdoc": r"\bsrcdoc\b",
    "absolute URL": r"[\"'`]/(?:api|static)\b",
}


def test_js_files_exist() -> None:
    names = {path.relative_to(STATIC / "js").as_posix() for path in JS_FILES}
    assert {"theme.js", "main.js", "api.js", "dom.js", "icons.js", "ui.js", "markdown.js"} <= names
    assert {"views/list.js", "views/note.js", "views/editor.js", "views/login.js"} <= names


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_no_forbidden_apis(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    for name, pattern in FORBIDDEN_JS.items():
        assert not re.search(pattern, source), f"{name} in {path.name}"


def test_only_api_js_talks_to_the_server() -> None:
    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        if path.name != "api.js":
            assert not re.search(r"\bfetch\s*\(|XMLHttpRequest", source), path.name


def test_only_markdown_js_reads_vendor_globals() -> None:
    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        if path.name != "markdown.js":
            assert "markdownit" not in source and "DOMPurify" not in source, path.name


def test_every_icon_is_defined_and_used() -> None:
    source = (STATIC / "js" / "icons.js").read_text(encoding="utf-8")
    defined = set(re.findall(r'^  "?([a-z0-9-]+)"?: \[', source, re.MULTILINE))
    others = "\n".join(p.read_text(encoding="utf-8") for p in JS_FILES if p.name != "icons.js")
    assert set(re.findall(r'\bicon(?:\(|: )"([^"]+)"', others)) <= defined
    for name in defined:
        assert f'"{name}"' in others, f"icon {name} is never used"


def test_index_html_rules() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline script"
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
    assert not re.search(r"\sstyle\s*=|<style", html), "inline style"
    for url in re.findall(r"(?:src|href)=\"([^\"]+)\"", html):
        assert not url.startswith(("/", "http:", "https:")), url
    scripts = re.findall(r"<script[^>]*>", html)
    assert scripts == [
        '<script src="static/js/theme.js">',
        '<script src="static/vendor/markdown-it.min.js" defer>',
        '<script src="static/vendor/purify.min.js" defer>',
        '<script src="static/js/main.js" type="module">',
    ]


def test_css_has_no_external_resources() -> None:
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "@import" not in css
    assert "url(" not in css
    assert "@font-face" not in css


def test_vendored_files_are_unchanged() -> None:
    for name, digest in VENDOR_SHA256.items():
        data = (STATIC / "vendor" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, name
    assert sorted(p.name for p in (STATIC / "vendor").iterdir()) == sorted(VENDOR_SHA256)


def test_index_page(anon: TestClient) -> None:
    response = anon.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert "etag" in response.headers
    assert_security_headers(response)
    again = anon.get("/", headers={"If-None-Match": response.headers["etag"]})
    assert again.status_code == 304
    assert_security_headers(again)


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/js/main.js", "text/javascript; charset=utf-8"),
        ("/static/js/views/editor.js", "text/javascript; charset=utf-8"),
        ("/static/vendor/purify.min.js", "text/javascript; charset=utf-8"),
        ("/static/css/app.css", "text/css; charset=utf-8"),
        ("/static/favicon.svg", "image/svg+xml"),
        ("/static/index.html", "text/html; charset=utf-8"),
    ],
)
def test_static_files(anon: TestClient, path: str, content_type: str) -> None:
    response = anon.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.headers["cache-control"] == "no-cache"
    assert_security_headers(response)
    revalidated = anon.get(path, headers={"If-None-Match": response.headers["etag"]})
    assert revalidated.status_code == 304


@pytest.mark.parametrize(
    "path",
    [
        "/static/../main.py",
        "/static/%2e%2e/main.py",
        "/static/..%2fmain.py",
        "/static/js/../../main.py",
    ],
)
def test_static_traversal(anon: TestClient, path: str) -> None:
    response = anon.get(path)
    assert response.status_code == 404
    assert "create_app" not in response.text
