"""Testes do itinerário OPEN-JAW (multi-cidade) e do tamanho do grupo.

Open-jaw é a viagem em que a volta parte de uma cidade diferente do destino
da ida — ex.: voa RIO->NAP, atravessa a Itália por terra e volta VCE->RIO.
O caso que motivou o recurso é justamente esse, com 4 passageiros e teto de
R$ 3.500 por pessoa.

O que precisa valer, e é o que cada bloco abaixo cobre:
- o modelo distingue "volta do próprio destino" de "volta de outra cidade";
- a SerpApi cota o itinerário REAL (type=3 + multi_city_json), não o redondo;
- a Travelpayouts, que não sabe cotar multi-cidade, sai do caminho em vez de
  gravar snapshot de uma viagem que o usuário não vai comprar;
- o filtro rejeita cotação redonda para watch open-jaw (e vice-versa);
- painel e alerta mostram a rota inteira e o custo do grupo.
"""
import json
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from voofacil.alerts.mensagem import montar_mensagem
from voofacil.engine.filtros import atende_criterios
from voofacil.engine.queries import atualizar_watch, criar_watch_regiao, criar_watch_rota
from voofacil.models import Watch
from voofacil.providers.serpapi_google import BASE_URL, SerpApiProvider
from voofacil.providers.travelpayouts import TravelpayoutsProvider
from voofacil.schemas import Candidato, PriceQuote

IDA = date(2027, 5, 7)
VOLTA = date(2027, 5, 22)


@pytest.fixture()
def sp():
    return SerpApiProvider(api_key="chave-teste", client=httpx.Client())


def _watch(**kw) -> Watch:
    """Watch do print do usuário: RIO->NAP na ida, VCE->RIO na volta."""
    base = dict(
        id=1,
        nome="RIO -> NAP (volta VCE)",
        tipo="ROTA",
        origem_iata="RIO",
        destino_iata="NAP",
        origem_volta_iata="VCE",
        passageiros=4,
        data_ini=IDA,
        data_fim=IDA,
        data_volta_ini=VOLTA,
        data_volta_fim=VOLTA,
        tipo_data_ida="EXATA",
        tipo_data_volta="EXATA",
        flexibilidade_dias=None,
        mes_especifico=None,
        ida_volta=True,
        duracao_min=None,
        duracao_max=None,
        preco_limite=Decimal("3500"),
        preco_minimo=None,
    )
    base.update(kw)
    return Watch(**base)


def _quote(**kw) -> PriceQuote:
    base = dict(
        origem="RIO", destino="NAP", data_ida=IDA, data_volta=VOLTA,
        origem_volta="VCE", preco=Decimal("3200"), fonte="SERPAPI",
    )
    base.update(kw)
    return PriceQuote(**base)


# ---------- modelo ----------


def test_open_jaw_verdadeiro_quando_volta_parte_de_outra_cidade():
    assert _watch().open_jaw is True


def test_open_jaw_falso_quando_volta_parte_do_proprio_destino():
    """Preencher origem_volta_iata com o próprio destino é só outra forma de
    escrever "viagem redonda" — não pode ligar o modo multi-cidade."""
    assert _watch(origem_volta_iata="NAP").open_jaw is False
    assert _watch(origem_volta_iata=None).open_jaw is False


def test_total_passageiros_trata_null_como_um():
    """As linhas que já existiam em produção ganham a coluna sem valor (a
    micro-migração não faz backfill), e NULL não pode virar multiplicador zero."""
    assert _watch(passageiros=None).total_passageiros == 1
    assert _watch(passageiros=4).total_passageiros == 4


# ---------- filtro ----------


def test_filtro_aceita_cotacao_com_a_volta_da_cidade_certa():
    assert atende_criterios(_watch(), _quote()) is True


def test_filtro_rejeita_cotacao_redonda_para_watch_open_jaw():
    """O ponto central: sem isto, a cotação redonda NAP->RIO (mais fácil de
    achar e normalmente mais barata) passaria, e o alerta anunciaria o preço
    de uma viagem que não é a que o usuário vai comprar."""
    assert atende_criterios(_watch(), _quote(origem_volta=None)) is False


def test_filtro_rejeita_cotacao_com_volta_de_terceira_cidade():
    assert atende_criterios(_watch(), _quote(origem_volta="FCO")) is False


def test_filtro_rejeita_cotacao_open_jaw_para_watch_redondo():
    watch_redondo = _watch(origem_volta_iata=None)
    assert atende_criterios(watch_redondo, _quote(origem_volta="VCE")) is False


def test_filtro_trata_volta_do_destino_como_redonda_nos_dois_lados():
    """Provider que preenche origem_volta com o próprio destino não pode ser
    reprovado por um watch redondo (que guarda None) — as duas grafias
    significam a mesma coisa."""
    watch_redondo = _watch(origem_volta_iata=None)
    assert atende_criterios(watch_redondo, _quote(origem_volta="NAP")) is True


def test_filtro_ignora_origem_da_volta_em_watch_so_ida():
    watch_ida = _watch(origem_volta_iata=None, ida_volta=False,
                       data_volta_ini=None, data_volta_fim=None)
    assert atende_criterios(watch_ida, _quote(data_volta=None, origem_volta=None)) is True


# ---------- SerpApi: type=3 + multi_city_json ----------


CORPO_MULTI_CITY = {
    "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?x=1"},
    "best_flights": [
        {"price": 3200, "flights": [{"airline": "TAP"}, {"airline": "TAP"}]},
        {"price": 4100, "flights": [{"airline": "LATAM"}]},
    ],
}


@respx.mock
def test_confirmar_open_jaw_monta_multi_city_json(sp):
    rota = respx.get(BASE_URL).mock(
        return_value=httpx.Response(200, json=CORPO_MULTI_CITY)
    )

    quote = sp.confirmar_google_flights(
        "RIO", "NAP", IDA, VOLTA, origem_volta="VCE",
    )

    enviado = dict(rota.calls.last.request.url.params)
    assert enviado["type"] == "3"
    trechos = json.loads(enviado["multi_city_json"])
    # RIO é código de cidade: precisa sair expandido nos aeroportos reais,
    # senão o Google devolve zero voos (ver CIDADES_MULTI_AEROPORTO).
    assert trechos == [
        {"departure_id": "GIG,SDU", "arrival_id": "NAP", "date": "2027-05-07"},
        {"departure_id": "VCE", "arrival_id": "GIG,SDU", "date": "2027-05-22"},
    ]
    # Os parâmetros do modo redondo não podem vazar junto — a SerpApi ignora,
    # mas mandá-los indica confusão de modo.
    assert "outbound_date" not in enviado
    assert "return_date" not in enviado
    assert quote.origem_volta == "VCE"
    assert quote.preco == Decimal("3200")


@respx.mock
def test_confirmar_sem_open_jaw_mantem_modo_redondo(sp):
    rota = respx.get(BASE_URL).mock(
        return_value=httpx.Response(200, json=CORPO_MULTI_CITY)
    )

    quote = sp.confirmar_google_flights("GRU", "LIS", IDA, VOLTA)

    enviado = dict(rota.calls.last.request.url.params)
    assert enviado["type"] == "1"
    assert enviado["outbound_date"] == "2027-05-07"
    assert enviado["return_date"] == "2027-05-22"
    assert "multi_city_json" not in enviado
    assert quote.origem_volta is None


@respx.mock
def test_escalas_none_em_multi_city_sem_layovers(sp):
    """Em multi_city_json a lista `flights` concatena os DOIS trechos, então
    "pernas - 1" contaria a emenda entre eles como conexão: um open-jaw direto
    nos dois trechos sairia como "1 escala". Melhor não afirmar nada."""
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=CORPO_MULTI_CITY))

    quote = sp.confirmar_google_flights("RIO", "NAP", IDA, VOLTA, origem_volta="VCE")

    assert quote.escalas is None


@respx.mock
def test_escalas_usa_layovers_quando_a_api_informa(sp):
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, json={
        "best_flights": [
            {"price": 3200, "flights": [{"airline": "TAP"}], "layovers": [{"name": "LIS"}]},
        ],
    }))

    quote = sp.confirmar_google_flights("RIO", "NAP", IDA, VOLTA, origem_volta="VCE")

    assert quote.escalas == 1


# ---------- Travelpayouts sai do caminho ----------


def test_travelpayouts_pula_watch_open_jaw():
    """A API de calendar cota origem<->destino e não tem parâmetro para "a
    volta parte de outra cidade". Cotar o redondo assim mesmo contaminaria o
    baseline com preços de outro itinerário."""
    chamou = []

    class TPEspiao(TravelpayoutsProvider):
        def calendar(self, *a, **kw):
            chamou.append((a, kw))
            return []

    tp = TPEspiao(token="tok", client=httpx.Client())

    assert tp.sweep(_watch()) == []
    assert chamou == []          # nenhuma chamada HTTP foi sequer montada


def test_travelpayouts_continua_varrendo_watch_redondo():
    chamou = []

    class TPEspiao(TravelpayoutsProvider):
        def calendar(self, *a, **kw):
            chamou.append((a, kw))
            return []

    tp = TPEspiao(token="tok", client=httpx.Client())
    tp.sweep(_watch(origem_volta_iata=None))

    assert chamou, "watch redondo precisa continuar sendo varrido pela Travelpayouts"


# ---------- queries ----------


def test_criar_watch_rota_com_open_jaw(session):
    w = criar_watch_rota(
        session, "RIO -> NAP (volta VCE)", "rio", "nap", IDA, VOLTA,
        Decimal("3500"), origem_volta="vce", passageiros=4,
    )
    assert w.origem_volta_iata == "VCE"
    assert w.passageiros == 4
    assert w.open_jaw is True


def test_criar_watch_rota_normaliza_volta_igual_ao_destino(session):
    w = criar_watch_rota(
        session, "RIO -> NAP", "RIO", "NAP", IDA, VOLTA,
        Decimal("3500"), origem_volta="NAP",
    )
    assert w.origem_volta_iata is None
    assert w.open_jaw is False


def test_criar_watch_rota_recusa_open_jaw_em_so_ida(session):
    with pytest.raises(ValueError, match="ida_volta"):
        criar_watch_rota(
            session, "RIO -> NAP", "RIO", "NAP", IDA, VOLTA,
            None, ida_volta=False, origem_volta="VCE",
        )


def test_criar_watch_recusa_passageiros_invalido(session):
    with pytest.raises(ValueError, match="passageiros"):
        criar_watch_rota(
            session, "x", "RIO", "NAP", IDA, VOLTA, None, passageiros=0,
        )
    with pytest.raises(ValueError, match="passageiros"):
        criar_watch_regiao(
            session, "x", "RIO", ["NAP"], IDA, VOLTA, None, passageiros=0,
        )


def test_atualizar_watch_define_e_limpa_open_jaw(session):
    w = criar_watch_rota(
        session, "RIO -> NAP", "RIO", "NAP", IDA, VOLTA, Decimal("3500"),
    )

    atualizar_watch(session, w.id, origem_volta="VCE", passageiros=4)
    assert w.origem_volta_iata == "VCE"
    assert w.passageiros == 4

    atualizar_watch(session, w.id, origem_volta=None)
    assert w.origem_volta_iata is None


def test_atualizar_watch_recusa_open_jaw_ao_virar_so_ida(session):
    """Sem esta guarda o watch violaria `ck_watch_origem_volta` no commit, e o
    painel mostraria um IntegrityError cru."""
    w = criar_watch_rota(
        session, "RIO -> NAP (volta VCE)", "RIO", "NAP", IDA, VOLTA,
        Decimal("3500"), origem_volta="VCE",
    )
    with pytest.raises(ValueError, match="ida_volta"):
        atualizar_watch(session, w.id, ida_volta=False)


def test_atualizar_watch_limpa_open_jaw_ao_virar_regiao(session):
    w = criar_watch_rota(
        session, "RIO -> NAP (volta VCE)", "RIO", "NAP", IDA, VOLTA,
        Decimal("3500"), origem_volta="VCE",
    )
    with pytest.raises(ValueError, match="REGIÃO"):
        atualizar_watch(session, w.id, destinos=["LIS", "MAD"])


# ---------- mensagem de alerta ----------


def _candidato(**kw) -> Candidato:
    base = dict(quote=_quote(), watch_id=1, snapshot_id=1, gatilho="LIMITE")
    base.update(kw)
    return Candidato(**base)


def test_mensagem_mostra_os_dois_trechos_do_open_jaw():
    msg = montar_mensagem(_candidato(), _watch())
    assert "RIO → NAP · volta VCE → RIO" in msg.html


def test_mensagem_mostra_total_do_grupo():
    """O preço cotado é por 1 pessoa; num grupo de 4 o número que decide a
    compra é o total."""
    msg = montar_mensagem(_candidato(), _watch())
    assert "4 passageiros" in msg.html
    assert "R$ 12.800,00" in msg.html          # 3200 x 4


def test_mensagem_omite_total_quando_viaja_uma_pessoa():
    msg = montar_mensagem(_candidato(), _watch(passageiros=1))
    assert "passageiros" not in msg.html


# ---------- a chave da SerpApi não pode ir para o log ----------


def test_silenciar_log_de_url_corta_o_request_line_do_httpx():
    """A SerpApi recebe a credencial na QUERY STRING, e o httpx registra cada
    requisição em INFO com a URL completa: com basicConfig(INFO) a chave saía
    em texto claro no log a cada varredura — em produção, no GitHub Actions de
    um repositório público."""
    import logging

    from voofacil.worker import _silenciar_log_de_url

    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.INFO)

    _silenciar_log_de_url()

    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)
    # erro de rede continua visível — é o que se quer depurar
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)


def test_mensagem_de_watch_redondo_nao_inventa_trecho():
    quote = _quote(origem_volta=None)
    # Nome limpo de propósito: a mensagem termina com "Monitoramento: <nome>",
    # e o nome default deste helper contém "(volta VCE)" — a asserção passaria
    # a medir o nome em vez da linha de rota.
    watch = _watch(origem_volta_iata=None, nome="RIO -> NAP")
    msg = montar_mensagem(_candidato(quote=quote), watch)
    assert "RIO → NAP" in msg.html
    assert "volta VCE" not in msg.html
