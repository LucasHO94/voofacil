"""Utilitários de agendamento — convertem a config (`.env`) em horários/triggers
usáveis pelo APScheduler (modo daemon do worker.py), pelo Windows Task Scheduler
(scripts/instalar_tarefas_windows.ps1) e pelo cron do GitHub Actions (nuvem).

Sobre o PADRÃO de horários (`scan_horarios` em voofacil/config.py): madrugada
(06:00), meio-dia (12:30) e noite (21:00) — espalhados ao longo do dia. Isso
NÃO é porque exista evidência forte de que uma hora específica do dia tenha
preço menor (não há). O que a pesquisa confirmou é que DIA DA SEMANA e
ANTECEDÊNCIA da compra importam mais do que a hora exata da varredura. O
espalhamento serve só para "fisgar" quedas de preço que aparecem e
desaparecem ao longo do dia, e para não concentrar todas as chamadas de API
num único horário.

LIMITAÇÃO conhecida (documentada, não contornada): o `on: schedule: cron` do
GitHub Actions é YAML ESTÁTICO — é lido pelo GitHub na hora de agendar o
workflow, não em tempo de execução, então não dá pra apontar pra
`voofacil.config.settings` e ficar 100% dinâmico na nuvem sem editar o
arquivo .github/workflows/worker.yml. `gerar_cron_actions()` (e o script
scripts/gerar_cron_actions.py) existem pra facilitar esse ajuste manual: eles
geram, a partir da MESMA config usada localmente, as linhas de cron já
convertidas para UTC prontas para colar no YAML.

Localmente (daemon do worker.py e tarefas do Windows Task Scheduler) o
agendamento É dinâmico de verdade: lê `settings.scan_horarios` /
`settings.scan_dias_semana` / `settings.explore_horario` em runtime.
"""
from __future__ import annotations

# Dias da semana em ordem ISO (segunda primeiro) — usado para deslocar o dia
# quando a conversão BRT -> UTC vira a virada da meia-noite.
_DIAS_ORDEM = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DIAS_VALIDOS = set(_DIAS_ORDEM)

# Numeração de dia-da-semana exigida pelo campo cron (POSIX): domingo = 0.
_CRON_DOW_NUM = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

# BRT = UTC-3 (Brasil não usa mais horário de verão desde 2019).
_OFFSET_BRT_PARA_UTC_MIN = 3 * 60


def parse_horarios(csv: str) -> list[tuple[int, int]]:
    """Converte um CSV "HH:MM,HH:MM,..." em [(hora, minuto), ...].

    Aceita espaços em volta de cada item (ex.: "06:00, 12:30 , 21:00") e
    qualquer quantidade de horários (1 ou N). Levanta ValueError se a string
    for vazia/None ou se algum item não estiver no formato HH:MM válido
    (hora 00-23, minuto 00-59).
    """
    if not csv or not csv.strip():
        raise ValueError("scan_horarios vazio — informe ao menos um horário HH:MM")

    horarios: list[tuple[int, int]] = []
    for item in csv.split(","):
        bruto = item.strip()
        if not bruto:
            continue
        partes = bruto.split(":")
        if len(partes) != 2 or not all(p.isdigit() for p in partes):
            raise ValueError(f"Horário inválido: {bruto!r} — use o formato HH:MM")
        hora, minuto = int(partes[0]), int(partes[1])
        if not (0 <= hora <= 23 and 0 <= minuto <= 59):
            raise ValueError(f"Horário fora do intervalo válido: {bruto!r}")
        horarios.append((hora, minuto))

    if not horarios:
        raise ValueError("scan_horarios vazio — informe ao menos um horário HH:MM")
    return horarios


def dias_semana_apscheduler(valor: str) -> str:
    """Valida/normaliza `valor` para uso em `CronTrigger(day_of_week=...)`.

    Aceita "*" (todos os dias, o padrão) ou uma lista CSV de abreviações em
    inglês, case-insensitive e com espaços tolerados: mon, tue, wed, thu,
    fri, sat, sun (mesmos nomes que o APScheduler já entende nativamente).
    Levanta ValueError em entrada vazia ou com dia desconhecido.
    """
    if valor is None:
        raise ValueError("scan_dias_semana não pode ser None")

    bruto = valor.strip()
    if bruto == "*":
        return "*"
    if not bruto:
        raise ValueError("scan_dias_semana vazio — use '*' para todos os dias")

    dias: list[str] = []
    for item in bruto.split(","):
        dia = item.strip().lower()
        if not dia:
            continue
        if dia not in _DIAS_VALIDOS:
            raise ValueError(
                f"Dia da semana inválido: {item.strip()!r} — use '*' ou combine "
                f"{', '.join(_DIAS_ORDEM)} separados por vírgula"
            )
        dias.append(dia)

    if not dias:
        raise ValueError("scan_dias_semana vazio — use '*' para todos os dias")
    return ",".join(dias)


def _brt_para_utc(hora: int, minuto: int) -> tuple[int, int, int]:
    """(hora_utc, minuto_utc, dias_a_frente) a partir de um horário em BRT.

    `dias_a_frente` é 0 ou 1 — só vira 1 quando o horário BRT cai entre
    21:00 e 23:59 (ex.: 21:00 BRT -> 00:00 UTC do dia seguinte).
    """
    total_min = hora * 60 + minuto + _OFFSET_BRT_PARA_UTC_MIN
    dias_a_frente, resto_min = divmod(total_min, 24 * 60)
    hora_utc, minuto_utc = divmod(resto_min, 60)
    return hora_utc, minuto_utc, dias_a_frente


def _dow_field_utc(dias_semana_norm: str, deslocamento_dias: int) -> str:
    """Campo day-of-week do cron (POSIX, dom=0) já ajustado pra virada de dia.

    Se `dias_semana_norm` for "*", o resultado é sempre "*" (deslocar todos
    os dias por N ainda resulta no conjunto de todos os dias). Caso
    contrário, cada dia é deslocado `deslocamento_dias` posições antes de
    virar número — isso é o que garante que uma varredura de segunda-feira
    às 21:00 BRT (que cai terça de madrugada em UTC) gere um cron com dia
    "terça", não "segunda".
    """
    if dias_semana_norm == "*":
        return "*"

    numeros: set[int] = set()
    for dia in dias_semana_norm.split(","):
        idx = _DIAS_ORDEM.index(dia)
        dia_deslocado = _DIAS_ORDEM[(idx + deslocamento_dias) % 7]
        numeros.add(_CRON_DOW_NUM[dia_deslocado])
    return ",".join(str(n) for n in sorted(numeros))


def gerar_cron_actions(
    horarios: list[tuple[int, int]],
    dias_semana: str,
    explore_hora: tuple[int, int],
) -> list[str]:
    """Gera linhas de cron (UTC) prontas para colar em `.github/workflows/worker.yml`,
    sob a chave `on: schedule:`.

    Recebe os horários já em BRT — hora local do Brasil — e faz a conversão
    para UTC (BRT = UTC-3), cuidando da virada de dia (ex.: 21:00 BRT vira
    00:00 UTC do dia seguinte, o que também desloca o dia-da-semana quando
    `dias_semana` não é "*"). `dias_semana` aceita o mesmo formato de
    `settings.scan_dias_semana` ("*" ou CSV mon..sun) — é validado com
    `dias_semana_apscheduler`.

    Uma linha é gerada por horário de varredura (na mesma ordem de
    `horarios`), seguida por uma última linha para `explore_hora`. Cada linha
    tem o formato YAML pronto para colar como item da lista `schedule:`:

        - cron: 'MINUTO HORA * * DIA_SEMANA'

    (campos no formato POSIX padrão do cron: minuto, hora, dia-do-mês, mês,
    dia-da-semana — os dois do meio ficam sempre "*", ou seja, todo dia do
    mês/mês).
    """
    dias_semana_norm = dias_semana_apscheduler(dias_semana)

    linhas: list[str] = []
    for hora, minuto in [*horarios, explore_hora]:
        hora_utc, minuto_utc, deslocamento = _brt_para_utc(hora, minuto)
        dow = _dow_field_utc(dias_semana_norm, deslocamento)
        linhas.append(f"- cron: '{minuto_utc} {hora_utc} * * {dow}'")
    return linhas
