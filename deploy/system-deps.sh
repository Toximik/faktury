#!/usr/bin/env bash
# =============================================================================
# system-deps.sh – Install all OS-level dependencies for the Fakturace app.
#
# Run as root (or via sudo) on a **fresh Ubuntu 22.04 / 24.04** server:
#
#   sudo bash deploy/system-deps.sh
#
# What it installs
# ----------------
#   1. Python 3.12 (via deadsnakes PPA on Ubuntu 22.04; built-in on 24.04)
#   2. venv / pip tooling
#   3. WeasyPrint native libraries  (Pango, Cairo, Fontconfig, GDK-Pixbuf)
#   4. Gunicorn (via pip inside venv, NOT system pip)
#   5. Nginx
#   6. Certbot + Nginx plugin for Let's Encrypt SSL
#   7. A dedicated system user 'faktury' to run the service
# =============================================================================

set -euo pipefail

echo "==> Updating package lists …"
apt-get update -qq

# ── 1. Python (3.11+) ────────────────────────────────────────────────────────
# The app needs Python >= 3.11 (it uses `X | None` / `list[...]` syntax, no
# 3.12-only features). We prefer whatever the distro ships:
#   • Debian 13 (trixie)  → python3 is 3.13   ── use it directly
#   • Debian 12 (bookworm)→ python3 is 3.11   ── use it directly
#   • Ubuntu 24.04        → python3 is 3.12    ── use it directly
#   • Ubuntu 22.04        → python3 is 3.10    ── pull 3.12 from deadsnakes (Ubuntu-only PPA)
# NOTE: the deadsnakes PPA lives on Launchpad and exists ONLY for Ubuntu; on
# Debian `add-apt-repository ppa:…` fails ('NoneType' has no attribute 'people').
. /etc/os-release   # sets $ID (debian|ubuntu) and $VERSION_ID

if command -v python3.12 &>/dev/null; then
    PYTHON=python3.12
    echo "==> Using existing $($PYTHON --version)."
elif [ "$ID" = "ubuntu" ] && [ "${VERSION_ID%%.*}" -lt 24 ]; then
    echo "==> Ubuntu <24.04 detected – adding deadsnakes PPA for Python 3.12 …"
    apt-get install -y software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y python3.12 python3.12-venv python3.12-dev
    PYTHON=python3.12
else
    # Debian, or Ubuntu >= 24.04 – use the distro's own python3.
    PYTHON=python3
    echo "==> Using distro Python: $(python3 --version) ($ID $VERSION_ID)."
fi

# venv/dev headers + bootstrap pip for whichever interpreter we settled on.
apt-get install -y \
    "${PYTHON}-venv" \
    "${PYTHON}-dev" \
    python3-pip     # only used to bootstrap; app uses venv pip

# Fail fast if we somehow ended up below the minimum.
"$PYTHON" - <<'PY'
import sys
assert sys.version_info >= (3, 11), f"Python >= 3.11 required, found {sys.version.split()[0]}"
print(f"==> Python OK: {sys.version.split()[0]}")
PY

# ── 2. Build tools (needed by some pip wheels) ───────────────────────────────
apt-get install -y build-essential libffi-dev libssl-dev

# sqlite3 CLI – used by deploy/backup.sh for a hot, consistent DB snapshot
# (`.backup`). The app itself uses Python's built-in sqlite3, but the backup
# script shells out to the standalone CLI, which is not installed by default.
apt-get install -y sqlite3

# ── 3. WeasyPrint native dependencies ────────────────────────────────────────
# WeasyPrint 60+ uses the system Pango/Cairo directly; no Python-level wrappers.
# See: https://doc.courtbouillon.org/weasyprint/stable/first_steps.html
apt-get install -y \
    libpango-1.0-0      \
    libpangocairo-1.0-0 \
    libcairo2           \
    libgdk-pixbuf-2.0-0 \
    libffi8             \
    gir1.2-pango-1.0    \
    shared-mime-info    \
    fonts-liberation    \
    fonts-dejavu-core   \
    fontconfig

# Refresh font cache so WeasyPrint can find the installed fonts
fc-cache -f -v

# ── 4. Nginx ─────────────────────────────────────────────────────────────────
apt-get install -y nginx

# ── 5. Certbot (Let's Encrypt SSL) ───────────────────────────────────────────
# Omit this block if you handle SSL at a firewall/load-balancer level instead.
apt-get install -y certbot python3-certbot-nginx

# ── 6. Gunicorn is installed inside the venv, NOT system-wide ────────────────
#    (see deploy/README or the Phase 5 checklist for venv setup instructions)

# ── 7. Dedicated system user ─────────────────────────────────────────────────
if id faktury &>/dev/null; then
    echo "==> System user 'faktury' already exists – skipping."
else
    echo "==> Creating system user 'faktury' …"
    useradd \
        --system \
        --no-create-home \
        --shell /usr/sbin/nologin \
        --comment "Fakturace application user" \
        faktury
fi

echo ""
echo "==> System dependencies installed successfully."
echo "    Next: copy your project to /srv/faktury and run the venv setup."
