"""Cria watches de exemplo/linha de comando (até a UI ficar pronta na Sprint 5).

Uso:
    python scripts/seed_watch.py rota GRU LIS 2026-09-01 2026-09-30 --limite 3500
    python scripts/seed_watch.py regiao GRU europa 2026-09-01 2026-11-30 --limite 4000
    python scripts/seed_watch.py listar
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from voofacil.db import SessionLocal, init_db  # noqa: E402
from voofacil.models import TipoWatch, Watch  # noqa: E402

# Listas curadas de destinos por região (IATA) — editáveis à vontade
REGIOES: dict[str, list[str]] = {
    "brasil_capitais": [
        "GIG", "BSB", "CNF", "SSA", "REC", "FOR", "POA", "CWB", "FLN", "BEL",
        "MAO", "NAT", "MCZ", "SLZ", "VIX", "GYN", "CGB", "CGR", "JPA", "AJU",
    ],
    "nordeste": ["SSA", "REC", "FOR", "NAT", "MCZ", "SLZ", "JPA", "AJU", "THE", "PMW"],
    "america_sul": ["EZE", "SCL", "MVD", "ASU", "LIM", "BOG", "UIO", "GYE", "CUZ"],
    "europa": [
        "LIS", "OPO", "MAD", "BCN", "CDG", "LHR", "FCO", "MXP", "AMS", "FRA",
        "MUC", "ZRH", "BRU", "VIE", "PRG", "ATH", "DUB", "CPH",
    ],
    "america_norte": ["MIA", "MCO", "JFK", "LAX", "YYZ", "MEX", "CUN"],
    "asia": ["DXB", "DOH", "IST", "NRT", "ICN", "BKK", "SIN"],
}

# kgmid (Google Knowledge Graph) por região, para a busca ampla via SerpApi
# Explore (arrival_area_id). ATENÇÃO: valores de exemplo — confirme o kgmid
# exato de cada região/país antes de confiar (busque o termo na SerpApi
# Google Knowledge Graph API ou no Google e copie o "kgmid" do resultado).
# Sem um kgmid confirmado, deixe em branco: o watch funciona normalmente
# com a lista curada acima, só não roda o Explore diário.
ARRIVAL_AREA_ID: dict[str, str] = {
    "europa": "/m/02j9z",
}


def cmd_rota(args) -> None:
    watch = Watch(
        nome=f"{args.origem.upper()} -> {args.destino.upper()} "
             f"{args.data_ini.strftime('%d/%m')}-{args.data_fim.strftime('%d/%m/%Y')}",
        tipo=TipoWatch.ROTA.value,
        origem_iata=args.origem.upper(),
        destino_iata=args.destino.upper(),
        data_ini=args.data_ini,
        data_fim=args.data_fim,
        preco_limite=args.limite,
    )
    _salvar(watch)


def cmd_regiao(args) -> None:
    if args.regiao not in REGIOES:
        raise SystemExit(f"Região desconhecida. Opções: {', '.join(REGIOES)}")
    area_id = ARRIVAL_AREA_ID.get(args.regiao)
    watch = Watch(
        nome=f"{args.origem.upper()} -> {args.regiao} "
             f"{args.data_ini.strftime('%d/%m')}-{args.data_fim.strftime('%d/%m/%Y')}",
        tipo=TipoWatch.REGIAO.value,
        origem_iata=args.origem.upper(),
        destino_iata=None,
        regiao_destinos=json.dumps(REGIOES[args.regiao]),
        arrival_area_id=area_id,
        data_ini=args.data_ini,
        data_fim=args.data_fim,
        preco_limite=args.limite,
    )
    if not area_id:
        print(f"Aviso: sem kgmid cadastrado para '{args.regiao}' — "
              "busca ampla via SerpApi Explore fica desativada para este watch "
              "(a lista curada de destinos continua funcionando normalmente).")
    _salvar(watch)


def _salvar(watch: Watch) -> None:
    with SessionLocal() as session:
        session.add(watch)
        session.commit()
        print(f"Watch #{watch.id} criado: {watch.nome}"
              + (f" | limite R$ {watch.preco_limite}" if watch.preco_limite else ""))


def cmd_listar(_args) -> None:
    with SessionLocal() as session:
        watches = session.scalars(select(Watch)).all()
        if not watches:
            print("Nenhum watch cadastrado.")
            return
        for w in watches:
            status = "ativo" if w.ativo else "INATIVO"
            limite = f" limite R$ {w.preco_limite}" if w.preco_limite else ""
            print(f"#{w.id} [{status}] {w.nome} ({w.tipo}, {len(w.destinos)} destino(s)){limite}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gerencia watches do VooFacil")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def data(s: str) -> date:
        return date.fromisoformat(s)

    p_rota = sub.add_parser("rota", help="origem -> destino específico")
    p_rota.add_argument("origem")
    p_rota.add_argument("destino")
    p_rota.add_argument("data_ini", type=data)
    p_rota.add_argument("data_fim", type=data)
    p_rota.add_argument("--limite", type=Decimal, default=None)
    p_rota.set_defaults(func=cmd_rota)

    p_reg = sub.add_parser("regiao", help="origem -> região (lista curada)")
    p_reg.add_argument("origem")
    p_reg.add_argument("regiao", choices=sorted(REGIOES))
    p_reg.add_argument("data_ini", type=data)
    p_reg.add_argument("data_fim", type=data)
    p_reg.add_argument("--limite", type=Decimal, default=None)
    p_reg.set_defaults(func=cmd_regiao)

    p_list = sub.add_parser("listar", help="lista watches")
    p_list.set_defaults(func=cmd_listar)

    args = parser.parse_args()
    init_db()
    args.func(args)


if __name__ == "__main__":
    main()
