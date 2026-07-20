"""
schemas.py – Pydantic request / response models (API contract layer).

Deliberately kept separate from SQLModel table definitions (models.py) so
the API contract and the database schema can evolve independently.

Monetary fields use decimal.Decimal throughout to avoid IEEE-754 rounding
errors on input. Responses use plain float (sufficient for JSON display).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


# ── Auth ──────────────────────────────────────────────────────────────────────

class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: Optional[str] = None


# ── Contact ───────────────────────────────────────────────────────────────────

class ContactCreate(BaseModel):
    name: str
    address: str
    ic: str
    dic: Optional[str] = None
    email: Optional[str] = None
    # Bank account in Czech local format – all optional
    bank_prefix: Optional[str] = None   # předčíslí
    bank_number: Optional[str] = None   # číslo účtu
    bank_code: Optional[str] = None     # kód banky
    bank_iban: Optional[str] = None     # IBAN (auto-computed, user-editable)
    # Optional per-client payment term in days (splatnost). None → app default.
    payment_due_days: Optional[int] = None

    @field_validator("payment_due_days")
    @classmethod
    def due_days_positive(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 1:
            raise ValueError("Splatnost musí být kladný počet dní.")
        return v


class ContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    address: str
    ic: str
    dic: Optional[str]
    email: Optional[str]
    bank_prefix: Optional[str]
    bank_number: Optional[str]
    bank_code: Optional[str]
    bank_iban: Optional[str]
    payment_due_days: Optional[int]


# ── SupplierProfile ───────────────────────────────────────────────────────────

class SupplierProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    address: str
    ic: str
    dic: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    bank_prefix: Optional[str]
    bank_number: str
    bank_code: str
    bank_iban: Optional[str]
    trade_register_date: Optional[date]


class SupplierProfileUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    ic: Optional[str] = None
    dic: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    bank_prefix: Optional[str] = None
    bank_number: Optional[str] = None
    bank_code: Optional[str] = None
    bank_iban: Optional[str] = None
    trade_register_date: Optional[date] = None


# ── Invoice ───────────────────────────────────────────────────────────────────
class LineItem(BaseModel):
    """A single line item on an invoice."""

    description: str
    quantity: Decimal = Decimal("1")
    unit: str = "ks"           # e.g. "hod", "ks", "paušál", "den"
    unit_price: Decimal
    vat_rate: Decimal = Decimal("0")   # fractional: 0.21 → 21 % VAT

    @field_validator("quantity", "unit_price", mode="before")
    @classmethod
    def must_be_positive(cls, v: Any) -> Any:
        d = Decimal(str(v))
        if d <= 0:
            raise ValueError("Must be greater than zero.")
        return d

    @field_validator("vat_rate", mode="before")
    @classmethod
    def vat_rate_range(cls, v: Any) -> Any:
        d = Decimal(str(v))
        if not (Decimal("0") <= d <= Decimal("1")):
            raise ValueError("vat_rate must be a fraction between 0 and 1 (e.g. 0.21).")
        return d


class CreditLineItem(LineItem):
    """A line item on a dobropis (credit note).

    Quantity stays positive (what is being returned); the unit price must be
    negative, so every line — and therefore the document total — is negative.
    Overrides the parent's sign validator (same method name replaces it in
    pydantic v2).
    """

    @field_validator("quantity", "unit_price", mode="before")
    @classmethod
    def must_be_positive(cls, v: Any, info: Any) -> Any:
        d = Decimal(str(v))
        if info.field_name == "unit_price":
            if d >= 0:
                raise ValueError(
                    "Dobropis musí mít zápornou jednotkovou cenu."
                )
        elif d <= 0:
            raise ValueError("Must be greater than zero.")
        return d


_PAYMENT_METHODS = {"transfer", "cash"}


class InvoiceCreate(BaseModel):
    contact_id: int
    date_issued: date
    date_due: date
    currency: str = "CZK"
    payment_method: str = "transfer"
    # Optional override of the invoice number. When omitted/empty, the server
    # assigns the next sequential value (YYYYNNNN).
    variable_symbol: Optional[str] = None
    items: list[LineItem]
    notes: Optional[str] = None

    @field_validator("payment_method")
    @classmethod
    def known_payment_method(cls, v: str) -> str:
        if v not in _PAYMENT_METHODS:
            raise ValueError("payment_method must be 'transfer' or 'cash'.")
        return v

    @field_validator("currency")
    @classmethod
    def currency_locked_to_czk(cls, v: str) -> str:
        # All aggregates (dashboard, Statistiky, CSV) naively sum across rows
        # and the SPAYD QR is a CZK-domestic standard, so new invoices are
        # locked to CZK server-side — the hidden form field is not a guard.
        if v.strip().upper() != "CZK":
            raise ValueError("Nové faktury lze vystavit pouze v CZK.")
        return "CZK"

    @field_validator("variable_symbol")
    @classmethod
    def vs_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not v.isdigit() or not (1 <= len(v) <= 10):
            raise ValueError("Číslo faktury musí být 1–10 číslic.")
        return v
    # NOTE: there is deliberately no client-settable stamp path here. The stamp
    # rendered on the PDF comes solely from the supplier profile, whose image is
    # uploaded through an authenticated handler that confines it to the user's
    # own uploads directory. Accepting an arbitrary filesystem path would allow
    # any authenticated user to read server-side files (e.g. .env, the DB) via
    # the generated PDF.

    @field_validator("items")
    @classmethod
    def items_not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("An invoice must have at least one line item.")
        return v

    @field_validator("date_due")
    @classmethod
    def due_after_issued(cls, v: date, info: Any) -> date:
        issued = info.data.get("date_issued")
        if issued and v < issued:
            raise ValueError("date_due must not be earlier than date_issued.")
        return v


class CreditNoteCreate(BaseModel):
    """Payload for issuing a dobropis against an existing invoice.

    Deliberately narrow: the contact, currency and payment method are inherited
    from the corrected invoice, and the number is always assigned from the
    dobropis series (YYYY9NNN) — there is no override.
    """

    date_issued: date
    date_due: date
    items: list[CreditLineItem]
    notes: Optional[str] = None

    @field_validator("items")
    @classmethod
    def items_not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("Dobropis musí mít alespoň jednu položku.")
        return v

    @field_validator("date_due")
    @classmethod
    def due_after_issued(cls, v: date, info: Any) -> date:
        issued = info.data.get("date_issued")
        if issued and v < issued:
            raise ValueError("date_due must not be earlier than date_issued.")
        return v


class InvoiceRead(BaseModel):
    """Full invoice representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    contact_id: int
    variable_symbol: str
    amount: float
    currency: str
    payment_method: str
    date_issued: date
    date_due: date
    date_paid: Optional[date]
    is_paid: bool
    notes: Optional[str]
    doc_type: str = "invoice"
    related_invoice_id: Optional[int] = None
    # Items are returned as a parsed list, never as a raw JSON string.
    items: list[dict[str, Any]] = []

    @model_validator(mode="before")
    @classmethod
    def parse_json_items(cls, data: Any) -> Any:
        """Parse the JSON-encoded items string from the DB model into a list."""
        # SQLModel instances expose attributes directly.
        raw: Any = (
            getattr(data, "items", "[]")
            if not isinstance(data, dict)
            else data.get("items", "[]")
        )
        if isinstance(raw, str):
            parsed = json.loads(raw)
            # Build a plain dict so Pydantic can hydrate remaining fields normally.
            if not isinstance(data, dict):
                data = {
                    field: getattr(data, field, None)
                    for field in [
                        "id", "contact_id", "variable_symbol", "amount",
                        "currency", "payment_method", "date_issued", "date_due",
                        "date_paid", "is_paid", "notes",
                        "doc_type", "related_invoice_id",
                    ]
                }
            data["items"] = parsed
        return data


class InvoiceUpdate(BaseModel):
    """Partial update for an existing invoice – all fields optional.

    There is deliberately no ``currency`` field: the stored value is immutable
    after creation, which both keeps new invoices on the CZK lock and lets
    legacy non-CZK invoices keep their original currency when edited (the form
    still submits one; it is ignored here).
    """

    contact_id: Optional[int] = None
    date_issued: Optional[date] = None
    date_due: Optional[date] = None
    payment_method: Optional[str] = None
    notes: Optional[str] = None
    items: Optional[list[LineItem]] = None

    @field_validator("payment_method")
    @classmethod
    def known_payment_method(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _PAYMENT_METHODS:
            raise ValueError("payment_method must be 'transfer' or 'cash'.")
        return v

    @model_validator(mode="after")
    def due_after_issued(self) -> "InvoiceUpdate":
        # Only meaningful when both dates are supplied in the same patch; a
        # partial update touching just one date can't be cross-checked here
        # (the other value lives on the stored row), so we skip that case.
        if (
            self.date_issued is not None
            and self.date_due is not None
            and self.date_due < self.date_issued
        ):
            raise ValueError("date_due must not be earlier than date_issued.")
        return self


class InvoiceSummary(BaseModel):
    """Lightweight invoice row for list / dashboard views."""

    id: int
    variable_symbol: str
    contact_name: str    # resolved in the route handler – not a DB column
    amount: float
    currency: str
    date_issued: date
    date_due: date
    is_paid: bool
    date_paid: Optional[date]
    doc_type: str = "invoice"


class MarkPaidRequest(BaseModel):
    """Optional body when toggling an invoice to Paid status.

    If ``date_paid`` is omitted the server defaults to today's date.
    """
    date_paid: Optional[date] = None


# ── Expense ───────────────────────────────────────────────────────────────────

class ExpenseCreate(BaseModel):
    date: date
    category: str          # e.g. "Software", "Travel", "Office", "Marketing"
    amount: Decimal
    currency: str = "CZK"
    description: str
    # NOTE: no client-settable attachment_path. The scan path is assigned
    # server-side only (the authenticated upload handlers confine it to the
    # user's own uploads dir). Accepting an arbitrary filesystem path here would
    # let a caller store a path pointing anywhere on the server, contradicting
    # the per-user upload confinement enforced everywhere else.

    @field_validator("amount", mode="before")
    @classmethod
    def amount_positive(cls, v: Any) -> Any:
        d = Decimal(str(v))
        if d <= 0:
            raise ValueError("Amount must be greater than zero.")
        return d

    @field_validator("currency")
    @classmethod
    def currency_locked_to_czk(cls, v: str) -> str:
        # Same single-currency lock as InvoiceCreate: every total naively sums
        # amounts across rows, so new expenses must be CZK.
        if v.strip().upper() != "CZK":
            raise ValueError("Nové náklady lze evidovat pouze v CZK.")
        return "CZK"


class ExpenseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    date: date
    category: str
    amount: float
    currency: str
    description: str
    attachment_path: Optional[str]
