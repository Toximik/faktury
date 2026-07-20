"""
backup.py – Per-user data export / import (full backup & restore).

A backup is a single ``.zip``::

    manifest.json   metadata, record counts, and the format version
    data.json       the user's SupplierProfile + Contacts + Invoices + Expenses
    files/          uploaded binaries (logo, stamp, expense scans),
                    referenced by archive-relative path from data.json

Design notes
------------
* Restore-to-fresh: ``import_user`` refuses to write into a user that already
  owns any Contact / Invoice / Expense rows (a blank SupplierProfile is fine).
* IDs are NOT preserved: rows are re-inserted with fresh autoincrement ids
  under the *target* user, and ``Invoice.contact_id`` is remapped via an
  old→new contact-id map.
* JSON columns (``Invoice.items`` / ``supplier_snapshot``) are embedded as
  parsed JSON for readability and re-serialised with the model helpers.
* Filesystem paths never leave the box. On export every path under
  ``uploads/<user>/`` becomes an archive ref (``files/...``); on import it is
  rebuilt under ``uploads/<new user>/``. Paths outside the uploads dir are
  dropped. The logo/stamp paths frozen inside each invoice's supplier snapshot
  are remapped too, so restored historical PDFs still find their images.
* The archive is untrusted input: it is fully validated (zip-slip, size/count
  caps, magic-byte sniff, format version) before any DB row or file is written.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from sqlmodel import Session, select

from app import uploads_util
from app.config import DEFAULT_BRAND_COLOR
from app.models import Contact, Expense, Invoice, SupplierProfile, User
from app.uploads_util import (
    MAX_SCAN_BYTES,
    content_matches_type,
    content_type_for_ext,
)

FORMAT_VERSION = 1
APP_TAG = "fakturace"

# ── Import hardening caps ─────────────────────────────────────────────────────

_MAX_FILE_COUNT = 10_000                       # zip entry count
_MAX_TOTAL_UNCOMPRESSED = 500 * 1024 * 1024    # 500 MB – zip-bomb guard
_MAX_BINARY_ENTRY = MAX_SCAN_BYTES             # 10 MB per uploaded file
_MAX_JSON_ENTRY = 64 * 1024 * 1024             # 64 MB for data.json / manifest.json


class BackupError(Exception):
    """Raised when an archive is malformed, unsafe, or cannot be restored."""


# ══════════════════════════════════════════════════════════════════════════════
#  Export
# ══════════════════════════════════════════════════════════════════════════════

def export_user(session: Session, user: User) -> bytes:
    """Serialise all data owned by ``user`` into an in-memory backup zip."""
    user_dir = (uploads_util.UPLOADS_DIR / str(user.id)).resolve()
    files_to_add: dict[str, Path] = {}  # archive ref → source path (deduped)

    def _ref(stored_path: Optional[str]) -> Optional[str]:
        """Turn an absolute uploads path into an archive ref, registering the file.

        Returns None when the path is empty, lives outside the user's uploads
        dir, or no longer exists on disk.
        """
        if not stored_path:
            return None
        src = Path(stored_path)
        try:
            rel = src.resolve().relative_to(user_dir)
        except (ValueError, OSError):
            return None
        if not src.is_file():
            return None
        ref = "files/" + rel.as_posix()
        files_to_add[ref] = src
        return ref

    # ── SupplierProfile ──────────────────────────────────────────────────────
    profile = session.exec(
        select(SupplierProfile).where(SupplierProfile.user_id == user.id)
    ).first()
    profile_data: Optional[dict[str, Any]] = None
    if profile is not None:
        profile_data = {
            "name": profile.name,
            "address": profile.address,
            "ic": profile.ic,
            "dic": profile.dic,
            "email": profile.email,
            "phone": profile.phone,
            "bank_prefix": profile.bank_prefix,
            "bank_number": profile.bank_number,
            "bank_code": profile.bank_code,
            "bank_iban": profile.bank_iban,
            "trade_register_date": (
                profile.trade_register_date.isoformat()
                if profile.trade_register_date else None
            ),
            "brand_color": profile.brand_color,
            "logo_file": _ref(profile.logo_path),
            "stamp_file": _ref(profile.stamp_path),
        }

    # ── Contacts ───────────────────────────────────────────────────────────────
    contacts = session.exec(
        select(Contact).where(Contact.user_id == user.id).order_by(Contact.id)
    ).all()
    contacts_data = [{
        "id": c.id,
        "name": c.name,
        "address": c.address,
        "ic": c.ic,
        "dic": c.dic,
        "email": c.email,
        "bank_prefix": c.bank_prefix,
        "bank_number": c.bank_number,
        "bank_code": c.bank_code,
        "bank_iban": c.bank_iban,
        "payment_due_days": c.payment_due_days,
    } for c in contacts]

    # ── Invoices ───────────────────────────────────────────────────────────────
    invoices = session.exec(
        select(Invoice).where(Invoice.user_id == user.id).order_by(Invoice.id)
    ).all()
    invoices_data = []
    for inv in invoices:
        snapshot = inv.get_supplier_snapshot()
        # Remap the uploads paths frozen in the snapshot so the restored
        # historical PDF still finds its logo/stamp on the new server. A path
        # that cannot be turned into an archive ref (outside the user's uploads
        # dir, or the file is gone) is dropped: absolute server paths must
        # never leave the box.
        for key in ("logo_path", "stamp_path"):
            val = snapshot.get(key)
            if val:
                snapshot[key] = _ref(val) or ""
        invoices_data.append({
            # The archive-local id exists solely so a dobropis's
            # related_invoice_id can be remapped on import (ids are never
            # preserved across a restore).
            "id": inv.id,
            "contact_id": inv.contact_id,
            "variable_symbol": inv.variable_symbol,
            "amount": inv.amount,
            "currency": inv.currency,
            "payment_method": inv.payment_method,
            "date_issued": inv.date_issued.isoformat(),
            "date_due": inv.date_due.isoformat(),
            "date_paid": inv.date_paid.isoformat() if inv.date_paid else None,
            "is_paid": inv.is_paid,
            "items": inv.get_items(),
            "notes": inv.notes,
            "doc_type": inv.doc_type,
            "related_invoice_id": inv.related_invoice_id,
            "supplier_snapshot": snapshot,
            "customer_snapshot": inv.get_customer_snapshot(),
        })

    # ── Expenses ───────────────────────────────────────────────────────────────
    expenses = session.exec(
        select(Expense).where(Expense.user_id == user.id).order_by(Expense.id)
    ).all()
    expenses_data = [{
        "date": e.date.isoformat(),
        "category": e.category,
        "amount": e.amount,
        "currency": e.currency,
        "description": e.description,
        "attachment_file": _ref(e.attachment_path),
    } for e in expenses]

    data = {
        "supplier_profile": profile_data,
        "contacts": contacts_data,
        "invoices": invoices_data,
        "expenses": expenses_data,
    }
    manifest = {
        "format_version": FORMAT_VERSION,
        "app": APP_TAG,
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_username": user.username,
        "source_user_id": user.id,
        "counts": {
            "contacts": len(contacts_data),
            "invoices": len(invoices_data),
            "expenses": len(expenses_data),
            "files": len(files_to_add),
        },
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("data.json", json.dumps(data, ensure_ascii=False, indent=2))
        for ref, src in files_to_add.items():
            zf.write(src, ref)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
#  Import
# ══════════════════════════════════════════════════════════════════════════════

def user_has_data(session: Session, user_id: int) -> bool:
    """True if the user owns any Contact / Invoice / Expense rows."""
    for model in (Contact, Invoice, Expense):
        if session.exec(select(model).where(model.user_id == user_id)).first() is not None:
            return True
    return False


def _assert_safe_member(name: str) -> None:
    """Reject zip entry names that are absolute or escape the archive root."""
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
        raise BackupError(f"Nebezpečná cesta v archivu: {name}")


def _read_json_member(zf: zipfile.ZipFile, name: str) -> Any:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise BackupError(f"V archivu chybí {name}.")
    if info.file_size > _MAX_JSON_ENTRY:
        raise BackupError(f"{name} je příliš velký.")
    try:
        return json.loads(zf.read(name).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError(f"{name} není platný JSON.")


def import_user(session: Session, target_user: User, archive_bytes: bytes) -> dict[str, int]:
    """Restore a backup zip into ``target_user`` (which must own no data).

    Returns a dict of restored counts. Raises BackupError on any problem; the
    DB transaction is rolled back and any files written are removed, so a
    failed import leaves no trace.
    """
    if user_has_data(session, target_user.id):
        raise BackupError(
            "Cílový uživatel již obsahuje data. Import je možný jen do prázdného účtu."
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile:
        raise BackupError("Soubor není platný ZIP archiv.")

    new_dir = uploads_util.UPLOADS_DIR / str(target_user.id)
    written: list[Path] = []

    with zf:
        infos = zf.infolist()
        if len(infos) > _MAX_FILE_COUNT:
            raise BackupError("Archiv obsahuje příliš mnoho položek.")
        if sum(i.file_size for i in infos) > _MAX_TOTAL_UNCOMPRESSED:
            raise BackupError("Archiv je příliš velký (možná zip bomba).")

        manifest = _read_json_member(zf, "manifest.json")
        if manifest.get("format_version") != FORMAT_VERSION:
            raise BackupError(
                f"Nepodporovaná verze zálohy: {manifest.get('format_version')!r}."
            )
        data = _read_json_member(zf, "data.json")

        # ── Validate & buffer every uploaded file (untrusted) ──────────────────
        file_bytes: dict[str, bytes] = {}
        for info in infos:
            name = info.filename
            if name in ("manifest.json", "data.json") or name.endswith("/"):
                continue
            if not name.startswith("files/"):
                raise BackupError(f"Neočekávaná položka v archivu: {name}")
            _assert_safe_member(name)
            if info.file_size > _MAX_BINARY_ENTRY:
                raise BackupError(f"Soubor v archivu je příliš velký: {name}")
            blob = zf.read(name)
            ext = name.rsplit(".", 1)[-1] if "." in name else ""
            ctype = content_type_for_ext(ext)
            if ctype is None or not content_matches_type(blob, ctype):
                raise BackupError(f"Obsah souboru neodpovídá příponě: {name}")
            file_bytes[name] = blob

        try:
            counts = _restore_rows(session, target_user, data, file_bytes, new_dir, written)
            session.commit()
        except BackupError:
            session.rollback()
            _cleanup(written)
            raise
        except Exception as exc:  # noqa: BLE001 – surface any failure as BackupError
            session.rollback()
            _cleanup(written)
            raise BackupError(f"Import selhal: {exc}") from exc

    return counts


def _cleanup(written: list[Path]) -> None:
    for p in written:
        p.unlink(missing_ok=True)


def _restore_file(
    ref: Optional[str],
    file_bytes: dict[str, bytes],
    new_dir: Path,
    written: list[Path],
    dest_rel: Optional[str] = None,
) -> Optional[str]:
    """Write an archive file under the target user's uploads dir; return its path."""
    if not ref:
        return None
    if ref not in file_bytes:
        raise BackupError(f"V archivu chybí soubor: {ref}")
    rel = dest_rel if dest_rel is not None else ref[len("files/"):]
    dest = new_dir / rel
    # zip-slip guard for the destination path
    try:
        dest.resolve().relative_to(new_dir.resolve())
    except ValueError:
        raise BackupError("Nebezpečná cílová cesta souboru.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(file_bytes[ref])
    written.append(dest)
    return str(dest)


def _restore_rows(
    session: Session,
    target_user: User,
    data: dict[str, Any],
    file_bytes: dict[str, bytes],
    new_dir: Path,
    written: list[Path],
) -> dict[str, int]:
    # ── SupplierProfile (upsert: unique per user) ──────────────────────────────
    prof_data = data.get("supplier_profile")
    if prof_data is not None:
        profile = session.exec(
            select(SupplierProfile).where(SupplierProfile.user_id == target_user.id)
        ).first()
        if profile is None:
            profile = SupplierProfile(user_id=target_user.id)
        profile.name = prof_data.get("name") or ""
        profile.address = prof_data.get("address") or ""
        profile.ic = prof_data.get("ic") or ""
        profile.dic = prof_data.get("dic")
        profile.email = prof_data.get("email")
        profile.phone = prof_data.get("phone")
        profile.bank_prefix = prof_data.get("bank_prefix")
        profile.bank_number = prof_data.get("bank_number") or ""
        profile.bank_code = prof_data.get("bank_code") or ""
        profile.bank_iban = prof_data.get("bank_iban")
        trd = prof_data.get("trade_register_date")
        profile.trade_register_date = date.fromisoformat(trd) if trd else None
        profile.brand_color = prof_data.get("brand_color") or DEFAULT_BRAND_COLOR
        profile.logo_path = _restore_file(prof_data.get("logo_file"), file_bytes, new_dir, written)
        profile.stamp_path = _restore_file(prof_data.get("stamp_file"), file_bytes, new_dir, written)
        session.add(profile)

    # ── Contacts (build old→new id map) ────────────────────────────────────────
    id_map: dict[Any, int] = {}
    contact_snap_map: dict[Any, dict[str, Any]] = {}  # old id → snapshot fields
    for c in data.get("contacts", []):
        contact = Contact(
            user_id=target_user.id,
            name=c.get("name") or "",
            address=c.get("address") or "",
            ic=c.get("ic") or "",
            dic=c.get("dic"),
            email=c.get("email"),
            bank_prefix=c.get("bank_prefix"),
            bank_number=c.get("bank_number"),
            bank_code=c.get("bank_code"),
            bank_iban=c.get("bank_iban"),
            payment_due_days=c.get("payment_due_days"),
        )
        session.add(contact)
        session.flush()  # assign autoincrement id
        id_map[c.get("id")] = contact.id
        contact_snap_map[c.get("id")] = contact.to_snapshot_dict()

    # ── Invoices (remap contact_id + related_invoice_id + snapshot paths) ──────
    # Invoices are archived in id order, so a dobropis (always created after
    # its corrected invoice) is processed after the original is already in the
    # map. related_invoice_id is only ever taken from the map — a crafted raw
    # value can never end up pointing at another tenant's row.
    invoice_id_map: dict = {}
    for inv in data.get("invoices", []):
        new_cid = id_map.get(inv.get("contact_id"))
        if new_cid is None:
            raise BackupError("Faktura odkazuje na neznámý kontakt.")
        snapshot = dict(inv.get("supplier_snapshot") or {})
        for key in ("logo_path", "stamp_path"):
            val = snapshot.get(key)
            if isinstance(val, str) and val.startswith("files/"):
                snapshot[key] = _restore_file(val, file_bytes, new_dir, written)
            elif val:
                # The archive is untrusted: anything that is not an archive ref
                # (e.g. an absolute path crafted to point at a server file)
                # must never be stored — the PDF renderer reads snapshot image
                # paths off disk (_image_to_data_uri).
                snapshot[key] = ""
        doc_type = inv.get("doc_type") or "invoice"
        invoice = Invoice(
            user_id=target_user.id,
            contact_id=new_cid,
            variable_symbol=inv["variable_symbol"],
            amount=float(inv["amount"]),
            currency=inv.get("currency") or "CZK",
            payment_method=inv.get("payment_method") or "transfer",
            date_issued=date.fromisoformat(inv["date_issued"]),
            date_due=date.fromisoformat(inv["date_due"]),
            date_paid=date.fromisoformat(inv["date_paid"]) if inv.get("date_paid") else None,
            is_paid=bool(inv.get("is_paid", False)),
            notes=inv.get("notes"),
            doc_type=doc_type,
            related_invoice_id=(
                invoice_id_map.get(inv.get("related_invoice_id"))
                if doc_type == "credit_note" else None
            ),
        )
        invoice.set_items(inv.get("items") or [])
        invoice.set_supplier_snapshot(snapshot)
        # Archives from before customer_snapshot existed carry none: synthesise
        # it from the archived contact so the restored invoice is frozen too.
        invoice.set_customer_snapshot(
            inv.get("customer_snapshot") or contact_snap_map.get(inv.get("contact_id")) or {}
        )
        session.add(invoice)
        session.flush()  # assign autoincrement id for related_invoice_id remaps
        if inv.get("id") is not None:
            invoice_id_map[inv.get("id")] = invoice.id

    # ── Expenses (restore scan under expenses/<new id>.<ext>) ──────────────────
    for e in data.get("expenses", []):
        expense = Expense(
            user_id=target_user.id,
            date=date.fromisoformat(e["date"]),
            category=e.get("category") or "",
            amount=float(e["amount"]),
            currency=e.get("currency") or "CZK",
            description=e.get("description") or "",
        )
        session.add(expense)
        session.flush()  # assign autoincrement id
        att = e.get("attachment_file")
        if att:
            ext = att.rsplit(".", 1)[-1] if "." in att else "bin"
            expense.attachment_path = _restore_file(
                att, file_bytes, new_dir, written,
                dest_rel=f"expenses/{expense.id}.{ext}",
            )
            session.add(expense)

    return {
        "contacts": len(data.get("contacts", [])),
        "invoices": len(data.get("invoices", [])),
        "expenses": len(data.get("expenses", [])),
    }
