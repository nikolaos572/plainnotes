# Security

This document describes how to report a vulnerability, what plainnotes protects against, how, how each control is tested, which third-party code it contains, what the security audit found, and the risks that remain.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting for this repository (the **Security** tab → **Report a vulnerability**). Describe the problem, the affected version or commit, and how to reproduce it. You will get an answer as soon as possible, and a fix will be released before details are made public.

## Threat model

plainnotes is a single-user notes server. It runs as one container on a home server, behind a reverse proxy (such as Nginx Proxy Manager) that terminates HTTPS. The owner is trusted: they write the notes, upload the attachments and configure the server. Anyone else on the network or the internet is untrusted.

What we protect:

- **The notes and attachments** in the data folder: their confidentiality (only the logged-in owner can read them), their integrity (nothing is written except what the owner saves, byte for byte) and their availability (nothing is lost by a crash, a concurrent edit or a mistaken delete).
- **The owner's browser session**: nothing inside a note, a title, a tag, a file name or an uploaded file may run code in the logged-in app.
- **The host**: the server never reads or writes outside the data folder, and the container runs without privileges.

Main attackers we consider:

1. Someone on the internet who reaches the login page: password guessing, forged or stolen sessions, request forgery from another site, clickjacking, attempts to exhaust resources.
2. Hostile content that ends up in the data folder: a note or file pasted, downloaded or synced from elsewhere (HTML, SVG, scripts, crafted Markdown, crafted file names, symlinks).
3. The supply chain: a compromised dependency or CDN.

Out of scope: an attacker who already has shell access to the host or write access to the data folder (they can read everything anyway), and a compromised reverse proxy.

## Controls

| Threat | Controls in the code | How it is tested |
|---|---|---|
| Script inside a note (XSS) | markdown-it with `html: false`, `typographer: false` and linkify limited to `http(s)://` and `mailto:`; DOMPurify (`USE_PROFILES: {html: true}`, `style` forbidden, no `data-*`); URL filter after sanitizing (links: relative, `http:`, `https:`, `mailto:`; images: relative, `http(s):`, `data:image/…`); CSP `script-src 'self'` without `unsafe-inline` or `unsafe-eval`; all other UI text through `textContent`; search highlights are plain-text segments built into `<mark>` elements on the client | `tests/test_e2e.py`: the XSS corpus (script, `onerror`, SVG, iframe, `javascript:`/`vbscript:`/`data:text/html` links, autolinks, entities, tab/newline/percent-encoded variants, reference links, forms, mXSS) in note content, titles, tag positions, attachment names and search terms; asserts no JS dialog, no extra `script` element, no `on*` attribute, only `http(s):` URLs and no `securitypolicyviolation` event. `tests/test_frontend.py` fails the build on `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `Function(`, string timers, `on*` properties/attributes, `style` attributes, `DOMParser` and absolute URLs |
| Script inside an uploaded file | Inline display only for an allowlist of image, audio, video and plain-text extensions, always with a fixed `Content-Type` chosen by extension; everything else (including `html`, `svg`, `xml`, `pdf`, `js`, `json`) is `application/octet-stream` with `Content-Disposition: attachment`; `X-Content-Type-Options: nosniff`; `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` on every attachment response. An uploaded HTML or SVG file opened in the browser would otherwise run scripts inside the logged-in app. Because of `nosniff`, an uploaded `.js` or `.css` file can never be loaded as a script or stylesheet either | `tests/test_attachments.py::test_serving_headers` (per type), `tests/test_security.py::test_headers_on_every_response_type` |
| Reading or writing outside the data folder | One helper, `fsutil.safe_child()`, validates every name from user input (note ID, title, attachment name, trash ID) and joins it to a known folder; IDs must match strict regexes; `..`, `/`, `\`, NUL and surrogates are refused; symlinks are never followed (`lstat`, `follow_symlinks=False`, `O_NOFOLLOW`); trash deletion uses `shutil.rmtree` only on real directories directly inside `.trash/` | `tests/test_security.py`: traversal corpus (`..`, `%2e%2e`, `%2f`, `%5c`, absolute paths, NUL bytes, overlong names, double encoding, overlong UTF-8) against note IDs, attachment names and trash IDs, through the test client and as raw request lines; symlinks pointing outside for note folders, `.md` files, loose files, attachments, `attachments/`, trash entries and `.trash/`; `tests/test_frontend.py::test_static_traversal` |
| Password guessing | Credentials compared as SHA-256 digests with `hmac.compare_digest`, both always checked; per-IP limit (5 failures in 15 minutes) and a global limit (50 in 15 minutes), `429` with `Retry-After`, credentials not checked while limited; 1 s delay after each failure; at most 10,000 IPs tracked | `tests/test_auth.py` (limiter unit tests with a fake clock, login API, `Retry-After`, memory bound, no credentials in the log) |
| Forged or stolen sessions | Stateless `v1.<expires>.<nonce>.<signature>` tokens signed with HMAC-SHA256; the signing key is derived from the secret key, username and password, so changing any of them ends every session; cookie `HttpOnly`, `Secure` (unless explicitly disabled for plain-HTTP testing), `SameSite=Strict`, `Path=<base>/` | `tests/test_auth.py` (valid, expired, tampered in every field, wrong version, other secret, changed password or username, renewal, logout) |
| Cross-site request forgery | `X-Requested-With: plainnotes` required on every `POST`, `PUT`, `PATCH` and `DELETE`, login and logout included; `SameSite=Strict`; no CORS headers; JSON bodies only; `GET` requests never change state (except the drop-in import and cookie renewal) | `tests/test_auth.py::test_every_mutation_requires_the_csrf_header` (walks every route), `test_no_cors_headers` |
| Clickjacking | `frame-ancestors 'none'` and `X-Frame-Options: DENY` on every response | `tests/test_security.py::test_headers_on_every_response_type` |
| Denial of service | 32 MiB body limit counted on the bytes received (uploads have their own optional limit and stream to disk with constant memory); 10 MiB note limit; queries ≤ 500 characters and 20 terms; linear-time regexes (possessive/atomic tag patterns, no backtracking constructs); bounded rate-limiter memory | `tests/test_api_notes.py::test_body_limit`, `test_content_too_large`, `tests/test_attachments.py` (50 MB streamed upload with bounded memory, limits), `tests/test_text.py::test_tag_regex_is_fast_on_pathological_input`, `tests/test_search.py::test_parse_limits` |
| Information leaks | `/docs`, `/redoc` and `/openapi.json` disabled; FastAPI's built-in OpenTelemetry is switched off; generic `500` bodies (traceback only in the server log); validation errors report field names, never values; no paths or tracebacks in responses; no passwords, cookies, note content or search queries in logs (access log off); `Cache-Control: no-store` on the API; `Referrer-Policy: no-referrer` | `tests/test_web.py` (docs 404, generic 500, validation errors), `tests/test_security.py::test_no_secrets_in_logs`, `tests/test_api_search.py::test_search_terms_are_not_logged` |
| Data loss | Atomic writes (temp file, `fsync`, `os.replace`, folder `fsync`); notes created in a temp folder and renamed into place; one lock around every change; revision (SHA-256) check on every save; delete moves to the trash; drafts kept in the browser | `tests/test_fsutil.py` (failure before `os.replace` keeps the original), `tests/test_api_notes.py::test_revision_conflict`, `tests/test_trash.py`, browser conflict and draft tests |
| Supply chain | Two direct Python dependencies (`fastapi`, `uvicorn`), every runtime package pinned with `--hash`; the Docker base image pinned by digest; two vendored JavaScript files, unmodified, with SHA-256 recorded under [Third-party code](#third-party-code) and checked by a test; no CDN, no web fonts, no outbound connections | `pip install --require-hashes` in the Dockerfile; `tests/test_frontend.py::test_vendored_files_are_unchanged` |
| Container | Runs as UID:GID 1000:1000; nothing installed beyond the Python packages; no `VOLUME`; works with `--read-only --cap-drop ALL --security-opt no-new-privileges` and needs no tmpfs; healthcheck in Python without curl | `tests/test_docker.py` |

Security headers on every response (`web.py`): `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; media-src 'self'; connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'` (replaced by the sandbox policy on attachments), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin`, `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()`, and no `Server` header. HSTS is set by the reverse proxy.

## Third-party code

The frontend uses two libraries, committed unmodified in `app/static/vendor/` and downloaded once from the npm registry:

| File | Library | Version | License | Source | SHA-256 |
|---|---|---|---|---|---|
| `markdown-it.min.js` | markdown-it | 14.3.3 | MIT | <https://registry.npmjs.org/markdown-it/-/markdown-it-14.3.3.tgz> (`package/dist/markdown-it.min.js`) | `494ba53db1aac9f676317dda7062bd9545b5f0bbe3f12b0ab1ab4f813825cfb0` |
| `purify.min.js` | DOMPurify | 3.4.17 | Apache-2.0 or MPL-2.0 | <https://registry.npmjs.org/dompurify/-/dompurify-3.4.17.tgz> (`package/dist/purify.min.js`) | `cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31` |

The button icons in `app/static/js/icons.js` are copied unchanged from [Lucide](https://lucide.dev) 1.59.0 (ISC; the icons Lucide took over from Feather are MIT), from <https://registry.npmjs.org/lucide-static/-/lucide-static-1.59.0.tgz>. Only the shapes of the icons in use are copied, together with both licence notices. They are drawing data, not code.

The server uses [FastAPI](https://fastapi.tiangolo.com/) (MIT) and [Uvicorn](https://www.uvicorn.org/) (BSD-3-Clause) and their dependencies, listed with hashes in `requirements.txt`.

## Audit (2026-10-18)

Everything below is automated and part of the test suite (run locally or with the "Run Test Suite" workflow on GitHub), except the dependency checks, which were run by hand.

1. **Route coverage.** `tests/test_auth.py::test_every_route_requires_a_session` walks all routes of the app (including included routers, through `fastapi.routing.iter_route_contexts`) and checks that each one except login, health and the index page returns `401` without a session; static files are a mount and not part of the walk. A route added later cannot ship unprotected. The same walk checks the CSRF header on every mutation, and `tests/test_read_only_and_base_path.py` checks read-only mode on every mutation.
2. **XSS corpus.** See the controls table. All payloads are neutralized before the CSP has to step in: the browser tests fail on any CSP violation report. While writing the tests, Playwright's own `wait_for_function` (which polls with `eval`) was blocked by the CSP, which confirms the policy is enforced.
3. **Path traversal corpus.** See the controls table. Literal `.` and `..` segments are removed by most HTTP clients, so they are also sent as raw request lines.
4. **Code review.** Searching the frontend for `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `Function(`, string-based timers and inline `on*=` attributes gives no hits; `tests/test_frontend.py` keeps it that way. HTML is parsed from note content only inside DOMPurify. `index.html` has no inline scripts, styles or handlers.
5. **Headers.** Checked for HTML, static files, `304` revalidation, API JSON, API errors, inline and downloaded attachments, health, `404` and the base-path redirect (`tests/test_security.py::test_headers_on_every_response_type`).
6. **Dependencies.** `pip-audit -r requirements.txt --require-hashes` (pip-audit 2.10.1, run with a throwaway virtualenv): no known vulnerabilities. Vendored JavaScript checked against the GitHub Advisory Database: markdown-it 14.3.3 is not affected by any published advisory (the most recent, GHSA-253c-mchw-3w2r about quadratic linkify paths, was fixed in 14.3.1); DOMPurify 3.4.17 is not affected by any published advisory (all were fixed in 3.4.16 or earlier, and most concern the `IN_PLACE`, `SAFE_FOR_TEMPLATES`, `CUSTOM_ELEMENT_HANDLING` and hook features, which plainnotes does not use). Before vendoring, both tarballs were checked against the registry's integrity hashes and diffed against the previous patch release.
7. **Container.** The image runs as UID 1000 (checked with `docker exec … id -u` and by the owner of files written to the host folder) and works with `--read-only --cap-drop ALL --security-opt no-new-privileges` without a tmpfs (`tests/test_docker.py`).

### Findings and fixes

| # | Finding | Fix |
|---|---|---|
| 1 | Restoring from the trash built the destination path from the note ID inside the user-supplied trash ID by plain path joining. The trash ID was already validated by a strict regex, so this was not exploitable, but it bypassed the single helper that every path built from user input must go through. | The destination now goes through `fsutil.safe_child()`. |
| 2 | `fsutil.InvalidName` carried the rejected name; had it ever reached the error handler, the `404` body would have echoed the user's input. | It is raised without arguments, so the response is always the generic `Not found.` |
| 3 | FastAPI 0.143 includes OpenTelemetry instrumentation that can add OTLP exporters from environment variables (`FASTAPI_OTEL_AUTO_CONFIGURE`, `OTEL_EXPORTER_OTLP_ENDPOINT`). | `create_app()` turns tracing, metrics, logs, operation spans and auto-configuration off explicitly. The transitive `opentelemetry-api` package is a no-op without an SDK and makes no network connections. |
| 4 | markdown-it's linkify also recognises `ftp:` and protocol-relative `//host` text by default; `//host` would have become an `https:` link. | Both schemas are removed, so only `http(s)://` and `mailto:` text becomes a link. |
| 5 | A malformed `%`-escape in the URL hash made the router throw, leaving a blank page. | Route parameters are decoded safely; the page shows "Not found" (browser test). |
| 6 | A FIFO named `*.md` in the data folder would block a server thread on `open`. | Files are opened with `O_NONBLOCK` and `O_NOFOLLOW`, then checked with `fstat` to be regular files. |
| 7 | Python maps undecodable file-name bytes to lone surrogates (`surrogateescape`): such names caused `500`s when returned as JSON, and a title containing lone surrogates could have created a non-UTF-8 file name. | Titles with surrogates are invalid; attachment names replace them with `_`; note and attachment names on disk that are not UTF-8 are skipped (with a one-time warning). |
| 8 | A hand-made folder name matching the ID pattern but holding an impossible date (month 13) caused a `500` on every list. | The creation time falls back to the file's mtime. |
| 9 | A logout request whose session was due for renewal could have received a fresh session cookie after the expiring one. | The renewal cookie is only added when the response sets no cookie itself (test). |
| 10 | If the final `os.replace` of an upload failed, the reserved empty file stayed behind. | It is removed. |

### Residual risks and accepted trade-offs

- **Notes are not encrypted at rest.** Anyone who can read the data folder or its backups can read every note. Use disk encryption on the host if that matters.
- **A copied session cookie stays valid until it expires** (`PLAINNOTES_SESSION_DAYS`, 30 days by default). Tokens are stateless, so logging out only removes the cookie from that browser. Changing `PLAINNOTES_SECRET_KEY` (or the username or password) ends every session at once.
- **`PLAINNOTES_FORWARDED_ALLOW_IPS=*` is safe only when the container is reachable solely through the reverse proxy.** If the port is published, anyone can fake `X-Forwarded-For` and get a fresh per-IP allowance for each guess; the global limit (50 failures per 15 minutes) still applies. With the default `127.0.0.1` behind a reverse proxy, all clients share the proxy's address for the per-IP limit, so set it to the proxy's address or network, as the README describes (`172.17.0.0/16` for a proxy in Docker on the same server). Trusting a network trusts every container on it. The counters live in memory only, so restarting the container clears a lockout.
- **Remote images reveal the viewer's IP address.** `https:` images inside notes are allowed on purpose (the owner writes the notes); viewing such a note in Markdown view makes the browser fetch the image from its host. `Referrer-Policy: no-referrer` keeps the note's URL private.
- **The global login limit can delay the owner's own login during an attack.** After 50 failed logins from anywhere in 15 minutes, every login waits until the window passes. Existing sessions keep working.
- **Attachment serving checks the file with `lstat` and then opens it by name** (Starlette's `FileResponse`). Swapping the file for a symlink in between requires write access to the data folder, which is outside the threat model.
- **The password is configured in an environment variable**, visible to anyone who can inspect the container (`docker inspect`) or read the command that started it.
- **Uploads are unlimited by default** (`PLAINNOTES_MAX_UPLOAD_MB=0`). Only the logged-in owner can upload, but a stolen session could fill the disk.
- **Requests with a wrong method get `405` before authentication**, which confirms that the path exists. The route list is public in this repository anyway.
- **Media files allowed inline** (images, audio, video, plain text) are rendered by the browser's own viewers under the sandbox policy; a crafted file could still target a bug in the browser's decoders.
- **Table column alignment is not shown** in Markdown view, because markdown-it expresses it with `style` attributes, which are forbidden.
