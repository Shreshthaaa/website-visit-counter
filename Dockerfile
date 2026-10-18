# syntax=docker/dockerfile:1.7

# ---------- build stage ----------
# Only the virtualenv is copied into the final image.
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install -r requirements.txt


# ---------- runtime stage ----------
FROM python:3.11-slim AS runtime

ARG GIT_SHA=unknown
ARG APP_VERSION=0.0.0-dev
ARG BUILD_TIME=unknown

# Also set as env vars below so /version reports them.
LABEL org.opencontainers.image.title="visit-counter" \
      org.opencontainers.image.source="https://github.com/Shreshthaaa/website-visit-counter" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.created="${BUILD_TIME}"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_SHA=${GIT_SHA} \
    APP_VERSION=${APP_VERSION}

# Fixed uid/gid to match runAsUser in the Helm chart.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app app ./app

# pip, setuptools and wheel are build tooling; nothing is installed at
# runtime. Removing them also removes the packages setuptools vendors, which
# accounted for 4 of the 7 HIGH findings in the first image scan.
RUN for d in /opt/venv/lib/python3.11/site-packages \
             /usr/local/lib/python3.11/site-packages; do \
      [ -d "$d" ] || continue; \
      rm -rf "$d/pip" "$d/setuptools" "$d/wheel" "$d/pkg_resources" \
             "$d/_distutils_hack" "$d/distutils-precedence.pth"; \
      find "$d" -maxdepth 1 \( -name 'pip-*' -o -name 'setuptools-*' \
           -o -name 'wheel-*' -o -name 'pkg_resources-*' \) -exec rm -rf {} + ; \
    done

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
