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
    _migrar_colunas_novas()


def _migrar_colunas_novas() -> None:
    """Micro-migração: adiciona colunas novas em tabelas JÁ EXISTENTES.

    `create_all` só cria tabelas que faltam — ele NÃO altera tabela existente.
    Sem isto, todo banco antigo (dev em SQLite e, principalmente, o Postgres/
    Neon de produção, que já tem `horario_agendado` sem `ultima_execucao`)
    quebraria com "column ... does not exist" no primeiro SELECT do ORM.
    Para um projeto pessoal isto substitui o Alembic — basta acrescentar aqui
    cada coluna nova de versões futuras.

    Roda nos DOIS dialetos: a descoberta das colunas existentes usa
    `sqlalchemy.inspect(engine).get_columns(...)`, que é portável, em vez do
    `PRAGMA table_info` (só SQLite) usado antes. A única diferença por dialeto
    é a grafia do tipo timestamp com fuso: Postgres quer
    "TIMESTAMP WITH TIME ZONE", SQLite guarda tudo como texto/"TIMESTAMP".
    """
    from sqlalchemy import inspect, text

    e_sqlite = engine.dialect.name == "sqlite"
    timestamp_tz = "TIMESTAMP" if e_sqlite else "TIMESTAMP WITH TIME ZONE"

    colunas_novas = {
        "watch": [
            ("arrival_area_id", "VARCHAR(40)"),
            ("tipo_data_ida", "VARCHAR(10)"),
            ("tipo_data_volta", "VARCHAR(10)"),
            ("flexibilidade_dias", "INTEGER"),
            ("data_volta_ini", "DATE"),
            ("data_volta_fim", "DATE"),
            ("mes_especifico", "INTEGER"),
            ("preco_minimo", "NUMERIC(12,2)"),
            ("preco_aproximado", "NUMERIC(12,2)"),
            # Itinerário open-jaw e tamanho do grupo (ver voofacil/models.py).
            # Ambas entram sem DEFAULT e sem NOT NULL: as linhas já existentes
            # em produção ficam com NULL, lido como "volta do próprio destino"
            # e "1 passageiro" — ver Watch.total_passageiros.
            ("origem_volta_iata", "VARCHAR(3)"),
            ("passageiros", "INTEGER"),
        ],
        "horario_agendado": [
            # Modelo catch-up do agendamento (ver voofacil/engine/agendamento.py).
            ("ultima_execucao", timestamp_tz),
        ],
    }

    # A checagem abaixo é check-then-act: entre ler as colunas e emitir o ALTER
    # existe uma janela. Ela importa de verdade uma única vez na vida do banco —
    # no deploy que introduz a coluna, quando worker (GitHub Actions) e painel
    # (Streamlit Cloud) reiniciam juntos e podem chamar init_db() ao mesmo tempo
    # contra o mesmo Neon: os dois veem a coluna faltando e os dois tentam criar,
    # e o perdedor da corrida morre com DuplicateColumn. Por isso o Postgres
    # ganha "IF NOT EXISTS" (o SQLite não suporta a cláusula, mas lá não há
    # concorrência entre processos que justifique).
    se_nao_existir = "" if e_sqlite else "IF NOT EXISTS "

    inspetor = inspect(engine)
    with engine.begin() as conn:
        for tabela, colunas in colunas_novas.items():
            if not inspetor.has_table(tabela):
                continue  # banco novo em que create_all ainda não passou por aqui
            existentes = {c["name"] for c in inspetor.get_columns(tabela)}
            for nome, tipo in colunas:
                if nome not in existentes:
                    conn.execute(text(
                        f"ALTER TABLE {tabela} ADD COLUMN {se_nao_existir}{nome} {tipo}"
                    ))
