"""Engine e sessão SQLAlchemy. SQLite local — 1 escritor (worker) + leitores (UI)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from farewatch.config import settings

engine = create_engine(
    settings.sqlalchemy_url,
    echo=False,
    # SQLite: permite uso pelo worker (thread do APScheduler) e pela UI
    connect_args={"check_same_thread": False} if settings.sqlalchemy_url.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Cria as tabelas se não existirem (projeto pequeno: sem Alembic por ora)."""
    from farewatch import models  # noqa: F401 — registra os mapeamentos

    models.Base.metadata.create_all(engine)
