"""Testes do detector em camadas e do cooldown."""
from datetime import date, timedelta
from decimal import Decimal

from voofacil.engine.detector import avaliar_snapshot, em_cooldown
from voofacil.engine.store import persistir_quotes
from voofacil.models import Alerta, TipoWatch, Watch, utcnow
from voofacil.schemas import PriceQuote


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


def _quote(preco: str, dia: int = 10, volta: int | None = None,
           volta_mes: int = 9, fonte: str = "TP_CACHE") -> PriceQuote:
    return PriceQuote(
        origem="GRU",
        destino="LIS",
        data_ida=date(2026, 9, dia),
        data_volta=date(2026, volta_mes, volta) if volta else None,
        preco=Decimal(preco),
        fonte=fonte,
    )


def _precos_uniformes(qtd: int, inicio=3000, passo=50) -> list[str]:
    return [str(inicio + passo * i) for i in range(qtd)]


def _historico(session, watch, precos: list[str], rodadas_distintas: bool = True):
    """Popula histórico com um snapshot por preço (dias de embarque 1..N).

    Por padrão cada snapshot é carimbado num DIA DE COLETA diferente, porque o
    warm-up das camadas estatísticas conta RODADAS DE COLETA e não linhas (ver
    `Baseline.rodadas`). Antes este helper gravava tudo numa única chamada de
    `persistir_quotes` — uma rodada só — e os testes de warm-up passavam
    demonstrando exatamente o defeito que deveriam impedir: 20 linhas escritas
    de uma vez não são 20 observações ao longo do tempo.

    `rodadas_distintas=False` reproduz o cold start real, em que UMA chamada
    de calendar traz o mês inteiro de uma vez.
    """
    snaps = persistir_quotes(
        session, watch, [_quote(p, dia=i + 1) for i, p in enumerate(precos)]
    )
    if rodadas_distintas:
        inicio = utcnow() - timedelta(days=len(snaps))
        for i, snap in enumerate(snaps):
            snap.coletado_em = inicio + timedelta(days=i)
        session.flush()
    return snaps


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
    _historico(session, w, ["3300", "3200", "3400", "3250", "3150"])   # warmup=5 rodadas
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
    _historico(session, w, ["3300", "3200"])   # só 2 rodadas < warmup de 5
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


# ---------- warm-up: rodadas de coleta, não linhas de snapshot ----------


def test_warmup_conta_rodadas_de_coleta_e_nao_linhas(session):
    """20 preços gravados numa RODADA só (uma chamada de calendar traz o mês
    inteiro e persistir_quotes dá flush) não aquecem camada estatística
    nenhuma. Contando linhas, aqueciam — e o resultado era alertar os dias
    mais baratos do próprio mês como se fossem queda de preço."""
    w = _watch(session)
    _historico(session, w, _precos_uniformes(20), rodadas_distintas=False)
    snap = persistir_quotes(session, w, [_quote("3150", dia=25)])[0]

    assert avaliar_snapshot(session, w, snap) is None


def test_primeira_varredura_da_vida_nao_gera_candidato(session):
    """Cenário exato da auditoria: watch novo, sem preco_limite, PRIMEIRA
    varredura da vida — a chamada de calendar grava 30 cotações de uma vez.
    Antes saíam 8 candidatos (7 PERCENTIL + 1 NOVO_MINIMO); nenhum deles era
    queda de preço, só a forma do calendário do mês."""
    w = _watch(session)
    snaps = persistir_quotes(
        session, w,
        [_quote(str(3000 + 50 * i), dia=i + 1) for i in range(30)],
    )

    candidatos = [c for c in (avaliar_snapshot(session, w, s) for s in snaps) if c]

    assert candidatos == []


# ---------- cooldown ----------


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


def test_cooldown_bloqueia_mesma_ida_e_mesma_volta(session):
    w = _watch(session, preco_limite=Decimal("3000"))
    snap1 = persistir_quotes(session, w, [_quote("2900", dia=10, volta=17)])[0]
    session.add(Alerta(watch_id=w.id, snapshot_id=snap1.id, gatilho="LIMITE",
                       preco=snap1.preco, canal="TELEGRAM"))
    session.flush()

    snap2 = persistir_quotes(session, w, [_quote("2900", dia=10, volta=17)])[0]

    assert em_cooldown(session, w.id, snap2) is True


def test_cooldown_nao_afeta_outra_data_de_volta(session):
    """Ida 10/09 volta 17/09 alertado NÃO pode silenciar ida 10/09 volta
    20/10: é outra viagem (uma semana vs seis semanas), com outro preço."""
    w = _watch(session, preco_limite=Decimal("3000"))
    snap1 = persistir_quotes(session, w, [_quote("2900", dia=10, volta=17)])[0]
    session.add(Alerta(watch_id=w.id, snapshot_id=snap1.id, gatilho="LIMITE",
                       preco=snap1.preco, canal="TELEGRAM"))
    session.flush()

    snap2 = persistir_quotes(
        session, w, [_quote("2900", dia=10, volta=20, volta_mes=10)]
    )[0]

    assert em_cooldown(session, w.id, snap2) is False
    assert avaliar_snapshot(session, w, snap2) is not None


def test_cooldown_engata_quando_alerta_guardou_o_preco_vivo(session):
    """O dispatcher grava em Alerta.preco o preço que o usuário viu — que
    depois da confirmação ao vivo é o preço VIVO, não o do cache. Comparar só
    Alerta.preco contra o snapshot do cache é comparar unidades diferentes: na
    auditoria gravou-se 3400 (vivo) e comparou-se contra 2900 (cache), o
    cooldown nunca engatava e saía alerta idêntico a cada varredura."""
    w = _watch(session, preco_limite=Decimal("3500"))
    snap1 = persistir_quotes(session, w, [_quote("2900", dia=20)])[0]
    session.add(Alerta(watch_id=w.id, snapshot_id=snap1.id, gatilho="LIMITE",
                       preco=Decimal("3400"), canal="TELEGRAM"))
    session.flush()

    snap2 = persistir_quotes(session, w, [_quote("2900", dia=20)])[0]

    assert em_cooldown(session, w.id, snap2) is True
    assert avaliar_snapshot(session, w, snap2) is None


def test_cooldown_bloqueia_preco_maior_que_o_ja_alertado_ao_vivo(session):
    """Alertamos 2500 (vivo); o cache subiu para 2600. Re-alertar um preço
    MAIOR que o já enviado seria enxurrada, não novidade."""
    w = _watch(session, preco_limite=Decimal("3000"))
    snap1 = persistir_quotes(session, w, [_quote("2400", dia=20)])[0]
    session.add(Alerta(watch_id=w.id, snapshot_id=snap1.id, gatilho="LIMITE",
                       preco=Decimal("2500"), canal="TELEGRAM"))
    session.flush()

    snap2 = persistir_quotes(session, w, [_quote("2600", dia=20)])[0]

    assert em_cooldown(session, w.id, snap2) is True


# ---------- percentil ----------


def test_percentil_dispara_com_historico_suficiente(session):
    w = _watch(session)   # sem limite fixo
    # 20 rodadas de coleta distintas (warmup_percentil=20), 3000..3950
    _historico(session, w, _precos_uniformes(20))
    # abaixo do p25 (~3200s), mas acima do minimo*0.98 (2940) e do error fare (~1725)
    snap = persistir_quotes(session, w, [_quote("3150", dia=25)])[0]

    cand = avaliar_snapshot(session, w, snap)

    assert cand is not None
    assert cand.gatilho == "PERCENTIL"


def test_percentil_nao_dispara_acima_do_p25(session):
    w = _watch(session)
    _historico(session, w, _precos_uniformes(20))
    snap = persistir_quotes(session, w, [_quote("3800", dia=25)])[0]

    assert avaliar_snapshot(session, w, snap) is None


def test_percentil_exige_warmup_proprio_maior_que_novo_minimo(session):
    w = _watch(session)
    # 10 rodadas: aquecido para novo_minimo/error_fare (warmup=5), mas
    # ainda abaixo do warmup_percentil (20) -> percentil deve ficar desativado
    _historico(session, w, _precos_uniformes(10))
    # abaixo do p25 desse historico curto, mas nao abaixo do minimo nem error fare
    snap = persistir_quotes(session, w, [_quote("3050", dia=25)])[0]

    assert avaliar_snapshot(session, w, snap) is None
