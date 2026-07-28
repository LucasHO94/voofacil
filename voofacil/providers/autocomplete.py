"""Autocomplete de lugares (cidade/aeroporto) para os campos de origem/destino
do painel — resolve "digite o nome" em código IATA, estilo Skyscanner.

Usa o endpoint público de autocomplete da Travelpayouts (mesmo provedor já
usado como fonte primária do projeto), que não exige token: é o serviço de UI
deles, separado da Data API autenticada em voofacil/providers/travelpayouts.py.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

URL = "https://autocomplete.travelpayouts.com/places2"

TIMEOUT = httpx.Timeout(5.0)


class LocalEncontrado(dict):
    """Um resultado do autocomplete — dict com pelo menos code/name/type/country_name."""


def buscar_locais(termo: str, *, locale: str = "pt", limite: int = 8) -> list[LocalEncontrado]:
    """Busca cidades/aeroportos cujo nome ou código bate com `termo`.

    Retorna [] em erro de rede/timeout (autocomplete é um recurso auxiliar —
    nunca deve travar o formulário) ou se `termo` tiver menos de 2 caracteres.
    Prioriza match EXATO de código IATA (ex.: buscar "GRU" traz o aeroporto
    Guarulhos primeiro, mesmo que cidades mais "populares" com weight maior
    também batam por outro critério do índice da Travelpayouts); o resto vem
    ordenado por popularidade (`weight`) decrescente.
    """
    termo = termo.strip()
    if len(termo) < 2:
        return []

    try:
        resp = httpx.get(
            URL,
            params=[
                ("term", termo),
                ("locale", locale),
                ("types[]", "city"),
                ("types[]", "airport"),
            ],
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        dados = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Autocomplete de lugares falhou para %r: %s", termo, exc)
        return []

    if not isinstance(dados, list):
        return []

    termo_upper = termo.upper()

    def chave_ordenacao(d: dict) -> tuple[int, float]:
        exato = str(d.get("code", "")).upper() == termo_upper
        return (0 if exato else 1, -float(d.get("weight") or 0))

    dados.sort(key=chave_ordenacao)
    return [LocalEncontrado(d) for d in dados[:limite]]


def rotulo_local(local: LocalEncontrado) -> str:
    """Formata um resultado para exibição: 'GRU — Guarulhos Cumbica SP, Brasil'."""
    pais = local.get("country_name") or ""
    nome = local.get("name") or local.get("code") or "?"
    sufixo = " (qualquer aeroporto)" if local.get("type") == "city" else ""
    return f"{local.get('code', '?')} — {nome}{sufixo}, {pais}"
