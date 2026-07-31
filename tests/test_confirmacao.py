"""Testes da confirmação ao vivo (SerpApi): revalidação e degradação graciosa."""
from datetime import date, timedelta
from decimal import Decimal

from voofacil.engine.confirmacao import confirmar_candidatos
from voofacil.engine.orcamento import registrar_chamada
from voofacil.engine.store import persistir_quotes
from voofacil.models import TipoWatch, Watch, utcnow
from voofacil.schemas import Candidato, PriceQuote


def _watch(session, **kw) -> Watch:
    base = dict(
        nome="GRU -> LIS",
        tipo=TipoWatch.ROTA.value,
        origem_iata="GRU",
        destino_iata="LIS",
        data_ini=date(2026, 9, 1),
        data_fim=date(2026, 9, 30),
        preco_limite=Decimal("3000"),
    )
    base.update(kw)
    w = Watch(**base)
    session.add(w)
    session.flush()
    return w


def _quote(preco: str, dia: int = 10, volta: int | None = 24,
           fonte: str = "TP_CACHE") -> PriceQuote:
    return PriceQuote(
        origem="GRU", destino="LIS", data_ida=date(2026, 9, dia),
        data_volta=date(2026, 9, volta) if volta else None,
        preco=Decimal(preco), fonte=fonte,
    )


def _candidato(session, watch, preco="2890.00", gatilho="LIMITE",
               volta: int | None = 24) -> Candidato:
    """Candidato ancorado num snapshot REAL do watch — a revalidação ao vivo
    precisa do watch e do snapshot para reaplicar critérios e gatilho."""
    quote = _quote(preco, volta=volta)
    snap = persistir_quotes(session, watch, [quote])[0]
    return Candidato(
        quote=quote, watch_id=watch.id, snapshot_id=snap.id, gatilho=gatilho
    )


def _historico(session, watch, precos: list[str]):
    """Histórico em RODADAS DE COLETA distintas (o warm-up conta dias, não
    linhas — ver Baseline.rodadas)."""
    snaps = persistir_quotes(
        session, watch, [_quote(p, dia=i + 1) for i, p in enumerate(precos)]
    )
    inicio = utcnow() - timedelta(days=len(snaps))
    for i, snap in enumerate(snaps):
        snap.coletado_em = inicio + timedelta(days=i)
    session.flush()
    return snaps


class FakeSerpApi:
    nome = "SERPAPI"
    configurado = True

    def __init__(self, preco_confirmado=None, explode=False):
        self.preco_confirmado = preco_confirmado
        self.explode = explode
        self.chamadas = 0

    def confirmar_google_flights(self, origem, destino, data_ida, data_volta=None,
                                 ao_cobrar=None, *, origem_volta=None):
        self.chamadas += 1
        self.ultima_origem_volta = origem_volta
        if self.explode:
            raise RuntimeError("falha simulada")
        if self.preco_confirmado is None:
            return None
        return PriceQuote(
            origem=origem, destino=destino, data_ida=data_ida, data_volta=data_volta,
            preco=Decimal(self.preco_confirmado), fonte="SERPAPI",
            deep_link="https://www.google.com/travel/flights?d=LIS",
        )


# ---------- confirmação que PASSA na revalidação ----------


def test_confirma_e_atualiza_preco_ao_vivo(session):
    w = _watch(session)                                   # limite 3000
    provider = FakeSerpApi(preco_confirmado="2790.00")
    resultado = confirmar_candidatos(session, [_candidato(session, w)], provider)

    assert len(resultado) == 1
    assert resultado[0].confirmado_ao_vivo is True
    assert resultado[0].quote.preco == Decimal("2790.00")
    assert resultado[0].quote.fonte == "SERPAPI"
    assert resultado[0].snapshot_id == 1   # mantém o vínculo com o snapshot original


def test_preco_vivo_que_subiu_mas_segue_sob_o_limite_e_confirmado(session):
    """A revalidação é sobre as REGRAS do watch, não sobre a direção do preço:
    2890 -> 2950 com limite 3000 continua sendo o que o usuário pediu."""
    w = _watch(session)                                   # limite 3000
    provider = FakeSerpApi(preco_confirmado="2950.00")
    resultado = confirmar_candidatos(session, [_candidato(session, w)], provider)

    assert len(resultado) == 1
    assert resultado[0].confirmado_ao_vivo is True
    assert resultado[0].quote.preco == Decimal("2950.00")


# ---------- confirmação que REPROVA: candidato descartado ----------


def test_preco_vivo_acima_do_limite_e_descartado(session):
    """O alerta que a auditoria produziu em produção: "GRU -> LIS por
    R$ 8.900,00 / OK Abaixo do seu limite / Seu limite: R$ 3.500,00 / Preço
    confirmado ao vivo agora". O preço vivo entrava por model_copy sem passar
    de novo pelo gatilho — agora o candidato morre aqui, e não vira alerta
    nem como "não confirmado" (o preço do cache já foi provado errado)."""
    w = _watch(session, preco_limite=Decimal("3500"))
    provider = FakeSerpApi(preco_confirmado="8900.00")

    resultado = confirmar_candidatos(session, [_candidato(session, w)], provider)

    assert resultado == []


def test_preco_vivo_que_viola_criterio_do_watch_e_descartado(session):
    """preco_minimo é critério de busca (piso), não gatilho: um preço vivo
    abaixo dele deixou de atender ao watch e não pode alertar."""
    w = _watch(session, preco_minimo=Decimal("2000"))
    provider = FakeSerpApi(preco_confirmado="900.00")

    resultado = confirmar_candidatos(session, [_candidato(session, w)], provider)

    assert resultado == []


def test_novo_minimo_que_deixa_de_valer_ao_vivo_e_descartado(session):
    """Não é só o LIMITE que é reavaliado: o gatilho estatístico também.
    Mínimo histórico 3150 (5 rodadas); cache viu 3000 (NOVO_MINIMO), mas ao
    vivo o voo está 3120 — acima de 3150*0.98=3087, ou seja, não é mínimo
    nenhum."""
    w = _watch(session, preco_limite=None)
    _historico(session, w, ["3300", "3200", "3400", "3250", "3150"])
    cand = _candidato(session, w, preco="3000.00", gatilho="NOVO_MINIMO")
    provider = FakeSerpApi(preco_confirmado="3120.00")

    resultado = confirmar_candidatos(session, [cand], provider)

    assert resultado == []


def test_gatilho_e_reavaliado_com_o_preco_vivo(session):
    """Se o preço vivo cruza uma camada mais grave que a do cache, o candidato
    sai com o gatilho REAL — é ele que descreve o preço enviado."""
    w = _watch(session)                                   # limite 3000
    _historico(session, w, ["3300", "3200", "3400", "3250", "3150"])   # média ~3260
    cand = _candidato(session, w, preco="2890.00", gatilho="LIMITE")
    provider = FakeSerpApi(preco_confirmado="1500.00")    # <= 50% da média

    resultado = confirmar_candidatos(session, [cand], provider)

    assert len(resultado) == 1
    assert resultado[0].gatilho == "ERROR_FARE"
    assert resultado[0].confirmado_ao_vivo is True


# ---------- degradação graciosa (INCERTEZA, não reprovação) ----------


def test_sem_resultado_ao_vivo_mantem_candidato_original(session):
    w = _watch(session)
    provider = FakeSerpApi(preco_confirmado=None)
    resultado = confirmar_candidatos(session, [_candidato(session, w)], provider)

    assert resultado[0].confirmado_ao_vivo is False
    assert resultado[0].quote.preco == Decimal("2890.00")   # preço do cache preservado


def test_falha_na_chamada_degrada_sem_quebrar(session):
    w = _watch(session)
    provider = FakeSerpApi(explode=True)
    resultado = confirmar_candidatos(session, [_candidato(session, w)], provider)

    assert resultado[0].confirmado_ao_vivo is False


def test_sem_provider_configurado_pula_confirmacao(session):
    class NaoConfigurado:
        nome = "SERPAPI"
        configurado = False

    w = _watch(session)
    resultado = confirmar_candidatos(session, [_candidato(session, w)], NaoConfigurado())
    assert resultado[0].confirmado_ao_vivo is False


def test_orcamento_esgotado_nao_chama_api(session):
    from datetime import date as date_cls
    hoje = date_cls.today()
    for _ in range(250):
        registrar_chamada(session, "SERPAPI", hoje)

    w = _watch(session)
    provider = FakeSerpApi(preco_confirmado="1.00")
    resultado = confirmar_candidatos(session, [_candidato(session, w)], provider)

    assert provider.chamadas == 0
    assert resultado[0].confirmado_ao_vivo is False
