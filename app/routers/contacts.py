"""routers/contacts.py – Client / counterparty management."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.auth import get_current_user
from app.database import get_session
from app.logic import validate_ico, validate_party_bank_and_ico
from app.models import Contact, User
from app.schemas import ContactCreate, ContactRead

router = APIRouter(prefix="/contacts", tags=["contacts"])

# Public, free, no-key REST API of the Czech business register (ARES, MF ČR).
_ARES_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"


@router.get("/ares/{ico}")
def lookup_ares(ico: str, _user: User = Depends(get_current_user)) -> dict:
    """Look up a Czech subject by IČO in ARES and return prefill fields.

    Returns ``{name, address, ic, dic}`` for the contact form. The DIČ is only
    present for VAT-registered subjects (often absent for our non-VAT clients).
    """
    err = validate_ico(ico)
    if err:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)

    req = urllib.request.Request(
        _ARES_URL.format(ico=ico),
        headers={"Accept": "application/json", "User-Agent": "Fakturace/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HTTPException(status_code=404, detail="Subjekt s tímto IČO nebyl v ARES nalezen.")
        raise HTTPException(status_code=502, detail="ARES je dočasně nedostupný.")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise HTTPException(status_code=502, detail="ARES je dočasně nedostupný.")

    if data.get("kod") == "NENALEZENO":
        raise HTTPException(status_code=404, detail="Subjekt s tímto IČO nebyl v ARES nalezen.")

    # Prefer the nicely line-broken delivery address; fall back to the flat one.
    addr = data.get("adresaDorucovaci") or {}
    lines = [addr.get("radekAdresy1"), addr.get("radekAdresy2"), addr.get("radekAdresy3")]
    address = "\n".join(line for line in lines if line)
    if not address:
        address = (data.get("sidlo") or {}).get("textovaAdresa", "")

    return {
        "name": data.get("obchodniJmeno", ""),
        "address": address,
        "ic": data.get("ico", ico),
        "dic": data.get("dic", "") or "",
    }


def _validate_contact_payload(payload: ContactCreate) -> str:
    """Validate IČO + bank account; return the IBAN to store (raises 422)."""
    error, iban = validate_party_bank_and_ico(
        ic=payload.ic or "",
        prefix=payload.bank_prefix or "",
        number=payload.bank_number or "",
        code=payload.bank_code or "",
        iban=payload.bank_iban or "",
    )
    if error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error)
    return iban


@router.post("", response_model=ContactRead, status_code=status.HTTP_201_CREATED)
def create_contact(
    payload: ContactCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ContactRead:
    iban = _validate_contact_payload(payload)
    data = payload.model_dump()
    data["bank_iban"] = iban or None
    contact = Contact(**data, user_id=current_user.id)
    session.add(contact)
    session.commit()
    session.refresh(contact)
    return contact


@router.get("", response_model=list[ContactRead])
def list_contacts(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ContactRead]:
    return session.exec(
        select(Contact)
        .where(Contact.user_id == current_user.id)
        .order_by(Contact.name)
    ).all()


@router.get("/{contact_id}", response_model=ContactRead)
def get_contact(
    contact_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ContactRead:
    contact = session.get(Contact, contact_id)
    if contact is None or contact.user_id != current_user.id:
        raise HTTPException(status_code=404, detail=f"Contact {contact_id} not found.")
    return contact


@router.patch("/{contact_id}", response_model=ContactRead)
def update_contact(
    contact_id: int,
    payload: ContactCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ContactRead:
    contact = session.get(Contact, contact_id)
    if contact is None or contact.user_id != current_user.id:
        raise HTTPException(status_code=404, detail=f"Contact {contact_id} not found.")
    iban = _validate_contact_payload(payload)
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(contact, field, value)
    # If any bank component was part of this update, store the validated IBAN.
    if {"bank_prefix", "bank_number", "bank_code", "bank_iban"} & updates.keys():
        contact.bank_iban = iban or None
    session.add(contact)
    session.commit()
    session.refresh(contact)
    return contact
