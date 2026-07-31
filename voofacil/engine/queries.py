"""Consultas e mutações usadas pelo painel Streamlit — sem SQL na camada de UI."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete as sa_delete, func, select

from voofacil.models import Alerta, PriceSnapshot, TipoData, TipoWatch, Watch, utcnow

# Recorte padrão do histórico. Um watch de REGIÃO gera ~1.600 snapshots por
# varredura (18 destinos x 3 meses x ~30 dias); em um mês são ~145.000 linhas.
# Carregar tudo do Neon a CADA rerun do Streamlit só para desenhar um gráfico é
# inviável — daí o recorte temporal + LIMIT abaixo.
DIAS_HISTORICO_PADRAO = 90
LIMITE_HISTORICO_PADRAO = 2000

# Sentinela para `atualizar_watch`: distingue "não mexa neste campo" de
# "grave NULL neste campo" (quase todo campo do Watch é nullable).
class _Manter:
    def __repr__(self) -> str:            # pragma: no cover - só para depuração
        return "<MANTER>"


MANTER = _Manter()

_TIPOS_DATA_VALIDOS = {t.value for t in TipoData}


def _commit_seguro(session) -> None:
    """Commit que nunca deixa a Session envenenada.

    O painel guarda UMA Session em `st.session_state` pela vida toda da aba: se
    um commit falha e ninguém faz rollback, todas as operações seguintes
    estouram PendingRollbackError até o usuário recarregar a página. Aqui o erro
    original continua subindo (a UI decide como mostrar), mas a sessão volta
    utilizável.
    """
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


def listar_watches(session) -> list[Watch]:
    return list(session.scalars(select(Watch).order_by(Watch.criado_em.desc())))


def criar_watch_rota(
    session, nome: str, origem: str, destino: str,
    data_ini: date, data_fim: date, preco_limite: Decimal | None,
    *,
    ida_volta: bool = True,
    duracao_min: int | None = None,
    duracao_max: int | None = None,
    tipo_data_ida: str = "INTERVALO",
    tipo_data_volta: str = "INTERVALO",
    flexibilidade_dias: int | None = None,
    data_volta_ini: date | None = None,
    data_volta_fim: date | None = None,
    mes_especifico: int | None = None,
    preco_minimo: Decimal | None = None,
    preco_aproximado: Decimal | None = None,
    origem_volta: str | None = None,
    passageiros: int = 1,
) -> Watch:
    """Cria um watch de rota específica.

    `origem_volta` torna o itinerário OPEN-JAW (a volta parte de outra cidade
    que não o destino); passar a própria cidade de destino é o mesmo que
    omitir. `passageiros` é o tamanho do grupo — não entra na busca, que é
    sempre por 1 pessoa, e serve para exibir o custo total (ver
    Watch.passageiros).
    """
    if not ida_volta and origem_volta:
        raise ValueError("origem_volta exige ida_volta=True — só-ida não tem trecho de retorno")
    if passageiros < 1:
        raise ValueError(f"passageiros deve ser >= 1 (recebido {passageiros!r})")

    origem_volta_iata = origem_volta.upper() if origem_volta else None
    if origem_volta_iata == destino.upper():
        origem_volta_iata = None      # "volta do próprio destino" = watch redondo comum

    w = Watch(
        nome=nome,
        tipo=TipoWatch.ROTA.value,
        origem_iata=origem.upper(),
        destino_iata=destino.upper(),
        origem_volta_iata=origem_volta_iata,
        passageiros=passageiros,
        data_ini=data_ini,
        data_fim=data_fim,
        preco_limite=preco_limite,
        ida_volta=ida_volta,
        duracao_min=duracao_min,
        duracao_max=duracao_max,
        tipo_data_ida=tipo_data_ida,
        tipo_data_volta=tipo_data_volta,
        flexibilidade_dias=flexibilidade_dias,
        data_volta_ini=data_volta_ini,
        data_volta_fim=data_volta_fim,
        mes_especifico=mes_especifico,
        preco_minimo=preco_minimo,
        preco_aproximado=preco_aproximado,
    )
    session.add(w)
    _commit_seguro(session)
    return w


def criar_watch_regiao(
    session, nome: str, origem: str, destinos: list[str],
    data_ini: date, data_fim: date, preco_limite: Decimal | None,
    arrival_area_id: str | None = None,
    *,
    ida_volta: bool = True,
    duracao_min: int | None = None,
    duracao_max: int | None = None,
    tipo_data_ida: str = "INTERVALO",
    tipo_data_volta: str = "INTERVALO",
    flexibilidade_dias: int | None = None,
    data_volta_ini: date | None = None,
    data_volta_fim: date | None = None,
    mes_especifico: int | None = None,
    preco_minimo: Decimal | None = None,
    preco_aproximado: Decimal | None = None,
    passageiros: int = 1,
) -> Watch:
    """Cria um watch de região (origem -> lista de destinos).

    Não aceita `origem_volta`: sem destino único não há de onde derivar o
    trecho de retorno de um open-jaw (ver a CheckConstraint
    `ck_watch_origem_volta`).
    """
    if passageiros < 1:
        raise ValueError(f"passageiros deve ser >= 1 (recebido {passageiros!r})")

    w = Watch(
        nome=nome,
        tipo=TipoWatch.REGIAO.value,
        origem_iata=origem.upper(),
        destino_iata=None,
        passageiros=passageiros,
        regiao_destinos=json.dumps([d.upper() for d in destinos]),
        arrival_area_id=arrival_area_id or None,
        data_ini=data_ini,
        data_fim=data_fim,
        preco_limite=preco_limite,
        ida_volta=ida_volta,
        duracao_min=duracao_min,
        duracao_max=duracao_max,
        tipo_data_ida=tipo_data_ida,
        tipo_data_volta=tipo_data_volta,
        flexibilidade_dias=flexibilidade_dias,
        data_volta_ini=data_volta_ini,
        data_volta_fim=data_volta_fim,
        mes_especifico=mes_especifico,
        preco_minimo=preco_minimo,
        preco_aproximado=preco_aproximado,
    )
    session.add(w)
    _commit_seguro(session)
    return w


def alternar_ativo(session, watch_id: int) -> Watch | None:
    w = session.get(Watch, watch_id)
    if w is None:
        return None
    try:
        w.ativo = not w.ativo
        session.commit()
    except Exception:
        session.rollback()
        raise
    return w


def atualizar_watch(
    session, watch_id: int,
    *,
    nome: str | object = MANTER,
    origem: str | object = MANTER,
    destino: str | None | object = MANTER,
    destinos: list[str] | None | object = MANTER,
    arrival_area_id: str | None | object = MANTER,
    data_ini: date | object = MANTER,
    data_fim: date | object = MANTER,
    preco_limite: Decimal | None | object = MANTER,
    ida_volta: bool | object = MANTER,
    duracao_min: int | None | object = MANTER,
    duracao_max: int | None | object = MANTER,
    tipo_data_ida: str | object = MANTER,
    tipo_data_volta: str | object = MANTER,
    flexibilidade_dias: int | None | object = MANTER,
    data_volta_ini: date | None | object = MANTER,
    data_volta_fim: date | None | object = MANTER,
    mes_especifico: int | None | object = MANTER,
    preco_minimo: Decimal | None | object = MANTER,
    preco_aproximado: Decimal | None | object = MANTER,
    origem_volta: str | None | object = MANTER,
    passageiros: int | object = MANTER,
    ativo: bool | object = MANTER,
) -> Watch | None:
    """Edita um watch existente — contraparte de `criar_watch_rota`/`criar_watch_regiao`.

    Só os campos EXPLICITAMENTE informados são tocados; o que não vier fica como
    está. Por isso o default de cada parâmetro é a sentinela `MANTER` e não
    `None`: `preco_limite=None` significa "apague o limite", enquanto omitir
    `preco_limite` significa "não mexa".

    Troca de tipo é suportada e coerente com as CheckConstraints do modelo:
    - passar `destino="NAP"` transforma o watch em ROTA (zera `regiao_destinos`);
    - passar `destinos=["LIS","MAD"]` transforma em REGIÃO (zera `destino_iata`).
    Passar os dois ao mesmo tempo é erro (ValueError).

    Retorna o Watch atualizado, ou None se `watch_id` não existir (mesma
    tolerância de `excluir_watch` — o painel pode ter um id velho na tela).
    Valida ANTES de gravar e faz rollback em caso de falha, para nunca deixar a
    Session do Streamlit envenenada com PendingRollbackError.
    """
    w = session.get(Watch, watch_id)
    if w is None:
        return None

    if destino is not MANTER and destinos is not MANTER:
        raise ValueError("informe 'destino' (ROTA) ou 'destinos' (REGIÃO), não os dois")

    # --- validações antes de encostar no objeto -----------------------------
    for rotulo, valor in (("tipo_data_ida", tipo_data_ida), ("tipo_data_volta", tipo_data_volta)):
        if valor is not MANTER and valor not in _TIPOS_DATA_VALIDOS:
            raise ValueError(f"{rotulo} inválido: {valor!r} (use {sorted(_TIPOS_DATA_VALIDOS)})")
    if mes_especifico is not MANTER and mes_especifico is not None:
        if not 1 <= int(mes_especifico) <= 12:
            raise ValueError(f"mes_especifico deve estar entre 1 e 12 (recebido {mes_especifico!r})")
    if destino is not MANTER and not destino:
        raise ValueError("um watch de ROTA precisa de destino")
    if destinos is not MANTER and not destinos:
        raise ValueError("um watch de REGIÃO precisa de ao menos um destino")

    novo_ini = w.data_ini if data_ini is MANTER else data_ini
    novo_fim = w.data_fim if data_fim is MANTER else data_fim
    if novo_ini and novo_fim and novo_ini > novo_fim:
        raise ValueError(f"data_ini ({novo_ini}) não pode ser depois de data_fim ({novo_fim})")

    if passageiros is not MANTER and int(passageiros) < 1:
        raise ValueError(f"passageiros deve ser >= 1 (recebido {passageiros!r})")

    # Open-jaw precisa continuar coerente DEPOIS da edição, não só antes: quem
    # troca um watch para só-ida, ou o converte em REGIÃO, sem limpar a origem
    # da volta violaria a CheckConstraint `ck_watch_origem_volta` — o erro
    # apareceria lá no commit, como IntegrityError cru na tela do painel.
    novo_open_jaw = w.origem_volta_iata if origem_volta is MANTER else origem_volta
    novo_destino = w.destino_iata if destino is MANTER else destino
    if novo_open_jaw:
        novo_ida_volta = w.ida_volta if ida_volta is MANTER else ida_volta
        vira_regiao = destinos is not MANTER
        if not novo_ida_volta:
            raise ValueError("origem_volta exige ida_volta=True — só-ida não tem trecho de retorno")
        if vira_regiao:
            raise ValueError("origem_volta só existe em watch de ROTA — um watch de REGIÃO não tem destino único")
        if novo_destino and novo_open_jaw.upper() == novo_destino.upper():
            novo_open_jaw = None      # volta do próprio destino: watch redondo comum

    try:
        if nome is not MANTER:
            w.nome = nome
        if origem is not MANTER:
            w.origem_iata = origem.upper()

        if destino is not MANTER:
            w.tipo = TipoWatch.ROTA.value
            w.destino_iata = destino.upper()
            w.regiao_destinos = None
            w.arrival_area_id = None
        elif destinos is not MANTER:
            w.tipo = TipoWatch.REGIAO.value
            w.destino_iata = None
            w.regiao_destinos = json.dumps([d.upper() for d in destinos])
            w.origem_volta_iata = None    # REGIÃO nunca tem trecho open-jaw

        if origem_volta is not MANTER:
            w.origem_volta_iata = novo_open_jaw.upper() if novo_open_jaw else None

        if arrival_area_id is not MANTER:
            w.arrival_area_id = arrival_area_id or None

        for campo, valor in (
            ("data_ini", data_ini),
            ("data_fim", data_fim),
            ("preco_limite", preco_limite),
            ("ida_volta", ida_volta),
            ("duracao_min", duracao_min),
            ("duracao_max", duracao_max),
            ("tipo_data_ida", tipo_data_ida),
            ("tipo_data_volta", tipo_data_volta),
            ("flexibilidade_dias", flexibilidade_dias),
            ("data_volta_ini", data_volta_ini),
            ("data_volta_fim", data_volta_fim),
            ("mes_especifico", mes_especifico),
            ("preco_minimo", preco_minimo),
            ("preco_aproximado", preco_aproximado),
            ("passageiros", passageiros),
            ("ativo", ativo),
        ):
            if valor is not MANTER:
                setattr(w, campo, valor)

        session.commit()
    except Exception:
        session.rollback()
        raise
    return w


def excluir_watch(session, watch_id: int) -> bool:
    """Exclui um watch e TUDO que pende dele (snapshots e alertas).

    Retorna True se algo foi excluído, False se o id não existe (o painel pode
    clicar na lixeira de uma linha que outro rerun já removeu).

    Os filhos são apagados em massa, com DELETE ... WHERE watch_id = :id, em vez
    de carregar `w.snapshots` inteiro na memória: um watch de REGIÃO acumula
    dezenas de milhares de snapshots e o cascade do ORM os traria TODOS do Neon
    só para apagá-los. O `cascade="all, delete-orphan"` do modelo continua sendo
    a rede de segurança (qualquer outro caminho que chame `session.delete(w)`
    não estoura NOT NULL), mas aqui já chegamos com as tabelas filhas limpas.

    Ordem obrigatória: alerta -> price_snapshot -> watch, porque `alerta`
    referencia `price_snapshot.id`.

    Qualquer falha faz rollback ANTES de propagar: o painel guarda a mesma
    Session em `st.session_state`, e uma sessão deixada em estado de erro
    quebraria todas as operações seguintes com PendingRollbackError até o
    usuário recarregar a página.
    """
    try:
        w = session.get(Watch, watch_id)
        if w is None:
            return False
        session.execute(sa_delete(Alerta).where(Alerta.watch_id == watch_id))
        session.execute(sa_delete(PriceSnapshot).where(PriceSnapshot.watch_id == watch_id))
        # As coleções podem já estar carregadas na Session do painel (o gráfico
        # acabou de lê-las). Sem expirar, o cascade tentaria apagar de novo linhas
        # que os DELETEs em massa acima já removeram — SAWarning "expected to
        # delete N row(s); 0 were matched". Expirado, o cascade recarrega e acha
        # coleção vazia.
        session.expire(w, ["snapshots", "alertas"])
        session.delete(w)
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise


def historico_precos(
    session, watch_id: int,
    *,
    dias: int | None = DIAS_HISTORICO_PADRAO,
    limite: int | None = LIMITE_HISTORICO_PADRAO,
) -> list[PriceSnapshot]:
    """Snapshots do watch em ordem cronológica CRESCENTE (para o gráfico).

    Antes esta função fazia `SELECT * FROM price_snapshot WHERE watch_id = :id`
    sem recorte nenhum. Um watch de REGIÃO gera ~1.600 snapshots por varredura,
    ~145.000 por mês — e o Streamlit chama isto a cada rerun. Agora:

    - `dias`: só o que foi coletado nos últimos N dias (None = sem recorte);
    - `limite`: no máximo N linhas, ficando com as MAIS RECENTES (None = sem
      teto). A consulta desce por `coletado_em DESC LIMIT n` e a lista é
      invertida no fim, então o retorno continua crescente.

    Se o recorte temporal não devolver nada (usuário voltou depois de meses sem
    varredura), cai para os últimos `limite` snapshots sem filtro de data — é
    melhor mostrar histórico antigo do que um gráfico vazio.

    Para watches com volume alto prefira `historico_precos_diario`, que agrega
    no banco em vez de trazer linha a linha.
    """
    snaps = _consulta_historico(session, watch_id, dias=dias, limite=limite)
    if not snaps and dias is not None:
        snaps = _consulta_historico(session, watch_id, dias=None, limite=limite)
    return snaps


def _consulta_historico(session, watch_id: int, *, dias: int | None, limite: int | None):
    stmt = select(PriceSnapshot).where(PriceSnapshot.watch_id == watch_id)
    if dias is not None:
        stmt = stmt.where(PriceSnapshot.coletado_em >= utcnow() - timedelta(days=dias))
    # DESC + LIMIT para pegar a ponta recente; reordena crescente em memória.
    stmt = stmt.order_by(PriceSnapshot.coletado_em.desc(), PriceSnapshot.id.desc())
    if limite is not None:
        stmt = stmt.limit(limite)
    return list(reversed(list(session.scalars(stmt))))


@dataclass(frozen=True)
class PontoHistorico:
    """Uma barra do histórico agregado: menor/maior preço de um destino num dia."""

    destino: str
    dia: str                 # 'YYYY-MM-DD' (texto, como o banco devolve)
    preco_min: Decimal
    preco_max: Decimal
    amostras: int


def historico_precos_diario(
    session, watch_id: int,
    *,
    dias: int | None = DIAS_HISTORICO_PADRAO,
) -> list[PontoHistorico]:
    """Histórico AGREGADO por (destino, dia) — feito no banco, não em Python.

    Alternativa barata a `historico_precos` para watches de REGIÃO: em vez de
    trazer 145.000 snapshots por rerun do Streamlit, traz no máximo
    `destinos x dias` linhas (18 x 90 = 1.620 no pior caso do watch "europa").
    O gráfico de série temporal fica igualmente legível usando `preco_min`.

    `func.date()` existe tanto no SQLite (dev/testes) quanto no PostgreSQL
    (Neon/produção, onde é o cast function-style para `date`). Em coluna
    `timestamptz` o Postgres resolve o dia no fuso da sessão; para um gráfico
    diário isso é irrelevante.
    """
    dia = func.date(PriceSnapshot.coletado_em).label("dia")
    stmt = (
        select(
            PriceSnapshot.destino,
            dia,
            func.min(PriceSnapshot.preco),
            func.max(PriceSnapshot.preco),
            func.count(PriceSnapshot.id),
        )
        .where(PriceSnapshot.watch_id == watch_id)
        .group_by(PriceSnapshot.destino, dia)
        .order_by(dia, PriceSnapshot.destino)
    )
    if dias is not None:
        stmt = stmt.where(PriceSnapshot.coletado_em >= utcnow() - timedelta(days=dias))
    return [
        PontoHistorico(
            destino=destino,
            dia=str(d),
            preco_min=Decimal(str(pmin)),
            preco_max=Decimal(str(pmax)),
            amostras=int(n),
        )
        for destino, d, pmin, pmax, n in session.execute(stmt)
    ]


def alertas_do_watch(session, watch_id: int, limite: int | None = 200) -> list[Alerta]:
    """Alertas do watch, do mais recente para o mais antigo.

    `limite` existe pelo mesmo motivo de `historico_precos`: a chamada acontece a
    cada rerun do Streamlit (uma linha vertical no gráfico por alerta) e um watch
    antigo pode ter milhares. `limite=None` traz tudo.
    """
    stmt = (
        select(Alerta)
        .where(Alerta.watch_id == watch_id)
        .order_by(Alerta.enviado_em.desc())
    )
    if limite is not None:
        stmt = stmt.limit(limite)
    return list(session.scalars(stmt))


def alertas_recentes(session, limite: int = 50) -> list[Alerta]:
    stmt = select(Alerta).order_by(Alerta.enviado_em.desc()).limit(limite)
    return list(session.scalars(stmt))
