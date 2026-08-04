"""Diagnóstico do agendamento — responde "por que não rodou nos horários?".

Roda contra O MESMO banco que o app resolve por DATABASE_URL (a mesma
`settings.sqlalchemy_url` que o painel e o worker usam). Não imprime NENHUMA
credencial: da URL só mostra esquema, host e nome do banco (nunca usuário/senha).

Uso:
    python -m scripts.diagnostico_agendamento

O que ele prova, olhando o carimbo `ultima_execucao` de cada horário:
- se estiver TUDO "nunca executou" ou com dias de atraso => o worker (GitHub
  Actions) NÃO está rodando/gravando NESTE banco => problema OPERACIONAL
  (Actions desativado, falhando, ou escrevendo em outro banco/SQLite efêmero);
- se estiver RECENTE (poucas horas) => a automação está viva; o problema é
  latência ou coleta, não disparo.

Rode nos DOIS lugares que compartilham o banco para comparar:
1. localmente (com o .env de produção apontando pro Neon), e
2. dispare o worker.yml manualmente no GitHub (aba Actions > Run workflow) e
   confira se o carimbo avança aqui depois.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from voofacil.config import descricao_banco, settings
from voofacil.db import SessionLocal, init_db
from voofacil.engine.agendamento import horarios_devidos, listar_horarios
from voofacil.models import HorarioAgendado, PriceSnapshot, TipoHorario, Watch

_OFFSET_BRT = 3


def _brt(dt: datetime | None) -> str:
    if dt is None:
        return "nunca executou"
    em_utc = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    agora = datetime.now(timezone.utc)
    atraso = agora - em_utc
    horas = atraso.total_seconds() / 3600
    quando = (em_utc - timedelta(hours=_OFFSET_BRT)).strftime("%d/%m %H:%M")
    if horas < 0:
        return f"{quando} BRT (no futuro?! relógio/tz)"
    return f"{quando} BRT (há {horas:.1f} h)"


def main() -> None:
    print("=" * 68)
    print("DIAGNÓSTICO DE AGENDAMENTO — VooFácil")
    print("=" * 68)
    print(f"Banco em uso .......: {descricao_banco(settings.sqlalchemy_url)}")
    if settings.sqlalchemy_url.startswith("sqlite"):
        print("  ⚠️  ATENÇÃO: apontando para SQLITE. Se este script está rodando")
        print("     no ambiente do WORKER (GitHub Actions) e vê SQLite, então o")
        print("     secret DATABASE_URL NÃO está setado lá e o worker grava num")
        print("     banco efêmero destruído a cada run — o Neon nunca recebe nada.")
    print(f"scan_dias_semana ...: {settings.scan_dias_semana!r}")
    print(f"poll (min) .........: {settings.agendamento_poll_minutos}")

    try:
        init_db()
    except OperationalError:
        print("\n" + "!" * 68)
        print("NÃO FOI POSSÍVEL CONECTAR AO BANCO ACIMA (timeout).")
        print("Isto NÃO é o diagnóstico do agendamento — é rede: esta máquina")
        print("não alcança o Neon (comum em Wi-Fi que bloqueia a porta 5432).")
        print("Rode este script de uma rede que alcance o Neon, OU simplesmente")
        print("abra a aba 'Agendamento' no painel Streamlit: o carimbo \"última\"")
        print("de cada horário mostra a MESMA informação, direto da fonte.")
        print("!" * 68)
        return

    agora = datetime.now(timezone.utc)
    print(f"agora (UTC) ........: {agora:%d/%m %H:%M}")
    print(f"agora (BRT) ........: {agora - timedelta(hours=_OFFSET_BRT):%d/%m %H:%M}")

    with SessionLocal() as s:
        for tipo in (TipoHorario.VARREDURA.value, TipoHorario.EXPLORE.value):
            print("\n" + "-" * 68)
            print(f"HORÁRIOS [{tipo}]")
            horarios = listar_horarios(s, tipo, incluir_inativos=True)
            if not horarios:
                print("  (nenhum cadastrado — a tabela está vazia p/ este tipo)")
            for h in horarios:
                flag = "" if h.ativo else "  🔴 PAUSADO (worker ignora)"
                print(f"  {h.hora:02d}:{h.minuto:02d} BRT | última: {_brt(h.ultima_execucao)}{flag}")
            devidos = horarios_devidos(s, tipo, agora)
            if devidos:
                rot = ", ".join(f"{h.hora:02d}:{h.minuto:02d}" for h in devidos)
                print(f"  ➜ DEVIDOS AGORA (o próximo poll deveria rodar): {rot}")
            else:
                print("  ➜ nada devido neste instante (já rodou hoje ou ainda não chegou a hora)")

        print("\n" + "-" * 68)
        n_watches = s.scalar(select(func.count()).select_from(Watch).where(Watch.ativo))
        n_total_w = s.scalar(select(func.count()).select_from(Watch))
        print(f"Watches ativos .....: {n_watches} (de {n_total_w} no total)")
        if not n_watches:
            print("  ⚠️  Sem watch ativo: a varredura roda, marca o horário como")
            print("     executado, mas não coleta nada — parece 'morto' sem estar.")

        ultimo = s.scalar(select(func.max(PriceSnapshot.coletado_em)))
        n_snaps = s.scalar(select(func.count()).select_from(PriceSnapshot))
        print(f"Snapshots no banco .: {n_snaps}")
        print(f"Snapshot mais novo .: {_brt(ultimo) if ultimo else 'NENHUM snapshot ainda'}")

    print("\n" + "=" * 68)
    print("LEITURA:")
    print("  • 'última' recente (horas) em todos os horários  => automação VIVA.")
    print("  • 'nunca executou' / atraso de dias              => worker não grava")
    print("    aqui: cheque a aba Actions (worker.yml desativado? runs vermelhos?)")
    print("    e o secret DATABASE_URL do GitHub.")
    print("  • 'última' recente mas 0 snapshot novo           => dispara mas não")
    print("    coleta (sem watch ativo / sem orçamento / rota sem oferta).")
    print("=" * 68)


if __name__ == "__main__":
    main()
