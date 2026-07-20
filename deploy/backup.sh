#!/usr/bin/env bash
# =============================================================================
# backup.sh – Create a timestamped, compressed, checksummed backup of the
# SQLite database *and* all user-uploaded files (logos, stamps, expense scans).
#
# Each run produces two files in BACKUP_DIR:
#   faktury_<timestamp>.tar.gz        – gzip archive (faktury.db + uploads/)
#   faktury_<timestamp>.tar.gz.sha256 – SHA-256 checksum of the archive
#
# Scheduled weekly by faktury-backup.timer (systemd). Run on demand with:
#   sudo systemctl start faktury-backup.service
# or directly:
#   bash /weby/faktury.tmcm.cz/app/deploy/backup.sh
#
# Verify a backup later with:
#   cd /data3/BACKUPS/faktury_app && sha256sum -c faktury_<timestamp>.tar.gz.sha256
#
# Adjust the paths below if your layout differs.
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
DB_PATH="/weby/faktury.tmcm.cz/app/faktury.db"
UPLOADS_DIR="/weby/faktury.tmcm.cz/app/app/uploads"
BACKUP_DIR="/data3/BACKUPS/faktury_app"
KEEP_DAYS=90   # Delete backups older than this many days

# ── Checksum helpers ────────────────────────────────────────────────────────────
# Linux ships `sha256sum`; macOS/BSD ship `shasum -a 256`. Support both so the
# script runs identically in production and on a dev box.
if command -v sha256sum >/dev/null 2>&1; then
    sha256_write() { sha256sum "$1" > "$1.sha256"; }
    sha256_check() { sha256sum -c "$1"; }
elif command -v shasum >/dev/null 2>&1; then
    sha256_write() { shasum -a 256 "$1" > "$1.sha256"; }
    sha256_check() { shasum -a 256 -c "$1"; }
else
    echo "[ERROR] Neither sha256sum nor shasum found – cannot checksum." >&2
    exit 1
fi

# ── Validate ──────────────────────────────────────────────────────────────────
if [[ ! -f "$DB_PATH" ]]; then
    echo "[ERROR] Database not found at $DB_PATH – aborting." >&2
    exit 1
fi

# ── Create backup + staging directories ─────────────────────────────────────────
mkdir -p "$BACKUP_DIR"

# Staging dir holds the consistent DB snapshot; cleaned up on any exit.
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

# ── Snapshot the database ───────────────────────────────────────────────────────
# SQLite's ".backup" gives a hot, consistent copy – safe while the app runs
# (no need to stop Gunicorn).
sqlite3 "$DB_PATH" ".backup '$STAGING/faktury.db'"

if [[ ! -s "$STAGING/faktury.db" ]]; then
    echo "[ERROR] DB snapshot is empty – something went wrong." >&2
    exit 1
fi

# ── Build the compressed archive ────────────────────────────────────────────────
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
DEST="$BACKUP_DIR/faktury_${TIMESTAMP}.tar.gz"

# Always include the DB snapshot; include uploads/ only if present (a fresh
# install may have none yet). Multiple -C switches let one archive pull from
# two different roots while keeping clean top-level names (faktury.db, uploads/).
TAR_ARGS=(-C "$STAGING" faktury.db)
if [[ -d "$UPLOADS_DIR" ]]; then
    TAR_ARGS+=(-C "$(dirname "$UPLOADS_DIR")" "$(basename "$UPLOADS_DIR")")
else
    echo "[WARN] Uploads dir not found at $UPLOADS_DIR – archiving DB only."
fi

tar -czf "$DEST" "${TAR_ARGS[@]}"

if [[ ! -s "$DEST" ]]; then
    echo "[ERROR] Archive is empty – something went wrong." >&2
    exit 1
fi

# ── Checksum ────────────────────────────────────────────────────────────────────
# Store the checksum with just the basename so verification works from inside
# BACKUP_DIR regardless of where it was generated.
BASE=$(basename "$DEST")
( cd "$BACKUP_DIR" && sha256_write "$BASE" )

# Verify the archive integrity immediately (catches a corrupt write).
( cd "$BACKUP_DIR" && sha256_check "$BASE.sha256" >/dev/null )

BACKUP_SIZE=$(du -sh "$DEST" | cut -f1)
echo "[OK] $(date '+%Y-%m-%d %H:%M:%S') – Backup created: $DEST ($BACKUP_SIZE), checksum verified."

# ── Prune old backups ─────────────────────────────────────────────────────────
DELETED=$(find "$BACKUP_DIR" \( -name "faktury_*.tar.gz" -o -name "faktury_*.tar.gz.sha256" \) -mtime "+${KEEP_DAYS}" -print -delete | wc -l | tr -d ' ')
if [[ "$DELETED" -gt 0 ]]; then
    echo "[OK] Pruned $DELETED file(s) older than ${KEEP_DAYS} days."
fi
