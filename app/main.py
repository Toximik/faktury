"""
main.py – FastAPI application entry point.

What lives here
---------------
  - Application factory and lifespan handler (DB table creation on startup).
  - Static file mounting (/static) and Jinja2 template engine setup.
  - Auth endpoints: POST /token (Bearer JSON), POST /login (cookie + redirect),
    POST /logout (clear cookie).
  - UI routes (GET HTML pages + POST form handlers) under the /ui prefix.
  - GET /health – lightweight liveness probe for Nginx / systemd checks.
  - Router includes for contacts, invoices, expenses, and export (JSON API).

All JSON API routes are implemented in app/routers/ to keep this file lean.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncGenerator, Optional

from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import (
    create_access_token_for_user,
    get_password_hash,
    resolve_user_from_token,
    validate_password_policy,
    verify_password,
)
from app.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    BRAND_COLOR_CHOICES,
    BRAND_COLOR_PALETTE,
    DEFAULT_BRAND_COLOR,
    DEFAULT_FLAT_RATE_PERCENT,
    DEFAULT_PAYMENT_DUE_DAYS,
    EXPENSE_FLAT_RATES,
    EXPENSE_FLAT_RATE_CAPS,
    HEALTH_ASSESSMENT_RATE,
    HEALTH_INSURANCE_RATE,
    INCOME_TAX_RATE,
    IS_PRODUCTION,
    SOCIAL_ASSESSMENT_RATE,
    SOCIAL_INSURANCE_RATE,
)
from app.backup import BackupError, export_user, import_user
from app.database import create_db_and_tables, get_session
from app.logic import (
    compute_tax_summary,
    currency_symbol,
    format_amount,
    format_money,
    get_next_credit_note_symbol,
    get_next_variable_symbol,
    validate_party_bank_and_ico,
)
from app.models import Contact, Expense, Invoice, SupplierProfile, TaxSetting, User
from app.routers import contacts, expenses, export, invoices
from app.schemas import Token
from app.uploads_util import (
    ALLOWED_IMAGE_TYPES as _ALLOWED_IMAGE_TYPES,
    ALLOWED_SCAN_TYPES as _ALLOWED_SCAN_TYPES,
    EXT_MAP as _EXT_MAP,
    MAX_IMAGE_BYTES as _MAX_IMAGE_BYTES,
    MAX_SCAN_BYTES as _MAX_SCAN_BYTES,
    UPLOADS_DIR as _UPLOADS_DIR,
    content_matches_type as _content_matches_type,
)


# ── Application lifespan ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Run startup tasks before serving requests."""
    create_db_and_tables()
    yield


# ── Paths ─────────────────────────────────────────────────────────────────────

_APP_DIR = Path(__file__).resolve().parent
# _UPLOADS_DIR is imported from app.uploads_util (single source of truth).
_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


# ── Application factory ───────────────────────────────────────────────────────

app = FastAPI(
    title="Fakturace",
    description="Lightweight Invoice & Expense Manager for freelancers.",
    version="1.0.0",
    # Disable the interactive docs in production to reduce attack surface.
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    lifespan=lifespan,
)

# ── CSRF protection (same-origin guard for cookie-authenticated writes) ──────
# State-changing requests authenticated via the session cookie must originate
# from our own pages. SameSite=Lax already blocks the common cross-site form
# POST; this is a second, explicit layer. Bearer-token API clients (which send
# an Authorization header, not the cookie) are exempt — a cross-site attacker
# cannot set that header without CORS, which we do not enable.

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


@app.middleware("http")
async def csrf_same_origin(request: Request, call_next):
    if request.method not in _SAFE_METHODS:
        has_cookie = "access_token" in request.cookies
        auth_header = request.headers.get("authorization", "")
        has_bearer = auth_header.lower().startswith("bearer ")
        if has_cookie and not has_bearer:
            host = request.headers.get("host", "")
            source = request.headers.get("origin") or request.headers.get("referer")
            same_origin = bool(source and host and urlparse(source).netloc == host)
            if not same_origin:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Požadavek z cizího původu byl zablokován (CSRF)."},
                )
    return await call_next(request)


# ── Static files (/static → app/static/, uploads excluded) ───────────────────
# NOTE: /static/uploads/ is intentionally NOT served here.
# Uploaded user files are served through the authenticated /uploads/ route below.

app.mount(
    "/static",
    StaticFiles(directory=str(_APP_DIR / "static")),
    name="static",
)

# ── Jinja2 templates ──────────────────────────────────────────────────────────

templates = Jinja2Templates(directory=str(_APP_DIR / "templates"))
# Display currency codes as symbols (CZK → Kč, EUR → €, …) in all HTML pages.
templates.env.filters["currency"] = currency_symbol
templates.env.globals["currency_symbol"] = currency_symbol
# Czech money formatting (thousands grouped with a non-breaking space):
#   {{ amount | money }}          -> "1 500,00 Kč"    (defaults to CZK)
#   {{ amount | money(inv.currency) }} -> "1 500,00 €"
#   {{ amount | amount }}         -> "1 500,00"       (digits only, no symbol)
templates.env.filters["money"] = format_money
templates.env.filters["amount"] = format_amount

_static_hashes: dict[str, str] = {}


def _static_url(path: str) -> str:
    """URL of a file under app/static/ with a content-hash cache buster.

    Nginx serves /static/ with a 30-day ``immutable`` cache, so the URL must
    change whenever the file does: ``static_url('style.css')`` →
    ``/static/style.css?v=<hash>``. Hashed once per process (files only change
    on deploy, which restarts the app).
    """
    if path not in _static_hashes:
        try:
            data = (_APP_DIR / "static" / path).read_bytes()
            _static_hashes[path] = hashlib.sha256(data).hexdigest()[:10]
        except OSError:
            _static_hashes[path] = ""
    v = _static_hashes[path]
    return f"/static/{path}?v={v}" if v else f"/static/{path}"

templates.env.globals["static_url"] = _static_url

# ── JSON API routers ──────────────────────────────────────────────────────────

app.include_router(contacts.router, prefix="/api")
app.include_router(invoices.router)
app.include_router(expenses.router)
app.include_router(export.router)


# ── UI auth dependency (redirects to /login instead of raising 401) ──────────

def _get_ui_user(request: Request, session: Session = Depends(get_session)) -> User:
    """Dependency for HTML page routes.

    Unlike get_current_user (which raises 401 JSON), this redirects an
    unauthenticated browser to the login page via HTTP 307. Token validity,
    account-active status, and token version are all enforced centrally by
    resolve_user_from_token.
    """
    _redirect = HTTPException(
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Location": "/login"},
    )
    token: Optional[str] = request.cookies.get("access_token")
    if not token:
        raise _redirect
    user = resolve_user_from_token(session, token)
    if user is None:
        raise _redirect
    return user


def _base_ctx(request: Request, user: Optional[User] = None) -> dict:
    """Shared context injected into every HTML template."""
    return {
        "request":      request,
        "current_year": date.today().year,
        "today_date":   date.today(),
        "active_nav":   "",
        "current_user": user,
        "default_due_days": DEFAULT_PAYMENT_DUE_DAYS,
        "brand_palette": BRAND_COLOR_PALETTE,
        "default_brand_color": DEFAULT_BRAND_COLOR,
    }


def _safe_redirect_target(target: str) -> str:
    """Clamp a form-supplied redirect to a local path (open-redirect guard)."""
    if not target or not target.startswith("/") or "//" in target:
        return "/"
    return target


def _invoice_rows(session: Session, inv_rows: list[Invoice]) -> list[dict]:
    """Resolve contact names into the display-row shape used by the invoice table."""
    rows = []
    for inv in inv_rows:
        contact = session.get(Contact, inv.contact_id)
        rows.append({
            "id":              inv.id,
            "variable_symbol": inv.variable_symbol,
            "contact_name":    contact.name if contact else "—",
            "amount":          inv.amount,
            "currency":        inv.currency,
            "date_issued":     inv.date_issued,
            "date_due":        inv.date_due,
            "is_paid":         inv.is_paid,
            "date_paid":       inv.date_paid,
            "doc_type":        inv.doc_type,
        })
    return rows


# ── Authenticated upload file serving ────────────────────────────────────────

@app.get("/uploads/{owner_id}/{path:path}", include_in_schema=False)
def serve_upload(
    owner_id: int,
    path: str,
    _user: User = Depends(_get_ui_user),
) -> Response:
    """Serve a user-uploaded file.

    Access rules:
    - The requesting user must own the file (owner_id == _user.id), OR
    - The requesting user must be an admin.
    Any other request gets a 404 (not 403, to avoid leaking existence).
    """
    if owner_id != _user.id and not _user.is_admin:
        raise HTTPException(status_code=404)

    file_path = _UPLOADS_DIR / str(owner_id) / path
    # Resolve and verify the path is still inside _UPLOADS_DIR (path traversal guard)
    try:
        resolved = file_path.resolve()
        _UPLOADS_DIR.resolve()
        resolved.relative_to(_UPLOADS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=404)

    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404)

    import mimetypes
    media_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
    return Response(content=resolved.read_bytes(), media_type=media_type)


# ── Auth endpoints ────────────────────────────────────────────────────────────

# Bcrypt hash of a random throwaway string (cost 12, same as real hashes).
# Verified against when the username does not exist, so an unknown username
# costs the same ~250 ms as a wrong password — without it, the fast-path
# return is a timing oracle for username enumeration. The hash value itself
# is meaningless; a match can never occur and would grant nothing anyway.
_TIMING_DUMMY_HASH = "$2b$12$pz4c2sahGwU02o2hZGNvsehKZ58RR3gU5ORp5ZKjr6TlBiP4KBbIq"


def _authenticate_user(session: Session, username: str, password: str) -> User:
    """Return the matching User or raise HTTP 401."""
    user: User | None = session.exec(
        select(User).where(User.username == username)
    ).first()
    password_ok = verify_password(
        password, user.hashed_password if user else _TIMING_DUMMY_HASH
    )
    if not user or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled.",
        )
    return user


@app.post(
    "/token",
    response_model=Token,
    tags=["auth"],
    summary="OAuth2 token endpoint (returns Bearer JSON)",
)
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
) -> Token:
    """Standard OAuth2 Password Grant.

    Returns a JSON body ``{"access_token": "...", "token_type": "bearer"}``.
    Use this endpoint when accessing the API programmatically (curl, scripts,
    the interactive /docs UI in development).
    """
    user = _authenticate_user(session, form_data.username, form_data.password)
    token = create_access_token_for_user(
        user,
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return Token(access_token=token, token_type="bearer")


@app.post(
    "/login",
    tags=["auth"],
    summary="Browser login – sets HttpOnly cookie and redirects",
    status_code=status.HTTP_303_SEE_OTHER,
)
def login_with_cookie(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
) -> Response:
    """Browser-friendly login endpoint.

    Validates credentials, sets an HttpOnly ``access_token`` cookie, then
    redirects to the dashboard (/). Used by the HTML login form. On failure it
    re-renders the login page with an inline message (never the raw JSON error
    that the API ``/token`` endpoint returns).

    Security properties of the cookie
    ----------------------------------
    - HttpOnly: not accessible from JavaScript (mitigates XSS token theft).
    - Secure:   only sent over HTTPS (enforced in production via IS_PRODUCTION).
    - SameSite=Lax: mitigates most CSRF attack scenarios.
    """
    try:
        user = _authenticate_user(session, form_data.username, form_data.password)
    except HTTPException as exc:
        message = (
            "Účet je zablokován."
            if exc.status_code == status.HTTP_403_FORBIDDEN
            else "Nesprávné uživatelské jméno nebo heslo."
        )
        return templates.TemplateResponse(
            request, "login.html",
            {"request": request, "error": message},
            status_code=exc.status_code,
        )

    token = create_access_token_for_user(
        user,
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=IS_PRODUCTION,     # False in dev (no TLS locally)
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return response


@app.post(
    "/logout",
    tags=["auth"],
    summary="Clear the session cookie and redirect to login",
    status_code=status.HTTP_303_SEE_OTHER,
)
def logout() -> Response:
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("access_token")
    return response


# ── GET /ucet ──────────────────────────────────────────────────────────────

@app.get("/ucet", tags=["ui"], include_in_schema=False)
def ui_account_page(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
) -> Response:
    supplier = session.exec(
        select(SupplierProfile).where(SupplierProfile.user_id == _user.id)
    ).first()
    ctx = _base_ctx(request, _user)
    ctx.update(active_nav="account", user=_user, supplier=supplier, error=None, success=None)
    return templates.TemplateResponse(request, "account.html", ctx)


# ── POST /ui/ucet ──────────────────────────────────────────────────────────

@app.post("/ui/ucet", tags=["ui"], include_in_schema=False)
def ui_update_account(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    new_username: str = Form(""),
    current_password: str = Form(...),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
) -> Response:
    supplier = session.exec(
        select(SupplierProfile).where(SupplierProfile.user_id == _user.id)
    ).first()

    def _render(error=None, success=None):
        ctx = _base_ctx(request, _user)
        ctx.update(active_nav="account", user=_user, supplier=supplier, error=error, success=success)
        return templates.TemplateResponse(request, "account.html", ctx)

    # Always verify current password first
    if not verify_password(current_password, _user.hashed_password):
        return _render(error="Současné heslo je nesprávné.")

    changed = False

    # Username change
    new_username = new_username.strip()
    if new_username and new_username != _user.username:
        existing = session.exec(
            select(User).where(User.username == new_username)
        ).first()
        if existing:
            return _render(error="Toto uživatelské jméno je již obsazeno.")
        _user.username = new_username
        changed = True

    # Password change
    if new_password:
        policy_error = validate_password_policy(new_password)
        if policy_error:
            return _render(error=policy_error)
        if new_password != confirm_password:
            return _render(error="Nové heslo a potvrzení se neshodují.")
        _user.hashed_password = get_password_hash(new_password)
        # Invalidate all other outstanding sessions for this account. The
        # current session is re-issued a fresh cookie below, so it survives.
        _user.token_version += 1
        changed = True

    if changed:
        session.add(_user)
        session.commit()
        session.refresh(_user)
        # Re-issue cookie with the (possibly new) username and current token
        # version so this session is not logged out by the bump above.
        token = create_access_token_for_user(
            _user,
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        response = templates.TemplateResponse(
            request, "account.html",
            {**_base_ctx(request, _user), "active_nav": "account",
             "user": _user, "supplier": supplier, "error": None, "success": "Změny byly uloženy."},
        )
        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            secure=IS_PRODUCTION,
            samesite="lax",
            max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )
        return response

    return _render(success="Žádné změny k uložení.")


# ── POST /ui/ucet/dodavatel ─────────────────────────────────────────────────

@app.post("/ui/ucet/dodavatel", tags=["ui"], include_in_schema=False)
def ui_update_supplier(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    name: str = Form(""),
    address: str = Form(""),
    ic: str = Form(""),
    dic: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    bank_prefix: str = Form(""),
    bank_number: str = Form(""),
    bank_code: str = Form(""),
    bank_iban: str = Form(""),
    trade_register_date: str = Form(""),
    brand_color: str = Form(""),
    osvc_activity_type: str = Form("main"),
) -> Response:
    """Save the supplier (business identity) profile."""
    supplier = session.exec(
        select(SupplierProfile).where(SupplierProfile.user_id == _user.id)
    ).first()
    if supplier is None:
        supplier = SupplierProfile(user_id=_user.id)

    # Validate IČO + bank account and auto-compute the IBAN used for the SPAYD
    # payment QR. A wrong account here means clients pay the wrong number, so
    # this must be correct before we persist.
    error, computed_iban = validate_party_bank_and_ico(
        ic=ic, prefix=bank_prefix, number=bank_number, code=bank_code, iban=bank_iban,
    )
    # Brand colour must be one of the curated palette values (it is interpolated
    # into the PDF's CSS); empty keeps the current value.
    chosen_color = brand_color.strip().upper()
    if chosen_color and chosen_color not in BRAND_COLOR_CHOICES:
        error = error or "Neplatná barva faktury."
    if error:
        ctx = _base_ctx(request, _user)
        ctx.update(active_nav="account", user=_user, supplier=supplier,
                   error=error, success=None)
        return templates.TemplateResponse(request, "account.html", ctx, status_code=422)

    if chosen_color:
        supplier.brand_color = chosen_color
    supplier.osvc_activity_type = (
        osvc_activity_type if osvc_activity_type in ("main", "secondary") else "main"
    )
    supplier.name    = name.strip()
    supplier.address = address.strip()
    supplier.ic      = ic.strip()
    supplier.dic     = dic.strip() or None
    supplier.email   = email.strip() or None
    supplier.phone   = phone.strip() or None
    supplier.bank_prefix = bank_prefix.strip() or None
    supplier.bank_number = bank_number.strip()
    supplier.bank_code   = bank_code.strip()
    supplier.bank_iban   = computed_iban or None
    if trade_register_date.strip():
        from datetime import date as _date
        try:
            supplier.trade_register_date = _date.fromisoformat(trade_register_date.strip())
        except ValueError:
            supplier.trade_register_date = None
    else:
        supplier.trade_register_date = None

    session.add(supplier)
    session.commit()
    session.refresh(supplier)

    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav="account", user=_user, supplier=supplier,
        error=None, success="Profil dodavatele byl uložen.",
        supplier_success=True,
    )
    return templates.TemplateResponse(request, "account.html", ctx)


# ── POST /ui/ucet/nahrat/{logo|stamp} ─────────────────────────────────────

# Allowed types, size caps, and the magic-byte sniff (_content_matches_type)
# live in app.uploads_util and are imported above so the backup import logic
# shares exactly the same defenses.


async def _read_upload_or_413(file: UploadFile, max_bytes: int) -> bytes:
    """Read up to max_bytes (+1 to detect overflow) or raise HTTP 413."""
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Soubor je příliš velký (max {max_bytes // (1024 * 1024)} MB).",
        )
    return data


@app.post("/ui/ucet/nahrat/{kind}", tags=["ui"], include_in_schema=False)
async def ui_upload_image(
    kind: str,
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    file: UploadFile = File(...),
) -> Response:
    """Upload logo or stamp image for the supplier profile."""
    if kind not in ("logo", "stamp"):
        raise HTTPException(status_code=404)

    content_type = file.content_type or ""
    if content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Nepodporovaný formát. Použijte JPG, PNG nebo WebP.")

    contents = await _read_upload_or_413(file, _MAX_IMAGE_BYTES)
    if not _content_matches_type(contents, content_type):
        raise HTTPException(status_code=400, detail="Obsah souboru neodpovídá uvedenému formátu.")

    ext = _EXT_MAP[content_type]
    user_dir = _UPLOADS_DIR / str(_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)

    # Remove any old file for this kind (different extension)
    for old in user_dir.glob(f"{kind}.*"):
        old.unlink(missing_ok=True)

    dest = user_dir / f"{kind}.{ext}"
    dest.write_bytes(contents)

    # Stored as an absolute path (moving the install dir breaks it); porting
    # to another machine goes through backup export/import, which rewrites
    # uploads paths on restore.
    stored_path = str(dest)

    supplier = session.exec(
        select(SupplierProfile).where(SupplierProfile.user_id == _user.id)
    ).first()
    if supplier is None:
        supplier = SupplierProfile(user_id=_user.id)

    if kind == "logo":
        supplier.logo_path = stored_path
    else:
        supplier.stamp_path = stored_path

    session.add(supplier)
    session.commit()

    return RedirectResponse(url="/ucet", status_code=status.HTTP_303_SEE_OTHER)


# ── POST /ui/ucet/smazat/{logo|stamp} ─────────────────────────────────────

@app.post("/ui/ucet/smazat/{kind}", tags=["ui"], include_in_schema=False)
def ui_delete_image(
    kind: str,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
) -> Response:
    """Remove logo or stamp image from the supplier profile."""
    if kind not in ("logo", "stamp"):
        raise HTTPException(status_code=404)

    supplier = session.exec(
        select(SupplierProfile).where(SupplierProfile.user_id == _user.id)
    ).first()
    if supplier:
        path = supplier.logo_path if kind == "logo" else supplier.stamp_path
        if path:
            Path(path).unlink(missing_ok=True)
        if kind == "logo":
            supplier.logo_path = None
        else:
            supplier.stamp_path = None
        session.add(supplier)
        session.commit()

    return RedirectResponse(url="/ucet", status_code=status.HTTP_303_SEE_OTHER)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"], summary="Liveness probe")
def health() -> dict:
    """Returns 200 OK when the process is running.

    Used by Nginx upstream health checks and systemd watchdog scripts.
    No authentication required – contains no sensitive information.
    """
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════════════════════
# UI ROUTES  –  HTML page rendering
# ══════════════════════════════════════════════════════════════════════════════

# ── GET /login ────────────────────────────────────────────────────────────────

@app.get("/login", tags=["ui"], include_in_schema=False)
def ui_login_page(request: Request) -> Response:
    return templates.TemplateResponse(request, "login.html")


# ── GET / (dashboard) ─────────────────────────────────────────────────────────

@app.get("/", tags=["ui"], include_in_schema=False)
def ui_dashboard(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
) -> Response:
    year = date.today().year

    # All invoices for the current year, newest first
    inv_rows = session.exec(
        select(Invoice)
        .where(Invoice.user_id == _user.id)
        .where(Invoice.variable_symbol.like(f"{year}%"))
        .order_by(Invoice.variable_symbol.desc())
    ).all()

    # Resolve contact names and build summary-friendly list
    invoice_list = _invoice_rows(session, inv_rows)
    total_paid = sum(inv.amount for inv in inv_rows if inv.is_paid)
    paid_count = sum(1 for inv in inv_rows if inv.is_paid)

    # Expenses for the current year
    exp_rows = session.exec(
        select(Expense)
        .where(Expense.user_id == _user.id)
        .where(Expense.date >= date(year, 1, 1))
        .where(Expense.date <= date(year, 12, 31))
        .order_by(Expense.date.desc())
    ).all()

    total_expenses = sum(e.amount for e in exp_rows)

    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav     = "dashboard",
        invoices       = invoice_list,
        expenses       = exp_rows,
        stats          = {
            "total_paid":     total_paid,
            "paid_count":     paid_count,
            "total_expenses": total_expenses,
            "expense_count":  len(exp_rows),
        },
    )
    return templates.TemplateResponse(request, "dashboard.html", ctx)


# ── GET /faktury  (all invoices, filterable) ──────────────────────────────────

@app.get("/faktury", tags=["ui"], include_in_schema=False)
def ui_invoices_list(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    year: Optional[int] = None,
    date_field: str = "issued",
) -> Response:
    """List all invoices, optionally filtered by year on the chosen date field."""
    if date_field not in ("issued", "due"):
        date_field = "issued"
    col = Invoice.date_issued if date_field == "issued" else Invoice.date_due

    stmt = select(Invoice).where(Invoice.user_id == _user.id)
    if year:
        stmt = stmt.where(col >= date(year, 1, 1)).where(col <= date(year, 12, 31))
    stmt = stmt.order_by(col.desc())
    inv_rows = session.exec(stmt).all()

    # Distinct years across both dates, for the filter dropdown.
    pairs = session.exec(
        select(Invoice.date_issued, Invoice.date_due).where(Invoice.user_id == _user.id)
    ).all()
    years = sorted({d.year for pair in pairs for d in pair}, reverse=True)

    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav    = "invoices",
        invoices      = _invoice_rows(session, inv_rows),
        years         = years,
        selected_year = year,
        date_field    = date_field,
    )
    return templates.TemplateResponse(request, "invoices_list.html", ctx)


# ── GET /naklady  (all expenses, filterable by year) ──────────────────────────

@app.get("/naklady", tags=["ui"], include_in_schema=False)
def ui_expenses_list(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    year: Optional[int] = None,
) -> Response:
    """List all expenses, optionally filtered by year."""
    stmt = select(Expense).where(Expense.user_id == _user.id)
    if year:
        stmt = stmt.where(Expense.date >= date(year, 1, 1)).where(Expense.date <= date(year, 12, 31))
    stmt = stmt.order_by(Expense.date.desc())
    exp_rows = session.exec(stmt).all()

    year_rows = session.exec(select(Expense.date).where(Expense.user_id == _user.id)).all()
    years = sorted({d.year for d in year_rows}, reverse=True)

    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav    = "expenses",
        expenses      = exp_rows,
        total         = sum(e.amount for e in exp_rows),
        years         = years,
        selected_year = year,
    )
    return templates.TemplateResponse(request, "expenses_list.html", ctx)


# ── GET /statistiky  (monthly income/expense breakdown for a year) ─────────────

@app.get("/statistiky", tags=["ui"], include_in_schema=False)
def ui_statistics(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    year: Optional[int] = None,
    date_field: str = "issued",
) -> Response:
    """Per-month sums of invoices and expenses for a calendar year.

    The issued/paid toggle re-groups invoices by ``date_issued`` (all invoices)
    or by ``date_paid`` (paid invoices only); expenses are always grouped by
    their own single ``date``.
    """
    if date_field not in ("issued", "paid"):
        date_field = "issued"
    paid_only = date_field == "paid"
    inv_col = Invoice.date_paid if paid_only else Invoice.date_issued

    # Years present on invoices (issue dates of all + paid dates of paid) and expenses.
    inv_pairs = session.exec(
        select(Invoice.date_issued, Invoice.date_paid).where(Invoice.user_id == _user.id)
    ).all()
    exp_dates = session.exec(
        select(Expense.date).where(Expense.user_id == _user.id)
    ).all()
    years = sorted(
        {d.year for pair in inv_pairs for d in pair if d is not None}
        | {d.year for d in exp_dates},
        reverse=True,
    )
    if year is None:
        year = date.today().year

    inv_stmt = select(Invoice).where(Invoice.user_id == _user.id)
    if paid_only:
        inv_stmt = inv_stmt.where(Invoice.is_paid == True)  # noqa: E712
    invoices = session.exec(
        inv_stmt
        .where(inv_col >= date(year, 1, 1))
        .where(inv_col <= date(year, 12, 31))
    ).all()
    expenses = session.exec(
        select(Expense)
        .where(Expense.user_id == _user.id)
        .where(Expense.date >= date(year, 1, 1))
        .where(Expense.date <= date(year, 12, 31))
    ).all()

    inv_by_month = [Decimal("0")] * 12
    for inv in invoices:
        d = inv.date_paid if paid_only else inv.date_issued
        inv_by_month[d.month - 1] += Decimal(str(inv.amount))

    exp_by_month = [Decimal("0")] * 12
    for exp in expenses:
        exp_by_month[exp.date.month - 1] += Decimal(str(exp.amount))

    months = [
        "Leden", "Únor", "Březen", "Duben", "Květen", "Červen",
        "Červenec", "Srpen", "Září", "Říjen", "Listopad", "Prosinec",
    ]
    months_short = [
        "Led", "Úno", "Bře", "Dub", "Kvě", "Črv",
        "Čvc", "Srp", "Zář", "Říj", "Lis", "Pro",
    ]
    rows = [
        {
            "name":     months[i],
            "short":    months_short[i],
            "invoices": float(inv_by_month[i]),
            "expenses": float(exp_by_month[i]),
            "diff":     float(inv_by_month[i] - exp_by_month[i]),
        }
        for i in range(12)
    ]
    totals = {
        "invoices": float(sum(inv_by_month)),
        "expenses": float(sum(exp_by_month)),
        "diff":     float(sum(inv_by_month) - sum(exp_by_month)),
    }

    # ── Chart geometry (SVG viewBox user-space coordinates) ──────────────────
    # Bars (invoices/expenses, always ≥ 0) and the profit line share one value
    # scale so they're directly comparable. The axis spans from the smallest
    # monthly profit (which can be negative) up to the largest invoice/expense
    # value, so the profit line can dip below a zero reference line.
    SLOT_W, PLOT_TOP, PLOT_H, BAR_W, BAR_GAP = 46, 10, 170, 12, 3
    diffs = [inv_by_month[i] - exp_by_month[i] for i in range(12)]
    axis_max = float(max([Decimal("0"), *inv_by_month, *exp_by_month]))
    axis_min = float(min([Decimal("0"), *diffs]))
    span = (axis_max - axis_min) or 1.0

    def _y(v: float) -> float:
        return PLOT_TOP + (axis_max - v) / span * PLOT_H

    zero_y = _y(0.0)
    chart_bars = []
    line_pts = []
    for i in range(12):
        cx = i * SLOT_W + SLOT_W / 2
        inv, exp, diff = float(inv_by_month[i]), float(exp_by_month[i]), float(diffs[i])
        inv_y, exp_y = _y(inv), _y(exp)
        chart_bars.append({
            "inv_x": cx - BAR_W - BAR_GAP / 2, "inv_y": inv_y, "inv_h": max(0.0, zero_y - inv_y),
            "exp_x": cx + BAR_GAP / 2,          "exp_y": exp_y, "exp_h": max(0.0, zero_y - exp_y),
            "bar_w": BAR_W, "cx": cx, "diff_y": _y(diff),
            "name": months[i], "short": months_short[i],
            "invoices": inv, "expenses": exp, "diff": diff,
        })
        line_pts.append(f"{cx:.1f},{_y(diff):.1f}")
    chart = {
        "width":    SLOT_W * 12,
        "height":   PLOT_TOP + PLOT_H + 22,
        "zero_y":   zero_y,
        "label_y":  PLOT_TOP + PLOT_H + 15,
        "bars":     chart_bars,
        "line":     " ".join(line_pts),
        "has_data": axis_max > 0 or axis_min < 0,
    }

    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav    = "stats",
        rows          = rows,
        totals        = totals,
        chart         = chart,
        years         = years,
        selected_year = year,
        date_field    = date_field,
    )
    return templates.TemplateResponse(request, "statistiky.html", ctx)


# ── GET /dane  (Daňové přiznání helper) ───────────────────────────────────────

@app.get("/dane", tags=["ui"], include_in_schema=False)
def ui_tax(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    year: Optional[int] = None,
    date_field: str = "paid",
    expense_mode: str = "real",
    flat_rate: int = DEFAULT_FLAT_RATE_PERCENT,
) -> Response:
    """Estimate the yearly income-tax position of an OSVČ.

    Income is the sum of invoices for the year (by ``date_paid`` of paid
    invoices — the cash-basis default — or by ``date_issued`` via the toggle).
    Expenses are either the real logged expenses (``expense_mode="real"``) or a
    flat-rate paušál (``expense_mode="flat"`` at ``flat_rate`` percent). The tax
    base is compared against the admin-set rozhodná částka for the year to flag
    whether social-insurance participation arises for a secondary activity.
    """
    if date_field not in ("issued", "paid"):
        date_field = "paid"
    if expense_mode not in ("real", "flat"):
        expense_mode = "real"
    if flat_rate not in EXPENSE_FLAT_RATE_CAPS:
        flat_rate = DEFAULT_FLAT_RATE_PERCENT
    paid_only = date_field == "paid"
    inv_col = Invoice.date_paid if paid_only else Invoice.date_issued

    # Years present on invoices (issue + paid dates) and expenses.
    inv_pairs = session.exec(
        select(Invoice.date_issued, Invoice.date_paid).where(Invoice.user_id == _user.id)
    ).all()
    exp_dates = session.exec(
        select(Expense.date).where(Expense.user_id == _user.id)
    ).all()
    years = sorted(
        {d.year for pair in inv_pairs for d in pair if d is not None}
        | {d.year for d in exp_dates},
        reverse=True,
    )
    if year is None:
        year = date.today().year

    inv_stmt = select(Invoice).where(Invoice.user_id == _user.id)
    if paid_only:
        inv_stmt = inv_stmt.where(Invoice.is_paid == True)  # noqa: E712
    invoices = session.exec(
        inv_stmt
        .where(inv_col >= date(year, 1, 1))
        .where(inv_col <= date(year, 12, 31))
    ).all()
    expenses = session.exec(
        select(Expense)
        .where(Expense.user_id == _user.id)
        .where(Expense.date >= date(year, 1, 1))
        .where(Expense.date <= date(year, 12, 31))
    ).all()

    income = sum((Decimal(str(inv.amount)) for inv in invoices), Decimal("0"))
    real_expenses = sum((Decimal(str(exp.amount)) for exp in expenses), Decimal("0"))

    setting = session.exec(
        select(TaxSetting).where(TaxSetting.year == year)
    ).first()
    social_threshold = setting.social_threshold_secondary if setting else None
    # Rates: use the year's admin-set values when a row exists, otherwise the
    # current config defaults (so estimates still compute for un-configured years).
    tax_rate = setting.income_tax_rate if setting else INCOME_TAX_RATE
    social_assessment_rate = setting.social_assessment_rate if setting else SOCIAL_ASSESSMENT_RATE
    social_insurance_rate = setting.social_insurance_rate if setting else SOCIAL_INSURANCE_RATE
    health_assessment_rate = setting.health_assessment_rate if setting else HEALTH_ASSESSMENT_RATE
    health_insurance_rate = setting.health_insurance_rate if setting else HEALTH_INSURANCE_RATE

    # Whether the user runs the OSVČ as main or secondary activity (from their
    # supplier profile) drives the social-insurance branch.
    profile = session.exec(
        select(SupplierProfile).where(SupplierProfile.user_id == _user.id)
    ).first()
    activity_type = profile.osvc_activity_type if profile else "main"
    if activity_type not in ("main", "secondary"):
        activity_type = "main"

    summary = compute_tax_summary(
        income=income,
        real_expenses=real_expenses,
        expense_mode=expense_mode,
        flat_rate_percent=flat_rate,
        flat_rate_cap=EXPENSE_FLAT_RATE_CAPS[flat_rate],
        social_threshold=social_threshold,
        tax_rate=tax_rate,
        social_assessment_rate=social_assessment_rate,
        social_insurance_rate=social_insurance_rate,
        health_assessment_rate=health_assessment_rate,
        health_insurance_rate=health_insurance_rate,
        activity_type=activity_type,
    )
    # Hand floats to the template (the czk macro formats with %); keep the
    # social verdict as-is.
    summary_view = {
        k: (float(v) if isinstance(v, Decimal) else v) for k, v in summary.items()
    }

    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav      = "tax",
        summary         = summary_view,
        years           = years,
        selected_year   = year,
        date_field      = date_field,
        expense_mode    = expense_mode,
        flat_rate       = flat_rate,
        flat_rates      = EXPENSE_FLAT_RATES,
        has_threshold   = social_threshold is not None,
        activity_type   = activity_type,
        # Rates as percentages, for truthful labels on the page.
        tax_rate_pct        = float(Decimal(str(tax_rate)) * 100),
        assessment_rate_pct = float(Decimal(str(social_assessment_rate)) * 100),
        insurance_rate_pct  = float(Decimal(str(social_insurance_rate)) * 100),
        health_assessment_rate_pct = float(Decimal(str(health_assessment_rate)) * 100),
        health_insurance_rate_pct  = float(Decimal(str(health_insurance_rate)) * 100),
    )
    return templates.TemplateResponse(request, "dane.html", ctx)


# ── GET /faktury/nova ─────────────────────────────────────────────────────────
# MUST be defined before /faktury/{invoice_id} so "nova" isn't parsed as an int.

@app.get("/faktury/nova", tags=["ui"], include_in_schema=False)
def ui_new_invoice_page(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
) -> Response:
    all_contacts = session.exec(
        select(Contact).where(Contact.user_id == _user.id).order_by(Contact.name)
    ).all()
    today = date.today()

    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav     = "new_invoice",
        contacts       = all_contacts,
        today          = today.isoformat(),
        due_days       = DEFAULT_PAYMENT_DUE_DAYS,
        payment_method = "transfer",
        next_vs        = get_next_variable_symbol(session, _user.id),
    )
    return templates.TemplateResponse(request, "invoice_form.html", ctx)


# ── GET /faktury/{id} ────────────────────────────────────────────────────────

@app.get("/faktury/{invoice_id}", tags=["ui"], include_in_schema=False)
def ui_invoice_detail(
    invoice_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
) -> Response:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.user_id != _user.id:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    # Customer data from the snapshot frozen at creation (same rule as the PDF);
    # legacy invoices without one fall back to the live contact.
    contact = invoice.get_customer_snapshot() or session.get(Contact, invoice.contact_id)
    supplier = invoice.get_supplier_snapshot()
    # Ensure numeric fields are floats so Jinja2 arithmetic works
    items = [
        {**it,
         "quantity":   float(it.get("quantity", 1)),
         "unit_price": float(it.get("unit_price", 0)),
         "vat_rate":   float(it.get("vat_rate", 0))}
        for it in invoice.get_items()
    ]
    # Cross-references: a dobropis links to its corrected invoice; an invoice
    # lists the dobropisy issued against it.
    related_invoice = None
    if invoice.doc_type == "credit_note" and invoice.related_invoice_id:
        candidate = session.get(Invoice, invoice.related_invoice_id)
        if candidate is not None and candidate.user_id == _user.id:
            related_invoice = candidate
    credit_notes = session.exec(
        select(Invoice).where(Invoice.related_invoice_id == invoice.id)
        .order_by(Invoice.variable_symbol)
    ).all() if invoice.doc_type == "invoice" else []
    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav      = "",
        invoice         = invoice,
        contact         = contact,
        supplier        = supplier,
        items           = items,
        related_invoice = related_invoice,
        credit_notes    = credit_notes,
    )
    return templates.TemplateResponse(request, "invoice_detail.html", ctx)


# ── GET /faktury/{id}/upravit ────────────────────────────────────────────────────

@app.get("/faktury/{invoice_id}/upravit", tags=["ui"], include_in_schema=False)
def ui_edit_invoice_page(
    invoice_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
) -> Response:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.user_id != _user.id:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    if invoice.doc_type == "credit_note":
        # Dobropisy are immutable (see update_invoice) — no edit form for them.
        return RedirectResponse(url=f"/faktury/{invoice_id}",
                                status_code=status.HTTP_303_SEE_OTHER)
    all_contacts = session.exec(
        select(Contact).where(Contact.user_id == _user.id).order_by(Contact.name)
    ).all()
    today = date.today()
    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav     = "",
        invoice        = invoice,
        contacts       = all_contacts,
        today          = today.isoformat(),
        due_days       = (invoice.date_due - invoice.date_issued).days,
        payment_method = invoice.payment_method,
    )
    return templates.TemplateResponse(request, "invoice_form.html", ctx)


# ── GET /faktury/{id}/duplikovat  (new invoice prefilled from an old one) ──────

@app.get("/faktury/{invoice_id}/duplikovat", tags=["ui"], include_in_schema=False)
def ui_duplicate_invoice_page(
    invoice_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
) -> Response:
    """New-invoice form prefilled from an existing invoice (recurring billing).

    Purely a form prefill: client, items, payment term/method and notes are
    copied; the date is today and the number is the next one in the series.
    Saving goes through the ordinary POST /invoices, so nothing is shared
    with the original (fresh snapshots, amount recomputed server-side).
    """
    original = session.get(Invoice, invoice_id)
    if original is None or original.user_id != _user.id:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    if original.doc_type != "invoice":
        return RedirectResponse(url=f"/faktury/{invoice_id}",
                                status_code=status.HTTP_303_SEE_OTHER)

    all_contacts = session.exec(
        select(Contact).where(Contact.user_id == _user.id).order_by(Contact.name)
    ).all()
    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav      = "new_invoice",
        invoice         = None,
        copy_of         = original,
        contacts        = all_contacts,
        prefilled_items = original.get_items(),
        today           = date.today().isoformat(),
        due_days        = (original.date_due - original.date_issued).days,
        payment_method  = original.payment_method,
        next_vs         = get_next_variable_symbol(session, _user.id),
    )
    return templates.TemplateResponse(request, "invoice_form.html", ctx)


# ── GET /faktury/{id}/dobropis  (dobropis form) ──────────────────────────

@app.get("/faktury/{invoice_id}/dobropis", tags=["ui"], include_in_schema=False)
def ui_new_credit_note_page(
    invoice_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
) -> Response:
    """Dobropis form, prefilled with the corrected invoice's items negated.

    Reuses invoice_form.html in credit-note mode (`credit_note_of` set): the
    contact is fixed to the original's, the number comes read-only from the
    dobropis series, and the line items stay editable for partial refunds.
    """
    original = session.get(Invoice, invoice_id)
    if original is None or original.user_id != _user.id:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    if original.doc_type != "invoice":
        return RedirectResponse(url=f"/faktury/{invoice_id}",
                                status_code=status.HTTP_303_SEE_OTHER)

    contact = session.get(Contact, original.contact_id)
    prefilled = [
        {**it, "unit_price": -float(it.get("unit_price", 0))}
        for it in original.get_items()
    ]
    today = date.today()
    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav      = "",
        invoice         = None,
        credit_note_of  = original,
        contact_name    = contact.name if contact else "—",
        prefilled_items = prefilled,
        next_vs         = get_next_credit_note_symbol(session, _user.id),
        today           = today.isoformat(),
        due_days        = DEFAULT_PAYMENT_DUE_DAYS,
        payment_method  = original.payment_method,
    )
    return templates.TemplateResponse(request, "invoice_form.html", ctx)


# ── POST /ui/faktury/{id}/prepnout-zaplaceno  (form button) ────────────────────────

@app.post("/ui/faktury/{invoice_id}/prepnout-zaplaceno", tags=["ui"], include_in_schema=False)
def ui_toggle_paid(
    invoice_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    date_paid: Optional[str] = Form(None),
    redirect_to: str = Form("/"),
) -> Response:
    """Toggle paid state from dashboard or detail page then redirect back."""
    redirect_to = _safe_redirect_target(redirect_to)

    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.user_id != _user.id:
        raise HTTPException(status_code=404, detail="Invoice not found.")

    if invoice.is_paid:
        invoice.is_paid   = False
        invoice.date_paid = None
    else:
        invoice.is_paid = True
        if date_paid:
            try:
                invoice.date_paid = date.fromisoformat(date_paid)
            except ValueError:
                invoice.date_paid = date.today()
        else:
            invoice.date_paid = date.today()

    session.add(invoice)
    session.commit()
    return RedirectResponse(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)


# ── GET /naklady/novy ─────────────────────────────────────────────────────────

@app.get("/naklady/novy", tags=["ui"], include_in_schema=False)
def ui_new_expense_page(
    request: Request,
    _user: User = Depends(_get_ui_user),
) -> Response:
    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav = "new_expense",
        today      = date.today().isoformat(),
    )
    return templates.TemplateResponse(request, "expense_form.html", ctx)


# ── POST /ui/naklady/novy  (form submit) ──────────────────────────────────────

# _ALLOWED_SCAN_TYPES is imported from app.uploads_util.
_SCAN_EXT_MAP = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}

@app.post("/ui/naklady/novy", tags=["ui"], include_in_schema=False)
async def ui_create_expense(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    date_str: str  = Form(..., alias="date"),
    category: str  = Form(...),
    amount: float  = Form(...),
    description: str = Form(...),
    scan: UploadFile = File(None),
) -> Response:
    """Handle the HTML expense form submission and redirect to dashboard."""
    def _render(error: str) -> Response:
        ctx = _base_ctx(request, _user)
        ctx.update(
            active_nav  = "new_expense",
            today       = date_str,
            flash_error = error,
        )
        return templates.TemplateResponse(request, "expense_form.html", ctx, status_code=422)

    if amount <= 0:
        return _render("Částka musí být větší než nula.")
    try:
        parsed_date = date.fromisoformat(date_str)
    except ValueError:
        return _render("Neplatné datum.")

    # Validate the optional scan up-front (mirroring the edit handler) so a
    # rejected file never leaves a half-created expense behind — and an
    # unsupported format is a visible error, not a silently dropped receipt.
    scan_data: Optional[bytes] = None
    scan_ext: Optional[str] = None
    if scan and scan.filename:
        content_type = scan.content_type or ""
        if content_type not in _ALLOWED_SCAN_TYPES:
            return _render("Nepodporovaný formát dokladu. Použijte JPG, PNG nebo PDF.")
        scan_data = await _read_upload_or_413(scan, _MAX_SCAN_BYTES)
        if not _content_matches_type(scan_data, content_type):
            return _render("Obsah souboru neodpovídá uvedenému formátu.")
        scan_ext = _SCAN_EXT_MAP[content_type]

    expense = Expense(
        user_id     = _user.id,
        date        = parsed_date,
        category    = category.strip(),
        amount      = float(Decimal(str(amount))),
        # Locked server-side; the form's hidden currency field is not a guard.
        currency    = "CZK",
        description = description.strip(),
    )
    session.add(expense)
    session.commit()
    session.refresh(expense)

    if scan_data is not None:
        scan_dir = _UPLOADS_DIR / str(_user.id) / "expenses"
        scan_dir.mkdir(parents=True, exist_ok=True)
        dest = scan_dir / f"{expense.id}.{scan_ext}"
        dest.write_bytes(scan_data)
        expense.attachment_path = str(dest)
        session.add(expense)
        session.commit()

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


# ── GET /naklady/{id}/upravit ────────────────────────────────────────────────────

@app.get("/naklady/{expense_id}/upravit", tags=["ui"], include_in_schema=False)
def ui_edit_expense_page(
    expense_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    redirect_to: str = "/",
) -> Response:
    expense = session.get(Expense, expense_id)
    if expense is None or expense.user_id != _user.id:
        raise HTTPException(status_code=404, detail="Expense not found.")
    ctx = _base_ctx(request, _user)
    ctx.update(
        active_nav  = "",
        expense     = expense,
        today       = expense.date.isoformat(),
        redirect_to = _safe_redirect_target(redirect_to),
    )
    return templates.TemplateResponse(request, "expense_form.html", ctx)


# ── POST /ui/naklady/{id}/upravit  (form submit) ─────────────────────────────────

@app.post("/ui/naklady/{expense_id}/upravit", tags=["ui"], include_in_schema=False)
async def ui_update_expense(
    expense_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    date_str: str  = Form(..., alias="date"),
    category: str  = Form(...),
    amount: float  = Form(...),
    description: str = Form(...),
    scan: UploadFile = File(None),
    redirect_to: str = Form("/"),
) -> Response:
    """Update an existing expense and optionally replace its scan attachment.

    The stored currency is never touched: it stays CZK for new rows and keeps
    its original value on legacy non-CZK rows.
    """
    expense = session.get(Expense, expense_id)
    if expense is None or expense.user_id != _user.id:
        raise HTTPException(status_code=404)

    def _render(error: str) -> Response:
        ctx = _base_ctx(request, _user)
        ctx.update(
            active_nav  = "",
            expense     = expense,
            today       = expense.date.isoformat(),
            flash_error = error,
            redirect_to = _safe_redirect_target(redirect_to),
        )
        return templates.TemplateResponse(request, "expense_form.html", ctx, status_code=422)

    if amount <= 0:
        return _render("Částka musí být větší než nula.")
    try:
        parsed_date = date.fromisoformat(date_str)
    except ValueError:
        return _render("Neplatné datum.")

    # Validate the optional replacement scan up-front so a bad file never leaves
    # the row half-updated.
    scan_data: Optional[bytes] = None
    scan_ext: Optional[str] = None
    if scan and scan.filename:
        content_type = scan.content_type or ""
        if content_type not in _ALLOWED_SCAN_TYPES:
            return _render("Nepodporovaný formát dokladu. Použijte JPG, PNG nebo PDF.")
        scan_data = await _read_upload_or_413(scan, _MAX_SCAN_BYTES)
        if not _content_matches_type(scan_data, content_type):
            return _render("Obsah souboru neodpovídá uvedenému formátu.")
        scan_ext = _SCAN_EXT_MAP[content_type]

    expense.date        = parsed_date
    expense.category    = category.strip()
    expense.amount      = float(Decimal(str(amount)))
    expense.description = description.strip()

    if scan_data is not None:
        scan_dir = _UPLOADS_DIR / str(_user.id) / "expenses"
        scan_dir.mkdir(parents=True, exist_ok=True)
        # Drop any previous scan (it may have had a different extension).
        for old in scan_dir.glob(f"{expense.id}.*"):
            old.unlink(missing_ok=True)
        dest = scan_dir / f"{expense.id}.{scan_ext}"
        dest.write_bytes(scan_data)
        expense.attachment_path = str(dest)

    session.add(expense)
    session.commit()
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=status.HTTP_303_SEE_OTHER)


# ── POST /ui/naklady/{id}/nahrat-sken ───────────────────────────────────────

@app.post("/ui/naklady/{expense_id}/nahrat-sken", tags=["ui"], include_in_schema=False)
async def ui_upload_expense_scan(
    expense_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    scan: UploadFile = File(...),
    redirect_to: str = Form("/"),
) -> Response:
    """Upload or replace the scan attachment for an existing expense."""
    expense = session.get(Expense, expense_id)
    if expense is None or expense.user_id != _user.id:
        raise HTTPException(status_code=404)

    content_type = scan.content_type or ""
    if content_type not in _ALLOWED_SCAN_TYPES:
        raise HTTPException(status_code=400, detail="Nepodporovaný formát. Použijte JPG, PNG nebo PDF.")

    data = await _read_upload_or_413(scan, _MAX_SCAN_BYTES)
    if not _content_matches_type(data, content_type):
        raise HTTPException(status_code=400, detail="Obsah souboru neodpovídá uvedenému formátu.")

    ext = _SCAN_EXT_MAP[content_type]
    scan_dir = _UPLOADS_DIR / str(_user.id) / "expenses"
    scan_dir.mkdir(parents=True, exist_ok=True)

    # Remove any previous scan for this expense
    for old in scan_dir.glob(f"{expense_id}.*"):
        old.unlink(missing_ok=True)

    dest = scan_dir / f"{expense_id}.{ext}"
    dest.write_bytes(data)

    expense.attachment_path = str(dest)
    session.add(expense)
    session.commit()

    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=status.HTTP_303_SEE_OTHER)


# ── POST /ui/naklady/{id}/smazat-sken ───────────────────────────────────────

@app.post("/ui/naklady/{expense_id}/smazat-sken", tags=["ui"], include_in_schema=False)
def ui_delete_expense_scan(
    expense_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    redirect_to: str = Form("/"),
) -> Response:
    """Remove the scan attachment from an expense."""
    expense = session.get(Expense, expense_id)
    if expense is None or expense.user_id != _user.id:
        raise HTTPException(status_code=404)

    if expense.attachment_path:
        Path(expense.attachment_path).unlink(missing_ok=True)
        expense.attachment_path = None
        session.add(expense)
        session.commit()

    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=status.HTTP_303_SEE_OTHER)


# ── GET /kontakty ────────────────────────────────────────────────────────────

@app.get("/kontakty", tags=["ui"], include_in_schema=False)
def ui_contacts_list(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
) -> Response:
    all_contacts = session.exec(
        select(Contact).where(Contact.user_id == _user.id).order_by(Contact.name)
    ).all()
    ctx = _base_ctx(request, _user)
    ctx.update(active_nav="contacts", contacts=all_contacts)
    return templates.TemplateResponse(request, "contacts.html", ctx)


# ── GET /kontakty/novy ─────────────────────────────────────────────────────────

@app.get("/kontakty/novy", tags=["ui"], include_in_schema=False)
def ui_new_contact_page(
    request: Request,
    _user: User = Depends(_get_ui_user),
) -> Response:
    ctx = _base_ctx(request, _user)
    ctx.update(active_nav="contacts", contact=None)
    return templates.TemplateResponse(request, "contact_form.html", ctx)


# ── POST /ui/kontakty/novy ─────────────────────────────────────────────────────

def _render_contact_form_error(
    request: Request, user: User, error: str, fields: dict, contact: Optional[Contact] = None,
) -> Response:
    """Re-render the contact form with an error banner and the submitted values."""
    ctx = _base_ctx(request, user)
    ctx.update(
        active_nav="contacts",
        contact=contact,                       # present → edit URL; None → new URL
        form=SimpleNamespace(**fields),        # repopulates inputs
        form_error=error,
    )
    return templates.TemplateResponse(request, "contact_form.html", ctx, status_code=422)


def _parse_due_days(raw: str) -> tuple[Optional[str], Optional[int]]:
    """Parse the optional payment-term form field. Returns (error, value)."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    if not raw.isdigit() or int(raw) < 1:
        return "Splatnost musí být kladný počet dní.", None
    return None, int(raw)


@app.post("/ui/kontakty/novy", tags=["ui"], include_in_schema=False)
def ui_create_contact(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    name: str         = Form(...),
    address: str      = Form(...),
    ic: str           = Form(...),
    dic: str          = Form(""),
    bank_prefix: str  = Form(""),
    bank_number: str  = Form(""),
    bank_code: str    = Form(""),
    bank_iban: str    = Form(""),
    email: str        = Form(""),
    payment_due_days: str = Form(""),
) -> Response:
    fields = dict(
        name=name.strip(), address=address.strip(), ic=ic.strip(), dic=dic.strip(),
        bank_prefix=bank_prefix.strip(), bank_number=bank_number.strip(),
        bank_code=bank_code.strip(), bank_iban=bank_iban.strip(), email=email.strip(),
        payment_due_days=payment_due_days.strip(),
    )
    due_err, due_days = _parse_due_days(payment_due_days)
    if due_err:
        return _render_contact_form_error(request, _user, due_err, fields)
    error, computed_iban = validate_party_bank_and_ico(
        ic=ic, prefix=bank_prefix, number=bank_number, code=bank_code, iban=bank_iban,
    )
    if error:
        return _render_contact_form_error(request, _user, error, fields)

    contact = Contact(
        user_id=_user.id,
        name=name.strip(),
        address=address.strip(),
        ic=ic.strip(),
        dic=dic.strip() or None,
        payment_due_days=due_days,
        bank_prefix=bank_prefix.strip() or None,
        bank_number=bank_number.strip() or None,
        bank_code=bank_code.strip() or None,
        bank_iban=computed_iban or None,
        email=email.strip() or None,
    )
    session.add(contact)
    session.commit()
    return RedirectResponse(url="/kontakty", status_code=status.HTTP_303_SEE_OTHER)


# ── GET /kontakty/{id}/upravit ───────────────────────────────────────────────────

@app.get("/kontakty/{contact_id}/upravit", tags=["ui"], include_in_schema=False)
def ui_edit_contact_page(
    contact_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
) -> Response:
    contact = session.get(Contact, contact_id)
    if contact is None or contact.user_id != _user.id:
        raise HTTPException(status_code=404, detail="Contact not found.")
    ctx = _base_ctx(request, _user)
    ctx.update(active_nav="contacts", contact=contact)
    return templates.TemplateResponse(request, "contact_form.html", ctx)


# ── POST /ui/kontakty/{id}/upravit ───────────────────────────────────────────────

@app.post("/ui/kontakty/{contact_id}/upravit", tags=["ui"], include_in_schema=False)
def ui_update_contact(
    contact_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    name: str         = Form(...),
    address: str      = Form(...),
    ic: str           = Form(...),
    dic: str          = Form(""),
    bank_prefix: str  = Form(""),
    bank_number: str  = Form(""),
    bank_code: str    = Form(""),
    bank_iban: str    = Form(""),
    email: str        = Form(""),
    payment_due_days: str = Form(""),
) -> Response:
    contact = session.get(Contact, contact_id)
    if contact is None or contact.user_id != _user.id:
        raise HTTPException(status_code=404, detail="Contact not found.")

    fields = dict(
        name=name.strip(), address=address.strip(), ic=ic.strip(), dic=dic.strip(),
        bank_prefix=bank_prefix.strip(), bank_number=bank_number.strip(),
        bank_code=bank_code.strip(), bank_iban=bank_iban.strip(), email=email.strip(),
        payment_due_days=payment_due_days.strip(),
    )
    due_err, due_days = _parse_due_days(payment_due_days)
    if due_err:
        return _render_contact_form_error(request, _user, due_err, fields, contact=contact)
    error, computed_iban = validate_party_bank_and_ico(
        ic=ic, prefix=bank_prefix, number=bank_number, code=bank_code, iban=bank_iban,
    )
    if error:
        return _render_contact_form_error(request, _user, error, fields, contact=contact)

    contact.name        = name.strip()
    contact.address     = address.strip()
    contact.ic          = ic.strip()
    contact.dic         = dic.strip() or None
    contact.payment_due_days = due_days
    contact.bank_prefix = bank_prefix.strip() or None
    contact.bank_number = bank_number.strip() or None
    contact.bank_code   = bank_code.strip() or None
    contact.bank_iban   = computed_iban or None
    contact.email       = email.strip() or None
    session.add(contact)
    session.commit()
    return RedirectResponse(url="/kontakty", status_code=status.HTTP_303_SEE_OTHER)


# ── POST /ui/kontakty/{id}/smazat ────────────────────────────────────────────

@app.post("/ui/kontakty/{contact_id}/smazat", tags=["ui"], include_in_schema=False)
def ui_delete_contact(
    contact_id: int,
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
) -> Response:
    contact = session.get(Contact, contact_id)
    if contact is None or contact.user_id != _user.id:
        raise HTTPException(status_code=404, detail="Contact not found.")

    # A contact referenced by invoices must stay: SQLite does not enforce the
    # FK here, and deleting it would orphan the invoices (their list rows and
    # any pre-snapshot PDF lose the customer, and a backup export would no
    # longer import).
    invoice_count = len(session.exec(
        select(Invoice.id)
        .where(Invoice.user_id == _user.id)
        .where(Invoice.contact_id == contact_id)
    ).all())
    if invoice_count:
        all_contacts = session.exec(
            select(Contact).where(Contact.user_id == _user.id).order_by(Contact.name)
        ).all()
        ctx = _base_ctx(request, _user)
        ctx.update(
            active_nav="contacts",
            contacts=all_contacts,
            flash_error=(
                f"Kontakt „{contact.name}“ nelze smazat – "
                f"je odběratelem na fakturách (počet: {invoice_count})."
            ),
        )
        return templates.TemplateResponse(
            request, "contacts.html", ctx, status_code=status.HTTP_409_CONFLICT
        )

    session.delete(contact)
    session.commit()
    return RedirectResponse(url="/kontakty", status_code=status.HTTP_303_SEE_OTHER)


# ── POST /ui/naklady/{id}/smazat  (form button) ──────────────────────────────

@app.post("/ui/naklady/{expense_id}/smazat", tags=["ui"], include_in_schema=False)
def ui_delete_expense(
    expense_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    redirect_to: str = Form("/"),
) -> Response:
    expense = session.get(Expense, expense_id)
    if expense is None or expense.user_id != _user.id:
        raise HTTPException(status_code=404, detail="Expense not found.")
    session.delete(expense)
    session.commit()
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=status.HTTP_303_SEE_OTHER)


# ── POST /ui/faktury/{id}/smazat  (form button) ──────────────────────────────

@app.post("/ui/faktury/{invoice_id}/smazat", tags=["ui"], include_in_schema=False)
def ui_delete_invoice(
    invoice_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_user),
    redirect_to: str = Form("/"),
) -> Response:
    """Delete an unpaid invoice, then redirect back to the originating page.

    Paid invoices are refused (409) — the UI hides the button for them, so this
    guard only fires on a stale or forged form. Mirrors DELETE /invoices/{id}.
    """
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.user_id != _user.id:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    if invoice.is_paid:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Zaplacenou fakturu nelze smazat – nejprve ji označte jako nezaplacenou.",
        )
    session.delete(invoice)
    session.commit()
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=status.HTTP_303_SEE_OTHER)


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN – User management  (is_admin required)
# ══════════════════════════════════════════════════════════════════════════════

# Default rate percentages (from config) used to pre-fill the admin tax form.
_DEFAULT_TAX_RATE_PCTS = {
    "income_tax":        float(Decimal(INCOME_TAX_RATE) * 100),
    "assessment":        float(Decimal(SOCIAL_ASSESSMENT_RATE) * 100),
    "insurance":         float(Decimal(SOCIAL_INSURANCE_RATE) * 100),
    "health_assessment": float(Decimal(HEALTH_ASSESSMENT_RATE) * 100),
    "health_insurance":  float(Decimal(HEALTH_INSURANCE_RATE) * 100),
}


def _get_ui_admin(
    _user: User = Depends(_get_ui_user),
) -> User:
    """Like _get_ui_user but additionally requires is_admin."""
    if not _user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only.")
    return _user


# ── GET /admin/uzivatele ──────────────────────────────────────────────────────────

@app.get("/admin/uzivatele", tags=["ui"], include_in_schema=False)
def ui_admin_users(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_admin),
) -> Response:
    users = session.exec(select(User).order_by(User.id)).all()
    tax_settings = session.exec(select(TaxSetting).order_by(TaxSetting.year.desc())).all()
    ctx = _base_ctx(request, _user)
    ctx.update(active_nav="admin", users=users, tax_settings=tax_settings,
               default_rate_pcts=_DEFAULT_TAX_RATE_PCTS)
    return templates.TemplateResponse(request, "admin_users.html", ctx)


# ── POST /ui/admin/uzivatele/novy ─────────────────────────────────────────────────

@app.post("/ui/admin/uzivatele/novy", tags=["ui"], include_in_schema=False)
def ui_admin_create_user(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_admin),
    username: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(False),
) -> Response:
    username = username.strip().lower()
    if len(username) < 3:
        return _admin_users_response(
            request, _user, session,
            error="Uživatelské jméno musí mít alespoň 3 znaky.", code=422)

    policy_error = validate_password_policy(password)
    if policy_error:
        return _admin_users_response(request, _user, session, error=policy_error, code=422)

    existing = session.exec(select(User).where(User.username == username)).first()
    if existing:
        return _admin_users_response(
            request, _user, session,
            error=f"Uživatel '{username}' již existuje.", code=422)

    new_user = User(
        username=username,
        hashed_password=get_password_hash(password),
        is_admin=is_admin,
        is_active=True,
    )
    session.add(new_user)
    session.commit()
    session.refresh(new_user)

    # Create blank supplier profile for the new user
    profile = SupplierProfile(user_id=new_user.id)
    session.add(profile)
    session.commit()

    return RedirectResponse(url="/admin/uzivatele", status_code=status.HTTP_303_SEE_OTHER)


# ── POST /ui/admin/reset-hesla  (admin recovery for a forgotten password) ──

@app.post("/ui/admin/reset-hesla", tags=["ui"], include_in_schema=False)
def ui_admin_reset_password(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_admin),
    target_id: int = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(""),
) -> Response:
    """Set a new password for a user who has forgotten theirs (admin-only).

    This is the recovery path. Unlike the self-service change on /ui/account it
    does NOT require the user's current password. Bumping token_version
    immediately invalidates that user's outstanding sessions. Admins cannot
    reset their own password here — they use /account, which verifies the
    current password (and if an admin has locked themselves out, another admin
    performs the reset).
    """
    target = session.get(User, target_id)
    if target is None:
        return _admin_users_response(
            request, _user, session, error="Cílový uživatel neexistuje.", code=404)
    if target.id == _user.id:
        return _admin_users_response(
            request, _user, session,
            error="Vlastní heslo změňte na stránce Účet (vyžaduje současné heslo).",
            code=400)

    policy_error = validate_password_policy(new_password)
    if policy_error:
        return _admin_users_response(request, _user, session, error=policy_error, code=422)
    if new_password != confirm_password:
        return _admin_users_response(
            request, _user, session,
            error="Nové heslo a potvrzení se neshodují.", code=422)

    target.hashed_password = get_password_hash(new_password)
    # The password just changed out from under any live session: bump the
    # version so all of the target's existing API/UI tokens stop working.
    target.token_version += 1
    session.add(target)
    session.commit()
    return _admin_users_response(
        request, _user, session,
        success=f"Heslo uživatele '{target.username}' bylo obnoveno.")


# ── POST /ui/admin/danova-nastaveni  (per-year tax parameters for all users) ──────

@app.post("/ui/admin/danova-nastaveni", tags=["ui"], include_in_schema=False)
def ui_admin_tax_settings(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_admin),
    year: int = Form(...),
    social_threshold_secondary: float = Form(...),
    income_tax_rate_pct: float = Form(...),
    social_assessment_rate_pct: float = Form(...),
    social_insurance_rate_pct: float = Form(...),
    health_assessment_rate_pct: float = Form(...),
    health_insurance_rate_pct: float = Form(...),
) -> Response:
    """Create or update the system-wide tax parameters for a calendar year.

    Holds the rozhodná částka for participation in důchodové pojištění on a
    secondary activity (vedlejší činnost) plus the rates that may change by
    government decision (income tax, social + health assessment-base % and
    premium %). Rates are entered as percentages and stored as fractions.
    Applies to all users; one row per year.
    """
    if year < 2000 or year > 2100:
        return _admin_users_response(
            request, _user, session, error="Zadej platný rok (2000–2100).", code=422)
    if social_threshold_secondary < 0:
        return _admin_users_response(
            request, _user, session,
            error="Rozhodná částka nemůže být záporná.", code=422)
    for label, pct in (
        ("Sazba daně z příjmů", income_tax_rate_pct),
        ("Sociální vyměřovací základ", social_assessment_rate_pct),
        ("Sazba sociálního pojistného", social_insurance_rate_pct),
        ("Zdravotní vyměřovací základ", health_assessment_rate_pct),
        ("Sazba zdravotního pojistného", health_insurance_rate_pct),
    ):
        if pct < 0 or pct > 100:
            return _admin_users_response(
                request, _user, session,
                error=f"{label} musí být v rozmezí 0–100 %.", code=422)

    # Percentages -> fractions.
    income_tax_rate = income_tax_rate_pct / 100
    social_assessment_rate = social_assessment_rate_pct / 100
    social_insurance_rate = social_insurance_rate_pct / 100
    health_assessment_rate = health_assessment_rate_pct / 100
    health_insurance_rate = health_insurance_rate_pct / 100

    existing = session.exec(select(TaxSetting).where(TaxSetting.year == year)).first()
    if existing:
        existing.social_threshold_secondary = social_threshold_secondary
        existing.income_tax_rate = income_tax_rate
        existing.social_assessment_rate = social_assessment_rate
        existing.social_insurance_rate = social_insurance_rate
        existing.health_assessment_rate = health_assessment_rate
        existing.health_insurance_rate = health_insurance_rate
        session.add(existing)
    else:
        session.add(TaxSetting(
            year=year,
            social_threshold_secondary=social_threshold_secondary,
            income_tax_rate=income_tax_rate,
            social_assessment_rate=social_assessment_rate,
            social_insurance_rate=social_insurance_rate,
            health_assessment_rate=health_assessment_rate,
            health_insurance_rate=health_insurance_rate,
        ))
    session.commit()
    return _admin_users_response(
        request, _user, session,
        success=f"Daňové parametry pro rok {year} byly uloženy.")


# ── POST /ui/admin/uzivatele/{id}/prepnout-aktivni ───────────────────────────────────

@app.post("/ui/admin/uzivatele/{target_id}/prepnout-aktivni", tags=["ui"], include_in_schema=False)
def ui_admin_toggle_user(
    target_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_admin),
) -> Response:
    target = session.get(User, target_id)
    if target is None:
        raise HTTPException(status_code=404)
    if target.id == _user.id:
        raise HTTPException(status_code=400, detail="Nemůžeš zablokovat sám sebe.")
    target.is_active = not target.is_active
    # Disabling must take effect immediately, not at token expiry: bump the
    # version so any outstanding API/UI tokens for this account stop working.
    if not target.is_active:
        target.token_version += 1
    session.add(target)
    session.commit()
    return RedirectResponse(url="/admin/uzivatele", status_code=status.HTTP_303_SEE_OTHER)


# ── POST /ui/admin/uzivatele/{id}/smazat ─────────────────────────────────────────

@app.post("/ui/admin/uzivatele/{target_id}/smazat", tags=["ui"], include_in_schema=False)
def ui_admin_delete_user(
    target_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_admin),
) -> Response:
    target = session.get(User, target_id)
    if target is None:
        raise HTTPException(status_code=404)
    if target.id == _user.id:
        raise HTTPException(status_code=400, detail="Nemůžeš smazat vlastní účet.")

    # Delete all data belonging to this user
    for expense in session.exec(select(Expense).where(Expense.user_id == target_id)).all():
        if expense.attachment_path:
            Path(expense.attachment_path).unlink(missing_ok=True)
        session.delete(expense)

    for inv in session.exec(select(Invoice).where(Invoice.user_id == target_id)).all():
        session.delete(inv)

    for contact in session.exec(select(Contact).where(Contact.user_id == target_id)).all():
        session.delete(contact)

    supplier = session.exec(
        select(SupplierProfile).where(SupplierProfile.user_id == target_id)
    ).first()
    if supplier:
        for kind in ("logo", "stamp"):
            path = supplier.logo_path if kind == "logo" else supplier.stamp_path
            if path:
                Path(path).unlink(missing_ok=True)
        session.delete(supplier)

    # Remove user upload directory
    import shutil
    user_upload_dir = _UPLOADS_DIR / str(target_id)
    if user_upload_dir.exists():
        shutil.rmtree(user_upload_dir, ignore_errors=True)

    session.delete(target)
    session.commit()
    return RedirectResponse(url="/admin/uzivatele", status_code=status.HTTP_303_SEE_OTHER)


# ── Data export / import (full per-user backup) ──────────────────────────────
# Admin-only. Export streams a .zip of one user's complete data (DB rows +
# uploaded files). Import restores such a zip into an *empty* target user
# (existing or freshly created) — see app/backup.py for the format & guards.

_MAX_BACKUP_BYTES = 200 * 1024 * 1024    # 200 MB upload cap for an import archive


def _admin_users_response(request: Request, _user: User, session: Session,
                          *, error: str = "", success: str = "",
                          code: int = 200) -> Response:
    """Re-render the admin users page with an optional flash message."""
    users = session.exec(select(User).order_by(User.id)).all()
    tax_settings = session.exec(select(TaxSetting).order_by(TaxSetting.year.desc())).all()
    ctx = _base_ctx(request, _user)
    ctx.update(active_nav="admin", users=users, tax_settings=tax_settings,
               default_rate_pcts=_DEFAULT_TAX_RATE_PCTS)
    if error:
        ctx["flash_error"] = error
    if success:
        ctx["flash_success"] = success
    return templates.TemplateResponse(request, "admin_users.html", ctx, status_code=code)


@app.get("/admin/zaloha/export/{user_id}", tags=["ui"], include_in_schema=False)
def ui_admin_export_backup(
    user_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_admin),
) -> Response:
    """Download a full backup zip of one user's data."""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404)
    archive = export_user(session, target)
    filename = f"fakturace-zaloha-{target.username}-{date.today().isoformat()}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/ui/admin/zaloha/import", tags=["ui"], include_in_schema=False)
async def ui_admin_import_backup(
    request: Request,
    session: Session = Depends(get_session),
    _user: User = Depends(_get_ui_admin),
    target: str = Form(...),               # existing user id, or "__new__"
    new_username: str = Form(""),
    new_password: str = Form(""),
    archive: UploadFile = File(...),
) -> Response:
    """Restore a backup zip into an empty target user (existing or new)."""
    data = await _read_upload_or_413(archive, _MAX_BACKUP_BYTES)

    # ── Resolve / create the target user ──────────────────────────────────────
    created_user: Optional[User] = None
    if target == "__new__":
        username = new_username.strip().lower()
        if len(username) < 3:
            return _admin_users_response(
                request, _user, session,
                error="Uživatelské jméno musí mít alespoň 3 znaky.", code=422)
        policy_error = validate_password_policy(new_password)
        if policy_error:
            return _admin_users_response(request, _user, session, error=policy_error, code=422)
        if session.exec(select(User).where(User.username == username)).first():
            return _admin_users_response(
                request, _user, session,
                error=f"Uživatel '{username}' již existuje.", code=422)
        target_user = User(
            username=username,
            hashed_password=get_password_hash(new_password),
            is_admin=False,
            is_active=True,
        )
        session.add(target_user)
        session.commit()
        session.refresh(target_user)
        session.add(SupplierProfile(user_id=target_user.id))
        session.commit()
        created_user = target_user
    else:
        try:
            target_user = session.get(User, int(target))
        except ValueError:
            target_user = None
        if target_user is None:
            return _admin_users_response(
                request, _user, session, error="Cílový uživatel neexistuje.", code=422)

    # ── Restore ────────────────────────────────────────────────────────────────
    try:
        summary = import_user(session, target_user, data)
    except BackupError as exc:
        # Roll back a freshly created user so a failed import leaves no orphan.
        if created_user is not None:
            orphan_profile = session.exec(
                select(SupplierProfile).where(SupplierProfile.user_id == created_user.id)
            ).first()
            if orphan_profile is not None:
                session.delete(orphan_profile)
            session.delete(created_user)
            session.commit()
        return _admin_users_response(request, _user, session, error=str(exc), code=409)

    return _admin_users_response(
        request, _user, session, success=(
            f"Import do účtu '{target_user.username}' dokončen: "
            f"{summary['contacts']} kontaktů, {summary['invoices']} faktur, "
            f"{summary['expenses']} nákladů."
        ))

