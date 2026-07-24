"""Testes do job de busca ampla (explore diário) do worker."""
from datetime import date
from decimal import Decimal

from farewatch.models import TipoWatch, Watch
from farewatch.schemas import PriceQuote
from farewatch.worker import _meses_a_frente, executar_explore


def _watch_regiao(session, **kw):
    base = dict(
        nome="GRU -> Europa",
        tipo=TipoWatch.REGIAO.value,
        origem_iata="GRU",
        destino_iata=None,
        regiao_destinos="[]",
        arrival_area_id="/m/02j9z",
        data_ini=date(2026, 9, 1),
        data_fim=date(2026, 11, 30),
    )
    base.update(kw)
    w = Watch(**base)
    session.add(w)
    session.flush()
    return w


class FakeExploreProvider:
    def __init__(self, quotes_por_mes: dict[int, list[PriceQuote]]):
        self.quotes_por_mes = quotes_por_mes
        self.chamadas = []

    def explorar_regiao(self, origem, arrival_area_id, mes):
        self.chamadas.append((origem, arrival_area_id, mes))
        return self.quotes_por_mes.get(mes, [])


def _quote(destino, preco, mes=9, dia=12):
    return PriceQuote(
        origem="GRU", destino=destino, data_ida=date(2026, mes, dia),
        preco=Decimal(preco), fonte="SERPAPI",
    )


def test_executar_explore_varre_watches_regiao_com_area_id(session):
    _watch_regiao(session)
    provider = FakeExploreProvider({9: [_quote("LIS", "3100"), _quote("MAD", "2950")]})

    resultado = executar_explore(session, provider)
    session.commit()

    assert sum(resultado.snapshots_por_watch.values()) >= 2
    assert len(provider.chamadas) == 3   # MESES_EXPLORE_A_FRENTE = 3


def test_executar_explore_ignora_watch_sem_area_id(session):
    _watch_regiao(session, arrival_area_id=None)
    provider = FakeExploreProvider({})

    resultado = executar_explore(session, provider)

    assert resultado.snapshots_por_watch == {}
    assert provider.chamadas == []


def test_executar_explore_ignora_watch_rota(session):
    Watch_rota = Watch(
        nome="GRU -> LIS", tipo=TipoWatch.ROTA.value, origem_iata="GRU",
        destino_iata="LIS", data_ini=date(2026, 9, 1), data_fim=date(2026, 9, 30),
    )
    session.add(Watch_rota)
    session.flush()
    provider = FakeExploreProvider({})

    resultado = executar_explore(session, provider)

    assert resultado.snapshots_por_watch == {}


def test_executar_explore_gera_candidato_com_limite(session):
    w = _watch_regiao(session, preco_limite=Decimal("3000"))
    provider = FakeExploreProvider({9: [_quote("MAD", "2950")]})

    resultado = executar_explore(session, provider)

    candidatos_mad = [c for c in resultado.candidatos if c.quote.destino == "MAD"]
    assert len(candidatos_mad) == 1
    assert candidatos_mad[0].gatilho == "LIMITE"


def test_meses_a_frente_vira_ano():
    assert _meses_a_frente(3, hoje=date(2026, 11, 15)) == [11, 12, 1]
