"""Agendamento dinâmico de varredura/explore, editável pelo painel Streamlit.

Antes, `SCAN_HORARIOS` e `EXPLORE_HORARIO` só existiam como CSV estático em
`voofacil/config.py` (variáveis de ambiente) — para mudar quantidade ou
valores era preciso editar o `.env` e reiniciar o processo. Agora os
horários moram no banco (`HorarioAgendado`, compartilhado entre painel e
worker via Postgres/Neon), e o painel pode adicionar/excluir/editar direto.
O `.env` (`settings.scan_horarios` / `settings.explore_horario`) continua
servindo só de SEED inicial — ver `garantir_horarios_padrao` — só semeia
quando a tabela do tipo está totalmente vazia; se o usuário apagar tudo de
propósito depois, não volta a semear sozinho.

Por que isso existe: o `on: schedule: cron` do GitHub Actions é YAML
ESTÁTICO — não dá pra apontar pro banco e disparar em horários 100%
dinâmicos na nuvem. A solução é o workflow rodar com frequência FIXA e curta
(a cada `settings.agendamento_poll_minutos`, ex.: 15 min) e, a CADA
execução, o worker perguntar `deve_rodar_agora()`: se a resposta for não,
sai sem gastar nenhuma chamada de API. `deve_rodar_agora` decide isso
comparando o horário BRT atual contra os `HorarioAgendado` ativos do tipo
pedido, com uma janela de tolerância (`settings.agendamento_tolerancia_minutos`)
para absorver o fato de o poll não bater exatamente em cima do minuto
configurado.

A aritmética de virada de meia-noite é a parte fácil de errar: um horário
configurado para 23:55 com "agora" às 00:02 BRT tem diferença real de 7
minutos, não ~1433 — `_diferenca_minutos_circular` cuida disso tratando os
1440 minutos do dia como um círculo (`min(diff, 1440 - diff)`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from voofacil.config import settings
from voofacil.models import HorarioAgendado
from voofacil.schedule_utils import parse_horarios

# BRT = UTC-3 (Brasil não usa mais horário de verão desde 2019) — mesma
# convenção usada em voofacil/schedule_utils.py, sem lib externa de timezone.
_OFFSET_UTC_PARA_BRT_HORAS = 3

_MINUTOS_POR_DIA = 24 * 60


def _validar_hora_minuto(hora: int, minuto: int) -> None:
    if not (0 <= hora <= 23):
        raise ValueError(f"Hora fora do intervalo válido (0-23): {hora!r}")
    if not (0 <= minuto <= 59):
        raise ValueError(f"Minuto fora do intervalo válido (0-59): {minuto!r}")


def _diferenca_minutos_circular(a: int, b: int) -> int:
    """Diferença mínima (em minutos) entre dois instantes do dia (0-1439),
    tratando o dia como um círculo — ou seja, considerando também a distância
    "dando a volta" pela meia-noite. Ex.: a=1435 (23:55), b=2 (00:02) ->
    diff bruto=1433, mas 1440-1433=7 é a distância real (a menor das duas)."""
    diff = abs(a - b)
    return min(diff, _MINUTOS_POR_DIA - diff)


def listar_horarios(
    session, tipo: str, *, incluir_inativos: bool = False
) -> list[HorarioAgendado]:
    """Lista os horários do `tipo` pedido, ordenados por (hora, minuto).

    Por padrão só retorna os ativos (`ativo=True`) — o que o poll/worker
    precisa. Passe `incluir_inativos=True` para listagem administrativa no
    painel (mostrar também os desativados)."""
    stmt = select(HorarioAgendado).where(HorarioAgendado.tipo == tipo)
    if not incluir_inativos:
        stmt = stmt.where(HorarioAgendado.ativo.is_(True))
    stmt = stmt.order_by(HorarioAgendado.hora, HorarioAgendado.minuto)
    return list(session.scalars(stmt))


def criar_horario(session, tipo: str, hora: int, minuto: int) -> HorarioAgendado:
    _validar_hora_minuto(hora, minuto)
    h = HorarioAgendado(tipo=tipo, hora=hora, minuto=minuto, ativo=True)
    session.add(h)
    session.commit()
    return h


def atualizar_horario(session, horario_id: int, hora: int, minuto: int) -> HorarioAgendado:
    _validar_hora_minuto(hora, minuto)
    h = session.get(HorarioAgendado, horario_id)
    if h is None:
        raise ValueError(f"HorarioAgendado {horario_id!r} não encontrado")
    h.hora = hora
    h.minuto = minuto
    session.commit()
    return h


def excluir_horario(session, horario_id: int) -> None:
    h = session.get(HorarioAgendado, horario_id)
    if h is not None:
        session.delete(h)
        session.commit()


def alternar_ativo(session, horario_id: int) -> HorarioAgendado:
    h = session.get(HorarioAgendado, horario_id)
    if h is None:
        raise ValueError(f"HorarioAgendado {horario_id!r} não encontrado")
    h.ativo = not h.ativo
    session.commit()
    return h


def _existe_algum(session, tipo: str) -> bool:
    """True se existir QUALQUER HorarioAgendado do `tipo`, ativo ou não —
    usado só por `garantir_horarios_padrao` para decidir se semeia (a
    semeadura é única na vida do banco, não reage a "zero horários ATIVOS
    no momento")."""
    stmt = select(HorarioAgendado.id).where(HorarioAgendado.tipo == tipo).limit(1)
    return session.scalar(stmt) is not None


def garantir_horarios_padrao(session) -> None:
    """Semeia a tabela `horario_agendado` a partir do `.env` — mas só na
    PRIMEIRA vez (tabela do tipo totalmente vazia, sem nenhuma linha, ativa
    ou inativa). Se o usuário apagar tudo de propósito depois pelo painel,
    a tabela fica vazia e permanece vazia — não há re-semeadura automática."""
    from voofacil.models import TipoHorario

    if not _existe_algum(session, TipoHorario.VARREDURA.value):
        for hora, minuto in parse_horarios(settings.scan_horarios):
            criar_horario(session, TipoHorario.VARREDURA.value, hora, minuto)

    if not _existe_algum(session, TipoHorario.EXPLORE.value):
        hora, minuto = parse_horarios(settings.explore_horario)[0]
        criar_horario(session, TipoHorario.EXPLORE.value, hora, minuto)


def deve_rodar_agora(session, tipo: str, agora_utc: datetime | None = None) -> bool:
    """True se, AGORA (horário BRT), algum `HorarioAgendado` ativo do `tipo`
    pedido estiver dentro da janela de tolerância (`settings.
    agendamento_tolerancia_minutos`). Usado pelo poll de frequência fixa
    (GitHub Actions/daemon local) para decidir se vale a pena varrer/
    explorar de verdade nesta execução, sem gastar chamada de API à toa.

    `agora_utc` é opcional (default: `datetime.now(timezone.utc)`) — passar
    um valor explícito é o que permite testar a virada de meia-noite de
    forma determinística."""
    if agora_utc is None:
        agora_utc = datetime.now(timezone.utc)

    agora_brt = agora_utc - timedelta(hours=_OFFSET_UTC_PARA_BRT_HORAS)
    agora_total_min = agora_brt.hour * 60 + agora_brt.minute

    for h in listar_horarios(session, tipo):
        horario_total_min = h.hora * 60 + h.minuto
        diff = _diferenca_minutos_circular(horario_total_min, agora_total_min)
        if diff <= settings.agendamento_tolerancia_minutos:
            return True
    return False
