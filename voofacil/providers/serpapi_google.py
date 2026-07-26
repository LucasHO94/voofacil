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
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx

from voofacil.config import settings
from voofacil.engine.orcamento import ha_orcamento, registrar_chamada
from voofacil.models import TipoData, TipoWatch, Watch
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


def _data_alvo(tipo: str | None, ini: date, fim: date | None) -> date:
    """Uma única data representativa de uma janela [ini, fim] — usada pelo
    sweep() de ROTA para gastar só 1 chamada de cota por watch/varredura.

    - EXATA/APROXIMADA: o próprio alvo do watch já é `ini` (a data única
      que o usuário quer) — buscar outra data desperdiçaria a chamada com
      um resultado que `atende_criterios` provavelmente descartaria.
    - INTERVALO (ou `fim` ausente/inválido): usa o MEIO da janela, um proxy
      melhor da faixa de preço típica do que a borda inicial.
    """
    if tipo in (TipoData.EXATA.value, TipoData.APROXIMADA.value):
        return ini
    if fim is None or fim <= ini:
        return ini
    return ini + (fim - ini) // 2


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

    # ---------- Contrato Provider (varredura multi-fonte) ----------

    def sweep(self, watch: Watch, session=None) -> list[PriceQuote]:
        """Varredura regular (não só confirmação) para watches tipo ROTA.

        Contexto: até aqui a SerpApi só era chamada para (a) CONFIRMAR um
        candidato já aprovado pelo detector no cache da Travelpayouts e
        (b) o Explore diário de busca ampla. Este método a inclui também na
        varredura 3x/dia "estilo Skyscanner": para watches ROTA, busca o
        Google Flights ao vivo NA MESMA rodada da Travelpayouts, dando ao
        usuário duas fontes independentes lado a lado (TP_CACHE + SERPAPI).
        Watches REGIAO são ignorados aqui — a busca ampla deles já é
        coberta pelo Explore diário (executar_explore em worker.py).

        Cota: só 1 chamada por watch/varredura, não 1 por dia da janela —
        3 varreduras/dia x N dias de janela estouraria o free tier de
        250/mês em poucos watches. A data usada é escolhida por
        `_data_alvo` (ver docstring lá): a própria data-alvo do watch
        quando EXATA/APROXIMADA, ou o meio da janela quando INTERVALO.
        A data de volta segue a mesma lógica sobre a janela de desembarque
        (data_volta_ini/fim) quando ela existe; senão é derivada de
        duracao_min (ou 7 noites, default) a partir da data de ida escolhida.

        `session` é opcional: o contrato genérico `Provider.sweep(watch)`
        (usado por `executar_varredura` em worker.py) não passa sessão.
        Quando chamada assim, abre uma sessão própria de curta duração só
        para checar/gravar o orçamento (ContadorApi) e a fecha ao final;
        passar uma sessão explicitamente (como fazem os testes) reaproveita
        a mesma transação do chamador.
        """
        if watch.tipo != TipoWatch.ROTA.value or not watch.destino_iata:
            return []
        if not self.configurado:
            return []

        data_ida = _data_alvo(watch.tipo_data_ida, watch.data_ini, watch.data_fim)
        data_volta = None
        if watch.ida_volta:
            if watch.data_volta_ini is not None:
                data_volta = _data_alvo(
                    watch.tipo_data_volta, watch.data_volta_ini, watch.data_volta_fim
                )
            else:
                noites = watch.duracao_min or watch.duracao_max or 7
                data_volta = data_ida + timedelta(days=noites)

        sessao_propria = session is None
        if sessao_propria:
            from voofacil.db import SessionLocal  # import tardio — evita custo/efeito no import do módulo

            session = SessionLocal()
        try:
            if not ha_orcamento(session, self.nome, settings.serpapi_orcamento_mes):
                log.info("Orçamento SerpApi esgotado — sweep de rota pulado (watch %s)", watch.id)
                return []
            try:
                quote = self.confirmar_google_flights(
                    watch.origem_iata, watch.destino_iata, data_ida, data_volta
                )
                registrar_chamada(session, self.nome)
            except Exception:  # noqa: BLE001 — 1 falha no sweep multi-fonte não derruba a varredura
                log.exception("Falha no sweep SerpApi (varredura regular) do watch %s", watch.id)
                return []
            if sessao_propria:
                session.commit()
            return [quote] if quote else []
        finally:
            if sessao_propria:
                session.close()
