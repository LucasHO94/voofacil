"""Confirmação ao vivo dos candidatos aprovados pelo detector (via SerpApi).

A Travelpayouts entrega o volume (cache, defasagem até 48h); antes de
alertar, tentamos 1 chamada `google_flights` por candidato para pegar o
preço real do momento — evita alertar um preço que já subiu.

Se o orçamento mensal (free tier 250/mês) já foi consumido ou a chamada
falha, o candidato segue para o alerta MESMO ASSIM, mas marcado como
"não confirmado" (dispatcher avisa o usuário a checar no link).
"""
from __future__ import annotations

import logging

from farewatch.config import settings
from farewatch.engine.orcamento import ha_orcamento, registrar_chamada
from farewatch.providers.serpapi_google import SerpApiProvider
from farewatch.schemas import Candidato

log = logging.getLogger(__name__)


def confirmar_candidatos(
    session, candidatos: list[Candidato], provider: SerpApiProvider | None = None
) -> list[Candidato]:
    """Tenta confirmar cada candidato ao vivo; retorna a lista (mutada in-place é evitado — Pydantic é imutável por padrão, então reconstrói)."""
    provider = provider or SerpApiProvider()
    if not provider.configurado:
        log.info("SerpApi não configurada — alertas sairão sem confirmação ao vivo")
        return candidatos

    confirmados = []
    for cand in candidatos:
        if not ha_orcamento(session, "SERPAPI", settings.serpapi_orcamento_mes):
            log.warning("Orçamento SerpApi do mês esgotado — candidato %s segue sem confirmação",
                        cand.snapshot_id)
            confirmados.append(cand)
            continue
        try:
            quote_viva = provider.confirmar_google_flights(
                cand.quote.origem, cand.quote.destino,
                cand.quote.data_ida, cand.quote.data_volta,
            )
            registrar_chamada(session, "SERPAPI")
        except Exception as exc:  # noqa: BLE001 — degrada para "não confirmado", nunca quebra a varredura
            log.warning("Falha ao confirmar candidato %s: %s", cand.snapshot_id, exc)
            confirmados.append(cand)
            continue

        if quote_viva is None:
            confirmados.append(cand)
            continue

        confirmados.append(
            cand.model_copy(update={"quote": quote_viva, "confirmado_ao_vivo": True})
        )
    return confirmados
