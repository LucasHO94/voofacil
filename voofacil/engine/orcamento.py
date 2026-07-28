"""Controle de orçamento de chamadas por provedor — mensal e diário.

Sem isto, "confirmar todo candidato ao vivo" ou a varredura multi-fonte
poderiam estourar o free tier em poucos dias de rota volátil. Os contadores
são persistidos (sobrevivem a restart). O teto DIÁRIO existe além do mensal
porque a SerpApi é chamada em 3 pontos independentes do worker (varredura
regular multi-fonte, confirmação de candidato e busca ampla/Explore) — um
teto só mensal (250) permite rajadas grandes num único dia se houver muitos
watches; o diário (padrão 8) evita isso mesmo com folga no mês.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from voofacil.config import settings
from voofacil.models import ContadorApi, ContadorDiarioApi


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


def _dia_str(hoje: date | None = None) -> str:
    return (hoje or date.today()).isoformat()


def consumo_diario(session, provedor: str, hoje: date | None = None) -> int:
    dia = _dia_str(hoje)
    row = session.scalar(
        select(ContadorDiarioApi).where(
            ContadorDiarioApi.provedor == provedor, ContadorDiarioApi.dia == dia
        )
    )
    return row.chamadas if row else 0


def ha_orcamento_diario(session, provedor: str, limite_diario: int, hoje: date | None = None) -> bool:
    return consumo_diario(session, provedor, hoje) < limite_diario


def registrar_chamada_diaria(session, provedor: str, hoje: date | None = None) -> int:
    """Incrementa o contador do dia corrente e retorna o total após o incremento."""
    dia = _dia_str(hoje)
    row = session.scalar(
        select(ContadorDiarioApi).where(
            ContadorDiarioApi.provedor == provedor, ContadorDiarioApi.dia == dia
        )
    )
    if row is None:
        row = ContadorDiarioApi(provedor=provedor, dia=dia, chamadas=0)
        session.add(row)
        session.flush()
    row.chamadas += 1
    session.flush()
    return row.chamadas


def ha_orcamento_serpapi(session, hoje: date | None = None) -> bool:
    """Combina o teto mensal (free tier, ex.: 250) E o teto diário (ex.: 8) da
    SerpApi — usado nos 3 pontos onde ela é chamada (sweep multi-fonte,
    confirmação de candidato, Explore). Basta UM dos dois estourar para
    bloquear; nenhum contador é decrementado quando bloqueado."""
    return (
        ha_orcamento(session, "SERPAPI", settings.serpapi_orcamento_mes, hoje)
        and ha_orcamento_diario(session, "SERPAPI", settings.serpapi_orcamento_dia, hoje)
    )


def registrar_chamada_serpapi(session, hoje: date | None = None) -> None:
    """Incrementa os dois contadores (mensal e diário) da SerpApi juntos —
    chame só depois de uma chamada real bem-sucedida à API."""
    registrar_chamada(session, "SERPAPI", hoje)
    registrar_chamada_diaria(session, "SERPAPI", hoje)
