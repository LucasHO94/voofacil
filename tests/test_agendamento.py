"""Testes do agendamento dinâmico (voofacil/engine/agendamento.py).

Cobre o CRUD de HorarioAgendado, a idempotência do seed a partir do .env
(garantir_horarios_padrao) e, com atenção especial, a aritmética circular de
deve_rodar_agora — em especial a virada de meia-noite (BRT), que é a parte
mais fácil de errar (ex.: horário configurado 23:55, agora 00:02 BRT deve
dar 7 minutos de diferença, não ~1433).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from voofacil.config import settings
from voofacil.engine.agendamento import (
    alternar_ativo,
    atualizar_horario,
    criar_horario,
    deve_rodar_agora,
    excluir_horario,
    garantir_horarios_padrao,
    listar_horarios,
)
from voofacil.models import HorarioAgendado
from voofacil.schedule_utils import parse_horarios


def _utc_de_brt(hora_brt: int, minuto_brt: int) -> datetime:
    """Monta um datetime UTC (tz-aware) correspondente a um horário BRT dado
    (apenas hora/minuto importam para deve_rodar_agora — a data-calendário é
    arbitrária e o overflow de dia por causa do +3h é resolvido pelo próprio
    timedelta)."""
    base = datetime(2026, 7, 24, hora_brt, minuto_brt, tzinfo=timezone.utc)
    return base + timedelta(hours=3)


# ---------- CRUD básico ----------


def test_criar_horario_happy_path(session):
    h = criar_horario(session, "VARREDURA", 6, 30)
    assert h.id is not None
    assert h.tipo == "VARREDURA"
    assert h.hora == 6
    assert h.minuto == 30
    assert h.ativo is True


@pytest.mark.parametrize("hora,minuto", [(-1, 0), (24, 0), (0, -1), (0, 60), (25, 61)])
def test_criar_horario_hora_minuto_invalidos(session, hora, minuto):
    with pytest.raises(ValueError):
        criar_horario(session, "VARREDURA", hora, minuto)


def test_listar_horarios_ordenado_e_so_ativos_por_padrao(session):
    criar_horario(session, "VARREDURA", 21, 0)
    criar_horario(session, "VARREDURA", 6, 0)
    h_meio = criar_horario(session, "VARREDURA", 12, 30)
    alternar_ativo(session, h_meio.id)  # desativa

    ativos = listar_horarios(session, "VARREDURA")
    assert [(h.hora, h.minuto) for h in ativos] == [(6, 0), (21, 0)]

    todos = listar_horarios(session, "VARREDURA", incluir_inativos=True)
    assert [(h.hora, h.minuto) for h in todos] == [(6, 0), (12, 30), (21, 0)]


def test_listar_horarios_nao_mistura_tipos(session):
    criar_horario(session, "VARREDURA", 6, 0)
    criar_horario(session, "EXPLORE", 5, 0)

    assert [h.hora for h in listar_horarios(session, "VARREDURA")] == [6]
    assert [h.hora for h in listar_horarios(session, "EXPLORE")] == [5]


def test_atualizar_horario_happy_path(session):
    h = criar_horario(session, "VARREDURA", 6, 0)
    atualizado = atualizar_horario(session, h.id, 7, 45)
    assert atualizado.hora == 7
    assert atualizado.minuto == 45


@pytest.mark.parametrize("hora,minuto", [(-1, 0), (24, 0), (0, -1), (0, 60)])
def test_atualizar_horario_invalido(session, hora, minuto):
    h = criar_horario(session, "VARREDURA", 6, 0)
    with pytest.raises(ValueError):
        atualizar_horario(session, h.id, hora, minuto)
    # não deve ter mudado nada
    inalterado = session.get(HorarioAgendado, h.id)
    assert (inalterado.hora, inalterado.minuto) == (6, 0)


def test_excluir_horario(session):
    h = criar_horario(session, "VARREDURA", 6, 0)
    excluir_horario(session, h.id)
    assert session.get(HorarioAgendado, h.id) is None
    assert listar_horarios(session, "VARREDURA", incluir_inativos=True) == []


def test_excluir_horario_id_inexistente_nao_levanta(session):
    excluir_horario(session, 999999)  # não deve levantar


def test_alternar_ativo(session):
    h = criar_horario(session, "VARREDURA", 6, 0)
    assert h.ativo is True
    desativado = alternar_ativo(session, h.id)
    assert desativado.ativo is False
    reativado = alternar_ativo(session, h.id)
    assert reativado.ativo is True


# ---------- garantir_horarios_padrao (seed a partir do .env) ----------


def test_garantir_horarios_padrao_semeia_quando_vazio(session):
    garantir_horarios_padrao(session)

    esperados_varredura = parse_horarios(settings.scan_horarios)
    varredura = listar_horarios(session, "VARREDURA")
    assert [(h.hora, h.minuto) for h in varredura] == sorted(esperados_varredura)

    hora_explore, minuto_explore = parse_horarios(settings.explore_horario)[0]
    explore = listar_horarios(session, "EXPLORE")
    assert len(explore) == 1
    assert (explore[0].hora, explore[0].minuto) == (hora_explore, minuto_explore)


def test_garantir_horarios_padrao_nao_resemeia_se_ja_existe(session):
    criar_horario(session, "VARREDURA", 3, 33)
    criar_horario(session, "EXPLORE", 4, 44)

    garantir_horarios_padrao(session)

    varredura = listar_horarios(session, "VARREDURA", incluir_inativos=True)
    explore = listar_horarios(session, "EXPLORE", incluir_inativos=True)
    assert [(h.hora, h.minuto) for h in varredura] == [(3, 33)]
    assert [(h.hora, h.minuto) for h in explore] == [(4, 44)]


def test_garantir_horarios_padrao_nao_resemeia_com_zero_ativos_mas_ha_inativo(session):
    """Usuário apagou (desativou) tudo de propósito — deve continuar vazio de
    ativos, sem o seed voltar a inserir os padrões do .env."""
    h = criar_horario(session, "VARREDURA", 3, 33)
    alternar_ativo(session, h.id)  # fica inativo, mas a linha continua existindo

    garantir_horarios_padrao(session)

    assert listar_horarios(session, "VARREDURA") == []  # nenhum ativo
    todos = listar_horarios(session, "VARREDURA", incluir_inativos=True)
    assert [(x.hora, x.minuto) for x in todos] == [(3, 33)]  # não resemeou


# ---------- deve_rodar_agora ----------


def test_deve_rodar_agora_dentro_da_tolerancia_bate(session, monkeypatch):
    monkeypatch.setattr(settings, "agendamento_tolerancia_minutos", 8)
    criar_horario(session, "VARREDURA", 10, 0)
    agora = _utc_de_brt(10, 5)  # 5 min de diferença
    assert deve_rodar_agora(session, "VARREDURA", agora) is True


def test_deve_rodar_agora_fora_da_tolerancia_nao_bate(session, monkeypatch):
    monkeypatch.setattr(settings, "agendamento_tolerancia_minutos", 8)
    criar_horario(session, "VARREDURA", 10, 0)
    agora = _utc_de_brt(10, 20)  # 20 min de diferença
    assert deve_rodar_agora(session, "VARREDURA", agora) is False


def test_deve_rodar_agora_exatamente_na_borda_bate(session, monkeypatch):
    monkeypatch.setattr(settings, "agendamento_tolerancia_minutos", 8)
    criar_horario(session, "VARREDURA", 10, 0)
    agora = _utc_de_brt(10, 8)  # exatamente 8 min de diferença == tolerância
    assert deve_rodar_agora(session, "VARREDURA", agora) is True


def test_deve_rodar_agora_um_minuto_alem_da_borda_nao_bate(session, monkeypatch):
    monkeypatch.setattr(settings, "agendamento_tolerancia_minutos", 8)
    criar_horario(session, "VARREDURA", 10, 0)
    agora = _utc_de_brt(10, 9)  # 9 min > tolerância de 8
    assert deve_rodar_agora(session, "VARREDURA", agora) is False


def test_deve_rodar_agora_virada_meia_noite_horario_perto_das_00_agora_perto_das_23_59(
    session, monkeypatch
):
    monkeypatch.setattr(settings, "agendamento_tolerancia_minutos", 8)
    criar_horario(session, "VARREDURA", 0, 2)   # horário configurado: 00:02
    agora = _utc_de_brt(23, 55)                  # agora: 23:55 (mesmo "lado" do dia anterior)
    # diferença real: 7 min (não ~1433) — dentro da tolerância de 8
    assert deve_rodar_agora(session, "VARREDURA", agora) is True


def test_deve_rodar_agora_virada_meia_noite_horario_perto_das_23_59_agora_perto_das_00(
    session, monkeypatch
):
    monkeypatch.setattr(settings, "agendamento_tolerancia_minutos", 8)
    criar_horario(session, "VARREDURA", 23, 55)  # horário configurado: 23:55
    agora = _utc_de_brt(0, 2)                     # agora: 00:02 do dia seguinte
    # diferença real: 7 min (não ~1433) — dentro da tolerância de 8
    assert deve_rodar_agora(session, "VARREDURA", agora) is True


def test_deve_rodar_agora_virada_meia_noite_fora_da_tolerancia_nao_bate(session, monkeypatch):
    monkeypatch.setattr(settings, "agendamento_tolerancia_minutos", 8)
    criar_horario(session, "VARREDURA", 23, 55)
    agora = _utc_de_brt(0, 20)  # diferença real: 25 min — fora da tolerância
    assert deve_rodar_agora(session, "VARREDURA", agora) is False


def test_deve_rodar_agora_tipo_errado_nao_bate(session, monkeypatch):
    monkeypatch.setattr(settings, "agendamento_tolerancia_minutos", 8)
    criar_horario(session, "VARREDURA", 10, 0)
    agora = _utc_de_brt(10, 0)  # bate exatamente, mas para o tipo errado
    assert deve_rodar_agora(session, "EXPLORE", agora) is False


def test_deve_rodar_agora_horario_inativo_nao_bate(session, monkeypatch):
    monkeypatch.setattr(settings, "agendamento_tolerancia_minutos", 8)
    h = criar_horario(session, "VARREDURA", 10, 0)
    alternar_ativo(session, h.id)  # desativa
    agora = _utc_de_brt(10, 0)  # bateria exatamente, se estivesse ativo
    assert deve_rodar_agora(session, "VARREDURA", agora) is False


def test_deve_rodar_agora_usa_relogio_real_quando_agora_utc_none(session, monkeypatch):
    """Sem agora_utc explícito, usa datetime.now(UTC) — cria um horário bem
    próximo do agora BRT real para validar que o default funciona."""
    monkeypatch.setattr(settings, "agendamento_tolerancia_minutos", 8)
    agora_real_utc = datetime.now(timezone.utc)
    agora_real_brt = agora_real_utc - timedelta(hours=3)
    criar_horario(session, "VARREDURA", agora_real_brt.hour, agora_real_brt.minute)
    assert deve_rodar_agora(session, "VARREDURA") is True
