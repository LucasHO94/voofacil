"""Consultas e mutações usadas pelo painel Streamlit — sem SQL na camada de UI."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from voofacil.models import Alerta, PriceSnapshot, TipoWatch, Watch


def listar_watches(session) -> list[Watch]:
    return list(session.scalars(select(Watch).order_by(Watch.criado_em.desc())))


def criar_watch_rota(
    session, nome: str, origem: str, destino: str,
    data_ini: date, data_fim: date, preco_limite: Decimal | None,
) -> Watch:
    w = Watch(
        nome=nome,
        tipo=TipoWatch.ROTA.value,
        origem_iata=origem.upper(),
        destino_iata=destino.upper(),
        data_ini=data_ini,
        data_fim=data_fim,
        preco_limite=preco_limite,
    )
    session.add(w)
    session.commit()
    return w


def criar_watch_regiao(
    session, nome: str, origem: str, destinos: list[str],
    data_ini: date, data_fim: date, preco_limite: Decimal | None,
    arrival_area_id: str | None = None,
) -> Watch:
    w = Watch(
        nome=nome,
        tipo=TipoWatch.REGIAO.value,
        origem_iata=origem.upper(),
        destino_iata=None,
        regiao_destinos=json.dumps([d.upper() for d in destinos]),
        arrival_area_id=arrival_area_id or None,
        data_ini=data_ini,
        data_fim=data_fim,
        preco_limite=preco_limite,
    )
    session.add(w)
    session.commit()
    return w


def alternar_ativo(session, watch_id: int) -> Watch:
    w = session.get(Watch, watch_id)
    w.ativo = not w.ativo
    session.commit()
    return w


def excluir_watch(session, watch_id: int) -> None:
    w = session.get(Watch, watch_id)
    if w is not None:
        session.delete(w)
        session.commit()


def historico_precos(session, watch_id: int) -> list[PriceSnapshot]:
    stmt = (
        select(PriceSnapshot)
        .where(PriceSnapshot.watch_id == watch_id)
        .order_by(PriceSnapshot.coletado_em)
    )
    return list(session.scalars(stmt))


def alertas_do_watch(session, watch_id: int) -> list[Alerta]:
    stmt = (
        select(Alerta)
        .where(Alerta.watch_id == watch_id)
        .order_by(Alerta.enviado_em.desc())
    )
    return list(session.scalars(stmt))


def alertas_recentes(session, limite: int = 50) -> list[Alerta]:
    stmt = select(Alerta).order_by(Alerta.enviado_em.desc()).limit(limite)
    return list(session.scalars(stmt))
