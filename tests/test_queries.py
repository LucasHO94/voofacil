"""Testes das consultas/mutações usadas pelo painel Streamlit."""
from datetime import date
from decimal import Decimal

from voofacil.engine.queries import (
    alertas_do_watch,
    alertas_recentes,
    alternar_ativo,
    criar_watch_regiao,
    criar_watch_rota,
    excluir_watch,
    historico_precos,
    listar_watches,
)
from voofacil.engine.store import persistir_quotes
from voofacil.models import Alerta
from voofacil.schemas import PriceQuote


def test_criar_watch_rota(session):
    w = criar_watch_rota(
        session, "GRU -> LIS", "gru", "lis",
        date(2026, 9, 1), date(2026, 9, 30), Decimal("3500"),
    )
    assert w.id is not None
    assert w.origem_iata == "GRU"
    assert w.destino_iata == "LIS"
    assert listar_watches(session) == [w]


def test_criar_watch_regiao_normaliza_destinos(session):
    w = criar_watch_regiao(
        session, "GRU -> Europa", "gru", ["lis", "mad"],
        date(2026, 9, 1), date(2026, 11, 30), None,
        arrival_area_id="/m/02j9z",
    )
    assert w.destinos == ["LIS", "MAD"]
    assert w.arrival_area_id == "/m/02j9z"


def test_alternar_ativo(session):
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    assert w.ativo is True

    alternar_ativo(session, w.id)
    assert session.get(type(w), w.id).ativo is False

    alternar_ativo(session, w.id)
    assert session.get(type(w), w.id).ativo is True


def test_excluir_watch_remove_do_banco(session):
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    wid = w.id

    excluir_watch(session, wid)

    assert listar_watches(session) == []
    assert session.get(type(w), wid) is None


def test_excluir_watch_inexistente_nao_quebra(session):
    excluir_watch(session, 9999)   # não deve lançar


def test_historico_precos_ordenado(session):
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    persistir_quotes(session, w, [
        PriceQuote(origem="GRU", destino="LIS", data_ida=date(2026, 9, 10),
                   preco=Decimal("3100"), fonte="TP_CACHE"),
        PriceQuote(origem="GRU", destino="LIS", data_ida=date(2026, 9, 15),
                   preco=Decimal("2900"), fonte="TP_CACHE"),
    ])
    session.commit()

    hist = historico_precos(session, w.id)
    assert [h.preco for h in hist] == [Decimal("3100"), Decimal("2900")]


def test_alertas_do_watch_e_recentes(session):
    w1 = criar_watch_rota(session, "a", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    w2 = criar_watch_rota(session, "b", "GRU", "MAD", date(2026, 9, 1), date(2026, 9, 30), None)
    snap = persistir_quotes(session, w1, [
        PriceQuote(origem="GRU", destino="LIS", data_ida=date(2026, 9, 10),
                   preco=Decimal("2900"), fonte="TP_CACHE"),
    ])[0]
    session.add(Alerta(watch_id=w1.id, snapshot_id=snap.id, gatilho="LIMITE",
                       preco=snap.preco, canal="TELEGRAM"))
    session.commit()

    assert len(alertas_do_watch(session, w1.id)) == 1
    assert len(alertas_do_watch(session, w2.id)) == 0
    assert len(alertas_recentes(session)) == 1
