"""Testes offline do autocomplete de lugares (respostas simuladas com respx)."""
import httpx
import pytest
import respx

from voofacil.providers.autocomplete import URL, buscar_locais, rotulo_local

RESULTADOS_SAO_PAULO = [
    {"type": "airport", "code": "GRU", "name": "Guarulhos Cumbica SP",
     "country_name": "Brasil", "weight": 3850},
    {"type": "city", "code": "SAO", "name": "São Paulo",
     "country_name": "Brasil", "weight": 6552},
    {"type": "airport", "code": "CGH", "name": "São Paulo Congonhas",
     "country_name": "Brasil", "weight": 426},
]

RESULTADOS_CODIGO_EXATO = [
    {"type": "city", "code": "TBS", "name": "Tbilisi",
     "country_name": "Geórgia", "weight": 258563},
    {"type": "airport", "code": "GRU", "name": "Guarulhos Cumbica SP",
     "country_name": "Brasil", "weight": 3850},
]


@respx.mock
def test_busca_retorna_locais_ordenados_por_peso():
    respx.get(URL).mock(return_value=httpx.Response(200, json=RESULTADOS_SAO_PAULO))
    resultados = buscar_locais("Sao Paulo")

    assert [r["code"] for r in resultados] == ["SAO", "GRU", "CGH"]


@respx.mock
def test_match_exato_de_codigo_vem_primeiro_mesmo_com_peso_menor():
    respx.get(URL).mock(return_value=httpx.Response(200, json=RESULTADOS_CODIGO_EXATO))
    resultados = buscar_locais("GRU")

    assert resultados[0]["code"] == "GRU"


@respx.mock
def test_respeita_o_limite():
    muitos = [
        {"type": "city", "code": f"C{i:02d}", "name": f"Cidade {i}",
         "country_name": "Brasil", "weight": i}
        for i in range(20)
    ]
    respx.get(URL).mock(return_value=httpx.Response(200, json=muitos))
    resultados = buscar_locais("cidade", limite=3)

    assert len(resultados) == 3


def test_termo_curto_nao_faz_requisicao():
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, json=[]))
        resultados = buscar_locais("g")
    assert resultados == []


@respx.mock
def test_erro_de_rede_retorna_lista_vazia():
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
    resultados = buscar_locais("Lisboa")
    assert resultados == []


@respx.mock
def test_resposta_em_formato_inesperado_retorna_lista_vazia():
    respx.get(URL).mock(return_value=httpx.Response(200, json={"nao": "e uma lista"}))
    resultados = buscar_locais("Lisboa")
    assert resultados == []


def test_rotulo_local_aeroporto():
    local = {"type": "airport", "code": "GRU", "name": "Guarulhos Cumbica SP",
             "country_name": "Brasil"}
    assert rotulo_local(local) == "GRU — Guarulhos Cumbica SP, Brasil"


def test_rotulo_local_cidade_indica_qualquer_aeroporto():
    local = {"type": "city", "code": "SAO", "name": "São Paulo", "country_name": "Brasil"}
    assert rotulo_local(local) == "SAO — São Paulo (qualquer aeroporto), Brasil"
