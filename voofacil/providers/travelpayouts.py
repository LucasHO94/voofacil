"""Cliente da Travelpayouts / Aviasales Data API (fonte primária, grátis).

IMPORTANTE: os preços vêm de CACHE (buscas de usuários do Aviasales, defasagem
de até 48h, retenção 2-7 dias). São indicativos — todo alerta deve ser
confirmado ao vivo (SerpApi) antes de disparar.

Endpoints usados (REST clássica, base api.travelpayouts.com):
- /v1/prices/calendar   -> mais barato por DIA do mês (o cavalo de batalha)
- /v1/prices/cheap      -> mais baratos da rota no mês (fallback/conferência)
- /v1/city-directions   -> destinos populares a partir da origem (descoberta)
- /v2/prices/month-matrix -> matriz do mês (alternativa ao calendar)

A pesquisa apontou migração de versão da API (REST antiga possivelmente
desligada em jun/2026) — por isso o smoke test em scripts/smoke_tp.py testa
cada endpoint e este cliente centraliza as URLs em ENDPOINTS para troca fácil.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from decimal import Decimal

import httpx

from voofacil.config import settings
from voofacil.models import Watch
from voofacil.schemas import PriceQuote

log = logging.getLogger(__name__)

BASE_URL = "https://api.travelpayouts.com"

ENDPOINTS = {
    "calendar": "/v1/prices/calendar",
    "cheap": "/v1/prices/cheap",
    "city_directions": "/v1/city-directions",
    "month_matrix": "/v2/prices/month-matrix",
    "latest": "/v2/prices/latest",
}

MAX_RETRIES_429 = 2


class TravelpayoutsError(RuntimeError):
    pass


def _parse_dt(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(valor: str | None) -> date | None:
    dt = _parse_dt(valor)
    return dt.date() if dt else None


def _meses_da_janela(ini: date, fim: date) -> list[str]:
    """['2026-09', '2026-10', ...] cobrindo a janela de embarque."""
    meses = []
    ano, mes = ini.year, ini.month
    while (ano, mes) <= (fim.year, fim.month):
        meses.append(f"{ano:04d}-{mes:02d}")
        mes += 1
        if mes > 12:
            ano, mes = ano + 1, 1
    return meses


def deep_link_aviasales(origem: str, destino: str, ida: date, volta: date | None) -> str:
    """Link de busca do Aviasales: GRU1009LIS2409 (IATA+DDMM), '1' = 1 adulto."""
    rota = f"{origem}{ida.day:02d}{ida.month:02d}{destino}"
    if volta:
        rota += f"{volta.day:02d}{volta.month:02d}"
    return f"https://www.aviasales.com/search/{rota}1"


class TravelpayoutsProvider:
    nome = "TP_CACHE"

    def __init__(self, token: str | None = None, client: httpx.Client | None = None):
        self.token = token or settings.tp_token
        self._client = client or httpx.Client(
            base_url=BASE_URL,
            timeout=httpx.Timeout(20.0),
            headers={"Accept-Encoding": "gzip"},
        )

    # ---------- HTTP ----------

    def _get(self, endpoint: str, params: dict) -> dict:
        if not self.token:
            raise TravelpayoutsError("TP_TOKEN ausente — configure no .env")
        params = {**params, "currency": settings.moeda_padrao.lower()}
        headers = {"X-Access-Token": self.token}
        for tentativa in range(MAX_RETRIES_429 + 1):
            resp = self._client.get(endpoint, params=params, headers=headers)
            if resp.status_code == 429:
                espera = int(resp.headers.get("Retry-After", "5"))
                log.warning("429 na Travelpayouts; aguardando %ss", espera)
                time.sleep(min(espera, 30))
                continue
            resp.raise_for_status()
            corpo = resp.json()
            if isinstance(corpo, dict) and corpo.get("success") is False:
                raise TravelpayoutsError(f"Erro da API: {corpo.get('error', corpo)}")
            return corpo
        raise TravelpayoutsError(f"Rate limit persistente em {endpoint}")

    # ---------- Endpoints ----------

    def calendar(self, origem: str, destino: str, mes: str) -> list[PriceQuote]:
        """Mais barato por dia de embarque no mês ('2026-09')."""
        corpo = self._get(
            ENDPOINTS["calendar"],
            {
                "origin": origem,
                "destination": destino,
                "depart_date": mes,
                "calendar_type": "departure_date",
            },
        )
        quotes = []
        for dia, item in (corpo.get("data") or {}).items():
            quote = self._item_para_quote(item, origem, destino, dia_fallback=dia)
            if quote:
                quotes.append(quote)
        return quotes

    def cheap(self, origem: str, destino: str, mes: str) -> list[PriceQuote]:
        """Bilhetes mais baratos da rota no mês (até ~1-2 conexões)."""
        corpo = self._get(
            ENDPOINTS["cheap"],
            {"origin": origem, "destination": destino, "depart_date": mes},
        )
        quotes = []
        for dest, itens in (corpo.get("data") or {}).items():
            for item in (itens or {}).values():
                quote = self._item_para_quote(item, origem, dest)
                if quote:
                    quotes.append(quote)
        return quotes

    def city_directions(self, origem: str) -> list[PriceQuote]:
        """Destinos populares (com preço em cache) a partir da origem — descoberta."""
        corpo = self._get(ENDPOINTS["city_directions"], {"origin": origem})
        quotes = []
        for dest, item in (corpo.get("data") or {}).items():
            quote = self._item_para_quote(item, origem, dest)
            if quote:
                quotes.append(quote)
        return quotes

    def month_matrix(self, origem: str, destino: str, mes: str) -> list[PriceQuote]:
        """Matriz do mês (v2) — resposta em lista com campo 'value'."""
        corpo = self._get(
            ENDPOINTS["month_matrix"],
            {"origin": origem, "destination": destino, "month": f"{mes}-01"},
        )
        quotes = []
        for item in corpo.get("data") or []:
            quote = self._item_para_quote(item, origem, destino)
            if quote:
                quotes.append(quote)
        return quotes

    # ---------- Normalização ----------

    def _item_para_quote(
        self, item: dict, origem: str, destino: str, dia_fallback: str | None = None
    ) -> PriceQuote | None:
        """Converte um item (formato v1 'price' ou v2 'value') em PriceQuote."""
        if not isinstance(item, dict):
            return None
        preco_bruto = item.get("price", item.get("value"))
        if preco_bruto in (None, 0):
            return None
        data_ida = (
            _parse_date(item.get("departure_at"))
            or _parse_date(item.get("depart_date"))
            or (_parse_date(dia_fallback) if dia_fallback else None)
        )
        if data_ida is None:
            return None
        data_volta = _parse_date(item.get("return_at")) or _parse_date(item.get("return_date"))
        return PriceQuote(
            origem=item.get("origin", origem)[:3].upper(),
            destino=item.get("destination", destino)[:3].upper(),
            data_ida=data_ida,
            data_volta=data_volta,
            preco=Decimal(str(preco_bruto)),
            moeda=settings.moeda_padrao,
            cia=item.get("airline"),
            escalas=item.get("transfers", item.get("number_of_changes")),
            fonte=self.nome,
            deep_link=deep_link_aviasales(origem, destino, data_ida, data_volta),
            expires_at=_parse_dt(item.get("expires_at")),
        )

    # ---------- Contrato Provider ----------

    def sweep(self, watch: Watch) -> list[PriceQuote]:
        """Varre todos os destinos x meses da janela do watch via calendar.

        Filtra para a janela [data_ini, data_fim] — o calendar devolve o mês
        inteiro, e as bordas podem cair fora da janela pedida.
        """
        quotes: list[PriceQuote] = []
        meses = _meses_da_janela(watch.data_ini, watch.data_fim)
        for destino in watch.destinos:
            for mes in meses:
                try:
                    lote = self.calendar(watch.origem_iata, destino, mes)
                except (httpx.HTTPError, TravelpayoutsError) as exc:
                    log.warning("Falha calendar %s->%s %s: %s",
                                watch.origem_iata, destino, mes, exc)
                    continue
                quotes.extend(
                    q for q in lote
                    if watch.data_ini <= q.data_ida <= watch.data_fim
                )
        return quotes
