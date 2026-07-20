"""
config.py – Application-wide settings loaded from environment variables.

Deployment: set these via an EnvironmentFile in the systemd unit, or
export them in your shell before running the app in development.

NEVER commit secrets to version control.
"""

from __future__ import annotations

import os
import secrets
import sys


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# ── Environment detection ─────────────────────────────────────────────────────

IS_PRODUCTION: bool = _env("APP_ENV").lower() == "production"


# ── JWT ───────────────────────────────────────────────────────────────────────

SECRET_KEY: str = _env("SECRET_KEY")
if not SECRET_KEY:
    if IS_PRODUCTION:
        print(
            "[FATAL] SECRET_KEY environment variable is not set. "
            "Refusing to start in production mode.",
            file=sys.stderr,
        )
        sys.exit(1)
    # Development fallback: random key per process. Sessions are invalidated on
    # every restart, which is acceptable in a dev environment.
    SECRET_KEY = secrets.token_hex(32)

ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(_env("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))  # 8 h


# ── Password policy ─────────────────────────────────────────────────────────
# Minimum length enforced everywhere a password is set (bootstrap, admin-created
# users, self-service change). bcrypt silently truncates input beyond 72 bytes,
# so we reject longer inputs rather than give a false sense of strength.
MIN_PASSWORD_LENGTH: int = 12
MAX_PASSWORD_BYTES: int = 72


# ── Invoicing defaults ──────────────────────────────────────────────────────
# Fallback payment term (splatnost) in days, used when a contact has no
# per-client term set. Drives the default invoice due date (issue date + N).
DEFAULT_PAYMENT_DUE_DAYS: int = 14


# ── Invoice brand colour ──────────────────────────────────────────────────────
# The accent colour used on the PDF (header rule, title, table header, totals
# band). User-selectable from a curated palette. Every colour below is dark
# enough that the white text on the coloured bands stays readable, so no
# contrast computation is needed. Colour carries no accounting information, so
# it is read live at render time (never frozen into the invoice snapshot).
# Default is the subtle graphite; vivid colours are opt-in via the picker.
DEFAULT_BRAND_COLOR: str = "#1F2937"

# (hex, Czech label) – order is the display order in the picker. The subtle
# graphite default leads; vivid colours (incl. purple) come last, opt-in.
BRAND_COLOR_PALETTE: list[tuple[str, str]] = [
    ("#1F2937", "Grafitová"),
    ("#1D4ED8", "Modrá"),
    ("#0F766E", "Tyrkysová"),
    ("#047857", "Zelená"),
    ("#B91C1C", "Červená"),
    ("#C2410C", "Oranžová"),
    ("#9D174D", "Purpurová"),
    ("#3E0097", "Fialová"),
]

# Allowed values for server-side validation (the colour is interpolated into
# CSS, so only these exact values are accepted). Upper-cased for comparison.
BRAND_COLOR_CHOICES: frozenset[str] = frozenset(hex_.upper() for hex_, _ in BRAND_COLOR_PALETTE)


# ── Tax helper (Daňové přiznání) ──────────────────────────────────────────────
# Constants used by the /dane page to estimate the income-tax liability of an
# OSVČ. These change rarely but warrant a yearly review. The per-year "rozhodná
# částka" for participation in důchodové (social) pojištění on a secondary
# activity is NOT here — it changes every year and is admin-managed in the
# TaxSetting DB table.
#
# Income-tax rate (daň z příjmů FO, §16). Kept as a string so it converts
# cleanly to Decimal in logic.py.
INCOME_TAX_RATE: str = "0.15"

# Sociální (důchodové) pojištění OSVČ. The assessment base (vyměřovací základ)
# is a fraction of the tax base, and the premium is a rate of that base.
#   - SOCIAL_ASSESSMENT_RATE: 55 % of the daňový základ since 2024 (was 50 %).
#   - SOCIAL_INSURANCE_RATE:  29.2 % (28 % důchodové + 1.2 % st. politika
#     zaměstnanosti). Review yearly. Sleva na poplatníka is intentionally NOT
#     modelled: at vedlejší činnost it is claimed against the main employment.
SOCIAL_ASSESSMENT_RATE: str = "0.55"
SOCIAL_INSURANCE_RATE: str = "0.292"

# Zdravotní pojištění OSVČ. The assessment base is 50 % of the daňový základ
# (unchanged by the 2024 reform, which raised only the social base to 55 %) and
# the premium rate is 13.5 %. Health insurance is owed on any profit for both
# main and secondary activity (no rozhodná-částka exemption). Review yearly.
HEALTH_ASSESSMENT_RATE: str = "0.50"
HEALTH_INSURANCE_RATE: str = "0.135"

# Paušální výdaje (flat-rate expenses): (percent, cap_czk, label).
# The cap is the maximum deductible amount for that rate (= percent of the
# 2 000 000 Kč income ceiling). Order is the display order in the picker.
EXPENSE_FLAT_RATES: list[tuple[int, int, str]] = [
    (80, 1_600_000, "80 % – řemeslná a zemědělská výroba"),
    (60, 1_200_000, "60 % – volná živnost (ostatní)"),
    (40,   800_000, "40 % – jiné příjmy ze samostatné činnosti"),
    (30,   600_000, "30 % – příjmy z nájmu"),
]

# Lookup: percent -> cap. Used for server-side validation of the chosen rate.
EXPENSE_FLAT_RATE_CAPS: dict[int, int] = {pct: cap for pct, cap, _ in EXPENSE_FLAT_RATES}

# Default flat rate pre-selected on the page (volná živnost is the common case).
DEFAULT_FLAT_RATE_PERCENT: int = 60


