"""Shared pytest fixtures."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from compass.models import Base, IdentityVersion, Job


@pytest.fixture
def session():
    """In-memory SQLite session for fast unit tests — no .env, no real DB."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    try:
        yield s
        s.rollback()
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def identity(session):
    """A persisted IdentityVersion — required because ResumeFact.identity_id is NOT NULL."""
    iv = IdentityVersion(label="test-identity")
    session.add(iv)
    session.flush()
    return iv


@pytest.fixture
def job(session):
    """A persisted Job — required because Job.link is NOT NULL."""
    j = Job(
        title="ML Engineer",
        company="TestCo",
        link="https://example.com/job/1",
        description="Looking for someone with ML pipeline experience.",
    )
    session.add(j)
    session.flush()
    return j
