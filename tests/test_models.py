"""Smoke tests da fundação: schema, relacionamentos e regras básicas."""
import json
from datetime import date
from decimal import Decimal

from voofacil.models import (
    Alerta,
    FonteSnapshot,
    Gatilho,
    PriceSnapshot,
    TipoData,
    TipoWatch,
    Watch,
)


def _watch_rota(**kw):
    base = dict(
        nome="GRU -> LIS set/26",
        tipo=TipoWatch.ROTA.value,
        origem_iata="GRU",
        destino_iata="LIS",
        data_ini=date(2026, 9, 1),
        data_fim=date(2026, 9, 30),
        preco_limite=Decimal("3500.00"),
    )
    base.update(kw)
    return Watch(**base)


def test_watch_rota_persiste_e_destinos(session):
    w = _watch_rota()
    session.add(w)
    session.commit()

    assert w.id is not None
    assert w.destinos == ["LIS"]
    assert w.moeda == "BRL"
    assert w.ativo is True


def test_watch_criterios_avancados_defaults(session):
    """Campos novos são opcionais: default preserva o comportamento atual (INTERVALO)."""
    w = _watch_rota()
    session.add(w)
    session.commit()

    assert w.tipo_data_ida == TipoData.INTERVALO.value
    assert w.tipo_data_volta == TipoData.INTERVALO.value
    assert w.flexibilidade_dias is None
    assert w.data_volta_ini is None
    assert w.data_volta_fim is None
    assert w.mes_especifico is None
    assert w.preco_minimo is None
    assert w.preco_aproximado is None


def test_watch_criterios_avancados_persiste(session):
    w = _watch_rota(
        tipo_data_ida=TipoData.APROXIMADA.value,
        tipo_data_volta=TipoData.EXATA.value,
        flexibilidade_dias=3,
        data_volta_ini=date(2026, 9, 20),
        data_volta_fim=date(2026, 9, 20),
        mes_especifico=9,
        preco_minimo=Decimal("1500.00"),
        preco_aproximado=Decimal("2800.00"),
    )
    session.add(w)
    session.commit()
    session.refresh(w)

    assert w.tipo_data_ida == "APROXIMADA"
    assert w.tipo_data_volta == "EXATA"
    assert w.flexibilidade_dias == 3
    assert w.data_volta_ini == date(2026, 9, 20)
    assert w.data_volta_fim == date(2026, 9, 20)
    assert w.mes_especifico == 9
    assert isinstance(w.preco_minimo, Decimal)
    assert w.preco_minimo == Decimal("1500.00")
    assert isinstance(w.preco_aproximado, Decimal)
    assert w.preco_aproximado == Decimal("2800.00")


def test_watch_regiao_lista_destinos(session):
    w = Watch(
        nome="GRU -> Europa",
        tipo=TipoWatch.REGIAO.value,
        origem_iata="GRU",
        destino_iata=None,
        regiao_destinos=json.dumps(["LIS", "MAD", "CDG"]),
        data_ini=date(2026, 9, 1),
        data_fim=date(2026, 11, 30),
    )
    session.add(w)
    session.commit()

    assert w.destinos == ["LIS", "MAD", "CDG"]


def test_snapshot_e_alerta_vinculados(session):
    w = _watch_rota()
    session.add(w)
    session.flush()

    snap = PriceSnapshot(
        watch_id=w.id,
        origem="GRU",
        destino="LIS",
        data_ida=date(2026, 9, 10),
        data_volta=date(2026, 9, 24),
        preco=Decimal("3123.45"),
        fonte=FonteSnapshot.TP_CACHE.value,
        deep_link="https://www.aviasales.com/search/GRU1009LIS2409",
    )
    session.add(snap)
    session.flush()

    alerta = Alerta(
        watch_id=w.id,
        snapshot_id=snap.id,
        gatilho=Gatilho.LIMITE.value,
        preco=snap.preco,
        canal="TELEGRAM",
    )
    session.add(alerta)
    session.commit()

    assert w.snapshots[0].preco == Decimal("3123.45")
    assert w.alertas[0].gatilho == "LIMITE"
    assert w.alertas[0].snapshot.deep_link.startswith("https://")


def test_preco_e_decimal_nao_float(session):
    w = _watch_rota()
    session.add(w)
    session.flush()
    snap = PriceSnapshot(
        watch_id=w.id,
        origem="GRU",
        destino="LIS",
        data_ida=date(2026, 9, 10),
        preco=Decimal("1999.99"),
        fonte=FonteSnapshot.SERPAPI.value,
    )
    session.add(snap)
    session.commit()
    session.refresh(snap)

    assert isinstance(snap.preco, Decimal)
    assert snap.preco == Decimal("1999.99")
