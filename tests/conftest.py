"""Fixtures compartilhadas — banco SQLite em memória, isolado por teste."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from farewatch.models import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestSession() as s:
        yield s
