"""Testes do controle de orçamento mensal por provedor."""
from datetime import date

from farewatch.engine.orcamento import consumo_atual, ha_orcamento, registrar_chamada


def test_consumo_inicial_e_zero(session):
    assert consumo_atual(session, "SERPAPI") == 0
    assert ha_orcamento(session, "SERPAPI", limite_mensal=250) is True


def test_registrar_chamada_incrementa(session):
    hoje = date(2026, 7, 24)
    assert registrar_chamada(session, "SERPAPI", hoje) == 1
    assert registrar_chamada(session, "SERPAPI", hoje) == 2
    assert consumo_atual(session, "SERPAPI", hoje) == 2


def test_orcamento_esgota_no_limite(session):
    hoje = date(2026, 7, 24)
    for _ in range(5):
        registrar_chamada(session, "SERPAPI", hoje)
    assert ha_orcamento(session, "SERPAPI", limite_mensal=5, hoje=hoje) is False
    assert ha_orcamento(session, "SERPAPI", limite_mensal=6, hoje=hoje) is True


def test_meses_diferentes_nao_se_misturam(session):
    registrar_chamada(session, "SERPAPI", date(2026, 6, 30))
    registrar_chamada(session, "SERPAPI", date(2026, 7, 1))
    registrar_chamada(session, "SERPAPI", date(2026, 7, 2))

    assert consumo_atual(session, "SERPAPI", date(2026, 6, 15)) == 1
    assert consumo_atual(session, "SERPAPI", date(2026, 7, 15)) == 2


def test_provedores_diferentes_sao_independentes(session):
    registrar_chamada(session, "SERPAPI", date(2026, 7, 1))
    registrar_chamada(session, "OUTRO", date(2026, 7, 1))
    registrar_chamada(session, "OUTRO", date(2026, 7, 1))

    assert consumo_atual(session, "SERPAPI", date(2026, 7, 1)) == 1
    assert consumo_atual(session, "OUTRO", date(2026, 7, 1)) == 2
