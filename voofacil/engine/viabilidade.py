"""Oráculo de VIABILIDADE de um monitoramento: por que ele nunca vai coletar.

Até aqui a única coisa no projeto que sabia responder "esses critérios são
impossíveis entre si?" era `_datas_da_busca`, escondida dentro do provider da
SerpApi — e ela respondia só para si mesma: devolvia None, pulava a chamada,
soltava um WARNING no stderr do worker e pronto. O painel aceitava criar o
watch dizendo "Monitoramento criado!", `criar_watch_rota` gravava sem reclamar,
e o usuário só descobria dias depois pelo selo vermelho de "0 snapshots em 7
dias" — que é a MESMA frase de "o agendamento morreu", "a rota não tem cache
na fonte gratuita" e "a API caiu".

CASO MEDIDO EM PRODUÇÃO (watch #4): embarque EXATO 07/05/2027, desembarque
EXATO 22/05/2027, duração fixada em 14 noites. 07/05 -> 22/05 são 15 noites;
nenhum par ida/volta satisfaz os três critérios ao mesmo tempo, então
NENHUMA cotação jamais passaria por `atende_criterios` e o banco ficou com 0
snapshots desde a criação. Um watch irmão com o MESMO par de datas e sem
duração fixada coletou R$ 5.410 pela TAP na mesma varredura — prova de que a
rota é cotável e de que a causa era exclusivamente a combinação de critérios.

Este módulo é PURO (sem sessão, sem rede, sem `st.`) e é a fonte ÚNICA
consumida por:
- voofacil/app/Painel.py      -> recusa antes de gravar, com a razão na tela;
- voofacil/engine/queries.py  -> recusa na API (criação E edição parcial), que
  é a porta usada por scripts/seeds/testes e que validava menos que o painel;
- voofacil/providers/*.py     -> não gasta cota nem rate limit num watch que
  vai ter 100% do resultado descartado depois;
- o selo de saúde do painel   -> explica os watches JÁ GRAVADOS, que continuam
  no banco coletando zero (validar só a entrada não conserta o #4 do usuário).

Divisão deliberada em dois níveis:
- `motivo_inviavel*` -> "isto NUNCA vai coletar nada" (bloqueia a gravação);
- `avisos_*`         -> "isto pode coletar muito menos do que você espera"
  (não bloqueia; só aparece na tela, porque o critério é legítimo).
"""
from __future__ import annotations

from datetime import date, timedelta

from voofacil.alerts.mensagem import fmt_brl
from voofacil.models import TipoData, TipoWatch, Watch

_MESES_PT = (
    "", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


def _eh_ida_volta(valor) -> bool:
    """None conta como ida-e-volta.

    `Watch.ida_volta` é NOT NULL com default True, mas o default do ORM só é
    aplicado no INSERT: um Watch TRANSIENTE (providers e testes montam vários)
    chega aqui com None. Tratar None como "somente ida" faria este módulo
    acusar de impossível justamente um watch redondo comum.
    """
    return valor is not False


# --------------------------------------------------------------------------
# Geometria das janelas — espelho EXATO do que `engine.filtros` vai aceitar
# --------------------------------------------------------------------------

def janela_aceita(
    tipo: str | None, ini: date | None, fim: date | None, flexibilidade_dias: int | None
) -> tuple[date, date] | None:
    """Intervalo [lo, hi] de datas que `voofacil.engine.filtros` vai ACEITAR.

    Espelha `_bate_janela_data` da engine: EXATA vira um ponto, APROXIMADA vira
    ± flexibilidade_dias em torno de `ini`, INTERVALO vira [ini, fim].
    None = o watch não define essa janela (nada a reconciliar).

    Mora AQUI, e não no provider da SerpApi (de onde veio), porque provider,
    validação de entrada e diagnóstico do painel precisam responder a mesma
    coisa — quando cada camada tinha a sua cópia elas divergiram, e a auditoria
    achou casos em que o provider fazia (e pagava) uma chamada que a engine
    depois descartava inteira.
    """
    if ini is None and fim is None:
        return None
    ini = ini if ini is not None else fim
    if tipo == TipoData.EXATA.value:
        return ini, ini
    if tipo == TipoData.APROXIMADA.value:
        folga = timedelta(days=abs(flexibilidade_dias or 0))
        return ini - folga, ini + folga
    hi = fim if fim is not None and fim >= ini else ini
    return ini, hi


def _janela_do_filtro(
    tipo: str | None, ini: date | None, fim: date | None, flexibilidade_dias: int | None
) -> tuple[date, date] | None:
    """Como `janela_aceita`, mas fiel ao ATALHO de `_bate_janela_data` para a
    janela ABERTA À ESQUERDA (só o `fim` preenchido, "voltar até 22/05").

    Nesse caso a engine aceita QUALQUER data até o teto, enquanto
    `janela_aceita` — que serve para ESCOLHER uma data de busca — colapsa no
    próprio teto. As duas se declaram espelhos e divergiam exatamente aqui; para
    julgar IMPOSSIBILIDADE vale a versão larga, senão este módulo recusaria
    watches que a engine aceitaria (falso positivo é pior que falso negativo:
    passaria a barrar monitoramento bom).
    """
    if ini is None and fim is not None:
        return date.min, fim
    return janela_aceita(tipo, ini, fim, flexibilidade_dias)


def _ultimo_dia(ano: int, mes: int) -> date:
    if mes == 12:
        return date(ano, 12, 31)
    return date(ano, mes + 1, 1) - timedelta(days=1)


def recortes_do_mes(janela: tuple[date, date], mes: int) -> list[tuple[date, date]]:
    """Trechos de `janela` que caem no mês `mes` (1-12) — pode haver mais de um
    quando a janela atravessa a virada do ano. Lista vazia = o mês pedido não
    tem NENHUM dia dentro da janela de embarque, ou seja, `mes_especifico`
    contradiz as datas.
    """
    lo, hi = janela
    trechos = []
    for ano in range(lo.year, hi.year + 1):
        ini = max(lo, date(ano, mes, 1))
        fim = min(hi, _ultimo_dia(ano, mes))
        if ini <= fim:
            trechos.append((ini, fim))
    return trechos


def _noites_das_janelas(
    janela_ida: tuple[date, date], janela_volta: tuple[date, date]
) -> tuple[int, int]:
    """(mínimo, máximo) de noites que o par de janelas permite.

    O mínimo sai da ida mais TARDE com a volta mais CEDO, e o máximo da ida
    mais cedo com a volta mais tarde. É a mesma conta que
    `travelpayouts._duracoes_alvo` já fazia para derivar a duração implícita —
    a diferença é que aqui ela também serve para CONFERIR a duração explícita,
    que era exatamente o que ninguém fazia.
    """
    ida_lo, ida_hi = janela_ida
    volta_lo, volta_hi = janela_volta
    return (volta_lo - ida_hi).days, (volta_hi - ida_lo).days


def _txt_janela(janela: tuple[date, date]) -> str:
    lo, hi = janela
    if lo == date.min:
        return f"até {hi:%d/%m/%Y}"       # janela aberta à esquerda
    return f"{lo:%d/%m/%Y}" if lo == hi else f"{lo:%d/%m/%Y} a {hi:%d/%m/%Y}"


def _txt_noites(lo: int, hi: int) -> str:
    return f"exatamente {lo} noites" if lo == hi else f"de {lo} a {hi} noites"


def _txt_duracao(dmin: int | None, dmax: int | None) -> str:
    if dmin is not None and dmax is not None:
        return f"exatamente {dmin} noites" if dmin == dmax else f"de {dmin} a {dmax} noites"
    if dmin is not None:
        return f"no mínimo {dmin} noites"
    return f"no máximo {dmax} noites"


def noites_implicitas(
    *,
    data_ini: date | None = None,
    data_fim: date | None = None,
    tipo_data_ida: str | None = None,
    tipo_data_volta: str | None = None,
    data_volta_ini: date | None = None,
    data_volta_fim: date | None = None,
    flexibilidade_dias: int | None = None,
) -> tuple[int, int] | None:
    """(mín, máx) de noites que as DATAS já determinam, ou None quando não há
    janela de desembarque (aí quem manda é a duração, e nada se contradiz).

    É o número que o formulário nunca mostrou: com ida e volta exatas a duração
    está 100% determinada e o campo "Duração da estadia" só pode contradizê-la.
    """
    janela_ida = janela_aceita(tipo_data_ida, data_ini, data_fim, flexibilidade_dias)
    janela_volta = janela_aceita(
        tipo_data_volta, data_volta_ini, data_volta_fim, flexibilidade_dias
    )
    if janela_ida is None or janela_volta is None:
        return None
    lo, hi = _noites_das_janelas(janela_ida, janela_volta)
    if hi < 0:
        return None                      # volta inteiramente antes da ida: não é estadia
    return max(lo, 0), hi


def noites_implicitas_do_watch(watch: Watch) -> tuple[int, int] | None:
    if not _eh_ida_volta(watch.ida_volta):
        return None
    return noites_implicitas(
        data_ini=watch.data_ini, data_fim=watch.data_fim,
        tipo_data_ida=watch.tipo_data_ida, tipo_data_volta=watch.tipo_data_volta,
        data_volta_ini=watch.data_volta_ini, data_volta_fim=watch.data_volta_fim,
        flexibilidade_dias=watch.flexibilidade_dias,
    )


# --------------------------------------------------------------------------
# "Este monitoramento NUNCA vai coletar" — bloqueia
# --------------------------------------------------------------------------

def motivo_inviavel_criterios(
    *,
    data_ini: date | None = None,
    data_fim: date | None = None,
    tipo_data_ida: str | None = None,
    tipo_data_volta: str | None = None,
    flexibilidade_dias: int | None = None,
    ida_volta: bool | None = True,
    data_volta_ini: date | None = None,
    data_volta_fim: date | None = None,
    duracao_min: int | None = None,
    duracao_max: int | None = None,
    mes_especifico: int | None = None,
    preco_minimo=None,
    preco_limite=None,
    tipo: str | None = None,
    destinos: list[str] | None = None,
) -> str | None:
    """A RAZÃO (texto pronto para a tela) pela qual esses critérios não podem
    produzir nenhuma cotação, ou None se a combinação é coletável.

    A ordem das checagens é do erro mais grosseiro para o mais sutil, para que
    a mensagem devolvida seja sempre a mais acionável possível.
    """
    redondo = _eh_ida_volta(ida_volta)
    eh_regiao = tipo == TipoWatch.REGIAO.value

    # -- fonte inexistente ---------------------------------------------------
    if eh_regiao and destinos is not None and not destinos:
        return ("Um monitoramento de região precisa de pelo menos um destino. Com a lista "
                "vazia a varredura não chega a fazer uma única chamada (o laço por destino "
                "não executa) e nem sequer um log de erro é gerado — o histórico fica "
                "permanentemente vazio.")
    if eh_regiao and not redondo:
        return ("Região/multidestino com 'Somente ida' não tem nenhuma fonte que possa "
                "coletar: o cache da Travelpayouts SEMPRE devolve uma data de volta "
                "(medido em 4 rotas, zero itens sem `return_at`), então 100% das cotações "
                "dela são descartadas; e a SerpApi só faz varredura ao vivo em "
                "monitoramentos de ROTA. Use 'Rota específica' ou desmarque 'Somente ida'.")

    # -- janelas internamente contraditórias ---------------------------------
    if data_ini and data_fim and data_ini > data_fim:
        return (f"A data final de embarque ({data_fim:%d/%m/%Y}) está antes da inicial "
                f"({data_ini:%d/%m/%Y}) — nenhuma data cabe nessa janela, o "
                "monitoramento nunca cotaria nada.")
    if not redondo and (data_volta_ini or data_volta_fim):
        return ("Este monitoramento está marcado como SOMENTE IDA, mas tem uma janela de "
                "desembarque configurada — os dois critérios se anulam: a busca é feita (e "
                "cobrada) em modo só-ida, a cotação volta sem data de retorno, e o filtro de "
                "desembarque descarta 100% do resultado. Desmarque 'Somente ida' ou apague a "
                "janela de desembarque.")
    if redondo and data_volta_ini and data_volta_fim and data_volta_ini > data_volta_fim:
        return (f"A data final de desembarque ({data_volta_fim:%d/%m/%Y}) está antes da "
                f"inicial ({data_volta_ini:%d/%m/%Y}) — janela de volta impossível.")
    if duracao_min is not None and duracao_max is not None and duracao_min > duracao_max:
        return (f"A duração mínima ({duracao_min} noites) é maior que a máxima "
                f"({duracao_max} noites) — nenhuma estadia caberia.")
    if preco_minimo is not None and preco_limite is not None and preco_minimo > preco_limite:
        return (f"O preço mínimo ({fmt_brl(preco_minimo)}) é maior que o limite máximo "
                f"({fmt_brl(preco_limite)}) — nenhum preço poderia disparar alerta.")

    janela_ida = janela_aceita(tipo_data_ida, data_ini, data_fim, flexibilidade_dias)
    if janela_ida is None:
        return ("Sem janela de embarque definida — o monitoramento não tem por onde "
                "começar a busca.")

    # -- mês específico x janela de embarque ---------------------------------
    # `_datas_da_busca` não conhecia `mes_especifico` e `_validar_criterios` nem
    # o recebia: a chamada era feita, COBRADA, e `_bate_mes_especifico`
    # reprovava a cotação inteira — 1 chamada por varredura, 3x/dia, para sempre.
    janelas_ida = [janela_ida]
    if mes_especifico:
        janelas_ida = recortes_do_mes(janela_ida, mes_especifico)
        if not janelas_ida:
            return (f"O mês escolhido ({_MESES_PT[mes_especifico]}) não tem nenhum dia dentro "
                    f"da janela de embarque ({_txt_janela(janela_ida)}). Toda cotação seria "
                    "descartada por estar fora do mês, depois de a busca já ter sido feita e "
                    "cobrada. Ajuste a janela de embarque ou tire a restrição de mês.")

    if not redondo:
        return None

    janela_volta = _janela_do_filtro(
        tipo_data_volta, data_volta_ini, data_volta_fim, flexibilidade_dias
    )
    if janela_volta is None:
        return None      # sem janela de desembarque a volta é derivada da duração

    # -- o caso do watch #4: datas x duração ---------------------------------
    dmin = duracao_min if duracao_min is not None else 0
    dmax = duracao_max
    for janela in janelas_ida:
        noites_lo, noites_hi = _noites_das_janelas(janela, janela_volta)
        possiveis_lo, possiveis_hi = max(noites_lo, 0), noites_hi
        if possiveis_hi < possiveis_lo:
            continue                                    # volta antes da ida
        if dmin > possiveis_hi or (dmax is not None and dmax < possiveis_lo):
            continue                                    # duração fora do que as datas permitem
        return None                                     # existe um par (ida, volta) válido

    janela = janelas_ida[0] if janelas_ida else janela_ida
    noites_lo, noites_hi = _noites_das_janelas(janela, janela_volta)
    if noites_hi < 0:
        return (f"A janela de desembarque ({_txt_janela(janela_volta)}) termina antes do "
                f"início da janela de embarque ({_txt_janela(janela)}) — a volta aconteceria "
                "antes da ida. Corrija uma das duas datas.")
    return (
        f"As datas escolhidas (embarque {_txt_janela(janela)}, desembarque "
        f"{_txt_janela(janela_volta)}) implicam {_txt_noites(max(noites_lo, 0), noites_hi)}, "
        f"mas a duração da estadia exige {_txt_duracao(duracao_min, duracao_max)} — nenhum par "
        "de ida e volta satisfaz os dois critérios ao mesmo tempo, então nenhuma cotação seria "
        f"aceita. Deixe a duração sem restrição, ou use {_txt_noites(max(noites_lo, 0), noites_hi)}, "
        "ou mude as datas."
    )


def motivo_inviavel(watch: Watch) -> str | None:
    """`motivo_inviavel_criterios` aplicado a um Watch já existente.

    É o que permite explicar os watches GRAVADOS ANTES desta validação: eles
    continuam no banco coletando zero, e validar só a entrada não os conserta.
    """
    return motivo_inviavel_criterios(
        data_ini=watch.data_ini,
        data_fim=watch.data_fim,
        tipo_data_ida=watch.tipo_data_ida,
        tipo_data_volta=watch.tipo_data_volta,
        flexibilidade_dias=watch.flexibilidade_dias,
        ida_volta=watch.ida_volta,
        data_volta_ini=watch.data_volta_ini,
        data_volta_fim=watch.data_volta_fim,
        duracao_min=watch.duracao_min,
        duracao_max=watch.duracao_max,
        mes_especifico=watch.mes_especifico,
        preco_minimo=watch.preco_minimo,
        preco_limite=watch.preco_limite,
        tipo=watch.tipo,
        destinos=watch.destinos,
    )


# --------------------------------------------------------------------------
# "Isto coleta muito menos do que você espera" — não bloqueia
# --------------------------------------------------------------------------

def avisos_criterios(
    *,
    data_ini: date | None = None,
    data_fim: date | None = None,
    tipo_data_ida: str | None = None,
    tipo_data_volta: str | None = None,
    flexibilidade_dias: int | None = None,
    ida_volta: bool | None = True,
    data_volta_ini: date | None = None,
    data_volta_fim: date | None = None,
    preco_minimo=None,
    preco_limite=None,
    tipo: str | None = None,
    open_jaw: bool = False,
) -> list[str]:
    """Combinações LEGÍTIMAS que reduzem muito (ou zeram, dependendo do dia) a
    coleta — e que hoje não deixam rastro nenhum na tela.

    Nenhuma delas justifica recusar a gravação; todas justificam avisar, porque
    o sintoma é sempre o mesmo "0 snapshots" e o usuário não tem como saber
    qual das causas é a dele.
    """
    avisos: list[str] = []
    redondo = _eh_ida_volta(ida_volta)

    if not redondo and tipo != TipoWatch.REGIAO.value:
        avisos.append(
            "Somente ida: o cache da Travelpayouts SEMPRE reporta uma data de volta "
            "(medido em 4 rotas, zero itens sem `return_at`), então 100% das cotações dela "
            "são descartadas por este monitoramento. A coleta depende exclusivamente da "
            "SerpApi, que faz 1 chamada por varredura e para quando o orçamento acaba."
        )
    if open_jaw:
        avisos.append(
            "Multi-cidade (open-jaw): só a SerpApi cota esse itinerário — a Travelpayouts "
            "não tem como representá-lo e pula o monitoramento por desenho. Sem chave da "
            "SerpApi ou sem orçamento no dia, a coleta fica em zero."
        )

    janela_ida = janela_aceita(tipo_data_ida, data_ini, data_fim, flexibilidade_dias)
    if janela_ida is not None and janela_ida[0] == janela_ida[1]:
        avisos.append(
            f"A janela de embarque é de um único dia ({janela_ida[0]:%d/%m/%Y}). O cache da "
            "Travelpayouts guarda só algumas dezenas de dias de embarque por rota (medido: "
            "3% a 10% do horizonte), então é provável que ele nunca tenha exatamente essa "
            "data — a coleta tende a vir só da SerpApi. Uma tolerância de alguns dias "
            "aumenta muito a chance de haver preço."
        )

    if (tipo_data_ida == TipoData.EXATA.value and data_ini and data_fim
            and data_ini != data_fim):
        avisos.append(
            f"A data de embarque está marcada como EXATA, então a data final da janela "
            f"({data_fim:%d/%m/%Y}) é ignorada: só {data_ini:%d/%m/%Y} é aceito."
        )
    if (redondo and tipo_data_volta == TipoData.EXATA.value and data_volta_ini
            and data_volta_fim and data_volta_ini != data_volta_fim):
        avisos.append(
            f"A data de desembarque está marcada como EXATA, então a data final da janela "
            f"({data_volta_fim:%d/%m/%Y}) é ignorada: só {data_volta_ini:%d/%m/%Y} é aceito."
        )

    # Tolerância 0 degrada APROXIMADA para EXATA sem nenhum sinal, e o painel
    # continua escrevendo "aproximada" na aba de critérios.
    if tipo_data_ida == TipoData.APROXIMADA.value and not flexibilidade_dias:
        avisos.append(
            "A data de embarque é 'aproximada' com tolerância 0 — na prática ela se comporta "
            "exatamente como uma data EXATA. Informe uma tolerância em dias para ter folga."
        )
    if (redondo and tipo_data_volta == TipoData.APROXIMADA.value
            and (data_volta_ini or data_volta_fim) and not flexibilidade_dias):
        avisos.append(
            "A data de desembarque é 'aproximada' com tolerância 0 — na prática ela se "
            "comporta exatamente como uma data EXATA."
        )

    if preco_minimo is not None and preco_limite is None:
        avisos.append(
            f"O preço mínimo ({fmt_brl(preco_minimo)}) é um PISO: toda cotação MAIS BARATA "
            "que ele é descartada antes de virar histórico. Se a ideia era 'espero pagar por "
            "volta disso', use o preço-alvo (aproximado), que é só uma referência no gráfico."
        )
    return avisos


def avisos_do_watch(watch: Watch) -> list[str]:
    return avisos_criterios(
        data_ini=watch.data_ini,
        data_fim=watch.data_fim,
        tipo_data_ida=watch.tipo_data_ida,
        tipo_data_volta=watch.tipo_data_volta,
        flexibilidade_dias=watch.flexibilidade_dias,
        ida_volta=watch.ida_volta,
        data_volta_ini=watch.data_volta_ini,
        data_volta_fim=watch.data_volta_fim,
        preco_minimo=watch.preco_minimo,
        preco_limite=watch.preco_limite,
        tipo=watch.tipo,
        open_jaw=bool(getattr(watch, "open_jaw", False)),
    )
