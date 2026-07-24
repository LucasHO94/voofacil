"""Smoke test EMPÍRICO da Travelpayouts Data API.

A pesquisa (jul/2026) apontou migração de versão da API, com a REST antiga
possivelmente desligada. Este script testa cada endpoint com o token real e
mostra o que está de pé — rode ANTES de confiar no monitoramento.

Uso:
    python scripts/smoke_tp.py            # usa TP_TOKEN do .env
    python scripts/smoke_tp.py GRU LIS    # rota customizada
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from farewatch.providers.travelpayouts import (  # noqa: E402
    ENDPOINTS,
    TravelpayoutsError,
    TravelpayoutsProvider,
)


def main() -> int:
    origem = sys.argv[1].upper() if len(sys.argv) > 2 else "GRU"
    destino = sys.argv[2].upper() if len(sys.argv) > 2 else "LIS"
    hoje = date.today()
    # mês que vem (mês corrente costuma ter cache mais raro no fim do mês)
    ano, mes = (hoje.year + (hoje.month == 12), hoje.month % 12 + 1)
    mes_alvo = f"{ano:04d}-{mes:02d}"

    tp = TravelpayoutsProvider()
    if not tp.token:
        print("ERRO: TP_TOKEN nao configurado no .env — cadastre-se em travelpayouts.com")
        return 2

    print(f"Rota de teste: {origem} -> {destino} | mes: {mes_alvo}\n")
    resultados: dict[str, str] = {}

    testes = {
        "calendar": lambda: tp.calendar(origem, destino, mes_alvo),
        "cheap": lambda: tp.cheap(origem, destino, mes_alvo),
        "city_directions": lambda: tp.city_directions(origem),
        "month_matrix": lambda: tp.month_matrix(origem, destino, mes_alvo),
    }

    for nome, chamada in testes.items():
        try:
            quotes = chamada()
            exemplo = ""
            if quotes:
                q = min(quotes, key=lambda x: x.preco)
                exemplo = (f" | mais barato: {q.origem}->{q.destino} "
                           f"{q.data_ida} R$ {q.preco}")
            resultados[nome] = f"OK ({len(quotes)} cotacoes){exemplo}"
        except TravelpayoutsError as exc:
            resultados[nome] = f"ERRO API: {exc}"
        except Exception as exc:  # noqa: BLE001 — smoke test quer ver tudo
            resultados[nome] = f"FALHOU: {type(exc).__name__}: {exc}"

    largura = max(len(n) for n in resultados)
    print("Resultado por endpoint:")
    for nome, status in resultados.items():
        print(f"  {nome:<{largura}}  [{ENDPOINTS[nome]}]  ->  {status}")

    ok = sum(1 for s in resultados.values() if s.startswith("OK"))
    print(f"\n{ok}/{len(resultados)} endpoints funcionando.")
    if ok == 0:
        print("NENHUM endpoint respondeu — a migração de versão da API se confirmou.")
        print("Próximo passo: adaptar ENDPOINTS/cliente para a nova Data API da Travelpayouts.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
