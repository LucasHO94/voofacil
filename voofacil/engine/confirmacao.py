"""Confirmação ao vivo dos candidatos aprovados pelo detector (via SerpApi).

A Travelpayouts entrega o volume (cache, defasagem até 48h); antes de
alertar, tentamos 1 chamada `google_flights` por candidato para pegar o
preço real do momento — evita alertar um preço que já subiu.

REVALIDAÇÃO (o motivo de este módulo existir): pegar o preço vivo não serve
de nada se ele entrar no alerta sem passar de novo pelas regras do watch.
Era exatamente o que acontecia — o preço vivo substituía o do cache via
`model_copy` e o candidato seguia com o gatilho decidido lá atrás, sobre o
preço antigo. O alerta real que isso produziu:

    GRU -> LIS por R$ 8.900,00
    OK Abaixo do seu limite
    Seu limite: R$ 3.500,00
    Preço confirmado ao vivo agora

Agora, obtido o preço vivo, ele é submetido de novo a (1) `atende_criterios`
do watch e (2) `gatilho_para_preco`, contra o MESMO baseline comparável do
snapshot que originou o candidato. Se qualquer um dos dois reprovar, o
candidato é DESCARTADO — não rebaixado para "não confirmado" com o preço do
cache. O motivo da escolha: aqui nós SABEMOS que o preço real de agora não
serve; mandar o preço do cache seria alertar um número que acabamos de
provar errado, e o cooldown ainda passaria a proteger uma viagem que nunca
deveria ter sido alertada. "Não confirmado" continua existindo só para a
INCERTEZA (sem orçamento, chamada falhou, sem resultado), nunca para a
reprovação.

Se o orçamento mensal (free tier 250/mês) OU o teto diário (padrão 8/dia,
ver voofacil/engine/orcamento.py) já foi consumido, ou a chamada falha, o
candidato segue para o alerta MESMO ASSIM, mas marcado como "não
confirmado" (dispatcher avisa o usuário a checar no link).
"""
from __future__ import annotations

import logging

from voofacil.engine.baseline import baseline_comparavel
from voofacil.engine.detector import gatilho_para_preco
from voofacil.engine.filtros import atende_criterios, atende_criterios_descoberta
from voofacil.engine.orcamento import ha_orcamento_serpapi, registrar_chamada_serpapi
from voofacil.models import PriceSnapshot, Watch
from voofacil.providers.serpapi_google import SerpApiProvider
from voofacil.schemas import Candidato, PriceQuote

log = logging.getLogger(__name__)


def _criterios_ok(watch: Watch, quote_original: PriceQuote, quote_viva: PriceQuote) -> bool:
    """Reaplica os critérios do watch sobre a cotação viva.

    Candidato vindo de DESCOBERTA (Explore) nunca tem data_volta — nem a
    cotação original nem a confirmação, que é pedida com as mesmas datas.
    Para esses, exigir ida_volta/duração/janela de volta reprovaria 100% dos
    candidatos de qualquer watch com ida_volta=True (o default), zerando a
    busca ampla; aplica-se então a mesma versão permissiva usada na coleta
    (`atende_criterios_descoberta`). O que protege esses candidatos é a
    reavaliação do gatilho, que é onde o preco_limite entra.
    """
    if watch.ida_volta and quote_original.data_volta is None:
        return atende_criterios_descoberta(watch, quote_viva)
    return atende_criterios(watch, quote_viva)


def _revalidar_ao_vivo(
    session, cand: Candidato, quote_viva: PriceQuote
) -> tuple[Candidato | None, str]:
    """Submete o preço vivo às regras do watch de novo.

    Devolve (candidato_confirmado, motivo). `candidato_confirmado` é None
    quando o preço vivo não passa — o motivo vai para o log.
    """
    watch = session.get(Watch, cand.watch_id)
    if watch is None:
        # Não dá para revalidar sem o watch (não deveria acontecer: o
        # candidato acabou de ser gerado a partir dele). Mantém o candidato
        # como NÃO confirmado, com o preço do cache — é incerteza, não
        # reprovação.
        log.warning("Watch %s sumiu — candidato %s segue sem confirmação",
                    cand.watch_id, cand.snapshot_id)
        return cand, "watch inexistente"

    if not _criterios_ok(watch, cand.quote, quote_viva):
        return None, "não atende aos critérios do watch"

    snap = session.get(PriceSnapshot, cand.snapshot_id)
    base = baseline_comparavel(session, cand.watch_id, snap) if snap is not None else None
    gatilho = gatilho_para_preco(watch, quote_viva.preco, base)
    if gatilho is None:
        return None, "preço vivo não dispara mais nenhum gatilho"

    # O gatilho gravado é o REAVALIADO, não o do cache: é ele que descreve o
    # preço que o usuário vai receber na mensagem.
    return cand.model_copy(
        update={"quote": quote_viva, "gatilho": gatilho, "confirmado_ao_vivo": True}
    ), gatilho


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
        if not ha_orcamento_serpapi(session):
            log.warning("Orçamento SerpApi esgotado (mensal ou diário) — candidato %s segue sem confirmação",
                        cand.snapshot_id)
            confirmados.append(cand)
            continue
        try:
            quote_viva = provider.confirmar_google_flights(
                cand.quote.origem, cand.quote.destino,
                cand.quote.data_ida, cand.quote.data_volta,
            )
            registrar_chamada_serpapi(session)
        except Exception as exc:  # noqa: BLE001 — degrada para "não confirmado", nunca quebra a varredura
            log.warning("Falha ao confirmar candidato %s: %s", cand.snapshot_id, exc)
            confirmados.append(cand)
            continue

        if quote_viva is None:
            confirmados.append(cand)
            continue

        confirmado, motivo = _revalidar_ao_vivo(session, cand, quote_viva)
        if confirmado is None:
            log.info(
                "Candidato %s (%s %s->%s %s) DESCARTADO na confirmação ao vivo: %s "
                "(cache R$ %s -> vivo R$ %s)",
                cand.snapshot_id, cand.gatilho, cand.quote.origem, cand.quote.destino,
                cand.quote.data_ida, motivo, cand.quote.preco, quote_viva.preco,
            )
            continue
        confirmados.append(confirmado)
    return confirmados
