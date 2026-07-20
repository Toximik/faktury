"""
init_db.py – One-time initialisation script.

Run from the project root after creating the venv and installing deps:

    python -m app.init_db

What it does
------------
1. Creates all database tables (idempotent – safe to re-run).
2. Creates the default 'master' user if one does not already exist.
   The password is read from the ADMIN_PASSWORD environment variable, or
   prompted interactively if the variable is not set.

Security notes
--------------
- Minimum password length is enforced (12 characters).
- Passwords are hashed with bcrypt (cost factor 12) via passlib.
- The plaintext password is never written to disk or logged.
"""

from __future__ import annotations

import getpass
import os
import sys

from sqlmodel import Session, select

from app.auth import get_password_hash, validate_password_policy
from app.config import MIN_PASSWORD_LENGTH
from app.database import create_db_and_tables, engine
from app.models import SupplierProfile, User


def _hash_password(password: str) -> str:
    return get_password_hash(password)


def _prompt_password() -> str:
    """Interactively prompt for a new password with confirmation."""
    password = getpass.getpass(f"Set master password (min {MIN_PASSWORD_LENGTH} chars): ")
    policy_error = validate_password_policy(password)
    if policy_error:
        print(f"[ERROR] {policy_error}", file=sys.stderr)
        sys.exit(1)

    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("[ERROR] Passwords do not match.", file=sys.stderr)
        sys.exit(1)

    return password


def _get_password() -> str:
    """Return the master password from env var or interactive prompt."""
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if password:
        policy_error = validate_password_policy(password)
        if policy_error:
            print(f"[ERROR] ADMIN_PASSWORD: {policy_error}", file=sys.stderr)
            sys.exit(1)
        return password
    return _prompt_password()


def init() -> None:
    # Step 1 – tables
    print("→ Creating database tables …")
    create_db_and_tables()
    print("  Tables OK.")

    # Step 2 – default master user
    with Session(engine) as session:
        existing: User | None = session.exec(
            select(User).where(User.username == "master")
        ).first()

        if existing:
            print("→ Master user already exists. Skipping user creation.")
            return

        password = _get_password()
        master = User(
            username="master",
            hashed_password=_hash_password(password),
            is_admin=True,
        )
        session.add(master)
        session.commit()
        session.refresh(master)
        print("\u2192 Master user created successfully.")

        # Step 3 – blank supplier profile for master
        profile = SupplierProfile(user_id=master.id)
        session.add(profile)
        session.commit()
        print("\u2192 Blank supplier profile created for master.")

    print("\nInitialisation complete. You can now start the application.")


if __name__ == "__main__":
    init()
