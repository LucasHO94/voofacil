"""Controle de orçamento mensal de chamadas por provedor (ex.: SerpApi 250/mês).

Sem isto, "confirmar todo candidato ao vivo" poderia estourar o free tier em
poucos dias de rota volátil. O contador é persistido (sobrevive a restart).
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from farewatch.models import ContadorApi


def _ano_mes(hoje: date | None = None) -> str:
    d = hoje or date.today()
    return f"{d.year:04d}-{d.month:02d}"


def consumo_atual(session, provedor: str, hoje: date | None = None) -> int:
    ano_mes = _ano_mes(hoje)
    row = session.scalar(
        select(ContadorApi).where(
            ContadorApi.provedor == provedor, ContadorApi.ano_mes == ano_mes
        )
    )
    return row.chamadas if row else 0


def ha_orcamento(session, provedor: str, limite_mensal: int, hoje: date | None = None) -> bool:
    return consumo_atual(session, provedor, hoje) < limite_mensal


def registrar_chamada(session, provedor: str, hoje: date | None = None) -> int:
    """Incrementa o contador do mês corrente e retorna o total após o incremento."""
    ano_mes = _ano_mes(hoje)
    row = session.scalar(
        select(ContadorApi).where(
            ContadorApi.provedor == provedor, ContadorApi.ano_mes == ano_mes
        )
    )
    if row is None:
        row = ContadorApi(provedor=provedor, ano_mes=ano_mes, chamadas=0)
        session.add(row)
        session.flush()
    row.chamadas += 1
    session.flush()
    return row.chamadas
