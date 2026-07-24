"""Worker do VooFacil — processo SEPARADO da UI Streamlit.

Roda a varredura periódica (APScheduler, 3x/dia com jitter) ou uma única vez:

    python -m voofacil.worker --once     # varre agora e sai (teste/manual)
    python -m voofacil.worker            # daemon com agendamento

NUNCA importar/rodar isto dentro do Streamlit (a página re-executa a cada
interação e criaria/mataria schedulers).
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select

from voofacil.alerts.dispatcher import canais_padrao, despachar
from voofacil.config import settings
from voofacil.db import SessionLocal, init_db
from voofacil.engine.confirmacao import confirmar_candidatos
from voofacil.engine.detector import avaliar_snapshot
from voofacil.engine.orcamento import ha_orcamento, registrar_chamada
from voofacil.engine.store import persistir_quotes
from voofacil.models import TipoWatch, Watch
from voofacil.providers.base import Provider
from voofacil.providers.serpapi_google import SerpApiProvider
from voofacil.providers.travelpayouts import TravelpayoutsProvider
from voofacil.schemas import Candidato

log = logging.getLogger("voofacil.worker")

# Horários das varreduras diárias (hora local) — espalhados p/ fisgar quedas
HORARIOS_VARREDURA = ["06:00", "12:30", "21:00"]
HORARIO_EXPLORE = "05:00"          # busca ampla (SerpApi Explore), 1x/dia — antes das varreduras
JITTER_SEGUNDOS = 900  # +/- 15 min para não bater sempre no mesmo horário
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
            novos = persistir_quotes(session, watch, quotes)
            total += len(novos)
            for snap in novos:
                candidato = avaliar_snapshot(session, watch, snap)
                if candidato:
                    resultado.candidatos.append(candidato)
        resultado.snapshots_por_watch[watch.id] = total
        log.info("Watch %s (%s): %s snapshots", watch.id, watch.nome, total)
    return resultado


def job_varredura() -> None:
    """Job agendado: sessão própria, commit no fim, erros logados."""
    with SessionLocal() as session:
        try:
            resultado = executar_varredura(session, providers=[TravelpayoutsProvider()])
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


def executar_explore(session, provider: SerpApiProvider) -> ResultadoVarredura:
    """Busca ampla diária: watches REGIAO com arrival_area_id via SerpApi Explore.

    Cada (watch, mês) consome 1 chamada do orçamento SerpApi — por isso é
    1x/dia e limitado a MESES_EXPLORE_A_FRENTE, não a cada varredura 3x/dia.
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
        for mes in _meses_a_frente(MESES_EXPLORE_A_FRENTE):
            if not ha_orcamento(session, "SERPAPI", settings.serpapi_orcamento_mes):
                log.warning("Orçamento SerpApi esgotado — explore do watch %s interrompido",
                            watch.id)
                break
            try:
                quotes = provider.explorar_regiao(watch.origem_iata, watch.arrival_area_id, mes)
                registrar_chamada(session, "SERPAPI")
            except Exception:  # noqa: BLE001 — um mês/watch com falha não derruba o resto
                log.exception("Falha no explore do watch %s (mes %s)", watch.id, mes)
                continue
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
    args = parser.parse_args()

    init_db()

    if args.once:
        job_varredura()
        return
    if args.once_explore:
        job_explore()
        return

    # Import tardio: APScheduler só é necessário no modo daemon
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BlockingScheduler()
    for horario in HORARIOS_VARREDURA:
        hora, minuto = horario.split(":")
        scheduler.add_job(
            job_varredura,
            CronTrigger(hour=int(hora), minute=int(minuto), jitter=JITTER_SEGUNDOS),
            id=f"varredura_{horario}",
            misfire_grace_time=3600,
        )
    hora, minuto = HORARIO_EXPLORE.split(":")
    scheduler.add_job(
        job_explore,
        CronTrigger(hour=int(hora), minute=int(minuto), jitter=JITTER_SEGUNDOS),
        id="explore_diario",
        misfire_grace_time=3600,
    )
    log.info("Worker agendado: varreduras %s + explore %s (jitter +/-%smin). Ctrl+C para sair.",
             ", ".join(HORARIOS_VARREDURA), HORARIO_EXPLORE, JITTER_SEGUNDOS // 60)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Worker encerrado")


if __name__ == "__main__":
    main()
