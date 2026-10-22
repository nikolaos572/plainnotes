# Contributing

How to run plainnotes from source, test it and update its dependencies.

## Running from source

Python 3.13 is needed:

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pip install -r requirements-dev.txt
mkdir -p /tmp/plainnotes-data
PLAINNOTES_USERNAME=admin PLAINNOTES_PASSWORD=devpassword123 \
  PLAINNOTES_SECRET_KEY=dev-only-secret-key-0123456789abcdef \
  PLAINNOTES_DATA_DIR=/tmp/plainnotes-data PLAINNOTES_COOKIE_SECURE=false \
  python -m app
```

Then open <http://localhost:8080/>. The frontend is plain JavaScript modules in `app/static/` with no build step; reload the page to see a change.

## Tests and linting

```bash
python -m pytest -m "not e2e and not docker"      # unit and API tests (seconds)
python -m playwright install --with-deps chromium  # once, for the browser tests
python -m pytest -m e2e                            # browser tests (Playwright + Chromium)
python -m pytest -m docker                         # builds the image and runs it (needs Docker)
ruff check . && ruff format --check .              # ruff format . fixes the formatting
```

`tests/test_frontend.py` also enforces the frontend's safety rules: no `innerHTML`, `eval`, inline event handlers, `style` attributes or absolute URLs. [SECURITY.md](SECURITY.md) lists them all.

Two GitHub Actions workflows run only when started by hand from the **Actions** tab: **Run Test Suite** (lint and every test, including the browser and Docker tests) and **Build and Export Docker Image**, which offers the image as a download for one day. Load it with `gunzip -c plainnotes.tar.gz | docker load`.

## Updating dependencies

Every dependency is updated by hand:

- **GitHub Actions:** the workflows in `.github/workflows/` name each action by its major version (`actions/checkout@v7`). To update one, set the number after `@` to the action's latest major release.
- **Docker base image:** the `FROM` line in the `Dockerfile` pins `python:3.13-slim` by digest, so a rebuild always starts from the same image. To update it, run `docker buildx imagetools inspect python:3.13-slim`, copy the `Digest:` value, and set the version in the tag to what `docker run --rm python:3.13-slim python --version` prints.
- **Python packages:** `requirements.txt` pins every runtime package with hashes. Change nothing by hand: run the `uv pip compile` command shown at the top of that file. Development tools are listed in `requirements-dev.txt`.
- **JavaScript libraries:** `app/static/vendor/` holds markdown-it and DOMPurify, unmodified, from the npm registry. To update one, download the new tarball, check it against the registry's integrity hash, copy the file from `package/dist/`, and update its version and SHA-256 in [SECURITY.md](SECURITY.md#third-party-code) and in `VENDOR_SHA256` in `tests/test_frontend.py`.
- **Icons:** `app/static/js/icons.js` holds the [Lucide](https://lucide.dev) icons in use, copied from the SVG files of the `lucide-static` npm package (`package/icons/<name>.svg`). To add one, copy its shapes into `ICONS` and call `icon("name")`. A test checks that every icon used exists and that every icon defined is used.
