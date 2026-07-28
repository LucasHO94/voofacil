"""Utilitários de agendamento — parsing/validação da config (`.env`) usada como
SEED inicial do agendamento dinâmico em banco (ver voofacil/engine/agendamento.py).

Sobre o PADRÃO de horários (`scan_horarios` em voofacil/config.py): madrugada
(06:00), meio-dia (12:30) e noite (21:00) — espalhados ao longo do dia. Isso
NÃO é porque exista evidência forte de que uma hora específica do dia tenha
preço menor (não há). O que a pesquisa confirmou é que DIA DA SEMANA e
ANTECEDÊNCIA da compra importam mais do que a hora exata da varredura. O
espalhamento serve só para "fisgar" quedas de preço que aparecem e
desaparecem ao longo do dia, e para não concentrar todas as chamadas de API
num único horário.

Os horários em si (quantidade e valores) são editáveis pelo painel Streamlit
depois do primeiro seed — ver `voofacil.engine.agendamento`. Tanto o daemon
local quanto o GitHub Actions rodam num poll de frequência fixa e curta
(`settings.agendamento_poll_minutos`) e decidem, via `deve_rodar_agora()`, se
é hora de varrer/explorar de verdade.
"""
from __future__ import annotations

_DIAS_ORDEM = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DIAS_VALIDOS = set(_DIAS_ORDEM)


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
