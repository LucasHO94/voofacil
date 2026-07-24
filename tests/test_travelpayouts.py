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

    # 3 destinos x 2 meses (set, out) = 6 chamadas
    assert rota.call_count == 6


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
