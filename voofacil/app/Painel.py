"""Painel Streamlit do VooFacil — CRUD de watches, histórico e alertas.

Só UI: nenhuma regra de negócio aqui (segue o padrão do B2F). O worker
(voofacil/worker.py) roda em processo SEPARADO — este painel só lê o
banco e, no botão "varrer agora", dispara uma varredura síncrona pontual.

Rodar com:
    streamlit run voofacil/app/Painel.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# CRÍTICO: injeta st.secrets -> os.environ ANTES de importar voofacil.config/db,
# para que o DATABASE_URL e os tokens do Streamlit Cloud sejam lidos pela config.
from voofacil.secrets_bridge import carregar_segredos_streamlit  # noqa: E402

carregar_segredos_streamlit()

import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from voofacil.alerts.dispatcher import canais_padrao, despachar  # noqa: E402
from voofacil.alerts.mensagem import fmt_brl
from voofacil.db import SessionLocal, init_db
from voofacil.engine.baseline import baseline_rota
from voofacil.engine.confirmacao import confirmar_candidatos
from voofacil.engine.queries import (
    alertas_do_watch,
    alertas_recentes,
    alternar_ativo,
    criar_watch_regiao,
    criar_watch_rota,
    excluir_watch,
    historico_precos,
    listar_watches,
)
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
from voofacil.worker import _providers_padrao, executar_varredura
from scripts.seed_watch import ARRIVAL_AREA_ID, REGIOES

st.set_page_config(page_title="VooFácil", page_icon="✈️", layout="wide")
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


def aba_watches(session):
    st.subheader("Meus monitoramentos")

    watches = listar_watches(session)
    if not watches:
        st.info("Nenhum watch cadastrado ainda — crie um abaixo.")
    for w in watches:
        col1, col2, col3, col4 = st.columns([5, 2, 1, 1])
        destino_txt = w.destino_iata if w.tipo == "ROTA" else f"{len(w.destinos)} destino(s)"
        col1.markdown(f"**#{w.id} {w.nome}** — {w.origem_iata} → {destino_txt}"
                       + (" 🔴 inativo" if not w.ativo else ""))
        limite_txt = fmt_brl(w.preco_limite) if w.preco_limite else "—"
        col2.markdown(f"Limite: {limite_txt}")
        if col3.button("⏸️" if w.ativo else "▶️", key=f"toggle_{w.id}"):
            alternar_ativo(session, w.id)
            st.rerun()
        if col4.button("🗑️", key=f"del_{w.id}"):
            excluir_watch(session, w.id)
            st.rerun()

    st.divider()
    st.subheader("Novo monitoramento")
    tipo = st.radio(
        "Tipo",
        ["Rota específica", "Região/país (busca ampla)", "Multidestino personalizado"],
        horizontal=True,
    )

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
        origem = st.text_input("Origem (IATA)", max_chars=3, placeholder="GRU").upper()
        c1, c2 = st.columns(2)
        data_ini = c1.date_input("Embarque a partir de", value=date.today() + timedelta(days=30))
        data_fim = c2.date_input("Até", value=date.today() + timedelta(days=90))
        limite_str = st.text_input("Preço máximo em R$ (opcional)", placeholder="3500.00")

        destino = regiao = None
        multidestino_str = ""
        if tipo == "Rota específica":
            destino = st.text_input("Destino (IATA)", max_chars=3, placeholder="LIS").upper()
        elif tipo == "Região/país (busca ampla)":
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
            )

            if tipo == "Rota específica":
                if not destino or len(destino) != 3:
                    st.error("Informe o destino em código IATA (3 letras).")
                    return
                criar_watch_rota(
                    session, f"{origem} -> {destino} {ini_final:%d/%m}-{fim_final:%d/%m/%Y}",
                    origem, destino, ini_final, fim_final, limite,
                    **kwargs_avancados,
                )
            elif tipo == "Região/país (busca ampla)":
                criar_watch_regiao(
                    session, f"{origem} -> {regiao} {ini_final:%d/%m}-{fim_final:%d/%m/%Y}",
                    origem, REGIOES[regiao], ini_final, fim_final, limite,
                    arrival_area_id=ARRIVAL_AREA_ID.get(regiao),
                    **kwargs_avancados,
                )
            else:
                destinos_custom = [d.strip().upper() for d in multidestino_str.split(",") if d.strip()]
                if not destinos_custom:
                    st.error("Informe ao menos um destino IATA (separado por vírgula).")
                    return
                criar_watch_regiao(
                    session,
                    f"{origem} -> {'/'.join(destinos_custom)} {ini_final:%d/%m}-{fim_final:%d/%m/%Y}",
                    origem, destinos_custom, ini_final, fim_final, limite,
                    **kwargs_avancados,
                )
            st.success("Monitoramento criado!")
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
            c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
            c1.markdown(
                f"**{h.hora:02d}:{h.minuto:02d}**" + ("" if h.ativo else " 🔴 pausado")
            )
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
        "⚠️ Mudanças aqui valem a partir da PRÓXIMA verificação do worker "
        "(rodando a cada ~15 minutos na nuvem via GitHub Actions) — não é "
        "instantâneo. O cron do GitHub Actions em si é fixo (dispara a cada "
        "~15 min); o que muda dinamicamente, a partir do que você configura "
        "aqui, é O QUE o worker decide fazer em cada execução."
    )

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
