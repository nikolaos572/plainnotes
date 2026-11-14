# Configuration

Every setting, the data folder, running plainnotes behind HTTPS and fixing common problems. To install it, see the [README](../README.md).

## Environment variables

Everything is configured with environment variables. An invalid value stops the server with a message that names the variable.

| Variable | Default | Meaning |
|---|---|---|
| `PLAINNOTES_USERNAME` | required | Login name (case-sensitive) |
| `PLAINNOTES_PASSWORD` | required | Login password; a warning is logged if it is shorter than 12 characters |
| `PLAINNOTES_SECRET_KEY` | required | At least 32 characters; signs session cookies. Generate it with `openssl rand -hex 32` and keep it the same between restarts: changing it logs out every device |
| `PLAINNOTES_DATA_DIR` | `/data` | Data folder; must exist and be writable (by UID 61001 in Docker) unless read-only mode is enabled. plainnotes keeps the notes in its `notes/` folder and uses `testing/` for the [storage check](#the-storage-check) |
| `PLAINNOTES_HOST` | `0.0.0.0` | Listen address |
| `PLAINNOTES_PORT` | `8080` | Listen port (1–65535) |
| `PLAINNOTES_SESSION_DAYS` | `30` | How long a login lasts (1–365); sessions are renewed while you use them |
| `PLAINNOTES_COOKIE_SECURE` | `true` | Set to `false` only for testing over plain HTTP |
| `PLAINNOTES_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-For` header is trusted: comma-separated IP addresses or networks, such as `172.17.0.0/16` for a proxy in Docker on the same server; see [Running behind HTTPS](#running-behind-https) |
| `PLAINNOTES_MAX_UPLOAD_MB` | `0` | Largest upload in MiB; `0` means unlimited |
| `PLAINNOTES_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `PLAINNOTES_READ_ONLY` | `false` | Serve notes without allowing any change |
| `PLAINNOTES_BASE_PATH` | empty | Serve under a sub-path, e.g. `/notes` (starts with `/`, no trailing `/`); see [Hosting under a sub-path](#hosting-under-a-sub-path) |

Booleans accept `true/false/1/0/yes/no`.

## The data folder

The notes live in the data folder, `/data` inside the container. Create a folder on the host and give it to UID:GID 61001:61001 before the first start, then mount it at `/data`:

```bash
mkdir -p data
sudo chown 61001:61001 data   # the user the container runs as
docker run ... -v "$(pwd)/data:/data" ... plainnotes:latest
```

The container runs as UID:GID 61001:61001, not root. That number is above the range Linux gives to real accounts (up to 60000), so it matches no login on the host: if anything ever escaped the container, it would land as an ID that owns nothing there, instead of an account such as uid 1000, which often has passwordless sudo. No user with that number needs to exist on the host; just give the data folder to it before the first start, as above. If the data folder is not writable, `docker logs plainnotes` shows the `chown` command that fixes it. The files plainnotes writes belong to 61001 too, so your own account can read the notes but needs `sudo` to change them; see [Your data](guide.md#your-data), which also shows how the notes are laid out in the folder.

### The storage check

At every start plainnotes creates `notes/` and `testing/` in the data folder if they are missing, empties `testing/`, and checks there that the storage supports the atomic file operations safe saving needs. Local Linux filesystems such as ext4, XFS, btrfs and ZFS (OpenZFS 2.2 or later) do. If the storage does not (as with NFS or SMB shares, or ZFS 2.1 and older), plainnotes still starts, but read-only, and shows why on every page. It does the same when the disk is full or read-only at startup. Mount the whole data folder at once, so that the check runs on the same storage as your notes.

When the check passes, the log says so:

```text
INFO app: Storage check passed in /data/testing: the filesystem supports RENAME_EXCHANGE (atomic saves) and RENAME_NOREPLACE (new files never replace existing ones).
```

With `PLAINNOTES_READ_ONLY=true` nothing is written to the data folder, so the check does not run, and the log says it was skipped.

## Running behind HTTPS

For use beyond a local test, put plainnotes behind a reverse proxy that provides HTTPS. The command in the [README](../README.md#run-it) is meant for this. The example here uses Nginx Proxy Manager (NPM), with both containers on Docker's default bridge network. What the command's options do:

- `--restart unless-stopped` restarts plainnotes after a reboot.
- `--read-only --cap-drop ALL --security-opt no-new-privileges` lock the container down.
- `PLAINNOTES_COOKIE_SECURE` is left at its default, `true`, so browsers send the session cookie only over HTTPS.
- `PLAINNOTES_FORWARDED_ALLOW_IPS` names the proxy whose `X-Forwarded-For` header is trusted, so the login rate limit sees each visitor's real address instead of the proxy's. `172.17.0.0/16` is Docker's default bridge network: a proxy that runs in Docker on the same server connects from an address in it. If your proxy runs on another machine, use that machine's IP address instead.

Then add a proxy host in NPM:

- **Details:** your domain; scheme `http`; forward hostname/IP: the IP address of the server that runs plainnotes; forward port `8080`. (Containers on the default bridge network cannot reach each other by name.)
- **SSL:** request a certificate and enable **Force SSL** and **HSTS**.
- **Advanced:** paste this, so large uploads are streamed instead of buffered and slow transfers don't time out:

  ```nginx
  client_max_body_size 0;
  proxy_request_buffering off;
  proxy_read_timeout 3600s;
  proxy_send_timeout 3600s;
  ```

Port 8080 on the server also answers over plain HTTP, but browsers cannot log in there: the session cookie is sent only over HTTPS.

### Hosting under a sub-path

To host plainnotes under a sub-path such as `https://example.com/notes/`, set `PLAINNOTES_BASE_PATH=/notes` and add a **Custom location** `/notes` in the NPM proxy host that forwards to the same address and port without stripping the prefix (the same Advanced settings apply).

## Troubleshooting

### The log says the data directory is not writable

`docker logs plainnotes` shows `Data directory /data is not writable by uid 61001`, followed by the command that fixes it. The data folder on the host does not belong to UID 61001: give it to that user with `sudo chown -R 61001:61001 <your data folder>`, then start plainnotes again.

### Every page says "Saving is turned off"

plainnotes found at startup that it cannot save safely, so it shows your notes read-only. The message on the page says why:

- **The storage does not support the safe file operations**, as with NFS or SMB shares, or ZFS before OpenZFS 2.2: move the data folder to a local Linux filesystem such as ext4, XFS, btrfs or ZFS 2.2 or later.
- **The disk was full at startup:** free some space.
- **The storage is read-only:** check the disk and how it is mounted. To serve a read-only copy on purpose, set `PLAINNOTES_READ_ONLY=true` instead.

Then restart plainnotes. See [The storage check](#the-storage-check).

### The browser refused the login cookie

The login page says "Your password was accepted, but the browser refused the login cookie because this page uses plain HTTP." Browsers send the session cookie only over HTTPS. Open plainnotes through your [HTTPS reverse proxy](#running-behind-https). For a local test only, set `PLAINNOTES_COOKIE_SECURE=false`, which lets the browser log in over plain HTTP.
