"""Testes da integração do worker: filtro de critérios em executar_varredura
e a inclusão condicional da SerpApi na lista de providers da varredura
regular (multi-fonte "estilo Skyscanner")."""
from datetime import date
from decimal import Decimal

from voofacil.engine.orcamento import registrar_chamada
from voofacil.models import PriceSnapshot, TipoWatch, Watch
from voofacil.schemas import Candidato, PriceQuote
from voofacil.worker import (
    TETO_CANDIDATOS_POR_WATCH,
    _providers_padrao,
    deduplicar_candidatos,
    executar_varredura,
)


def _watch(session, **kw):
    base = dict(
        nome="GRU -> LIS",
        tipo=TipoWatch.ROTA.value,
        origem_iata="GRU",
        destino_iata="LIS",
        data_ini=date(2026, 9, 1),
        data_fim=date(2026, 9, 30),
        ida_volta=False,   # simplifica: quotes de teste não precisam de data_volta
    )
    base.update(kw)
    w = Watch(**base)
    session.add(w)
    session.flush()
    return w


def _quote(**kw) -> PriceQuote:
    base = dict(
        origem="GRU", destino="LIS", data_ida=date(2026, 9, 10),
        preco=Decimal("3000"), fonte="TP_CACHE",
    )
    base.update(kw)
    return PriceQuote(**base)


class FakeProvider:
    nome = "FAKE"

    def __init__(self, quotes):
        self.quotes = quotes

    def sweep(self, watch):
        return self.quotes


class FakeSerpApi:
    """Duplo simples do SerpApiProvider — só precisa satisfazer o Provider
    protocol (.nome + .sweep) e o atributo `.configurado` usado por
    _providers_padrao."""

    nome = "SERPAPI"

    def __init__(self, configurado: bool, quotes: list[PriceQuote] | None = None):
        self.configurado = configurado
        self.quotes = quotes or []
        self.chamadas = 0

    def sweep(self, watch):
        self.chamadas += 1
        return self.quotes


# ---------- filtro atende_criterios integrado em executar_varredura ----------


def test_executar_varredura_descarta_quote_que_nao_atende_criterios(session):
    w = _watch(session, mes_especifico=9)
    provider = FakeProvider([
        _quote(data_ida=date(2026, 9, 12)),    # setembro -- passa
        _quote(data_ida=date(2026, 10, 1)),    # outubro -- descartada
    ])

    resultado = executar_varredura(session, providers=[provider])
    session.commit()

    assert resultado.snapshots_por_watch == {w.id: 1}


def test_executar_varredura_aplica_criterio_de_data_exata(session):
    w = _watch(session, tipo_data_ida="EXATA", data_ini=date(2026, 9, 15))
    provider = FakeProvider([
        _quote(data_ida=date(2026, 9, 15)),
        _quote(data_ida=date(2026, 9, 16)),
    ])

    resultado = executar_varredura(session, providers=[provider])
    session.commit()

    assert resultado.snapshots_por_watch == {w.id: 1}


def test_executar_varredura_aplica_criterio_de_preco_minimo(session):
    w = _watch(session, preco_minimo=Decimal("2000"))
    provider = FakeProvider([
        _quote(preco=Decimal("2500")),
        _quote(preco=Decimal("1000")),
    ])

    resultado = executar_varredura(session, providers=[provider])
    session.commit()

    assert resultado.snapshots_por_watch == {w.id: 1}


def test_executar_varredura_sem_filtro_mantem_todas_as_quotes(session):
    """Watch sem nenhum critério avançado (defaults) não descarta nada."""
    w = _watch(session)
    provider = FakeProvider([
        _quote(data_ida=date(2026, 9, 5)),
        _quote(data_ida=date(2026, 9, 20)),
    ])

    resultado = executar_varredura(session, providers=[provider])
    session.commit()

    assert resultado.snapshots_por_watch == {w.id: 2}


# ---------- inclusão condicional da SerpApi na varredura regular ----------


def test_providers_padrao_sempre_inclui_travelpayouts(session):
    providers = _providers_padrao(session, serpapi=FakeSerpApi(configurado=False))
    assert len(providers) == 1
    assert providers[0].nome == "TP_CACHE"


def test_providers_padrao_inclui_serpapi_quando_configurada_e_ha_orcamento(session):
    fake_serpapi = FakeSerpApi(configurado=True)

    providers = _providers_padrao(session, serpapi=fake_serpapi)

    assert [p.nome for p in providers] == ["TP_CACHE", "SERPAPI"]
    assert providers[1] is fake_serpapi


def test_providers_padrao_exclui_serpapi_quando_nao_configurada(session):
    providers = _providers_padrao(session, serpapi=FakeSerpApi(configurado=False))
    assert [p.nome for p in providers] == ["TP_CACHE"]


def test_providers_padrao_exclui_serpapi_quando_orcamento_esgotado(session, monkeypatch):
    from voofacil.config import settings as cfg

    monkeypatch.setattr(cfg, "serpapi_orcamento_mes", 2)
    registrar_chamada(session, "SERPAPI")
    registrar_chamada(session, "SERPAPI")   # consumo == limite -> sem orçamento

    providers = _providers_padrao(session, serpapi=FakeSerpApi(configurado=True))

    assert [p.nome for p in providers] == ["TP_CACHE"]


def test_executar_varredura_usa_duas_fontes_quando_serpapi_esta_na_lista(session):
    """Prova o "estilo Skyscanner": quando _providers_padrao inclui a
    SerpApi (configurada + com orçamento), a MESMA varredura consulta duas
    fontes independentes para o watch -- Travelpayouts (cache) e SerpApi
    (ao vivo) lado a lado."""
    w = _watch(session)
    tp = FakeProvider([_quote(fonte="TP_CACHE", preco=Decimal("3000"))])
    fake_serpapi = FakeSerpApi(
        configurado=True, quotes=[_quote(fonte="SERPAPI", preco=Decimal("2950"))]
    )

    # _providers_padrao já é testado isoladamente acima quanto à decisão de
    # incluir/excluir a SerpApi; aqui simulamos o resultado dela (a
    # Travelpayouts real + a SerpApi condicionalmente incluída) e
    # verificamos que executar_varredura de fato usa as duas.
    providers = [tp, fake_serpapi]

    resultado = executar_varredura(session, providers=providers)
    session.commit()

    assert resultado.snapshots_por_watch == {w.id: 2}
    assert fake_serpapi.chamadas == 1
    fontes = {s.fonte for s in session.query(PriceSnapshot).filter_by(watch_id=w.id)}
    assert fontes == {"TP_CACHE", "SERPAPI"}


# ---------- deduplicação e teto de candidatos por varredura ----------


def _cand(preco="3000", destino="LIS", dia=10, volta=None, fonte="TP_CACHE",
          watch_id=1, snapshot_id=1, gatilho="LIMITE") -> Candidato:
    return Candidato(
        quote=_quote(
            destino=destino,
            data_ida=date(2026, 9, dia),
            data_volta=date(2026, 9, volta) if volta else None,
            preco=Decimal(preco),
            fonte=fonte,
        ),
        watch_id=watch_id,
        snapshot_id=snapshot_id,
        gatilho=gatilho,
    )


def test_deduplicar_colapsa_mesma_viagem_mantendo_a_mais_barata():
    """Sweep multi-fonte vê a MESMA rota/dia por TP_CACHE e por SERPAPI na
    mesma rodada — é um candidato só, o mais barato."""
    resultado = deduplicar_candidatos([
        _cand(preco="3000", fonte="TP_CACHE", snapshot_id=1),
        _cand(preco="2800", fonte="SERPAPI", snapshot_id=2),
    ])

    assert len(resultado) == 1
    assert resultado[0].quote.preco == Decimal("2800")
    assert resultado[0].snapshot_id == 2


def test_deduplicar_nao_junta_datas_de_volta_diferentes():
    resultado = deduplicar_candidatos([
        _cand(preco="3000", dia=10, volta=17, snapshot_id=1),
        _cand(preco="3100", dia=10, volta=24, snapshot_id=2),
    ])

    assert len(resultado) == 2


def test_deduplicar_aplica_teto_por_watch_mantendo_os_mais_baratos():
    candidatos = [
        _cand(preco=str(3000 + 10 * i), dia=i + 1, snapshot_id=i + 1)
        for i in range(10)
    ]

    resultado = deduplicar_candidatos(candidatos, teto_por_watch=3)

    assert [c.quote.preco for c in resultado] == [
        Decimal("3000"), Decimal("3010"), Decimal("3020"),
    ]


def test_deduplicar_conta_o_teto_por_watch_e_nao_globalmente():
    candidatos = (
        [_cand(preco=str(3000 + i), dia=i + 1, watch_id=1, snapshot_id=i + 1)
         for i in range(4)]
        + [_cand(preco=str(4000 + i), dia=i + 1, watch_id=2, snapshot_id=10 + i)
           for i in range(4)]
    )

    resultado = deduplicar_candidatos(candidatos, teto_por_watch=2)

    assert [c.watch_id for c in resultado] == [1, 1, 2, 2]


def test_varredura_de_um_mes_nao_gera_dez_candidatos(session):
    """Cenário exato da auditoria: watch com limite 3500 e 1 mês de calendar
    (30 cotações), banco vazio antes => 10 candidatos LIMITE numa ÚNICA
    varredura. Isso seriam 10 chamadas SerpApi (teto diário = 8) e 10
    mensagens POR CANAL."""
    w = _watch(session, preco_limite=Decimal("3500"))
    provider = FakeProvider([
        _quote(data_ida=date(2026, 9, i + 1), preco=Decimal(3000 + 55 * i))
        for i in range(30)
    ])   # 10 cotações <= 3500 (3000, 3055, ..., 3495)

    resultado = executar_varredura(session, providers=[provider])
    session.commit()

    assert session.query(PriceSnapshot).count() == 30
    assert resultado.snapshots_por_watch == {w.id: 30}
    assert len(resultado.candidatos) == TETO_CANDIDATOS_POR_WATCH
    assert [c.quote.preco for c in resultado.candidatos] == [
        Decimal("3000"), Decimal("3055"), Decimal("3110"),
    ]
