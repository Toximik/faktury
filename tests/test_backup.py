"""Tests for per-user data export / import (app/backup.py + admin routes)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app import uploads_util
from app.backup import BackupError, export_user, import_user
from app.models import Contact, Expense, Invoice, SupplierProfile, User
from tests.conftest import ORIGIN, make_user

# Minimal byte blobs whose magic bytes match their declared type.
PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32
PDF = b"%PDF-1.4\n" + b"x" * 32


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    """Redirect the uploads dir to a temp path for the duration of the test."""
    monkeypatch.setattr(uploads_util, "UPLOADS_DIR", tmp_path)
    return tmp_path


def _seed_source(engine, uploads) -> int:
    """Create a user with a profile (logo+stamp), 2 contacts, 3 invoices, 2 expenses."""
    uid = make_user(engine, "source")
    user_dir = uploads / str(uid)
    (user_dir / "expenses").mkdir(parents=True)
    (user_dir / "logo.png").write_bytes(PNG)
    (user_dir / "stamp.png").write_bytes(PNG)

    with Session(engine) as s:
        prof = s.exec(
            select(SupplierProfile).where(SupplierProfile.user_id == uid)
        ).first()
        prof.name = "Jan Novák"
        prof.ic = "12345678"
        prof.bank_number = "2000145399"
        prof.bank_code = "0800"
        prof.trade_register_date = date(2020, 1, 15)
        prof.logo_path = str(user_dir / "logo.png")
        prof.stamp_path = str(user_dir / "stamp.png")
        s.add(prof)

        c1 = Contact(user_id=uid, name="Klient A", address="Praha", ic="00000001",
                     payment_due_days=14)
        c2 = Contact(user_id=uid, name="Klient B", address="Brno", ic="00000002")
        s.add(c1)
        s.add(c2)
        s.commit()
        s.refresh(c1)
        s.refresh(c2)

        snap = prof.to_pdf_dict()  # includes the absolute logo/stamp paths
        plan = [(c1.id, "20240001", False), (c1.id, "20240002", True),
                (c2.id, "20240003", False)]
        for i, (cid, vs, paid) in enumerate(plan):
            inv = Invoice(
                user_id=uid, contact_id=cid, variable_symbol=vs, amount=1000.0 + i,
                currency="CZK", payment_method="transfer",
                date_issued=date(2024, 1, 1), date_due=date(2024, 1, 15),
                date_paid=date(2024, 1, 10) if paid else None, is_paid=paid,
            )
            inv.set_items([{"description": f"Práce {i}", "quantity": "1",
                            "unit": "ks", "unit_price": "1000", "vat_rate": "0"}])
            inv.set_supplier_snapshot(snap)
            s.add(inv)

        e1 = Expense(user_id=uid, date=date(2024, 2, 1), category="SW",
                     amount=50.0, currency="CZK", description="Licence")
        s.add(e1)
        s.commit()
        s.refresh(e1)
        scan = user_dir / "expenses" / f"{e1.id}.pdf"
        scan.write_bytes(PDF)
        e1.attachment_path = str(scan)
        s.add(e1)

        e2 = Expense(user_id=uid, date=date(2024, 3, 1), category="HW",
                     amount=999.0, currency="CZK", description="Myš")
        s.add(e2)
        s.commit()
    return uid


def _empty_archive(data=None, format_version=1) -> bytes:
    """Build a minimal valid-structure archive with no files."""
    if data is None:
        data = {"supplier_profile": None, "contacts": [], "invoices": [], "expenses": []}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(
            {"format_version": format_version, "app": "fakturace"}))
        zf.writestr("data.json", json.dumps(data))
    return buf.getvalue()


# ── Round-trip ───────────────────────────────────────────────────────────────

def test_export_archive_structure(db_engine, uploads):
    src_id = _seed_source(db_engine, uploads)
    with Session(db_engine) as s:
        archive = export_user(s, s.get(User, src_id))

    zf = zipfile.ZipFile(io.BytesIO(archive))
    names = set(zf.namelist())
    assert {"manifest.json", "data.json", "files/logo.png", "files/stamp.png"} <= names
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format_version"] == 1
    assert manifest["source_username"] == "source"
    assert manifest["counts"] == {"contacts": 2, "invoices": 3, "expenses": 2, "files": 3}


def test_export_import_roundtrip(db_engine, uploads):
    src_id = _seed_source(db_engine, uploads)
    with Session(db_engine) as s:
        archive = export_user(s, s.get(User, src_id))

    tgt_id = make_user(db_engine, "target")
    with Session(db_engine) as s:
        summary = import_user(s, s.get(User, tgt_id), archive)
    assert summary == {"contacts": 2, "invoices": 3, "expenses": 2}

    with Session(db_engine) as s:
        contacts = s.exec(select(Contact).where(Contact.user_id == tgt_id)).all()
        invoices = s.exec(
            select(Invoice).where(Invoice.user_id == tgt_id)
            .order_by(Invoice.variable_symbol)
        ).all()
        expenses = s.exec(select(Expense).where(Expense.user_id == tgt_id)).all()

        assert len(contacts) == 2 and len(invoices) == 3 and len(expenses) == 2

        # contact_id remapped to the target user's own contacts
        cids = {c.id for c in contacts}
        assert all(inv.contact_id in cids for inv in invoices)

        # line items + supplier snapshot preserved
        inv0 = invoices[0]
        assert inv0.get_items()[0]["description"] == "Práce 0"
        snap = inv0.get_supplier_snapshot()
        assert snap["name"] == "Jan Novák"

        # snapshot image path remapped under the target dir and file restored
        assert str(uploads / str(tgt_id)) in snap["logo_path"]
        assert Path(snap["logo_path"]).read_bytes() == PNG

        # profile images restored
        prof = s.exec(
            select(SupplierProfile).where(SupplierProfile.user_id == tgt_id)
        ).first()
        assert prof.name == "Jan Novák"
        assert prof.trade_register_date == date(2020, 1, 15)
        assert Path(prof.logo_path).read_bytes() == PNG
        assert Path(prof.stamp_path).read_bytes() == PNG

        # expense scan restored under the new expense id
        scanned = [e for e in expenses if e.attachment_path]
        assert len(scanned) == 1
        assert Path(scanned[0].attachment_path).read_bytes() == PDF
        assert f"expenses/{scanned[0].id}.pdf" in scanned[0].attachment_path.replace("\\", "/")


def test_customer_snapshot_roundtrip_and_legacy_synthesis(db_engine, uploads):
    """A customer snapshot survives verbatim; a legacy invoice without one gets
    it synthesised from the archived contact on import."""
    src_id = _seed_source(db_engine, uploads)
    with Session(db_engine) as s:
        invoices = s.exec(
            select(Invoice).where(Invoice.user_id == src_id)
            .order_by(Invoice.variable_symbol)
        ).all()
        # First invoice: frozen snapshot that deliberately differs from the
        # live contact ("Klient A"). Remaining invoices stay legacy ("{}").
        invoices[0].set_customer_snapshot(
            {"name": "Historický název", "address": "Stará 1", "ic": "00000001",
             "dic": "", "email": ""})
        s.add(invoices[0])
        s.commit()
        archive = export_user(s, s.get(User, src_id))

    tgt_id = make_user(db_engine, "target")
    with Session(db_engine) as s:
        import_user(s, s.get(User, tgt_id), archive)
        restored = s.exec(
            select(Invoice).where(Invoice.user_id == tgt_id)
            .order_by(Invoice.variable_symbol)
        ).all()
        assert restored[0].get_customer_snapshot()["name"] == "Historický název"
        # Legacy rows: synthesised from the contact they were issued to.
        assert restored[1].get_customer_snapshot()["name"] == "Klient A"
        assert restored[2].get_customer_snapshot()["name"] == "Klient B"


# ── Snapshot image paths (untrusted in both directions) ──────────────────────

def test_export_drops_snapshot_paths_it_cannot_bundle(db_engine, uploads, tmp_path):
    """A snapshot path outside the user's uploads dir (or whose file is gone)
    must not leak into the archive as an absolute server path."""
    uid = make_user(db_engine, "source")
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG)

    with Session(db_engine) as s:
        c = Contact(user_id=uid, name="Klient", address="Praha", ic="00000001")
        s.add(c)
        s.commit()
        s.refresh(c)
        inv = Invoice(
            user_id=uid, contact_id=c.id, variable_symbol="20240001", amount=100.0,
            date_issued=date(2024, 1, 1), date_due=date(2024, 1, 15),
        )
        inv.set_items([])
        inv.set_supplier_snapshot({
            "name": "Jan Novák",
            "logo_path": str(outside),                            # outside uploads/
            "stamp_path": str(uploads / str(uid) / "gone.png"),   # inside, but missing
        })
        s.add(inv)
        s.commit()
        archive = export_user(s, s.get(User, uid))

    zf = zipfile.ZipFile(io.BytesIO(archive))
    snap = json.loads(zf.read("data.json"))["invoices"][0]["supplier_snapshot"]
    assert snap["logo_path"] == ""
    assert snap["stamp_path"] == ""
    assert not any(n.startswith("files/") for n in zf.namelist())


def test_import_sanitizes_non_archive_snapshot_paths(db_engine, uploads):
    """A crafted archive must not smuggle a server filesystem path into a
    restored supplier snapshot (the PDF renderer reads those paths off disk)."""
    data = {
        "supplier_profile": None,
        "contacts": [{"id": 1, "name": "Klient", "address": "Praha", "ic": "00000001"}],
        "invoices": [{
            "contact_id": 1, "variable_symbol": "20240001", "amount": 100.0,
            "date_issued": "2024-01-01", "date_due": "2024-01-15",
            "items": [],
            "supplier_snapshot": {
                "name": "Jan Novák",
                "logo_path": "/etc/passwd",
                "stamp_path": "../../uploads/1/stamp.png",
            },
        }],
        "expenses": [],
    }
    tgt_id = make_user(db_engine, "target")
    with Session(db_engine) as s:
        import_user(s, s.get(User, tgt_id), _empty_archive(data))
        inv = s.exec(select(Invoice).where(Invoice.user_id == tgt_id)).first()
        snap = inv.get_supplier_snapshot()
    assert snap["logo_path"] == ""
    assert snap["stamp_path"] == ""
    assert snap["name"] == "Jan Novák"


# ── Import guards ──────────────────────────────────────────────────────────────

def test_import_refuses_non_empty_target(db_engine, uploads):
    src_id = _seed_source(db_engine, uploads)
    with Session(db_engine) as s:
        archive = export_user(s, s.get(User, src_id))
    # The source user already owns data → import into it must be refused.
    with Session(db_engine) as s:
        with pytest.raises(BackupError, match="prázdného"):
            import_user(s, s.get(User, src_id), archive)


def test_import_rejects_unknown_format_version(db_engine, uploads):
    tgt_id = make_user(db_engine, "target")
    with Session(db_engine) as s:
        with pytest.raises(BackupError, match="verze"):
            import_user(s, s.get(User, tgt_id), _empty_archive(format_version=999))


def test_import_rejects_zip_slip(db_engine, uploads):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format_version": 1}))
        zf.writestr("data.json", json.dumps(
            {"supplier_profile": None, "contacts": [], "invoices": [], "expenses": []}))
        zf.writestr("files/../../evil.png", PNG)
    tgt_id = make_user(db_engine, "target")
    with Session(db_engine) as s:
        with pytest.raises(BackupError, match="Nebezpečná"):
            import_user(s, s.get(User, tgt_id), buf.getvalue())


def test_import_rejects_bad_magic_bytes(db_engine, uploads):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format_version": 1}))
        zf.writestr("data.json", json.dumps(
            {"supplier_profile": None, "contacts": [], "invoices": [], "expenses": []}))
        zf.writestr("files/logo.png", b"<script>not a png</script>")
    tgt_id = make_user(db_engine, "target")
    with Session(db_engine) as s:
        with pytest.raises(BackupError, match="neodpovídá"):
            import_user(s, s.get(User, tgt_id), buf.getvalue())


def test_import_rejects_garbage_zip(db_engine, uploads):
    tgt_id = make_user(db_engine, "target")
    with Session(db_engine) as s:
        with pytest.raises(BackupError, match="ZIP"):
            import_user(s, s.get(User, tgt_id), b"this is not a zip file")


# ── HTTP endpoints ─────────────────────────────────────────────────────────────

def test_export_endpoint_requires_admin(client, db_engine, uploads):
    make_user(db_engine, "alice")  # non-admin
    client.post("/login", data={"username": "alice", "password": "password-1234"},
                headers=ORIGIN)
    r = client.get("/admin/zaloha/export/1", follow_redirects=False)
    assert r.status_code == 403


def test_http_export_then_import_new_user(client, db_engine, uploads):
    make_user(db_engine, "master", is_admin=True)
    src_id = _seed_source(db_engine, uploads)
    client.post("/login", data={"username": "master", "password": "password-1234"},
                headers=ORIGIN)

    r = client.get(f"/admin/zaloha/export/{src_id}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    archive = r.content

    r2 = client.post(
        "/ui/admin/zaloha/import",
        data={"target": "__new__", "new_username": "restored",
              "new_password": "password-1234"},
        files={"archive": ("backup.zip", archive, "application/zip")},
        headers=ORIGIN,
    )
    assert r2.status_code == 200, r2.text
    assert "dokončen" in r2.text

    with Session(db_engine) as s:
        restored = s.exec(select(User).where(User.username == "restored")).first()
        assert restored is not None
        invoices = s.exec(select(Invoice).where(Invoice.user_id == restored.id)).all()
        assert len(invoices) == 3


def test_http_import_into_nonempty_user_returns_409(client, db_engine, uploads):
    make_user(db_engine, "master", is_admin=True)
    src_id = _seed_source(db_engine, uploads)
    client.post("/login", data={"username": "master", "password": "password-1234"},
                headers=ORIGIN)
    archive = client.get(f"/admin/zaloha/export/{src_id}").content

    # Import back into the (non-empty) source user → 409.
    r = client.post(
        "/ui/admin/zaloha/import",
        data={"target": str(src_id)},
        files={"archive": ("backup.zip", archive, "application/zip")},
        headers=ORIGIN,
    )
    assert r.status_code == 409
    assert "prázdného" in r.text
