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
    # Cidade de onde a VOLTA parte, quando ela difere do destino da ida —
    # itinerário "open-jaw"/multi-cidade (ex.: voa RIO->NAP, roda a Itália por
    # terra e volta VCE->RIO). NULL = volta sai do próprio destino, que é o
    # comportamento de todo watch redondo comum. Só a SerpApi consegue cotar
    # isso (engine google_flights, type=3 + multi_city_json); a API de calendar
    # da Travelpayouts não tem representação para o caso — ver
    # `voofacil.providers.travelpayouts.sweep`, que pula esses watches.
    origem_volta_iata: Mapped[str | None] = mapped_column(String(3))
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
    # Tamanho do grupo que vai viajar. NÃO entra na busca: as cotações são
    # sempre por 1 passageiro, e `preco_limite` é comparado com esse preço
    # individual. Serve para o painel e o alerta mostrarem o custo total do
    # grupo (preço x passageiros) — buscar com adults=N devolveria apenas
    # tarifas com N assentos na mesma classe, tipicamente mais caras por pessoa
    # do que a promoção de 1-2 assentos que interessa detectar.
    passageiros: Mapped[int] = mapped_column(default=1)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # `cascade="all, delete-orphan"` é OBRIGATÓRIO aqui: sem ele o SQLAlchemy
    # trata a exclusão de um Watch como "desvincular os filhos", ou seja, tenta
    # UPDATE price_snapshot SET watch_id = NULL — e watch_id é NOT NULL. Excluir
    # um monitoramento que já coletou preço estourava
    # `IntegrityError: null value in column "watch_id" violates not-null
    # constraint` e, como o painel guarda a MESMA Session em st.session_state,
    # toda operação seguinte morria com PendingRollbackError até recarregar a
    # página. Com o cascade no nível do ORM o SQLAlchemy emite os DELETEs dos
    # filhos explicitamente — sem exigir ALTER de constraint (ON DELETE CASCADE)
    # no Postgres de produção.
    #
    # A ordem dos DELETEs fica a cargo do unit of work, que respeita a dependência
    # entre tabelas: `alerta` referencia `price_snapshot`, logo os alertas são
    # apagados ANTES dos snapshots. (Ver também
    # `voofacil.engine.queries.excluir_watch`, que faz a mesma limpeza em massa,
    # sem carregar as coleções, para watches com dezenas de milhares de linhas.)
    snapshots: Mapped[list["PriceSnapshot"]] = relationship(
        back_populates="watch",
        cascade="all, delete-orphan",
    )
    alertas: Mapped[list["Alerta"]] = relationship(
        back_populates="watch",
        cascade="all, delete-orphan",
    )

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
        # Aceita NULL porque a micro-migração adiciona a coluna nas linhas
        # pré-existentes do Postgres sem preenchê-las (ver voofacil/db.py); a
        # leitura trata NULL como 1 pela property `total_passageiros`.
        CheckConstraint(
            "passageiros IS NULL OR passageiros >= 1",
            name="ck_watch_passageiros",
        ),
        # Open-jaw só faz sentido em watch de ROTA e com volta: um REGIAO não
        # tem destino único de onde derivar o trecho de retorno, e um watch de
        # só-ida não tem retorno nenhum.
        CheckConstraint(
            "origem_volta_iata IS NULL OR (tipo = 'ROTA' AND ida_volta = true)",
            name="ck_watch_origem_volta",
        ),
    )

    @property
    def total_passageiros(self) -> int:
        """Tamanho do grupo, tolerando o NULL das linhas anteriores à coluna."""
        return self.passageiros or 1

    @property
    def open_jaw(self) -> bool:
        """True se a volta parte de uma cidade diferente do destino da ida."""
        return bool(self.origem_volta_iata) and self.origem_volta_iata != self.destino_iata

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
