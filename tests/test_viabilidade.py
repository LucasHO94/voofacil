"""Viabilidade dos critérios de um monitoramento — o defeito do watch #4.

O watch #4 de produção (RIO -> NAP, embarque EXATO 07/05/2027, desembarque
EXATO 22/05/2027, duração fixada em 14 noites) nunca coletou um único snapshot:
07/05 -> 22/05 são 15 noites, e nenhum par ida/volta satisfaz os três critérios
ao mesmo tempo. O painel aceitou criá-lo dizendo "Monitoramento criado!", a API
de queries gravou sem reclamar, e o único sinal foi um WARNING no stderr do
worker. O watch irmão #5, com o MESMO par de datas e sem duração fixada, coletou
R$ 5.410 pela TAP na mesma varredura.

Estes testes cobrem as três frentes da correção:
1. o oráculo puro (`voofacil.engine.viabilidade`) e sua concordância com o
   veredito de `_datas_da_busca`, que era quem já sabia a resposta;
2. as portas de gravação (criação E edição parcial em `engine/queries.py`);
3. os providers, que faziam (e pagavam) chamadas cujo resultado a engine
   descartava inteiro.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from voofacil.engine.orcamento import consumo_atual
from voofacil.engine.queries import atualizar_watch, criar_watch_regiao, criar_watch_rota
from voofacil.engine.viabilidade import (
    avisos_criterios,
    avisos_do_watch,
    janela_aceita,
    motivo_inviavel,
    motivo_inviavel_criterios,
    noites_implicitas,
    noites_implicitas_do_watch,
    recortes_do_mes,
)
from voofacil.models import TipoWatch, Watch
from voofacil.providers.serpapi_google import (
    BASE_URL as SERPAPI_URL,
    SerpApiProvider,
    _datas_da_busca,
)
from voofacil.providers.travelpayouts import BASE_URL as TP_URL, TravelpayoutsProvider

IDA = date(2027, 5, 7)
VOLTA = date(2027, 5, 22)      # 15 noites depois — o número que ninguém mostrava


def _watch(**kw) -> Watch:
    """Watch transiente com a forma do #4 (datas exatas, ida e volta)."""
    base = dict(
        id=4,
        nome="RIO -> NAP 07/05-22/05/2027",
        tipo=TipoWatch.ROTA.value,
        origem_iata="RIO",
        destino_iata="NAP",
        data_ini=IDA,
        data_fim=IDA,
        tipo_data_ida="EXATA",
        tipo_data_volta="EXATA",
        flexibilidade_dias=None,
        data_volta_ini=VOLTA,
        data_volta_fim=None,
        mes_especifico=None,
        ida_volta=True,
        duracao_min=None,
        duracao_max=None,
        preco_minimo=None,
        preco_limite=None,
    )
    base.update(kw)
    return Watch(**base)


# ==========================================================================
# 1. O oráculo puro
# ==========================================================================

def test_watch_4_e_recusado_citando_as_15_noites():
    """A recusa precisa DIZER o número que as datas implicam: sem ele o usuário
    não tem como saber qual dos três campos (ida, volta, duração) corrigir."""
    motivo = motivo_inviavel(_watch(duracao_min=14, duracao_max=14))

    assert motivo
    assert "15 noites" in motivo
    assert "14 noites" in motivo
    assert "07/05/2027" in motivo and "22/05/2027" in motivo


def test_watch_5_irmao_sem_duracao_fixada_e_viavel():
    """Mesmas datas, sem duração — é o watch que cotou R$ 5.410 pela TAP."""
    assert motivo_inviavel(_watch()) is None


def test_duracao_que_bate_com_as_datas_e_viavel():
    assert motivo_inviavel(_watch(duracao_min=15, duracao_max=15)) is None


@pytest.mark.parametrize("noites", [1, 7, 13, 14, 16, 21, 30])
def test_oraculo_concorda_com_datas_da_busca_em_toda_duracao_errada(noites):
    """O oráculo tem que dar exatamente o mesmo veredito de `_datas_da_busca`,
    que já sabia a resposta e a guardava só para si (devolvia None, pulava a
    chamada e logava). Com o par 07/05 -> 22/05, só 15 sobrevive."""
    w = _watch(duracao_min=noites, duracao_max=noites)

    assert _datas_da_busca(w) is None
    assert motivo_inviavel(w) is not None


def test_oraculo_concorda_com_datas_da_busca_no_caso_viavel():
    w = _watch(duracao_min=15, duracao_max=15)

    assert _datas_da_busca(w) == (IDA, VOLTA)
    assert motivo_inviavel(w) is None


def test_aproximada_com_folga_aceita_14_noites():
    """Falso positivo que uma checagem ingênua produziria: EXATA e APROXIMADA
    gravam AMBAS data_ini == data_fim == <alvo>, e a tolerância mora em
    flexibilidade_dias. Com ±3 dias, embarcar em 08/05 dá 14 noites."""
    w = _watch(tipo_data_ida="APROXIMADA", flexibilidade_dias=3,
               duracao_min=14, duracao_max=14)

    assert motivo_inviavel(w) is None
    assert _datas_da_busca(w) == (date(2027, 5, 8), VOLTA)


def test_aproximada_com_tolerancia_zero_se_comporta_como_exata():
    """`flex or None` no painel APAGA a tolerância quando o usuário digita 0, e
    a janela vira um ponto — o watch passa a ter o comportamento do #4."""
    w = _watch(tipo_data_ida="APROXIMADA", flexibilidade_dias=0,
               duracao_min=14, duracao_max=14)

    assert _datas_da_busca(w) is None
    assert motivo_inviavel(w) is not None
    assert any("tolerância 0" in a for a in avisos_do_watch(w))


def test_janela_de_volta_inteiramente_antes_da_de_ida():
    w = _watch(
        tipo_data_ida="INTERVALO", data_ini=date(2027, 5, 7), data_fim=date(2027, 5, 20),
        tipo_data_volta="INTERVALO",
        data_volta_ini=date(2027, 4, 1), data_volta_fim=date(2027, 4, 20),
    )

    motivo = motivo_inviavel(w)
    assert motivo and "antes" in motivo
    assert _datas_da_busca(w) is None


def test_somente_ida_com_janela_de_desembarque_orfa():
    """Bloqueio dos dois lados: a chamada é feita e PAGA em modo só-ida, a
    cotação volta sem retorno e o filtro de desembarque descarta tudo."""
    motivo = motivo_inviavel(_watch(ida_volta=False))

    assert motivo and "SOMENTE IDA" in motivo


def test_mes_especifico_fora_da_janela_de_embarque():
    motivo = motivo_inviavel(_watch(
        tipo_data_ida="INTERVALO", data_ini=date(2027, 5, 1), data_fim=date(2027, 5, 30),
        data_volta_ini=None, mes_especifico=8,
    ))

    assert motivo and "agosto" in motivo


def test_mes_especifico_que_intersecta_a_janela_e_viavel():
    assert motivo_inviavel(_watch(
        tipo_data_ida="INTERVALO", data_ini=date(2027, 4, 1), data_fim=date(2027, 5, 31),
        data_volta_ini=None, mes_especifico=4,
    )) is None


def test_regiao_sem_destinos_nao_tem_o_que_varrer():
    """Único caso invisível até para quem lê o log: o diagnóstico por destino
    vive DENTRO do laço, que com a lista vazia não executa."""
    motivo = motivo_inviavel_criterios(
        data_ini=IDA, data_fim=IDA, tipo=TipoWatch.REGIAO.value, destinos=[],
    )
    assert motivo and "destino" in motivo


def test_regiao_somente_ida_nao_tem_nenhuma_fonte():
    """O achado mais fácil de disparar de todos: um único checkbox. A TP
    descarta 100% dos só-ida (sempre reporta return_at) e a SerpApi só varre
    watches de ROTA."""
    motivo = motivo_inviavel_criterios(
        data_ini=IDA, data_fim=IDA, ida_volta=False,
        tipo=TipoWatch.REGIAO.value, destinos=["LIS", "MAD"],
    )
    assert motivo and "Somente ida" in motivo


def test_duracao_min_maior_que_max():
    motivo = motivo_inviavel(_watch(duracao_min=20, duracao_max=5))
    assert motivo and "duração mínima" in motivo


def test_piso_de_preco_acima_do_teto():
    motivo = motivo_inviavel(_watch(
        preco_minimo=Decimal("9000"), preco_limite=Decimal("3500"),
    ))
    assert motivo and "mínimo" in motivo


def test_ida_volta_none_conta_como_ida_e_volta():
    """`Watch.ida_volta` só ganha o default True no INSERT: um Watch transiente
    (providers e testes montam vários) chega com None. Tratar None como
    "somente ida" acusaria de impossível um watch redondo comum."""
    w = Watch(id=1, nome="x", tipo=TipoWatch.REGIAO.value, origem_iata="GRU",
              regiao_destinos=json.dumps(["LIS"]),
              data_ini=date(2026, 9, 1), data_fim=date(2026, 9, 30))

    assert w.ida_volta is None
    assert motivo_inviavel(w) is None


# ---------- helpers de geometria ----------

def test_noites_implicitas_de_datas_exatas():
    assert noites_implicitas_do_watch(_watch()) == (15, 15)


def test_noites_implicitas_de_janelas():
    assert noites_implicitas(
        data_ini=date(2026, 9, 1), data_fim=date(2026, 9, 10),
        tipo_data_ida="INTERVALO", tipo_data_volta="INTERVALO",
        data_volta_ini=date(2026, 9, 20), data_volta_fim=date(2026, 9, 28),
    ) == (10, 27)


def test_noites_implicitas_none_sem_janela_de_volta():
    assert noites_implicitas_do_watch(_watch(data_volta_ini=None)) is None


def test_janela_de_volta_aberta_a_esquerda_nao_vira_falso_positivo():
    """"Voltar ATÉ 22/05" (só o fim preenchido): `_bate_janela_data` aceita
    qualquer volta até o teto, mas `_janela_aceita` — que serve para ESCOLHER
    uma data de busca — colapsa no próprio teto. Uma estadia de 3 noites
    (embarque 07/05, volta 10/05) é aceita pela engine; se o oráculo usasse a
    janela estreita, ele a recusaria e passaria a barrar monitoramento bom."""
    w = _watch(
        tipo_data_volta="INTERVALO", data_volta_ini=None, data_volta_fim=VOLTA,
        duracao_min=3, duracao_max=3,
    )
    assert motivo_inviavel(w) is None

    # e o teto continua valendo: 20 noites passariam de 22/05
    assert motivo_inviavel(_watch(
        tipo_data_volta="INTERVALO", data_volta_ini=None, data_volta_fim=VOLTA,
        duracao_min=20, duracao_max=20,
    )) is not None


def test_janela_aceita_espelha_os_tres_tipos():
    assert janela_aceita("EXATA", IDA, date(2027, 5, 30), None) == (IDA, IDA)
    assert janela_aceita("APROXIMADA", IDA, None, 3) == (date(2027, 5, 4), date(2027, 5, 10))
    assert janela_aceita("INTERVALO", IDA, date(2027, 5, 30), None) == (IDA, date(2027, 5, 30))
    assert janela_aceita("INTERVALO", None, None, None) is None


def test_recortes_do_mes_atravessa_a_virada_do_ano():
    assert recortes_do_mes((date(2026, 11, 1), date(2027, 12, 31)), 5) == [
        (date(2027, 5, 1), date(2027, 5, 31)),
    ]
    assert recortes_do_mes((date(2027, 5, 1), date(2027, 5, 30)), 8) == []


# ---------- avisos (não bloqueiam) ----------

def test_aviso_somente_ida_explica_que_a_travelpayouts_nao_serve():
    avisos = avisos_criterios(
        data_ini=date(2027, 5, 1), data_fim=date(2027, 5, 30), ida_volta=False,
        tipo=TipoWatch.ROTA.value,
    )
    assert any("Travelpayouts" in a for a in avisos)


def test_aviso_open_jaw_depende_so_da_serpapi():
    w = _watch(origem_volta_iata="VCE")
    assert any("open-jaw" in a for a in avisos_do_watch(w))


def test_aviso_data_exata_contra_cache_esparso():
    assert any("único dia" in a for a in avisos_do_watch(_watch()))


def test_aviso_piso_sem_teto():
    avisos = avisos_criterios(
        data_ini=date(2027, 5, 1), data_fim=date(2027, 5, 30),
        preco_minimo=Decimal("9000"), preco_limite=None,
    )
    assert any("PISO" in a for a in avisos)


def test_aviso_exata_ignora_a_data_final_da_janela():
    avisos = avisos_do_watch(_watch(data_fim=date(2027, 5, 30)))
    assert any("ignorada" in a for a in avisos)


def test_watch_saudavel_nao_gera_aviso_algum():
    w = _watch(
        tipo_data_ida="INTERVALO", tipo_data_volta="INTERVALO",
        data_ini=date(2027, 5, 1), data_fim=date(2027, 5, 15),
        data_volta_ini=date(2027, 5, 20), data_volta_fim=date(2027, 5, 30),
    )
    assert avisos_do_watch(w) == []


# ==========================================================================
# 2. As portas de gravação — criação E edição parcial
# ==========================================================================

def test_criar_watch_rota_recusa_o_watch_4(session):
    with pytest.raises(ValueError, match="15 noites"):
        criar_watch_rota(
            session, "RIO -> NAP", "RIO", "NAP", IDA, IDA, None,
            tipo_data_ida="EXATA", tipo_data_volta="EXATA",
            data_volta_ini=VOLTA, duracao_min=14, duracao_max=14,
        )


def test_criar_watch_rota_recusa_duracao_invertida(session):
    """`atualizar_watch` já barrava coisas que a criação aceitava: watches
    impossíveis entravam pela porta da criação e depois não podiam nem ser
    corrigidos pela mesma trilha."""
    with pytest.raises(ValueError, match="duração mínima"):
        criar_watch_rota(
            session, "z", "RIO", "NAP", date(2027, 5, 1), date(2027, 5, 30), None,
            data_volta_ini=date(2027, 6, 1), duracao_min=30, duracao_max=2,
        )


def test_criar_watch_rota_recusa_janela_invertida(session):
    with pytest.raises(ValueError, match="antes"):
        criar_watch_rota(
            session, "z", "RIO", "NAP", date(2027, 5, 30), date(2027, 5, 1), None,
        )


def test_criar_watch_regiao_recusa_lista_vazia(session):
    with pytest.raises(ValueError, match="destino"):
        criar_watch_regiao(
            session, "z", "RIO", [], date(2027, 5, 1), date(2027, 5, 30), None,
        )


def test_criar_watch_regiao_recusa_somente_ida(session):
    with pytest.raises(ValueError, match="Somente ida"):
        criar_watch_regiao(
            session, "z", "RIO", ["LIS", "MAD"], date(2027, 5, 1), date(2027, 5, 30),
            None, ida_volta=False,
        )


def test_criar_watch_recusa_mes_especifico_fora_do_dominio(session):
    """A criação não validava nem o que a edição já validava."""
    with pytest.raises(ValueError, match="mes_especifico"):
        criar_watch_rota(
            session, "z", "RIO", "NAP", date(2027, 5, 1), date(2027, 5, 30), None,
            mes_especifico=13,
        )


def test_atualizar_watch_nao_deixa_matar_um_watch_saudavel(session):
    """A prova de que dava para editar um watch VÁLIDO até deixá-lo impossível:
    o #5 (que coleta) vira o #4 (que nunca coletou) com um único kwarg, sem
    exceção nenhuma, e o painel escrevia "Monitoramento atualizado!"."""
    w = criar_watch_rota(
        session, "RIO -> NAP válido", "RIO", "NAP", IDA, IDA, None,
        tipo_data_ida="EXATA", tipo_data_volta="EXATA", data_volta_ini=VOLTA,
    )
    assert _datas_da_busca(w) == (IDA, VOLTA)      # coleta

    with pytest.raises(ValueError, match="15 noites"):
        atualizar_watch(session, w.id, duracao_min=14, duracao_max=14)

    session.rollback()
    assert _datas_da_busca(session.get(Watch, w.id)) == (IDA, VOLTA)   # continua coletando


def test_atualizar_watch_cruza_o_valor_novo_com_o_que_esta_no_banco(session):
    """Edição PARCIAL: só data_ini/data_fim mesclavam MANTER com o banco antes
    de comparar. Mexer só na duração mínima produzia um estado que a criação
    recusaria em bloco."""
    w = criar_watch_rota(
        session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 5), None,
        data_volta_ini=date(2026, 9, 15), data_volta_fim=date(2026, 9, 25),
        duracao_min=10, duracao_max=14,
    )

    with pytest.raises(ValueError, match="duração"):
        atualizar_watch(session, w.id, duracao_min=40)

    session.rollback()
    assert session.get(Watch, w.id).duracao_min == 10


def test_atualizar_watch_recusa_so_ida_deixando_a_janela_de_volta_antiga(session):
    """Semântica MANTER: `atualizar_watch(id, ida_volta=False)` deixava a janela
    de volta intacta e transformava um watch saudável em watch morto que ainda
    por cima gastava cota SerpApi a cada varredura."""
    w = criar_watch_rota(
        session, "x", "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 5), None,
        data_volta_ini=date(2026, 9, 15), data_volta_fim=date(2026, 9, 25),
    )

    with pytest.raises(ValueError, match="SOMENTE IDA"):
        atualizar_watch(session, w.id, ida_volta=False)

    session.rollback()
    assert session.get(Watch, w.id).ida_volta is True


def test_recusa_nao_envenena_a_sessao_do_painel(session):
    """O painel guarda a MESMA Session pela vida da aba: uma recusa não pode
    deixar tudo o mais em PendingRollbackError."""
    with pytest.raises(ValueError):
        criar_watch_rota(
            session, "z", "RIO", "NAP", IDA, IDA, None,
            tipo_data_ida="EXATA", tipo_data_volta="EXATA",
            data_volta_ini=VOLTA, duracao_min=14, duracao_max=14,
        )

    ok = criar_watch_rota(session, "depois", "GRU", "LIS",
                          date(2026, 9, 1), date(2026, 9, 30), None)
    assert ok.id is not None


# ==========================================================================
# 3. Os providers não pagam por um watch impossível
# ==========================================================================

@respx.mock
def test_serpapi_sweep_nao_chama_api_com_mes_fora_da_janela(session):
    """Antes, `mes_especifico` não era reconciliado em lugar nenhum: a chamada
    era feita, o HTTP 200 voltava, o orçamento era debitado — e só então
    `_bate_mes_especifico` reprovava a cotação inteira. Nenhuma rota respx está
    registrada: se o código tentar a rede, o teste falha."""
    sp = SerpApiProvider(api_key="chave-teste", client=httpx.Client())
    w = _watch(
        tipo_data_ida="INTERVALO", data_ini=date(2027, 5, 1), data_fim=date(2027, 5, 30),
        tipo_data_volta="INTERVALO", data_volta_ini=None,
        mes_especifico=8, duracao_min=7, duracao_max=7,
    )

    assert sp.sweep(w, session=session) == []
    assert consumo_atual(session, "SERPAPI") == 0


@respx.mock
def test_serpapi_mira_o_mes_pedido_em_vez_do_meio_da_janela(session):
    """Variante silenciosa e mais traiçoeira: abril É alcançável dentro de
    01/04-31/05, mas `_data_alvo` devolvia o MEIO da janela (01/05) e a única
    chamada da varredura cotava sempre o mês errado."""
    from voofacil.engine.filtros import atende_criterios

    rota = respx.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json={
        "best_flights": [{"price": 3100, "flights": [{"airline": "TAP"}]}],
    }))
    sp = SerpApiProvider(api_key="chave-teste", client=httpx.Client())
    w = _watch(
        origem_iata="GRU", destino_iata="LIS",
        tipo_data_ida="INTERVALO", data_ini=date(2027, 4, 1), data_fim=date(2027, 5, 31),
        tipo_data_volta="INTERVALO", data_volta_ini=None,
        mes_especifico=4, duracao_min=7, duracao_max=7,
    )

    quotes = sp.sweep(w, session=session)

    saida = date.fromisoformat(rota.calls.last.request.url.params["outbound_date"])
    assert saida.month == 4, "a chamada tem que cair no mês que o watch exige"
    assert len(quotes) == 1
    assert atende_criterios(w, quotes[0]) is True   # a chamada não foi jogada fora


@respx.mock
def test_serpapi_sweep_explica_o_motivo_no_log(session, caplog):
    """O WARNING existia, mas descrevia os campos crus. Agora ele carrega a
    razão em português — a mesma frase que o painel mostra."""
    sp = SerpApiProvider(api_key="chave-teste", client=httpx.Client())
    with caplog.at_level(logging.WARNING):
        assert sp.sweep(_watch(duracao_min=14, duracao_max=14), session=session) == []

    assert "15 noites" in caplog.text


@respx.mock
def test_travelpayouts_sweep_nao_faz_chamada_em_watch_impossivel(caplog):
    """O sweep da TP não tinha checagem nenhuma: montava as consultas, fazia o
    HTTP e deixava a engine jogar tudo fora — enquanto o log por destino ainda
    reportava "N cotações úteis"."""
    tp = TravelpayoutsProvider(token="tok", client=httpx.Client(base_url=TP_URL))

    with caplog.at_level(logging.WARNING):
        assert tp.sweep(_watch(duracao_min=14, duracao_max=14)) == []

    assert "15 noites" in caplog.text


@respx.mock
def test_travelpayouts_avisa_que_so_ida_sera_descartado(caplog):
    """Medido em 4 rotas: ZERO itens sem `return_at`. Todo item que sai daqui é
    descartado pela engine num watch só-ida — e isso não deixava rastro."""
    respx.get(f"{TP_URL}/v1/prices/calendar").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {}})
    )
    tp = TravelpayoutsProvider(token="tok", client=httpx.Client(base_url=TP_URL))
    w = _watch(tipo_data_ida="INTERVALO", tipo_data_volta="INTERVALO",
               data_volta_ini=None, ida_volta=False)

    with caplog.at_level(logging.WARNING):
        tp.sweep(w)

    assert "somente ida" in caplog.text
    assert "SerpApi" in caplog.text


def test_travelpayouts_avisa_regiao_sem_destino(caplog):
    """Antes o laço por destino não executava e o provider retornava [] em
    silêncio absoluto — nem uma linha de log."""
    tp = TravelpayoutsProvider(token="tok", client=httpx.Client(base_url=TP_URL))
    w = Watch(id=9, nome="vazio", tipo=TipoWatch.ROTA.value, origem_iata="RIO",
              destino_iata=None, data_ini=date(2027, 5, 1), data_fim=date(2027, 5, 30))

    with caplog.at_level(logging.WARNING):
        assert tp.sweep(w) == []

    assert "nenhum destino" in caplog.text
