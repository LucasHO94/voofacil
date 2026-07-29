"""Modelos ORM do VooFacil.

Três entidades centrais:
- Watch: o que monitorar (rota específica ou origem->região com lista de destinos)
- PriceSnapshot: cada preço observado (série temporal — base do baseline)
- Alerta: alertas enviados (dedupe/cooldown e auditoria)

Preço é sempre Decimal/Numeric — nunca float (convenção do workspace).
"""
from __future__ import annotations

import enum
import json
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TipoWatch(str, enum.Enum):
    ROTA = "ROTA"        # origem -> destino específico
    REGIAO = "REGIAO"    # origem -> lista de destinos (região/país)


class FonteSnapshot(str, enum.Enum):
    TP_CACHE = "TP_CACHE"    # Travelpayouts (preço em cache, indicativo)
    SERPAPI = "SERPAPI"      # SerpApi/Google Flights (preço ao vivo, confirmado)


class Gatilho(str, enum.Enum):
    LIMITE = "LIMITE"              # abaixo do preco_limite definido pelo usuário
    NOVO_MINIMO = "NOVO_MINIMO"    # abaixo do menor valor já registrado
    PERCENTIL = "PERCENTIL"        # no p10-p25 da distribuição (fase 2)
    ERROR_FARE = "ERROR_FARE"      # <= 50% do baseline — prioridade máxima


class TipoData(str, enum.Enum):
    EXATA = "EXATA"                # a data precisa bater exatamente
    APROXIMADA = "APROXIMADA"      # tolerância de flexibilidade_dias em torno da data
    INTERVALO = "INTERVALO"        # qualquer data dentro de [ini, fim]


class TipoHorario(str, enum.Enum):
    VARREDURA = "VARREDURA"    # varredura regular (equivalente a scan_horarios)
    EXPLORE = "EXPLORE"        # busca ampla/SerpApi Explore (equivalente a explore_horario)


class Watch(Base):
    __tablename__ = "watch"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(80))                     # rótulo humano ("GRU -> Europa set/26")
    tipo: Mapped[str] = mapped_column(String(6), default=TipoWatch.ROTA.value)
    origem_iata: Mapped[str] = mapped_column(String(3))
    destino_iata: Mapped[str | None] = mapped_column(String(3))       # NULL quando tipo=REGIAO
    regiao_destinos: Mapped[str | None] = mapped_column(Text)         # JSON: ["LIS","MAD",...] p/ REGIAO
    arrival_area_id: Mapped[str | None] = mapped_column(String(40))   # kgmid Google Explore (ex.: /m/02j9z = Europa)
    data_ini: Mapped[date]                                            # janela de embarque
    data_fim: Mapped[date]
    tipo_data_ida: Mapped[str] = mapped_column(String(10), default=TipoData.INTERVALO.value)
    tipo_data_volta: Mapped[str] = mapped_column(String(10), default=TipoData.INTERVALO.value)
    flexibilidade_dias: Mapped[int | None]                            # tolerância p/ tipo_data_*='APROXIMADA'
    data_volta_ini: Mapped[date | None]                               # janela de desembarque (independente da ida)
    data_volta_fim: Mapped[date | None]
    mes_especifico: Mapped[int | None]                                # 1-12; se setado, restringe a esse mês
    ida_volta: Mapped[bool] = mapped_column(Boolean, default=True)
    duracao_min: Mapped[int | None]                                   # noites mínimas (ida_volta)
    duracao_max: Mapped[int | None]
    preco_limite: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))   # teto/máximo
    preco_minimo: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))   # piso de preço
    preco_aproximado: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))  # preço-alvo informativo
    moeda: Mapped[str] = mapped_column(String(3), default="BRL")
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    snapshots: Mapped[list["PriceSnapshot"]] = relationship(back_populates="watch")
    alertas: Mapped[list["Alerta"]] = relationship(back_populates="watch")

    __table_args__ = (
        CheckConstraint("tipo IN ('ROTA','REGIAO')", name="ck_watch_tipo"),
        CheckConstraint(
            "(tipo = 'ROTA' AND destino_iata IS NOT NULL) OR tipo = 'REGIAO'",
            name="ck_watch_destino",
        ),
        CheckConstraint(
            "tipo_data_ida IN ('EXATA','APROXIMADA','INTERVALO')",
            name="ck_watch_tipo_data_ida",
        ),
        CheckConstraint(
            "tipo_data_volta IN ('EXATA','APROXIMADA','INTERVALO')",
            name="ck_watch_tipo_data_volta",
        ),
        CheckConstraint(
            "mes_especifico IS NULL OR (mes_especifico BETWEEN 1 AND 12)",
            name="ck_watch_mes_especifico",
        ),
    )

    @property
    def destinos(self) -> list[str]:
        """Lista de IATAs de destino a varrer (1 para ROTA, N para REGIAO)."""
        if self.tipo == TipoWatch.ROTA.value:
            return [self.destino_iata] if self.destino_iata else []
        return json.loads(self.regiao_destinos or "[]")


class PriceSnapshot(Base):
    __tablename__ = "price_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watch.id"))
    origem: Mapped[str] = mapped_column(String(3))
    destino: Mapped[str] = mapped_column(String(3))
    data_ida: Mapped[date]
    data_volta: Mapped[date | None]
    preco: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    moeda: Mapped[str] = mapped_column(String(3), default="BRL")
    cia: Mapped[str | None] = mapped_column(String(60))
    escalas: Mapped[int | None]
    fonte: Mapped[str] = mapped_column(String(10))
    deep_link: Mapped[str | None] = mapped_column(Text)               # URL de compra/busca
    coletado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    watch: Mapped[Watch] = relationship(back_populates="snapshots")

    __table_args__ = (
        Index("ix_snapshot_rota", "origem", "destino", "data_ida"),
        Index("ix_snapshot_watch", "watch_id", "coletado_em"),
    )


class Alerta(Base):
    __tablename__ = "alerta"

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watch.id"))
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("price_snapshot.id"))
    gatilho: Mapped[str] = mapped_column(String(12))
    preco: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    confirmado_ao_vivo: Mapped[bool] = mapped_column(Boolean, default=False)
    canal: Mapped[str] = mapped_column(String(10))                    # TELEGRAM | EMAIL
    enviado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    watch: Mapped[Watch] = relationship(back_populates="alertas")
    snapshot: Mapped[PriceSnapshot] = relationship()

    __table_args__ = (
        Index("ix_alerta_watch", "watch_id", "enviado_em"),
    )


class ContadorApi(Base):
    """Orçamento mensal de chamadas por provedor (ex.: SerpApi 250/mês)."""

    __tablename__ = "contador_api"

    id: Mapped[int] = mapped_column(primary_key=True)
    provedor: Mapped[str] = mapped_column(String(20))                 # 'SERPAPI'
    ano_mes: Mapped[str] = mapped_column(String(7))                   # '2026-07'
    chamadas: Mapped[int] = mapped_column(default=0)

    __table_args__ = (
        Index("ux_contador_provedor_mes", "provedor", "ano_mes", unique=True),
    )


class ContadorDiarioApi(Base):
    """Orçamento DIÁRIO de chamadas por provedor — complementa ContadorApi
    (mensal). Existe para permitir um teto tipo "no máximo N chamadas/dia",
    independente de quantas ainda restam no mês (ex.: SerpApi 8/dia, mesmo
    com folga no orçamento mensal de 250)."""

    __tablename__ = "contador_diario_api"

    id: Mapped[int] = mapped_column(primary_key=True)
    provedor: Mapped[str] = mapped_column(String(20))                 # 'SERPAPI'
    dia: Mapped[str] = mapped_column(String(10))                      # '2026-07-28'
    chamadas: Mapped[int] = mapped_column(default=0)

    __table_args__ = (
        Index("ux_contador_diario_provedor_dia", "provedor", "dia", unique=True),
    )


class HorarioAgendado(Base):
    """Horários de varredura/explore, editáveis pelo painel Streamlit e
    persistidos no banco (Postgres/Neon em produção) — substituem o que antes
    só existia como CSV estático em `settings.scan_horarios` /
    `settings.explore_horario`. O .env continua servindo de SEED inicial (ver
    `voofacil.engine.agendamento.garantir_horarios_padrao`), mas depois de
    semeado o banco manda: o usuário pode adicionar, excluir e editar
    quantidade/valores direto no painel, sem reiniciar o processo.

    `hora`/`minuto` são sempre horário LOCAL do Brasil (BRT, UTC-3) — o que o
    usuário vê e edita no painel. A conversão para UTC (se necessária) fica a
    cargo de quem consome (ex.: geração de cron do GitHub Actions)."""

    __tablename__ = "horario_agendado"

    id: Mapped[int] = mapped_column(primary_key=True)
    tipo: Mapped[str] = mapped_column(String(10))
    hora: Mapped[int]
    minuto: Mapped[int]
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Carimbo (em UTC, tz-aware) da última execução REAL deste horário — quando
    # o worker de fato varreu/explorou por causa dele, NÃO quando "era pra ter"
    # rodado. É o estado que sustenta o modelo catch-up de
    # `voofacil.engine.agendamento`: em vez de perguntar "estou EM CIMA do
    # horário agora?", pergunta-se "a ocorrência mais recente deste horário é
    # POSTERIOR a `ultima_execucao`?". NULL = nunca executou, logo está devido
    # (é também o valor de toda linha pré-existente no banco de produção após a
    # micro-migração de voofacil/db.py).
    ultima_execucao: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("tipo IN ('VARREDURA','EXPLORE')", name="ck_horario_agendado_tipo"),
        CheckConstraint("hora BETWEEN 0 AND 23", name="ck_horario_agendado_hora"),
        CheckConstraint("minuto BETWEEN 0 AND 59", name="ck_horario_agendado_minuto"),
        Index("ix_horario_agendado_tipo_ativo", "tipo", "ativo"),
    )
