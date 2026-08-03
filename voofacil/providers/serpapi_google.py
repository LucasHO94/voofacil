"""Cliente SerpApi — confirmação ao vivo (google_flights) + busca ampla (google_travel_explore).

Zona cinzenta de ToS (a SerpApi raspa o Google por conta própria e assume o
risco da coleta) — usado com parcimônia:
- google_flights: só para CONFIRMAR um candidato já aprovado pelo detector
  (a Travelpayouts serve o volume; aqui é 1 chamada pontual).
- google_travel_explore: 1x/dia por watch REGIAO, para descobrir destinos
  fora da lista curada (arrival_area_id = kgmid do Google, ex. Europa).

Free tier: 250 buscas/mês (50/hora). O orçamento é controlado via
ContadorApi (tabela `contador_api`) — ver orcamento.py.

CÓDIGO DE CIDADE x CÓDIGO DE AEROPORTO (causa raiz medida em 29/07/2026 do
"Google Flights hasn't returned any results" que zerou a produção): o painel
deixa o usuário escolher uma CIDADE no autocomplete ("RIO — Rio de Janeiro
(qualquer aeroporto)"), e o Google Flights NÃO entende códigos IATA
metropolitanos. Reproduzido com a chave real, mesmas datas do watch do
usuário (07/05/2027 -> 22/05/2027):

    departure_id=RIO      -> 0 voos + erro "hasn't returned any results"
    departure_id=GIG      -> 9 voos, mais barato R$ 5.426
    departure_id=GIG,SDU  -> 11 voos, mais barato R$ 5.426 (origens GIG e SDU)

A SerpApi aceita vários aeroportos separados por vírgula em
departure_id/arrival_id — é o que CIDADES_MULTI_AEROPORTO faz aqui, o que
preserva a intenção "qualquer aeroporto da cidade" em vez de escolher um.
A cotação devolvida mantém o código que o usuário cadastrou (RIO), não o
aeroporto expandido.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from voofacil.config import settings
from voofacil.engine.orcamento import ha_orcamento_serpapi, registrar_chamada_serpapi
# A janela aceita pela engine, o recorte por mês e o veredito de viabilidade
# moram em engine/viabilidade.py justamente para que provider, validação de
# entrada e diagnóstico do painel não possam divergir — quando cada camada
# tinha a sua cópia, a auditoria achou casos em que o provider fazia (e pagava)
# uma chamada que a engine depois descartava inteira.
from voofacil.engine.viabilidade import (
    janela_aceita as _janela_aceita,
    motivo_inviavel,
    recortes_do_mes,
)
from voofacil.models import TipoData, TipoWatch, Watch
from voofacil.schemas import PriceQuote

log = logging.getLogger(__name__)

BASE_URL = "https://serpapi.com/search"

# Noites usadas quando o watch é ida-e-volta mas não diz nada sobre a volta
# (sem janela de desembarque e sem duração) — mantém o comportamento histórico.
NOITES_PADRAO = 7

# Códigos IATA METROPOLITANOS (cidade com mais de um aeroporto) -> aeroportos
# reais, separados por vírgula como a SerpApi espera. Só entram aqui códigos
# que NÃO são também um aeroporto; um código desconhecido passa intacto.
CIDADES_MULTI_AEROPORTO = {
    "RIO": "GIG,SDU",           # Rio de Janeiro
    "SAO": "GRU,CGH,VCP",       # São Paulo
    "BHZ": "CNF,PLU",           # Belo Horizonte
    "BUE": "EZE,AEP",           # Buenos Aires
    "NYC": "JFK,EWR,LGA",       # Nova York
    "WAS": "IAD,DCA,BWI",       # Washington
    "CHI": "ORD,MDW",           # Chicago
    "YTO": "YYZ,YTZ",           # Toronto
    "LON": "LHR,LGW,STN,LTN,LCY",   # Londres
    "PAR": "CDG,ORY,BVA",       # Paris
    "MIL": "MXP,LIN,BGY",       # Milão
    "ROM": "FCO,CIA",           # Roma
    "STO": "ARN,BMA,NYO",       # Estocolmo
    "MOW": "SVO,DME,VKO",       # Moscou
    "TYO": "HND,NRT",           # Tóquio
    "OSA": "KIX,ITM",           # Osaka
    "SEL": "ICN,GMP",           # Seul
    "BJS": "PEK,PKX",           # Pequim
}


class SerpApiError(RuntimeError):
    pass


def _escalas_do_voo(voo: dict, *, multi_city: bool) -> int | None:
    """Número de conexões do voo, ou None quando não dá para afirmar.

    O caminho preferido é `layovers`, que a SerpApi já entrega contado. O
    fallback "número de pernas - 1" só vale para itinerário de um trecho: num
    `multi_city_json` a lista `flights` concatena os DOIS trechos, e um
    open-jaw direto nos dois (RIO->NAP + VCE->RIO) sairia como "1 escala" —
    a emenda entre os trechos contada como conexão. Devolver None nesse caso
    faz o alerta dizer "escalas não informadas" em vez de um número falso
    (ver voofacil/alerts/mensagem.py).
    """
    layovers = voo.get("layovers")
    if isinstance(layovers, list):
        return len(layovers)
    if multi_city:
        return None
    pernas = voo.get("flights") or []
    return max(len(pernas) - 1, 0)


def _ids_busca(codigo: str) -> str:
    """'RIO' -> 'GIG,SDU'. Código desconhecido (ou já de aeroporto) passa intacto."""
    return CIDADES_MULTI_AEROPORTO.get((codigo or "").upper(), codigo)


def _parse_price_insights_date(valor: str | None) -> date | None:
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        return None


def _preco_numerico(valor) -> Decimal | None:
    """Decimal positivo, ou None se o campo vier nulo/não numérico.

    Existe porque `min(voos, key=lambda v: v.get("price", inf))` explodia com
    TypeError quando algum voo trazia "price": null — a resposta inteira era
    perdida por causa de um item.
    """
    if valor is None or isinstance(valor, bool):
        return None
    try:
        preco = Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return None
    return preco if preco > 0 else None


def _primeiro_dia_do_mes(mes: int, hoje: date | None = None) -> date:
    """1º dia da PRÓXIMA ocorrência do mês `mes` (1-12).

    Usar sempre o ano corrente colocava a data no PASSADO na virada do ano
    (em dez/2026, mes=1 virava 2026-01-01), e `atende_criterios` descartava
    a descoberta inteira depois de a chamada já ter sido cobrada.
    """
    hoje = hoje or date.today()
    ano = hoje.year if mes >= hoje.month else hoje.year + 1
    return date(ano, mes, 1)


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


def _entre(valor: date, lo: date, hi: date) -> date:
    return min(max(valor, lo), hi)


def _datas_da_busca(watch: Watch) -> tuple[date, date | None] | None:
    """(data_ida, data_volta) da chamada de cota, JÁ reconciliadas com TODOS os
    critérios do watch. None = os critérios são incompatíveis entre si e
    nenhuma combinação passaria por `atende_criterios` — não chame a API.

    Antes, a ida saía de `_data_alvo` (meio da janela) e a volta de
    "duracao_min or duracao_max or 7", sem olhar um para o outro nem para a
    janela de desembarque. Resultado medido pela auditoria: janela de ida
    01-30/09, janela de volta 20-28/09, duração 14-21 noites produziam
    outbound=15/09 + return=24/09 (9 noites) — `atende_criterios` reprovava
    SEMPRE, e 1 das 8 chamadas diárias ia para o lixo em toda varredura.

    O cálculo agora é geométrico e feito de uma vez:
      ida  ∈ janela_de_ida ∩ [volta_lo - duracao_max, volta_hi - duracao_min]
      volta ∈ janela_de_volta ∩ [ida + duracao_min, ida + duracao_max]
    A ida preferida (`_data_alvo`) é encaixada nesse intervalo viável em vez de
    ser imposta: com janela de ida do tipo INTERVALO o usuário É flexível na
    ida, então deslizar 15/09 -> 14/09 para caber na volta 28/09 (14 noites)
    coleta dado em vez de desistir. Com ida EXATA/APROXIMADA o intervalo viável
    já é estreito e o encaixe não pode sair dele — se ficar vazio, devolve None.
    """
    janela_ida = _janela_aceita(
        watch.tipo_data_ida, watch.data_ini, watch.data_fim, watch.flexibilidade_dias
    )
    if janela_ida is None:
        return None
    # `mes_especifico` não era reconciliado em lugar nenhum: `_data_alvo`
    # devolvia o MEIO da janela e, com a janela cruzando dois meses, a única
    # chamada da varredura cotava sempre o mês errado — a chamada era feita,
    # COBRADA, e `_bate_mes_especifico` reprovava 100% do resultado, 3x/dia,
    # para sempre. Recortar a janela no mês pedido não é "chutar" uma data: a
    # data escolhida continua sendo uma que o watch aceita.
    if watch.mes_especifico:
        recortes = recortes_do_mes(janela_ida, watch.mes_especifico)
        if not recortes:
            return None       # o mês pedido não tem nenhum dia dentro da janela
        janela_ida = recortes[0]
    ida_pref = _data_alvo(watch.tipo_data_ida, watch.data_ini, watch.data_fim)

    if not watch.ida_volta:
        return _entre(ida_pref, *janela_ida), None

    dmin = watch.duracao_min if watch.duracao_min is not None else 0
    dmax = watch.duracao_max
    if dmax is not None and dmax < dmin:
        return None

    janela_volta = _janela_aceita(
        watch.tipo_data_volta, watch.data_volta_ini, watch.data_volta_fim,
        watch.flexibilidade_dias,
    )
    if janela_volta is None:
        # Sem janela de desembarque: o retorno é derivado da duração, como antes.
        ida = _entre(ida_pref, *janela_ida)
        noites = watch.duracao_min or watch.duracao_max or NOITES_PADRAO
        if dmax is not None:
            noites = min(noites, dmax)
        return ida, ida + timedelta(days=max(noites, 0))

    volta_lo, volta_hi = janela_volta
    ida_lo, ida_hi = janela_ida
    if dmax is not None:
        ida_lo = max(ida_lo, volta_lo - timedelta(days=dmax))
    ida_hi = min(ida_hi, volta_hi - timedelta(days=dmin))
    if ida_lo > ida_hi:
        return None                      # nenhuma ida admite uma volta válida

    ida = _entre(ida_pref, ida_lo, ida_hi)
    volta_lo = max(volta_lo, ida + timedelta(days=dmin))
    if dmax is not None:
        volta_hi = min(volta_hi, ida + timedelta(days=dmax))
    if volta_lo > volta_hi:
        return None
    volta_pref = _data_alvo(
        watch.tipo_data_volta,
        watch.data_volta_ini if watch.data_volta_ini is not None else volta_lo,
        watch.data_volta_fim,
    )
    return ida, _entre(volta_pref, volta_lo, volta_hi)


class SerpApiProvider:
    nome = "SERPAPI"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key if api_key is not None else settings.serpapi_key
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))

    @property
    def configurado(self) -> bool:
        return bool(self.api_key)

    def _get(self, params: dict, ao_cobrar: Callable[[], None] | None = None) -> dict:
        """GET na SerpApi. `ao_cobrar` é chamado assim que o HTTP 200 volta.

        A contabilização precisa acontecer AQUI, e não depois do parsing: a
        SerpApi cobra a busca no momento em que ela é concluída com sucesso —
        inclusive quando o corpo traz `error: "Google Flights hasn't returned
        any results"` (o `search_metadata.status` desse corpo é "Success",
        verificado ao vivo). Contabilizar só depois de montar o PriceQuote
        fazia toda falha pós-HTTP (erro no corpo, JSON inválido, ValidationError)
        gastar cota real e deixar o contador em zero — o orçamento passava a
        mentir justamente no dia em que a API estivesse com problema.
        """
        if not self.api_key:
            raise SerpApiError("SERPAPI_KEY ausente — configure no .env")
        # URL absoluta: ignora qualquer base_url do client (evita ambiguidade de barra final)
        resp = self._client.get(BASE_URL, params={**params, "api_key": self.api_key})
        resp.raise_for_status()
        if ao_cobrar is not None:
            ao_cobrar()
        corpo = resp.json()
        if corpo.get("error"):
            raise SerpApiError(str(corpo["error"]))
        return corpo

    def confirmar_google_flights(
        self,
        origem: str,
        destino: str,
        data_ida: date,
        data_volta: date | None = None,
        ao_cobrar: Callable[[], None] | None = None,
        *,
        origem_volta: str | None = None,
    ) -> PriceQuote | None:
        """Confirma o preço AO VIVO de um itinerário específico (engine=google_flights).

        Usado só quando um candidato já cruzou um gatilho no cache da
        Travelpayouts — 1 chamada, não uma varredura.

        `origem_volta` liga o modo MULTI-CIDADE (open-jaw): o retorno parte de
        outra cidade que não o destino da ida — ex.: voa RIO->NAP, atravessa a
        Itália por terra, volta VCE->RIO. A SerpApi expressa isso com
        `type=3` + `multi_city_json` (um objeto por trecho), e nesse modo
        ignora departure_id/arrival_id/outbound_date/return_date, que por isso
        não são enviados. Ignorar `origem_volta` e cotar o redondo NAP->RIO
        devolveria o preço de uma viagem que o usuário não vai comprar.

        `ao_cobrar` (opcional) é disparado assim que a chamada HTTP volta 200,
        antes de qualquer parsing — ver `_get`.
        """
        multi_city = bool(origem_volta) and data_volta is not None
        params = {
            "engine": "google_flights",
            "currency": settings.moeda_padrao,
            "gl": "br",
            "hl": "pt-br",
        }
        if multi_city:
            params["type"] = "3"
            params["multi_city_json"] = json.dumps([
                {
                    "departure_id": _ids_busca(origem),
                    "arrival_id": _ids_busca(destino),
                    "date": data_ida.isoformat(),
                },
                {
                    "departure_id": _ids_busca(origem_volta),
                    "arrival_id": _ids_busca(origem),
                    "date": data_volta.isoformat(),
                },
            ])
        else:
            params["departure_id"] = _ids_busca(origem)
            params["arrival_id"] = _ids_busca(destino)
            params["outbound_date"] = data_ida.isoformat()
            params["type"] = "1" if data_volta else "2"   # 1=ida-e-volta, 2=só ida
            if data_volta:
                params["return_date"] = data_volta.isoformat()

        corpo = self._get(params, ao_cobrar)
        brutos = corpo.get("best_flights") or corpo.get("other_flights") or []
        # Filtra ANTES do min(): um voo com "price": null fazia o min() comparar
        # None com float e derrubar a resposta inteira (TypeError).
        voos = [
            (v, _preco_numerico(v.get("price")))
            for v in brutos if isinstance(v, dict)
        ]
        voos = [(v, p) for v, p in voos if p is not None]
        if not voos:
            return None
        mais_barato, preco = min(voos, key=lambda par: par[1])

        pernas = mais_barato.get("flights") or []
        cia = pernas[0].get("airline") if pernas else None
        deep_link = corpo.get("search_metadata", {}).get("google_flights_url")

        return PriceQuote(
            origem=origem,
            destino=destino,
            data_ida=data_ida,
            data_volta=data_volta,
            origem_volta=origem_volta.upper() if origem_volta else None,
            preco=Decimal(str(preco)),
            moeda=settings.moeda_padrao,
            cia=cia,
            escalas=_escalas_do_voo(mais_barato, multi_city=multi_city),
            fonte=self.nome,
            deep_link=deep_link,
        )

    def explorar_regiao(
        self, origem: str, arrival_area_id: str, mes: int,
        ao_cobrar: Callable[[], None] | None = None,
    ) -> list[PriceQuote]:
        """Busca ampla: origem -> região/país inteiro num mês (engine=google_travel_explore).

        `arrival_area_id` é o kgmid do Google (ex.: /m/02j9z para Europa).
        Cada destino é montado isoladamente: um item malformado (IATA com
        menos de 3 letras, preço fora do contrato) levantava ValidationError e
        derrubava a resposta INTEIRA, jogando fora dezenas de destinos válidos
        de uma chamada que já tinha sido cobrada.
        """
        params = {
            "engine": "google_travel_explore",
            "departure_id": _ids_busca(origem),
            "arrival_area_id": arrival_area_id,
            "month": mes,
            "currency": settings.moeda_padrao,
            "gl": "br",
            "hl": "pt-br",
        }
        corpo = self._get(params, ao_cobrar)
        quotes = []
        descartados = 0
        for destino in corpo.get("destinations", []) or []:
            if not isinstance(destino, dict):
                descartados += 1
                continue
            preco = _preco_numerico(destino.get("price"))
            iata = destino.get("airport_code") or destino.get("id")
            if preco is None or not iata:
                descartados += 1
                continue
            try:
                quotes.append(
                    PriceQuote(
                        origem=origem,
                        destino=str(iata)[:3].upper(),
                        data_ida=_parse_price_insights_date(destino.get("date"))
                        or _primeiro_dia_do_mes(mes),
                        preco=preco,
                        moeda=settings.moeda_padrao,
                        fonte=self.nome,
                        deep_link=destino.get("link"),
                    )
                )
            except Exception as exc:  # noqa: BLE001 — 1 destino ruim não invalida os outros
                descartados += 1
                log.warning("Destino ignorado no Explore (%s): %s", iata, exc)
        if descartados:
            log.info("Explore %s: %s destinos aproveitados, %s descartados",
                     origem, len(quotes), descartados)
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
        N varreduras/dia x N dias de janela estouraria o free tier de
        250/mês em poucos watches; o teto diário adicional
        (ha_orcamento_serpapi, ver voofacil/engine/orcamento.py) protege
        contra isso mesmo quando o mês ainda tem folga. O par (ida, volta) é
        escolhido por `_datas_da_busca` (ver docstring lá), que reconcilia a
        janela de embarque, a janela de desembarque e a faixa de duração —
        e devolve None quando esses critérios são incompatíveis entre si, caso
        em que a chamada NÃO é feita (a cota fica preservada em vez de comprar
        um resultado que `atende_criterios` reprovaria).

        `session` é opcional: o contrato genérico `Provider.sweep(watch)`
        (usado por `executar_varredura` em worker.py) não passa sessão.
        Quando chamada assim, abre uma sessão própria de curta duração só
        para checar/gravar o orçamento (ContadorApi + ContadorDiarioApi) e a fecha ao final;
        passar uma sessão explicitamente (como fazem os testes) reaproveita
        a mesma transação do chamador.
        """
        if watch.tipo != TipoWatch.ROTA.value or not watch.destino_iata:
            return []
        if not self.configurado:
            return []

        # Antes de qualquer coisa, o veredito de viabilidade — que sabe MAIS que
        # `_datas_da_busca` (mês específico, piso acima do teto, janela de volta
        # órfã em watch só-ida). Esses casos deixavam a chamada acontecer e ser
        # COBRADA para `atende_criterios` reprovar 100% do resultado depois.
        motivo = motivo_inviavel(watch)
        if motivo:
            log.warning(
                "Watch %s (%s): critérios IMPOSSÍVEIS — %s A chamada SerpApi NÃO foi feita "
                "(1 das %s chamadas diárias preservada).",
                watch.id, watch.nome, motivo, settings.serpapi_orcamento_dia,
            )
            return []

        datas = _datas_da_busca(watch)
        if datas is None:
            log.warning(
                "Watch %s (%s): critérios de data INCOMPATÍVEIS — embarque %s [%s..%s], "
                "desembarque %s [%s..%s], duração %s-%s noites. Nenhum par ida/volta "
                "satisfaz o watch, então a chamada SerpApi NÃO foi feita (1 das %s "
                "chamadas diárias preservada). Ajuste as datas ou a duração no painel.",
                watch.id, watch.nome, watch.tipo_data_ida, watch.data_ini, watch.data_fim,
                watch.tipo_data_volta, watch.data_volta_ini, watch.data_volta_fim,
                watch.duracao_min, watch.duracao_max, settings.serpapi_orcamento_dia,
            )
            return []
        data_ida, data_volta = datas

        sessao_propria = session is None
        if sessao_propria:
            from voofacil.db import SessionLocal  # import tardio — evita custo/efeito no import do módulo

            session = SessionLocal()
        try:
            if not ha_orcamento_serpapi(session):
                log.info("Orçamento SerpApi esgotado (mensal ou diário) — sweep de rota pulado (watch %s)", watch.id)
                return []
            try:
                # A contabilização vai por callback (disparado no HTTP 200, antes
                # do parsing): se o corpo vier com erro ou o parsing falhar, a
                # chamada já foi cobrada pela SerpApi e precisa constar no contador.
                quote = self.confirmar_google_flights(
                    watch.origem_iata, watch.destino_iata, data_ida, data_volta,
                    ao_cobrar=lambda: registrar_chamada_serpapi(session),
                    origem_volta=watch.origem_volta_iata if watch.open_jaw else None,
                )
            except Exception:  # noqa: BLE001 — 1 falha no sweep multi-fonte não derruba a varredura
                log.exception("Falha no sweep SerpApi (varredura regular) do watch %s", watch.id)
                return []
            return [quote] if quote else []
        finally:
            if sessao_propria:
                # Commit no finally (não no caminho feliz): a contabilização de uma
                # chamada que falhou DEPOIS do HTTP 200 também precisa ser gravada.
                try:
                    session.commit()
                except Exception:  # noqa: BLE001
                    session.rollback()
                    log.exception("Falha ao gravar o consumo SerpApi do watch %s", watch.id)
                finally:
                    session.close()
