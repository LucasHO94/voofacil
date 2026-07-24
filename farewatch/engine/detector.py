"""Detecção de "bom preço" em camadas (da mais urgente à mais fina).

Ordem de avaliação (a primeira que disparar define o gatilho):
1. ERROR_FARE   — preço <= 50% da média histórica (tarifa-erro; prioridade máxima)
2. LIMITE       — preço <= limite fixo definido no watch (piso de segurança)
3. NOVO_MINIMO  — preço < mínimo histórico - margem (trailing minimum)
   (PERCENTIL entra na fase 2, quando houver semanas de histórico)

Camadas estatísticas exigem warm-up (N snapshots) para não gerar ruído no
cold start. Cooldown de 12h impede re-alertar o mesmo preço/rota/dia.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from farewatch.config import settings
from farewatch.engine.baseline import Baseline, baseline_rota
from farewatch.models import Alerta, Gatilho, PriceSnapshot, Watch, utcnow
from farewatch.schemas import Candidato, PriceQuote


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
    """True se já alertamos esta rota/dia com preço igual ou menor nas últimas N horas."""
    desde = utcnow() - timedelta(hours=settings.cooldown_horas)
    stmt = (
        select(Alerta.id)
        .join(PriceSnapshot, Alerta.snapshot_id == PriceSnapshot.id)
        .where(
            Alerta.watch_id == watch_id,
            PriceSnapshot.origem == snap.origem,
            PriceSnapshot.destino == snap.destino,
            PriceSnapshot.data_ida == snap.data_ida,
            Alerta.enviado_em >= desde,
            Alerta.preco <= snap.preco,   # preço novo NÃO é menor que o já alertado
        )
        .limit(1)
    )
    return session.execute(stmt).first() is not None


def _gatilho_disparado(watch: Watch, snap: PriceSnapshot, base: Baseline | None) -> str | None:
    aquecido = base is not None and base.n >= settings.warmup_snapshots

    if aquecido:
        fator = Decimal(str(settings.fator_error_fare))
        if snap.preco <= base.media * fator:
            return Gatilho.ERROR_FARE.value

    if watch.preco_limite is not None and snap.preco <= watch.preco_limite:
        return Gatilho.LIMITE.value

    if aquecido:
        margem = Decimal(str(settings.margem_novo_minimo))
        if snap.preco < base.minimo * (Decimal("1") - margem):
            return Gatilho.NOVO_MINIMO.value

    return None


def avaliar_snapshot(session, watch: Watch, snap: PriceSnapshot) -> Candidato | None:
    """Avalia um snapshot recém-coletado; retorna Candidato se merecer alerta."""
    mes = f"{snap.data_ida.year:04d}-{snap.data_ida.month:02d}"
    base = baseline_rota(
        session,
        snap.origem,
        snap.destino,
        mes_embarque=mes,
        excluir_snapshot_id=snap.id,
    )

    gatilho = _gatilho_disparado(watch, snap, base)
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
