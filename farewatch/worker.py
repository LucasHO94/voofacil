"""Worker do FareWatch — processo SEPARADO da UI Streamlit.

Roda a varredura periódica (APScheduler, 3x/dia com jitter) ou uma única vez:

    python -m farewatch.worker --once     # varre agora e sai (teste/manual)
    python -m farewatch.worker            # daemon com agendamento

NUNCA importar/rodar isto dentro do Streamlit (a página re-executa a cada
interação e criaria/mataria schedulers).
"""
from __future__ import annotations

import argparse
import logging

from sqlalchemy import select

from farewatch.db import SessionLocal, init_db
from farewatch.engine.store import persistir_quotes
from farewatch.models import Watch
from farewatch.providers.base import Provider
from farewatch.providers.travelpayouts import TravelpayoutsProvider

log = logging.getLogger("farewatch.worker")

# Horários das varreduras diárias (hora local) — espalhados p/ fisgar quedas
HORARIOS_VARREDURA = ["06:00", "12:30", "21:00"]
JITTER_SEGUNDOS = 900  # +/- 15 min para não bater sempre no mesmo horário


def executar_varredura(session, providers: list[Provider]) -> dict[int, int]:
    """Varre todos os watches ativos com cada provider e persiste os snapshots.

    Retorna {watch_id: qtde de snapshots novos}. Commit fica a cargo do chamador.
    """
    resultado: dict[int, int] = {}
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
            total += len(persistir_quotes(session, watch, quotes))
        resultado[watch.id] = total
        log.info("Watch %s (%s): %s snapshots", watch.id, watch.nome, total)
    return resultado


def job_varredura() -> None:
    """Job agendado: sessão própria, commit no fim, erros logados."""
    with SessionLocal() as session:
        try:
            resultado = executar_varredura(session, providers=[TravelpayoutsProvider()])
            session.commit()
            log.info("Varredura concluída: %s watches, %s snapshots",
                     len(resultado), sum(resultado.values()))
        except Exception:  # noqa: BLE001
            session.rollback()
            log.exception("Varredura abortada")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Worker de varredura do FareWatch")
    parser.add_argument("--once", action="store_true",
                        help="executa uma varredura única e sai")
    args = parser.parse_args()

    init_db()

    if args.once:
        job_varredura()
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
    log.info("Worker agendado para %s (jitter +/-%smin). Ctrl+C para sair.",
             ", ".join(HORARIOS_VARREDURA), JITTER_SEGUNDOS // 60)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Worker encerrado")


if __name__ == "__main__":
    main()
