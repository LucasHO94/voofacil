"""Persistência das cotações coletadas — transforma PriceQuote em PriceSnapshot."""
from __future__ import annotations

from farewatch.models import PriceSnapshot, Watch
from farewatch.schemas import PriceQuote


def persistir_quotes(session, watch: Watch, quotes: list[PriceQuote]) -> list[PriceSnapshot]:
    """Grava as cotações de uma varredura como snapshots (série temporal).

    Cada observação é registrada — mesmo preço repetido é amostra legítima da
    série. Retorna os snapshots criados (ainda não commitados).
    """
    snapshots = []
    for q in quotes:
        snap = PriceSnapshot(
            watch_id=watch.id,
            origem=q.origem,
            destino=q.destino,
            data_ida=q.data_ida,
            data_volta=q.data_volta,
            preco=q.preco,
            moeda=q.moeda,
            cia=q.cia,
            escalas=q.escalas,
            fonte=q.fonte,
            deep_link=q.deep_link,
            expires_at=q.expires_at,
        )
        session.add(snap)
        snapshots.append(snap)
    session.flush()
    return snapshots
