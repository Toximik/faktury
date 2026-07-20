"""Tests for the Daňové přiznání helper: compute_tax_summary logic, the admin
per-year tax-parameter upsert, and the /dane page aggregation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlmodel import Session

from app.config import (
    HEALTH_ASSESSMENT_RATE,
    HEALTH_INSURANCE_RATE,
    INCOME_TAX_RATE,
    SOCIAL_ASSESSMENT_RATE,
    SOCIAL_INSURANCE_RATE,
)
from app.logic import compute_tax_summary, format_amount, format_money
from app.models import Contact, Expense, Invoice, SupplierProfile, TaxSetting
from tests.conftest import ORIGIN, make_user


# ── Money formatting (Czech thousands grouping) ──────────────────────────────

NBSP = " "

def test_format_amount_groups_thousands():
    assert format_amount(1500) == f"1{NBSP}500,00"
    assert format_amount(1234567.5) == f"1{NBSP}234{NBSP}567,50"
    assert format_amount(999) == "999,00"
    assert format_amount(-2500) == f"-2{NBSP}500,00"


def test_format_money_adds_symbol():
    assert format_money(1500) == f"1{NBSP}500,00{NBSP}Kč"
    assert format_money(1500, "EUR") == f"1{NBSP}500,00{NBSP}€"


# ── Pure logic ───────────────────────────────────────────────────────────────

def _summary(**kw):
    base = dict(
        income=0,
        real_expenses=0,
        expense_mode="real",
        flat_rate_percent=60,
        flat_rate_cap=1_200_000,
        social_threshold=None,
        tax_rate=INCOME_TAX_RATE,
        social_assessment_rate=SOCIAL_ASSESSMENT_RATE,
        social_insurance_rate=SOCIAL_INSURANCE_RATE,
        health_assessment_rate=HEALTH_ASSESSMENT_RATE,
        health_insurance_rate=HEALTH_INSURANCE_RATE,
    )
    base.update(kw)
    return compute_tax_summary(**base)


def test_real_expenses_tax_base():
    s = _summary(income=500_000, real_expenses=120_000, expense_mode="real")
    assert s["expenses"] == Decimal("120000")
    assert s["tax_base"] == Decimal("380000")


def test_flat_rate_applies_percent():
    s = _summary(income=500_000, expense_mode="flat", flat_rate_percent=60,
                 flat_rate_cap=1_200_000)
    assert s["expenses"] == Decimal("300000")           # 60 % of 500 000
    assert s["tax_base"] == Decimal("200000")


def test_flat_rate_is_capped():
    # 60 % of 3 000 000 = 1 800 000, but capped at 1 200 000.
    s = _summary(income=3_000_000, expense_mode="flat", flat_rate_percent=60,
                 flat_rate_cap=1_200_000)
    assert s["expenses"] == Decimal("1200000")
    assert s["tax_base"] == Decimal("1800000")


def test_tax_base_never_negative():
    s = _summary(income=100_000, real_expenses=250_000, expense_mode="real")
    assert s["tax_base"] == Decimal("0")
    assert s["tax"] == Decimal("0")


def test_tax_rounds_base_down_to_hundreds():
    # base 380 199 -> rounded 380 100 -> 15 % = 57 015
    s = _summary(income=500_199, real_expenses=120_000, expense_mode="real")
    assert s["tax_base_rounded"] == Decimal("380100")
    assert s["tax"] == Decimal("57015")


def test_no_taxpayer_credit_field():
    # Sleva na poplatníka is not modelled for vedlejší činnost.
    s = _summary(income=500_000, real_expenses=120_000, expense_mode="real")
    assert "tax_after_credit" not in s
    assert "taxpayer_credit" not in s


def test_social_insurance_estimate():
    # base 200 000 -> vyměřovací základ 110 000 (55 %) -> 29.2 % = 32 120.
    s = _summary(income=300_000, real_expenses=100_000)
    assert s["tax_base"] == Decimal("200000")
    assert s["social_assessment_base"] == Decimal("110000")
    assert s["social_insurance"] == Decimal("32120")


def test_health_insurance_estimate():
    # base 200 000 -> vyměřovací základ 100 000 (50 %) -> 13.5 % = 13 500.
    s = _summary(income=300_000, real_expenses=100_000)
    assert s["health_assessment_base"] == Decimal("100000")
    assert s["health_insurance"] == Decimal("13500")


def test_health_insurance_owed_for_secondary_below_threshold():
    # Even a small secondary profit (no social participation) still owes health.
    s = _summary(income=120_000, real_expenses=100_000, social_threshold=105_520,
                 activity_type="secondary")
    assert s["social_participation"] is False       # social: exempt
    assert s["health_insurance"] == Decimal("1350")  # health: 20 000 × 50 % × 13.5 %


def test_social_insurance_rounds_up():
    # base 100 003 -> 55 % = 55 001.65 -> 55 002 -> 29.2 % = 16 060.584 -> 16 061.
    s = _summary(income=100_003, real_expenses=0)
    assert s["social_assessment_base"] == Decimal("55002")
    assert s["social_insurance"] == Decimal("16061")


def test_social_participation_threshold():
    above = _summary(income=300_000, real_expenses=100_000, social_threshold=105_520)
    assert above["tax_base"] == Decimal("200000")
    assert above["social_participation"] is True

    below = _summary(income=200_000, real_expenses=150_000, social_threshold=105_520)
    assert below["tax_base"] == Decimal("50000")
    assert below["social_participation"] is False


def test_social_participation_none_when_unset():
    s = _summary(income=300_000, real_expenses=100_000, social_threshold=None)
    assert s["social_participation"] is None
    assert s["income_limit"] is None
    assert s["income_headroom"] is None


def test_main_activity_always_participates():
    # Main activity: mandatory regardless of profit, threshold ignored, no limit.
    s = _summary(income=120_000, real_expenses=100_000, social_threshold=105_520,
                 activity_type="main")
    assert s["tax_base"] == Decimal("20000")      # well below threshold
    assert s["social_participation"] is True
    assert s["income_limit"] is None
    assert s["income_headroom"] is None
    # Estimate still computed: 20 000 × 55 % = 11 000 × 29.2 % = 3 212.
    assert s["social_assessment_base"] == Decimal("11000")
    assert s["social_insurance"] == Decimal("3212")


def test_main_activity_without_threshold():
    # No rozhodná částka set is fine for main activity.
    s = _summary(income=300_000, real_expenses=100_000, social_threshold=None,
                 activity_type="main")
    assert s["social_participation"] is True


def test_income_limit_real_mode():
    # real: limit = threshold + expenses; headroom = limit − income.
    s = _summary(income=150_000, real_expenses=100_000, social_threshold=105_520)
    assert s["income_limit"] == Decimal("205520")
    assert s["income_headroom"] == Decimal("55520")


def test_income_limit_flat_mode():
    # flat 60 %: limit = threshold / 0.4 = 2.5 × threshold.
    s = _summary(income=100_000, expense_mode="flat", flat_rate_percent=60,
                 social_threshold=100_000)
    assert s["income_limit"] == Decimal("250000")
    assert s["income_headroom"] == Decimal("150000")


def test_income_headroom_zero_when_over():
    s = _summary(income=300_000, real_expenses=100_000, social_threshold=105_520)
    assert s["social_participation"] is True
    assert s["income_headroom"] == Decimal("0")


# ── Admin per-year tax settings ──────────────────────────────────────────────

def _login(client, username, password="password-1234"):
    client.cookies.clear()
    client.post("/login", data={"username": username, "password": password})


def _tax_form(**overrides):
    data = {
        "year": 2025,
        "social_threshold_secondary": 105520,
        "income_tax_rate_pct": 15,
        "social_assessment_rate_pct": 55,
        "social_insurance_rate_pct": 29.2,
        "health_assessment_rate_pct": 50,
        "health_insurance_rate_pct": 13.5,
    }
    data.update(overrides)
    return data


def test_admin_can_set_tax_setting(client, db_engine):
    make_user(db_engine, "admin", is_admin=True)
    _login(client, "admin")
    r = client.post("/ui/admin/danova-nastaveni", data=_tax_form(),
                    headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 200
    with Session(db_engine) as s:
        from sqlmodel import select
        ts = s.exec(select(TaxSetting).where(TaxSetting.year == 2025)).first()
        assert ts is not None
        assert ts.social_threshold_secondary == 105520
        # Percentages are stored as fractions.
        assert ts.income_tax_rate == 0.15
        assert ts.social_assessment_rate == 0.55
        assert abs(ts.social_insurance_rate - 0.292) < 1e-9
        assert ts.health_assessment_rate == 0.50
        assert abs(ts.health_insurance_rate - 0.135) < 1e-9


def test_admin_tax_setting_upsert(client, db_engine):
    make_user(db_engine, "admin", is_admin=True)
    _login(client, "admin")
    client.post("/ui/admin/danova-nastaveni",
                data=_tax_form(social_threshold_secondary=100000), headers=ORIGIN)
    client.post("/ui/admin/danova-nastaveni",
                data=_tax_form(social_threshold_secondary=105520,
                               social_insurance_rate_pct=30), headers=ORIGIN)
    with Session(db_engine) as s:
        from sqlmodel import select
        rows = s.exec(select(TaxSetting).where(TaxSetting.year == 2025)).all()
        assert len(rows) == 1                     # updated, not duplicated
        assert rows[0].social_threshold_secondary == 105520
        assert rows[0].social_insurance_rate == 0.30


def test_admin_tax_rate_out_of_range_rejected(client, db_engine):
    make_user(db_engine, "admin", is_admin=True)
    _login(client, "admin")
    r = client.post("/ui/admin/danova-nastaveni",
                    data=_tax_form(social_insurance_rate_pct=150),
                    headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 422
    with Session(db_engine) as s:
        from sqlmodel import select
        assert s.exec(select(TaxSetting)).first() is None


def test_per_year_rate_overrides_default_on_page(client, db_engine):
    uid = make_user(db_engine, "alice")
    _seed_invoice_and_expense(db_engine, uid, 2025)   # base 250 000
    with Session(db_engine) as s:
        # Insurance rate 30 % instead of the default 29.2 %.
        s.add(TaxSetting(year=2025, social_threshold_secondary=105520,
                         income_tax_rate=0.15, social_assessment_rate=0.55,
                         social_insurance_rate=0.30))
        s.commit()
    _login(client, "alice")
    r = client.get("/dane?year=2025&date_field=paid&expense_mode=real")
    assert r.status_code == 200
    # vyměřovací základ 137 500 (55 % of 250 000) × 30 % = 41 250, thousands grouped.
    assert format_money(41250) in r.text


def test_non_admin_cannot_set_tax_setting(client, db_engine):
    make_user(db_engine, "alice")
    _login(client, "alice")
    r = client.post("/ui/admin/danova-nastaveni", data=_tax_form(),
                    headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 403


# ── /dane page aggregation ───────────────────────────────────────────────────

def _seed_invoice_and_expense(engine, user_id, year):
    with Session(engine) as s:
        contact = Contact(user_id=user_id, name="Klient", address="Praha", ic="")
        s.add(contact)
        s.commit()
        s.refresh(contact)
        s.add(Invoice(
            user_id=user_id, contact_id=contact.id, variable_symbol=f"{year}0001",
            amount=300_000, date_issued=date(year, 1, 10),
            date_due=date(year, 1, 24), date_paid=date(year, 2, 1), is_paid=True,
        ))
        s.add(Expense(
            user_id=user_id, date=date(year, 3, 1), category="Materiál",
            amount=50_000, description="x",
        ))
        s.commit()


def _set_activity(engine, user_id, activity):
    from sqlmodel import select
    with Session(engine) as s:
        p = s.exec(select(SupplierProfile).where(
            SupplierProfile.user_id == user_id)).first()
        p.osvc_activity_type = activity
        s.add(p)
        s.commit()


def test_dane_page_uses_paid_invoices(client, db_engine):
    uid = make_user(db_engine, "alice")
    _set_activity(db_engine, uid, "secondary")
    _seed_invoice_and_expense(db_engine, uid, 2025)
    with Session(db_engine) as s:
        s.add(TaxSetting(year=2025, social_threshold_secondary=105520))
        s.commit()

    _login(client, "alice")
    r = client.get("/dane?year=2025&date_field=paid&expense_mode=real")
    assert r.status_code == 200
    body = r.text
    # income 300 000, real expenses 50 000 -> base 250 000 (>= threshold)
    assert "Vzniká účast na důchodovém pojištění" in body
    assert "Pojištění začneš platit při příjmech od" in body


def test_dane_page_flat_rate(client, db_engine):
    uid = make_user(db_engine, "alice")
    _set_activity(db_engine, uid, "secondary")
    _seed_invoice_and_expense(db_engine, uid, 2025)
    _login(client, "alice")
    # No TaxSetting for the year -> threshold-unset notice.
    r = client.get("/dane?year=2025&date_field=paid&expense_mode=flat&flat_rate=60")
    assert r.status_code == 200
    assert "není nastavena" in r.text


def test_dane_page_main_activity_always_pays(client, db_engine):
    # Main activity: premium is shown regardless of profit and with no TaxSetting.
    uid = make_user(db_engine, "alice")           # default profile = main
    _seed_invoice_and_expense(db_engine, uid, 2025)
    _login(client, "alice")
    r = client.get("/dane?year=2025&date_field=paid&expense_mode=real")
    assert r.status_code == 200
    body = r.text
    assert "Povinné důchodové pojištění" in body
    # Estimate present: base 250 000 -> 55 % = 137 500 -> 29.2 % = 40 150.
    assert format_money(40150) in body
    # Health card present: base 250 000 -> 50 % = 125 000 -> 13.5 % = 16 875.
    assert "Zdravotní pojištění" in body
    assert format_money(16875) in body
    # No secondary-only elements.
    assert "Rozhodná částka" not in body
    assert "Pojištění začneš platit při příjmech od" not in body


def test_supplier_form_saves_activity_type(client, db_engine):
    make_user(db_engine, "alice")
    _login(client, "alice")
    r = client.post("/ui/ucet/dodavatel",
                    data={"name": "Test", "ic": "", "osvc_activity_type": "secondary"},
                    headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 200
    from sqlmodel import select
    with Session(db_engine) as s:
        p = s.exec(select(SupplierProfile).where(SupplierProfile.user_id != None)).first()
        assert p.osvc_activity_type == "secondary"
