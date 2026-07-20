"""Dobropis (credit note) tests: series, creation, immutability, backup."""

from __future__ import annotations

from datetime import date

from sqlmodel import Session, select

from app.models import Invoice, User
from tests.conftest import ORIGIN, bearer, make_user

VALID_ICO = "25596641"


def _contact(client, h):
    return client.post("/api/contacts", headers=h, json={
        "name": "K", "address": "A", "ic": VALID_ICO}).json()["id"]


def _invoice(client, h, cid, **overrides):
    payload = {"contact_id": cid, "date_issued": "2026-01-01",
               "date_due": "2026-01-15",
               "items": [{"description": "Práce", "quantity": 2, "unit_price": 500,
                          "vat_rate": 0}],
               **overrides}
    r = client.post("/invoices", headers=h, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _credit_payload(**overrides):
    return {"date_issued": "2026-02-01", "date_due": "2026-02-15",
            "items": [{"description": "Vratka", "quantity": 2, "unit_price": -500,
                       "vat_rate": 0}],
            **overrides}


def _setup(client, db_engine, username="alice"):
    make_user(db_engine, username)
    h = bearer(client, username)
    cid = _contact(client, h)
    return h, cid


# ── Creation & series ─────────────────────────────────────────────────────────

def test_credit_note_created_from_invoice(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    r = client.post(f"/invoices/{inv['id']}/credit-note", headers=h,
                    json=_credit_payload())
    assert r.status_code == 201, r.text
    cn = r.json()
    year = str(date.today().year)
    assert cn["variable_symbol"] == f"{year}9001"
    assert cn["doc_type"] == "credit_note"
    assert cn["related_invoice_id"] == inv["id"]
    assert cn["amount"] == -1000.0
    # Inherited from the corrected invoice:
    assert cn["contact_id"] == inv["contact_id"]
    assert cn["currency"] == inv["currency"]
    assert cn["payment_method"] == inv["payment_method"]


def test_credit_note_series_is_separate(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    year = str(date.today().year)
    cn1 = client.post(f"/invoices/{inv['id']}/credit-note", headers=h,
                      json=_credit_payload()).json()
    cn2 = client.post(f"/invoices/{inv['id']}/credit-note", headers=h,
                      json=_credit_payload()).json()
    assert (cn1["variable_symbol"], cn2["variable_symbol"]) == \
        (f"{year}9001", f"{year}9002")
    # The invoice series is not advanced by dobropisy.
    inv2 = _invoice(client, h, cid)
    assert inv2["variable_symbol"] == f"{year}0002"


def test_credit_note_rejects_positive_price(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    r = client.post(f"/invoices/{inv['id']}/credit-note", headers=h,
                    json=_credit_payload(items=[{"description": "x", "quantity": 1,
                                                 "unit_price": 500, "vat_rate": 0}]))
    assert r.status_code == 422


def test_credit_note_of_credit_note_rejected(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    cn = client.post(f"/invoices/{inv['id']}/credit-note", headers=h,
                     json=_credit_payload()).json()
    r = client.post(f"/invoices/{cn['id']}/credit-note", headers=h,
                    json=_credit_payload())
    assert r.status_code == 409


def test_credit_note_cross_tenant_404(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    make_user(db_engine, "bob")
    h_bob = bearer(client, "bob")
    r = client.post(f"/invoices/{inv['id']}/credit-note", headers=h_bob,
                    json=_credit_payload())
    assert r.status_code == 404


# ── Immutability & deletion rules ────────────────────────────────────────────

def test_credit_note_is_immutable(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    cn = client.post(f"/invoices/{inv['id']}/credit-note", headers=h,
                     json=_credit_payload()).json()
    r = client.patch(f"/invoices/{cn['id']}", headers=h, json={"notes": "x"})
    assert r.status_code == 409
    # The paid toggle still works (money actually returned).
    r = client.patch(f"/invoices/{cn['id']}/paid", headers=h, json={})
    assert r.status_code == 200
    assert r.json()["is_paid"] is True


def test_invoice_with_credit_note_cannot_be_deleted(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    cn = client.post(f"/invoices/{inv['id']}/credit-note", headers=h,
                     json=_credit_payload()).json()
    assert client.delete(f"/invoices/{inv['id']}", headers=h).status_code == 409
    # Delete the dobropis first, then the invoice goes through.
    assert client.delete(f"/invoices/{cn['id']}", headers=h).status_code == 204
    assert client.delete(f"/invoices/{inv['id']}", headers=h).status_code == 204


# ── PDF & UI ─────────────────────────────────────────────────────────────────

def test_credit_note_pdf_renders(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    cn = client.post(f"/invoices/{inv['id']}/credit-note", headers=h,
                     json=_credit_payload()).json()
    r = client.get(f"/invoices/{cn['id']}/pdf", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "dobropis" in r.headers["content-disposition"]


def test_credit_note_ui_form_prefills_negated_items(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    r = client.get(f"/faktury/{inv['id']}/dobropis")
    assert r.status_code == 200
    assert "Dobropis k faktuře" in r.text
    assert "-500" in r.text          # negated unit price in data-initial-items
    year = str(date.today().year)
    assert f"{year}9001" in r.text   # next number from the dobropis series


def test_credit_note_detail_and_list_render(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    cn = client.post(f"/invoices/{inv['id']}/credit-note", headers=h,
                     json=_credit_payload()).json()
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    r = client.get(f"/faktury/{cn['id']}")
    assert r.status_code == 200
    assert "Opravný doklad k faktuře" in r.text
    # The original's detail lists its dobropis.
    r = client.get(f"/faktury/{inv['id']}")
    assert cn["variable_symbol"] in r.text
    # The list page shows the type badge.
    r = client.get("/faktury")
    assert "Dobropis" in r.text


# ── Backup round-trip ────────────────────────────────────────────────────────

def test_backup_roundtrip_remaps_credit_note_reference(client, db_engine, tmp_path,
                                                       monkeypatch):
    from app import uploads_util
    from app.backup import export_user, import_user
    monkeypatch.setattr(uploads_util, "UPLOADS_DIR", tmp_path)

    h, cid = _setup(client, db_engine, "source")
    inv = _invoice(client, h, cid)
    cn = client.post(f"/invoices/{inv['id']}/credit-note", headers=h,
                     json=_credit_payload()).json()

    with Session(db_engine) as s:
        src = s.exec(select(User).where(User.username == "source")).first()
        archive = export_user(s, src)

    tgt_id = make_user(db_engine, "target")
    with Session(db_engine) as s:
        import_user(s, s.get(User, tgt_id), archive)
        s.commit()

    with Session(db_engine) as s:
        restored = s.exec(
            select(Invoice).where(Invoice.user_id == tgt_id)
            .order_by(Invoice.variable_symbol)
        ).all()
        assert [i.doc_type for i in restored] == ["invoice", "credit_note"]
        original, credit = restored
        assert credit.related_invoice_id == original.id   # remapped, not raw
        assert original.related_invoice_id is None
        assert credit.amount == -1000.0
