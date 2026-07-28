"""Testes do controle de orçamento (mensal e diário) por provedor."""
from datetime import date

from voofacil.config import settings
from voofacil.engine.orcamento import (
    consumo_atual,
    consumo_diario,
    ha_orcamento,
    ha_orcamento_diario,
    ha_orcamento_serpapi,
    registrar_chamada,
    registrar_chamada_diaria,
    registrar_chamada_serpapi,
)


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


# ---------- teto DIÁRIO (complementa o mensal) ----------


def test_consumo_diario_inicial_e_zero(session):
    assert consumo_diario(session, "SERPAPI") == 0
    assert ha_orcamento_diario(session, "SERPAPI", limite_diario=8) is True


def test_registrar_chamada_diaria_incrementa(session):
    hoje = date(2026, 7, 28)
    assert registrar_chamada_diaria(session, "SERPAPI", hoje) == 1
    assert registrar_chamada_diaria(session, "SERPAPI", hoje) == 2
    assert consumo_diario(session, "SERPAPI", hoje) == 2


def test_orcamento_diario_esgota_no_limite(session):
    hoje = date(2026, 7, 28)
    for _ in range(8):
        registrar_chamada_diaria(session, "SERPAPI", hoje)
    assert ha_orcamento_diario(session, "SERPAPI", limite_diario=8, hoje=hoje) is False
    assert ha_orcamento_diario(session, "SERPAPI", limite_diario=9, hoje=hoje) is True


def test_dias_diferentes_nao_se_misturam(session):
    registrar_chamada_diaria(session, "SERPAPI", date(2026, 7, 27))
    registrar_chamada_diaria(session, "SERPAPI", date(2026, 7, 28))
    registrar_chamada_diaria(session, "SERPAPI", date(2026, 7, 28))

    assert consumo_diario(session, "SERPAPI", date(2026, 7, 27)) == 1
    assert consumo_diario(session, "SERPAPI", date(2026, 7, 28)) == 2


# ---------- combinação mensal + diário (o que o worker usa de fato) ----------


def test_ha_orcamento_serpapi_ok_quando_ambos_tem_folga(session):
    assert ha_orcamento_serpapi(session) is True


def test_ha_orcamento_serpapi_bloqueia_por_teto_diario_mesmo_com_mes_de_folga(session, monkeypatch):
    monkeypatch.setattr(settings, "serpapi_orcamento_dia", 2)
    monkeypatch.setattr(settings, "serpapi_orcamento_mes", 250)
    hoje = date(2026, 7, 28)
    registrar_chamada_serpapi(session, hoje)
    registrar_chamada_serpapi(session, hoje)  # bate o teto diário (2)

    assert ha_orcamento_serpapi(session, hoje) is False
    # o mês continua com folga enorme — só o diário bloqueou
    assert ha_orcamento(session, "SERPAPI", 250, hoje) is True


def test_ha_orcamento_serpapi_bloqueia_por_teto_mensal_mesmo_com_dia_de_folga(session, monkeypatch):
    monkeypatch.setattr(settings, "serpapi_orcamento_dia", 8)
    monkeypatch.setattr(settings, "serpapi_orcamento_mes", 2)
    hoje = date(2026, 7, 28)
    registrar_chamada_serpapi(session, hoje)
    registrar_chamada_serpapi(session, hoje)  # bate o teto mensal (2), diário só em 2/8

    assert ha_orcamento_serpapi(session, hoje) is False
    assert ha_orcamento_diario(session, "SERPAPI", 8, hoje) is True


def test_registrar_chamada_serpapi_incrementa_os_dois_contadores(session):
    hoje = date(2026, 7, 28)
    registrar_chamada_serpapi(session, hoje)

    assert consumo_atual(session, "SERPAPI", hoje) == 1
    assert consumo_diario(session, "SERPAPI", hoje) == 1
