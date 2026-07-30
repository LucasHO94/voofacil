"""Testes de voofacil.engine.filtros.atende_criterios — cada critério isolado."""
from datetime import date
from decimal import Decimal

from voofacil.engine.filtros import atende_criterios, atende_criterios_descoberta
from voofacil.models import TipoData, TipoWatch, Watch
from voofacil.schemas import PriceQuote


def _watch(session, **kw):
    base = dict(
        nome="GRU -> LIS",
        tipo=TipoWatch.ROTA.value,
        origem_iata="GRU",
        destino_iata="LIS",
        data_ini=date(2026, 9, 1),
        data_fim=date(2026, 9, 30),
    )
    base.update(kw)
    w = Watch(**base)
    session.add(w)
    session.commit()
    return w


def _quote(**kw):
    base = dict(
        origem="GRU",
        destino="LIS",
        data_ida=date(2026, 9, 10),
        data_volta=date(2026, 9, 24),
        preco=Decimal("3000"),
        fonte="TP_CACHE",
    )
    base.update(kw)
    return PriceQuote(**base)


# --- data de ida: EXATA ---

def test_data_ida_exata_bate(session):
    w = _watch(session, tipo_data_ida=TipoData.EXATA.value, data_ini=date(2026, 9, 10))
    q = _quote(data_ida=date(2026, 9, 10))
    assert atende_criterios(w, q) is True


def test_data_ida_exata_nao_bate(session):
    w = _watch(session, tipo_data_ida=TipoData.EXATA.value, data_ini=date(2026, 9, 10))
    q = _quote(data_ida=date(2026, 9, 11))
    assert atende_criterios(w, q) is False


# --- data de ida: APROXIMADA ---

def test_data_ida_aproximada_dentro_tolerancia(session):
    w = _watch(
        session, tipo_data_ida=TipoData.APROXIMADA.value,
        data_ini=date(2026, 9, 10), flexibilidade_dias=3,
    )
    q = _quote(data_ida=date(2026, 9, 13))
    assert atende_criterios(w, q) is True


def test_data_ida_aproximada_fora_tolerancia(session):
    w = _watch(
        session, tipo_data_ida=TipoData.APROXIMADA.value,
        data_ini=date(2026, 9, 10), flexibilidade_dias=3,
    )
    q = _quote(data_ida=date(2026, 9, 14))
    assert atende_criterios(w, q) is False


# --- data de ida: INTERVALO ---

def test_data_ida_intervalo_dentro(session):
    w = _watch(
        session, tipo_data_ida=TipoData.INTERVALO.value,
        data_ini=date(2026, 9, 1), data_fim=date(2026, 9, 30),
    )
    q = _quote(data_ida=date(2026, 9, 15))
    assert atende_criterios(w, q) is True


def test_data_ida_intervalo_fora(session):
    w = _watch(
        session, tipo_data_ida=TipoData.INTERVALO.value,
        data_ini=date(2026, 9, 1), data_fim=date(2026, 9, 30),
    )
    q = _quote(data_ida=date(2026, 10, 1), data_volta=date(2026, 10, 15))
    assert atende_criterios(w, q) is False


# --- data de volta (janela independente) ---

def test_data_volta_exata_bate(session):
    w = _watch(session, tipo_data_volta=TipoData.EXATA.value, data_volta_ini=date(2026, 9, 24))
    q = _quote(data_volta=date(2026, 9, 24))
    assert atende_criterios(w, q) is True


def test_data_volta_exata_nao_bate(session):
    w = _watch(session, tipo_data_volta=TipoData.EXATA.value, data_volta_ini=date(2026, 9, 24))
    q = _quote(data_volta=date(2026, 9, 25))
    assert atende_criterios(w, q) is False


def test_data_volta_intervalo_dentro(session):
    w = _watch(
        session, tipo_data_volta=TipoData.INTERVALO.value,
        data_volta_ini=date(2026, 9, 20), data_volta_fim=date(2026, 9, 28),
    )
    q = _quote(data_volta=date(2026, 9, 24))
    assert atende_criterios(w, q) is True


def test_data_volta_intervalo_fora(session):
    w = _watch(
        session, tipo_data_volta=TipoData.INTERVALO.value,
        data_volta_ini=date(2026, 9, 20), data_volta_fim=date(2026, 9, 28),
    )
    q = _quote(data_volta=date(2026, 9, 29))
    assert atende_criterios(w, q) is False


def test_data_volta_sem_janela_nao_filtra(session):
    """data_volta_ini/fim None: mantém comportamento atual (calendário decide o retorno)."""
    w = _watch(session)
    q = _quote(data_volta=date(2026, 12, 31))
    assert atende_criterios(w, q) is True


def test_data_volta_so_com_limite_superior_filtra(session):
    """Janela aberta à esquerda (só data_volta_fim): o teto tem de valer.
    Antes caía no atalho `ini is None -> True` e não filtrava nada — "voltar
    até 28/09" aceitava uma volta em dezembro."""
    w = _watch(session, data_volta_ini=None, data_volta_fim=date(2026, 9, 28))
    assert atende_criterios(w, _quote(data_volta=date(2026, 9, 24))) is True
    assert atende_criterios(w, _quote(data_volta=date(2026, 12, 31))) is False


# --- mês específico (isolado da janela de data via range amplo) ---

def test_mes_especifico_bate(session):
    w = _watch(session, data_ini=date(2026, 9, 1), data_fim=date(2026, 12, 31), mes_especifico=9)
    q = _quote(data_ida=date(2026, 9, 5), data_volta=date(2026, 9, 19))
    assert atende_criterios(w, q) is True


def test_mes_especifico_nao_bate(session):
    w = _watch(session, data_ini=date(2026, 9, 1), data_fim=date(2026, 12, 31), mes_especifico=9)
    q = _quote(data_ida=date(2026, 10, 5), data_volta=date(2026, 10, 19))
    assert atende_criterios(w, q) is False


# --- duração da estadia ---

def test_duracao_min_bate(session):
    w = _watch(session, duracao_min=10)
    q = _quote(data_ida=date(2026, 9, 10), data_volta=date(2026, 9, 21))  # 11 noites
    assert atende_criterios(w, q) is True


def test_duracao_min_nao_bate(session):
    w = _watch(session, duracao_min=10)
    q = _quote(data_ida=date(2026, 9, 10), data_volta=date(2026, 9, 15))  # 5 noites
    assert atende_criterios(w, q) is False


def test_duracao_max_bate(session):
    w = _watch(session, duracao_max=14)
    q = _quote(data_ida=date(2026, 9, 10), data_volta=date(2026, 9, 20))  # 10 noites
    assert atende_criterios(w, q) is True


def test_duracao_max_nao_bate(session):
    w = _watch(session, duracao_max=14)
    q = _quote(data_ida=date(2026, 9, 10), data_volta=date(2026, 9, 30))  # 20 noites
    assert atende_criterios(w, q) is False


# --- ida_volta vs somente-ida ---

def test_ida_volta_rejeita_quote_sem_volta(session):
    w = _watch(session, ida_volta=True)
    q = _quote(data_volta=None)
    assert atende_criterios(w, q) is False


def test_ida_volta_aceita_quote_com_volta(session):
    w = _watch(session, ida_volta=True)
    q = _quote(data_volta=date(2026, 9, 24))
    assert atende_criterios(w, q) is True


def test_somente_ida_rejeita_quote_com_volta(session):
    w = _watch(session, ida_volta=False)
    q = _quote(data_volta=date(2026, 9, 24))
    assert atende_criterios(w, q) is False


def test_somente_ida_aceita_quote_sem_volta(session):
    w = _watch(session, ida_volta=False)
    q = _quote(data_volta=None)
    assert atende_criterios(w, q) is True


# --- preco_minimo ---

def test_preco_minimo_bate(session):
    w = _watch(session, preco_minimo=Decimal("2000"))
    q = _quote(preco=Decimal("2500"))
    assert atende_criterios(w, q) is True


def test_preco_minimo_nao_bate(session):
    w = _watch(session, preco_minimo=Decimal("2000"))
    q = _quote(preco=Decimal("1500"))
    assert atende_criterios(w, q) is False


def test_sem_preco_minimo_nao_filtra(session):
    w = _watch(session)
    q = _quote(preco=Decimal("1"))
    assert atende_criterios(w, q) is True


# --- atende_criterios_descoberta (fontes de discovery, ex.: SerpApi Explore,
# que nunca reportam data_volta) ---

def _quote_descoberta(**kw):
    """Cotação típica do Explore: só ida, sem data de volta."""
    base = dict(
        origem="GRU", destino="LIS", data_ida=date(2026, 9, 10),
        data_volta=None, preco=Decimal("3000"), fonte="SERPAPI",
    )
    base.update(kw)
    return PriceQuote(**base)


def test_descoberta_nao_exige_data_volta_em_watch_ida_volta(session):
    """Este é o bug que atende_criterios (estrito) causaria: um watch default
    (ida_volta=True) rejeitaria 100% das cotações do Explore, que nunca tem
    data_volta. atende_criterios_descoberta não filtra por ida_volta/duração."""
    w = _watch(session, ida_volta=True)
    q = _quote_descoberta()
    assert atende_criterios(w, q) is False          # o filtro estrito rejeitaria
    assert atende_criterios_descoberta(w, q) is True  # o de descoberta aceita


def test_descoberta_aplica_janela_de_data_ida(session):
    w = _watch(session, tipo_data_ida=TipoData.EXATA.value, data_ini=date(2026, 9, 10))
    assert atende_criterios_descoberta(w, _quote_descoberta(data_ida=date(2026, 9, 10))) is True
    assert atende_criterios_descoberta(w, _quote_descoberta(data_ida=date(2026, 9, 11))) is False


def test_descoberta_aplica_mes_especifico(session):
    w = _watch(session, data_ini=date(2026, 9, 1), data_fim=date(2026, 12, 31), mes_especifico=9)
    assert atende_criterios_descoberta(w, _quote_descoberta(data_ida=date(2026, 9, 5))) is True
    assert atende_criterios_descoberta(w, _quote_descoberta(data_ida=date(2026, 10, 5))) is False


def test_descoberta_aplica_preco_minimo(session):
    w = _watch(session, preco_minimo=Decimal("2000"))
    assert atende_criterios_descoberta(w, _quote_descoberta(preco=Decimal("2500"))) is True
    assert atende_criterios_descoberta(w, _quote_descoberta(preco=Decimal("1500"))) is False


def test_descoberta_ignora_duracao_e_janela_de_volta(session):
    """Mesmo com duracao_min/max ou janela de volta definidas, a descoberta
    (sem data_volta) não é rejeitada por isso — a confirmação ao vivo valida depois."""
    w = _watch(session, duracao_min=10, duracao_max=14,
               data_volta_ini=date(2026, 9, 20), data_volta_fim=date(2026, 9, 24))
    assert atende_criterios_descoberta(w, _quote_descoberta()) is True
