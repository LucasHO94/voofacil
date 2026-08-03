"""Cliente da Travelpayouts / Aviasales Data API (fonte primária, grátis).

IMPORTANTE: os preços vêm de CACHE (buscas de usuários do Aviasales, defasagem
de até 48h, retenção 2-7 dias). São indicativos — todo alerta deve ser
confirmado ao vivo (SerpApi) antes de disparar.

Endpoints usados (REST clássica, base api.travelpayouts.com):
- /v1/prices/calendar   -> mais barato por DIA de embarque (o cavalo de batalha)
- /v1/prices/cheap      -> mais baratos da rota no mês (fallback/conferência)
- /v1/city-directions   -> destinos populares a partir da origem (descoberta)
- /v2/prices/month-matrix -> matriz do mês (alternativa ao calendar)

A pesquisa apontou migração de versão da API (REST antiga possivelmente
desligada em jun/2026) — por isso o smoke test em scripts/smoke_tp.py testa
cada endpoint e este cliente centraliza as URLs em ENDPOINTS para troca fácil.

COMPORTAMENTO REAL DO /v1/prices/calendar (medido contra a API de produção
em 29/07/2026, token real, ver relatório da correção do defeito "coleta
zerada"). Três descobertas mudam o desenho do sweep():

1. `depart_date` é IGNORADO. Pedir 2026-08, 2026-10 ou 2027-05 devolve
   EXATAMENTE o mesmo conjunto (GRU->LIS: 41 itens, chaves idênticas, de
   2026-08-04 a 2027-07-01). Omitir o parâmetro devolve o mesmo conjunto.
   Consequência: o laço "um mês da janela = uma chamada" que existia aqui
   fazia N chamadas HTTP idênticas por destino (3 destinos x 2 meses = 6
   chamadas para 3 respostas distintas) e não ampliava nada. Agora é UMA
   chamada base por destino, e a janela é aplicada localmente.

2. A fonte REPORTA a data de volta. Nas 4 rotas medidas (GRU->LIS 41,
   GRU->CDG 18, RIO->LIS 14, RIO->FCO 6 itens), ZERO itens vieram sem
   `return_at`. Ou seja: a premissa de que "a Travelpayouts não informa a
   volta" é falsa — não era preciso afrouxar `atende_criterios` nem inventar
   um preço "só de ida". O que faltava era pedir direito.

3. Os parâmetros que a API RESPEITA para ida-e-volta são `length` (noites
   exatas de estadia) e `return_date` no formato MÊS ('2026-10'):
   - `length=14` -> só itinerários de 14 noites; `length=7` -> só de 7.
   - `return_date=2026-10` -> só itinerários com volta em out/2026.
   - `return_date=2026-10-20` (dia exato) -> 0 itens; `trip_duration` -> ignorado.
   Essas consultas dirigidas trazem itinerários que a consulta base NÃO
   traz (medido: +3 em GRU->LIS, +2 em GRU->CDG, +1 em RIO->FCO), por isso
   o sweep faz a base E as dirigidas quando o watch é ida-e-volta.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx

from voofacil.config import settings
from voofacil.engine.viabilidade import motivo_inviavel
from voofacil.models import TipoData, Watch
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

# Teto de chamadas DIRIGIDAS (length=/return_date=) por destino, além da
# chamada base. A Travelpayouts é grátis mas tem rate limit (429, tratado em
# _get); um watch REGIAO com 20 destinos e uma janela de volta de 6 meses
# geraria 120 chamadas por varredura sem este teto.
MAX_CONSULTAS_DIRIGIDAS = 6
# Quantas durações distintas amostrar quando a faixa duracao_min..duracao_max
# é larga (ex.: 7 a 30 noites = 24 valores; pedir todos seria desperdício).
MAX_DURACOES_ALVO = 3


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


def _amostrar(valores: list[int], teto: int) -> list[int]:
    """Até `teto` valores distribuídos ao longo de `valores` (extremos incluídos)."""
    if len(valores) <= teto:
        return valores
    if teto <= 1:
        return valores[:1]
    passo = (len(valores) - 1) / (teto - 1)
    indices = sorted({round(i * passo) for i in range(teto)})
    return [valores[i] for i in indices]


def _janela_ida_efetiva(watch: Watch) -> tuple[date, date]:
    """Janela de embarque usada como PRÉ-filtro do sweep.

    É deliberadamente um SUPERSET do que `atende_criterios` vai exigir depois
    (a autoridade continua sendo o filtro da engine): com
    tipo_data_ida=APROXIMADA o painel grava data_ini=data_fim=<data alvo> e a
    tolerância mora em flexibilidade_dias, então recortar por [data_ini,
    data_fim] cru descartaria aqui, dentro do provider, exatamente as datas
    vizinhas que o watch aceita.
    """
    ini, fim = watch.data_ini, watch.data_fim
    if fim is None:
        fim = ini
    if fim < ini:
        ini, fim = fim, ini
    if watch.tipo_data_ida == TipoData.APROXIMADA.value and watch.flexibilidade_dias:
        folga = timedelta(days=abs(watch.flexibilidade_dias))
        return ini - folga, fim + folga
    return ini, fim


def _janela_volta_efetiva(watch: Watch) -> tuple[date, date] | None:
    """Janela de desembarque do watch, ou None quando ele não define nenhuma."""
    ini, fim = watch.data_volta_ini, watch.data_volta_fim
    if ini is None and fim is None:
        return None
    ini = ini if ini is not None else fim
    fim = fim if fim is not None else ini
    if fim < ini:
        ini, fim = fim, ini
    if watch.tipo_data_volta == TipoData.APROXIMADA.value and watch.flexibilidade_dias:
        folga = timedelta(days=abs(watch.flexibilidade_dias))
        return ini - folga, fim + folga
    return ini, fim


def _meses_de_volta(watch: Watch) -> list[str]:
    """Meses ('2027-05') a pedir em `return_date` — só para watch ida-e-volta
    com janela de desembarque definida."""
    if not watch.ida_volta:
        return []
    janela = _janela_volta_efetiva(watch)
    if janela is None:
        return []
    return _meses_da_janela(*janela)


def _duracoes_alvo(watch: Watch) -> list[int]:
    """Durações (noites) a pedir em `length`, o único parâmetro de duração que
    a API respeita (`trip_duration` é ignorado — medido em 29/07/2026).

    Preferência: a faixa explícita do watch (duracao_min/duracao_max). Se ela
    não existir, usa a duração IMPLÍCITA pelas janelas de ida e de volta — é o
    caso do watch de datas exatas (ida 07/05, volta 22/05 => 15 noites), em que
    o usuário nunca preencheu duração mas a duração está determinada.
    """
    if not watch.ida_volta:
        return []
    dmin, dmax = watch.duracao_min, watch.duracao_max
    if dmin is not None or dmax is not None:
        piso = dmin if dmin is not None else dmax
        teto = dmax if dmax is not None else dmin
        faixa = [n for n in range(min(piso, teto), max(piso, teto) + 1) if n > 0]
        return _amostrar(faixa, MAX_DURACOES_ALVO)

    janela_volta = _janela_volta_efetiva(watch)
    if janela_volta is None:
        return []
    ida_ini, ida_fim = _janela_ida_efetiva(watch)
    volta_ini, volta_fim = janela_volta
    piso = (volta_ini - ida_fim).days
    teto = (volta_fim - ida_ini).days
    faixa = [n for n in range(max(piso, 1), max(teto, 0) + 1)]
    return _amostrar(faixa, MAX_DURACOES_ALVO)


def deep_link_aviasales(origem: str, destino: str, ida: date, volta: date | None) -> str:
    """Link de busca do Aviasales: GRU1009LIS2409 (IATA+DDMM), '1' = 1 adulto."""
    rota = f"{origem}{ida.day:02d}{ida.month:02d}{destino}"
    if volta:
        rota += f"{volta.day:02d}{volta.month:02d}"
    return f"https://www.aviasales.com/search/{rota}1"


class TravelpayoutsProvider:
    nome = "TP_CACHE"

    def __init__(self, token: str | None = None, client: httpx.Client | None = None):
        self.token = token if token is not None else settings.tp_token
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

    def calendar(
        self,
        origem: str,
        destino: str,
        mes: str | None = None,
        *,
        return_date: str | None = None,
        length: int | None = None,
    ) -> list[PriceQuote]:
        """Mais barato por dia de embarque, com filtros de ida-e-volta opcionais.

        - `mes` ('2026-09') vira `depart_date`. HOJE a API ignora esse
          parâmetro (ver docstring do módulo), então o sweep() o omite; ele
          continua aqui porque é o contrato documentado do endpoint e volta a
          valer sozinho se a API passar a respeitá-lo.
        - `return_date` deve ser um MÊS ('2026-10'): restringe a itinerários
          cuja VOLTA cai nesse mês (dia exato devolve vazio).
        - `length` é a estadia em NOITES: restringe a itinerários com
          exatamente essa duração.
        """
        params = {
            "origin": origem,
            "destination": destino,
            "calendar_type": "departure_date",
        }
        if mes:
            params["depart_date"] = mes
        if return_date:
            params["return_date"] = return_date
        if length is not None:
            params["length"] = length
        corpo = self._get(ENDPOINTS["calendar"], params)
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
        """Converte um item (formato v1 'price' ou v2 'value') em PriceQuote.

        origem/destino vêm de quem CHAMOU, não do item. A API normaliza os
        códigos para o nível CIDADE na resposta (medido: pedir GRU->LIS devolve
        origin='SAO'; pedir RIO->FCO devolve destination='ROM'), e gravar isso
        faria o histórico do watch "GRU -> LIS" aparecer como "SAO -> LIS" no
        painel e nos alertas. Nos endpoints de descoberta (cheap,
        city_directions) o chamador já passa o destino vindo da própria
        resposta, então a regra vale para todos.
        """
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
        if data_volta is not None and data_volta < data_ida:
            data_volta = None   # cache inconsistente: volta antes da ida não é itinerário
        return PriceQuote(
            origem=origem[:3].upper(),
            destino=destino[:3].upper(),
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

    def _consultas_do_watch(self, watch: Watch) -> list[dict]:
        """Os conjuntos de parâmetros de calendar() a disparar por destino.

        Sempre a consulta BASE (sem nenhum filtro: a API devolve todo o cache
        da rota, ~40 itens espalhados por 12 meses, todos com data de volta) e,
        para watch ida-e-volta, as DIRIGIDAS — `length=<noites>` e
        `return_date=<mês da janela de desembarque>` — que trazem itinerários
        ausentes da base (ver docstring do módulo).
        """
        consultas: list[dict] = [{}]
        for noites in _duracoes_alvo(watch):
            consultas.append({"length": noites})
        for mes in _meses_de_volta(watch):
            consultas.append({"return_date": mes})
        return consultas[: 1 + MAX_CONSULTAS_DIRIGIDAS]

    def sweep(self, watch: Watch) -> list[PriceQuote]:
        """Varre os destinos do watch no cache da Travelpayouts.

        Uma consulta base por destino + consultas dirigidas de ida-e-volta
        (ver _consultas_do_watch). O laço "um mês da janela = uma chamada" que
        existia aqui foi removido porque `depart_date` é ignorado pela API:
        eram N chamadas HTTP com resposta idêntica (medido em 29/07/2026).

        O recorte por janela de embarque continua sendo feito aqui, mas como
        PRÉ-filtro largo (ver _janela_ida_efetiva) — quem decide de verdade é
        `atende_criterios` na engine.

        Watch OPEN-JAW é pulado: o endpoint /v1/prices/calendar cota
        origem<->destino e não tem nenhum parâmetro para "a volta parte de
        outra cidade". Cotar o redondo mesmo assim geraria snapshots de um
        itinerário que o usuário não vai comprar, contaminando o baseline e
        podendo disparar alerta com preço irreal. Esses watches ficam a cargo
        exclusivo da SerpApi (type=3 + multi_city_json).
        """
        if watch.open_jaw:
            log.info(
                "Watch %s (%s): open-jaw (volta de %s) — Travelpayouts não cota "
                "multi-cidade, coleta fica só com a SerpApi",
                watch.id, watch.nome, watch.origem_volta_iata,
            )
            return []

        # Critérios contraditórios: o sweep não tinha nenhuma checagem de
        # viabilidade — montava as consultas, fazia as chamadas HTTP e deixava
        # `atende_criterios` (em worker.py) jogar tudo fora. O log por destino
        # ainda reportava "N cotações úteis" > 0, o que tornava o diagnóstico
        # AINDA mais confuso: o log do TP sugeria sucesso e o do worker imprimia
        # "0 snapshots". Agora a razão sai aqui e o rate limit fica preservado.
        motivo = motivo_inviavel(watch)
        if motivo:
            log.warning(
                "Watch %s (%s): critérios IMPOSSÍVEIS — %s Nenhuma chamada à "
                "Travelpayouts foi feita.", watch.id, watch.nome, motivo,
            )
            return []

        if not watch.destinos:
            # Era o único caso invisível até para quem lê o log: o diagnóstico
            # por destino vive DENTRO do laço, então com a lista vazia o
            # provider retornava [] em silêncio absoluto.
            log.warning(
                "Watch %s (%s): nenhum destino configurado — Travelpayouts não tem o "
                "que varrer.", watch.id, watch.nome,
            )
            return []

        if watch.ida_volta is False:
            # Medido em 4 rotas: ZERO itens vieram sem `return_at`. Ou seja,
            # `_bate_ida_volta` (que exige data_volta is None num watch só-ida)
            # descarta 100% do que sai daqui, DEPOIS do log de "cotações úteis".
            log.warning(
                "Watch %s (%s): somente ida — o /v1/prices/calendar SEMPRE devolve "
                "`return_at` (medido: nenhum item sem volta em 4 rotas), então toda "
                "cotação da Travelpayouts será descartada pela engine antes de virar "
                "snapshot. Este monitoramento depende exclusivamente da SerpApi.",
                watch.id, watch.nome,
            )

        quotes: list[PriceQuote] = []
        ida_ini, ida_fim = _janela_ida_efetiva(watch)
        consultas = self._consultas_do_watch(watch)

        for destino in watch.destinos:
            vistos: set[tuple] = set()
            brutos = sem_volta = fora_da_janela = 0
            for extra in consultas:
                try:
                    lote = self.calendar(watch.origem_iata, destino, **extra)
                except (httpx.HTTPError, TravelpayoutsError) as exc:
                    log.warning("Falha calendar %s->%s %s: %s",
                                watch.origem_iata, destino, extra or "(base)", exc)
                    continue
                for q in lote:
                    brutos += 1
                    if q.data_volta is None:
                        sem_volta += 1
                    if not (ida_ini <= q.data_ida <= ida_fim):
                        fora_da_janela += 1
                        continue
                    chave = (q.data_ida, q.data_volta, q.preco, q.cia)
                    if chave in vistos:
                        continue
                    vistos.add(chave)
                    quotes.append(q)
            # Diagnóstico por destino: sem isto, "0 snapshots" é indistinguível
            # entre "a rota não tem cache" e "o filtro comeu tudo" — foi
            # exatamente essa ambiguidade que deixou a coleta zerada sem alarme.
            log.info(
                "TP %s->%s: %s chamada(s), %s itens brutos, %s sem data de volta, "
                "%s fora da janela [%s..%s], %s cotações úteis",
                watch.origem_iata, destino, len(consultas), brutos, sem_volta,
                fora_da_janela, ida_ini, ida_fim, len(vistos),
            )
        return quotes
