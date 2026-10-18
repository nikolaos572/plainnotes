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
RUN mkdir /data && chown 1000:1000 /data

USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]
CMD ["python", "-m", "app"]
