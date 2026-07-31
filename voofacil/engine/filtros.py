"""Filtro de critérios de busca avançados — aplica os critérios de um Watch
sobre uma cotação (PriceQuote) recém-coletada, ANTES de persistir.

Critérios cobertos por atende_criterios (todos precisam bater — AND):
- data de embarque: exata | aproximada (± flexibilidade_dias) | intervalo
  (data_ini/data_fim, que já existem e continuam representando a janela
  de embarque)
- data de desembarque: mesma semântica, mas usando a janela independente
  data_volta_ini/data_volta_fim. Se ambas forem None (e ida_volta=True),
  não filtra por data de volta — mantém o comportamento atual (calendário
  decide o retorno; duração ainda se aplica separadamente)
- mês específico (mes_especifico) — alternativa às datas, restringe pelo
  mês de embarque
- duração da estadia (duracao_min/duracao_max), quando há volta
- ida_volta vs "somente ida" (embarque sem desembarque): watch com
  ida_volta=True exige quote com data_volta; watch com ida_volta=False
  exige quote SEM data_volta
- origem da volta (open-jaw): watch com origem_volta_iata exige que o trecho
  de retorno parta daquela cidade; watch comum exige retorno do próprio
  destino
- preco_minimo — piso de preço (preco_limite é usado em outro lugar, como
  teto para disparar alerta — não é filtro de critério de busca aqui)

Retorna False -> a cotação é descartada antes de virar PriceSnapshot.
"""
from __future__ import annotations

from datetime import date

from voofacil.models import TipoData, Watch
from voofacil.schemas import PriceQuote


def _bate_janela_data(
    data_quote: date | None,
    tipo: str | None,
    ini: date | None,
    fim: date | None,
    flexibilidade_dias: int | None,
) -> bool:
    """Confere uma data da cotação contra uma janela exata|aproximada|intervalo.

    ini=None E fim=None significa "sem janela definida" -> não filtra (True).
    Se só `fim` foi preenchido, a janela é aberta à esquerda e `fim` vale como
    teto (antes esse caso caía no `return True` e não filtrava nada — um watch
    com "voltar até 28/09" aceitava uma volta em dezembro).
    tipo=None (Watch construído sem passar pelo default do ORM) cai no
    comportamento de INTERVALO, que é o default do contrato.
    """
    if ini is None and fim is None:
        return True
    if data_quote is None:
        return False
    if ini is None:
        return data_quote <= fim

    if tipo == TipoData.EXATA.value:
        return data_quote == ini

    if tipo == TipoData.APROXIMADA.value:
        tolerancia = flexibilidade_dias if flexibilidade_dias is not None else 0
        return abs((data_quote - ini).days) <= tolerancia

    # INTERVALO (default, inclusive quando tipo é None)
    limite_fim = fim if fim is not None else ini
    return ini <= data_quote <= limite_fim


def _bate_data_ida(watch: Watch, quote: PriceQuote) -> bool:
    return _bate_janela_data(
        quote.data_ida, watch.tipo_data_ida, watch.data_ini, watch.data_fim,
        watch.flexibilidade_dias,
    )


def _bate_data_volta(watch: Watch, quote: PriceQuote) -> bool:
    if watch.data_volta_ini is None and watch.data_volta_fim is None:
        return True  # sem janela de desembarque definida: calendário decide o retorno
    return _bate_janela_data(
        quote.data_volta, watch.tipo_data_volta, watch.data_volta_ini,
        watch.data_volta_fim, watch.flexibilidade_dias,
    )


def _bate_mes_especifico(watch: Watch, quote: PriceQuote) -> bool:
    if watch.mes_especifico is None:
        return True
    return quote.data_ida.month == watch.mes_especifico


def _bate_duracao(watch: Watch, quote: PriceQuote) -> bool:
    if quote.data_volta is None:
        return True  # sem volta, duração da estadia não se aplica
    noites = (quote.data_volta - quote.data_ida).days
    if watch.duracao_min is not None and noites < watch.duracao_min:
        return False
    if watch.duracao_max is not None and noites > watch.duracao_max:
        return False
    return True


def _bate_ida_volta(watch: Watch, quote: PriceQuote) -> bool:
    if watch.ida_volta:
        return quote.data_volta is not None
    return quote.data_volta is None   # "embarque sem desembarque": só ida


def _bate_preco_minimo(watch: Watch, quote: PriceQuote) -> bool:
    if watch.preco_minimo is None:
        return True
    return quote.preco >= watch.preco_minimo


def _origem_volta_efetiva(valor: str | None, destino: str | None) -> str | None:
    """Normaliza "de onde a volta parte" para comparação.

    None e "igual ao destino" são a MESMA coisa — as duas formas de dizer
    "volta redonda" — e precisam colapsar antes do ==, senão um watch comum
    (origem_volta_iata=None) rejeitaria a cotação de um provider que preencheu
    origem_volta com o próprio destino.
    """
    if valor is None:
        return None
    valor = valor.upper()
    if destino is not None and valor == destino.upper():
        return None
    return valor


def _bate_open_jaw(watch: Watch, quote: PriceQuote) -> bool:
    """A volta precisa partir da cidade que o watch pede.

    Sem isto um watch open-jaw (volta de VCE) aceitaria a cotação redonda
    NAP->RIO que a busca comum devolve, e alertaria com um preço de um
    itinerário que o usuário não vai comprar.
    """
    if quote.data_volta is None:
        return True  # só-ida não tem trecho de retorno a conferir
    esperada = _origem_volta_efetiva(watch.origem_volta_iata, watch.destino_iata)
    obtida = _origem_volta_efetiva(quote.origem_volta, quote.destino)
    return esperada == obtida


def atende_criterios(watch: Watch, quote: PriceQuote) -> bool:
    """True se a cotação satisfaz TODOS os critérios do watch (data ida/volta
    exata|aproximada|intervalo, mês específico, duração min/max, ida_volta vs
    somente-ida, preco_minimo). Retorna False -> descarta ANTES de persistir.

    Use para fontes que reportam ida/volta de forma confiável (Travelpayouts
    calendar, SerpApi confirmação/sweep ao vivo). Para fontes de DESCOBERTA
    que nunca preenchem data_volta (ex.: SerpApi Explore), use
    atende_criterios_descoberta — aplicar este aqui rejeitaria 100% dos
    resultados de qualquer watch com ida_volta=True (o default).
    """
    return (
        _bate_ida_volta(watch, quote)
        and _bate_open_jaw(watch, quote)
        and _bate_data_ida(watch, quote)
        and _bate_data_volta(watch, quote)
        and _bate_mes_especifico(watch, quote)
        and _bate_duracao(watch, quote)
        and _bate_preco_minimo(watch, quote)
    )


def atende_criterios_descoberta(watch: Watch, quote: PriceQuote) -> bool:
    """Versão permissiva de atende_criterios para fontes de DESCOBERTA (ex.:
    SerpApi Explore), que mostram só o destino/preço/data de ida mais barata
    do mês — nunca uma data de volta real. Aplica apenas o que a descoberta
    consegue de fato avaliar (janela/mês de embarque, preço mínimo); NÃO
    filtra por ida_volta/duração/janela de volta, pois essas dimensões só
    existem depois da confirmação ao vivo (confirmar_candidatos), que roda
    com os critérios completos do watch antes de qualquer alerta sair.
    """
    return (
        _bate_data_ida(watch, quote)
        and _bate_mes_especifico(watch, quote)
        and _bate_preco_minimo(watch, quote)
    )
