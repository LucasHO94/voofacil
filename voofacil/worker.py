"""Worker do VooFacil — processo SEPARADO da UI Streamlit.

Roda o agendamento DINÂMICO (via banco, editável pelo painel Streamlit) ou
uma ação única/manual:

    python -m voofacil.worker --once          # varredura agora e sai (teste/manual)
    python -m voofacil.worker --once-explore  # busca ampla agora e sai (teste/manual)
    python -m voofacil.worker --auto          # decide via banco (job_auto) e sai — usado pelo poll
    python -m voofacil.worker                 # daemon: poll recorrente chamando job_auto()

Antes, os horários (settings.scan_horarios/explore_horario, .env) eram fixos
e agendados via CronTrigger direto no processo. Agora eles moram no banco
(HorarioAgendado, ver voofacil/engine/agendamento.py) e tanto o daemon local
quanto a nuvem (GitHub Actions, ver .github/workflows/worker.yml) fazem
POLL de frequência fixa e curta (settings.agendamento_poll_minutos) chamando
job_auto(), que decide internamente (deve_rodar_agora) se é hora de varrer
e/ou explorar de verdade. --once/--once-explore continuam existindo tal
qual antes, sem depender do banco, para teste manual e para o botão "Varrer
agora" do painel.

NUNCA importar/rodar isto dentro do Streamlit (a página re-executa a cada
interação e criaria/mataria schedulers).
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from voofacil.alerts.dispatcher import canais_padrao, despachar
from voofacil.config import settings
from voofacil.db import SessionLocal, init_db
from voofacil.engine.agendamento import deve_rodar_agora, garantir_horarios_padrao
from voofacil.engine.confirmacao import confirmar_candidatos
from voofacil.engine.detector import avaliar_snapshot
from voofacil.engine.filtros import atende_criterios, atende_criterios_descoberta
from voofacil.engine.orcamento import ha_orcamento_serpapi, registrar_chamada_serpapi
from voofacil.engine.store import persistir_quotes
from voofacil.models import TipoHorario, TipoWatch, Watch
from voofacil.providers.base import Provider
from voofacil.providers.serpapi_google import SerpApiProvider
from voofacil.providers.travelpayouts import TravelpayoutsProvider
from voofacil.schedule_utils import dias_semana_apscheduler
from voofacil.schemas import Candidato

log = logging.getLogger("voofacil.worker")

# BRT = UTC-3 (Brasil não usa mais horário de verão desde 2019) — mesma
# convenção de voofacil/engine/agendamento.py e voofacil/schedule_utils.py.
_OFFSET_UTC_PARA_BRT_HORAS = 3
# Ordem mon..sun == Python datetime.weekday() (Monday=0..Sunday=6) — usada só
# para checar scan_dias_semana em _dia_permitido_hoje (a lista CSV vem de
# voofacil.schedule_utils.dias_semana_apscheduler, que já valida o formato).
_DIAS_PY_WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

JITTER_SEGUNDOS = 60  # pequeno jitter no poll do daemon local (o poll já é frequente)
MESES_EXPLORE_A_FRENTE = 3         # quantos meses da janela do watch explorar por dia


@dataclass
class ResultadoVarredura:
    snapshots_por_watch: dict[int, int] = field(default_factory=dict)
    candidatos: list[Candidato] = field(default_factory=list)


def executar_varredura(session, providers: list[Provider]) -> ResultadoVarredura:
    """Varre os watches ativos, persiste snapshots e avalia gatilhos de alerta.

    Commit fica a cargo do chamador.
    """
    resultado = ResultadoVarredura()
    watches = session.scalars(select(Watch).where(Watch.ativo)).all()
    if not watches:
        log.info("Nenhum watch ativo — nada a varrer")
        return resultado

    for watch in watches:
        total = 0
        for provider in providers:
            try:
                quotes = provider.sweep(watch)
            except Exception:  # noqa: BLE001 — um provider caído não derruba o resto
                log.exception("Provider %s falhou no watch %s", provider.nome, watch.id)
                continue
            quotes = [q for q in quotes if atende_criterios(watch, q)]
            novos = persistir_quotes(session, watch, quotes)
            total += len(novos)
            for snap in novos:
                candidato = avaliar_snapshot(session, watch, snap)
                if candidato:
                    resultado.candidatos.append(candidato)
        resultado.snapshots_por_watch[watch.id] = total
        log.info("Watch %s (%s): %s snapshots", watch.id, watch.nome, total)
    return resultado


def _providers_padrao(session, serpapi: SerpApiProvider | None = None) -> list[Provider]:
    """Providers usados na varredura regular (3x/dia).

    A Travelpayouts (cache) é sempre incluída — é a fonte barata/de volume.
    A SerpApi (Google Flights) entra também quando está configurada (API
    key presente) E há orçamento disponível (mensal E diário, ver
    voofacil/engine/orcamento.py) — dando ao usuário, para
    watches ROTA, uma segunda cotação AO VIVO na MESMA varredura ("estilo
    Skyscanner": duas fontes independentes lado a lado). Antes desta
    integração, a SerpApi só era usada para confirmar candidatos já
    aprovados pelo detector e no Explore diário — ver SerpApiProvider.sweep().
    `serpapi` é injetável para testes.
    """
    providers: list[Provider] = [TravelpayoutsProvider()]
    serpapi = serpapi or SerpApiProvider()
    if serpapi.configurado and ha_orcamento_serpapi(session):
        providers.append(serpapi)
    return providers


def job_varredura() -> None:
    """Job agendado: sessão própria, commit no fim, erros logados."""
    with SessionLocal() as session:
        try:
            resultado = executar_varredura(session, providers=_providers_padrao(session))
            confirmados = confirmar_candidatos(session, resultado.candidatos)
            enviados = despachar(session, confirmados, canais_padrao())
            session.commit()
            log.info(
                "Varredura concluída: %s watches, %s snapshots, %s candidatos, %s alertas",
                len(resultado.snapshots_por_watch),
                sum(resultado.snapshots_por_watch.values()),
                len(resultado.candidatos),
                len(enviados),
            )
        except Exception:  # noqa: BLE001
            session.rollback()
            log.exception("Varredura abortada")


def _meses_a_frente(qtd: int, hoje: date | None = None) -> list[int]:
    """[9, 10, 11] a partir do mês corrente — parâmetro `month` do Explore."""
    hoje = hoje or date.today()
    return [((hoje.month - 1 + i) % 12) + 1 for i in range(qtd)]


def _meses_para_explorar(watch: Watch, qtd: int, hoje: date | None = None) -> list[int]:
    """Meses a varrer no Explore: só o mês do watch se mes_especifico estiver
    setado (evita gastar orçamento SerpApi em meses que atende_criterios só
    ia descartar depois); senão os próximos `qtd` meses, como antes."""
    if watch.mes_especifico:
        return [watch.mes_especifico]
    return _meses_a_frente(qtd, hoje)


def executar_explore(session, provider: SerpApiProvider) -> ResultadoVarredura:
    """Busca ampla diária: watches REGIAO com arrival_area_id via SerpApi Explore.

    Cada (watch, mês) consome 1 chamada do orçamento SerpApi (mensal E
    diário) — por isso é 1x/dia e limitado a MESES_EXPLORE_A_FRENTE, não a
    cada varredura 3x/dia.
    """
    resultado = ResultadoVarredura()
    watches = session.scalars(
        select(Watch).where(
            Watch.ativo,
            Watch.tipo == TipoWatch.REGIAO.value,
            Watch.arrival_area_id.is_not(None),
        )
    ).all()
    if not watches:
        return resultado

    for watch in watches:
        total = 0
        for mes in _meses_para_explorar(watch, MESES_EXPLORE_A_FRENTE):
            if not ha_orcamento_serpapi(session):
                log.warning("Orçamento SerpApi esgotado (mensal ou diário) — explore do watch %s interrompido",
                            watch.id)
                break
            try:
                quotes = provider.explorar_regiao(watch.origem_iata, watch.arrival_area_id, mes)
                registrar_chamada_serpapi(session)
            except Exception:  # noqa: BLE001 — um mês/watch com falha não derruba o resto
                log.exception("Falha no explore do watch %s (mes %s)", watch.id, mes)
                continue
            quotes = [q for q in quotes if atende_criterios_descoberta(watch, q)]
            novos = persistir_quotes(session, watch, quotes)
            total += len(novos)
            for snap in novos:
                candidato = avaliar_snapshot(session, watch, snap)
                if candidato:
                    resultado.candidatos.append(candidato)
        resultado.snapshots_por_watch[watch.id] = total
        log.info("Explore watch %s (%s): %s destinos descobertos", watch.id, watch.nome, total)
    return resultado


def job_explore() -> None:
    """Job diário de busca ampla (separado da varredura 3x/dia)."""
    provider = SerpApiProvider()
    if not provider.configurado:
        log.info("SerpApi não configurada — pulando explore diário")
        return
    with SessionLocal() as session:
        try:
            resultado = executar_explore(session, provider)
            confirmados = confirmar_candidatos(session, resultado.candidatos, provider)
            enviados = despachar(session, confirmados, canais_padrao())
            session.commit()
            log.info(
                "Explore concluído: %s watches, %s destinos, %s alertas",
                len(resultado.snapshots_por_watch),
                sum(resultado.snapshots_por_watch.values()),
                len(enviados),
            )
        except Exception:  # noqa: BLE001
            session.rollback()
            log.exception("Explore abortado")


def _dia_permitido_hoje(agora_utc: datetime | None = None) -> bool:
    """True se o dia de hoje (data BRT) está entre os dias permitidos por
    `settings.scan_dias_semana` ("*" = todos os dias, ou CSV mon..sun).

    `agora_utc` é opcional (default: `datetime.now(timezone.utc)`) — só
    existe para permitir teste determinístico, mesmo padrão de
    `deve_rodar_agora` em voofacil/engine/agendamento.py. Dia da semana
    continua vindo do `.env` (não é editável pelo painel) — é a única parte
    do agendamento antigo que segue estática de propósito.
    """
    dias = dias_semana_apscheduler(settings.scan_dias_semana)
    if dias == "*":
        return True

    if agora_utc is None:
        agora_utc = datetime.now(timezone.utc)
    agora_brt = agora_utc - timedelta(hours=_OFFSET_UTC_PARA_BRT_HORAS)
    dia_hoje = _DIAS_PY_WEEKDAY[agora_brt.weekday()]
    return dia_hoje in dias.split(",")


def job_auto(agora_utc: datetime | None = None) -> None:
    """Job único do poll de frequência fixa (chamado a cada
    `settings.agendamento_poll_minutos`, tanto pelo GitHub Actions quanto
    pelo daemon local) — decide via banco se é hora de varrer e/ou explorar
    agora, sem gastar nenhuma chamada de API quando não é.

    As duas checagens (VARREDURA/EXPLORE) são independentes: se os horários
    coincidirem, as duas disparam na mesma chamada. `garantir_horarios_padrao`
    e `deve_rodar_agora` rodam numa sessão PRÓPRIA, só de leitura/seed, que é
    fechada ANTES de chamar `job_varredura()`/`job_explore()` — essas duas
    abrem cada uma a sua própria sessão internamente.

    `agora_utc` é opcional (default: `datetime.now(timezone.utc)`) — só
    existe para permitir teste determinístico (mesmo padrão de
    `deve_rodar_agora`).
    """
    if agora_utc is None:
        agora_utc = datetime.now(timezone.utc)

    if not _dia_permitido_hoje(agora_utc):
        log.info(
            "Hoje não é dia permitido por scan_dias_semana=%s — nada agendado",
            settings.scan_dias_semana,
        )
        return

    with SessionLocal() as session:
        garantir_horarios_padrao(session)
        rodar_varredura = deve_rodar_agora(session, TipoHorario.VARREDURA.value, agora_utc)
        rodar_explore = deve_rodar_agora(session, TipoHorario.EXPLORE.value, agora_utc)

    if not rodar_varredura and not rodar_explore:
        log.info("nada agendado para agora")
        return

    if rodar_varredura:
        job_varredura()
    if rodar_explore:
        job_explore()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Worker de varredura do VooFacil")
    parser.add_argument("--once", action="store_true",
                        help="executa uma varredura única (Travelpayouts + confirmação) e sai")
    parser.add_argument("--once-explore", action="store_true",
                        help="executa uma rodada única de busca ampla (SerpApi Explore) e sai")
    parser.add_argument("--auto", action="store_true",
                        help="decide via banco (job_auto/deve_rodar_agora) se varre e/ou explora "
                             "agora e sai — usado pelo poll do GitHub Actions/daemon local")
    args = parser.parse_args()

    init_db()

    if args.once:
        job_varredura()
        return
    if args.once_explore:
        job_explore()
        return
    if args.auto:
        job_auto()
        return

    # Import tardio: APScheduler só é necessário no modo daemon
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler()
    scheduler.add_job(
        job_auto,
        IntervalTrigger(minutes=settings.agendamento_poll_minutos, jitter=JITTER_SEGUNDOS),
        id="poll_auto",
        misfire_grace_time=3600,
    )
    log.info(
        "Worker em modo poll: a cada %s min chama job_auto(), que decide via banco "
        "(HorarioAgendado, editável pelo painel) se varre e/ou explora agora "
        "(dias=%s vindo do .env, jitter +/-%ss). Ctrl+C para sair.",
        settings.agendamento_poll_minutos, settings.scan_dias_semana, JITTER_SEGUNDOS,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Worker encerrado")


if __name__ == "__main__":
    main()
