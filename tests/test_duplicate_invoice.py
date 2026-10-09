"""Duplikovat fakturu: the new-invoice form prefilled from an existing invoice."""

from __future__ import annotations

import re
from datetime import date

from sqlmodel import Session

from app.models import Invoice

from tests.conftest import make_user
from tests.test_credit_notes import _credit_payload, _invoice, _setup


def _login(client, username="alice"):
    client.post("/login", data={"username": username, "password": "password-1234"})


def test_duplicate_form_prefills_from_original(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid, notes="Měsíční paušál",
                   items=[{"description": "Web care September", "quantity": 1,
                           "unit_price": 4200, "vat_rate": 0}])
    _login(client)
    r = client.get(f"/faktury/{inv['id']}/duplikovat")
    assert r.status_code == 200
    assert f"kopie {inv['variable_symbol']}" in r.text
    assert "Web care September" in r.text      # items in data-initial-items
    assert "Měsíční paušál" in r.text          # notes copied
    assert re.search(rf'<option value="{cid}"[^>]*selected', r.text)  # client
    assert 'value="14"' in r.text              # 2026-01-01 → 2026-01-15 term
    assert date.today().isoformat() in r.text  # issue date is today
    year = str(date.today().year)
    assert f"{year}0002" in r.text             # next number, not the original's
    # Saving is a plain create — the original is untouched.
    r = client.post("/invoices", headers=h, json={
        "contact_id": cid, "date_issued": date.today().isoformat(),
        "date_due": date.today().isoformat(),
        "items": [{"description": "Správa webu – říjen", "quantity": 1,
                   "unit_price": 4200, "vat_rate": 0}]})
    assert r.status_code == 201
    assert r.json()["variable_symbol"] == f"{year}0002"
    with Session(db_engine) as s:
        assert s.get(Invoice, inv["id"]).get_items()[0]["description"] == \
            "Web care September"


def test_duplicate_other_users_invoice_is_404(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    make_user(db_engine, "bob")
    _login(client, "bob")
    r = client.get(f"/faktury/{inv['id']}/duplikovat")
    assert r.status_code == 404


def test_duplicate_credit_note_redirects(client, db_engine):
    h, cid = _setup(client, db_engine)
    inv = _invoice(client, h, cid)
    cn = client.post(f"/invoices/{inv['id']}/credit-note", headers=h,
                     json=_credit_payload()).json()
    _login(client)
    r = client.get(f"/faktury/{cn['id']}/duplikovat", follow_redirects=False)
    assert r.status_code == 303
