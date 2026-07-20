"""Security & multi-tenant isolation regression tests."""

from __future__ import annotations

from sqlmodel import Session, select

from app.models import Contact, Invoice, User
from tests.conftest import ORIGIN, bearer, make_user

VALID_ICO = "25596641"     # passes the mod-11 check
VALID_ICO_2 = "27082440"


def _contact(client, headers, ic=VALID_ICO, name="Klient"):
    return client.post("/api/contacts", headers=headers, json={
        "name": name, "address": "Ulice 1\n110 00 Praha", "ic": ic,
    })


def _invoice_payload(contact_id, vat_rate=0):
    return {
        "contact_id": contact_id,
        "date_issued": "2026-01-10", "date_due": "2026-01-24",
        "items": [{"description": "Práce", "quantity": 1, "unit_price": 1000,
                   "vat_rate": vat_rate}],
    }


# ── Fix #1: cross-tenant contact isolation ───────────────────────────────────

def test_cannot_create_invoice_for_another_users_contact(client, db_engine):
    make_user(db_engine, "alice")
    make_user(db_engine, "bob")
    bob_h = bearer(client, "bob")
    bob_contact = _contact(client, bob_h).json()["id"]

    alice_h = bearer(client, "alice")
    r = client.post("/invoices", headers=alice_h,
                    json=_invoice_payload(bob_contact))
    assert r.status_code == 404


def test_cannot_repoint_invoice_to_foreign_contact(client, db_engine):
    make_user(db_engine, "alice")
    make_user(db_engine, "bob")
    alice_h = bearer(client, "alice")
    bob_h = bearer(client, "bob")
    alice_contact = _contact(client, alice_h).json()["id"]
    bob_contact = _contact(client, bob_h).json()["id"]

    inv = client.post("/invoices", headers=alice_h,
                      json=_invoice_payload(alice_contact)).json()["id"]
    r = client.patch(f"/invoices/{inv}", headers=alice_h,
                     json={"contact_id": bob_contact})
    assert r.status_code == 404


# ── Fix #2: arbitrary-file read via stamp_image_path ─────────────────────────

def test_stamp_image_path_is_not_accepted(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    cid = _contact(client, h).json()["id"]
    payload = _invoice_payload(cid)
    payload["stamp_image_path"] = "/etc/passwd"
    r = client.post("/invoices", headers=h, json=payload)
    assert r.status_code == 201
    assert "stamp_image_path" not in r.json()
    with Session(db_engine) as s:
        inv = s.exec(select(Invoice)).first()
        # The model has no such field at all — the path can't be stored.
        assert not hasattr(inv, "stamp_image_path")


def test_unknown_user_and_wrong_password_are_indistinguishable(client, db_engine):
    """Same status and body for both failure modes (no username enumeration).
    Timing is equalised too: _authenticate_user verifies against a dummy
    bcrypt hash when the username does not exist."""
    make_user(db_engine, "alice")
    wrong_pw = client.post("/token", data={"username": "alice", "password": "bad-password-1"})
    no_user = client.post("/token", data={"username": "ghost", "password": "bad-password-1"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()


# ── is_active enforcement on the JSON API ────────────────────────────────────

def test_disabled_user_token_is_rejected_by_api(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    assert client.get("/api/contacts", headers=h).status_code == 200
    # Disable the account out of band.
    with Session(db_engine) as s:
        u = s.exec(select(User).where(User.username == "alice")).first()
        u.is_active = False
        u.token_version += 1
        s.add(u)
        s.commit()
    assert client.get("/api/contacts", headers=h).status_code == 401


# ── token_version revocation ─────────────────────────────────────────────────

def test_token_version_bump_invalidates_old_token(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    assert client.get("/api/contacts", headers=h).status_code == 200
    with Session(db_engine) as s:
        u = s.exec(select(User).where(User.username == "alice")).first()
        u.token_version += 1     # simulate password change elsewhere
        s.add(u)
        s.commit()
    assert client.get("/api/contacts", headers=h).status_code == 401


# ── CSRF same-origin guard ───────────────────────────────────────────────────

def test_cookie_write_without_origin_is_blocked(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    # Cookie is now in the jar; no Origin header → CSRF block.
    r = client.post("/ui/ucet/dodavatel", data={"ic": VALID_ICO},
                    follow_redirects=False)
    assert r.status_code == 403


def test_cookie_write_with_same_origin_is_allowed(client, db_engine):
    make_user(db_engine, "alice")
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    r = client.post("/ui/ucet/dodavatel", data={"ic": VALID_ICO},
                    headers=ORIGIN, follow_redirects=False)
    assert r.status_code in (200, 303)


def test_bearer_api_is_exempt_from_csrf(client, db_engine):
    make_user(db_engine, "alice")
    h = bearer(client, "alice")
    # No Origin header, but Bearer auth → allowed.
    r = _contact(client, h)
    assert r.status_code == 201


# ── Browser login renders HTML, not raw JSON, on bad credentials ─────────────

def test_bad_login_rerenders_form_not_json(client, db_engine):
    make_user(db_engine, "alice")
    r = client.post("/login", data={"username": "alice", "password": "wrong-password"},
                    follow_redirects=False)
    assert r.status_code == 401
    assert "text/html" in r.headers["content-type"]
    assert "Nesprávné uživatelské jméno nebo heslo." in r.text
    assert '"detail"' not in r.text          # not the JSON API error body


def test_good_login_sets_cookie_and_redirects(client, db_engine):
    make_user(db_engine, "alice")
    r = client.post("/login", data={"username": "alice", "password": "password-1234"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "access_token" in r.cookies


# ── Admin password reset (recovery for a forgotten password) ─────────────────

def _login_as_admin(client, db_engine, username="admin"):
    """Create an admin and log in via the cookie flow (clearing any prior jar)."""
    make_user(db_engine, username, is_admin=True)
    client.cookies.clear()
    client.post("/login", data={"username": username, "password": "password-1234"})


def _reset(client, target_id, new="brand-new-pass-9", confirm=None):
    return client.post(
        "/ui/admin/reset-hesla", headers=ORIGIN, follow_redirects=False,
        data={"target_id": target_id, "new_password": new,
              "confirm_password": new if confirm is None else confirm},
    )


def _can_login(client, username, password):
    """Status of a fresh Bearer login (clear the cookie jar so /token isn't
    treated as a cookie-authenticated write by the CSRF guard)."""
    client.cookies.clear()
    return client.post("/token", data={"username": username, "password": password}).status_code


def test_admin_can_reset_user_password(client, db_engine):
    bob_id = make_user(db_engine, "bob")
    # Bob's current token works before the reset.
    bob_h = bearer(client, "bob")
    assert client.get("/api/contacts", headers=bob_h).status_code == 200

    _login_as_admin(client, db_engine)
    r = _reset(client, bob_id)
    assert r.status_code == 200
    assert "bylo obnoveno" in r.text

    # Reset revokes the user's outstanding sessions (token_version bump)...
    assert client.get("/api/contacts", headers=bob_h).status_code == 401
    # ...the new password works and the old one no longer does.
    assert _can_login(client, "bob", "brand-new-pass-9") == 200
    assert _can_login(client, "bob", "password-1234") == 401


def test_non_admin_cannot_reset_password(client, db_engine):
    make_user(db_engine, "alice")
    bob_id = make_user(db_engine, "bob")
    client.cookies.clear()
    client.post("/login", data={"username": "alice", "password": "password-1234"})
    assert _reset(client, bob_id).status_code == 403
    # Bob's password is untouched.
    assert _can_login(client, "bob", "password-1234") == 200


def test_reset_password_enforces_policy(client, db_engine):
    bob_id = make_user(db_engine, "bob")
    _login_as_admin(client, db_engine)
    assert _reset(client, bob_id, new="short").status_code == 422
    # Too-short password rejected → original still valid.
    assert _can_login(client, "bob", "password-1234") == 200


def test_reset_password_requires_matching_confirmation(client, db_engine):
    bob_id = make_user(db_engine, "bob")
    _login_as_admin(client, db_engine)
    r = _reset(client, bob_id, new="brand-new-pass-9", confirm="different-pass-9")
    assert r.status_code == 422
    assert "neshodují" in r.text
    assert _can_login(client, "bob", "password-1234") == 200


def test_admin_cannot_reset_own_password_here(client, db_engine):
    admin_id = make_user(db_engine, "admin", is_admin=True)
    client.cookies.clear()
    client.post("/login", data={"username": "admin", "password": "password-1234"})
    # Self-reset is blocked (admins use /account, which verifies the current pw).
    assert _reset(client, admin_id).status_code == 400


def test_admin_create_user_error_paths_rerender_with_message(client, db_engine):
    """Invalid submissions re-render the admin page (422), never 500."""
    _login_as_admin(client, db_engine)

    r = client.post("/ui/admin/uzivatele/novy", headers=ORIGIN,
                    data={"username": "ab", "password": "password-1234"})
    assert r.status_code == 422
    assert "alespoň 3 znaky" in r.text

    r = client.post("/ui/admin/uzivatele/novy", headers=ORIGIN,
                    data={"username": "newuser", "password": "short"})
    assert r.status_code == 422
    assert "Heslo musí mít alespoň" in r.text

    r = client.post("/ui/admin/uzivatele/novy", headers=ORIGIN,
                    data={"username": "admin", "password": "password-1234"})
    assert r.status_code == 422
    assert "již existuje" in r.text
