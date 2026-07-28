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
from voofacil.engine.filtros import atende_criterios, atende_criterios_descoberta
from voofacil.engine.orcamento import ha_orcamento_serpapi, registrar_chamada_serpapi
from voofacil.engine.store import persistir_quotes
from voofacil.models import TipoWatch, Watch
from voofacil.providers.base import Provider
from voofacil.providers.serpapi_google import SerpApiProvider
from voofacil.providers.travelpayouts import TravelpayoutsProvider
from voofacil.schedule_utils import dias_semana_apscheduler, parse_horarios
from voofacil.schemas import Candidato

log = logging.getLogger("voofacil.worker")

# Horários de varredura/explore agora vêm de settings.scan_horarios /
# settings.scan_dias_semana / settings.explore_horario (voofacil/config.py),
# lidos dinamicamente dentro de main() via voofacil.schedule_utils — nada
# mais fica hardcoded aqui (ver histórico do arquivo para os valores antigos).
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

    horarios = parse_horarios(settings.scan_horarios)
    dias_semana = dias_semana_apscheduler(settings.scan_dias_semana)
    hora_explore, minuto_explore = parse_horarios(settings.explore_horario)[0]

    scheduler = BlockingScheduler()
    for hora, minuto in horarios:
        scheduler.add_job(
            job_varredura,
            CronTrigger(hour=hora, minute=minuto, day_of_week=dias_semana, jitter=JITTER_SEGUNDOS),
            id=f"varredura_{hora:02d}{minuto:02d}",
            misfire_grace_time=3600,
        )
    scheduler.add_job(
        job_explore,
        CronTrigger(hour=hora_explore, minute=minuto_explore, day_of_week=dias_semana,
                    jitter=JITTER_SEGUNDOS),
        id="explore_diario",
        misfire_grace_time=3600,
    )
    log.info(
        "Worker agendado: varreduras %s + explore %02d:%02d (dias=%s, jitter +/-%smin). Ctrl+C para sair.",
        ", ".join(f"{h:02d}:{m:02d}" for h, m in horarios),
        hora_explore, minuto_explore, dias_semana, JITTER_SEGUNDOS // 60,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Worker encerrado")


if __name__ == "__main__":
    main()
