"""
routers/export.py – Annual statistics export (monthly breakdown).

Endpoints
---------
  GET /export/{year}?date_field=issued|paid
        Download a UTF-8 CSV mirroring the Statistiky page's monthly
        breakdown table for the requested calendar year.

The CSV is the *same table* shown on /statistiky: 12 month rows of
invoice sum / expense sum / difference, plus a year-total footer. The
``date_field`` toggle is honoured identically to the page:

  issued  – all invoices grouped by ``date_issued``
  paid    – paid invoices only, grouped by ``date_paid``

Expenses are always grouped by their own ``date``.

The file uses a UTF-8 BOM prefix (﻿) so Microsoft Excel opens it
correctly without requiring manual encoding selection.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Response
from sqlmodel import Session, select

from app.auth import get_current_user
from app.database import get_session
from app.models import Expense, Invoice, User

router = APIRouter(prefix="/export", tags=["export"])

_CSV_MEDIA_TYPE = "text/csv; charset=utf-8"

_MONTHS = [
    "Leden", "Únor", "Březen", "Duben", "Květen", "Červen",
    "Červenec", "Srpen", "Září", "Říjen", "Listopad", "Prosinec",
]


def _fmt_amount(value: Decimal) -> str:
    """Format a Decimal with two places and a Czech (comma) decimal separator.

    Czech-locale Excel parses comma-decimal numbers natively, matching the
    on-screen Statistiky table which also renders ``1234,56``.
    """
    return f"{value:.2f}".replace(".", ",")


@router.get("/{year}")
def export_year(
    year: int,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
    date_field: str = "issued",
) -> Response:
    """Export the Statistiky monthly breakdown for ``year`` as CSV.

    Mirrors ``ui_statistics`` in ``main.py``: same aggregation, same
    issued/paid semantics, so the file matches the table on screen.
    """

    if date_field not in ("issued", "paid"):
        date_field = "issued"
    paid_only = date_field == "paid"
    inv_col = Invoice.date_paid if paid_only else Invoice.date_issued

    # ── Fetch the year's invoices (matching the page's filter) ────────────────
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

    # ── Aggregate per month (Decimal arithmetic) ──────────────────────────────
    inv_by_month = [Decimal("0")] * 12
    for inv in invoices:
        d = inv.date_paid if paid_only else inv.date_issued
        inv_by_month[d.month - 1] += Decimal(str(inv.amount))

    exp_by_month = [Decimal("0")] * 12
    for exp in expenses:
        exp_by_month[exp.date.month - 1] += Decimal(str(exp.amount))

    # ── Build CSV in memory ───────────────────────────────────────────────────
    buffer = io.StringIO()
    buffer.write("﻿")  # UTF-8 BOM – Excel compatibility
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL)

    basis = "datum zaplacení" if paid_only else "datum vystavení"
    writer.writerow([f"PŘEHLED PO MĚSÍCÍCH ZA ROK {year}"])
    writer.writerow([f"Faktury podle: {basis}"])
    writer.writerow([f"Exportováno: {date.today().strftime('%d.%m.%Y')}"])
    writer.writerow([])

    writer.writerow(["Měsíc", "Faktury (Kč)", "Náklady (Kč)", "Rozdíl (Kč)"])
    for i in range(12):
        writer.writerow([
            _MONTHS[i],
            _fmt_amount(inv_by_month[i]),
            _fmt_amount(exp_by_month[i]),
            _fmt_amount(inv_by_month[i] - exp_by_month[i]),
        ])

    inv_total = sum(inv_by_month, Decimal("0"))
    exp_total = sum(exp_by_month, Decimal("0"))
    writer.writerow([
        f"Celkem {year}",
        _fmt_amount(inv_total),
        _fmt_amount(exp_total),
        _fmt_amount(inv_total - exp_total),
    ])

    # ── Return as file download ────────────────────────────────────────────────
    csv_bytes = buffer.getvalue().encode("utf-8")
    filename = f"statistiky-{year}-{date_field}.csv"
    return Response(
        content=csv_bytes,
        media_type=_CSV_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
