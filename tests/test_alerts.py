"""Testes dos canais de alerta e do despachante."""
import json
from datetime import date
from decimal import Decimal

import httpx
import respx

from farewatch.alerts.dispatcher import despachar
from farewatch.alerts.mensagem import fmt_brl, montar_mensagem
from farewatch.alerts.telegram import API_URL, TelegramChannel
from farewatch.models import Alerta, TipoWatch, Watch
from farewatch.schemas import Candidato, PriceQuote


def _watch(session, **kw):
    base = dict(
        nome="GRU -> LIS set/26",
        tipo=TipoWatch.ROTA.value,
        origem_iata="GRU",
        destino_iata="LIS",
        data_ini=date(2026, 9, 1),
        data_fim=date(2026, 9, 30),
        preco_limite=Decimal("3500"),
    )
    base.update(kw)
    w = Watch(**base)
    session.add(w)
    session.flush()
    return w


def _candidato(watch_id: int, snapshot_id: int = 1) -> Candidato:
    return Candidato(
        quote=PriceQuote(
            origem="GRU",
            destino="LIS",
            data_ida=date(2026, 9, 15),
            data_volta=date(2026, 9, 29),
            preco=Decimal("2890.00"),
            cia="LA",
            escalas=0,
            fonte="TP_CACHE",
            deep_link="https://www.aviasales.com/search/GRU1509LIS29091",
        ),
        watch_id=watch_id,
        snapshot_id=snapshot_id,
        gatilho="LIMITE",
        baseline_min=Decimal("3100.00"),
    )


def test_fmt_brl():
    assert fmt_brl(Decimal("2890.5")) == "R$ 2.890,50"
    assert fmt_brl(Decimal("1234567.89")) == "R$ 1.234.567,89"


def test_montar_mensagem_conteudo(session):
    w = _watch(session)
    msg = montar_mensagem(_candidato(w.id), w)

    assert "GRU → LIS" in msg.html
    assert "R$ 2.890,00" in msg.html
    assert "R$ 3.500,00" in msg.html          # limite do watch
    assert "R$ 3.100,00" in msg.html          # menor histórico
    assert "cache" in msg.html                 # não confirmado ao vivo
    assert msg.deep_link.endswith("GRU1509LIS29091")
    assert "LIMITE" in msg.assunto


@respx.mock
def test_telegram_envia_com_botao(session):
    w = _watch(session)
    rota = respx.post(f"{API_URL}/bot123:abc/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    canal = TelegramChannel(token="123:abc", chat_id="999",
                            client=httpx.Client(base_url=API_URL))
    ok = canal.enviar(montar_mensagem(_candidato(w.id), w))

    assert ok is True
    corpo = json.loads(rota.calls[0].request.content)
    assert corpo["chat_id"] == "999"
    assert corpo["parse_mode"] == "HTML"
    teclado = json.loads(corpo["reply_markup"])
    assert teclado["inline_keyboard"][0][0]["url"].startswith("https://www.aviasales.com")


@respx.mock
def test_telegram_falha_http_retorna_false(session):
    w = _watch(session)
    respx.post(f"{API_URL}/bot123:abc/sendMessage").mock(
        return_value=httpx.Response(500)
    )
    canal = TelegramChannel(token="123:abc", chat_id="999",
                            client=httpx.Client(base_url=API_URL))

    assert canal.enviar(montar_mensagem(_candidato(w.id), w)) is False


def test_telegram_sem_config_nao_envia():
    canal = TelegramChannel(token="", chat_id="")
    assert canal.configurado is False


class CanalFake:
    def __init__(self, nome="FAKE", sucesso=True):
        self.nome = nome
        self.sucesso = sucesso
        self.mensagens = []

    def enviar(self, msg):
        self.mensagens.append(msg)
        return self.sucesso


def test_despachar_registra_alerta_por_canal(session):
    w = _watch(session)
    c1, c2 = CanalFake("TELEGRAM"), CanalFake("EMAIL")

    enviados = despachar(session, [_candidato(w.id)], [c1, c2])
    session.commit()

    assert len(enviados) == 2
    assert {a.canal for a in enviados} == {"TELEGRAM", "EMAIL"}
    assert session.query(Alerta).count() == 2
    assert len(c1.mensagens) == 1


def test_despachar_envio_falho_nao_registra(session):
    w = _watch(session)
    falho = CanalFake("TELEGRAM", sucesso=False)

    enviados = despachar(session, [_candidato(w.id)], [falho])
    session.commit()

    # sem registro => cooldown não trava => re-tenta na próxima varredura
    assert enviados == []
    assert session.query(Alerta).count() == 0


def test_despachar_sem_canais_nao_quebra(session):
    w = _watch(session)
    assert despachar(session, [_candidato(w.id)], []) == []
