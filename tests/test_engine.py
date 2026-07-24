"""Testes da engine: persistência de quotes, baseline e varredura do worker."""
from datetime import date
from decimal import Decimal

from farewatch.engine.baseline import baseline_rota
from farewatch.engine.store import persistir_quotes
from farewatch.models import PriceSnapshot, TipoWatch, Watch
from farewatch.schemas import PriceQuote
from farewatch.worker import executar_varredura


def _watch(session, **kw):
    base = dict(
        nome="GRU -> LIS",
        tipo=TipoWatch.ROTA.value,
        origem_iata="GRU",
        destino_iata="LIS",
        data_ini=date(2026, 9, 1),
        data_fim=date(2026, 9, 30),
    )
    base.update(kw)
    w = Watch(**base)
    session.add(w)
    session.flush()
    return w


def _quote(preco: str, dia: int = 10, mes: int = 9) -> PriceQuote:
    return PriceQuote(
        origem="GRU",
        destino="LIS",
        data_ida=date(2026, mes, dia),
        preco=Decimal(preco),
        fonte="TP_CACHE",
    )


class FakeProvider:
    nome = "FAKE"

    def __init__(self, quotes):
        self.quotes = quotes
        self.chamadas = 0

    def sweep(self, watch):
        self.chamadas += 1
        return self.quotes


class ExplodingProvider:
    nome = "BOOM"

    def sweep(self, watch):
        raise RuntimeError("api fora do ar")


def test_persistir_quotes_grava_snapshots(session):
    w = _watch(session)
    snaps = persistir_quotes(session, w, [_quote("3100.00"), _quote("2990.50", dia=15)])
    session.commit()

    assert len(snaps) == 2
    assert all(s.watch_id == w.id for s in snaps)
    assert {s.preco for s in snaps} == {Decimal("3100.00"), Decimal("2990.50")}


def test_baseline_rota_min_media_p25(session):
    w = _watch(session)
    precos = ["3000", "3200", "3400", "2800", "3600", "3100"]
    persistir_quotes(session, w, [_quote(p, dia=i + 1) for i, p in enumerate(precos)])
    session.commit()

    b = baseline_rota(session, "GRU", "LIS")
    assert b is not None
    assert b.n == 6
    assert b.minimo == Decimal("2800")
    assert b.media == Decimal("3183.33")
    assert b.p25 < b.media


def test_baseline_segmenta_por_mes_embarque(session):
    w = _watch(session, data_fim=date(2026, 12, 31))
    persistir_quotes(session, w, [
        _quote("3000", dia=10, mes=9),
        _quote("5000", dia=20, mes=12),   # dezembro caro (alta temporada)
    ])
    session.commit()

    setembro = baseline_rota(session, "GRU", "LIS", mes_embarque="2026-09")
    dezembro = baseline_rota(session, "GRU", "LIS", mes_embarque="2026-12")
    assert setembro.minimo == Decimal("3000")
    assert dezembro.minimo == Decimal("5000")


def test_baseline_exclui_snapshot_avaliado(session):
    w = _watch(session)
    persistir_quotes(session, w, [_quote("3000")])
    novo = persistir_quotes(session, w, [_quote("2500", dia=15)])[0]
    session.commit()

    b = baseline_rota(session, "GRU", "LIS", excluir_snapshot_id=novo.id)
    assert b.minimo == Decimal("3000")   # o próprio 2500 não conta no histórico


def test_baseline_sem_historico_retorna_none(session):
    assert baseline_rota(session, "GRU", "XXX") is None


def test_executar_varredura_persiste_por_watch(session):
    w1 = _watch(session)
    w2 = _watch(session, nome="GRU -> MAD", destino_iata="MAD")
    provider = FakeProvider([_quote("3100"), _quote("2990", dia=15)])

    resultado = executar_varredura(session, providers=[provider])
    session.commit()

    assert resultado.snapshots_por_watch == {w1.id: 2, w2.id: 2}
    assert provider.chamadas == 2
    assert session.query(PriceSnapshot).count() == 4


def test_executar_varredura_ignora_watch_inativo(session):
    _watch(session, ativo=False)
    provider = FakeProvider([_quote("3100")])

    resultado = executar_varredura(session, providers=[provider])

    assert resultado.snapshots_por_watch == {}
    assert provider.chamadas == 0


def test_provider_quebrado_nao_derruba_varredura(session):
    w = _watch(session)
    bom = FakeProvider([_quote("3100")])

    resultado = executar_varredura(session, providers=[ExplodingProvider(), bom])
    session.commit()

    assert resultado.snapshots_por_watch == {w.id: 1}


def test_varredura_gera_candidato_abaixo_do_limite(session):
    w = _watch(session, preco_limite=Decimal("3000"))
    provider = FakeProvider([_quote("2890")])

    resultado = executar_varredura(session, providers=[provider])
    session.commit()

    assert len(resultado.candidatos) == 1
    cand = resultado.candidatos[0]
    assert cand.watch_id == w.id
    assert cand.gatilho == "LIMITE"
    assert cand.quote.preco == Decimal("2890")
