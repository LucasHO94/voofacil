"""Testes da engine: persistência de quotes, baseline e varredura do worker."""
from datetime import date, timedelta
from decimal import Decimal

from voofacil.engine.baseline import baseline_comparavel, baseline_rota
from voofacil.engine.store import persistir_quotes
from voofacil.models import PriceSnapshot, TipoWatch, Watch, utcnow
from voofacil.schemas import PriceQuote
from voofacil.worker import executar_varredura


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


def _quote(preco: str, dia: int = 10, mes: int = 9, volta: int | None = None,
           fonte: str = "TP_CACHE") -> PriceQuote:
    return PriceQuote(
        origem="GRU",
        destino="LIS",
        data_ida=date(2026, mes, dia),
        data_volta=date(2026, mes, volta) if volta else None,
        preco=Decimal(preco),
        fonte=fonte,
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


def test_baseline_conta_rodadas_de_coleta_e_span(session):
    """`n` conta linhas; `rodadas`/`span_dias` contam TEMPO DE OBSERVAÇÃO. Uma
    chamada de calendar grava o mês inteiro numa tacada — 30 linhas, 1 rodada,
    span 0 — e é isso que o warm-up precisa enxergar."""
    w = _watch(session)
    snaps = persistir_quotes(session, w, [
        _quote("3000", dia=1), _quote("3100", dia=2), _quote("3200", dia=3),
    ])

    base = baseline_rota(session, "GRU", "LIS")
    assert (base.n, base.rodadas, base.span_dias) == (3, 1, 0)

    snaps[0].coletado_em = utcnow() - timedelta(days=2)
    session.flush()

    base = baseline_rota(session, "GRU", "LIS")
    assert (base.n, base.rodadas, base.span_dias) == (3, 2, 2)


def test_baseline_comparavel_ignora_so_ida_de_outro_watch(session):
    """Prova da auditoria: baseline do watch ida-e-volta dava n=5,
    mínimo=4000,00; bastou UM snapshot só-ida de OUTRO watch para virar n=6,
    mínimo=2100,00. Só-ida custar ~metade do round-trip é normal, então a
    mistura fabricava ERROR_FARE falso e silenciava NOVO_MINIMO legítimo."""
    ida_e_volta = _watch(session)
    so_ida = _watch(session, nome="GRU -> LIS só ida", ida_volta=False)
    persistir_quotes(session, ida_e_volta, [
        _quote(p, dia=i + 1, volta=28)
        for i, p in enumerate(["4000", "4200", "4400", "4100", "4300"])
    ])
    alvo = persistir_quotes(session, ida_e_volta, [_quote("3900", dia=20, volta=28)])[0]

    antes = baseline_comparavel(session, ida_e_volta.id, alvo)
    persistir_quotes(session, so_ida, [_quote("2100", dia=12)])   # só-ida, outro watch
    depois = baseline_comparavel(session, ida_e_volta.id, alvo)

    assert (antes.n, antes.minimo) == (5, Decimal("4000"))
    assert (depois.n, depois.minimo) == (5, Decimal("4000"))


def test_baseline_comparavel_segmenta_por_fonte(session):
    """TP_CACHE (defasado até 48h) e SERPAPI (ao vivo) não são o mesmo
    patamar de preço; não podem formar um baseline só."""
    w = _watch(session)
    persistir_quotes(session, w, [
        _quote("3000", dia=1),
        _quote("2000", dia=2, fonte="SERPAPI"),
    ])
    alvo = persistir_quotes(session, w, [_quote("2900", dia=20)])[0]   # TP_CACHE

    base = baseline_comparavel(session, w.id, alvo)

    assert (base.n, base.minimo) == (1, Decimal("3000"))


def test_baseline_comparavel_segmenta_por_watch(session):
    """Dois watches na mesma rota podem ter critérios (duração, janela de
    volta) que produzem patamares diferentes — cada um tem seu histórico."""
    meu = _watch(session)
    outro = _watch(session, nome="GRU -> LIS outro")
    persistir_quotes(session, meu, [_quote("3000", dia=1, volta=28)])
    persistir_quotes(session, outro, [_quote("1200", dia=2, volta=28)])
    alvo = persistir_quotes(session, meu, [_quote("2900", dia=20, volta=28)])[0]

    base = baseline_comparavel(session, meu.id, alvo)

    assert (base.n, base.minimo) == (1, Decimal("3000"))


def test_executar_varredura_persiste_por_watch(session):
    # ida_volta=False: o _quote() daqui não seta data_volta — sem isso,
    # atende_criterios (integrado em executar_varredura) descartaria as
    # quotes pelo default ida_volta=True do Watch (este teste cobre o fluxo
    # de persistência, não critérios de ida/volta).
    w1 = _watch(session, ida_volta=False)
    w2 = _watch(session, nome="GRU -> MAD", destino_iata="MAD", ida_volta=False)
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
    # ida_volta=False: ver comentário em test_executar_varredura_persiste_por_watch
    w = _watch(session, ida_volta=False)
    bom = FakeProvider([_quote("3100")])

    resultado = executar_varredura(session, providers=[ExplodingProvider(), bom])
    session.commit()

    assert resultado.snapshots_por_watch == {w.id: 1}


def test_varredura_gera_candidato_abaixo_do_limite(session):
    # ida_volta=False: ver comentário em test_executar_varredura_persiste_por_watch
    w = _watch(session, preco_limite=Decimal("3000"), ida_volta=False)
    provider = FakeProvider([_quote("2890")])

    resultado = executar_varredura(session, providers=[provider])
    session.commit()

    assert len(resultado.candidatos) == 1
    cand = resultado.candidatos[0]
    assert cand.watch_id == w.id
    assert cand.gatilho == "LIMITE"
    assert cand.quote.preco == Decimal("2890")
