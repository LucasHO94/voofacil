"""Testes da confirmação ao vivo (SerpApi) e sua degradação graciosa."""
from datetime import date
from decimal import Decimal

from farewatch.engine.confirmacao import confirmar_candidatos
from farewatch.engine.orcamento import registrar_chamada
from farewatch.schemas import Candidato, PriceQuote


def _candidato(preco="2890.00", snapshot_id=1) -> Candidato:
    return Candidato(
        quote=PriceQuote(
            origem="GRU", destino="LIS", data_ida=date(2026, 9, 10),
            data_volta=date(2026, 9, 24), preco=Decimal(preco), fonte="TP_CACHE",
        ),
        watch_id=1,
        snapshot_id=snapshot_id,
        gatilho="LIMITE",
    )


class FakeSerpApi:
    nome = "SERPAPI"
    configurado = True

    def __init__(self, preco_confirmado=None, explode=False):
        self.preco_confirmado = preco_confirmado
        self.explode = explode
        self.chamadas = 0

    def confirmar_google_flights(self, origem, destino, data_ida, data_volta=None):
        self.chamadas += 1
        if self.explode:
            raise RuntimeError("falha simulada")
        if self.preco_confirmado is None:
            return None
        return PriceQuote(
            origem=origem, destino=destino, data_ida=data_ida, data_volta=data_volta,
            preco=Decimal(self.preco_confirmado), fonte="SERPAPI",
            deep_link="https://www.google.com/travel/flights?d=LIS",
        )


def test_confirma_e_atualiza_preco_ao_vivo(session):
    provider = FakeSerpApi(preco_confirmado="2950.00")
    resultado = confirmar_candidatos(session, [_candidato()], provider)

    assert len(resultado) == 1
    assert resultado[0].confirmado_ao_vivo is True
    assert resultado[0].quote.preco == Decimal("2950.00")
    assert resultado[0].quote.fonte == "SERPAPI"
    assert resultado[0].snapshot_id == 1   # mantém o vínculo com o snapshot original


def test_sem_resultado_ao_vivo_mantem_candidato_original(session):
    provider = FakeSerpApi(preco_confirmado=None)
    resultado = confirmar_candidatos(session, [_candidato()], provider)

    assert resultado[0].confirmado_ao_vivo is False
    assert resultado[0].quote.preco == Decimal("2890.00")   # preço do cache preservado


def test_falha_na_chamada_degrada_sem_quebrar(session):
    provider = FakeSerpApi(explode=True)
    resultado = confirmar_candidatos(session, [_candidato()], provider)

    assert resultado[0].confirmado_ao_vivo is False


def test_sem_provider_configurado_pula_confirmacao(session):
    class NaoConfigurado:
        nome = "SERPAPI"
        configurado = False

    resultado = confirmar_candidatos(session, [_candidato()], NaoConfigurado())
    assert resultado[0].confirmado_ao_vivo is False


def test_orcamento_esgotado_nao_chama_api(session):
    from datetime import date as date_cls
    hoje = date_cls.today()
    for _ in range(250):
        registrar_chamada(session, "SERPAPI", hoje)

    provider = FakeSerpApi(preco_confirmado="1.00")
    resultado = confirmar_candidatos(session, [_candidato()], provider)

    assert provider.chamadas == 0
    assert resultado[0].confirmado_ao_vivo is False
