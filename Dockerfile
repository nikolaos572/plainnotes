# Pinned by digest, so every build starts from the same image; docs/CONTRIBUTING.md explains how to update it.
FROM python:3.13.16-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAINNOTES_DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install --require-hashes -r requirements.txt
COPY app ./app
# Run as UID/GID 61001. That is above the range Linux gives to real accounts (up to 60000),
# so it matches no login on the host, such as uid 1000, which often has passwordless sudo.
# A host data folder must be owned by 61001 (see the README).
RUN mkdir /data && chown 61001:61001 /data

USER 61001:61001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]
CMD ["python", "-m", "app"]
