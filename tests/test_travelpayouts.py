"""Testes offline do cliente Travelpayouts (respostas simuladas com respx)."""
import json
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from voofacil.models import TipoWatch, Watch
from voofacil.providers.travelpayouts import (
    BASE_URL,
    TravelpayoutsProvider,
    _meses_da_janela,
    deep_link_aviasales,
)


@pytest.fixture()
def tp():
    return TravelpayoutsProvider(token="token-teste", client=httpx.Client(base_url=BASE_URL))


CALENDAR_BODY = {
    "success": True,
    "currency": "brl",
    "data": {
        "2026-09-10": {
            "origin": "GRU",
            "destination": "LIS",
            "price": 3123,
            "transfers": 1,
            "airline": "TP",
            "departure_at": "2026-09-10T22:15:00Z",
            "return_at": "2026-09-24T14:00:00Z",
            "expires_at": "2026-07-26T00:00:00Z",
        },
        "2026-09-15": {
            "origin": "GRU",
            "destination": "LIS",
            "price": 2890,
            "transfers": 0,
            "airline": "LA",
            "departure_at": "2026-09-15T09:30:00Z",
            "expires_at": "2026-07-25T12:00:00Z",
        },
    },
}


@respx.mock
def test_calendar_normaliza_para_quotes(tp):
    respx.get(f"{BASE_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json=CALENDAR_BODY)
    )
    quotes = tp.calendar("GRU", "LIS", "2026-09")

    assert len(quotes) == 2
    mais_barato = min(quotes, key=lambda q: q.preco)
    assert mais_barato.preco == Decimal("2890")
    assert mais_barato.cia == "LA"
    assert mais_barato.escalas == 0
    assert mais_barato.fonte == "TP_CACHE"
    assert mais_barato.data_ida == date(2026, 9, 15)
    assert mais_barato.data_volta is None
    assert "aviasales.com/search/GRU1509LIS" in mais_barato.deep_link


@respx.mock
def test_calendar_ignora_itens_sem_preco(tp):
    corpo = {"success": True, "data": {"2026-09-01": {"price": 0}, "2026-09-02": None}}
    respx.get(f"{BASE_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json=corpo)
    )
    assert tp.calendar("GRU", "LIS", "2026-09") == []


@respx.mock
def test_month_matrix_formato_v2_lista(tp):
    corpo = {
        "success": True,
        "data": [
            {
                "depart_date": "2026-09-03",
                "origin": "GRU",
                "destination": "MAD",
                "value": 3410,
                "number_of_changes": 1,
                "return_date": "2026-09-17",
            }
        ],
    }
    respx.get(f"{BASE_URL}/v2/prices/month-matrix").mock(
        return_value=httpx.Response(200, json=corpo)
    )
    quotes = tp.month_matrix("GRU", "MAD", "2026-09")

    assert len(quotes) == 1
    assert quotes[0].preco == Decimal("3410")
    assert quotes[0].escalas == 1
    assert quotes[0].data_volta == date(2026, 9, 17)


@respx.mock
def test_retry_em_429_respeita_retry_after(tp, monkeypatch):
    esperas = []
    monkeypatch.setattr("voofacil.providers.travelpayouts.time.sleep", esperas.append)
    rota = respx.get(f"{BASE_URL}/v1/prices/calendar")
    rota.side_effect = [
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, json=CALENDAR_BODY),
    ]
    quotes = tp.calendar("GRU", "LIS", "2026-09")

    assert len(quotes) == 2
    assert esperas == [3]


@respx.mock
def test_sweep_filtra_janela_do_watch(tp):
    respx.get(f"{BASE_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json=CALENDAR_BODY)
    )
    watch = Watch(
        id=1,
        nome="teste",
        tipo=TipoWatch.ROTA.value,
        origem_iata="GRU",
        destino_iata="LIS",
        data_ini=date(2026, 9, 12),   # exclui o voo do dia 10
        data_fim=date(2026, 9, 30),
    )
    quotes = tp.sweep(watch)

    assert [q.data_ida for q in quotes] == [date(2026, 9, 15)]


@respx.mock
def test_sweep_regiao_varre_todos_destinos(tp):
    rota = respx.get(f"{BASE_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json=CALENDAR_BODY)
    )
    watch = Watch(
        id=2,
        nome="GRU -> Iberia",
        tipo=TipoWatch.REGIAO.value,
        origem_iata="GRU",
        destino_iata=None,
        regiao_destinos=json.dumps(["LIS", "MAD", "OPO"]),
        data_ini=date(2026, 9, 1),
        data_fim=date(2026, 10, 31),
    )
    tp.sweep(watch)

    # 3 destinos x 1 chamada base = 3 chamadas.
    # Este teste exigia 6 (3 destinos x 2 meses da janela) e assim CONGELAVA um
    # desperdício: medido contra a API real em 29/07/2026, `depart_date` é
    # ignorado — pedir 2026-09 e 2026-10 devolve o MESMO corpo, com as mesmas
    # chaves. As 3 chamadas extras eram HTTP puro, sem nenhum dado novo.
    assert rota.call_count == 3
    # e nenhuma delas manda depart_date (o parâmetro que a API ignora)
    assert all("depart_date" not in c.request.url.params for c in rota.calls)


# ---------- ida-e-volta de verdade (defeito "coleta zerada") ----------
#
# Fixtures abaixo copiam o FORMATO REAL da resposta de produção medida em
# 29/07/2026 (RIO->FCO, token real): a API normaliza os códigos para nível
# cidade ("destination": "ROM" mesmo quando se pede FCO) e preenche return_at.


def _corpo(*itens: dict) -> dict:
    """{'YYYY-MM-DD': item} no formato do /v1/prices/calendar."""
    return {
        "success": True,
        "currency": "brl",
        "data": {i["departure_at"][:10]: i for i in itens},
    }


def _item(ida: str, volta: str | None, preco: int, **kw) -> dict:
    item = {
        "origin": "RIO",
        "destination": "NAP",
        "airline": kw.get("airline", "TP"),
        "departure_at": f"{ida}T23:00:00-03:00",
        "expires_at": "2026-07-30T20:00:00Z",
        "price": preco,
        "transfers": kw.get("transfers", 1),
    }
    if volta:
        item["return_at"] = f"{volta}T05:55:00+02:00"
    return item


def _watch_real() -> Watch:
    """O watch REAL do usuário: RIO -> NAP, embarque exato 07/05/2027,
    desembarque exato 22/05/2027, ida-e-volta, teto R$ 3.500.

    O painel grava data_ini = data_fim = <data exata> quando tipo_data_ida é
    EXATA (ver Painel.py), por isso a janela de embarque é de um dia só.
    """
    return Watch(
        id=2,
        nome="RIO -> NAP 07/05-22/05/2027",
        tipo=TipoWatch.ROTA.value,
        origem_iata="RIO",
        destino_iata="NAP",
        data_ini=date(2027, 5, 7),
        data_fim=date(2027, 5, 7),
        tipo_data_ida="EXATA",
        tipo_data_volta="EXATA",
        data_volta_ini=date(2027, 5, 22),
        data_volta_fim=None,
        ida_volta=True,
        duracao_min=None,
        duracao_max=None,
        preco_limite=Decimal("3500"),
    )


@respx.mock
def test_calendar_envia_return_date_e_length(tp):
    rota = respx.get(f"{BASE_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json=CALENDAR_BODY)
    )
    tp.calendar("RIO", "NAP", return_date="2027-05", length=15)

    params = rota.calls.last.request.url.params
    assert params["return_date"] == "2027-05"
    assert params["length"] == "15"
    assert params["calendar_type"] == "departure_date"
    assert "depart_date" not in params


@respx.mock
def test_calendar_omite_depart_date_quando_mes_e_none(tp):
    rota = respx.get(f"{BASE_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json=CALENDAR_BODY)
    )
    tp.calendar("RIO", "NAP")
    assert "depart_date" not in rota.calls.last.request.url.params

    tp.calendar("RIO", "NAP", "2027-05")
    assert rota.calls.last.request.url.params["depart_date"] == "2027-05"


@respx.mock
def test_sweep_ida_volta_pede_consultas_dirigidas(tp):
    """O watch de datas exatas não preenche duracao_min/max, mas a duração
    ESTÁ determinada (07/05 -> 22/05 = 15 noites): o sweep precisa pedir
    length=15 e return_date=2027-05, os dois únicos parâmetros de ida-e-volta
    que a API respeita."""
    rota = respx.get(f"{BASE_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {}})
    )
    tp.sweep(_watch_real())

    consultas = [dict(c.request.url.params) for c in rota.calls]
    assert len(consultas) == 3                       # base + length + return_date
    assert not any("length" in c or "return_date" in c for c in consultas[:1])
    assert consultas[1]["length"] == "15"
    assert consultas[2]["return_date"] == "2027-05"


@respx.mock
def test_sweep_watch_real_produz_cotacao_ida_e_volta(tp):
    """PROVA do defeito "coleta zerada", reproduzindo o cenário da produção.

    A consulta CRUA (a única que existia) devolve, para o dia 07/05/2027, um
    item SEM return_at — exatamente o que a auditoria mediu ("1 item na janela,
    ida=2027-05-07 volta=None") e o que `atende_criterios` reprovava, zerando a
    coleta. As consultas DIRIGIDAS (length=15 / return_date=2027-05), que o
    sweep agora dispara, trazem o mesmo dia COM a volta que o watch pede.
    """
    from voofacil.engine.filtros import atende_criterios

    SO_IDA = _corpo(_item("2027-05-07", None, 2400))
    IDA_E_VOLTA = _corpo(
        _item("2027-05-07", "2027-05-22", 3211, airline="AZ"),
        _item("2027-05-15", "2027-05-30", 4100),   # fora da janela de embarque
    )

    def responder(request):
        dirigida = "length" in request.url.params or "return_date" in request.url.params
        return httpx.Response(200, json=IDA_E_VOLTA if dirigida else SO_IDA)

    respx.get(f"{BASE_URL}/v1/prices/calendar").mock(side_effect=responder)
    watch = _watch_real()

    # ANTES: só a consulta crua -> a única cotação da janela não tem volta.
    crua = [q for q in tp.calendar("RIO", "NAP") if q.data_ida == date(2027, 5, 7)]
    assert [q.data_volta for q in crua] == [None]
    assert [q for q in crua if atende_criterios(watch, q)] == []

    # DEPOIS: o sweep completo.
    uteis = [q for q in tp.sweep(watch) if atende_criterios(watch, q)]
    assert len(uteis) == 1
    q = uteis[0]
    assert (q.origem, q.destino) == ("RIO", "NAP")   # não "ROM"/código de cidade
    assert q.data_ida == date(2027, 5, 7)
    assert q.data_volta == date(2027, 5, 22)
    assert q.preco == Decimal("3211")
    assert q.fonte == "TP_CACHE"


@respx.mock
def test_sweep_deduplica_itens_repetidos_entre_consultas(tp):
    """base + length + return_date devolvem conjuntos que se sobrepõem — o
    mesmo itinerário não pode virar 3 snapshots na mesma varredura."""
    respx.get(f"{BASE_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json=_corpo(
            _item("2027-05-07", "2027-05-22", 3211),
        ))
    )
    quotes = tp.sweep(_watch_real())
    assert len(quotes) == 1


@respx.mock
def test_sweep_aproximada_nao_descarta_datas_vizinhas(tp):
    """Com tipo_data_ida=APROXIMADA o painel grava data_ini=data_fim=<alvo> e
    põe a tolerância em flexibilidade_dias; recortar por [data_ini, data_fim]
    cru descartaria dentro do provider justamente o que o watch aceita."""
    respx.get(f"{BASE_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json=_corpo(
            _item("2027-05-09", "2027-05-24", 3050),   # +2 dias do alvo
            _item("2027-05-13", "2027-05-28", 2900),   # +6 dias: fora da tolerância
        ))
    )
    watch = _watch_real()
    watch.tipo_data_ida = "APROXIMADA"
    watch.tipo_data_volta = "APROXIMADA"
    watch.flexibilidade_dias = 3

    quotes = tp.sweep(watch)
    assert [q.data_ida for q in quotes] == [date(2027, 5, 9)]


@respx.mock
def test_item_usa_os_codigos_pedidos_e_nao_os_da_resposta(tp):
    """A API responde com código de CIDADE (pedir GRU->LIS devolve
    origin='SAO'; pedir RIO->FCO devolve destination='ROM'). Gravar isso faria
    o histórico do watch aparecer com outra rota no painel."""
    corpo = _corpo(_item("2026-09-12", "2026-09-18", 5898))
    corpo["data"]["2026-09-12"]["origin"] = "SAO"
    corpo["data"]["2026-09-12"]["destination"] = "ROM"
    respx.get(f"{BASE_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json=corpo)
    )
    quotes = tp.calendar("GRU", "FCO", "2026-09")

    assert (quotes[0].origem, quotes[0].destino) == ("GRU", "FCO")


@respx.mock
def test_calendar_ignora_volta_anterior_a_ida(tp):
    """Cache inconsistente não pode virar uma 'estadia negativa'."""
    respx.get(f"{BASE_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json=_corpo(
            _item("2027-05-07", "2027-05-01", 3000),
        ))
    )
    assert tp.calendar("RIO", "NAP")[0].data_volta is None


def test_meses_da_janela_vira_ano():
    assert _meses_da_janela(date(2026, 11, 15), date(2027, 1, 10)) == [
        "2026-11", "2026-12", "2027-01",
    ]


def test_deep_link_ida_e_volta():
    link = deep_link_aviasales("GRU", "LIS", date(2026, 9, 10), date(2026, 9, 24))
    assert link == "https://www.aviasales.com/search/GRU1009LIS24091"


def test_deep_link_so_ida():
    link = deep_link_aviasales("GRU", "REC", date(2026, 8, 5), None)
    assert link == "https://www.aviasales.com/search/GRU0508REC1"
