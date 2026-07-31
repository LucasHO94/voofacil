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
job_auto(), que decide internamente se é hora de varrer e/ou explorar de
verdade. --once/--once-explore continuam existindo tal qual antes, sem
depender do banco, para teste manual e para o botão "Varrer agora" do painel.

MODELO CATCH-UP (recuperação) — job_auto NÃO usa mais janela de tolerância.
O poll da nuvem não é confiável: medindo o histórico deste repo, dos 96
disparos/dia prometidos pelo cron '*/15' o GitHub Actions entrega de 4 a 7,
em horários imprevisíveis. Perguntar "o poll caiu a ±7 min de algum horário
agendado?" dava não quase sempre — resultado medido: ZERO varreduras em 4
dias. A pergunta certa é "algum horário PASSOU desde a última vez que rodei?"
(igual ao Persistent=true do systemd-timer). O fluxo de job_auto é, por tipo:

    horarios_devidos()  ->  roda o job UMA vez  ->  marcar_executados()

A ORDEM é deliberada: marca-se DEPOIS de rodar. Marcar antes daria semântica
at-most-once (se o processo morresse no meio, aquele horário estaria perdido
pelo resto do dia); marcar depois dá at-least-once, que é o correto aqui —
o pior caso vira uma varredura repetida, não uma varredura perdida.

Com isso, a frequência do poll afeta só a LATÊNCIA (quão rápido um horário
atrasado é recuperado), nunca a CORRETUDE: se o horário das 09:00 passou e o
próximo disparo real só chega às 11:37, a varredura acontece às 11:37.

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
from voofacil.engine.agendamento import (
    garantir_horarios_padrao,
    horarios_devidos,
    listar_horarios,
    marcar_executados,
)
from voofacil.engine.confirmacao import confirmar_candidatos
from voofacil.engine.detector import avaliar_snapshot
from voofacil.engine.filtros import atende_criterios, atende_criterios_descoberta
from voofacil.engine.orcamento import ha_orcamento_serpapi, registrar_chamada_serpapi
from voofacil.engine.store import persistir_quotes
from voofacil.models import HorarioAgendado, TipoHorario, TipoWatch, Watch
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
# Teto de segurança de candidatos por watch numa MESMA varredura (ver
# deduplicar_candidatos). 3 é folgado para um bot pessoal e cabe com sobra no
# teto diário da SerpApi (settings.serpapi_orcamento_dia, padrão 8), que é
# consumido também pelo sweep multi-fonte e pelo Explore.
TETO_CANDIDATOS_POR_WATCH = 3


@dataclass
class ResultadoVarredura:
    snapshots_por_watch: dict[int, int] = field(default_factory=dict)
    candidatos: list[Candidato] = field(default_factory=list)


def _chave_viagem(cand: Candidato) -> tuple:
    """Identidade da VIAGEM de um candidato — o que a deduplicação trata como
    "o mesmo": watch, rota e as duas datas."""
    q = cand.quote
    return (cand.watch_id, q.origem, q.destino, q.data_ida, q.data_volta)


def _resumo_candidatos(candidatos: list[Candidato]) -> str:
    """Linha legível de candidatos para log ("LIS 2027-05-07/2027-05-22 R$ 3200 (LIMITE, TP_CACHE)")."""
    return "; ".join(
        f"{c.quote.origem}->{c.quote.destino} {c.quote.data_ida}"
        f"{'/' + str(c.quote.data_volta) if c.quote.data_volta else ''}"
        f" R$ {c.quote.preco} ({c.gatilho}, {c.quote.fonte})"
        for c in candidatos
    )


def deduplicar_candidatos(
    candidatos: list[Candidato], teto_por_watch: int = TETO_CANDIDATOS_POR_WATCH
) -> list[Candidato]:
    """Reduz os candidatos de UMA varredura antes de confirmar/alertar.

    Por que existe: `avaliar_snapshot` consulta o cooldown na tabela `alerta`,
    que só é escrita lá na frente por `despachar` — dentro da mesma varredura
    não havia deduplicação nenhuma, cada snapshot virava um candidato
    independente. Medido: watch GRU->LIS com limite 3500 e um mês de calendar
    (30 cotações), banco vazio antes => 10 candidatos numa única varredura,
    todos LIMITE. Isso seriam 10 chamadas SerpApi (o teto diário é 8) e 10
    mensagens POR CANAL. Num watch REGIAO com 18 destinos escala para centenas.

    Duas etapas:
    1. mesma viagem (`_chave_viagem`) coletada mais de uma vez na rodada — o
       caso típico é a mesma rota/dia vindo de TP_CACHE e de SERPAPI no sweep
       multi-fonte — colapsa no candidato MAIS BARATO;
    2. teto de segurança por watch: sobram os `teto_por_watch` mais baratos.

    O corte NUNCA é silencioso: o que ficou de fora sai no log com rota, datas,
    preço e gatilho, para que "não recebi alerta daquele dia" tenha resposta.
    """
    if not candidatos:
        return []

    melhores: dict[tuple, Candidato] = {}
    for cand in candidatos:
        chave = _chave_viagem(cand)
        atual = melhores.get(chave)
        if atual is None or cand.quote.preco < atual.quote.preco:
            melhores[chave] = cand
    if len(melhores) < len(candidatos):
        log.info(
            "Deduplicação de candidatos: %s -> %s (mesma viagem vista mais de uma "
            "vez na rodada; ficou a cotação mais barata de cada uma)",
            len(candidatos), len(melhores),
        )

    por_watch: dict[int, list[Candidato]] = {}
    for cand in melhores.values():
        por_watch.setdefault(cand.watch_id, []).append(cand)

    resultado: list[Candidato] = []
    for watch_id in sorted(por_watch):
        ordenados = sorted(
            por_watch[watch_id], key=lambda c: (c.quote.preco, c.quote.data_ida)
        )
        if len(ordenados) > teto_por_watch:
            cortados = ordenados[teto_por_watch:]
            log.warning(
                "Teto de %s candidato(s) por varredura atingido no watch %s — %s "
                "candidato(s) mais caros NÃO viraram alerta: %s",
                teto_por_watch, watch_id, len(cortados), _resumo_candidatos(cortados),
            )
            ordenados = ordenados[:teto_por_watch]
        resultado.extend(ordenados)
    return resultado


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
    resultado.candidatos = deduplicar_candidatos(resultado.candidatos)
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


def job_varredura() -> bool:
    """Job agendado: sessão própria, commit no fim, erros logados.

    Devolve True se a varredura concluiu e False se abortou. Quem chama
    (job_auto) usa isso para NÃO carimbar o horário como executado quando a
    rodada falhou — senão uma API fora do ar consumiria a ocorrência do dia
    e o horário só voltaria a ser tentado no dia seguinte.
    """
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
            return True
        except Exception:  # noqa: BLE001
            session.rollback()
            log.exception("Varredura abortada")
            return False


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
    resultado.candidatos = deduplicar_candidatos(resultado.candidatos)
    return resultado


def job_explore() -> bool:
    """Job diário de busca ampla (separado da varredura 3x/dia).

    Devolve True se concluiu e False se abortou (mesma semântica de
    job_varredura — ver docstring de lá). SerpApi não configurada conta como
    concluído: não é falha transitória, tentar de novo no próximo poll não
    mudaria nada.
    """
    provider = SerpApiProvider()
    if not provider.configurado:
        log.info("SerpApi não configurada — pulando explore diário")
        return True
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
            return True
        except Exception:  # noqa: BLE001
            session.rollback()
            log.exception("Explore abortado")
            return False


def _dia_permitido_hoje(agora_utc: datetime | None = None) -> bool:
    """True se o dia de hoje (data BRT) está entre os dias permitidos por
    `settings.scan_dias_semana` ("*" = todos os dias, ou CSV mon..sun).

    `agora_utc` é opcional (default: `datetime.now(timezone.utc)`) — só
    existe para permitir teste determinístico, mesmo padrão de
    `horarios_devidos` em voofacil/engine/agendamento.py. Dia da semana
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


def _hhmm(horario: HorarioAgendado) -> str:
    """"06:00" — hora/minuto do HorarioAgendado (que são BRT) para log."""
    return f"{horario.hora:02d}:{horario.minuto:02d}"


def _resumo_horarios(horarios: list[HorarioAgendado]) -> str:
    """Diagnóstico legível de um conjunto de horários ativos, com a última
    execução de cada um — ex.: "06:00 (última exec. 2026-07-24 11:37 UTC),
    12:30 (nunca executou)".

    Só é usado no log do caso "nada devido". A automação já ficou 4 dias morta
    logando apenas "nada agendado para agora", sem nenhum dado para investigar;
    esta string existe para que o próximo diagnóstico saia de um log só."""
    if not horarios:
        return "nenhum horário ativo"
    partes = []
    for h in horarios:
        if h.ultima_execucao is None:
            quando = "nunca executou"
        else:
            # Normaliza antes de rotular "UTC": o psycopg devolve o valor no fuso
            # da SESSÃO do Postgres, então imprimir cru com o sufixo fixo faria o
            # log mentir a hora se esse fuso não fosse UTC — justamente no log que
            # existe para diagnosticar agendamento morto. Naive == UTC, mesma
            # convenção de engine/agendamento._como_utc_aware.
            bruto = h.ultima_execucao
            em_utc = (bruto.replace(tzinfo=timezone.utc) if bruto.tzinfo is None
                      else bruto.astimezone(timezone.utc))
            quando = f"última exec. {em_utc:%Y-%m-%d %H:%M} UTC"
        partes.append(f"{_hhmm(h)} ({quando})")
    return ", ".join(partes)


def _marcar_executados_por_id(ids: list[int], agora_utc: datetime) -> None:
    """Carimba `ultima_execucao` nos horários de `ids` numa sessão NOVA.

    Por que por ID e não pelos objetos lidos antes: os HorarioAgendado são
    lidos numa sessão que é fechada ANTES de rodar job_varredura()/job_explore()
    (para não segurar conexão ociosa do Neon durante uma varredura que pode
    levar minutos). Usar aqueles objetos destacados arriscaria
    DetachedInstanceError; recarregá-los por ID mantém o padrão de cada job
    abrir a própria sessão e ainda pega o estado mais recente do banco.

    Um horário excluído pelo painel entre a leitura e a marcação simplesmente
    some da lista (`session.get` devolve None) — não é erro."""
    if not ids:
        return
    with SessionLocal() as session:
        horarios = [h for h in (session.get(HorarioAgendado, i) for i in ids) if h is not None]
        marcar_executados(session, horarios, agora_utc)


def job_auto(agora_utc: datetime | None = None) -> None:
    """Job único do poll de frequência fixa (chamado a cada
    `settings.agendamento_poll_minutos`, tanto pelo GitHub Actions quanto
    pelo daemon local) — decide via banco se há varredura e/ou explore
    DEVIDOS, sem gastar nenhuma chamada de API quando não há.

    Modelo catch-up (ver docstring do módulo): para cada tipo pega-se a lista
    de horários devidos (`horarios_devidos`); se a lista não estiver vazia,
    roda-se o job UMA vez — independente de quantos horários estejam devidos,
    porque uma varredura já cobre todos eles — e só DEPOIS marca-se a lista
    inteira como executada (`marcar_executados`), E SÓ SE o job tiver
    CONCLUÍDO (job_varredura/job_explore devolvem bool). Essas duas condições
    juntas é que dão at-least-once de verdade: se o processo morrer no meio,
    ou se a rodada abortar (API fora do ar, banco indisponível), o horário
    continua devido e é retentado no próximo poll, em vez de ser consumido
    por uma execução que não coletou nada.

    As duas checagens (VARREDURA/EXPLORE) são independentes: se ambas
    estiverem devidas, as duas disparam na mesma chamada.
    `garantir_horarios_padrao` e `horarios_devidos` rodam numa sessão PRÓPRIA,
    só de leitura/seed, fechada ANTES de chamar `job_varredura()`/
    `job_explore()` — essas duas abrem cada uma a sua própria sessão
    internamente, e a marcação abre mais uma (ver `_marcar_executados_por_id`).

    `agora_utc` é opcional (default: `datetime.now(timezone.utc)`) — só
    existe para permitir teste determinístico (mesmo padrão de
    `horarios_devidos`).
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
        devidos_varredura = horarios_devidos(session, TipoHorario.VARREDURA.value, agora_utc)
        devidos_explore = horarios_devidos(session, TipoHorario.EXPLORE.value, agora_utc)
        # Só os IDs e os rótulos atravessam o fim da sessão — os objetos ficam
        # para trás de propósito (ver _marcar_executados_por_id).
        ids_varredura = [h.id for h in devidos_varredura]
        ids_explore = [h.id for h in devidos_explore]
        rotulos_varredura = ", ".join(_hhmm(h) for h in devidos_varredura)
        rotulos_explore = ", ".join(_hhmm(h) for h in devidos_explore)

        if not ids_varredura and not ids_explore:
            # Caminho mais comum (a maioria dos polls não tem nada a fazer):
            # loga o estado completo do agendamento, senão um agendamento morto
            # fica indistinguível de um agendamento ocioso.
            log.info(
                "Nada devido agora (catch-up, agora=%s UTC) — VARREDURA: %s | EXPLORE: %s",
                agora_utc.strftime("%Y-%m-%d %H:%M"),
                _resumo_horarios(listar_horarios(session, TipoHorario.VARREDURA.value)),
                _resumo_horarios(listar_horarios(session, TipoHorario.EXPLORE.value)),
            )
            return

    if ids_varredura:
        log.info("Varredura devida — recuperando horário(s) %s (BRT)", rotulos_varredura)
        if job_varredura():                                   # só carimba se CONCLUIU
            _marcar_executados_por_id(ids_varredura, agora_utc)
        else:
            log.warning(
                "Varredura abortada — horário(s) %s continuam devidos e serão "
                "retentados no próximo poll", rotulos_varredura,
            )
    if ids_explore:
        log.info("Explore devido — recuperando horário(s) %s (BRT)", rotulos_explore)
        if job_explore():
            _marcar_executados_por_id(ids_explore, agora_utc)
        else:
            log.warning(
                "Explore abortado — horário(s) %s continuam devidos e serão "
                "retentados no próximo poll", rotulos_explore,
            )


def _silenciar_log_de_url() -> None:
    """Impede que a chave da SerpApi apareça nos logs.

    O httpx registra cada requisição em INFO com a URL COMPLETA, e a SerpApi
    recebe a credencial como query string (`?...&api_key=...`) — não como
    header. Com `basicConfig(level=INFO)` isso imprime a chave em texto claro
    em toda varredura. Em produção o worker roda no GitHub Actions de um
    repositório PÚBLICO: o mascaramento automático de secrets do Actions ajuda,
    mas depende de casamento exato da string e não é uma garantia sobre a qual
    valha a pena apostar uma credencial.

    WARNING mantém erros de rede visíveis (que é o que se quer depurar) e
    descarta a linha de request que carrega a chave.
    """
    for nome in ("httpx", "httpcore"):
        logging.getLogger(nome).setLevel(logging.WARNING)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _silenciar_log_de_url()
    parser = argparse.ArgumentParser(description="Worker de varredura do VooFacil")
    parser.add_argument("--once", action="store_true",
                        help="executa uma varredura única (Travelpayouts + confirmação) e sai")
    parser.add_argument("--once-explore", action="store_true",
                        help="executa uma rodada única de busca ampla (SerpApi Explore) e sai")
    parser.add_argument("--auto", action="store_true",
                        help="decide via banco (job_auto/horarios_devidos) se há varredura e/ou "
                             "explore DEVIDOS — inclusive horários atrasados, modelo catch-up — "
                             "roda e sai; usado pelo poll do GitHub Actions/daemon local")
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
        "(HorarioAgendado, editável pelo painel) se há horário DEVIDO — inclusive "
        "atrasado, modelo catch-up: o poll define só a latência da recuperação "
        "(dias=%s vindo do .env, jitter +/-%ss). Ctrl+C para sair.",
        settings.agendamento_poll_minutos, settings.scan_dias_semana, JITTER_SEGUNDOS,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Worker encerrado")


if __name__ == "__main__":
    main()
