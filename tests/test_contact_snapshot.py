"""Contact deletion guard + customer snapshot on invoices.

An issued invoice is a historical document: the customer block it shows is
frozen into Invoice.customer_snapshot at creation time, and a contact that is
referenced by invoices cannot be deleted (SQLite does not enforce the FK, so
the guard is application-level).
"""

from __future__ import annotations

from datetime import date

from sqlmodel import Session, select

from app.models import Contact, Invoice
from tests.conftest import ORIGIN, make_user


def _seed_contact(engine, uid: int, name: str = "Klient A") -> int:
    with Session(engine) as s:
        c = Contact(user_id=uid, name=name, address="Praha 1", ic="00000001",
                    email="klient@example.cz")
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def _login(client, username: str) -> None:
    r = client.post("/login", data={"username": username, "password": "password-1234"})
    assert r.status_code in (200, 303)


def _create_invoice(client, contact_id: int) -> int:
    r = client.post("/invoices", json={
        "contact_id": contact_id,
        "date_issued": "2026-07-01",
        "date_due": "2026-07-15",
        "items": [{"description": "Práce", "quantity": 1, "unit_price": 1000}],
    }, headers=ORIGIN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ── Deletion guard ────────────────────────────────────────────────────────────

def test_delete_contact_with_invoices_is_blocked(client, db_engine):
    uid = make_user(db_engine, "user1")
    cid = _seed_contact(db_engine, uid)
    _login(client, "user1")
    inv_id = _create_invoice(client, cid)

    r = client.post(f"/ui/kontakty/{cid}/smazat", headers=ORIGIN)
    assert r.status_code == 409
    assert "nelze smazat" in r.text

    with Session(db_engine) as s:
        assert s.get(Contact, cid) is not None

    # The invoice and its PDF remain intact.
    assert client.get(f"/invoices/{inv_id}/pdf").status_code == 200


def test_delete_contact_without_invoices_succeeds(client, db_engine):
    uid = make_user(db_engine, "user1")
    cid = _seed_contact(db_engine, uid)
    _login(client, "user1")

    r = client.post(f"/ui/kontakty/{cid}/smazat", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    with Session(db_engine) as s:
        assert s.get(Contact, cid) is None


# ── Customer snapshot ─────────────────────────────────────────────────────────

def test_invoice_freezes_customer_at_creation(client, db_engine):
    uid = make_user(db_engine, "user1")
    cid = _seed_contact(db_engine, uid, name="Původní jméno")
    _login(client, "user1")
    inv_id = _create_invoice(client, cid)

    # Later edit of the contact must not change the issued invoice.
    with Session(db_engine) as s:
        c = s.get(Contact, cid)
        c.name = "Nové jméno"
        c.address = "Brno"
        s.add(c)
        s.commit()

    with Session(db_engine) as s:
        snap = s.get(Invoice, inv_id).get_customer_snapshot()
    assert snap["name"] == "Původní jméno"
    assert snap["address"] == "Praha 1"

    detail = client.get(f"/faktury/{inv_id}")
    assert detail.status_code == 200
    assert "Původní jméno" in detail.text
    assert "Nové jméno" not in detail.text

    assert client.get(f"/invoices/{inv_id}/pdf").status_code == 200


def test_reassigning_invoice_contact_refreshes_snapshot(client, db_engine):
    uid = make_user(db_engine, "user1")
    cid1 = _seed_contact(db_engine, uid, name="Klient A")
    with Session(db_engine) as s:
        c2 = Contact(user_id=uid, name="Klient B", address="Brno", ic="00000002")
        s.add(c2)
        s.commit()
        s.refresh(c2)
        cid2 = c2.id
    _login(client, "user1")
    inv_id = _create_invoice(client, cid1)

    r = client.patch(f"/invoices/{inv_id}", json={"contact_id": cid2}, headers=ORIGIN)
    assert r.status_code == 200, r.text

    with Session(db_engine) as s:
        snap = s.get(Invoice, inv_id).get_customer_snapshot()
    assert snap["name"] == "Klient B"


def test_legacy_invoice_without_snapshot_uses_live_contact(client, db_engine):
    """Invoices from before customer_snapshot existed fall back to the contact."""
    uid = make_user(db_engine, "user1")
    cid = _seed_contact(db_engine, uid, name="Legacy klient")
    with Session(db_engine) as s:
        inv = Invoice(
            user_id=uid, contact_id=cid, variable_symbol="20260001", amount=100.0,
            date_issued=date(2026, 1, 1), date_due=date(2026, 1, 15),
        )
        inv.set_items([{"description": "X", "quantity": "1",
                        "unit": "ks", "unit_price": "100", "vat_rate": "0"}])
        s.add(inv)
        s.commit()
        s.refresh(inv)
        inv_id = inv.id
        assert inv.get_customer_snapshot() == {}

    _login(client, "user1")
    detail = client.get(f"/faktury/{inv_id}")
    assert detail.status_code == 200
    assert "Legacy klient" in detail.text
    assert client.get(f"/invoices/{inv_id}/pdf").status_code == 200
