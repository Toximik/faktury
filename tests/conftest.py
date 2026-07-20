"""Shared pytest fixtures.

Each test gets a fresh, isolated SQLite database in a temp file. We patch the
module-level engine in app.database BEFORE the app's lifespan runs, so both
create_db_and_tables() and the get_session dependency use the test engine and
the real faktury.db is never touched.
"""

from __future__ import annotations

import os
import tempfile

# Force development mode so a random SECRET_KEY is generated and the Secure
# cookie flag is off (TestClient speaks plain HTTP).
os.environ["APP_ENV"] = "development"

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import app.database as database
import app.models  # noqa: F401  – registers tables on SQLModel.metadata
from app.auth import get_password_hash
from app.models import SupplierProfile, User

# Same-origin header the CSRF middleware expects for cookie-authenticated writes.
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def db_engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_engine(
        f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False}
    )
    database.engine = engine          # both lifespan/Alembic and get_session read this
    SQLModel.metadata.create_all(engine)
    # The schema above is born current — stamp it at the Alembic head so the
    # app's startup `alembic upgrade head` (run in the lifespan against this
    # engine) is a no-op instead of trying to re-create existing tables.
    from alembic import command
    command.stamp(database._alembic_config(), "head")
    yield engine
    os.unlink(tmp.name)


@pytest.fixture
def client(db_engine):
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_user(engine, username, password="password-1234", *, is_admin=False,
              is_active=True, dic=None):
    """Insert a user + supplier profile, returning the user id."""
    with Session(engine) as s:
        user = User(
            username=username,
            hashed_password=get_password_hash(password),
            is_admin=is_admin,
            is_active=is_active,
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        s.add(SupplierProfile(user_id=user.id, dic=dic))
        s.commit()
        return user.id


def bearer(client, username, password="password-1234"):
    """Log in via /token and return an Authorization header dict (Bearer)."""
    r = client.post("/token", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
