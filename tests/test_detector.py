"""Testes do detector em camadas e do cooldown."""
from datetime import date
from decimal import Decimal

from farewatch.engine.detector import avaliar_snapshot, em_cooldown
from farewatch.engine.store import persistir_quotes
from farewatch.models import Alerta, TipoWatch, Watch
from farewatch.schemas import PriceQuote


def _watch(session, **kw):
    base = dict(
        nome="GRU -> LIS",
        tipo=TipoWatch.ROTA.value,
        origem_iata="GRU",
        destino_iata="LIS",
        data_ini=date(2026, 9, 1),
        data_fim=date(2026, 9, 30),
    )
    base.update(kw)
    w = Watch(**base)
    session.add(w)
    session.flush()
    return w


def _quote(preco: str, dia: int = 10) -> PriceQuote:
    return PriceQuote(
        origem="GRU",
        destino="LIS",
        data_ida=date(2026, 9, dia),
        preco=Decimal(preco),
        fonte="TP_CACHE",
    )


def _historico(session, watch, precos: list[str]):
    """Popula histórico com um snapshot por preço (dias 1..N)."""
    return persistir_quotes(
        session, watch, [_quote(p, dia=i + 1) for i, p in enumerate(precos)]
    )


def test_limite_fixo_dispara_sem_historico(session):
    w = _watch(session, preco_limite=Decimal("3000"))
    snap = persistir_quotes(session, w, [_quote("2999.99", dia=20)])[0]

    cand = avaliar_snapshot(session, w, snap)

    assert cand is not None
    assert cand.gatilho == "LIMITE"
    assert cand.snapshot_id == snap.id


def test_acima_do_limite_nao_dispara(session):
    w = _watch(session, preco_limite=Decimal("3000"))
    snap = persistir_quotes(session, w, [_quote("3000.01", dia=20)])[0]

    assert avaliar_snapshot(session, w, snap) is None


def test_novo_minimo_dispara_apos_warmup(session):
    w = _watch(session)   # sem limite fixo
    _historico(session, w, ["3300", "3200", "3400", "3250", "3150"])   # warmup=5
    # 3150 * 0.98 = 3087 -> 3000 dispara
    snap = persistir_quotes(session, w, [_quote("3000", dia=20)])[0]

    cand = avaliar_snapshot(session, w, snap)

    assert cand is not None
    assert cand.gatilho == "NOVO_MINIMO"
    assert cand.baseline_min == Decimal("3150")


def test_novo_minimo_respeita_margem(session):
    w = _watch(session)
    _historico(session, w, ["3300", "3200", "3400", "3250", "3150"])
    # 3100 > 3150*0.98=3087 -> dentro da margem de ruído, não dispara
    snap = persistir_quotes(session, w, [_quote("3100", dia=20)])[0]

    assert avaliar_snapshot(session, w, snap) is None


def test_cold_start_sem_warmup_nao_dispara_minimo(session):
    w = _watch(session)
    _historico(session, w, ["3300", "3200"])   # só 2 < warmup de 5
    snap = persistir_quotes(session, w, [_quote("2000", dia=20)])[0]

    assert avaliar_snapshot(session, w, snap) is None


def test_error_fare_tem_prioridade_sobre_limite(session):
    w = _watch(session, preco_limite=Decimal("3000"))
    _historico(session, w, ["3300", "3200", "3400", "3250", "3150"])
    # média ~3260; 50% = 1630 -> 1500 é error fare (e também abaixo do limite)
    snap = persistir_quotes(session, w, [_quote("1500", dia=20)])[0]

    cand = avaliar_snapshot(session, w, snap)

    assert cand is not None
    assert cand.gatilho == "ERROR_FARE"


def test_cooldown_bloqueia_repeticao_mesmo_preco(session):
    w = _watch(session, preco_limite=Decimal("3000"))
    snap1 = persistir_quotes(session, w, [_quote("2900", dia=20)])[0]
    session.add(Alerta(watch_id=w.id, snapshot_id=snap1.id, gatilho="LIMITE",
                       preco=snap1.preco, canal="TELEGRAM"))
    session.flush()

    snap2 = persistir_quotes(session, w, [_quote("2900", dia=20)])[0]

    assert em_cooldown(session, w.id, snap2) is True
    assert avaliar_snapshot(session, w, snap2) is None


def test_cooldown_libera_preco_menor(session):
    w = _watch(session, preco_limite=Decimal("3000"))
    snap1 = persistir_quotes(session, w, [_quote("2900", dia=20)])[0]
    session.add(Alerta(watch_id=w.id, snapshot_id=snap1.id, gatilho="LIMITE",
                       preco=snap1.preco, canal="TELEGRAM"))
    session.flush()

    snap2 = persistir_quotes(session, w, [_quote("2700", dia=20)])[0]   # caiu mais

    assert em_cooldown(session, w.id, snap2) is False
    cand = avaliar_snapshot(session, w, snap2)
    assert cand is not None


def test_cooldown_nao_afeta_outra_data(session):
    w = _watch(session, preco_limite=Decimal("3000"))
    snap1 = persistir_quotes(session, w, [_quote("2900", dia=20)])[0]
    session.add(Alerta(watch_id=w.id, snapshot_id=snap1.id, gatilho="LIMITE",
                       preco=snap1.preco, canal="TELEGRAM"))
    session.flush()

    snap2 = persistir_quotes(session, w, [_quote("2900", dia=25)])[0]   # outro dia de voo

    assert em_cooldown(session, w.id, snap2) is False
