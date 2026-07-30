"""Baseline estatístico por rota (+ mês de embarque) a partir do histórico.

Alimenta os gatilhos: trailing minimum (Sprint 3), percentil e error fare.
Segmentar por mês de embarque separa sazonalidade (setembro ≠ dezembro).

SEGMENTAÇÃO — um baseline só é honesto se compara coisas comparáveis, e
origem/destino/mês não bastam. Medido em produção, misturar dimensões na
mesma rota corrompia os dois lados do gatilho:

- ida_volta: com o histórico de um watch ida-e-volta o baseline dava n=5,
  mínimo=4000,00; bastou entrar UM snapshot só-ida de OUTRO watch para virar
  n=6, mínimo=2100,00. Só-ida custar ~50% do round-trip é normal, então a
  mistura (a) fabrica ERROR_FARE falso (preço <= 50% da média) e (b) silencia
  NOVO_MINIMO legítimo, porque o mínimo "histórico" vira inatingível.
- fonte: TP_CACHE (cache defasado em até 48h) e SERPAPI (ao vivo) são
  patamares de preço diferentes para o mesmo voo.
- watch: dois watches na mesma rota podem ter critérios (duração, janela de
  volta, só-ida) que produzem preços de patamares distintos.

`baseline_comparavel` aplica as quatro segmentações de uma vez e é o que o
detector e a confirmação ao vivo usam. `baseline_rota` continua aberta e
permissiva para uso informativo (ex.: a linha de "mínimo histórico" no
gráfico do painel, que é sobre a rota inteira e não sobre um gatilho).

WARM-UP conta RODADAS DE COLETA, não linhas (ver `Baseline.rodadas`): uma
única chamada de calendar da Travelpayouts grava ~30 snapshots de uma vez e
`persistir_quotes` dá flush, então eles já entram no baseline uns dos outros
na MESMA rodada. Contando linhas, um watch sem nenhum histórico ficava
"aquecido" logo na primeira varredura da vida e disparava PERCENTIL para os
dias mais baratos do próprio mês — que não são queda de preço nenhuma, são só
a forma do calendário. `rodadas` (dias distintos de coleta) e `span_dias`
medem tempo de observação de verdade.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from voofacil.models import PriceSnapshot


@dataclass(frozen=True)
class Baseline:
    n: int                      # nº de observações (linhas de snapshot)
    rodadas: int                # nº de RODADAS DE COLETA distintas (dias com coleta)
    span_dias: int              # dias entre a coleta mais antiga e a mais recente
    minimo: Decimal
    media: Decimal
    p25: Decimal                # 1º quartil — teto do "muito barato"


def baseline_rota(
    session,
    origem: str,
    destino: str,
    mes_embarque: str | None = None,
    excluir_snapshot_id: int | None = None,
    watch_id: int | None = None,
    ida_volta: bool | None = None,
    fonte: str | None = None,
) -> Baseline | None:
    """Baseline da rota; `mes_embarque` no formato '2026-09' restringe à sazonalidade.

    `excluir_snapshot_id` permite avaliar um snapshot recém-gravado contra o
    histórico ANTERIOR a ele (senão ele seria o próprio mínimo).

    `watch_id`, `ida_volta` e `fonte` são as segmentações de comparabilidade
    (ver docstring do módulo); None em qualquer uma delas = não filtra por
    aquela dimensão. Quem avalia gatilho deve usar `baseline_comparavel`, que
    preenche as três a partir do snapshot em avaliação.

    Retorna None se não há histórico.
    """
    stmt = select(
        PriceSnapshot.preco, PriceSnapshot.data_ida, PriceSnapshot.coletado_em
    ).where(
        PriceSnapshot.origem == origem.upper(),
        PriceSnapshot.destino == destino.upper(),
    )
    if excluir_snapshot_id is not None:
        stmt = stmt.where(PriceSnapshot.id != excluir_snapshot_id)
    if watch_id is not None:
        stmt = stmt.where(PriceSnapshot.watch_id == watch_id)
    if ida_volta is not None:
        stmt = stmt.where(
            PriceSnapshot.data_volta.is_not(None) if ida_volta
            else PriceSnapshot.data_volta.is_(None)
        )
    if fonte is not None:
        stmt = stmt.where(PriceSnapshot.fonte == fonte)

    # filtro por mês em Python: portátil e barato no volume de um bot pessoal
    linhas = [
        (Decimal(preco), coletado_em)
        for preco, data_ida, coletado_em in session.execute(stmt)
        if mes_embarque is None or f"{data_ida.year:04d}-{data_ida.month:02d}" == mes_embarque
    ]
    if not linhas:
        return None

    precos = [p for p, _ in linhas]
    # Rodadas = DIAS distintos de coleta. `coletado_em` chega naive no SQLite e
    # aware no Postgres; .date() serve aos dois. None só aconteceria em linha
    # nunca flushada (o default do modelo é utcnow) — ignorada por segurança.
    dias_coleta = sorted({c.date() for _, c in linhas if c is not None})
    span_dias = (dias_coleta[-1] - dias_coleta[0]).days if dias_coleta else 0

    if len(precos) >= 4:
        p25 = Decimal(str(statistics.quantiles([float(p) for p in precos], n=4)[0]))
    else:
        p25 = min(precos)

    return Baseline(
        n=len(precos),
        rodadas=len(dias_coleta),
        span_dias=span_dias,
        minimo=min(precos),
        media=Decimal(str(round(statistics.mean(float(p) for p in precos), 2))),
        p25=p25.quantize(Decimal("0.01")),
    )


def baseline_comparavel(session, watch_id: int, snap: PriceSnapshot) -> Baseline | None:
    """Baseline formado só por snapshots COMPARÁVEIS a `snap`.

    Mesmo watch, mesma rota, mesmo mês de embarque, mesmo tipo de viagem
    (só-ida vs ida-e-volta) e mesma fonte — e sem o próprio `snap`, que está
    sendo avaliado contra o passado. É esta a função que o detector e a
    confirmação ao vivo devem usar; ver a seção SEGMENTAÇÃO no topo do módulo
    para o porquê de cada dimensão.
    """
    mes = f"{snap.data_ida.year:04d}-{snap.data_ida.month:02d}"
    return baseline_rota(
        session,
        snap.origem,
        snap.destino,
        mes_embarque=mes,
        excluir_snapshot_id=snap.id,
        watch_id=watch_id,
        ida_volta=snap.data_volta is not None,
        fonte=snap.fonte,
    )
