"""Painel Streamlit do VooFacil — CRUD de watches, histórico e alertas.

Só UI: nenhuma regra de negócio aqui (segue o padrão do B2F). O worker
(voofacil/worker.py) roda em processo SEPARADO — este painel só lê o
banco e, no botão "varrer agora", dispara uma varredura síncrona pontual.

Rodar com:
    streamlit run voofacil/app/Painel.py
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# CRÍTICO: injeta st.secrets -> os.environ ANTES de importar voofacil.config/db,
# para que o DATABASE_URL e os tokens do Streamlit Cloud sejam lidos pela config.
from voofacil.secrets_bridge import carregar_segredos_streamlit  # noqa: E402

carregar_segredos_streamlit()

import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from voofacil.alerts.dispatcher import canais_padrao, despachar  # noqa: E402
from voofacil.alerts.mensagem import fmt_brl
from voofacil.db import SessionLocal, init_db
from voofacil.engine.baseline import baseline_rota
from voofacil.engine.confirmacao import confirmar_candidatos
from voofacil.engine.queries import (
    alertas_do_watch,
    alertas_recentes,
    alternar_ativo,
    atualizar_watch,
    criar_watch_regiao,
    criar_watch_rota,
    excluir_watch,
    historico_precos,
    listar_watches,
)
from voofacil.models import PriceSnapshot, utcnow
# NOTA: módulo criado por outro agente em paralelo (contrato combinado
# previamente) — pode não existir ainda neste ambiente enquanto os dois
# trabalham ao mesmo tempo; a integração final é verificada depois.
from voofacil.engine.agendamento import (  # noqa: E402
    alternar_ativo as alternar_ativo_horario,
    atualizar_horario,
    criar_horario,
    excluir_horario,
    garantir_horarios_padrao,
    listar_horarios,
)
from voofacil.providers.autocomplete import buscar_locais, rotulo_local
from voofacil.worker import _providers_padrao, _silenciar_log_de_url, executar_varredura
from scripts.seed_watch import ARRIVAL_AREA_ID, REGIOES

st.set_page_config(page_title="VooFácil", page_icon="✈️", layout="wide")
# O botão "Varrer agora" chama a SerpApi daqui, e a chave dela viaja na query
# string — sem isto o httpx a imprimiria em texto claro no log do Streamlit
# Cloud a cada varredura manual. Ver _silenciar_log_de_url.
_silenciar_log_de_url()
init_db()


def _session():
    if "db_session" not in st.session_state:
        st.session_state.db_session = SessionLocal()
    return st.session_state.db_session


def _grafico_historico(session, watch) -> go.Figure:
    snapshots = historico_precos(session, watch.id)
    fig = go.Figure()
    if not snapshots:
        fig.update_layout(title="Sem histórico ainda — rode uma varredura")
        return fig

    for destino in sorted({s.destino for s in snapshots}):
        pontos = [s for s in snapshots if s.destino == destino]
        fig.add_trace(go.Scatter(
            x=[p.coletado_em for p in pontos],
            y=[float(p.preco) for p in pontos],
            mode="lines+markers",
            name=destino,
        ))

    base = baseline_rota(session, watch.origem_iata, snapshots[-1].destino)
    if base:
        fig.add_hline(y=float(base.minimo), line_dash="dot", line_color="green",
                       annotation_text=f"mínimo histórico {fmt_brl(base.minimo)}")
    if watch.preco_limite:
        fig.add_hline(y=float(watch.preco_limite), line_dash="dash", line_color="red",
                       annotation_text=f"seu limite {fmt_brl(watch.preco_limite)}")
    # D7: o preço-alvo (aproximado) configurado pelo usuário deixou de ser
    # decorativo — vira uma linha de referência no gráfico, igual ao limite e ao
    # mínimo histórico. Assim o alvo que a pessoa digitou realmente aparece.
    if watch.preco_aproximado:
        fig.add_hline(y=float(watch.preco_aproximado), line_dash="dashdot",
                       line_color="royalblue",
                       annotation_text=f"seu preço-alvo {fmt_brl(watch.preco_aproximado)}")

    for alerta in alertas_do_watch(session, watch.id):
        fig.add_vline(x=alerta.enviado_em, line_color="orange", opacity=0.3)

    fig.update_layout(
        title=f"{watch.origem_iata} → {'/'.join(watch.destinos) if watch.tipo == 'REGIAO' else watch.destino_iata}",
        yaxis_title="Preço (R$)",
        separators=",.",
        height=420,
        legend_title="Destino",
    )
    return fig


MESES_PT = [
    "Nenhum", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]  # índice = mês (1-12); índice 0 ("Nenhum") = sem restrição de mês

TIPOS_DATA = ["INTERVALO", "APROXIMADA", "EXATA"]
_ROTULOS_TIPO_DATA_IDA = {
    "INTERVALO": "Intervalo (usa as datas de embarque acima)",
    "APROXIMADA": "Data aproximada (com tolerância)",
    "EXATA": "Data exata",
}
_ROTULOS_TIPO_DATA_VOLTA = {
    "INTERVALO": "Deixe o calendário decidir (padrão)",
    "APROXIMADA": "Data aproximada (com tolerância)",
    "EXATA": "Data exata",
}


def _buscador_local(rotulo: str, key: str) -> str | None:
    """Campo de busca por nome (cidade/aeroporto/país) que resolve pro código
    IATA — estilo Skyscanner. Roda FORA do form (mesmo motivo do Tipo/
    Critérios avançados: precisa rerodar a cada busca, não só no submit).

    Também aceita digitar o código IATA direto (3 letras) sem escolher da
    lista — não força o usuário que já sabe o código a fazer buscas extras.
    Uma escolha explícita na lista de resultados tem prioridade sobre o
    código digitado (cobre o caso de a pessoa procurar por nome de cidade).
    """
    termo = st.text_input(
        f"{rotulo} — cidade, aeroporto, país ou código IATA",
        key=f"termo_{key}",
        placeholder="Ex.: Lisboa, LIS, Portugal",
    ).strip()

    codigo_manual = termo.upper() if len(termo) == 3 and termo.isalpha() else None
    codigo_selecionado = None

    if len(termo) >= 2:
        resultados = buscar_locais(termo)
        if resultados:
            opcoes = ["(escolha um resultado ou use o código digitado acima)"] + [
                rotulo_local(r) for r in resultados
            ]
            escolha = st.selectbox(
                f'Resultados para "{termo}"', opcoes, key=f"sel_{key}",
            )
            if escolha != opcoes[0]:
                codigo_selecionado = resultados[opcoes.index(escolha) - 1]["code"]
        else:
            st.caption("Nenhum resultado — se já souber o código IATA (3 letras), pode digitar direto.")

    codigo_final = codigo_selecionado or codigo_manual
    st.caption(f"✅ Usando **{codigo_final}**" if codigo_final else "⚠️ Ainda sem código IATA resolvido.")
    return codigo_final


# ==========================================================================
# Helpers PUROS (sem `st.`) — extraídos para poderem ser testados de verdade,
# não só por AST. Cobrem a montagem do nome (D8), a validação de entrada (D13)
# e o selo de saúde (D8). Ver tests/test_painel_agendamento.py.
# ==========================================================================

_ROTULO_TIPO_DATA_CURTO = {
    "INTERVALO": "Intervalo (qualquer data da janela)",
    "APROXIMADA": "Aproximada (com tolerância)",
    "EXATA": "Exata",
}


def _fmt_data(d) -> str:
    """Data em dd/mm/aaaa, ou travessão explícito quando não definida."""
    return f"{d:%d/%m/%Y}" if d else "—"


def _nome_watch(
    origem: str, destino_txt: str, ida_ini, ida_fim,
    *,
    somente_ida: bool,
    volta_ini=None,
    volta_fim=None,
    tipo_data_ida: str = "INTERVALO",
    tipo_data_volta: str = "INTERVALO",
    origem_volta: str | None = None,
) -> str:
    """Monta o nome do watch a partir do que foi REALMENTE configurado (D8).

    O nome antigo saía de origem/destino + janela de ida apenas; quando a ida era
    EXATA a janela colapsava (ini == fim) e o mesmo dia aparecia duas vezes, e a
    data de VOLTA nunca entrava — foi exatamente o "#2 RIO -> NAP 07/05-07/05/2027"
    que o usuário abriu chamado. Agora:

    - ida com data única (EXATA/APROXIMADA ou janela de 1 dia) imprime a data uma
      vez só; se houver volta única, vira "<ida> a <volta>";
    - ida em janela imprime "<ini> a <fim>" e a volta entra como "volta ...";
    - o rótulo final descreve ida/volta e a natureza das datas (exatas/aproximada).

    Ex.: "RIO -> NAP - 07/05/2027 a 22/05/2027 (ida e volta, datas exatas)".
    Num itinerário open-jaw a cidade de retorno entra logo após o destino —
    "RIO -> NAP (volta VCE) - ..." —, senão o nome afirmaria uma viagem
    redonda que não é a que está sendo monitorada.
    Trunca em 80 caracteres (a coluna `Watch.nome` é String(80)).
    """
    ida_unica = ida_ini == ida_fim
    if ida_unica:
        datas_txt = f"{ida_ini:%d/%m/%Y}"
    else:
        datas_txt = f"{ida_ini:%d/%m} a {ida_fim:%d/%m/%Y}"

    if not somente_ida:
        if volta_ini and volta_fim and volta_ini != volta_fim:
            # janela própria de desembarque
            datas_txt += f", volta {volta_ini:%d/%m} a {volta_fim:%d/%m/%Y}"
        elif volta_ini:
            if ida_unica:
                datas_txt = f"{ida_ini:%d/%m/%Y} a {volta_ini:%d/%m/%Y}"
            else:
                datas_txt += f", volta {volta_ini:%d/%m/%Y}"

    descritores = ["somente ida"] if somente_ida else ["ida e volta"]
    exatas = tipo_data_ida == "EXATA" and (somente_ida or tipo_data_volta == "EXATA")
    if exatas:
        descritores.append("datas exatas")
    else:
        if tipo_data_ida == "EXATA":
            descritores.append("ida exata")
        elif tipo_data_ida == "APROXIMADA":
            descritores.append("ida aproximada")
        if not somente_ida:
            if tipo_data_volta == "EXATA":
                descritores.append("volta exata")
            elif tipo_data_volta == "APROXIMADA":
                descritores.append("volta aproximada")

    destino_rotulo = destino_txt
    if not somente_ida and origem_volta and origem_volta.upper() != destino_txt.upper():
        destino_rotulo = f"{destino_txt} (volta {origem_volta.upper()})"

    nome = f"{origem} -> {destino_rotulo} - {datas_txt} ({', '.join(descritores)})"
    return nome[:80]


def _texto_destino(tipo: str, destino_iata, destinos) -> str:
    """Rótulo curto de destino para o nome do watch (não estoura os 80 chars)."""
    if tipo == "ROTA":
        return (destino_iata or "?").upper()
    lista = [d.upper() for d in (destinos or [])]
    if len(lista) <= 3:
        return "/".join(lista) if lista else "?"
    return f"{len(lista)} destinos"


def _criterios_watch(watch) -> list[tuple[str, str]]:
    """Lista (rótulo, valor) de CADA critério configurável do watch (D8).

    Um valor não definido vira travessão explícito ("—") — o painel escondia
    quase tudo o que o usuário configurava; aqui nada fica implícito.
    """
    if watch.duracao_min and watch.duracao_max:
        if watch.duracao_min == watch.duracao_max:
            dur = f"{watch.duracao_min} noites (exata)"
        else:
            dur = f"{watch.duracao_min} a {watch.duracao_max} noites"
    elif watch.duracao_min:
        dur = f"a partir de {watch.duracao_min} noites"
    elif watch.duracao_max:
        dur = f"até {watch.duracao_max} noites"
    else:
        dur = "—"

    if watch.data_volta_ini and watch.data_volta_fim and watch.data_volta_ini != watch.data_volta_fim:
        janela_volta = f"{_fmt_data(watch.data_volta_ini)} a {_fmt_data(watch.data_volta_fim)}"
    elif watch.data_volta_ini:
        janela_volta = _fmt_data(watch.data_volta_ini)
    else:
        janela_volta = "—"

    if watch.data_ini and watch.data_fim and watch.data_ini != watch.data_fim:
        embarque = f"{_fmt_data(watch.data_ini)} a {_fmt_data(watch.data_fim)}"
    else:
        embarque = _fmt_data(watch.data_ini)

    mes = MESES_PT[watch.mes_especifico] if watch.mes_especifico else "—"

    itens = [
        ("Embarque (janela)", embarque),
        ("Tipo da data de embarque",
         _ROTULO_TIPO_DATA_CURTO.get(watch.tipo_data_ida, watch.tipo_data_ida)),
        ("Somente ida?", "não — ida e volta" if watch.ida_volta else "sim (sem volta)"),
    ]
    if watch.ida_volta:
        itens.append((
            "Volta parte de",
            f"{watch.origem_volta_iata} (multi-cidade)" if watch.open_jaw
            else f"{watch.destino_iata or '—'} (mesmo destino)",
        ))
        itens.append(("Tipo da data de desembarque",
                      _ROTULO_TIPO_DATA_CURTO.get(watch.tipo_data_volta, watch.tipo_data_volta)))
        itens.append(("Janela de desembarque", janela_volta))
        itens.append(("Duração da estadia", dur))
    pessoas = watch.total_passageiros
    itens.extend([
        ("Mês específico", mes),
        ("Passageiros",
         f"{pessoas} (preços cotados por pessoa)" if pessoas > 1 else "1"),
        ("Preço máximo (limite)", fmt_brl(watch.preco_limite) if watch.preco_limite else "—"),
        ("Preço mínimo (piso)", fmt_brl(watch.preco_minimo) if watch.preco_minimo else "—"),
        ("Preço-alvo (aproximado)",
         fmt_brl(watch.preco_aproximado) if watch.preco_aproximado else "—"),
        ("Flexibilidade (dias)",
         str(watch.flexibilidade_dias) if watch.flexibilidade_dias is not None else "—"),
    ])
    return itens


def _selo_saude(n_24h: int, n_7d: int) -> tuple[str, str]:
    """Traduz a contagem de snapshots recentes num selo (nível, texto) (D8).

    É o item de maior valor da aba: com a coleta zerada e o agendamento morto,
    é este selo que denuncia sozinho que o monitoramento não coleta nada há dias.
    """
    if n_24h > 0:
        return ("verde", f"🟢 coletando — {n_24h} nas últimas 24h · {n_7d} em 7 dias")
    if n_7d > 0:
        return ("amarelo",
                f"🟡 nada nas últimas 24h (mas {n_7d} nos últimos 7 dias) — pode estar atrasando")
    return ("vermelho", "🔴 este monitoramento não está coletando nada (0 snapshots em 7 dias)")


def _validar_criterios(
    *,
    data_ini,
    data_fim,
    somente_ida: bool,
    data_volta_ini=None,
    data_volta_fim=None,
    duracao_min=None,
    duracao_max=None,
    preco_minimo=None,
    preco_limite=None,
    destinos_custom=None,
) -> str | None:
    """Valida os critérios ANTES de gravar (D13); devolve a RAZÃO ou None se ok.

    Sem isto o painel aceitava, com "Monitoramento criado!", watches que nunca
    produzem nada: data final antes da inicial, duração mínima maior que a máxima,
    piso de preço acima do teto, e destinos digitados por nome (não IATA).
    """
    if data_ini and data_fim and data_ini > data_fim:
        return (f"A data final de embarque ({data_fim:%d/%m/%Y}) está antes da inicial "
                f"({data_ini:%d/%m/%Y}) — nenhuma data cabe nessa janela, o "
                "monitoramento nunca cotaria nada.")
    if (not somente_ida and data_volta_ini and data_volta_fim
            and data_volta_ini > data_volta_fim):
        return (f"A data final de desembarque ({data_volta_fim:%d/%m/%Y}) está antes da "
                f"inicial ({data_volta_ini:%d/%m/%Y}) — janela de volta impossível.")
    if duracao_min is not None and duracao_max is not None and duracao_min > duracao_max:
        return (f"A duração mínima ({duracao_min} noites) é maior que a máxima "
                f"({duracao_max} noites) — nenhuma estadia caberia.")
    if preco_minimo is not None and preco_limite is not None and preco_minimo > preco_limite:
        return (f"O preço mínimo ({fmt_brl(preco_minimo)}) é maior que o limite máximo "
                f"({fmt_brl(preco_limite)}) — nenhum preço poderia disparar alerta.")
    for d in destinos_custom or []:
        if len(d) != 3 or not d.isalpha():
            return (f"Destino inválido: {d!r}. Use códigos IATA de 3 letras (ex.: LIS, "
                    "MAD, CDG). Nomes de cidade não são aceitos no campo multidestino.")
    return None


class _FalhaEscrita:
    """Sentinela de `_escrita_protegida`: a mutação falhou e virou st.error."""


FALHA_ESCRITA = _FalhaEscrita()


def _escrita_protegida(descricao: str, funcao, session, *args, **kwargs):
    """Executa uma mutação sem nunca deixar a Session do painel envenenada.

    O painel guarda a MESMA Session em st.session_state; uma exceção não tratada
    numa escrita quebraria TODAS as operações seguintes com PendingRollbackError
    até recarregar a página. Aqui qualquer falha vira rollback defensivo +
    st.error com a razão, devolvendo `FALHA_ESCRITA` para o chamador decidir se
    faz `st.rerun()`. As funções de `queries.py` já fazem rollback interno; este
    wrapper é a segunda rede (e a que converte a exceção em mensagem na tela).
    """
    try:
        return funcao(session, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - proteger a sessão exige capturar tudo
        try:
            session.rollback()
        except Exception:
            pass
        st.error(f"Não foi possível {descricao}: {exc}")
        return FALHA_ESCRITA


def _contagem_snapshots_recentes(session, watch_id: int) -> tuple[int, int]:
    """(snapshots nas últimas 24h, nas últimas 24h*7) — base do selo de saúde.

    Query enxuta (COUNT no banco, não traz linha nenhuma para o Python). NÃO
    reaproveita `historico_precos`, que cai de volta para dados antigos quando a
    janela recente está vazia — justamente o caso que o selo precisa flagrar.
    """
    agora = utcnow()

    def _contar(desde) -> int:
        return int(session.scalar(
            select(func.count(PriceSnapshot.id)).where(
                PriceSnapshot.watch_id == watch_id,
                PriceSnapshot.coletado_em >= desde,
            )
        ) or 0)

    return _contar(agora - timedelta(hours=24)), _contar(agora - timedelta(days=7))


def _form_editar_watch(session, w) -> None:
    """Formulário de edição de um watch, pré-preenchido com os valores atuais (D8).

    Diferente do form de criação (que revela campos conforme seletores FORA do
    form), aqui TODOS os campos aparecem de uma vez, já carregados — um st.form
    não rerroda a cada widget, então mostrar tudo é o caminho previsível. A
    interpretação (colapsar datas EXATA/APROXIMADA, ligar/desligar volta) acontece
    no submit, com a MESMA validação da criação (D13) antes de gravar.
    """
    st.markdown(f"#### ✏️ Editando **#{w.id} {w.nome}**")
    eh_rota = w.tipo == "ROTA"

    with st.form(f"editar_watch_{w.id}"):
        origem = st.text_input(
            "Origem (IATA)", value=w.origem_iata or "", key=f"edit_origem_{w.id}",
        ).strip().upper()
        destino_str = destinos_str = ""
        if eh_rota:
            destino_str = st.text_input(
                "Destino (IATA)", value=w.destino_iata or "", key=f"edit_destino_{w.id}",
            ).strip().upper()
        else:
            destinos_str = st.text_area(
                "Destinos (IATA separados por vírgula)",
                value=", ".join(w.destinos), key=f"edit_destinos_{w.id}",
            )

        c1, c2 = st.columns(2)
        data_ini = c1.date_input(
            "Embarque — início (ou data-alvo, se exata/aproximada)",
            value=w.data_ini, key=f"edit_data_ini_{w.id}",
        )
        data_fim = c2.date_input("Embarque — fim", value=w.data_fim, key=f"edit_data_fim_{w.id}")
        tipo_data_ida = st.selectbox(
            "Tipo de data de embarque", TIPOS_DATA,
            index=TIPOS_DATA.index(w.tipo_data_ida) if w.tipo_data_ida in TIPOS_DATA else 0,
            format_func=lambda t: _ROTULOS_TIPO_DATA_IDA[t], key=f"edit_tdi_{w.id}",
        )

        ida_volta = st.checkbox("Ida e volta", value=w.ida_volta, key=f"edit_iv_{w.id}")
        st.caption("Campos de volta abaixo só valem quando 'Ida e volta' está marcado.")
        tipo_data_volta = st.selectbox(
            "Tipo de data de desembarque", TIPOS_DATA,
            index=TIPOS_DATA.index(w.tipo_data_volta) if w.tipo_data_volta in TIPOS_DATA else 0,
            format_func=lambda t: _ROTULOS_TIPO_DATA_VOLTA[t], key=f"edit_tdv_{w.id}",
        )
        usar_janela_volta = st.checkbox(
            "Definir janela própria de desembarque (só p/ tipo Intervalo)",
            value=w.data_volta_ini is not None, key=f"edit_ujv_{w.id}",
        )
        cv1, cv2 = st.columns(2)
        data_volta_ini = cv1.date_input(
            "Desembarque — início (ou data-alvo)",
            value=w.data_volta_ini or (date.today() + timedelta(days=37)),
            key=f"edit_dvi_{w.id}",
        )
        data_volta_fim = cv2.date_input(
            "Desembarque — fim",
            value=w.data_volta_fim or (date.today() + timedelta(days=97)),
            key=f"edit_dvf_{w.id}",
        )

        cd1, cd2 = st.columns(2)
        dur_min = int(cd1.number_input(
            "Duração mínima (noites, 0 = sem restrição)", min_value=0, max_value=180,
            value=int(w.duracao_min or 0), step=1, key=f"edit_dmin_{w.id}",
        ))
        dur_max = int(cd2.number_input(
            "Duração máxima (noites, 0 = sem restrição)", min_value=0, max_value=180,
            value=int(w.duracao_max or 0), step=1, key=f"edit_dmax_{w.id}",
        ))

        mes_nome = st.selectbox(
            "Mês específico", MESES_PT, index=w.mes_especifico or 0, key=f"edit_mes_{w.id}",
        )
        flex = int(st.number_input(
            "Flexibilidade (dias, para datas aproximadas)", min_value=0, max_value=60,
            value=int(w.flexibilidade_dias or 0), step=1, key=f"edit_flex_{w.id}",
        ))

        cp1, cp2, cp3 = st.columns(3)
        limite_str = cp1.text_input(
            "Preço máximo (R$)", value=str(w.preco_limite) if w.preco_limite else "",
            key=f"edit_lim_{w.id}",
        )
        minimo_str = cp2.text_input(
            "Preço mínimo (R$)", value=str(w.preco_minimo) if w.preco_minimo else "",
            key=f"edit_min_{w.id}",
        )
        aprox_str = cp3.text_input(
            "Preço-alvo aproximado (R$)",
            value=str(w.preco_aproximado) if w.preco_aproximado else "", key=f"edit_apx_{w.id}",
        )

        bsalvar, bcancelar = st.columns(2)
        salvar = bsalvar.form_submit_button("Salvar alterações", type="primary")
        cancelar = bcancelar.form_submit_button("Cancelar")

    if cancelar:
        st.session_state.watch_editando = None
        st.rerun()
    if not salvar:
        return

    # --- interpretação + validação (mesma disciplina da criação) ---
    try:
        limite = Decimal(limite_str) if limite_str.strip() else None
        preco_minimo = Decimal(minimo_str) if minimo_str.strip() else None
        preco_aproximado = Decimal(aprox_str) if aprox_str.strip() else None
    except InvalidOperation:
        st.error("Preço inválido — use números (ex.: 3500.00).")
        return
    if not origem or len(origem) != 3:
        st.error("Informe a origem em código IATA (3 letras).")
        return

    if tipo_data_ida in ("EXATA", "APROXIMADA"):
        ini_final = fim_final = data_ini
    else:
        ini_final, fim_final = data_ini, data_fim

    somente_ida = not ida_volta
    if somente_ida:
        volta_ini = volta_fim = None
    elif tipo_data_volta in ("EXATA", "APROXIMADA"):
        volta_ini, volta_fim = data_volta_ini, None
    elif usar_janela_volta:
        volta_ini, volta_fim = data_volta_ini, data_volta_fim
    else:
        volta_ini = volta_fim = None

    duracao_min = dur_min or None
    duracao_max = dur_max or None
    mes_especifico = MESES_PT.index(mes_nome) if mes_nome != "Nenhum" else None
    flexibilidade_dias = flex or None

    destinos_custom = None
    destino_iata = None
    if eh_rota:
        destino_iata = destino_str
        if not destino_iata or len(destino_iata) != 3:
            st.error("Informe o destino em código IATA (3 letras).")
            return
    else:
        destinos_custom = [d.strip().upper() for d in destinos_str.split(",") if d.strip()]
        if not destinos_custom:
            st.error("Informe ao menos um destino IATA (separado por vírgula).")
            return

    erro = _validar_criterios(
        data_ini=ini_final, data_fim=fim_final, somente_ida=somente_ida,
        data_volta_ini=volta_ini, data_volta_fim=volta_fim,
        duracao_min=duracao_min, duracao_max=duracao_max,
        preco_minimo=preco_minimo, preco_limite=limite,
        destinos_custom=destinos_custom,
    )
    if erro:
        st.error(erro)
        return

    destino_nome = destino_iata if eh_rota else _texto_destino("REGIAO", None, destinos_custom)
    tdv = tipo_data_volta if not somente_ida else "INTERVALO"
    nome = _nome_watch(
        origem, destino_nome, ini_final, fim_final, somente_ida=somente_ida,
        volta_ini=volta_ini, volta_fim=volta_fim,
        tipo_data_ida=tipo_data_ida, tipo_data_volta=tdv,
    )
    campos_destino = {"destino": destino_iata} if eh_rota else {"destinos": destinos_custom}

    resultado = _escrita_protegida(
        "salvar as alterações", atualizar_watch, session, w.id,
        nome=nome, origem=origem, **campos_destino,
        data_ini=ini_final, data_fim=fim_final,
        preco_limite=limite, preco_minimo=preco_minimo, preco_aproximado=preco_aproximado,
        ida_volta=ida_volta, duracao_min=duracao_min, duracao_max=duracao_max,
        tipo_data_ida=tipo_data_ida, tipo_data_volta=tdv,
        flexibilidade_dias=flexibilidade_dias,
        data_volta_ini=volta_ini, data_volta_fim=volta_fim,
        mes_especifico=mes_especifico,
    )
    if resultado is FALHA_ESCRITA:
        return
    st.session_state.watch_editando = None
    if resultado is None:
        st.error("Monitoramento não encontrado (pode ter sido excluído em outra aba).")
    else:
        st.success(f"Monitoramento atualizado: {nome}")
    st.rerun()


def aba_watches(session):
    st.subheader("Meus monitoramentos")

    watches = listar_watches(session)
    if not watches:
        st.info("Nenhum watch cadastrado ainda — crie um abaixo.")
    editando_id = st.session_state.get("watch_editando")
    for w in watches:
        # Enquanto este watch está em edição, a linha vira o formulário de edição.
        if editando_id == w.id:
            _form_editar_watch(session, w)
            st.divider()
            continue

        col1, col2, col3, col4, col5 = st.columns([5, 2, 1, 1, 1])
        destino_txt = w.destino_iata if w.tipo == "ROTA" else f"{len(w.destinos)} destino(s)"
        col1.markdown(f"**#{w.id} {w.nome}** — {w.origem_iata} → {destino_txt}"
                       + (" 🔴 inativo" if not w.ativo else ""))
        limite_txt = fmt_brl(w.preco_limite) if w.preco_limite else "—"
        col2.markdown(f"Limite: {limite_txt}")
        if col3.button("✏️", key=f"editar_{w.id}", help="Editar critérios"):
            st.session_state.watch_editando = w.id
            st.rerun()
        if col4.button("⏸️" if w.ativo else "▶️", key=f"toggle_{w.id}",
                       help="Pausar/retomar"):
            if _escrita_protegida("pausar/retomar o monitoramento",
                                  alternar_ativo, session, w.id) is not FALHA_ESCRITA:
                st.rerun()
        if col5.button("🗑️", key=f"del_{w.id}", help="Excluir"):
            if _escrita_protegida("excluir o monitoramento",
                                  excluir_watch, session, w.id) is not FALHA_ESCRITA:
                st.rerun()

        # SELO DE SAÚDE (D8): é o que denuncia sozinho coleta zerada/agendamento
        # morto. Watch pausado não coleta de propósito, então nem entra no selo.
        if not w.ativo:
            st.caption("⏸️ pausado — não coleta enquanto estiver inativo")
        else:
            n_24h, n_7d = _contagem_snapshots_recentes(session, w.id)
            nivel, texto = _selo_saude(n_24h, n_7d)
            if nivel == "vermelho":
                st.error(texto)
            elif nivel == "amarelo":
                st.warning(texto)
            else:
                st.caption(texto)

        # CRITÉRIOS (D8): cada critério configurável, com travessão quando vazio.
        with st.expander("Critérios deste monitoramento"):
            for rotulo, valor in _criterios_watch(w):
                st.markdown(f"- **{rotulo}:** {valor}")

        st.divider()

    st.subheader("Novo monitoramento")
    tipo = st.radio(
        "Tipo",
        ["Rota específica", "Região/país (busca ampla)", "Multidestino personalizado"],
        horizontal=True,
    )

    # ---------- Origem/destino (busca por nome, fora do form pelo mesmo
    # motivo do Tipo acima: precisa reagir a cada busca, não só no submit) ---
    st.markdown("**De onde e para onde**")
    col_origem, col_destino = st.columns(2)
    with col_origem:
        origem = _buscador_local("Origem", "origem")
    destino = None
    if tipo == "Rota específica":
        with col_destino:
            destino = _buscador_local("Destino", "destino")

    # ---------- Multi-cidade / open-jaw ----------
    # Fica FORA do form pelo mesmo motivo do Tipo/Origem/Destino: o checkbox
    # decide se o buscador aparece, e widgets dentro de um st.form só reagem
    # no submit. Só existe para ROTA — um watch de REGIÃO não tem destino
    # único de onde derivar o trecho de volta.
    origem_volta = None
    if tipo == "Rota específica":
        if st.checkbox(
            "A volta parte de outra cidade (multi-cidade / open-jaw)",
            help="Ex.: voa Rio → Nápoles, atravessa a Itália por terra e volta "
                 "de Veneza. Esses monitoramentos são cotados só pela SerpApi — "
                 "o Travelpayouts não cota multi-cidade.",
        ):
            origem_volta = _buscador_local("Cidade de onde a volta parte", "origem_volta")

    # ---------- Critérios avançados (opcionais) ----------
    # FORA do st.form: widgets dentro de um form só disparam rerun no submit,
    # então os SELETORES que decidem quais campos aparecem (mesmo padrão já
    # usado pelo radio "Tipo" acima) precisam ficar aqui para reagir na hora.
    # Os campos de VALOR correspondentes (datas/números) ficam dentro do
    # form, renderizados condicionalmente a partir destas escolhas.
    with st.expander("Critérios avançados (opcional)"):
        st.caption("Deixe como está para o comportamento padrão: qualquer data dentro da "
                   "janela de embarque abaixo, ida e volta, sem piso de preço.")

        somente_ida = st.checkbox("Somente ida (sem volta)")

        st.markdown("**Data de embarque**")
        tipo_data_ida = st.selectbox(
            "Tipo de data de embarque", TIPOS_DATA,
            format_func=lambda t: _ROTULOS_TIPO_DATA_IDA[t],
        )

        tipo_data_volta = "INTERVALO"
        usar_janela_volta = False
        modo_duracao = "Sem restrição"
        if not somente_ida:
            st.markdown("**Data de desembarque (retorno)**")
            tipo_data_volta = st.selectbox(
                "Tipo de data de desembarque", TIPOS_DATA,
                format_func=lambda t: _ROTULOS_TIPO_DATA_VOLTA[t],
                key="sel_tipo_data_volta",
            )
            if tipo_data_volta == "INTERVALO":
                usar_janela_volta = st.checkbox(
                    "Definir janela própria de desembarque (padrão: calendário decide)",
                )
            st.markdown("**Duração da estadia**")
            modo_duracao = st.radio(
                "Duração", ["Sem restrição", "Exata", "Faixa (mínimo/máximo)"],
                horizontal=True,
            )

        st.markdown("**Mês específico**")
        mes_nome = st.selectbox("Restringir a um mês", MESES_PT)
        mes_especifico = MESES_PT.index(mes_nome) if mes_nome != "Nenhum" else None

        st.markdown("**Preço**")
        cp1, cp2 = st.columns(2)
        minimo_str = cp1.text_input("Preço mínimo em R$ (opcional)", placeholder="1500.00")
        aprox_str = cp2.text_input("Preço aproximado em R$ (opcional)", placeholder="2800.00")

    precisa_flexibilidade = tipo_data_ida == "APROXIMADA" or (
        not somente_ida and tipo_data_volta == "APROXIMADA"
    )
    precisa_campos_avancados = (
        tipo_data_ida != "INTERVALO"
        or precisa_flexibilidade
        or (not somente_ida and (
            tipo_data_volta != "INTERVALO"
            or usar_janela_volta
            or modo_duracao != "Sem restrição"
        ))
    )

    with st.form("novo_watch", clear_on_submit=True):
        c1, c2 = st.columns(2)
        data_ini = c1.date_input("Embarque a partir de", value=date.today() + timedelta(days=30))
        data_fim = c2.date_input("Até", value=date.today() + timedelta(days=90))
        cl1, cl2 = st.columns([2, 1])
        limite_str = cl1.text_input(
            "Preço máximo POR PESSOA em R$ (opcional)", placeholder="3500.00",
        )
        passageiros = cl2.number_input(
            "Passageiros", min_value=1, max_value=9, value=1, step=1,
            help="As cotações são sempre por 1 pessoa, e o limite acima é "
                 "comparado com esse preço individual. O grupo serve para o "
                 "painel e o alerta mostrarem o custo total da viagem.",
        )

        regiao = None
        multidestino_str = ""
        if tipo == "Região/país (busca ampla)":
            regiao = st.selectbox("Região", sorted(REGIOES))
            st.caption(f"{len(REGIOES.get(regiao, []))} destinos na lista curada"
                       + (" · busca ampla via SerpApi Explore habilitada"
                          if regiao in ARRIVAL_AREA_ID else " · sem kgmid — só a lista curada"))
        else:
            multidestino_str = st.text_area(
                "Destinos (códigos IATA separados por vírgula)",
                placeholder="LIS, MAD, CDG, FCO",
            )
            st.caption("Vira um monitoramento tipo Região — sem busca ampla via SerpApi Explore "
                       "(sem kgmid para uma lista personalizada).")

        # Valores dependentes das escolhas do expander acima — só aparecem
        # quando relevantes, então o form continua simples por padrão.
        data_ida_alvo = None
        data_volta_ini_form = data_volta_fim_form = None
        duracao_min = duracao_max = None
        flexibilidade_dias = None
        if precisa_campos_avancados:
            st.caption("Valores dos critérios avançados escolhidos acima:")

            if tipo_data_ida == "EXATA":
                data_ida_alvo = st.date_input(
                    "Data exata de embarque", value=date.today() + timedelta(days=30),
                    key="data_ida_exata",
                )
            elif tipo_data_ida == "APROXIMADA":
                data_ida_alvo = st.date_input(
                    "Data alvo de embarque", value=date.today() + timedelta(days=30),
                    key="data_ida_aprox",
                )

            if not somente_ida:
                if tipo_data_volta == "EXATA":
                    data_volta_ini_form = st.date_input(
                        "Data exata de desembarque", value=date.today() + timedelta(days=37),
                        key="data_volta_exata",
                    )
                elif tipo_data_volta == "APROXIMADA":
                    data_volta_ini_form = st.date_input(
                        "Data alvo de desembarque", value=date.today() + timedelta(days=37),
                        key="data_volta_aprox",
                    )
                elif usar_janela_volta:
                    cv1, cv2 = st.columns(2)
                    data_volta_ini_form = cv1.date_input(
                        "Desembarque a partir de", value=date.today() + timedelta(days=37),
                        key="data_volta_ini",
                    )
                    data_volta_fim_form = cv2.date_input(
                        "Até", value=date.today() + timedelta(days=97),
                        key="data_volta_fim",
                    )

                if modo_duracao == "Exata":
                    dur = st.number_input("Noites", min_value=1, max_value=180, value=7, step=1)
                    duracao_min = duracao_max = int(dur)
                elif modo_duracao == "Faixa (mínimo/máximo)":
                    cd1, cd2 = st.columns(2)
                    duracao_min = int(cd1.number_input(
                        "Mínimo (noites)", min_value=1, max_value=180, value=7, step=1,
                    ))
                    duracao_max = int(cd2.number_input(
                        "Máximo (noites)", min_value=1, max_value=180, value=14, step=1,
                    ))

            if precisa_flexibilidade:
                flexibilidade_dias = int(st.number_input(
                    "Tolerância (dias) — usada nas datas aproximadas acima",
                    min_value=0, max_value=60, value=3, step=1,
                ))

        if st.form_submit_button("Criar monitoramento", type="primary"):
            try:
                limite = Decimal(limite_str) if limite_str.strip() else None
                preco_minimo = Decimal(minimo_str) if minimo_str.strip() else None
                preco_aproximado = Decimal(aprox_str) if aprox_str.strip() else None
            except InvalidOperation:
                st.error("Preço inválido — use números (ex.: 3500.00).")
                return
            if not origem or len(origem) != 3:
                st.error("Informe a origem em código IATA (3 letras).")
                return

            # Data(s) EXATA/APROXIMADA substituem a janela simples (data_ini/data_fim)
            if tipo_data_ida in ("EXATA", "APROXIMADA") and data_ida_alvo is not None:
                ini_final = fim_final = data_ida_alvo
            else:
                ini_final, fim_final = data_ini, data_fim

            # Destinos: resolvidos ANTES de validar, para checar os IATAs do
            # multidestino (D13) sem gravar nome-de-cidade como se fosse código.
            destino_iata = destino.upper() if destino else None
            destinos_custom = None
            origem_volta_iata = None
            if tipo == "Rota específica":
                if not destino_iata or len(destino_iata) != 3:
                    st.error("Informe o destino em código IATA (3 letras).")
                    return
                if origem_volta is not None:
                    origem_volta_iata = origem_volta.upper() if origem_volta else None
                    if not origem_volta_iata or len(origem_volta_iata) != 3:
                        st.error("Informe em código IATA (3 letras) a cidade de onde a volta parte, "
                                 "ou desmarque a opção de multi-cidade.")
                        return
                    if somente_ida:
                        st.error("Multi-cidade exige ida e volta — um monitoramento somente de ida "
                                 "não tem trecho de retorno.")
                        return
                    if origem_volta_iata == destino_iata:
                        origem_volta_iata = None   # mesma cidade: é uma volta redonda comum
            elif tipo == "Multidestino personalizado":
                destinos_custom = [d.strip().upper() for d in multidestino_str.split(",") if d.strip()]
                if not destinos_custom:
                    st.error("Informe ao menos um destino IATA (separado por vírgula).")
                    return

            # D13: recusa antes de gravar, com a RAZÃO. Sem isto o painel criava
            # watches que nunca cotam nada (janela invertida, duração impossível,
            # piso acima do teto, destino que não é IATA) dizendo "criado!".
            erro = _validar_criterios(
                data_ini=ini_final, data_fim=fim_final, somente_ida=somente_ida,
                data_volta_ini=data_volta_ini_form, data_volta_fim=data_volta_fim_form,
                duracao_min=duracao_min, duracao_max=duracao_max,
                preco_minimo=preco_minimo, preco_limite=limite,
                destinos_custom=destinos_custom,
            )
            if erro:
                st.error(erro)
                return

            kwargs_avancados = dict(
                ida_volta=not somente_ida,
                duracao_min=duracao_min,
                duracao_max=duracao_max,
                tipo_data_ida=tipo_data_ida,
                tipo_data_volta=tipo_data_volta if not somente_ida else "INTERVALO",
                flexibilidade_dias=flexibilidade_dias,
                data_volta_ini=data_volta_ini_form if not somente_ida else None,
                data_volta_fim=data_volta_fim_form if not somente_ida else None,
                mes_especifico=mes_especifico,
                preco_minimo=preco_minimo,
                preco_aproximado=preco_aproximado,
                passageiros=int(passageiros),
            )
            nome_comum = dict(
                somente_ida=somente_ida,
                volta_ini=data_volta_ini_form if not somente_ida else None,
                volta_fim=data_volta_fim_form if not somente_ida else None,
                tipo_data_ida=tipo_data_ida,
                tipo_data_volta=tipo_data_volta if not somente_ida else "INTERVALO",
                origem_volta=origem_volta_iata,
            )

            if tipo == "Rota específica":
                nome = _nome_watch(origem, destino_iata, ini_final, fim_final, **nome_comum)
                resultado = _escrita_protegida(
                    "criar o monitoramento", criar_watch_rota, session, nome,
                    origem, destino_iata, ini_final, fim_final, limite,
                    origem_volta=origem_volta_iata,
                    **kwargs_avancados,
                )
            elif tipo == "Região/país (busca ampla)":
                nome = _nome_watch(origem, regiao, ini_final, fim_final, **nome_comum)
                resultado = _escrita_protegida(
                    "criar o monitoramento", criar_watch_regiao, session, nome,
                    origem, REGIOES[regiao], ini_final, fim_final, limite,
                    arrival_area_id=ARRIVAL_AREA_ID.get(regiao),
                    **kwargs_avancados,
                )
            else:
                destino_nome = _texto_destino("REGIAO", None, destinos_custom)
                nome = _nome_watch(origem, destino_nome, ini_final, fim_final, **nome_comum)
                resultado = _escrita_protegida(
                    "criar o monitoramento", criar_watch_regiao, session, nome,
                    origem, destinos_custom, ini_final, fim_final, limite,
                    **kwargs_avancados,
                )
            if resultado is FALHA_ESCRITA:
                return
            st.success(f"Monitoramento criado: {nome}")
            # Os buscadores de Origem/Destino ficam FORA do st.form (ver
            # _buscador_local), então clear_on_submit não os alcança — limpa
            # na mão para não reusar sem querer a mesma origem/destino no
            # próximo monitoramento.
            for k in ("termo_origem", "sel_origem", "termo_destino", "sel_destino",
                      "termo_origem_volta", "sel_origem_volta"):
                st.session_state.pop(k, None)
            st.rerun()


def aba_historico(session):
    watches = listar_watches(session)
    if not watches:
        st.info("Crie um monitoramento na aba anterior para ver histórico aqui.")
        return

    opcoes = {f"#{w.id} {w.nome}": w for w in watches}
    escolha = st.selectbox("Monitoramento", list(opcoes))
    watch = opcoes[escolha]
    st.plotly_chart(_grafico_historico(session, watch), width="stretch")


def aba_alertas(session):
    alertas = alertas_recentes(session)
    if not alertas:
        st.info("Nenhum alerta enviado ainda.")
        return
    for a in alertas:
        watch = a.watch
        st.markdown(
            f"**{a.enviado_em:%d/%m/%Y %H:%M}** · {watch.nome} · "
            f"{fmt_brl(a.preco)} · `{a.gatilho}` · canal {a.canal}"
            + (" · ✔️ confirmado ao vivo" if a.confirmado_ao_vivo else " · ⚠️ preço de cache")
        )


_TITULOS_TIPO_HORARIO = {
    "VARREDURA": "🛫 Varredura regular",
    "EXPLORE": "🌍 Busca ampla (Explore)",
}

# BRT = UTC-3 (Brasil não usa mais horário de verão desde 2019) — mesma
# convenção de voofacil/engine/agendamento.py e voofacil/worker.py, sem lib
# externa de timezone.
_OFFSET_UTC_PARA_BRT_HORAS = 3


def _texto_ultima_execucao(ultima_execucao: datetime | None) -> str:
    """Formata `HorarioAgendado.ultima_execucao` (UTC) para leitura em BRT.

    É o carimbo da execução REAL (quando o worker de fato varreu por causa
    daquele horário) — é olhando para ele que o usuário confirma, com os
    próprios olhos, que a automação está viva. `None` = nunca rodou.

    O SQLite devolve datetime NAIVE mesmo em coluna `DateTime(timezone=True)`;
    como tudo que o worker grava está em UTC, um valor sem tzinfo é assumido
    como UTC (mesmo tratamento de `agendamento._como_utc_aware`).
    """
    if ultima_execucao is None:
        return "⏳ ainda não rodou"
    if ultima_execucao.tzinfo is None:
        em_utc = ultima_execucao.replace(tzinfo=timezone.utc)
    else:
        em_utc = ultima_execucao.astimezone(timezone.utc)
    em_brt = em_utc - timedelta(hours=_OFFSET_UTC_PARA_BRT_HORAS)
    return f"✅ última: {em_brt:%d/%m %H:%M}"


def _secao_horarios(session, tipo: str) -> None:
    """Lista/edita/exclui/adiciona horários de um `tipo` (VARREDURA ou EXPLORE).

    Usa `voofacil.engine.agendamento` (contrato combinado com o outro agente)
    para toda a lógica — este bloco é só UI, igual ao resto do arquivo.
    """
    st.markdown(f"#### {_TITULOS_TIPO_HORARIO.get(tipo, tipo)}")

    horarios = listar_horarios(session, tipo, incluir_inativos=True)
    if not horarios:
        st.info("Nenhum horário cadastrado ainda.")

    editando = st.session_state.get("agendamento_editando")

    for h in horarios:
        if editando == (tipo, h.id):
            c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
            nova_hora = c1.number_input(
                "Hora", min_value=0, max_value=23, value=int(h.hora), step=1,
                key=f"edit_hora_{tipo}_{h.id}",
            )
            novo_minuto = c2.number_input(
                "Minuto", min_value=0, max_value=59, value=int(h.minuto), step=1,
                key=f"edit_minuto_{tipo}_{h.id}",
            )
            if c3.button("✅", key=f"confirmar_edit_{tipo}_{h.id}"):
                try:
                    atualizar_horario(session, h.id, int(nova_hora), int(novo_minuto))
                except ValueError as e:
                    st.error(str(e))
                else:
                    st.session_state.agendamento_editando = None
                    st.rerun()
            if c4.button("❌", key=f"cancelar_edit_{tipo}_{h.id}"):
                st.session_state.agendamento_editando = None
                st.rerun()
        else:
            # c1 mais larga que na edição: além do HH:MM, carrega o carimbo da
            # última execução real (em caption, logo abaixo) — informação nova
            # sem espremer os botões nem poluir a linha do horário.
            c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
            c1.markdown(
                f"**{h.hora:02d}:{h.minuto:02d}**" + ("" if h.ativo else " 🔴 pausado")
            )
            c1.caption(_texto_ultima_execucao(h.ultima_execucao))
            if c2.button("✏️", key=f"editar_horario_{tipo}_{h.id}"):
                st.session_state.agendamento_editando = (tipo, h.id)
                st.rerun()
            if c3.button("⏸️" if h.ativo else "▶️", key=f"toggle_horario_{tipo}_{h.id}"):
                alternar_ativo_horario(session, h.id)
                st.rerun()
            if c4.button("🗑️", key=f"del_horario_{tipo}_{h.id}"):
                excluir_horario(session, h.id)
                st.rerun()

    st.markdown("**Adicionar horário**")
    with st.form(f"novo_horario_{tipo}", clear_on_submit=True):
        ca, cb, cc = st.columns([2, 2, 1])
        hora_nova = ca.number_input(
            "Hora", min_value=0, max_value=23, value=6, step=1, key=f"add_hora_{tipo}",
        )
        minuto_novo = cb.number_input(
            "Minuto", min_value=0, max_value=59, value=0, step=1, key=f"add_minuto_{tipo}",
        )
        if cc.form_submit_button("Adicionar horário", width="stretch"):
            try:
                criar_horario(session, tipo, int(hora_nova), int(minuto_novo))
            except ValueError as e:
                st.error(str(e))
            else:
                st.success("Horário adicionado!")
                st.rerun()


def aba_agendamento(session):
    st.subheader("Horários de varredura")
    st.caption(
        "⏰ O horário que você configura aqui é um **\"não antes de\"**, não uma "
        "promessa de pontualidade: a varredura roda no PRIMEIRO disparo do "
        "worker DEPOIS do horário. Na nuvem quem dispara é o GitHub Actions, "
        "que descarta boa parte dos agendamentos de alta frequência — medimos "
        "de 4 a 7 execuções por dia, não as 96 que o cron pede — então "
        "o atraso pode ir de minutos a algumas horas. Nenhum horário é perdido: "
        "quando o worker finalmente acorda, ele roda o que ficou para trás "
        "(no máximo uma vez por horário por dia). O carimbo \"última\" de cada "
        "horário abaixo mostra quando ele DE FATO rodou — é por ali que você "
        "confirma que a automação está viva."
    )

    # O worker roda em outro processo/máquina e a sessão do painel sobrevive
    # entre reruns no st.session_state — sem expirar, o SQLAlchemy serviria o
    # `ultima_execucao` que já estava na identity map (ele não sobrescreve
    # atributo já carregado) e o carimbo abaixo nasceria velho. Só afeta
    # exibição; as demais abas já renderizaram neste ponto.
    session.expire_all()

    col1, col2 = st.columns(2)
    with col1:
        _secao_horarios(session, "VARREDURA")
    with col2:
        _secao_horarios(session, "EXPLORE")


def main() -> None:
    session = _session()

    if not st.session_state.get("horarios_padrao_garantidos"):
        garantir_horarios_padrao(session)
        st.session_state.horarios_padrao_garantidos = True

    st.title("✈️ VooFácil — monitor de passagens aéreas")
    if st.sidebar.button("🔎 Varrer agora", type="primary", width="stretch"):
        with st.spinner("Consultando preços..."):
            resultado = executar_varredura(session, providers=_providers_padrao(session))
            confirmados = confirmar_candidatos(session, resultado.candidatos)
            enviados = despachar(session, confirmados, canais_padrao())
            session.commit()
        st.sidebar.success(
            f"{sum(resultado.snapshots_por_watch.values())} cotações · "
            f"{len(enviados)} alerta(s) enviado(s)"
        )

    aba1, aba2, aba3, aba4 = st.tabs(
        ["📋 Watches", "📈 Histórico", "🔔 Alertas", "⏰ Agendamento"]
    )
    with aba1:
        aba_watches(session)
    with aba2:
        aba_historico(session)
    with aba3:
        aba_alertas(session)
    with aba4:
        aba_agendamento(session)


main()
