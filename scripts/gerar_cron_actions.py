"""Gera as linhas de cron (UTC) do .github/workflows/worker.yml a partir da
config atual (voofacil/config.py: SCAN_HORARIOS / SCAN_DIAS_SEMANA /
EXPLORE_HORARIO no .env).

LIMITAÇÃO: o "on: schedule: cron" do GitHub Actions é YAML ESTÁTICO — o
GitHub lê esse arquivo na hora de agendar o workflow, não em tempo de
execução. Não dá pra fazer o agendamento da nuvem ler o .env dinamicamente.
Este script existe pra facilitar a parte manual: gera, a partir da MESMA
config usada localmente (worker.py em modo daemon / Task Scheduler), as
linhas já convertidas pra UTC — é só colar no bloco `on: schedule:` do
worker.yml sempre que você mudar a quantidade/horário das varreduras.

Uso:
    python scripts/gerar_cron_actions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voofacil.config import settings  # noqa: E402
from voofacil.schedule_utils import (  # noqa: E402
    gerar_cron_actions,
    parse_horarios,
)


def main() -> None:
    horarios = parse_horarios(settings.scan_horarios)
    explore_hora = parse_horarios(settings.explore_horario)[0]

    linhas = gerar_cron_actions(horarios, settings.scan_dias_semana, explore_hora)

    # As últimas MESES_EXPLORE_A_FRENTE... não, aqui é só: N horários de
    # varredura seguidos de 1 horário de explore (mesma ordem de entrada).
    rotulos = [f"{h:02d}:{m:02d} BRT (varredura)" for h, m in horarios]
    rotulos.append(f"{explore_hora[0]:02d}:{explore_hora[1]:02d} BRT (explore)")

    print("# Gerado a partir de SCAN_HORARIOS/SCAN_DIAS_SEMANA/EXPLORE_HORARIO")
    print("# (voofacil/config.py / .env). Cole dentro de 'on: schedule:' em")
    print("# .github/workflows/worker.yml, substituindo as linhas '- cron: ...'")
    print("# atuais. Lembre de ajustar também o 'if' do step 'Rodar varredura'")
    print("# no worker.yml se a linha do explore mudar de posição/valor,")
    print("# já que ele decide varredura-vs-explore comparando")
    print("# github.event.schedule com o cron do explore.")
    print("#")
    print("# ATENÇÃO: não há evidência forte de que uma hora do dia específica")
    print("# tenha preço menor — o que a pesquisa confirma é que dia da semana")
    print("# e antecedência da compra importam mais. O espalhamento de horários")
    print("# serve pra fisgar quedas de preço ao longo do dia, não pra acertar")
    print("# uma 'hora mágica'.")
    print()
    for linha, rotulo in zip(linhas, rotulos):
        print(f"    {linha}   # {rotulo}")


if __name__ == "__main__":
    main()
