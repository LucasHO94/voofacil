"""Baseline estatístico por rota (+ mês de embarque) a partir do histórico.

Alimenta os gatilhos: trailing minimum (Sprint 3), percentil e error fare.
Segmentar por mês de embarque separa sazonalidade (setembro ≠ dezembro).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from farewatch.models import PriceSnapshot


@dataclass(frozen=True)
class Baseline:
    n: int                      # nº de observações
    minimo: Decimal
    media: Decimal
    p25: Decimal                # 1º quartil — teto do "muito barato"


def baseline_rota(
    session,
    origem: str,
    destino: str,
    mes_embarque: str | None = None,
    excluir_snapshot_id: int | None = None,
) -> Baseline | None:
    """Baseline da rota; `mes_embarque` no formato '2026-09' restringe à sazonalidade.

    `excluir_snapshot_id` permite avaliar um snapshot recém-gravado contra o
    histórico ANTERIOR a ele (senão ele seria o próprio mínimo).
    Retorna None se não há histórico.
    """
    stmt = select(PriceSnapshot.preco).where(
        PriceSnapshot.origem == origem.upper(),
        PriceSnapshot.destino == destino.upper(),
    )
    if excluir_snapshot_id is not None:
        stmt = stmt.where(PriceSnapshot.id != excluir_snapshot_id)
    precos = [Decimal(p) for p, in session.execute(stmt)]

    if mes_embarque:
        # filtro por mês em Python: portátil e barato no volume de um bot pessoal
        stmt_datas = select(PriceSnapshot.preco, PriceSnapshot.data_ida).where(
            PriceSnapshot.origem == origem.upper(),
            PriceSnapshot.destino == destino.upper(),
        )
        if excluir_snapshot_id is not None:
            stmt_datas = stmt_datas.where(PriceSnapshot.id != excluir_snapshot_id)
        precos = [
            Decimal(p)
            for p, d in session.execute(stmt_datas)
            if f"{d.year:04d}-{d.month:02d}" == mes_embarque
        ]

    if not precos:
        return None

    if len(precos) >= 4:
        p25 = Decimal(str(statistics.quantiles([float(p) for p in precos], n=4)[0]))
    else:
        p25 = min(precos)

    return Baseline(
        n=len(precos),
        minimo=min(precos),
        media=Decimal(str(round(statistics.mean(float(p) for p in precos), 2))),
        p25=p25.quantize(Decimal("0.01")),
    )
