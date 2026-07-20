"""Validation, Czech-correctness, and upload-hardening tests."""

from __future__ import annotations

from datetime import date

from app.auth import validate_password_policy
from app.logic import (
    cz_account_to_iban,
    get_next_variable_symbol,
    validate_cz_bank_account,
    validate_ico,
)
from sqlmodel import Session
from tests.conftest import ORIGIN, bearer, make_user

VALID_ICO = "25596641"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# ── Pure validators ──────────────────────────────────────────────────────────

def test_ico_checksum():
    assert validate_ico(VALID_ICO) is None
    assert validate_ico("12345678") is not None    # bad check digit
    assert validate_ico("123") is not None          # wrong length


def test_bank_account_and_iban():
    assert validate_cz_bank_account("2000145399", "0800", "19") is None
    assert validate_cz_bank_account("2000145399", "080") is not None   # bad code
    assert cz_account_to_iban("2000145399", "0800", "19") == "CZ6508000000192000145399"


def test_password_policy():
    assert validate_password_policy("short") is not None        # < 12
    assert validate_password_policy("x" * 200) is not None      # > 72 bytes
    assert validate_password_policy("a-good-password") is None


# ── Variable symbol ──────────────────────────────────────────────────────────

def test_variable_symbol_is_sequential_four_digit(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = client.post("/api/contacts", headers=h, json={
        "name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]
    payload = {"contact_id": cid, "date_issued": "2026-01-01",
               "date_due": "2026-01-15",
               "items": [{"description": "x", "quantity": 1, "unit_price": 10,
                          "vat_rate": 0}]}
    vs1 = client.post("/invoices", headers=h, json=payload).json()["variable_symbol"]
    vs2 = client.post("/invoices", headers=h, json=payload).json()["variable_symbol"]
    year = str(date.today().year)
    assert vs1 == f"{year}0001"
    assert vs2 == f"{year}0002"


def test_variable_symbol_unique_index_enforced(client, db_engine):
    """A duplicate (user_id, variable_symbol) must be rejected at the DB level."""
    import sqlalchemy
    make_user(db_engine, "alice")
    with Session(db_engine) as s:
        uid = s.exec(sqlalchemy.text("SELECT id FROM user")).first()[0]
    from app.models import Invoice
    with Session(db_engine) as s:
        s.add(Invoice(user_id=uid, contact_id=1, variable_symbol="20260001",
                      amount=1, date_issued=date(2026, 1, 1), date_due=date(2026, 1, 2)))
        s.commit()
    import pytest
    with pytest.raises(Exception):
        with Session(db_engine) as s:
            s.add(Invoice(user_id=uid, contact_id=1, variable_symbol="20260001",
                          amount=1, date_issued=date(2026, 1, 1), date_due=date(2026, 1, 2)))
            s.commit()


def test_custom_vs_in_series_format_advances_series(client, db_engine):
    """A custom number matching YYYYNNNN continues the automatic series."""
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = _api_contact(client, h)
    year = str(date.today().year)
    r = client.post("/invoices", headers=h,
                    json=_invoice_payload(cid, variable_symbol=f"{year}0007"))
    assert r.status_code == 201
    r = client.post("/invoices", headers=h, json=_invoice_payload(cid))
    assert r.json()["variable_symbol"] == f"{year}0008"


def test_custom_vs_outside_series_format_is_ignored(client, db_engine):
    """Custom numbers not matching YYYYNNNN don't touch (or poison) the series."""
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = _api_contact(client, h)
    year = str(date.today().year)
    # Oversized year-prefixed number: previously exhausted the sequence (500).
    assert client.post("/invoices", headers=h,
                       json=_invoice_payload(cid, variable_symbol=f"{year}999999")
                       ).status_code == 201
    # Number in a completely different shape.
    assert client.post("/invoices", headers=h,
                       json=_invoice_payload(cid, variable_symbol="12345")
                       ).status_code == 201
    r = client.post("/invoices", headers=h, json=_invoice_payload(cid))
    assert r.status_code == 201
    assert r.json()["variable_symbol"] == f"{year}0001"


# ── Invoice deletion (unpaid only) ───────────────────────────────────────────

def test_delete_unpaid_invoice(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = _api_contact(client, h)
    inv_id = client.post("/invoices", headers=h, json=_invoice_payload(cid)).json()["id"]
    assert client.delete(f"/invoices/{inv_id}", headers=h).status_code == 204
    assert client.get("/invoices", headers=h).json() == []


def test_delete_paid_invoice_rejected(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = _api_contact(client, h)
    inv_id = client.post("/invoices", headers=h, json=_invoice_payload(cid)).json()["id"]
    client.patch(f"/invoices/{inv_id}/paid", headers=h, json={})
    r = client.delete(f"/invoices/{inv_id}", headers=h)
    assert r.status_code == 409
    # Toggling back to unpaid makes it deletable.
    client.patch(f"/invoices/{inv_id}/paid", headers=h, json={})
    assert client.delete(f"/invoices/{inv_id}", headers=h).status_code == 204


def test_delete_invoice_cross_tenant_404(client, db_engine):
    make_user(db_engine, "alice")
    make_user(db_engine, "bob")
    h_alice = bearer(client, "alice")
    cid = _api_contact(client, h_alice)
    inv_id = client.post("/invoices", headers=h_alice,
                         json=_invoice_payload(cid)).json()["id"]
    h_bob = bearer(client, "bob")
    assert client.delete(f"/invoices/{inv_id}", headers=h_bob).status_code == 404
    assert client.get("/invoices", headers=h_alice).json() != []


def test_ui_delete_invoice_form(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = _api_contact(client, h)
    inv_id = client.post("/invoices", headers=h, json=_invoice_payload(cid)).json()["id"]
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    r = client.post(f"/ui/faktury/{inv_id}/smazat", headers=ORIGIN,
                    follow_redirects=False, data={"redirect_to": "/faktury"})
    assert r.status_code == 303
    assert r.headers["location"] == "/faktury"
    assert client.get("/invoices", headers=h).json() == []


# ── Currency lock (CZK-only for new records, enforced server-side) ───────────

def _invoice_payload(cid, **overrides):
    return {"contact_id": cid, "date_issued": "2026-01-01",
            "date_due": "2026-01-15",
            "items": [{"description": "x", "quantity": 1, "unit_price": 10,
                       "vat_rate": 0}],
            **overrides}


def _api_contact(client, h):
    return client.post("/api/contacts", headers=h, json={
        "name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]


def test_invoice_create_rejects_non_czk(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = _api_contact(client, h)
    r = client.post("/invoices", headers=h, json=_invoice_payload(cid, currency="EUR"))
    assert r.status_code == 422
    assert "CZK" in r.text
    # Case-insensitive CZK is accepted and normalised.
    r = client.post("/invoices", headers=h, json=_invoice_payload(cid, currency="czk"))
    assert r.status_code == 201
    assert r.json()["currency"] == "CZK"


def test_invoice_update_cannot_change_currency(client, db_engine):
    """PATCH ignores currency: new invoices stay CZK, legacy rows keep theirs."""
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = _api_contact(client, h)
    inv_id = client.post("/invoices", headers=h,
                         json=_invoice_payload(cid)).json()["id"]

    r = client.patch(f"/invoices/{inv_id}", headers=h, json={"currency": "EUR"})
    assert r.status_code == 200
    assert r.json()["currency"] == "CZK"

    # A legacy non-CZK invoice keeps its currency through an ordinary edit.
    from app.models import Invoice
    with Session(db_engine) as s:
        inv = s.get(Invoice, inv_id)
        inv.currency = "EUR"
        s.add(inv)
        s.commit()
    r = client.patch(f"/invoices/{inv_id}", headers=h,
                     json={"notes": "beze změny měny", "currency": "CZK"})
    assert r.status_code == 200
    assert r.json()["currency"] == "EUR"


def test_expense_create_api_rejects_non_czk(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    r = client.post("/expenses", headers=h, json={
        "date": "2026-01-01", "category": "SW", "amount": 100,
        "currency": "USD", "description": "x"})
    assert r.status_code == 422
    assert "CZK" in r.text


def test_expense_form_ignores_tampered_currency(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    r = client.post("/ui/naklady/novy", headers=ORIGIN, follow_redirects=False,
                    data={"date": "2026-06-17", "category": "SW", "amount": "100",
                          "description": "x", "currency": "EUR"})
    assert r.status_code == 303
    from app.models import Expense
    from sqlmodel import select
    with Session(db_engine) as s:
        assert s.exec(select(Expense)).first().currency == "CZK"


# ── Contact validation through the API ───────────────────────────────────────

def test_contact_bad_ico_rejected(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    r = client.post("/api/contacts", headers=h,
                    json={"name": "K", "address": "A", "ic": "12345678"})
    assert r.status_code == 422


def test_contact_iban_autocomputed(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    r = client.post("/api/contacts", headers=h, json={
        "name": "K", "address": "A", "ic": VALID_ICO,
        "bank_number": "2000145399", "bank_code": "0800", "bank_prefix": "19"})
    assert r.status_code == 201
    assert r.json()["bank_iban"] == "CZ6508000000192000145399"


# ── Neplátce DPH guard ───────────────────────────────────────────────────────

def _invoice_with_vat(client, h, cid, vat):
    return client.post("/invoices", headers=h, json={
        "contact_id": cid, "date_issued": "2026-01-01", "date_due": "2026-01-15",
        "items": [{"description": "x", "quantity": 1, "unit_price": 100,
                   "vat_rate": vat}]})


def test_non_vat_payer_cannot_charge_vat(client, db_engine):
    make_user(db_engine, "alice", dic=None)        # no DIČ → neplátce
    h = bearer(client, "alice")
    cid = client.post("/api/contacts", headers=h, json={
        "name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]
    assert _invoice_with_vat(client, h, cid, 0.21).status_code == 422
    assert _invoice_with_vat(client, h, cid, 0).status_code == 201


def test_vat_payer_can_charge_vat(client, db_engine):
    make_user(db_engine, "alice", dic="CZ25596641")  # has DIČ → plátce
    h = bearer(client, "alice")
    cid = client.post("/api/contacts", headers=h, json={
        "name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]
    assert _invoice_with_vat(client, h, cid, 0.21).status_code == 201


# ── Upload hardening ─────────────────────────────────────────────────────────

def _login_cookie(client):
    client.post("/login", data={"username": "alice", "password": "password-1234"})


def test_upload_rejects_content_type_mismatch(client, db_engine):
    make_user(db_engine, "alice")
    _login_cookie(client)
    r = client.post("/ui/ucet/nahrat/logo", headers=ORIGIN,
                    files={"file": ("x.png", b"not a real png", "image/png")},
                    follow_redirects=False)
    assert r.status_code == 400


def test_upload_rejects_oversize(client, db_engine):
    make_user(db_engine, "alice")
    _login_cookie(client)
    big = PNG + b"\x00" * (5 * 1024 * 1024 + 1)
    r = client.post("/ui/ucet/nahrat/logo", headers=ORIGIN,
                    files={"file": ("x.png", big, "image/png")},
                    follow_redirects=False)
    assert r.status_code == 413


def test_upload_accepts_valid_png(client, db_engine):
    make_user(db_engine, "alice")
    _login_cookie(client)
    r = client.post("/ui/ucet/nahrat/logo", headers=ORIGIN,
                    files={"file": ("x.png", PNG, "image/png")},
                    follow_redirects=False)
    assert r.status_code == 303


# ── ARES lookup (network mocked) ─────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        import json
        return json.dumps(self._payload).encode("utf-8")


def test_ares_lookup_prefills_fields(client, db_engine, monkeypatch):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    payload = {
        "ico": VALID_ICO, "obchodniJmeno": "Testovací s.r.o.",
        "dic": "CZ" + VALID_ICO,
        "adresaDorucovaci": {"radekAdresy1": "Ulice 1", "radekAdresy2": "Holešovice",
                             "radekAdresy3": "170 00 Praha 7"},
    }
    import app.routers.contacts as contacts_mod
    monkeypatch.setattr(contacts_mod.urllib.request, "urlopen",
                        lambda req, timeout=8: _FakeResp(payload))
    r = client.get(f"/api/contacts/ares/{VALID_ICO}", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Testovací s.r.o."
    assert body["address"] == "Ulice 1\nHolešovice\n170 00 Praha 7"
    assert body["dic"] == "CZ" + VALID_ICO


def test_ares_lookup_rejects_bad_ico(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    r = client.get("/api/contacts/ares/12345678", headers=h)   # bad checksum
    assert r.status_code == 422


def test_ares_lookup_requires_auth(client, db_engine):
    r = client.get(f"/api/contacts/ares/{VALID_ICO}")
    assert r.status_code == 401


# ── Per-contact payment term (splatnost) ─────────────────────────────────────

def test_contact_payment_due_days_roundtrip(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    r = client.post("/api/contacts", headers=h, json={
        "name": "K", "address": "A", "ic": VALID_ICO, "payment_due_days": 30})
    assert r.status_code == 201
    assert r.json()["payment_due_days"] == 30


def test_contact_payment_due_days_optional(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    r = client.post("/api/contacts", headers=h, json={
        "name": "K", "address": "A", "ic": VALID_ICO})
    assert r.status_code == 201
    assert r.json()["payment_due_days"] is None


def test_contact_payment_due_days_rejects_non_positive(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    r = client.post("/api/contacts", headers=h, json={
        "name": "K", "address": "A", "ic": VALID_ICO, "payment_due_days": 0})
    assert r.status_code == 422


def test_contact_form_saves_and_rejects_term(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    base = {"name": "K", "address": "A", "ic": VALID_ICO}

    # Valid term via the HTML form → 303 redirect, value persisted.
    ok = client.post("/ui/kontakty/novy", headers=ORIGIN,
                     data={**base, "payment_due_days": "21"}, follow_redirects=False)
    assert ok.status_code == 303
    from app.models import Contact
    from sqlmodel import select
    with Session(db_engine) as s:
        assert s.exec(select(Contact)).first().payment_due_days == 21

    # Invalid term → 422, form re-rendered with the Czech error.
    bad = client.post("/ui/kontakty/novy", headers=ORIGIN,
                      data={**base, "payment_due_days": "-5"}, follow_redirects=False)
    assert bad.status_code == 422
    assert "Splatnost musí být kladný počet dní." in bad.text


# ── Expense editing (UI) ──────────────────────────────────────────────────────

def _make_expense_ui(client, db_engine, **overrides):
    """Create one expense through the HTML form; return its id."""
    from app.models import Expense
    from sqlmodel import select
    data = {"date": "2026-06-17", "category": "Software",
            "amount": "100", "description": "Předplatné", **overrides}
    r = client.post("/ui/naklady/novy", headers=ORIGIN, data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    with Session(db_engine) as s:
        return s.exec(select(Expense).order_by(Expense.id.desc())).first().id


def test_expense_edit_updates_fields(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    eid = _make_expense_ui(client, db_engine)

    r = client.post(f"/ui/naklady/{eid}/upravit", headers=ORIGIN, follow_redirects=False,
                    data={"date": "2026-07-01", "category": "Hardware",
                          "amount": "250.50", "description": "Nový monitor",
                          "redirect_to": "/naklady"})
    assert r.status_code == 303
    assert r.headers["location"] == "/naklady"

    from app.models import Expense
    with Session(db_engine) as s:
        exp = s.get(Expense, eid)
        assert exp.category == "Hardware"
        assert exp.amount == 250.50
        assert exp.description == "Nový monitor"
        assert exp.date.isoformat() == "2026-07-01"


def test_expense_create_rejects_bad_date(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})

    r = client.post("/ui/naklady/novy", headers=ORIGIN, follow_redirects=False,
                    data={"date": "not-a-date", "category": "Software",
                          "amount": "100", "description": "x"})
    assert r.status_code == 422
    assert "Neplatné datum." in r.text

    # Nothing was written.
    from app.models import Expense
    from sqlmodel import select
    with Session(db_engine) as s:
        assert s.exec(select(Expense)).first() is None


def test_expense_create_rejects_unsupported_scan_type(client, db_engine):
    """An unsupported receipt format is a visible error and nothing is saved
    (previously the expense was created and the scan silently dropped)."""
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})

    r = client.post("/ui/naklady/novy", headers=ORIGIN, follow_redirects=False,
                    data={"date": "2026-06-17", "category": "Software",
                          "amount": "100", "description": "x"},
                    files={"scan": ("scan.gif", b"GIF89a" + b"\x00" * 16, "image/gif")})
    assert r.status_code == 422
    assert "Nepodporovaný formát dokladu" in r.text

    from app.models import Expense
    from sqlmodel import select
    with Session(db_engine) as s:
        assert s.exec(select(Expense)).first() is None


def test_expense_create_rejects_scan_magic_mismatch(client, db_engine):
    """A scan whose bytes don't match its declared type must not leave a
    half-created expense behind."""
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})

    r = client.post("/ui/naklady/novy", headers=ORIGIN, follow_redirects=False,
                    data={"date": "2026-06-17", "category": "Software",
                          "amount": "100", "description": "x"},
                    files={"scan": ("scan.png", b"not a real png", "image/png")})
    assert r.status_code == 422
    assert "neodpovídá uvedenému formátu" in r.text

    from app.models import Expense
    from sqlmodel import select
    with Session(db_engine) as s:
        assert s.exec(select(Expense)).first() is None


def test_expense_create_with_valid_scan_stores_attachment(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})

    r = client.post("/ui/naklady/novy", headers=ORIGIN, follow_redirects=False,
                    data={"date": "2026-06-17", "category": "Software",
                          "amount": "100", "description": "x"},
                    files={"scan": ("scan.png", PNG, "image/png")})
    assert r.status_code == 303

    from pathlib import Path
    from app.models import Expense
    from sqlmodel import select
    with Session(db_engine) as s:
        exp = s.exec(select(Expense)).first()
        assert exp.attachment_path
        path = Path(exp.attachment_path)
        assert path.is_file() and path.read_bytes() == PNG
    path.unlink()  # keep the real uploads dir clean


def test_expense_edit_rejects_bad_amount(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    eid = _make_expense_ui(client, db_engine)

    r = client.post(f"/ui/naklady/{eid}/upravit", headers=ORIGIN, follow_redirects=False,
                    data={"date": "2026-06-17", "category": "Software",
                          "amount": "-5", "description": "x"})
    assert r.status_code == 422
    assert "Částka musí být větší než nula." in r.text


def test_expense_edit_page_prefills(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    eid = _make_expense_ui(client, db_engine)

    r = client.get(f"/naklady/{eid}/upravit")
    assert r.status_code == 200
    assert "Upravit náklad" in r.text
    assert "Software" in r.text
    assert f"/ui/naklady/{eid}/upravit" in r.text


def test_expense_edit_blocks_other_users_expense(client, db_engine):
    make_user(db_engine, "alice")
    make_user(db_engine, "bob")
    # Bob creates an expense.
    client.post("/login", data={"username": "bob", "password": "password-1234"})
    bob_eid = _make_expense_ui(client, db_engine, description="Bobs")
    # Alice must not be able to edit it – cross-tenant access is reported as 404.
    # (Clear the cookie jar so the new login replaces bob's session cleanly.)
    client.cookies.clear()
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    r = client.post(f"/ui/naklady/{bob_eid}/upravit", headers=ORIGIN, follow_redirects=False,
                    data={"date": "2026-06-17", "category": "Hacked",
                          "amount": "1", "description": "x"})
    assert r.status_code == 404
    # And the GET edit page is likewise denied.
    assert client.get(f"/naklady/{bob_eid}/upravit").status_code == 404


# ── Configurable invoice brand colour ────────────────────────────────────────

def test_new_supplier_defaults_to_brand_color(client, db_engine):
    make_user(db_engine, "alice")
    from app.models import SupplierProfile
    from sqlmodel import select
    with Session(db_engine) as s:
        assert s.exec(select(SupplierProfile)).first().brand_color == "#1F2937"


def test_supplier_save_persists_palette_color(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    r = client.post("/ui/ucet/dodavatel", headers=ORIGIN,
                    data={"name": "X", "ic": VALID_ICO, "brand_color": "#047857"},
                    follow_redirects=False)
    assert r.status_code == 200
    from app.models import SupplierProfile
    from sqlmodel import select
    with Session(db_engine) as s:
        assert s.exec(select(SupplierProfile)).first().brand_color == "#047857"


def test_supplier_save_rejects_off_palette_color(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    r = client.post("/ui/ucet/dodavatel", headers=ORIGIN,
                    data={"name": "X", "ic": VALID_ICO, "brand_color": "#123456"},
                    follow_redirects=False)
    assert r.status_code == 422


def test_render_sanitizes_bad_brand_color():
    """A non-hex colour must never reach the CSS; it falls back to the default."""
    from app.logic import render_invoice_to_pdf
    inv = {"variable_symbol": "20260001", "currency": "CZK",
           "items": [{"description": "x", "quantity": 1, "unit_price": 100, "vat_rate": 0}],
           "date_issued": None, "date_due": None}
    contact = {"name": "K", "address": "A", "ic": VALID_ICO}
    # An injection attempt must not crash and must produce a valid PDF.
    pdf = render_invoice_to_pdf(inv, contact, {}, "", brand_color="red;}body{x:1")
    assert pdf[:5] == b"%PDF-"


def _invoice_payload(cid, **extra):
    return {"contact_id": cid, "date_issued": "2026-06-17", "date_due": "2026-07-01",
            "items": [{"description": "x", "quantity": 1, "unit_price": 1000, "vat_rate": 0}],
            **extra}


def test_invoice_number_custom_override(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = client.post("/api/contacts", headers=h, json={"name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]
    r = client.post("/invoices", headers=h, json=_invoice_payload(cid, variable_symbol="55501"))
    assert r.status_code == 201
    assert r.json()["variable_symbol"] == "55501"


def test_invoice_number_duplicate_rejected(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = client.post("/api/contacts", headers=h, json={"name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]
    client.post("/invoices", headers=h, json=_invoice_payload(cid, variable_symbol="55501"))
    dup = client.post("/invoices", headers=h, json=_invoice_payload(cid, variable_symbol="55501"))
    assert dup.status_code == 409


def test_invoice_number_auto_when_omitted(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = client.post("/api/contacts", headers=h, json={"name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]
    r = client.post("/invoices", headers=h, json=_invoice_payload(cid))
    assert r.status_code == 201
    assert r.json()["variable_symbol"].startswith(str(date.today().year))


def test_invoice_number_bad_format_rejected(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = client.post("/api/contacts", headers=h, json={"name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]
    r = client.post("/invoices", headers=h, json=_invoice_payload(cid, variable_symbol="ABC-1"))
    assert r.status_code == 422


def test_invoice_update_rejects_due_before_issued(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = client.post("/api/contacts", headers=h, json={"name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]
    inv = client.post("/invoices", headers=h, json=_invoice_payload(cid)).json()
    # Both dates supplied in one patch, due before issued → rejected by schema.
    r = client.patch(f"/invoices/{inv['id']}", headers=h,
                     json={"date_issued": "2026-07-01", "date_due": "2026-06-01"})
    assert r.status_code == 422


def test_invoice_update_rejects_due_before_stored_issued(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = client.post("/api/contacts", headers=h, json={"name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]
    # Stored issue date is 2026-06-17 (from _invoice_payload).
    inv = client.post("/invoices", headers=h, json=_invoice_payload(cid)).json()
    # Patch only date_due, earlier than the stored issue date → rejected by route.
    r = client.patch(f"/invoices/{inv['id']}", headers=h, json={"date_due": "2026-06-01"})
    assert r.status_code == 422


def test_expense_create_ignores_client_attachment_path(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    # A client-supplied filesystem path must never be honoured; the scan path is
    # set server-side only, confined to the user's own uploads dir.
    r = client.post("/expenses", headers=h, json={
        "date": "2026-06-17", "category": "Software", "amount": 100,
        "description": "x", "attachment_path": "/etc/passwd",
    })
    assert r.status_code == 201
    assert r.json()["attachment_path"] is None


def test_new_invoice_form_shows_editable_number(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    html = client.get("/faktury/nova").text
    assert 'name="variable_symbol"' in html
    assert 'value="%s0001"' % date.today().year in html


def _vs_rows(html):
    return html.count("font-mono font-semibold text-blue-700")


def test_navbar_has_create_dropdown_and_list_links(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    html = client.get("/").text
    assert 'href="/faktury"' in html and 'href="/naklady"' in html
    assert ">Faktura<" in html and ">Náklad<" in html      # + dropdown items


def test_faktury_filters_by_year_and_date_field(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = client.post("/api/contacts", headers=h, json={
        "name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]
    base = {"contact_id": cid, "items": [{"description": "x", "quantity": 1,
                                          "unit_price": 100, "vat_rate": 0}]}
    # issued 2025, due 2026
    client.post("/invoices", headers=h, json={**base, "date_issued": "2025-12-20", "date_due": "2026-01-05"})
    # issued + due 2026
    client.post("/invoices", headers=h, json={**base, "date_issued": "2026-06-01", "date_due": "2026-06-15"})
    client.post("/login", data={"username": "alice", "password": "password-1234"})

    assert _vs_rows(client.get("/faktury").text) == 2                          # all
    assert _vs_rows(client.get("/faktury?year=2026&date_field=issued").text) == 1
    assert _vs_rows(client.get("/faktury?year=2026&date_field=due").text) == 2  # both due in 2026


def test_naklady_filters_by_year(client, db_engine):
    from app.models import Expense
    from datetime import date as _date
    uid = make_user(db_engine, "alice")
    with Session(db_engine) as s:
        s.add(Expense(user_id=uid, date=_date(2025, 3, 1), category="SW", amount=500, description="loni"))
        s.add(Expense(user_id=uid, date=_date(2026, 2, 1), category="SW", amount=900, description="letos"))
        s.commit()
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    all_html = client.get("/naklady").text
    assert "loni" in all_html and "letos" in all_html
    y2026 = client.get("/naklady?year=2026").text
    assert "letos" in y2026 and "loni" not in y2026


def test_invoice_form_exposes_contact_term(client, db_engine):
    """The new-invoice form must carry each contact's term as data-due-days."""
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    # One contact with a custom term, one without (→ default).
    client.post("/api/contacts", headers=h, json={
        "name": "Custom", "address": "A", "ic": VALID_ICO, "payment_due_days": 30})
    client.post("/api/contacts", headers=h, json={
        "name": "Default", "address": "A", "ic": "27082440"})
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    html = client.get("/faktury/nova").text
    assert 'data-due-days="30"' in html
    assert 'data-due-days="14"' in html      # the app default for the second contact
