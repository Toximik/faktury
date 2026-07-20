"""
models.py – SQLModel ORM definitions.

Tables
------
- User            : application login credential store
- SupplierProfile : per-user business identity (replaces supplier.json)
- Contact         : client / counterparty master data
- Invoice         : issued invoice header + JSON line-items
- Expense         : logged business expense
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from app.config import (
    DEFAULT_BRAND_COLOR,
    HEALTH_ASSESSMENT_RATE,
    HEALTH_INSURANCE_RATE,
    INCOME_TAX_RATE,
    SOCIAL_ASSESSMENT_RATE,
    SOCIAL_INSURANCE_RATE,
)


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class User(SQLModel, table=True):
    """Single-user authentication record."""

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True, max_length=64)
    hashed_password: str = Field(max_length=256)
    is_admin: bool = Field(default=False)
    is_active: bool = Field(default=True)

    # Incremented whenever outstanding sessions must be invalidated (password
    # change, account disable). Embedded as the 'ver' claim in issued JWTs and
    # checked on every authenticated request, so a stale token is rejected even
    # before its 'exp' lapses.
    token_version: int = Field(default=1)


# ---------------------------------------------------------------------------
# SupplierProfile  (one per user – the freelancer's own business details)
# ---------------------------------------------------------------------------

class SupplierProfile(SQLModel, table=True):
    """Freelancer / company identity used on every generated PDF.

    Bank account is stored in Czech local format components:
      bank_prefix  – předčíslí, optional (e.g. "19")
      bank_number  – číslo účtu, required  (e.g. "2000145399")
      bank_code    – kód banky, required   (e.g. "0800")
      bank_iban    – IBAN, auto-computed but user-editable (e.g. "CZ6508000000192000145399")

    trade_register_date – date of entry into the živnostenský rejstřík.
      When set, the PDF footer renders:
        "Fyzická osoba zapsaná v živnostenském rejstříku od DD. M. YYYY."
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True, index=True)

    name: str = Field(default="", max_length=255)
    address: str = Field(default="", max_length=500)
    ic: str = Field(default="", max_length=20)
    dic: Optional[str] = Field(default=None, max_length=20)
    email: Optional[str] = Field(default=None, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=50)

    # Bank account – Czech local format
    bank_prefix: Optional[str] = Field(default=None, max_length=6)   # předčíslí (optional)
    bank_number: str = Field(default="", max_length=10)               # číslo účtu
    bank_code: str = Field(default="", max_length=4)                  # kód banky
    bank_iban: Optional[str] = Field(default=None, max_length=34)     # IBAN (auto-computed)

    trade_register_date: Optional[date] = Field(default=None)

    # OSVČ activity type – drives the social-insurance calculation on the Daně
    # page: "main" (hlavní činnost – pojistné se platí vždy) or "secondary"
    # (vedlejší činnost – účast vzniká až od rozhodné částky).
    osvc_activity_type: str = Field(default="main", max_length=10)

    # Accent colour for the generated PDF (hex). Read live at render time.
    brand_color: str = Field(default=DEFAULT_BRAND_COLOR, max_length=7)

    # Branding images (filesystem paths relative to project root)
    logo_path: Optional[str] = Field(default=None, max_length=500)
    stamp_path: Optional[str] = Field(default=None, max_length=500)

    def bank_account_display(self) -> str:
        """Return formatted Czech account string, e.g. '19-2000145399/0800'."""
        if self.bank_prefix:
            return f"{self.bank_prefix}-{self.bank_number}/{self.bank_code}"
        return f"{self.bank_number}/{self.bank_code}"

    def to_pdf_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for passing to render_invoice_to_pdf."""
        return {
            "name":                self.name,
            "address":             self.address,
            "ic":                  self.ic,
            "dic":                 self.dic or "",
            "email":               self.email or "",
            "phone":               self.phone or "",
            "bank_account":        self.bank_account_display(),
            "bank_iban":           self.bank_iban or "",
            "trade_register_date": (
                self.trade_register_date.strftime("%-d. %-m. %Y")
                if self.trade_register_date else None
            ),
            "logo_path":           self.logo_path or "",
            "stamp_path":          self.stamp_path or "",
        }


# ---------------------------------------------------------------------------
# Contact  (client / counterparty)
# ---------------------------------------------------------------------------

class Contact(SQLModel, table=True):
    """Business contact / client record."""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)

    name: str = Field(max_length=255)
    address: str = Field(max_length=500)
    ic: str = Field(max_length=20)
    dic: Optional[str] = Field(default=None, max_length=20)
    email: Optional[str] = Field(default=None, max_length=255)

    # Bank account – Czech local format (optional for contacts)
    bank_prefix: Optional[str] = Field(default=None, max_length=6)
    bank_number: Optional[str] = Field(default=None, max_length=10)
    bank_code: Optional[str] = Field(default=None, max_length=4)
    bank_iban: Optional[str] = Field(default=None, max_length=34)

    # Default payment term (splatnost) in days for this client. When None, the
    # app-wide DEFAULT_PAYMENT_DUE_DAYS is used for the invoice due date.
    payment_due_days: Optional[int] = Field(default=None)

    def bank_account_display(self) -> str:
        """Return formatted Czech account string or empty string."""
        if not self.bank_number or not self.bank_code:
            return ""
        if self.bank_prefix:
            return f"{self.bank_prefix}-{self.bank_number}/{self.bank_code}"
        return f"{self.bank_number}/{self.bank_code}"

    def to_snapshot_dict(self) -> dict[str, Any]:
        """Freeze the fields the invoice PDF and detail page render.

        Stored into Invoice.customer_snapshot at creation time so later edits
        to the contact never change what an already-issued invoice shows.
        """
        return {
            "name":    self.name,
            "address": self.address,
            "ic":      self.ic,
            "dic":     self.dic or "",
            "email":   self.email or "",
        }


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------

class Invoice(SQLModel, table=True):
    """Invoice header record."""

    # Per-user uniqueness of the Variable Symbol — guards the read-then-increment
    # series generators against concurrent creates. Lives in the model metadata
    # (not a startup side-effect) so create_all and Alembic autogenerate both see
    # it; the name matches the index legacy SQLite databases already carry.
    __table_args__ = (
        Index("ix_invoice_user_vs", "user_id", "variable_symbol", unique=True),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    contact_id: int = Field(foreign_key="contact.id", index=True)

    # CZ/SK payment reference – unique per user+year
    variable_symbol: str = Field(max_length=20, index=True)

    amount: float
    currency: str = Field(default="CZK", max_length=3)

    # "transfer" (Převodem) or "cash" (Hotově). Cash invoices hide the bank
    # payment details + QR on the PDF.
    payment_method: str = Field(default="transfer", max_length=10)

    date_issued: date
    date_due: date
    date_paid: Optional[date] = Field(default=None)
    is_paid: bool = Field(default=False)

    # "invoice" (faktura) or "credit_note" (dobropis). A dobropis carries
    # negative amounts, is numbered from its own series (YYYY9NNN) and is
    # immutable after creation; related_invoice_id points at the corrected
    # invoice (always set for credit notes, always NULL for invoices).
    # Deliberately not a DB-level FK: SQLite runs without PRAGMA foreign_keys,
    # so the delete_invoice guard is the real protection — and a plain column
    # keeps ALTER TABLE simple for the hand-rolled migrations.
    doc_type: str = Field(default="invoice", max_length=12)
    related_invoice_id: Optional[int] = Field(default=None)

    # JSON line-items
    items: str = Field(default="[]", sa_column_kwargs={"name": "items"})

    notes: Optional[str] = Field(default=None, max_length=1000)
    # NOTE: there is deliberately no per-invoice image path field. The stamp on
    # the PDF comes solely from the supplier profile snapshot — a client-
    # settable filesystem path here would be an arbitrary server-file read via
    # PDF generation (see the schemas.py InvoiceCreate note).

    # Snapshot of the supplier profile at the time of creation (frozen forever)
    supplier_snapshot: str = Field(default="{}", sa_column_kwargs={"name": "supplier_snapshot"})

    # Snapshot of the customer (Contact) at the time of creation, same idea as
    # supplier_snapshot: the issued document must not change when the contact
    # is later edited. "{}" on legacy rows → renderers fall back to the live
    # contact. Refreshed only when an update re-assigns the invoice's contact.
    customer_snapshot: str = Field(default="{}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_items(self) -> list[dict[str, Any]]:
        return json.loads(self.items)

    def set_items(self, items: list[dict[str, Any]]) -> None:
        self.items = json.dumps(items, ensure_ascii=False)

    def get_supplier_snapshot(self) -> dict[str, Any]:
        return json.loads(self.supplier_snapshot)

    def set_supplier_snapshot(self, data: dict[str, Any]) -> None:
        self.supplier_snapshot = json.dumps(data, ensure_ascii=False, default=str)

    def get_customer_snapshot(self) -> dict[str, Any]:
        return json.loads(self.customer_snapshot)

    def set_customer_snapshot(self, data: dict[str, Any]) -> None:
        self.customer_snapshot = json.dumps(data, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Expense
# ---------------------------------------------------------------------------

class Expense(SQLModel, table=True):
    """A single logged business expense."""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)

    date: date
    category: str = Field(max_length=100)
    amount: float
    currency: str = Field(default="CZK", max_length=3)
    description: str = Field(max_length=500)
    attachment_path: Optional[str] = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# TaxSetting  (admin-managed, system-wide, one row per calendar year)
# ---------------------------------------------------------------------------

class TaxSetting(SQLModel, table=True):
    """System-wide tax parameters that change each calendar year.

    Set by an admin under user administration and applied to **all** users
    (not per-user). Currently holds the "rozhodná částka" for participation in
    důchodové (social) pojištění of an OSVČ performing a *secondary* activity
    (vedlejší činnost): when the yearly tax base reaches this amount, the
    obligation to pay social insurance arises. The value differs every year, so
    it lives in the DB rather than in config.py.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    year: int = Field(unique=True, index=True)

    # Rozhodná částka pro účast na důchodovém pojištění – vedlejší činnost (CZK).
    social_threshold_secondary: float = Field(default=0.0)

    # Rates that may change by government decision. Stored as fractions (e.g.
    # 0.15). Defaults come from config so a fresh row / legacy table keeps the
    # current values. Editing a year's row re-computes only that year.
    income_tax_rate: float = Field(default=float(INCOME_TAX_RATE))               # daň z příjmů
    social_assessment_rate: float = Field(default=float(SOCIAL_ASSESSMENT_RATE)) # soc. vyměřovací základ (% z daň. základu)
    social_insurance_rate: float = Field(default=float(SOCIAL_INSURANCE_RATE))   # sazba soc. pojistného
    health_assessment_rate: float = Field(default=float(HEALTH_ASSESSMENT_RATE)) # zdrav. vyměřovací základ (% z daň. základu)
    health_insurance_rate: float = Field(default=float(HEALTH_INSURANCE_RATE))   # sazba zdrav. pojistného
