"""Resolução da URL do banco e o guard contra SQLite efêmero na nuvem.

Estes dois pontos foram apontados por auditoria como a causa mais provável de
"o agendamento não roda nos horários da aba": o worker no GitHub Actions ou
caía num SQLite destruído a cada run (secret DATABASE_URL ausente), ou quebrava
antes de conectar (scheme `postgres://`/`postgresql://` sem o driver psycopg2).
Ambos deixavam o Neon — que o painel lê — sem nenhuma escrita, em silêncio.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from voofacil import worker
from voofacil.config import _normalizar_url_postgres, descricao_banco


# -- normalização de scheme -------------------------------------------------

def test_normaliza_postgres_cru_para_psycopg():
    assert _normalizar_url_postgres("postgres://u:p@host/db") == "postgresql+psycopg://u:p@host/db"


def test_normaliza_postgresql_para_psycopg():
    assert _normalizar_url_postgres("postgresql://u:p@host/db") == "postgresql+psycopg://u:p@host/db"


def test_nao_mexe_no_que_ja_tem_driver():
    url = "postgresql+psycopg://u:p@host/db"
    assert _normalizar_url_postgres(url) == url


def test_nao_mexe_em_sqlite():
    assert _normalizar_url_postgres("sqlite:///x.db") == "sqlite:///x.db"


# -- descrição mascarada (segura para log) ----------------------------------

def test_descricao_banco_nao_vaza_usuario_nem_senha():
    d = descricao_banco("postgresql+psycopg://usuario:senha_secreta@ep-abc.sa-east-1.neon.tech/neondb")
    assert "senha_secreta" not in d
    assert "usuario" not in d
    assert "ep-abc.sa-east-1.neon.tech/neondb" in d


def test_descricao_banco_marca_sqlite_como_efemero():
    assert "SQLite" in descricao_banco("sqlite:///data/voofacil.db")


# -- guard: SQLite na nuvem é sempre engano ---------------------------------

def test_guard_aborta_com_sqlite_no_github_actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(worker, "settings", SimpleNamespace(sqlalchemy_url="sqlite:///data/voofacil.db"))
    with pytest.raises(SystemExit):
        worker._abortar_se_sqlite_no_actions()


def test_guard_deixa_passar_postgres_no_actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(worker, "settings",
                        SimpleNamespace(sqlalchemy_url="postgresql+psycopg://h/db"))
    worker._abortar_se_sqlite_no_actions()   # não levanta


def test_guard_ignora_sqlite_fora_do_actions(monkeypatch):
    """Local (dev/--once/testes) não tem GITHUB_ACTIONS: SQLite é legítimo."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(worker, "settings", SimpleNamespace(sqlalchemy_url="sqlite:///data/voofacil.db"))
    worker._abortar_se_sqlite_no_actions()   # não levanta
