"""Testes offline do cliente SerpApi (google_flights + google_travel_explore)."""
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from voofacil.engine.orcamento import consumo_atual, registrar_chamada
from voofacil.models import Watch
from voofacil.providers.serpapi_google import BASE_URL, SerpApiError, SerpApiProvider


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


# ---------- sweep() — varredura multi-fonte (Travelpayouts + SerpApi) ----------


def _watch_rota(**kw) -> Watch:
    """Watch ROTA transiente (não persistido — sweep() só lê atributos simples)."""
    base = dict(
        id=1,
        nome="GRU -> LIS",
        tipo="ROTA",
        origem_iata="GRU",
        destino_iata="LIS",
        data_ini=date(2026, 9, 1),
        data_fim=date(2026, 9, 30),
        tipo_data_ida="INTERVALO",
        tipo_data_volta="INTERVALO",
        flexibilidade_dias=None,
        data_volta_ini=None,
        data_volta_fim=None,
        mes_especifico=None,
        ida_volta=True,
        duracao_min=None,
        duracao_max=None,
    )
    base.update(kw)
    return Watch(**base)


def test_sweep_ignora_watch_tipo_regiao(sp):
    w = _watch_rota(tipo="REGIAO", destino_iata=None)
    assert sp.sweep(w) == []


def test_sweep_sem_api_key_retorna_vazio_sem_chamar_rede():
    sp = SerpApiProvider(api_key="")
    w = _watch_rota()
    assert sp.sweep(w) == []   # não deve nem tentar abrir sessão/rede


@respx.mock
def test_sweep_intervalo_usa_meio_da_janela_de_embarque(sp, session):
    rota = respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=GOOGLE_FLIGHTS_BODY))
    w = _watch_rota(
        tipo_data_ida="INTERVALO", data_ini=date(2026, 9, 1), data_fim=date(2026, 9, 30),
    )

    quotes = sp.sweep(w, session=session)

    assert len(quotes) == 1
    params = rota.calls.last.request.url.params
    assert params["outbound_date"] == "2026-09-15"   # meio de [01/09, 30/09]
    assert params["type"] == "1"                       # ida_volta=True (default)
    assert "return_date" in params


@respx.mock
def test_sweep_exata_usa_a_data_alvo_do_watch(sp, session):
    rota = respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=GOOGLE_FLIGHTS_BODY))
    w = _watch_rota(
        tipo_data_ida="EXATA", data_ini=date(2026, 9, 10), data_fim=date(2026, 9, 30),
    )

    sp.sweep(w, session=session)

    assert rota.calls.last.request.url.params["outbound_date"] == "2026-09-10"


@respx.mock
def test_sweep_somente_ida_nao_envia_data_de_volta(sp, session):
    rota = respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=GOOGLE_FLIGHTS_BODY))
    w = _watch_rota(ida_volta=False)

    sp.sweep(w, session=session)

    params = rota.calls.last.request.url.params
    assert params["type"] == "2"
    assert "return_date" not in params


@respx.mock
def test_sweep_usa_janela_propria_de_desembarque_quando_definida(sp, session):
    rota = respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=GOOGLE_FLIGHTS_BODY))
    w = _watch_rota(
        tipo_data_volta="EXATA",
        data_volta_ini=date(2026, 9, 24),
    )

    sp.sweep(w, session=session)

    assert rota.calls.last.request.url.params["return_date"] == "2026-09-24"


@respx.mock
def test_sweep_sem_janela_de_volta_deriva_da_duracao_min(sp, session):
    rota = respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=GOOGLE_FLIGHTS_BODY))
    w = _watch_rota(tipo_data_ida="EXATA", data_ini=date(2026, 9, 10), duracao_min=12)

    sp.sweep(w, session=session)

    assert rota.calls.last.request.url.params["return_date"] == "2026-09-22"   # 10/09 + 12


@respx.mock
def test_sweep_registra_chamada_no_orcamento(sp, session):
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=GOOGLE_FLIGHTS_BODY))
    w = _watch_rota()

    sp.sweep(w, session=session)

    assert consumo_atual(session, "SERPAPI") == 1


@respx.mock
def test_sweep_respeita_orcamento_esgotado(sp, session, monkeypatch):
    from voofacil.config import settings as cfg

    monkeypatch.setattr(cfg, "serpapi_orcamento_mes", 3)
    for _ in range(3):
        registrar_chamada(session, "SERPAPI")
    w = _watch_rota()

    # nenhuma rota respx registrada -- se o código tentar chamar a rede,
    # respx (modo strict, default) levanta erro de "rota não mockada" e o
    # teste falha, provando que o orçamento barrou a chamada antes da rede.
    quotes = sp.sweep(w, session=session)

    assert quotes == []
    assert consumo_atual(session, "SERPAPI") == 3   # não incrementou


@respx.mock
def test_sweep_nao_registra_chamada_quando_api_falha(sp, session):
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, json={"error": "Invalid API key."}))
    w = _watch_rota()

    quotes = sp.sweep(w, session=session)

    assert quotes == []
    assert consumo_atual(session, "SERPAPI") == 0


def test_sweep_cria_sessao_propria_quando_nenhuma_e_passada(sp, monkeypatch):
    """session=None (contrato genérico Provider.sweep(watch)) não deve tocar
    no banco real -- aqui trocamos voofacil.db.SessionLocal por um sqlite em
    memória antes de chamar, e destravamos a rede via respx manualmente."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import voofacil.db as db_module
    from voofacil.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    FakeSessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", FakeSessionLocal)

    with respx.mock:
        respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=GOOGLE_FLIGHTS_BODY))
        w = _watch_rota()
        quotes = sp.sweep(w)   # session=None

    assert len(quotes) == 1
    with FakeSessionLocal() as verificacao:
        assert consumo_atual(verificacao, "SERPAPI") == 1
