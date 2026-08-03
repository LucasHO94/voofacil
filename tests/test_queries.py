"""Testes das consultas/mutações usadas pelo painel Streamlit."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from voofacil.engine.queries import (
    alertas_do_watch,
    alertas_recentes,
    alternar_ativo,
    atualizar_watch,
    criar_watch_regiao,
    criar_watch_rota,
    excluir_watch,
    historico_precos,
    historico_precos_diario,
    listar_watches,
)
from voofacil.engine.store import persistir_quotes
from voofacil.models import Alerta, PriceSnapshot, Watch, utcnow
from voofacil.schemas import PriceQuote


def _snapshot(session, watch, *, preco="3000", destino="LIS", dias_atras=0, minutos=0):
    """Cria e persiste um snapshot com `coletado_em` controlado."""
    snap = PriceSnapshot(
        watch_id=watch.id,
        origem=watch.origem_iata,
        destino=destino,
        data_ida=date(2026, 9, 10),
        preco=Decimal(preco),
        fonte="TP_CACHE",
        coletado_em=utcnow() - timedelta(days=dias_atras, minutes=minutos),
    )
    session.add(snap)
    session.commit()
    return snap


def test_criar_watch_rota(session):
    w = criar_watch_rota(
        session, "GRU -> LIS", "gru", "lis",
        date(2026, 9, 1), date(2026, 9, 30), Decimal("3500"),
    )
    assert w.id is not None
    assert w.origem_iata == "GRU"
    assert w.destino_iata == "LIS"
    assert listar_watches(session) == [w]


def test_criar_watch_rota_com_criterios_avancados(session):
    w = criar_watch_rota(
        session, "GRU -> LIS set/26 flex", "gru", "lis",
        date(2026, 9, 1), date(2026, 9, 30), Decimal("3500"),
        ida_volta=True,
        duracao_min=10,
        duracao_max=20,
        tipo_data_ida="APROXIMADA",
        tipo_data_volta="INTERVALO",
        flexibilidade_dias=3,
        data_volta_ini=date(2026, 9, 15),
        data_volta_fim=date(2026, 10, 5),
        mes_especifico=9,
        preco_minimo=Decimal("1500"),
        preco_aproximado=Decimal("2800"),
    )

    assert w.id is not None
    assert w.duracao_min == 10
    assert w.duracao_max == 20
    assert w.tipo_data_ida == "APROXIMADA"
    assert w.tipo_data_volta == "INTERVALO"
    assert w.flexibilidade_dias == 3
    assert w.data_volta_ini == date(2026, 9, 15)
    assert w.data_volta_fim == date(2026, 10, 5)
    assert w.mes_especifico == 9
    assert w.preco_minimo == Decimal("1500")
    assert w.preco_aproximado == Decimal("2800")


def test_criar_watch_regiao_com_criterios_avancados(session):
    w = criar_watch_regiao(
        session, "GRU -> Europa flex", "gru", ["lis", "mad"],
        date(2026, 9, 1), date(2026, 11, 30), None,
        mes_especifico=10,
        preco_minimo=Decimal("1000"),
    )

    assert w.mes_especifico == 10
    assert w.preco_minimo == Decimal("1000")
    # defaults preservados para os demais kwargs novos
    assert w.tipo_data_ida == "INTERVALO"
    assert w.tipo_data_volta == "INTERVALO"


def test_criar_watch_regiao_normaliza_destinos(session):
    w = criar_watch_regiao(
        session, "GRU -> Europa", "gru", ["lis", "mad"],
        date(2026, 9, 1), date(2026, 11, 30), None,
        arrival_area_id="/m/02j9z",
    )
    assert w.destinos == ["LIS", "MAD"]
    assert w.arrival_area_id == "/m/02j9z"


def test_alternar_ativo(session):
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    assert w.ativo is True

    alternar_ativo(session, w.id)
    assert session.get(type(w), w.id).ativo is False

    alternar_ativo(session, w.id)
    assert session.get(type(w), w.id).ativo is True


def test_excluir_watch_remove_do_banco(session):
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    wid = w.id

    excluir_watch(session, wid)

    assert listar_watches(session) == []
    assert session.get(type(w), wid) is None


def test_excluir_watch_inexistente_nao_quebra(session):
    assert excluir_watch(session, 9999) is False   # não deve lançar


def test_excluir_watch_com_snapshots_e_alertas(session):
    """Bloqueador D1: um clique na lixeira de um watch que já coletou preço
    derrubava o painel inteiro (IntegrityError NOT NULL em
    price_snapshot.watch_id)."""
    w = criar_watch_rota(session, "RIO -> NAP", "RIO", "NAP",
                         date(2027, 5, 7), date(2027, 5, 7), Decimal("3500"))
    outro = criar_watch_rota(session, "intocado", "GRU", "LIS",
                             date(2026, 9, 1), date(2026, 9, 30), None)
    snap = _snapshot(session, w, destino="NAP", preco="3400")
    snap_outro = _snapshot(session, outro, preco="2500")
    session.add(Alerta(watch_id=w.id, snapshot_id=snap.id, gatilho="LIMITE",
                       preco=snap.preco, canal="TELEGRAM"))
    session.commit()
    wid = w.id

    assert excluir_watch(session, wid) is True

    assert session.get(Watch, wid) is None
    assert session.scalars(
        select(PriceSnapshot).where(PriceSnapshot.watch_id == wid)).all() == []
    assert session.scalars(select(Alerta).where(Alerta.watch_id == wid)).all() == []
    # o vizinho continua intacto — a exclusão é cirúrgica
    assert session.get(Watch, outro.id) is not None
    assert session.get(PriceSnapshot, snap_outro.id) is not None


def test_excluir_watch_nao_envenena_a_sessao(session):
    """O painel guarda a MESMA Session em st.session_state: depois de excluir,
    tudo o mais tem de continuar funcionando sem recarregar a página."""
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    snap = _snapshot(session, w)
    session.add(Alerta(watch_id=w.id, snapshot_id=snap.id, gatilho="LIMITE",
                       preco=snap.preco, canal="EMAIL"))
    session.commit()

    excluir_watch(session, w.id)

    novo = criar_watch_rota(session, "depois", "GRU", "MAD",
                            date(2026, 9, 1), date(2026, 9, 30), None)
    assert novo.id is not None
    assert [x.id for x in listar_watches(session)] == [novo.id]


def test_excluir_watch_com_colecoes_ja_carregadas(session):
    """O gráfico do painel carrega w.snapshots antes de o usuário clicar na
    lixeira; a exclusão precisa funcionar com a coleção já em memória."""
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    _snapshot(session, w)
    assert len(w.snapshots) == 1   # força o carregamento

    assert excluir_watch(session, w.id) is True
    assert session.scalars(select(PriceSnapshot)).all() == []


def test_historico_precos_ordenado(session):
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    persistir_quotes(session, w, [
        PriceQuote(origem="GRU", destino="LIS", data_ida=date(2026, 9, 10),
                   preco=Decimal("3100"), fonte="TP_CACHE"),
        PriceQuote(origem="GRU", destino="LIS", data_ida=date(2026, 9, 15),
                   preco=Decimal("2900"), fonte="TP_CACHE"),
    ])
    session.commit()

    hist = historico_precos(session, w.id)
    assert [h.preco for h in hist] == [Decimal("3100"), Decimal("2900")]


def test_historico_precos_respeita_limite_e_fica_com_os_recentes(session):
    """D14: sem LIMIT, um watch de REGIÃO traria ~145 mil linhas por rerun."""
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    for i in range(10):
        _snapshot(session, w, preco=str(3000 + i), minutos=10 - i)

    hist = historico_precos(session, w.id, limite=3)

    assert len(hist) == 3
    # ordem crescente preservada e ficamos com a ponta MAIS RECENTE
    assert [h.preco for h in hist] == [Decimal("3007"), Decimal("3008"), Decimal("3009")]


def test_historico_precos_recorta_por_dias(session):
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    _snapshot(session, w, preco="2000", dias_atras=400)
    _snapshot(session, w, preco="3000", dias_atras=1)

    assert [h.preco for h in historico_precos(session, w.id, dias=90)] == [Decimal("3000")]
    assert len(historico_precos(session, w.id, dias=None, limite=None)) == 2


def test_historico_precos_cai_para_dados_antigos_se_janela_vazia(session):
    """Melhor mostrar histórico velho do que gráfico vazio quando o usuário
    voltou depois de meses sem varredura."""
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    _snapshot(session, w, preco="2000", dias_atras=400)

    assert [h.preco for h in historico_precos(session, w.id, dias=90)] == [Decimal("2000")]


def test_historico_precos_diario_agrega_por_destino_e_dia(session):
    w = criar_watch_regiao(session, "GRU -> Europa", "gru", ["lis", "mad"],
                           date(2026, 9, 1), date(2026, 11, 30), None)
    _snapshot(session, w, destino="LIS", preco="3000")
    _snapshot(session, w, destino="LIS", preco="2500")
    _snapshot(session, w, destino="MAD", preco="4000")

    pontos = historico_precos_diario(session, w.id)

    assert len(pontos) == 2       # 2 destinos x 1 dia, contra 3 snapshots crus
    por_destino = {p.destino: p for p in pontos}
    assert por_destino["LIS"].preco_min == Decimal("2500")
    assert por_destino["LIS"].preco_max == Decimal("3000")
    assert por_destino["LIS"].amostras == 2
    assert por_destino["MAD"].amostras == 1


def test_alertas_do_watch_respeita_limite(session):
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    snap = _snapshot(session, w)
    for _ in range(5):
        session.add(Alerta(watch_id=w.id, snapshot_id=snap.id, gatilho="LIMITE",
                           preco=snap.preco, canal="TELEGRAM"))
    session.commit()

    assert len(alertas_do_watch(session, w.id, limite=2)) == 2
    assert len(alertas_do_watch(session, w.id, limite=None)) == 5


def test_atualizar_watch_altera_so_o_que_foi_informado(session):
    # A data de volta usada aqui era 22/05/2027 — 263 noites depois do embarque
    # de 01/09/2026, contra uma duração de 7-14 noites que o teste MANTINHA. Ou
    # seja: o próprio teste montava um watch impossível para provar que campos
    # não informados não mudam. Trocada por uma volta coerente com a duração,
    # que é o que o teste realmente quer verificar.
    w = criar_watch_rota(session, "antigo", "GRU", "LIS",
                         date(2026, 9, 1), date(2026, 9, 30), Decimal("3500"),
                         duracao_min=7, duracao_max=14)

    atualizar_watch(session, w.id, nome="RIO -> NAP", origem="rio", destino="nap",
                    preco_limite=Decimal("3200"), tipo_data_ida="EXATA",
                    data_volta_ini=date(2026, 9, 10), ativo=False)

    w = session.get(Watch, w.id)
    assert w.nome == "RIO -> NAP"
    assert (w.origem_iata, w.destino_iata) == ("RIO", "NAP")
    assert w.preco_limite == Decimal("3200")
    assert w.tipo_data_ida == "EXATA"
    assert w.data_volta_ini == date(2026, 9, 10)
    assert w.ativo is False
    # não informados ficam como estavam
    assert (w.duracao_min, w.duracao_max) == (7, 14)
    assert w.tipo_data_volta == "INTERVALO"


def test_atualizar_watch_none_apaga_campo(session):
    """None é valor, não ausência: limpa o campo de verdade."""
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30),
                         Decimal("3500"), preco_minimo=Decimal("1000"))

    atualizar_watch(session, w.id, preco_limite=None, preco_minimo=None)

    w = session.get(Watch, w.id)
    assert w.preco_limite is None
    assert w.preco_minimo is None


def test_atualizar_watch_troca_tipo_nos_dois_sentidos(session):
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)

    atualizar_watch(session, w.id, destinos=["lis", "mad"], arrival_area_id="/m/02j9z")
    w = session.get(Watch, w.id)
    assert w.tipo == "REGIAO"
    assert w.destinos == ["LIS", "MAD"]
    assert w.destino_iata is None
    assert w.arrival_area_id == "/m/02j9z"

    atualizar_watch(session, w.id, destino="nap")
    w = session.get(Watch, w.id)
    assert w.tipo == "ROTA"
    assert w.destinos == ["NAP"]
    assert w.regiao_destinos is None
    assert w.arrival_area_id is None


def test_atualizar_watch_inexistente_retorna_none(session):
    assert atualizar_watch(session, 9999, nome="x") is None


@pytest.mark.parametrize("kwargs", [
    {"tipo_data_ida": "QUALQUER"},
    {"tipo_data_volta": "QUALQUER"},
    {"mes_especifico": 13},
    {"destino": "NAP", "destinos": ["LIS"]},
    {"destinos": []},
    {"data_ini": date(2027, 6, 1), "data_fim": date(2027, 1, 1)},
])
def test_atualizar_watch_valida_antes_de_gravar(session, kwargs):
    """Erro de validação não pode chegar ao banco nem deixar a Session do
    painel em PendingRollbackError."""
    w = criar_watch_rota(session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)

    with pytest.raises(ValueError):
        atualizar_watch(session, w.id, **kwargs)

    session.rollback()   # descarta as alterações em memória do objeto
    w = session.get(Watch, w.id)
    assert w.nome == "x"
    assert w.destino_iata == "LIS"
    # sessão continua utilizável
    assert len(listar_watches(session)) == 1


def test_alertas_do_watch_e_recentes(session):
    w1 = criar_watch_rota(session, "a", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), None)
    w2 = criar_watch_rota(session, "b", "GRU", "MAD", date(2026, 9, 1), date(2026, 9, 30), None)
    snap = persistir_quotes(session, w1, [
        PriceQuote(origem="GRU", destino="LIS", data_ida=date(2026, 9, 10),
                   preco=Decimal("2900"), fonte="TP_CACHE"),
    ])[0]
    session.add(Alerta(watch_id=w1.id, snapshot_id=snap.id, gatilho="LIMITE",
                       preco=snap.preco, canal="TELEGRAM"))
    session.commit()

    assert len(alertas_do_watch(session, w1.id)) == 1
    assert len(alertas_do_watch(session, w2.id)) == 0
    assert len(alertas_recentes(session)) == 1
