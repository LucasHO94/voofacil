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
    _migrar_sqlite()


def _migrar_sqlite() -> None:
    """Micro-migração: adiciona colunas novas em bancos SQLite já criados.

    create_all não altera tabelas existentes; para um projeto pessoal isto
    substitui o Alembic. Adicionar aqui cada coluna nova de versões futuras.
    """
    if not settings.sqlalchemy_url.startswith("sqlite"):
        return
    from sqlalchemy import text

    colunas_novas = {
        "watch": [("arrival_area_id", "VARCHAR(40)")],
    }
    with engine.begin() as conn:
        for tabela, colunas in colunas_novas.items():
            existentes = {
                linha[1] for linha in conn.execute(text(f"PRAGMA table_info({tabela})"))
            }
            for nome, tipo in colunas:
                if nome not in existentes:
                    conn.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}"))
