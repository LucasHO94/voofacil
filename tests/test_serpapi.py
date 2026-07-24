"""Testes offline do cliente SerpApi (google_flights + google_travel_explore)."""
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from farewatch.providers.serpapi_google import BASE_URL, SerpApiError, SerpApiProvider


@pytest.fixture()
def sp():
    return SerpApiProvider(api_key="chave-teste", client=httpx.Client())


GOOGLE_FLIGHTS_BODY = {
    "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?..."},
    "best_flights": [
        {
            "price": 2890,
            "flights": [
                {"airline": "LATAM"},
                {"airline": "LATAM"},
            ],
        },
        {"price": 3200, "flights": [{"airline": "TAP"}]},
    ],
}

EXPLORE_BODY = {
    "destinations": [
        {"airport_code": "LIS", "price": 3100, "date": "2026-09-12",
         "link": "https://www.google.com/travel/flights?d=LIS"},
        {"airport_code": "MAD", "price": 2950, "date": "2026-09-20",
         "link": "https://www.google.com/travel/flights?d=MAD"},
        {"airport_code": None, "price": 5000},   # sem IATA -> descartado
    ],
}


@respx.mock
def test_confirmar_google_flights_pega_mais_barato(sp):
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=GOOGLE_FLIGHTS_BODY))

    quote = sp.confirmar_google_flights("GRU", "LIS", date(2026, 9, 10), date(2026, 9, 24))

    assert quote is not None
    assert quote.preco == Decimal("2890")
    assert quote.cia == "LATAM"
    assert quote.escalas == 1
    assert quote.fonte == "SERPAPI"
    assert quote.deep_link.startswith("https://www.google.com")


@respx.mock
def test_confirmar_google_flights_sem_resultado(sp):
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, json={"best_flights": []}))
    assert sp.confirmar_google_flights("GRU", "XXX", date(2026, 9, 10)) is None


@respx.mock
def test_confirmar_google_flights_propaga_erro_api(sp):
    respx.get(BASE_URL).mock(
        return_value=httpx.Response(200, json={"error": "Invalid API key."})
    )
    with pytest.raises(SerpApiError):
        sp.confirmar_google_flights("GRU", "LIS", date(2026, 9, 10))


@respx.mock
def test_explorar_regiao_normaliza_destinos(sp):
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=EXPLORE_BODY))

    quotes = sp.explorar_regiao("GRU", "/m/02j9z", mes=9)

    assert len(quotes) == 2   # o item sem airport_code foi descartado
    precos = {q.destino: q.preco for q in quotes}
    assert precos == {"LIS": Decimal("3100"), "MAD": Decimal("2950")}
    assert all(q.fonte == "SERPAPI" for q in quotes)
    assert all(q.data_ida.month == 9 for q in quotes)


def test_sem_api_key_nao_configurado():
    sp = SerpApiProvider(api_key="")
    assert sp.configurado is False
    with pytest.raises(SerpApiError):
        sp.confirmar_google_flights("GRU", "LIS", date(2026, 9, 10))
