"""Detecção de "bom preço" em camadas (da mais urgente à mais fina).

Ordem de avaliação (a primeira que disparar define o gatilho):
1. ERROR_FARE   — preço <= 50% da média histórica (tarifa-erro; prioridade máxima)
2. LIMITE       — preço <= limite fixo definido no watch (piso de segurança)
3. NOVO_MINIMO  — preço < mínimo histórico - margem (trailing minimum)
4. PERCENTIL    — preço <= p25 da distribuição (imita "typical price" do Google
   Flights); exige mais histórico que as demais camadas (~3-6 semanas de
   coleta) para não gerar ruído com poucas amostras — útil sobretudo em
   watches sem preco_limite (busca ampla) que ainda não têm um piso definido.

Camadas estatísticas exigem warm-up para não gerar ruído no cold start, e o
warm-up é medido em RODADAS DE COLETA (dias distintos), NÃO em linhas de
snapshot — ver `Baseline.rodadas` em voofacil/engine/baseline.py. Contando
linhas, uma única chamada de calendar (que grava ~30 preços de uma vez, todos
já visíveis uns aos outros por causa do flush de persistir_quotes) "aquecia" o
watch na PRIMEIRA varredura da vida: medido, um watch novo sem preco_limite
gerava 8 candidatos (7 PERCENTIL + 1 NOVO_MINIMO) só por causa da forma do
calendário do próprio mês. Como as rodadas são dias distintos, exigir N
rodadas já embute um span mínimo de N-1 dias de observação.

Cooldown de `settings.cooldown_horas` impede re-alertar a mesma viagem.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import or_, select

from voofacil.config import settings
from voofacil.engine.baseline import Baseline, baseline_comparavel
from voofacil.models import Alerta, Gatilho, PriceSnapshot, Watch, utcnow
from voofacil.schemas import Candidato, PriceQuote


def _quote_de_snapshot(snap: PriceSnapshot) -> PriceQuote:
    return PriceQuote(
        origem=snap.origem,
        destino=snap.destino,
        data_ida=snap.data_ida,
        data_volta=snap.data_volta,
        preco=snap.preco,
        moeda=snap.moeda,
        cia=snap.cia,
        escalas=snap.escalas,
        fonte=snap.fonte,
        deep_link=snap.deep_link,
        expires_at=snap.expires_at,
    )


def em_cooldown(session, watch_id: int, snap: PriceSnapshot) -> bool:
    """True se já alertamos ESTA viagem por um preço que não é maior que o atual.

    "Esta viagem" = (watch, origem, destino, data_ida, data_volta). A data de
    VOLTA faz parte da chave: sem ela, um alerta de 10/09 volta 17/09
    silenciava 10/09 volta 20/10, que é outra viagem inteiramente — férias de
    uma semana e de seis semanas não são o mesmo produto nem o mesmo preço.

    A comparação de preço olha DOIS valores do alerta antigo e libera só
    quando o preço novo é menor que ambos:
    - `Alerta.preco` — o preço que o usuário de fato viu na mensagem (pode ser
      o preço VIVO da confirmação SerpApi);
    - o preço do PriceSnapshot que originou aquele alerta — a mesma série de
      onde vem `snap.preco` agora.
    Comparar só `Alerta.preco <= snap.preco` era comparar unidades diferentes
    quando a confirmação ao vivo trocava o preço: gravava-se 8900 (vivo) e
    comparava-se contra 2900 (cache), 8900 <= 2900 é falso e o cooldown NUNCA
    engatava — três varreduras seguidas geraram três alertas idênticos e
    gastaram três chamadas SerpApi. Comparar só o snapshot também não basta:
    se alertamos 2500 ao vivo e o cache vier a 2600 depois, re-alertar um
    preço MAIOR seria enxurrada. Exigir menor que os dois cobre os dois casos.
    """
    desde = utcnow() - timedelta(hours=settings.cooldown_horas)
    stmt = (
        select(Alerta.id)
        .join(PriceSnapshot, Alerta.snapshot_id == PriceSnapshot.id)
        .where(
            Alerta.watch_id == watch_id,
            PriceSnapshot.origem == snap.origem,
            PriceSnapshot.destino == snap.destino,
            PriceSnapshot.data_ida == snap.data_ida,
            PriceSnapshot.data_volta == snap.data_volta,
            Alerta.enviado_em >= desde,
            # preço novo NÃO é menor que o já alertado (nem pelo valor enviado,
            # nem pelo snapshot que o originou)
            or_(Alerta.preco <= snap.preco, PriceSnapshot.preco <= snap.preco),
        )
        .limit(1)
    )
    return session.execute(stmt).first() is not None


def _aquecido(base: Baseline | None, minimo_rodadas: int) -> bool:
    """True se o baseline tem observação suficiente para uma camada estatística.

    Conta RODADAS DE COLETA (dias distintos), não linhas — ver docstring do
    módulo. `settings.warmup_snapshots`/`warmup_percentil` passam a significar
    "rodadas", que é o que o nome sempre quis dizer.
    """
    return base is not None and base.rodadas >= minimo_rodadas


def gatilho_para_preco(watch: Watch, preco: Decimal, base: Baseline | None) -> str | None:
    """Camada que `preco` cruza (a primeira da lista), ou None se nenhuma.

    Separada de `avaliar_snapshot` de propósito: a confirmação ao vivo
    (voofacil/engine/confirmacao.py) precisa reavaliar EXATAMENTE o mesmo
    gatilho contra o preço vivo, contra o mesmo baseline — senão um preço que
    subiu depois da coleta viraria alerta assim mesmo.
    """
    if _aquecido(base, settings.warmup_snapshots):
        fator = Decimal(str(settings.fator_error_fare))
        if preco <= base.media * fator:
            return Gatilho.ERROR_FARE.value

    if watch.preco_limite is not None and preco <= watch.preco_limite:
        return Gatilho.LIMITE.value

    if _aquecido(base, settings.warmup_snapshots):
        margem = Decimal(str(settings.margem_novo_minimo))
        if preco < base.minimo * (Decimal("1") - margem):
            return Gatilho.NOVO_MINIMO.value

    if _aquecido(base, settings.warmup_percentil):
        if preco <= base.p25:
            return Gatilho.PERCENTIL.value

    return None


def avaliar_snapshot(session, watch: Watch, snap: PriceSnapshot) -> Candidato | None:
    """Avalia um snapshot recém-coletado; retorna Candidato se merecer alerta."""
    base = baseline_comparavel(session, watch.id, snap)

    gatilho = gatilho_para_preco(watch, snap.preco, base)
    if gatilho is None:
        return None
    if em_cooldown(session, watch.id, snap):
        return None

    return Candidato(
        quote=_quote_de_snapshot(snap),
        watch_id=watch.id,
        snapshot_id=snap.id,
        gatilho=gatilho,
        baseline_min=base.minimo if base else None,
    )
