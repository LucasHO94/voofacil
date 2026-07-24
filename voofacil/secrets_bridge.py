"""Ponte de segredos para o Streamlit Community Cloud.

Na nuvem, o Streamlit entrega os segredos via st.secrets (não como variáveis de
ambiente). O resto do app lê configuração via pydantic-settings (env vars). Esta
ponte copia st.secrets -> os.environ ANTES de voofacil.config ser importado, para
que o mesmo código funcione em três lugares sem alteração:

- Streamlit Cloud: segredos vêm de st.secrets -> injetados aqui.
- GitHub Actions (worker): segredos já vêm como env vars -> ponte é no-op.
- Dev local: usa o .env (pydantic-settings) -> ponte é no-op.

Deve ser chamada na PRIMEIRA linha do entrypoint Streamlit, antes de importar
qualquer módulo que carregue voofacil.config/db.
"""
from __future__ import annotations

import os


def carregar_segredos_streamlit() -> None:
    """Se rodando sob Streamlit com secrets definidos, injeta-os em os.environ.

    Sem secrets.toml (dev local / worker no GitHub Actions), o Streamlit só
    levanta a exceção ao ITERAR os secrets — por isso a leitura fica toda dentro
    do try, tornando a ponte um no-op silencioso nesses ambientes.
    """
    try:
        import streamlit as st
    except ModuleNotFoundError:
        return

    try:
        itens = dict(st.secrets).items()  # dispara o parse do secrets.toml
    except Exception:  # noqa: BLE001 — sem secrets configurados: nada a fazer
        return

    for chave, valor in itens:
        if isinstance(valor, (str, int, float)) and chave not in os.environ:
            os.environ[chave] = str(valor)
