"""
routers/invoices.py – Invoice CRUD and PDF generation endpoints.

Endpoints
---------
  POST   /invoices                  Create a new invoice.
  POST   /invoices/{id}/credit-note Issue a dobropis (credit note) against an invoice.
  GET    /invoices                  List invoices (filterable by year / status).
  PATCH  /invoices/{id}/paid        Toggle the Paid/Unpaid state.
  DELETE /invoices/{id}             Remove an unpaid invoice.
  GET    /invoices/{id}/pdf         Generate and stream the invoice PDF.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth import get_current_user
from app.config import DEFAULT_BRAND_COLOR
from app.database import get_session
from app.logic import (
    generate_spayd_qr,
    get_next_credit_note_symbol,
    get_next_variable_symbol,
    render_invoice_to_pdf,
    slugify_filename,
)
from app.models import Contact, Invoice, SupplierProfile, User
from app.schemas import (
    CreditNoteCreate,
    InvoiceCreate,
    InvoiceRead,
    InvoiceSummary,
    InvoiceUpdate,
    LineItem,
    MarkPaidRequest,
)

router = APIRouter(prefix="/invoices", tags=["invoices"])

_TWO_PLACES = Decimal("0.01")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _compute_total(items: list[LineItem]) -> float:
    """Sum line-item totals (incl. VAT) using Decimal arithmetic.

    Rounding is applied per-line (matching Czech bookkeeping practice),
    then the rounded values are summed.
    """
    total = Decimal("0")
    for item in items:
        subtotal = (item.quantity * item.unit_price).quantize(
            _TWO_PLACES, rounding=ROUND_HALF_UP
        )
        vat = (subtotal * item.vat_rate).quantize(
            _TWO_PLACES, rounding=ROUND_HALF_UP
        )
        total += subtotal + vat
    return float(total)


def _get_contact_or_404(session: Session, contact_id: int, user_id: int) -> Contact:
    """Fetch a contact, scoped to its owner.

    A contact belonging to another user is reported as 404 (never 403) so the
    endpoint does not leak the existence of other tenants' records.
    """
    contact = session.get(Contact, contact_id)
    if contact is None or contact.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contact {contact_id} not found.",
        )
    return contact


def _assert_vat_consistency(supplier: SupplierProfile | None, items: list[LineItem]) -> None:
    """Reject VAT on invoices issued by a non-VAT payer (neplátce DPH).

    A supplier is a VAT payer only if their profile carries a DIČ. A neplátce
    must not charge VAT, and the PDF layout has no VAT-breakdown section, so a
    non-zero rate would also render an invoice whose line sums don't match the
    total. (A full plátce-DPH layout with a VAT recap is a separate feature.)
    """
    is_vat_payer = bool(supplier and (supplier.dic or "").strip())
    if not is_vat_payer and any(item.vat_rate > 0 for item in items):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Neplátce DPH nemůže účtovat DPH – sazba musí být 0. "
                   "Doplňte DIČ v profilu dodavatele, pokud jste plátce DPH.",
        )


def _get_invoice_or_404(session: Session, invoice_id: int) -> Invoice:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice {invoice_id} not found.",
        )
    return invoice


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("", response_model=InvoiceRead, status_code=status.HTTP_201_CREATED)
def create_invoice(
    payload: InvoiceCreate,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> InvoiceRead:
    """Create a new invoice.

    The Variable Symbol is assigned automatically (YYYYNN, current year).
    The total ``amount`` is computed server-side from the line items so
    the stored value is always consistent with the line-item breakdown.
    """
    contact = _get_contact_or_404(session, payload.contact_id, _user.id)

    # Snapshot the supplier profile at creation time (frozen forever).
    supplier = session.exec(
        select(SupplierProfile).where(SupplierProfile.user_id == _user.id)
    ).first()
    _assert_vat_consistency(supplier, payload.items)
    snapshot = supplier.to_pdf_dict() if supplier else {}

    total = _compute_total(payload.items)
    items_json = json.dumps(
        [item.model_dump(mode="json") for item in payload.items],
        ensure_ascii=False,
    )

    def _new_invoice(vs: str) -> Invoice:
        inv = Invoice(
            user_id=_user.id,
            contact_id=payload.contact_id,
            variable_symbol=vs,
            amount=total,
            currency=payload.currency,
            payment_method=payload.payment_method,
            date_issued=payload.date_issued,
            date_due=payload.date_due,
            items=items_json,
            notes=payload.notes,
        )
        inv.set_supplier_snapshot(snapshot)
        inv.set_customer_snapshot(contact.to_snapshot_dict())
        return inv

    # User-supplied number: use it verbatim, but reject a duplicate clearly
    # (the unique index on (user_id, variable_symbol) enforces uniqueness).
    if payload.variable_symbol:
        invoice = _new_invoice(payload.variable_symbol)
        session.add(invoice)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Faktura s číslem {payload.variable_symbol} již existuje.",
            )
        session.refresh(invoice)
        return InvoiceRead.model_validate(invoice)

    # Otherwise assign the next sequential number, retrying if a concurrent
    # create claimed the same symbol.
    for _attempt in range(5):
        invoice = _new_invoice(get_next_variable_symbol(session, _user.id))
        session.add(invoice)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            continue
        session.refresh(invoice)
        return InvoiceRead.model_validate(invoice)

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Nepodařilo se přidělit variabilní symbol. Zkuste to prosím znovu.",
    )


@router.post("/{invoice_id}/credit-note", response_model=InvoiceRead,
             status_code=status.HTTP_201_CREATED)
def create_credit_note(
    invoice_id: int,
    payload: CreditNoteCreate,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> InvoiceRead:
    """Issue a dobropis (credit note) correcting an existing invoice.

    The dobropis inherits the corrected invoice's contact, currency, payment
    method and frozen customer snapshot (the document names the customer as
    they stood on the original), takes the next number from the dobropis
    series (YYYY9NNN), and stores a negative total. It is immutable after
    creation — a wrong dobropis is deleted (while unpaid) and reissued.
    """
    original = _get_invoice_or_404(session, invoice_id)
    if original.user_id != _user.id:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found.")
    if original.doc_type != "invoice":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Dobropis lze vystavit pouze k faktuře, ne k jinému dobropisu.",
        )

    supplier = session.exec(
        select(SupplierProfile).where(SupplierProfile.user_id == _user.id)
    ).first()
    _assert_vat_consistency(supplier, payload.items)
    snapshot = supplier.to_pdf_dict() if supplier else {}

    total = _compute_total(payload.items)
    items_json = json.dumps(
        [item.model_dump(mode="json") for item in payload.items],
        ensure_ascii=False,
    )

    # Next number from the dobropis series, retrying if a concurrent create
    # claimed the same symbol (same contract as create_invoice).
    for _attempt in range(5):
        credit_note = Invoice(
            user_id=_user.id,
            contact_id=original.contact_id,
            variable_symbol=get_next_credit_note_symbol(session, _user.id),
            amount=total,
            currency=original.currency,
            payment_method=original.payment_method,
            date_issued=payload.date_issued,
            date_due=payload.date_due,
            items=items_json,
            notes=payload.notes,
            doc_type="credit_note",
            related_invoice_id=original.id,
        )
        credit_note.set_supplier_snapshot(snapshot)
        # Name the customer as frozen on the corrected invoice; a legacy
        # original without a snapshot gets one frozen from the live contact.
        customer_snap = original.get_customer_snapshot()
        if not customer_snap:
            contact = session.get(Contact, original.contact_id)
            customer_snap = contact.to_snapshot_dict() if contact else {}
        credit_note.set_customer_snapshot(customer_snap)
        session.add(credit_note)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            continue
        session.refresh(credit_note)
        return InvoiceRead.model_validate(credit_note)

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Nepodařilo se přidělit číslo dobropisu. Zkuste to prosím znovu.",
    )


@router.get("", response_model=list[InvoiceSummary])
def list_invoices(
    year: Optional[int] = None,
    is_paid: Optional[bool] = None,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> list[InvoiceSummary]:
    """List invoices, newest first.

    Query parameters
    ----------------
    year    Filter by invoice year (matched against the Variable Symbol prefix).
    is_paid Filter by paid / unpaid status.
    """
    stmt = select(Invoice).where(Invoice.user_id == _user.id)
    if year is not None:
        stmt = stmt.where(Invoice.variable_symbol.like(f"{year}%"))
    if is_paid is not None:
        stmt = stmt.where(Invoice.is_paid == is_paid)
    stmt = stmt.order_by(Invoice.variable_symbol.desc())

    invoices = session.exec(stmt).all()

    # Resolve contact names in one pass. At this scale, N+1 is acceptable;
    # we avoid a JOIN to keep the query logic simple.
    result: list[InvoiceSummary] = []
    for inv in invoices:
        contact = session.get(Contact, inv.contact_id)
        result.append(
            InvoiceSummary(
                id=inv.id,
                variable_symbol=inv.variable_symbol,
                contact_name=contact.name if contact else "—",
                amount=inv.amount,
                currency=inv.currency,
                date_issued=inv.date_issued,
                date_due=inv.date_due,
                is_paid=inv.is_paid,
                date_paid=inv.date_paid,
                doc_type=inv.doc_type,
            )
        )
    return result


@router.patch("/{invoice_id}/paid", response_model=InvoiceRead)
def toggle_paid(
    invoice_id: int,
    payload: MarkPaidRequest = MarkPaidRequest(),
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> InvoiceRead:
    """Toggle the paid / unpaid state of an invoice.

    - Unpaid → Paid: ``date_paid`` is set to ``payload.date_paid`` or today.
    - Paid   → Unpaid: ``date_paid`` is cleared.
    """
    invoice = _get_invoice_or_404(session, invoice_id)
    if invoice.user_id != _user.id:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found.")

    if invoice.is_paid:
        # Toggle OFF
        invoice.is_paid = False
        invoice.date_paid = None
    else:
        # Toggle ON
        invoice.is_paid = True
        invoice.date_paid = payload.date_paid or date.today()

    session.add(invoice)
    session.commit()
    session.refresh(invoice)
    return InvoiceRead.model_validate(invoice)


@router.patch("/{invoice_id}", response_model=InvoiceRead)
def update_invoice(
    invoice_id: int,
    payload: InvoiceUpdate,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> InvoiceRead:
    """Update editable fields of an existing invoice.

    Credit notes (dobropisy) are immutable after creation — apart from the
    paid toggle — so a correcting document never drifts from what was issued;
    a wrong dobropis is deleted (while unpaid) and reissued.
    """
    invoice = _get_invoice_or_404(session, invoice_id)
    if invoice.user_id != _user.id:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found.")
    if invoice.doc_type == "credit_note":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Dobropis nelze upravit – smažte jej a vystavte znovu.",
        )

    if payload.contact_id is not None:
        new_contact = _get_contact_or_404(session, payload.contact_id, _user.id)
        invoice.contact_id = payload.contact_id
        # The document now names a different customer: re-freeze the snapshot.
        invoice.set_customer_snapshot(new_contact.to_snapshot_dict())
    if payload.date_issued is not None:
        invoice.date_issued = payload.date_issued
    if payload.date_due is not None:
        invoice.date_due = payload.date_due
    # Re-check against the resulting state: a partial update touching only one
    # date is validated here against the other (stored) value, which the
    # schema-level validator cannot see.
    if invoice.date_due < invoice.date_issued:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_due must not be earlier than date_issued.",
        )
    if payload.payment_method is not None:
        invoice.payment_method = payload.payment_method
    if payload.notes is not None:
        invoice.notes = payload.notes
    if payload.items is not None:
        supplier = session.exec(
            select(SupplierProfile).where(SupplierProfile.user_id == _user.id)
        ).first()
        _assert_vat_consistency(supplier, payload.items)
        invoice.set_items([item.model_dump(mode="json") for item in payload.items])
        invoice.amount = _compute_total(payload.items)

    session.add(invoice)
    session.commit()
    session.refresh(invoice)
    return InvoiceRead.model_validate(invoice)


@router.delete("/{invoice_id}", response_model=None)
def delete_invoice(
    invoice_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> Response:
    """Permanently remove an unpaid invoice or dobropis.

    Paid documents are settled and are refused (409) — toggle back to unpaid
    first. An invoice with dobropisy issued against it is also refused: the
    dobropisy must be deleted first, or the back-references would dangle.
    Deleting from the middle of a year's series leaves a gap in the číselná
    řada (numbering continues from the maximum), which is the user's
    deliberate choice to make.
    """
    invoice = _get_invoice_or_404(session, invoice_id)
    if invoice.user_id != _user.id:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found.")
    if invoice.is_paid:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Zaplacenou fakturu nelze smazat – nejprve ji označte jako nezaplacenou.",
        )
    credit_notes = session.exec(
        select(Invoice).where(Invoice.related_invoice_id == invoice.id)
    ).all()
    if credit_notes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Fakturu s vystaveným dobropisem nelze smazat – "
                   "nejprve smažte dobropis.",
        )
    session.delete(invoice)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{invoice_id}/pdf")
def download_pdf(
    invoice_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> Response:
    """Generate the invoice PDF on-the-fly and return it as a file download."""
    invoice = _get_invoice_or_404(session, invoice_id)
    if invoice.user_id != _user.id:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found.")
    # Customer data comes from the snapshot frozen at creation; only legacy
    # invoices (created before customer_snapshot existed) read the live contact.
    contact_data: Any = invoice.get_customer_snapshot()
    if not contact_data:
        contact_data = _get_contact_or_404(session, invoice.contact_id, _user.id)

    snapshot = invoice.get_supplier_snapshot()
    iban = snapshot.get("bank_iban", "")

    # Cash invoices carry no bank payment details, so no QR is generated.
    # Neither does a dobropis: the money flows supplier → customer, so a
    # payment QR pointing at the supplier's own account would be wrong.
    qr_base64 = ""
    if iban and invoice.payment_method != "cash" and invoice.doc_type != "credit_note":
        qr_base64 = generate_spayd_qr(
            iban=iban,
            amount=Decimal(str(invoice.amount)),
            vs=invoice.variable_symbol,
            currency=invoice.currency,
            message=f"Faktura {invoice.variable_symbol}",
        )

    # Dobropis PDFs reference the corrected invoice under the title.
    related_vs = ""
    if invoice.doc_type == "credit_note" and invoice.related_invoice_id:
        related = session.get(Invoice, invoice.related_invoice_id)
        if related is not None and related.user_id == _user.id:
            related_vs = related.variable_symbol

    # Brand colour is read LIVE from the current supplier profile (not frozen
    # into the snapshot) – colour carries no accounting information.
    supplier = session.exec(
        select(SupplierProfile).where(SupplierProfile.user_id == _user.id)
    ).first()
    brand_color = supplier.brand_color if supplier else DEFAULT_BRAND_COLOR

    pdf_bytes = render_invoice_to_pdf(
        invoice_data=invoice,
        contact_data=contact_data,
        supplier_data=snapshot,
        qr_code_base64=qr_base64,
        brand_color=brand_color,
        related_vs=related_vs,
    )

    supplier_slug = slugify_filename(snapshot.get("name", ""))
    if invoice.doc_type == "credit_note":
        filename = (
            f"{supplier_slug}-dobropis-{invoice.variable_symbol}.pdf"
            if supplier_slug
            else f"dobropis-{invoice.variable_symbol}.pdf"
        )
    else:
        filename = (
            f"{supplier_slug}-{invoice.variable_symbol}.pdf"
            if supplier_slug
            else f"faktura-{invoice.variable_symbol}.pdf"
        )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
