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
from voofacil.providers.travelpayouts import TravelpayoutsProvider
from voofacil.worker import executar_varredura
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
    tipo = st.radio("Tipo", ["Rota específica", "Região/país (busca ampla)"], horizontal=True)

    with st.form("novo_watch", clear_on_submit=True):
        origem = st.text_input("Origem (IATA)", max_chars=3, placeholder="GRU").upper()
        c1, c2 = st.columns(2)
        data_ini = c1.date_input("Embarque a partir de", value=date.today() + timedelta(days=30))
        data_fim = c2.date_input("Até", value=date.today() + timedelta(days=90))
        limite_str = st.text_input("Preço-limite em R$ (opcional)", placeholder="3500.00")

        if tipo == "Rota específica":
            destino = st.text_input("Destino (IATA)", max_chars=3, placeholder="LIS").upper()
        else:
            regiao = st.selectbox("Região", sorted(REGIOES))
            st.caption(f"{len(REGIOES.get(regiao, []))} destinos na lista curada"
                       + (" · busca ampla via SerpApi Explore habilitada"
                          if regiao in ARRIVAL_AREA_ID else " · sem kgmid — só a lista curada"))

        if st.form_submit_button("Criar monitoramento", type="primary"):
            try:
                limite = Decimal(limite_str) if limite_str.strip() else None
            except InvalidOperation:
                st.error("Preço-limite inválido.")
                return
            if not origem or len(origem) != 3:
                st.error("Informe a origem em código IATA (3 letras).")
                return

            if tipo == "Rota específica":
                if not destino or len(destino) != 3:
                    st.error("Informe o destino em código IATA (3 letras).")
                    return
                criar_watch_rota(
                    session, f"{origem} -> {destino} {data_ini:%d/%m}-{data_fim:%d/%m/%Y}",
                    origem, destino, data_ini, data_fim, limite,
                )
            else:
                criar_watch_regiao(
                    session, f"{origem} -> {regiao} {data_ini:%d/%m}-{data_fim:%d/%m/%Y}",
                    origem, REGIOES[regiao], data_ini, data_fim, limite,
                    arrival_area_id=ARRIVAL_AREA_ID.get(regiao),
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


def main() -> None:
    session = _session()

    st.title("✈️ VooFácil — monitor de passagens aéreas")
    if st.sidebar.button("🔎 Varrer agora", type="primary", width="stretch"):
        with st.spinner("Consultando preços..."):
            resultado = executar_varredura(session, providers=[TravelpayoutsProvider()])
            confirmados = confirmar_candidatos(session, resultado.candidatos)
            enviados = despachar(session, confirmados, canais_padrao())
            session.commit()
        st.sidebar.success(
            f"{sum(resultado.snapshots_por_watch.values())} cotações · "
            f"{len(enviados)} alerta(s) enviado(s)"
        )

    aba1, aba2, aba3 = st.tabs(["📋 Watches", "📈 Histórico", "🔔 Alertas"])
    with aba1:
        aba_watches(session)
    with aba2:
        aba_historico(session)
    with aba3:
        aba_alertas(session)


main()
