# =============================================================================
# Fakturace – container image (Gunicorn + Uvicorn workers, WeasyPrint)
#
# Runtime data lives outside the image, in bind mounts:
#   /data/faktury.db     SQLite database (DATABASE_URL below)
#   /app/app/uploads     user uploads (logos, stamps, expense scans)
#   /backups             output of deploy/backup.sh
# Secrets (SECRET_KEY, APP_ENV, …) come from an env file, as with systemd.
#
# Build and run with the compose file in the server's stack directory
# (/containers/faktury on sweetgum); see deploy/SERVER_SETUP.md for the
# non-Docker layout.
# =============================================================================
FROM python:3.13.18-slim-trixie

# WeasyPrint needs Pango/HarfBuzz and fonts; sqlite3 is used by backup.sh.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
        shared-mime-info fontconfig fonts-liberation fonts-dejavu-core \
        sqlite3 \
 && rm -rf /var/lib/apt/lists/*

# Dedicated non-root user with a fixed UID, so the host data dirs can be
# owned by it.
RUN groupadd --system --gid 10001 faktury \
 && useradd --system --uid 10001 --gid faktury --no-create-home \
        --home-dir /tmp --shell /usr/sbin/nologin faktury

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==26.0.0

COPY . .

ENV DATABASE_URL=sqlite:////data/faktury.db \
    DB_PATH=/data/faktury.db \
    UPLOADS_DIR=/app/app/uploads \
    BACKUP_DIR=/backups \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORWARDED_ALLOW_IPS=*

USER faktury
EXPOSE 8000

# 2 workers is ample for a single-user system; --timeout covers slow
# WeasyPrint PDF renders. FORWARDED_ALLOW_IPS=* trusts X-Forwarded-* from
# the reverse proxy – the port must only be published on localhost.
CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", \
     "--timeout", "120", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
