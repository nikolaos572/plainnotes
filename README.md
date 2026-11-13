# plainnotes

A self-hosted, single-user web app for writing and finding notes that are stored as plain text files.

![A note in Markdown view in the dark theme, with code blocks, tags and a task list](docs/screenshot.png)

## Features

- **Your notes are plain files.** Each note is one UTF-8 `.md` file in its own folder, saved byte for byte. There is no database and no index: edit the files with any editor or sync tool, and the changes show up on the next page load.
- **Write in plain text, read as Markdown.** A plain-text editor, a Text view that shows the file exactly as stored, and a Markdown view with a Copy button on every code block.
- **Find things again.** `#tags` anywhere in the text, `[[wikilinks]]` between notes, pinned notes, and search that ignores case and accents.
- **Attachments of any type and size:** upload them, drag and drop them, or paste a screenshot.
- **Recovery safeguards:** atomic saves with retained previous content, title/content conflict checks, trash with undo, and independent browser drafts. Keep backups too; [recovery details](docs/guide.md#recovering-previous-content) explain the limits.
- **Small and locked down:** one Docker image that runs as non-root with a read-only filesystem, a strict Content Security Policy, and nothing loaded from the internet.
- Light, dark and auto themes, keyboard shortcuts, printing, a read-only mode and hosting under a sub-path.

## Run it

```bash
docker build -t plainnotes .
mkdir -p data
sudo chown 61001:61001 data   # the user the container runs as
docker run -d --name plainnotes \
  --restart unless-stopped \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  -e PLAINNOTES_USERNAME=admin \
  -e PLAINNOTES_PASSWORD='choose-a-long-password' \
  -e PLAINNOTES_SECRET_KEY='paste-64-random-hex-characters-here' \
  -e PLAINNOTES_FORWARDED_ALLOW_IPS='172.17.0.0/16' \
  -v "$(pwd)/data:/data" \
  -p 8080:8080 \
  plainnotes:latest
```

Generate the secret key with `openssl rand -hex 32` and keep it the same between restarts: changing it logs out every device.

Then put a reverse proxy that provides HTTPS in front of port 8080, and open plainnotes at its address: browsers send the login cookie only over HTTPS. [Running behind HTTPS](docs/configuration.md#running-behind-https) shows how to set this up with Nginx Proxy Manager and explains each option above.

> **To try plainnotes before your HTTPS proxy is ready,** add `-e PLAINNOTES_COOKIE_SECURE=false` to the command and open <http://localhost:8080/>. It lets the browser log in over plain HTTP, so use it only for a test.

> **The data folder** has to belong to UID:GID 61001:61001, the user the container runs as, before the first start. That ID matches no account on the host, and no user with that number needs to exist. Keep the folder on a local Linux filesystem such as ext4, XFS, btrfs or ZFS 2.2 or later: on a network share, plainnotes starts read-only. See [The data folder](docs/configuration.md#the-data-folder).

## Security

Everything requires a login, failed logins are rate-limited, and nothing inside a note or an attachment can run code in your browser. [SECURITY.md](docs/SECURITY.md) has the details and explains how to report a vulnerability.

## Documentation

- [Configuration](docs/configuration.md): every environment variable, the data folder and its storage check, running behind HTTPS and fixing common problems.
- [User guide](docs/guide.md): your data, writing notes, tags and search, attachments and keyboard shortcuts.
- [Security](docs/SECURITY.md): how to report a vulnerability, the threat model, the controls, the audit and the third-party code.
- [Contributing](docs/CONTRIBUTING.md): running plainnotes from source, the tests and updating dependencies.

## License

MIT, © 2026 Nikolaos Baras; see [LICENSE](LICENSE). If you build on plainnotes, a link back to <https://github.com/nikolaos572/plainnotes> is appreciated.

plainnotes is built with [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [markdown-it](https://github.com/markdown-it/markdown-it), [DOMPurify](https://github.com/cure53/DOMPurify) and [Lucide](https://lucide.dev) icons; see [Third-party code](docs/SECURITY.md#third-party-code) for versions and licences.
