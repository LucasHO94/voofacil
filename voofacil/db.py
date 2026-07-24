"""Engine e sessão SQLAlchemy.

Dois modos, escolhidos pela DATABASE_URL:
- SQLite local (dev): 1 escritor (worker) + leitores (UI) no mesmo arquivo.
- Postgres/Neon (nuvem): estado compartilhado entre o worker (GitHub Actions)
  e o painel (Streamlit Cloud). O Neon faz scale-to-zero e acorda na conexão,
  então usamos pool_pre_ping + connect_timeout para tolerar o cold start.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from voofacil.config import settings

_url = settings.sqlalchemy_url
if _url.startswith("sqlite"):
    engine = create_engine(
        _url,
        echo=False,
        connect_args={"check_same_thread": False},
    )
else:
    # Postgres (Neon e afins): reconexão resiliente ao cold start do serverless
    engine = create_engine(
        _url,
        echo=False,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Cria as tabelas se não existirem (projeto pequeno: sem Alembic por ora)."""
    from voofacil import models  # noqa: F401 — registra os mapeamentos

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
