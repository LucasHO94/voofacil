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
(`cron: '*/15 * * * *'`) e, a CADA execução, o worker perguntar ao banco se
há trabalho a fazer; se não houver, sai sem gastar nenhuma chamada de API.

MODELO CATCH-UP (recuperação) — por que NÃO é mais uma janela de tolerância:
o cron do GitHub Actions em repositório free NÃO é confiável. Medição real do
histórico deste repo: dos 96 disparos/dia prometidos por `*/15`, chegam de 4 a
7 — a plataforma descarta ~95% dos agendamentos de alta frequência e entrega o
resto em horários imprevisíveis, com 1 a 3 horas entre execuções. A lógica
antiga perguntava "o horário BRT de AGORA está a ±7 min de algum horário
agendado?"; com ~5 polls/dia caindo em minutos aleatórios contra janelas que
somavam 112 dos 1440 minutos do dia, a resposta era praticamente sempre não.
Resultado medido: ZERO varreduras em 4 dias, todas as execuções logando "nada
agendado para agora" — a automação inteira estava morta.

A pergunta certa, então, não é "estou EM CIMA do horário?" e sim "algum
horário PASSOU desde a última vez que eu rodei?" — o mesmo modelo do
`Persistent=true` do systemd-timer ou do misfire handling do APScheduler.
Isso é imune tanto a atraso quanto a disparo descartado: se o horário das
09:00 passou e o próximo poll real só acontece às 11:37, a varredura acontece
às 11:37. E cada horário dispara NO MÁXIMO uma vez por dia, porque a
comparação é sempre contra a ocorrência MAIS RECENTE dele (nunca acumula N
execuções atrasadas).

O estado que sustenta isso é `HorarioAgendado.ultima_execucao` (UTC, tz-aware,
NULL = nunca executou => devido). O fluxo do worker é: `horarios_devidos()` ->
roda UMA varredura -> `marcar_executados()` com a lista devolvida.

REFERENCIAL DE COMPARAÇÃO: tudo é comparado em UTC. `hora`/`minuto` do
`HorarioAgendado` são BRT (UTC-3, sem lib de timezone — convenção do projeto),
então a ocorrência é montada no calendário BRT e convertida de volta para UTC
antes de ser comparada com `ultima_execucao`.
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


def _validar_hora_minuto(hora: int, minuto: int) -> None:
    if not (0 <= hora <= 23):
        raise ValueError(f"Hora fora do intervalo válido (0-23): {hora!r}")
    if not (0 <= minuto <= 59):
        raise ValueError(f"Minuto fora do intervalo válido (0-59): {minuto!r}")


def _como_utc_aware(dt: datetime) -> datetime:
    """Normaliza `dt` para UTC tz-aware.

    Existe por causa de uma pegadinha real: comparar datetime naive com aware
    levanta TypeError, e o SQLite devolve datetime NAIVE mesmo em colunas
    declaradas `DateTime(timezone=True)` (ele não guarda offset). Como tudo que
    este módulo grava está em UTC, um valor sem tzinfo é assumido como UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ocorrencia_mais_recente_utc(hora: int, minuto: int, agora_utc: datetime) -> datetime:
    """Instante (em UTC) da ocorrência MAIS RECENTE de HH:MM (BRT) até `agora`.

    Ou seja: hoje às HH:MM no calendário BRT; se HH:MM ainda não chegou hoje,
    ontem às HH:MM. É este instante que se compara com `ultima_execucao` —
    daí a virada de meia-noite sair de graça, sem aritmética circular: às
    00:10 BRT, a ocorrência mais recente de 23:50 é a de ONTEM, e a de 00:05 é
    a de HOJE."""
    # Deslocar o relógio em -3h dá o "calendário de parede" BRT (a tzinfo
    # continua sendo UTC, é só um artifício aritmético — desfeito no fim).
    agora_brt = agora_utc - timedelta(hours=_OFFSET_UTC_PARA_BRT_HORAS)
    ocorrencia_brt = agora_brt.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if ocorrencia_brt > agora_brt:
        ocorrencia_brt -= timedelta(days=1)  # HH:MM ainda não chegou hoje
    return ocorrencia_brt + timedelta(hours=_OFFSET_UTC_PARA_BRT_HORAS)


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
    """Cria um horário JÁ CARIMBADO com o instante da criação.

    O carimbo inicial não é decorativo: sem ele o horário nasceria com
    `ultima_execucao=None`, que no modelo catch-up significa "nunca executou,
    está devido" — e o próximo poll dispararia uma varredura completa na hora,
    mesmo que o usuário tenha acabado de cadastrar 23:00 às 09:00 da manhã.
    Carimbar na criação dá a semântica que se espera de um agendador: vale a
    partir da PRÓXIMA ocorrência, não retroativamente.
    """
    _validar_hora_minuto(hora, minuto)
    h = HorarioAgendado(
        tipo=tipo, hora=hora, minuto=minuto, ativo=True,
        ultima_execucao=datetime.now(timezone.utc),
    )
    session.add(h)
    session.commit()
    return h


def atualizar_horario(session, horario_id: int, hora: int, minuto: int) -> HorarioAgendado:
    """Muda o HH:MM de um horário, recarimbando-o (mesma razão de
    `criar_horario`: editar 06:00 -> 07:00 às 09:00 não deve disparar uma
    varredura imediata só porque a nova ocorrência de hoje já passou)."""
    _validar_hora_minuto(hora, minuto)
    h = session.get(HorarioAgendado, horario_id)
    if h is None:
        raise ValueError(f"HorarioAgendado {horario_id!r} não encontrado")
    h.hora = hora
    h.minuto = minuto
    h.ultima_execucao = datetime.now(timezone.utc)
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


def horarios_devidos(
    session, tipo: str, agora_utc: datetime | None = None
) -> list[HorarioAgendado]:
    """Horários ATIVOS do `tipo` que estão DEVIDOS agora (modelo catch-up).

    Um horário está devido quando a sua ocorrência mais recente (hoje às HH:MM
    em BRT, ou ontem às HH:MM se HH:MM ainda não chegou hoje) é POSTERIOR ao
    seu `ultima_execucao`. `ultima_execucao` NULL significa "nunca executou" e
    portanto está sempre devido.

    A comparação é estrita (`ocorrência > ultima_execucao`): ter executado
    exatamente no instante da ocorrência já conta como executado, e um horário
    nunca dispara duas vezes pela mesma ocorrência.

    `agora_utc` é opcional (default: `datetime.now(timezone.utc)`) — passar um
    valor explícito é o que permite testar a virada de meia-noite e o poll
    atrasado de forma determinística. Ordenação: (hora, minuto), herdada de
    `listar_horarios`."""
    if agora_utc is None:
        agora_utc = datetime.now(timezone.utc)
    agora_utc = _como_utc_aware(agora_utc)

    devidos: list[HorarioAgendado] = []
    for h in listar_horarios(session, tipo):
        if h.ultima_execucao is None:
            devidos.append(h)  # nunca executou => devido
            continue
        ocorrencia = _ocorrencia_mais_recente_utc(h.hora, h.minuto, agora_utc)
        if ocorrencia > _como_utc_aware(h.ultima_execucao):
            devidos.append(h)
    return devidos


def marcar_executados(
    session, horarios: list[HorarioAgendado], agora_utc: datetime | None = None
) -> None:
    """Carimba `ultima_execucao = agora_utc` em cada horário da lista e commita.

    Chamado pelo worker DEPOIS de rodar a varredura/explore de verdade — é o
    que fecha o ciclo do catch-up e impede que a mesma ocorrência dispare de
    novo no próximo poll."""
    if not horarios:
        return  # nada a fazer — evita um commit inútil (round-trip no Neon)
    if agora_utc is None:
        agora_utc = datetime.now(timezone.utc)
    agora_utc = _como_utc_aware(agora_utc)

    for h in horarios:
        h.ultima_execucao = agora_utc
        # Reanexa objetos que venham de uma sessão já fechada (o worker lê os
        # horários numa sessão e pode gravar em outra); em objeto já presente
        # nesta sessão, `add` é no-op.
        session.add(h)
    session.commit()


def deve_rodar_agora(session, tipo: str, agora_utc: datetime | None = None) -> bool:
    """True se houver ALGUM horário devido do `tipo` — açúcar sobre
    `horarios_devidos`, mantido por compatibilidade com quem só precisa do
    sim/não. NÃO usa mais janela de tolerância: ver o modelo catch-up
    explicado no topo do módulo."""
    return bool(horarios_devidos(session, tipo, agora_utc))
