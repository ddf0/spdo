"""Shared fixtures: a migrated PostgreSQL database per test session."""

import os

os.environ.setdefault("SPDO_PASSWORD_ROUNDS", "4")  # fast bcrypt in tests
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from spdo.config import settings

TEST_DB_URL = os.environ.get(
    "SPDO_TEST_DATABASE_URL", "postgresql+psycopg://spdo:spdo@127.0.0.1:55432/spdo"
)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Fresh schema: downgrade to base, then upgrade to head."""
    if TEST_DB_URL == settings.database_url:
        pytest.exit("Test DB must differ from SPDO_DATABASE_URL: tests drop the schema")
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = create_engine(TEST_DB_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Session inside a transaction that is rolled back after the test."""
    with engine.connect() as conn:
        trans = conn.begin()
        with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
            yield s
        trans.rollback()


@pytest.fixture
def base_rows(session: Session) -> dict[str, int]:
    """One user, category and component."""
    uid = session.execute(
        text(
            "insert into users(username, full_name, role, password_hash, is_active)"
            " values ('u', 'U', 'user', 'h', true) returning id"
        )
    ).scalar_one()
    cid = session.execute(
        text("insert into categories(name, description) values ('c', '') returning id")
    ).scalar_one()
    mid = session.execute(
        text("insert into components(name) values ('m') returning id")
    ).scalar_one()
    return {"user": uid, "category": cid, "component": mid}


@pytest.fixture
def client(session: Session):
    """HTTP client whose requests share the rolled-back test session."""
    from fastapi.testclient import TestClient

    from spdo.db.session import get_session
    from spdo.main import app

    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
