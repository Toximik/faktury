"""
logic.py – Core business logic.

Three responsibilities:
  1. Variable Symbol generation  – YYYYNN, sequential within the year.
  2. SPAYD QR Code generation    – Base64 PNG, ready for <img src="...">.
  3. PDF rendering               – Jinja2 → HTML → WeasyPrint → bytes.

All monetary arithmetic uses decimal.Decimal to avoid IEEE-754 rounding
errors that are unacceptable in financial documents.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Union

from markupsafe import Markup, escape
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import Session, select
from weasyprint import HTML

from app.config import DEFAULT_BRAND_COLOR
from app.models import Invoice

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_TWO_PLACES = Decimal("0.01")
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Display symbols for the currencies the app offers. The ISO 4217 code stays
# the canonical stored value (models/DB/API); only the human-facing display
# uses the symbol. Unknown codes fall back to the code itself.
CURRENCY_SYMBOLS = {
    "CZK": "Kč",
    "EUR": "€",
    "USD": "$",
    "GBP": "£",
}


def currency_symbol(code: str | None) -> str:
    """Return the display symbol for an ISO 4217 currency code (e.g. CZK → Kč).

    Falls back to the code itself for currencies we don't have a symbol for.
    """
    if not code:
        return ""
    return CURRENCY_SYMBOLS.get(code.upper(), code)

# Czech bank account validation weights (ČNB vyhláška 169/2011 Sb.)
_WEIGHTS_NUMBER = [6, 3, 7, 9, 10, 5, 8, 4, 2, 1]  # account number padded to 10 digits
_WEIGHTS_PREFIX = [10, 5, 8, 4, 2, 1]               # prefix padded to 6 digits


# ---------------------------------------------------------------------------
# Czech bank account utilities
# ---------------------------------------------------------------------------

def _mod11_check(digits: str, weights: list[int]) -> bool:
    """Return True if the weighted mod-11 checksum is satisfied."""
    padded = digits.zfill(len(weights))
    return sum(int(d) * w for d, w in zip(padded, weights)) % 11 == 0


def validate_cz_bank_account(
    number: str,
    code: str,
    prefix: str = "",
) -> str | None:
    """Validate a Czech bank account number.

    Returns None if valid, or a human-readable error string.

    Args:
        number:  Account number (číslo účtu), digits only, max 10.
        code:    Bank code (kód banky), exactly 4 digits.
        prefix:  Předčíslí (optional). If non-empty, must be 1–6 digits.
    """
    if not number.isdigit() or not (1 <= len(number) <= 10):
        return "Číslo účtu musí být 1–10 číslic."
    if not code.isdigit() or len(code) != 4:
        return "Kód banky musí být přesně 4 číslice."
    if prefix and (not prefix.isdigit() or len(prefix) > 6):
        return "Předčíslí musí být 1–6 číslic."

    if not _mod11_check(number, _WEIGHTS_NUMBER):
        return "Číslo účtu neprojde kontrolním součtem (mod 11)."
    if prefix and not _mod11_check(prefix, _WEIGHTS_PREFIX):
        return "Předčíslí neprojde kontrolním součtem (mod 11)."

    return None  # valid


def validate_ico(ico: str) -> str | None:
    """Validate a Czech IČO (8-digit company id with mod-11 check digit).

    Returns None if valid, or a human-readable Czech error string. The ČSÚ
    algorithm: weight digits 1–7 by 8..2, take sum mod 11; the check digit is
    (11 - mod) mod 10.
    """
    if not ico.isdigit() or len(ico) != 8:
        return "IČO musí být 8 číslic."
    weights = [8, 7, 6, 5, 4, 3, 2]
    total = sum(int(ico[i]) * weights[i] for i in range(7))
    check = (11 - (total % 11)) % 10
    if check != int(ico[7]):
        return "IČO neprojde kontrolním součtem."
    return None


def cz_account_to_iban(number: str, code: str, prefix: str = "") -> str:
    """Convert Czech local account format to IBAN.

    CZ IBAN structure: CZ + kk + BBBB + PPPPPP + NNNNNNNNNN
      kk   = 2-digit MOD-97 check digits
      BBBB = bank code (4 digits)
      PPPP = prefix padded to 6 digits
      NNNN = account number padded to 10 digits

    Example: prefix=19, number=2000145399, code=0800
      → CZ6508000000192000145399
    """
    bban = code.zfill(4) + (prefix or "").zfill(6) + number.zfill(10)
    # MOD-97: move CZ00 to end → bban + "123500" (C=12, Z=35, 00)
    check = 98 - (int(bban + "123500") % 97)
    return f"CZ{check:02d}{bban}"


def validate_party_bank_and_ico(
    ic: str = "",
    prefix: str = "",
    number: str = "",
    code: str = "",
    iban: str = "",
) -> tuple[str | None, str]:
    """Validate a party's IČO and Czech bank account; auto-compute the IBAN.

    Used for both the supplier profile and client contacts. Empty fields are
    allowed (validated only when present). Returns ``(error, iban)`` where
    ``error`` is a Czech message or None, and ``iban`` is the value to store —
    computed from prefix/number/code when the caller did not supply one.
    """
    ic = (ic or "").strip()
    prefix = (prefix or "").strip()
    number = (number or "").strip()
    code = (code or "").strip()
    iban = (iban or "").strip()

    if ic:
        err = validate_ico(ic)
        if err:
            return err, iban

    if number or code:
        err = validate_cz_bank_account(number, code, prefix)
        if err:
            return err, iban
        if not iban:
            iban = cz_account_to_iban(number, code, prefix)

    return None, iban


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _to_decimal(value: Any) -> Decimal:
    """Safely convert any numeric type to Decimal."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def compute_tax_summary(
    *,
    income: Any,
    real_expenses: Any,
    expense_mode: str,
    flat_rate_percent: int,
    flat_rate_cap: Any,
    social_threshold: Any = None,
    tax_rate: Any,
    social_assessment_rate: Any,
    social_insurance_rate: Any,
    health_assessment_rate: Any,
    health_insurance_rate: Any,
    activity_type: str = "secondary",
) -> dict[str, Decimal | bool | None]:
    """Estimate an OSVČ's income-tax and social-insurance position for a year.

    All arithmetic is Decimal. Inputs may be int/float/str/Decimal.

    - ``expense_mode``       : "real" (use ``real_expenses``) or "flat" (paušál).
    - ``flat_rate_percent``  : 30/40/60/80, applied to ``income`` and capped at
                               ``flat_rate_cap`` (only used when mode == "flat").
    - ``social_threshold``   : rozhodná částka for vedlejší činnost (or None when
                               the admin has not set a value for the year). When
                               present, the tax base is compared against it to
                               decide whether důchodové pojištění participation
                               arises.
    - ``social_assessment_rate`` : fraction of the tax base that forms the
                               social vyměřovací základ (0.55 since 2024).
    - ``social_insurance_rate``  : social premium rate on that base (0.292).
    - ``health_assessment_rate`` : fraction of the tax base forming the health
                               vyměřovací základ (0.50).
    - ``health_insurance_rate``  : health premium rate on that base (0.135).
                               Health insurance is owed on any profit, for both
                               main and secondary činnost (no threshold).
    - ``activity_type``      : "main" (hlavní činnost – participation is
                               mandatory, the rozhodná částka is ignored, and
                               income_limit/headroom are None) or "secondary"
                               (vedlejší činnost – participation arises only from
                               the rozhodná částka).

    Income tax does NOT subtract sleva na poplatníka: at vedlejší činnost it is
    claimed against the taxpayer's main employment, not here.

    Returns a dict of Decimals plus ``social_participation`` (bool | None):
    ``income``, ``expenses``, ``tax_base``, ``tax_base_rounded`` (rounded down
    to whole hundreds per §16 ZDP), ``tax`` (15 % of the rounded base, to whole
    CZK), ``social_assessment_base`` (vyměřovací základ), ``social_insurance``
    (estimated premium, rounded up to whole CZK), ``income_limit`` (the income
    at which the obligation begins, given the expense mode) and
    ``income_headroom`` (how much more can still be earned before it does; both
    None when no threshold is set).
    """
    income = _to_decimal(income)
    real_expenses = _to_decimal(real_expenses)
    tax_rate = _to_decimal(tax_rate)
    social_assessment_rate = _to_decimal(social_assessment_rate)
    social_insurance_rate = _to_decimal(social_insurance_rate)
    health_assessment_rate = _to_decimal(health_assessment_rate)
    health_insurance_rate = _to_decimal(health_insurance_rate)

    if expense_mode == "flat":
        expenses = (income * Decimal(flat_rate_percent) / Decimal(100)).quantize(
            _TWO_PLACES, rounding=ROUND_HALF_UP
        )
        if flat_rate_cap is not None:
            cap = _to_decimal(flat_rate_cap)
            if expenses > cap:
                expenses = cap
    else:
        expenses = real_expenses

    tax_base = income - expenses
    if tax_base < 0:
        tax_base = Decimal("0")

    # §16: the tax base is rounded down to whole hundreds before applying the rate.
    tax_base_rounded = (tax_base // Decimal(100)) * Decimal(100)
    tax = (tax_base_rounded * tax_rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    # Sociální pojištění: vyměřovací základ = 55 % of the tax base; the premium
    # (29.2 %) is rounded up to whole crowns (na celé koruny nahoru).
    social_assessment_base = (tax_base * social_assessment_rate).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    social_insurance = (social_assessment_base * social_insurance_rate).quantize(
        Decimal("1"), rounding=ROUND_CEILING
    )

    # Zdravotní pojištění: vyměřovací základ = 50 % of the tax base; premium
    # (13.5 %) rounded up to whole crowns. Owed on any profit, main or secondary.
    health_assessment_base = (tax_base * health_assessment_rate).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    health_insurance = (health_assessment_base * health_insurance_rate).quantize(
        Decimal("1"), rounding=ROUND_CEILING
    )

    social_participation: bool | None = None
    income_limit: Decimal | None = None
    income_headroom: Decimal | None = None
    if activity_type == "main":
        # Hlavní činnost: participation is mandatory regardless of profit; the
        # rozhodná částka does not apply, so there is no income limit/headroom.
        social_participation = True
    elif social_threshold is not None:
        # Vedlejší činnost: participation arises only once the tax base reaches
        # the rozhodná částka.
        threshold = _to_decimal(social_threshold)
        social_participation = tax_base >= threshold
        # Income level at which the tax base reaches the threshold (and the
        # obligation begins), given the chosen expense mode:
        #   real : tax_base = income − expenses          → income = threshold + expenses
        #   flat : tax_base = income × (1 − rate)        → income = threshold / (1 − rate)
        if expense_mode == "flat":
            factor = Decimal(1) - Decimal(flat_rate_percent) / Decimal(100)
            if factor > 0:
                income_limit = (threshold / factor).quantize(
                    _TWO_PLACES, rounding=ROUND_HALF_UP
                )
        else:
            income_limit = threshold + real_expenses
        if income_limit is not None:
            income_headroom = income_limit - income
            if income_headroom < 0:
                income_headroom = Decimal("0")

    return {
        "income":            income,
        "expenses":          expenses,
        "tax_base":          tax_base,
        "tax_base_rounded":  tax_base_rounded,
        "tax":               tax,
        "social_threshold":  None if social_threshold is None else _to_decimal(social_threshold),
        "social_participation":   social_participation,
        "social_assessment_base": social_assessment_base,
        "social_insurance":       social_insurance,
        "health_assessment_base": health_assessment_base,
        "health_insurance":       health_insurance,
        "income_limit":           income_limit,
        "income_headroom":        income_headroom,
        "activity_type":          activity_type,
    }


def _as_dict(obj: Any) -> dict[str, Any]:
    """Normalise a SQLModel instance or plain dict to a plain dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):      # SQLModel / Pydantic v2
        return obj.model_dump()
    if hasattr(obj, "dict"):            # Pydantic v1 fallback
        return obj.dict()
    return vars(obj)


def format_amount(value: Any) -> str:
    """Format a number Czech-style with 2 decimals, no currency: '1\u00a0500,00'.

    Groups thousands with a non-breaking space (so the number never breaks
    across a line) and uses a comma as the decimal separator. Single source of
    truth for money digit formatting across the whole app — HTML pages (via the
    ``amount`` / ``money`` Jinja filters) and the PDF.
    """
    rounded = _to_decimal(value).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    int_part, dec_part = f"{rounded:.2f}".split(".")
    negative = int_part.startswith("-")
    digits = int_part.lstrip("-")

    # Build thousands-grouped string right-to-left
    grouped = ""
    for i, ch in enumerate(reversed(digits)):
        if i and i % 3 == 0:
            grouped = "\u00a0" + grouped  # non-breaking space
        grouped = ch + grouped

    if negative:
        grouped = "-" + grouped

    return f"{grouped},{dec_part}"


def format_money(value: Any, currency: str = "CZK") -> str:
    """Format a number Czech-style with a currency symbol: '1\u00a0500,00\u00a0Kč'.

    The currency is shown as its display symbol (CZK → Kč, EUR → €, …) via
    currency_symbol(). Digit grouping is delegated to format_amount().
    """
    return f"{format_amount(value)}\u00a0{currency_symbol(currency)}"


# Backwards-compatible alias used by the PDF render env.
def _format_czk(value: Decimal, currency: str = "CZK") -> str:
    return format_money(value, currency)


def _image_to_data_uri(path: str | Path) -> str:
    """Read an image file and return it as an embedded data URI.

    Embedding avoids any WeasyPrint filesystem path resolution issues.
    Returns an empty string if the file does not exist.
    """
    p = Path(path)
    if not p.is_file():
        return ""
    suffix = p.suffix.lower().lstrip(".")
    mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix
    encoded = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{encoded}"


def _nl2br(value: str) -> Markup:
    """Jinja2 filter: escape HTML then replace newlines with <br> tags.

    Returning Markup tells Jinja2 the string is already safe so it will
    not be escaped a second time during rendering.
    """
    return Markup(str(escape(value)).replace("\n", "<br>\n"))


def slugify_filename(value: str) -> str:
    """Turn a free-text name into an ASCII, space-free filename component.

    Transliterates Czech diacritics (á→a, č→c, ...) then keeps only
    [A-Za-z0-9]. Used to build the invoice PDF download filename from the
    supplier name.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]+", "", ascii_only)


# ---------------------------------------------------------------------------
# 1. Variable Symbol Generator
# ---------------------------------------------------------------------------

def _max_series_seq(session: Session, user_id: int, year_prefix: str,
                    lo: int, hi: int) -> int:
    """Highest 4-digit sequence in [lo, hi] among the user's year's symbols.

    Only symbols matching the series format exactly — the year followed by a
    4-digit sequence — count; that includes user-supplied custom numbers, so
    after a custom ``20260007`` the next automatic number is ``20260008``.
    Custom numbers in any other shape (shorter, longer, or a different prefix)
    live outside the series and are ignored, which also prevents an oversized
    custom number such as ``2026999999`` from exhausting the sequence.
    """
    rows = session.exec(
        select(Invoice.variable_symbol)
        .where(Invoice.user_id == user_id)
        .where(Invoice.variable_symbol.like(f"{year_prefix}%"))
    ).all()

    max_seq = 0
    for vs in rows:
        suffix = vs[len(year_prefix):]
        if len(suffix) == 4 and suffix.isdigit() and lo <= int(suffix) <= hi:
            max_seq = max(max_seq, int(suffix))
    return max_seq


def get_next_variable_symbol(session: Session, user_id: int) -> str:
    """Return the next invoice Variable Symbol (YYYYNNNN) for the user and year.

    The invoice series runs 0001–8999; the 9NNN block of each year is reserved
    for the dobropis series (get_next_credit_note_symbol).

    Concurrency note: this is a read-then-increment, so two parallel callers can
    propose the same symbol. The unique index on (user_id, variable_symbol)
    turns the loser's INSERT into an IntegrityError, which create_invoice retries.
    """
    current_year = date.today().year
    seq = _max_series_seq(session, user_id, str(current_year), 1, 8999) + 1
    if seq > 8999:
        raise ValueError(
            f"Variable symbol sequence exhausted for {current_year}."
        )
    return f"{current_year}{seq:04d}"


def get_next_credit_note_symbol(session: Session, user_id: int) -> str:
    """Return the next dobropis Variable Symbol (YYYY9NNN) for the user and year.

    Dobropisy have their own series in the year's reserved 9NNN block:
    20269001, 20269002, … 20269999 (999 per year). Same read-then-increment
    concurrency contract as get_next_variable_symbol.
    """
    current_year = date.today().year
    seq = _max_series_seq(session, user_id, str(current_year), 9001, 9999)
    next_seq = max(seq, 9000) + 1
    if next_seq > 9999:
        raise ValueError(
            f"Credit note sequence exhausted for {current_year}."
        )
    return f"{current_year}{next_seq:04d}"


# ---------------------------------------------------------------------------
# 2. SPAYD QR Code Generator
# ---------------------------------------------------------------------------

def generate_spayd_qr(
    iban: str,
    amount: Decimal,
    vs: str,
    currency: str = "CZK",
    message: str = "",
) -> str:
    """Generate a SPAYD-compliant QR code and return it as a Base64 SVG string.

    The returned string can be embedded directly into an HTML <img> tag:

        <img src="data:image/svg+xml;base64,{{ qr_code_base64 }}">

    Uses qrplatba 1.x which renders SVG natively (higher quality for print).

    Args:
        iban:       Payee IBAN (e.g. "CZ6508000000192000145399").
        amount:     Invoice total as Decimal.
        vs:         Variable symbol (6-digit string, e.g. "202601").
        currency:   ISO 4217 code, default "CZK".
        message:    Optional payment message / note.

    Returns:
        Base64-encoded SVG bytes as an ASCII string.
    """
    # Import lazily so the rest of the module loads even if qrplatba is absent
    from qrplatba import QRPlatbaGenerator

    qr = QRPlatbaGenerator(
        iban,
        amount=float(amount),
        x_vs=vs,
        message=message or None,
        currency=currency,
    )
    svg_bytes: bytes = qr.make_image().to_string()
    return base64.b64encode(svg_bytes).decode("ascii")


# ---------------------------------------------------------------------------
# 3. Line-item computation (Decimal arithmetic)
# ---------------------------------------------------------------------------

def _compute_items(
    raw_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], Decimal, Decimal, Decimal]:
    """Enrich raw line-item dicts with computed Decimal totals.

    Expected keys in each input item:
        description (str)
        quantity    (numeric – int, float, str, or Decimal)
        unit        (str, optional – e.g. "hod", "ks", "paušál")
        unit_price  (numeric)
        vat_rate    (numeric – fractional, e.g. 0.21 for 21 %; default 0)

    Returns:
        (enriched_items, subtotal_excl_vat, total_vat, total_incl_vat)
    """
    computed: list[dict[str, Any]] = []
    subtotal_excl = Decimal("0")
    total_vat = Decimal("0")
    total_incl = Decimal("0")

    for item in raw_items:
        qty        = _to_decimal(item.get("quantity", 1))
        unit_price = _to_decimal(item.get("unit_price", 0))
        vat_rate   = _to_decimal(item.get("vat_rate", 0))

        # Round each sub-calculation to 2 decimal places per CZ accounting norms
        subtotal   = (qty * unit_price).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        vat_amount = (subtotal * vat_rate).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        line_total = subtotal + vat_amount

        subtotal_excl += subtotal
        total_vat     += vat_amount
        total_incl    += line_total

        computed.append({
            **item,                          # preserve any extra fields (unit, notes…)
            "quantity":   qty,
            "unit_price": unit_price,
            "vat_rate":   vat_rate,
            "vat_pct":    int(vat_rate * 100),  # 0.21 → 21, for display
            "subtotal":   subtotal,
            "vat_amount": vat_amount,
            "line_total": line_total,
        })

    return computed, subtotal_excl, total_vat, total_incl


# ---------------------------------------------------------------------------
# 4. PDF Renderer
# ---------------------------------------------------------------------------

def render_invoice_to_pdf(
    invoice_data: Union[dict[str, Any], Any],
    contact_data: Union[dict[str, Any], Any],
    supplier_data: dict[str, Any],
    qr_code_base64: str,
    brand_color: str = DEFAULT_BRAND_COLOR,
    related_vs: str = "",
) -> bytes:
    """Render an invoice to a PDF document returned as raw bytes.

    Args:
        invoice_data:    Invoice model instance or equivalent dict.
                         Expected keys match the Invoice SQLModel fields.
        contact_data:    Contact model instance or equivalent dict.
        supplier_data:   Freelancer's own business details as a plain dict.
                         Recommended keys:
                           name, address, ic, dic (optional),
                           bank_account, email (optional), phone (optional),
                           tagline (optional)
        qr_code_base64:  Output of generate_spayd_qr().
        brand_color:     Accent colour (hex). Sanitised here as defence in depth
                         because it is interpolated into the template's CSS.
        related_vs:      For a dobropis (doc_type == "credit_note"): the
                         variable symbol of the corrected invoice, shown as
                         "Opravný doklad k faktuře č. X" under the title.

    Returns:
        Raw PDF bytes, suitable for:
            return Response(content=pdf_bytes, media_type="application/pdf")
    """
    invoice = _as_dict(invoice_data)
    contact = _as_dict(contact_data)

    # Only ever let a clean #RRGGBB value reach the CSS; fall back otherwise.
    if not _HEX_COLOR.match(brand_color or ""):
        brand_color = DEFAULT_BRAND_COLOR

    # Parse JSON items string (present when invoice came from model_dump())
    raw_items: Any = invoice.get("items", [])
    if isinstance(raw_items, str):
        raw_items = json.loads(raw_items)

    computed_items, subtotal_excl, total_vat, total_incl = _compute_items(raw_items)

    # Logo – from supplier profile snapshot
    logo_uri = ""
    logo_path = supplier_data.get("logo_path", "")
    if logo_path:
        logo_uri = _image_to_data_uri(logo_path)

    # Stamp – taken solely from the supplier profile. Its image is uploaded
    # through an authenticated handler that confines the file to the user's own
    # uploads directory, so the path is trusted. We deliberately do NOT honour
    # any per-invoice path: accepting a client-supplied filesystem path here
    # would turn PDF generation into an arbitrary server-file read.
    stamp_uri = ""
    stamp_path = supplier_data.get("stamp_path")
    if stamp_path:
        stamp_uri = _image_to_data_uri(stamp_path)

    currency = invoice.get("currency", "CZK")

    # Build a fresh Jinja2 environment per render to allow per-invoice defaults
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["czk"]     = lambda v: _format_czk(_to_decimal(v), currency)
    env.filters["date_cz"] = lambda d: d.strftime("%d.%m.%Y") if d else "—"
    env.filters["nl2br"]   = _nl2br

    template = env.get_template("invoice.html")

    html_content = template.render(
        invoice       = invoice,
        contact       = contact,
        supplier      = supplier_data,
        items         = computed_items,
        subtotal_excl = subtotal_excl,
        total_vat     = total_vat,
        total_incl    = total_incl,
        currency      = currency,
        qr_code_base64= qr_code_base64,
        logo_uri      = logo_uri,
        stamp_uri     = stamp_uri,
        brand_color   = brand_color,
        is_credit     = invoice.get("doc_type") == "credit_note",
        related_vs    = related_vs,
    )

    return HTML(
        string   = html_content,
        base_url = str(_TEMPLATES_DIR),  # Resolves any relative asset paths
    ).write_pdf()
