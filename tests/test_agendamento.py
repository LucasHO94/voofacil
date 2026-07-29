"""Testes do agendamento dinâmico (voofacil/engine/agendamento.py).

Cobre o CRUD de HorarioAgendado, a idempotência do seed a partir do .env
(garantir_horarios_padrao) e, com atenção especial, o MODELO CATCH-UP de
horarios_devidos/marcar_executados/deve_rodar_agora.

O catch-up existe porque o GitHub Actions entrega só 4-7 dos 96 disparos/dia
prometidos por `*/15 * * * *`, em horários imprevisíveis: perguntar "estou em
cima do horário agora?" dava não praticamente sempre (zero varreduras em 4
dias em produção). A pergunta passou a ser "alguma ocorrência PASSOU desde a
última execução?". Os pontos fáceis de errar — e por isso testados com
datetimes EXPLÍCITOS, nunca datetime.now() — são o poll muito atrasado
(horário 09:00 atendido às 11:37) e a virada de meia-noite BRT (23:50 visto
às 00:10 do dia seguinte já rodou; 00:05 visto às 00:10 é ocorrência nova).
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
    horarios_devidos,
    listar_horarios,
    marcar_executados,
)
from voofacil.models import HorarioAgendado
from voofacil.schedule_utils import parse_horarios


def _utc_de_brt(dia: int, hora_brt: int, minuto_brt: int) -> datetime:
    """Monta o datetime UTC (tz-aware) equivalente a um instante de parede BRT
    (UTC-3) em julho/2026. `dia` explícito porque, no modelo catch-up, a DATA
    importa: 23:50 de ontem e 23:50 de hoje são ocorrências diferentes."""
    return datetime(2026, 7, dia, hora_brt, minuto_brt, tzinfo=timezone.utc) + timedelta(hours=3)


def _aware(dt: datetime) -> datetime:
    """O SQLite devolve datetime NAIVE mesmo em DateTime(timezone=True) — para
    comparar com um UTC aware nos asserts, assume-se UTC (é o que gravamos)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


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


# ---------- horarios_devidos (modelo catch-up) ----------


def _com_ultima_execucao(session, h: HorarioAgendado, quando_utc) -> HorarioAgendado:
    """Carimba ultima_execucao direto (sem passar por marcar_executados, que é
    testado à parte) para montar o estado inicial de cada cenário."""
    h.ultima_execucao = quando_utc
    session.commit()
    return h


def _criar_nunca_executado(session, tipo: str, hora: int, minuto: int) -> HorarioAgendado:
    """Cria um horário com `ultima_execucao=None`.

    `criar_horario` carimba o instante da criação de propósito (para um horário
    novo não disparar varredura na hora — ver docstring dela), então este helper
    limpa o carimbo para reproduzir o estado "nunca executou". Não é artificial:
    é exatamente o estado das linhas que já existem em produção, semeadas antes
    da coluna existir."""
    return _com_ultima_execucao(session, criar_horario(session, tipo, hora, minuto), None)


def test_horario_nunca_executado_esta_devido(session):
    h = _criar_nunca_executado(session, "VARREDURA", 9, 0)
    assert h.ultima_execucao is None
    agora = _utc_de_brt(24, 11, 0)
    assert horarios_devidos(session, "VARREDURA", agora) == [h]


def test_ocorrencia_de_hoje_ja_passou_e_ultima_execucao_e_de_ontem_esta_devido(session):
    h = criar_horario(session, "VARREDURA", 9, 0)
    # rodou ontem às 09:02; hoje já são 11:00 e a ocorrência de hoje (09:00) passou
    _com_ultima_execucao(session, h, _utc_de_brt(23, 9, 2))
    agora = _utc_de_brt(24, 11, 0)
    assert horarios_devidos(session, "VARREDURA", agora) == [h]


def test_ja_executado_depois_da_ocorrencia_mais_recente_nao_esta_devido(session):
    h = criar_horario(session, "VARREDURA", 9, 0)
    # ocorrência mais recente = hoje 09:00, e já rodou hoje às 09:04
    _com_ultima_execucao(session, h, _utc_de_brt(24, 9, 4))
    agora = _utc_de_brt(24, 11, 0)
    assert horarios_devidos(session, "VARREDURA", agora) == []


def test_horario_ainda_nao_chegou_hoje_mas_ja_rodou_hoje_de_manha_nao_esta_devido(session):
    """Horário 21:00, agora 10:00 BRT: a ocorrência mais recente é a de ONTEM
    às 21:00, e a execução das 07:00 de hoje é posterior a ela — não é devido
    (o 21:00 de hoje ainda não chegou)."""
    h = criar_horario(session, "VARREDURA", 21, 0)
    _com_ultima_execucao(session, h, _utc_de_brt(24, 7, 0))
    agora = _utc_de_brt(24, 10, 0)
    assert horarios_devidos(session, "VARREDURA", agora) == []


def test_poll_muito_atrasado_dispara_mesmo_assim(session):
    """O caso que estava QUEBRADO em produção: o GitHub Actions descartou os
    disparos das 09:00 e só executou às 11:37. Na lógica antiga (janela de
    ±7 min) isso dava False e a varredura nunca acontecia; no catch-up, roda."""
    h = _criar_nunca_executado(session, "VARREDURA", 9, 0)
    agora = _utc_de_brt(24, 11, 37)
    assert horarios_devidos(session, "VARREDURA", agora) == [h]
    assert deve_rodar_agora(session, "VARREDURA", agora) is True


def test_poll_muito_atrasado_com_execucao_de_ontem_tambem_dispara(session):
    h = criar_horario(session, "VARREDURA", 9, 0)
    _com_ultima_execucao(session, h, _utc_de_brt(23, 9, 5))
    agora = _utc_de_brt(24, 11, 37)
    assert horarios_devidos(session, "VARREDURA", agora) == [h]


def test_virada_meia_noite_horario_2350_ja_rodou_ontem_nao_esta_devido(session):
    """Horário 23:50, agora 00:10 BRT do dia seguinte: a ocorrência mais
    recente é a de ONTEM às 23:50 e ela já foi atendida às 23:51."""
    h = criar_horario(session, "VARREDURA", 23, 50)
    _com_ultima_execucao(session, h, _utc_de_brt(24, 23, 51))
    agora = _utc_de_brt(25, 0, 10)
    assert horarios_devidos(session, "VARREDURA", agora) == []


def test_virada_meia_noite_horario_0005_ocorrencia_de_hoje_e_nova_esta_devido(session):
    """Horário 00:05, agora 00:10 BRT: a ocorrência mais recente é a de HOJE
    às 00:05, e a última execução (ontem 00:06) é anterior a ela."""
    h = criar_horario(session, "VARREDURA", 0, 5)
    _com_ultima_execucao(session, h, _utc_de_brt(24, 0, 6))
    agora = _utc_de_brt(25, 0, 10)
    assert horarios_devidos(session, "VARREDURA", agora) == [h]


def test_varios_horarios_passados_desde_a_ultima_execucao_voltam_todos(session):
    """O worker roda UMA varredura e marca todos de uma vez — o catch-up não
    acumula N execuções atrasadas, mas também não perde nenhum horário."""
    h6 = criar_horario(session, "VARREDURA", 6, 0)
    h9 = criar_horario(session, "VARREDURA", 9, 0)
    h12 = criar_horario(session, "VARREDURA", 12, 30)
    h21 = criar_horario(session, "VARREDURA", 21, 0)
    for h in (h6, h9, h12, h21):
        _com_ultima_execucao(session, h, _utc_de_brt(24, 5, 0))  # rodou hoje 05:00

    agora = _utc_de_brt(24, 13, 0)  # 06:00, 09:00 e 12:30 já passaram; 21:00 não
    devidos = horarios_devidos(session, "VARREDURA", agora)
    assert [(h.hora, h.minuto) for h in devidos] == [(6, 0), (9, 0), (12, 30)]


def test_horario_inativo_nunca_esta_devido(session):
    h = criar_horario(session, "VARREDURA", 9, 0)
    alternar_ativo(session, h.id)  # desativa
    agora = _utc_de_brt(24, 11, 37)  # estaria devido, se ativo
    assert horarios_devidos(session, "VARREDURA", agora) == []
    assert deve_rodar_agora(session, "VARREDURA", agora) is False


def test_tipo_errado_nao_vaza(session):
    criar_horario(session, "VARREDURA", 9, 0)
    agora = _utc_de_brt(24, 11, 37)
    assert horarios_devidos(session, "EXPLORE", agora) == []
    assert deve_rodar_agora(session, "EXPLORE", agora) is False


def test_ultima_execucao_naive_nao_levanta_typeerror(session):
    """SQLite devolve datetime NAIVE mesmo em DateTime(timezone=True);
    comparar naive com aware levantaria TypeError se não fosse tratado."""
    h = criar_horario(session, "VARREDURA", 9, 0)
    _com_ultima_execucao(session, h, datetime(2026, 7, 23, 12, 5))  # naive, = UTC
    agora = _utc_de_brt(24, 11, 37)
    assert horarios_devidos(session, "VARREDURA", agora) == [h]


def test_horarios_devidos_usa_relogio_real_quando_agora_utc_none(session):
    """Sem agora_utc explícito, usa datetime.now(UTC) — um horário nunca
    executado está devido em qualquer instante."""
    h = _criar_nunca_executado(session, "VARREDURA", 9, 0)
    assert horarios_devidos(session, "VARREDURA") == [h]
    assert deve_rodar_agora(session, "VARREDURA") is True


# ---------- marcar_executados ----------


def test_marcar_executados_grava_persiste_e_encerra_a_pendencia(session):
    h = _criar_nunca_executado(session, "VARREDURA", 9, 0)
    agora = _utc_de_brt(24, 11, 37)
    assert horarios_devidos(session, "VARREDURA", agora) == [h]

    marcar_executados(session, [h], agora)

    # persistiu de verdade no banco (não só no objeto em memória)
    session.expire_all()
    do_banco = session.get(HorarioAgendado, h.id)
    assert do_banco.ultima_execucao is not None
    assert _aware(do_banco.ultima_execucao) == agora

    # e deixou de estar devido, inclusive num poll posterior do mesmo dia
    assert horarios_devidos(session, "VARREDURA", agora) == []
    assert horarios_devidos(session, "VARREDURA", _utc_de_brt(24, 23, 0)) == []
    # mas volta a ficar devido na ocorrência do dia seguinte
    assert [x.id for x in horarios_devidos(session, "VARREDURA", _utc_de_brt(25, 9, 1))] == [h.id]


def test_marcar_executados_marca_todos_da_lista(session):
    h6 = _criar_nunca_executado(session, "VARREDURA", 6, 0)
    h9 = _criar_nunca_executado(session, "VARREDURA", 9, 0)
    agora = _utc_de_brt(24, 11, 37)
    devidos = horarios_devidos(session, "VARREDURA", agora)
    assert len(devidos) == 2

    marcar_executados(session, devidos, agora)

    session.expire_all()
    for hid in (h6.id, h9.id):
        assert session.get(HorarioAgendado, hid).ultima_execucao is not None
    assert horarios_devidos(session, "VARREDURA", agora) == []


def test_marcar_executados_lista_vazia_nao_levanta(session):
    marcar_executados(session, [], _utc_de_brt(24, 11, 37))


def test_marcar_executados_usa_relogio_real_quando_agora_utc_none(session):
    h = criar_horario(session, "VARREDURA", 9, 0)
    antes = datetime.now(timezone.utc)
    marcar_executados(session, [h])
    gravado = _aware(session.get(HorarioAgendado, h.id).ultima_execucao)
    assert antes <= gravado <= datetime.now(timezone.utc)


# ---------- carimbo na criação/edição (não disparar varredura por engano) ----------


def test_criar_horario_nasce_carimbado_e_nao_dispara_na_hora(session):
    """Cadastrar 23:00 às 09:00 da manhã não pode disparar uma varredura
    imediata. Sem carimbo na criação o horário nasceria "nunca executou", que
    no catch-up significa devido, e o próximo poll varreria na hora."""
    agora = _utc_de_brt(24, 9, 0)
    h = criar_horario(session, "VARREDURA", 23, 0)

    assert h.ultima_execucao is not None
    assert horarios_devidos(session, "VARREDURA", agora) == []


def test_criar_horario_dispara_na_proxima_ocorrencia(session):
    """O carimbo adia para a próxima ocorrência, não cancela o horário."""
    h = criar_horario(session, "VARREDURA", 23, 0)
    _com_ultima_execucao(session, h, _utc_de_brt(24, 9, 0))  # criado às 09:00 BRT

    assert horarios_devidos(session, "VARREDURA", _utc_de_brt(24, 22, 0)) == []
    assert [x.id for x in horarios_devidos(session, "VARREDURA", _utc_de_brt(24, 23, 5))] == [h.id]


def test_atualizar_horario_recarimba_e_nao_dispara_na_hora(session):
    """Editar 06:00 -> 07:00 às 09:00 não pode varrer na hora só porque a nova
    ocorrência de hoje (07:00) já passou."""
    h = criar_horario(session, "VARREDURA", 6, 0)
    _com_ultima_execucao(session, h, _utc_de_brt(24, 6, 1))

    atualizar_horario(session, h.id, 7, 0)

    assert horarios_devidos(session, "VARREDURA", _utc_de_brt(24, 9, 0)) == []
