# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Fakturace** — a lightweight, self-hosted invoice & expense manager for Czech freelancers (FastAPI + SQLModel + SQLite). It generates Czech-format invoice PDFs with SPAYD payment QR codes and exports annual accounting CSVs. UI text and business rules are Czech-specific (IČO/DIČ, variable symbol, živnostenský rejstřík, Czech bank account format).

## Commands

Run everything through the venv (`.venv/`).

```bash
# Tests (pytest + httpx; each test gets an isolated temp SQLite DB)
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest tests/test_security.py -q     # single file

# Development server (auto-reload, /docs + /redoc enabled when APP_ENV != production)
.venv/bin/uvicorn app.main:app --reload --port 8000

# One-time DB + master-user bootstrap (idempotent). Prompts for password,
# or pass ADMIN_PASSWORD=... (min 12 chars).
.venv/bin/python -m app.init_db

# Install deps
.venv/bin/pip install -r requirements.txt
```

Production runs under Gunicorn with UvicornWorker over a Unix socket behind Nginx — see [deploy/SERVER_SETUP.md](deploy/SERVER_SETUP.md) and [deploy/faktury.service](deploy/faktury.service).

## Configuration

Settings come from environment variables (loaded via systemd `EnvironmentFile` in prod, or your shell in dev) — see [app/config.py](app/config.py). `SECRET_KEY` is **required** in production (the app refuses to start without it); in dev a random per-process key is generated, which invalidates sessions on every restart. `APP_ENV=production` also disables `/docs` + `/redoc` and enables the Secure cookie flag. Template: [deploy/.env.example](deploy/.env.example).

Note: supplier details now live in the `SupplierProfile` DB table (per user), not in environment variables. `supplier.json` and the `SUPPLIER_*` env vars are legacy (the `supplier.json` file has been removed).

## Architecture

Three layers, deliberately kept separate:

- **[app/main.py](app/main.py)** (~1200 lines) — the app factory plus all **HTML UI routes** (Jinja2-rendered pages under `/...` and form POST handlers under `/ui/...`) and the auth endpoints (`/token`, `/login`, `/logout`). Keep JSON API logic *out* of here.
- **[app/routers/](app/routers/)** — the **JSON API** (contacts, invoices, expenses, export). Mounted in `main.py`. Note the prefix asymmetry: `contacts` is included with `prefix="/api"` (→ `/api/contacts`), while invoices/expenses/export keep their own router prefixes (→ `/invoices`, `/expenses`, `/export`).
- **[app/logic.py](app/logic.py)** — framework-free **business logic**: variable-symbol generation, Czech bank-account mod-11 validation, SPAYD QR generation, and PDF rendering (Jinja2 → HTML → WeasyPrint). Import from here; don't reimplement.
- **[app/backup.py](app/backup.py)** — framework-free **backup/restore** logic (`export_user` / `import_user`). See "Data export & import" below.

[app/models.py](app/models.py) holds SQLModel tables; [app/schemas.py](app/schemas.py) holds Pydantic request/response models; [app/auth.py](app/auth.py) holds auth primitives and dependencies. [app/uploads_util.py](app/uploads_util.py) is the single source of truth for the uploads dir, size caps, allowed MIME types, and the magic-byte sniff (`content_matches_type`) — imported by both `main.py`'s upload handlers and `backup.py`'s import validation so they share identical defenses; don't redefine these constants.

### Authentication — two dependencies, pick the right one

Auth accepts **both** an `Authorization: Bearer` header (header wins) and an `access_token` HttpOnly cookie set by `/login`. There are two dependencies:

- `get_current_user` ([app/auth.py](app/auth.py)) — for **JSON API** routes; raises HTTP 401.
- `_get_ui_user` ([app/main.py](app/main.py)) — for **HTML page** routes; redirects unauthenticated browsers to `/login` (307) instead of 401.

Passwords are bcrypt (direct `bcrypt`, **not** passlib — passlib is unmaintained and breaks with bcrypt ≥4). JWT is HS256.

Both dependencies funnel through `resolve_user_from_token` ([app/auth.py](app/auth.py)), which enforces three things on every request: valid signature/expiry, `is_active`, and that the token's `ver` claim matches the user's `token_version`. **Mint tokens only via `create_access_token_for_user`** so the `ver` claim is embedded. Bump `User.token_version` whenever outstanding sessions must die (password change, account disable) — that is the revocation mechanism (there is no server-side session store). Password rules live in one place: `validate_password_policy` (min 12 chars, ≤72 bytes); use it at every site that sets a password.

**CSRF:** a middleware (`csrf_same_origin` in [app/main.py](app/main.py)) requires a same-origin `Origin`/`Referer` for unsafe methods authenticated by cookie. Bearer-token (API) requests are exempt. Tests that POST with the cookie must send `Origin: http://testserver`.

**Uploads:** never trust the client `Content-Type`. Route every upload through `_read_upload_or_413` (size cap, bounded read) + `_content_matches_type` (magic-byte sniff); files are written only under `app/uploads/<user_id>/`.

### Per-user data isolation

Every domain table (`Contact`, `Invoice`, `Expense`, `SupplierProfile`) has a `user_id` FK. All queries must scope by the authenticated user's id — follow the existing pattern in the routers; don't write queries that can read another user's rows. `is_admin` users manage other users via `/admin/uzivatele` (create, block/unblock, delete, full backup export/import, and **password reset**).

**Admin password reset** (`POST /ui/admin/reset-hesla` in [app/main.py](app/main.py)) is the forgotten-password recovery path: unlike the self-service change on `/ui/ucet`, it does **not** require the user's current password. It enforces `validate_password_policy` + a confirmation match, and **bumps the target's `token_version`** so their outstanding sessions die immediately. Admins **cannot** reset their own password here (returns 400) — they use `/ucet`, which verifies the current password; a locked-out admin is reset by another admin. The reset card on [admin_users.html](app/templates/admin_users.html) excludes the current admin from its user dropdown.

**Invariant:** any helper that fetches a row by id must also verify ownership and raise 404 (never 403) on mismatch — see `_get_contact_or_404`/`_get_invoice_or_404` in [app/routers/invoices.py](app/routers/invoices.py). A `session.get(Model, id)` without a following `user_id` check is a cross-tenant leak.

**Never read a client-supplied filesystem path.** PDF rendering embeds images by reading files off disk (`_image_to_data_uri` in [app/logic.py](app/logic.py)), so any path that reaches it must originate from a confined source (the authenticated upload handlers write only under `app/uploads/<user_id>/`). The invoice stamp comes solely from the supplier profile for this reason; do not reintroduce a per-invoice or otherwise client-settable image path. For the same reason `ExpenseCreate` (the JSON API input schema) deliberately omits `attachment_path` — an expense's scan path is assigned server-side only by the authenticated upload handlers; do not re-add it as a client-settable field.

### Data export & import (backup / restore)

A **full per-user backup** is a single `.zip` (`manifest.json` + `data.json` + `files/`) produced by `export_user` and consumed by `import_user` in [app/backup.py](app/backup.py). It round-trips one user's complete data: SupplierProfile, Contacts, Invoices (with their JSON `items` + `supplier_snapshot`), Expenses, and all uploaded files (logo, stamp, expense scans). This is distinct from `GET /export/{year}` (the Statistiky monthly-breakdown CSV — see Statistiky below), which is unchanged.

- **Admin-only**, driven from the admin users page ([admin_users.html](app/templates/admin_users.html)): `GET /admin/zaloha/export/{user_id}` (zip download) and `POST /ui/admin/zaloha/import` (restore into an existing **empty** user or a freshly created one). Both routes live in [app/main.py](app/main.py) alongside the other `/admin/*` UI routes (not a JSON router — they're cookie-auth HTML/redirect flows, and a router file would hit a circular import needing `main.py`'s `_get_ui_admin`/`templates`).
- **Restore-to-fresh only:** `import_user` refuses (raises `BackupError` → 409) if the target user already owns any Contact/Invoice/Expense (a blank SupplierProfile is fine). No merge, no wipe.
- **IDs are not preserved.** Rows get fresh autoincrement ids under the target user; `Invoice.contact_id` is remapped via an old→new map built while inserting contacts, and a dobropis's `related_invoice_id` is remapped the same way (each invoice carries an archive-local `id` for this; the raw archived value is never stored, so a crafted archive can't point a dobropis at another tenant's row). Archives from before dobropisy existed import with `doc_type="invoice"` — additive, no `FORMAT_VERSION` bump.
- **Filesystem paths never leave the box — in either direction.** On export, any path under `uploads/<user>/` becomes an archive-relative ref (`files/...`) and the bytes are bundled; a snapshot image path that can't be bundled (outside the uploads dir, or the file is gone) is exported as `""`, never as a server path. On import they're rebuilt under `uploads/<new user>/`, and any snapshot `logo_path`/`stamp_path` that is not a `files/...` ref is blanked — the PDF renderer reads those paths off disk, so a crafted archive must not be able to point one at a server file. The logo/stamp paths frozen inside each invoice's `supplier_snapshot` are remapped too, so restored historical PDFs still find their images.
- **The archive is untrusted input.** Before any DB write, `import_user` enforces: zip-slip rejection, file-count + per-entry + total-uncompressed caps (zip-bomb guard), magic-byte validation on every file (via `content_matches_type`), and a `format_version` check. A failed import rolls back the DB transaction **and** deletes any files already written, leaving no trace. Bump `FORMAT_VERSION` in `backup.py` if the archive schema changes.

### Domain rules to preserve

- **Money uses `decimal.Decimal`**, never float arithmetic, in `logic.py` (DB columns are `float`, but compute/format with Decimal — `ROUND_HALF_UP`). Invoice `amount` is **computed server-side** from line items, never trusted from the client.
- **Variable Symbol = invoice number** format is `YYYYNNNN`, sequential per user per calendar year — generated by `get_next_variable_symbol` in [app/logic.py](app/logic.py). **The invoice series runs 0001–8999; the 9NNN block of each year is reserved for the dobropis series** (see below). It's a read-then-increment, so a unique index on `(user_id, variable_symbol)` guards against races and `create_invoice` retries on `IntegrityError`. The new-invoice form prefills the next number in an **editable** field; `InvoiceCreate.variable_symbol` is an optional override (1–10 digits) — when supplied, a collision returns **409** rather than auto-incrementing; when omitted, the sequential number is assigned. **Only symbols matching the series format exactly (current year + 4-digit sequence, in the series' range) advance the series** — a custom `20260007` makes the next auto number `20260008`, while a custom number in any other shape (shorter, longer, different prefix) lives outside the series and is ignored by auto-numbering (this also stops an oversized year-prefixed custom number from exhausting the sequence). The number is **not** editable when updating an existing invoice.
- **Dobropis (credit note):** `Invoice.doc_type` is `"invoice"` or `"credit_note"`; a dobropis is created **only from an existing invoice** via `POST /invoices/{id}/credit-note` (`CreditNoteCreate` — dates, items, notes only) and inherits the original's contact, currency, payment method and frozen `customer_snapshot`; `related_invoice_id` (plain column, deliberately no DB-level FK) always points at the corrected invoice. Its number comes from a **separate series `YYYY9NNN`** (`get_next_credit_note_symbol`, 9001–9999 per year) with no manual override. Line items use `CreditLineItem`: **quantity positive, unit_price negative**, so the stored `amount` is negative and every naive `sum(amount)` aggregate (dashboard, Statistiky, CSV) subtracts refunds automatically. A dobropis is **immutable after creation** (PATCH returns 409; only the paid toggle — meaning "money returned" — works); a wrong one is deleted (while unpaid) and reissued. An invoice with dobropisy cannot be deleted (409) until they're deleted first. The PDF renders "Dobropis č. X / Opravný doklad k faktuře č. Y", suppresses the payment-details block and SPAYD QR (money flows supplier → customer), and the UI form ([invoice_form.html](app/templates/invoice_form.html) in `credit_note_of` mode, route `GET /faktury/{id}/dobropis`) prefills the original's items negated and editable for partial refunds.
- **Invoices are deletable only while unpaid** — `DELETE /invoices/{id}` ([app/routers/invoices.py](app/routers/invoices.py)) and the `POST /ui/faktury/{id}/smazat` form handler ([app/main.py](app/main.py)) both return **409** for a paid invoice (toggle it unpaid first) or for an invoice with dobropisy issued against it; it's a hard delete, mirroring expenses. The UI (row kebab menu + detail page) hides the button for paid documents and warns in the confirm dialog that deleting a non-last number leaves a gap in the číselná řada (auto-numbering continues from the max).
- **VAT / plátce DPH:** all users are non-VAT-registered (neplátci DPH). The PDF is a *non-VAT* layout (no VAT-breakdown recap), and `_assert_vat_consistency` rejects `vat_rate > 0` from a supplier with no `DIČ`. A full plátce-DPH layout is intentionally **not implemented**. (The `vat_rate` field still exists on line items but must stay 0 for these users.)
- **Currency — locked to CZK, displayed as symbols:** the `currency` column (ISO 4217 code) is the canonical stored value on `Invoice`/`Expense`, but the human-facing display always shows a **symbol** (`CZK→Kč`, `EUR→€`, `USD→$`, `GBP→£`) via `currency_symbol()` in [app/logic.py](app/logic.py) — the single source of truth for the map. It is wired into HTML pages as the Jinja `currency` filter + `currency_symbol` global (registered in [main.py](app/main.py)), drives the PDF through `_format_czk` (the Statistiky CSV in [export.py](app/routers/export.py) is a CZK-only summary). New invoices/expenses are **locked to CZK, enforced server-side**: `InvoiceCreate.currency` / `ExpenseCreate.currency` reject anything but CZK (case-insensitive, normalised), `InvoiceUpdate` deliberately has **no** currency field (the stored value is immutable after creation), and the UI expense handlers hardcode CZK rather than reading the form value. The hidden `value="CZK"` fields in [invoice_form.html](app/templates/invoice_form.html) / [expense_form.html](app/templates/expense_form.html) are a UI convenience, not a guard (the invoice form keeps `#currency` as a hidden input because the JS payload builder reads it; on edit its value is sent but ignored by the API). Backup import intentionally bypasses the lock so legacy non-CZK rows restore verbatim. The lock exists because all **aggregates assume a single currency** — the dashboard, Statistiky, the Náklady "Celkem" total, and the CSV summary all naively `sum(amount)` across rows regardless of currency, and the SPAYD payment QR is a Czech-domestic standard bound to a CZK IBAN. Do **not** re-expose the selector without first making those sums per-currency. The display/storage layer stays currency-aware on purpose, so legacy non-CZK rows still render their correct symbol (and editing such an invoice preserves its currency rather than flipping it to CZK).
- **ARES autofill:** the contact form's "ARES" button calls `GET /api/contacts/ares/{ico}` ([app/routers/contacts.py](app/routers/contacts.py)), which queries the free, no-key Czech business register (`ares.gov.cz`) server-side via stdlib `urllib` and returns `{name, address, ic, dic}`. No API key or registration is required.
- **IČO and bank accounts** are validated via `validate_party_bank_and_ico` (IČO mod-11, account mod-11) at every supplier/contact save (UI handlers and the contacts API); the IBAN is auto-computed from prefix/number/code when not supplied.
- **Payment term (splatnost):** `Contact.payment_due_days` is an optional per-client term in days. When creating an invoice, the due date defaults to `issue date + term`, falling back to `DEFAULT_PAYMENT_DUE_DAYS` (config.py, 14) when unset. The invoice form recomputes the date client-side from the selected option's `data-due-days`; the field stays editable.
- **Invoice brand colour:** `SupplierProfile.brand_color` is user-selectable from a curated palette (`BRAND_COLOR_PALETTE` in config.py; subtle graphite `#1F2937` default, vivid colours opt-in). It is the PDF accent — applied as **lines/text only** (header rule, title, table-header underline, totals top-line + amount), not filled blocks — via a single `--brand` CSS variable in [invoice.html](app/templates/invoice.html). It is read **live** at PDF render time (passed into `render_invoice_to_pdf`), **not** frozen into the supplier snapshot — colour carries no accounting information, so changing it re-skins past invoices too. Only palette values are accepted on save (`BRAND_COLOR_CHOICES`), and `render_invoice_to_pdf` re-sanitises the hex (`_HEX_COLOR`) because it is interpolated into CSS. The web UI itself stays fixed blue (Tailwind); only the PDF is themed.
- **Supplier & customer snapshots**: when an invoice is created, the supplier profile is frozen into `Invoice.supplier_snapshot` and the contact into `Invoice.customer_snapshot` (both JSON — use the `get_/set_` helpers) so an issued document never changes when the profile or contact is later edited. The customer snapshot is re-frozen only when an update re-assigns the invoice's `contact_id`. Invoices with an empty (`{}`) customer snapshot (e.g. rows from a pre-snapshot dev database) fall back to the live contact in the PDF and detail page. Backup archives carry `customer_snapshot` per invoice; on import, archives from before this field exist get it synthesised from the archived contact (no `FORMAT_VERSION` bump — the change is additive). Line items are also stored as JSON in `Invoice.items` (use `get_items`/`set_items`).
- **Contacts referenced by invoices cannot be deleted** — `ui_delete_contact` re-renders the list with a 409 + flash error instead. SQLite runs without `PRAGMA foreign_keys`, so this application-level guard is the only thing preventing orphaned `Invoice.contact_id` rows (which would break legacy PDFs and make backup exports un-importable).
- **Czech bank accounts** are stored as components (prefix/number/code) and validated with weighted mod-11 checksums; IBAN is derived. See `validate_cz_bank_account` and `bank_account_display()`.
- **PDF images** are embedded as base64 data URIs (`_image_to_data_uri`) to avoid WeasyPrint filesystem path issues.

### Uploads

User-uploaded files (logos, stamps, expense scans) are stored under `app/uploads/` and served through the **authenticated** `/uploads/{owner_id}/{path}` route in `main.py` — they are deliberately *not* exposed via the public `/static` mount.

### UI navigation & shared table macros

The navbar ([base.html](app/templates/base.html)) has a **"+" dropdown** (Faktura → `/faktury/nova`, Náklad → `/naklady/novy`) plus **Přehled** (`/`, dashboard), **Faktury** (`/faktury`), **Náklady** (`/naklady`), **Kontakty** (`/kontakty`), **Statistiky** (`/statistiky`), Účet (`/ucet`). The `/faktury` page lists all invoices with filters (year + whether the year applies to issue or due date); `/naklady` lists all expenses filtered by year. Note `/invoices` is the **JSON API** (returns `InvoiceSummary`), so the UI list lives at `/faktury`.

**Route language convention:** all **GUI** routes are Czech — the visible pages (`/faktury`, `/faktury/nova`, `/faktury/{id}`, `/faktury/{id}/upravit`, `/faktury/{id}/dobropis`, `/naklady`, `/naklady/novy`, `/naklady/{id}/upravit`, `/kontakty`, `/kontakty/novy`, `/kontakty/{id}/upravit`, `/ucet`, `/admin/uzivatele`) and the form-POST handlers under `/ui/...` (e.g. `/ui/faktury/{id}/prepnout-zaplaceno`, `/ui/faktury/{id}/smazat`, `/ui/naklady/{id}/nahrat-sken`, `/ui/ucet/dodavatel`, `/ui/admin/uzivatele/{id}/smazat`, `/ui/admin/zaloha/import`). The **JSON API stays English** — `/invoices`, `/expenses` (their routers), `/api/contacts`, `/export`, plus `/invoices/{id}/pdf` — because it's a technical interface the page JS and tests call directly. The `/ui/` and `/api/` prefixes are namespaces, not a language choice. When adding a UI route, name it in Czech and keep any API endpoint it calls English.

**Statistiky** (`ui_statistics` in [main.py](app/main.py) → [statistiky.html](app/templates/statistiky.html)) is the monthly overview: 12 rows (one per month) of invoice sum, expense sum, and their diff for a selected year, plus a grand-total footer. A `date_field` toggle re-groups invoices by **data vystavení** (all invoices, by `date_issued`) or **data zaplacení** (paid invoices only — `is_paid` filter applies *only* in this mode — by `date_paid`); expenses always group by their own `date`. Sums use `Decimal`. The page also hosts the **CSV download** button (`/export/{year}?date_field=…` in [export.py](app/routers/export.py)); that endpoint mirrors this exact monthly table — same `date_field` issued/paid semantics and same `Decimal` aggregation — so the file matches what's on screen. The old navbar Export link was removed.

The invoice and expense tables are shared Jinja macros in [_macros.html](app/templates/_macros.html) (`invoice_table`, `expense_table`), used by both the dashboard and the list pages; each takes a `redirect_to` so row actions (mark paid, edit, attach/delete) return to the originating page.

**Expenses are editable** via `GET /naklady/{id}/upravit` + `POST /ui/naklady/{id}/upravit` (the "Upravit" row action). [expense_form.html](app/templates/expense_form.html) is dual-purpose (create vs edit) keyed on an `expense` template var, mirroring [contact_form.html](app/templates/contact_form.html)'s `contact` pattern. The edit handler validates everything — including the optional replacement scan (type + magic-byte sniff) — **before** mutating the row, so a rejected file never leaves a half-updated expense. The edit link URL-encodes `redirect_to` because the Náklady list's `redirect_to` carries a query string (the year filter). All dropdowns (the navbar "+" and per-row "⋮" menus) are driven by one global script in base.html keyed on `.kebab-btn` / `.kebab-menu`. User-facing expense wording is **"Náklad/Náklady"** (the model/routes remain `Expense`/`/expenses`).

### Frontend / CSS

Tailwind is **self-hosted** (no CDN): `app/static/css/tailwind.css` is a pre-compiled, purged build that is committed and served by Nginx. After changing utility classes in any template, regenerate it with the standalone CLI (no Node.js required):

```bash
.tools/tailwindcss -c tailwind.config.js -i app/static/css/tailwind.input.css -o app/static/css/tailwind.css --minify
```

The `.tools/` binary is dev-only and excluded from git and deploys. If you ever need to fetch it: download `tailwindcss-macos-arm64` from the tailwindcss v3 GitHub releases into `.tools/`.

### Database & migrations

**The database engine is chosen by the `DATABASE_URL` env var** (read in [app/database.py](app/database.py)). Unset → SQLite at the **project root** (`faktury.db`, one level above `app/`) so it survives deploys — the default for the self-hosted deployment. Set it to a server DB URL (e.g. `postgresql+psycopg://…`) to run on Postgres/MySQL; SQLite-only engine options (`check_same_thread`) are applied conditionally on the URL scheme, and pool pre-ping/recycle are enabled for server DBs. All queries go through SQLModel/SQLAlchemy — there is no hand-written dialect-specific SQL outside `database.py`, so the swap is a config change. (Note: `Invoice.contact_id`/`user_id` FKs are *not* enforced by SQLite but *are* on a server DB — the app-level ownership/delete guards already cover this, but it's the thing to exercise after a switch.)

Schema is managed by **Alembic** (see [alembic/](alembic/) + [alembic.ini](alembic.ini)). `create_db_and_tables()` runs `alembic upgrade head` on startup. env.py binds Alembic to `app.database.engine` and `SQLModel.metadata` (so the CLI, startup, and the test-suite's swapped engine all migrate the same DB; `render_as_batch` is on for SQLite). Three startup states:

- **Fresh DB** (no `user` table) → `upgrade head` builds everything from `0001_baseline`.
- **Legacy DB** (tables exist, no *stamped* `alembic_version` row — i.e. one from the old `PRAGMA user_version` scheme, or a test DB built from `metadata.create_all`) → finish any pending legacy PRAGMA migrations, then `alembic stamp 0001_baseline`, then `upgrade head`. The old PRAGMA migration functions survive in `database.py` **only** for this adoption step (`_finish_legacy_pragma_migrations`); do not add new steps there.
- **Alembic-managed DB** → plain `upgrade head` (no-op when current). A DB stamped at an unknown/newer revision aborts startup (Alembic `CommandError`) — downgrade protection.

**To change the schema:** edit the SQLModel models, then `alembic revision --autogenerate -m "…" --rev-id NNNN_slug` (use the next zero-padded number), review the generated file, and it applies on next startup. Run `alembic check` to confirm models and migrations are in sync — it must report "No new upgrade operations detected". The VS uniqueness index (`ix_invoice_user_vs`) now lives in `Invoice.__table_args__` (model metadata), so autogenerate sees it — it is no longer a startup side-effect.
