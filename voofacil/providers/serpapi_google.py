"""Cliente SerpApi — confirmação ao vivo (google_flights) + busca ampla (google_travel_explore).

Zona cinzenta de ToS (a SerpApi raspa o Google por conta própria e assume o
risco da coleta) — usado com parcimônia:
- google_flights: só para CONFIRMAR um candidato já aprovado pelo detector
  (a Travelpayouts serve o volume; aqui é 1 chamada pontual).
- google_travel_explore: 1x/dia por watch REGIAO, para descobrir destinos
  fora da lista curada (arrival_area_id = kgmid do Google, ex. Europa).

Free tier: 250 buscas/mês (50/hora). O orçamento é controlado via
ContadorApi (tabela `contador_api`) — ver orcamento.py.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

import httpx

from voofacil.config import settings
from voofacil.schemas import PriceQuote

log = logging.getLogger(__name__)

BASE_URL = "https://serpapi.com/search"


class SerpApiError(RuntimeError):
    pass


def _parse_price_insights_date(valor: str | None) -> date | None:
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        return None


class SerpApiProvider:
    nome = "SERPAPI"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key or settings.serpapi_key
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))

    @property
    def configurado(self) -> bool:
        return bool(self.api_key)

    def _get(self, params: dict) -> dict:
        if not self.api_key:
            raise SerpApiError("SERPAPI_KEY ausente — configure no .env")
        # URL absoluta: ignora qualquer base_url do client (evita ambiguidade de barra final)
        resp = self._client.get(BASE_URL, params={**params, "api_key": self.api_key})
        resp.raise_for_status()
        corpo = resp.json()
        if corpo.get("error"):
            raise SerpApiError(str(corpo["error"]))
        return corpo

    def confirmar_google_flights(
        self, origem: str, destino: str, data_ida: date, data_volta: date | None = None
    ) -> PriceQuote | None:
        """Confirma o preço AO VIVO de um itinerário específico (engine=google_flights).

        Usado só quando um candidato já cruzou um gatilho no cache da
        Travelpayouts — 1 chamada, não uma varredura.
        """
        params = {
            "engine": "google_flights",
            "departure_id": origem,
            "arrival_id": destino,
            "outbound_date": data_ida.isoformat(),
            "type": "1" if data_volta else "2",   # 1=ida-e-volta, 2=só ida
            "currency": settings.moeda_padrao,
            "gl": "br",
            "hl": "pt-br",
        }
        if data_volta:
            params["return_date"] = data_volta.isoformat()

        corpo = self._get(params)
        voos = corpo.get("best_flights") or corpo.get("other_flights") or []
        if not voos:
            return None
        mais_barato = min(voos, key=lambda v: v.get("price", float("inf")))
        preco = mais_barato.get("price")
        if not preco:
            return None

        pernas = mais_barato.get("flights") or []
        cia = pernas[0].get("airline") if pernas else None
        escalas = max(len(pernas) - 1, 0)
        deep_link = corpo.get("search_metadata", {}).get("google_flights_url")

        return PriceQuote(
            origem=origem,
            destino=destino,
            data_ida=data_ida,
            data_volta=data_volta,
            preco=Decimal(str(preco)),
            moeda=settings.moeda_padrao,
            cia=cia,
            escalas=escalas,
            fonte=self.nome,
            deep_link=deep_link,
        )

    def explorar_regiao(
        self, origem: str, arrival_area_id: str, mes: int
    ) -> list[PriceQuote]:
        """Busca ampla: origem -> região/país inteiro num mês (engine=google_travel_explore).

        `arrival_area_id` é o kgmid do Google (ex.: /m/02j9z para Europa).
        """
        params = {
            "engine": "google_travel_explore",
            "departure_id": origem,
            "arrival_area_id": arrival_area_id,
            "month": mes,
            "currency": settings.moeda_padrao,
            "gl": "br",
            "hl": "pt-br",
        }
        corpo = self._get(params)
        quotes = []
        for destino in corpo.get("destinations", []):
            preco = destino.get("price")
            iata = destino.get("airport_code") or destino.get("id")
            if not preco or not iata:
                continue
            quotes.append(
                PriceQuote(
                    origem=origem,
                    destino=str(iata)[:3].upper(),
                    data_ida=_parse_price_insights_date(destino.get("date"))
                    or date(date.today().year, mes, 1),
                    preco=Decimal(str(preco)),
                    moeda=settings.moeda_padrao,
                    fonte=self.nome,
                    deep_link=destino.get("link"),
                )
            )
        return quotes
