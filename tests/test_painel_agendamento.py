"""Smoke test da aba "Agendamento" no Painel Streamlit.

Painel.py é um script Streamlit — chama `main()` no nível do módulo, então
um `import` de verdade já disparava efeitos colaterais (init_db, criação de
sessão, widgets fora de contexto de execução do Streamlit) mesmo antes desta
mudança. Além disso, este arquivo importa `voofacil.engine.agendamento`,
módulo criado por outro agente em paralelo que pode não existir ainda neste
ambiente.

Por isso o teste aqui é deliberadamente leve: valida que o arquivo
CONTINUA COMPILANDO (sem erro de sintaxe) após a nova aba ser adicionada, e
que a integração esperada com `voofacil.engine.agendamento` está de fato
presente no código-fonte. Cobertura funcional da lógica de agendamento em si
já existe (e deve continuar existindo) nos testes de
`voofacil/engine/agendamento.py`, escritos por outro agente.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

PAINEL_PATH = Path(__file__).resolve().parent.parent / "voofacil" / "app" / "Painel.py"


def _fonte() -> str:
    return PAINEL_PATH.read_text(encoding="utf-8")


def test_painel_compila_sem_erro_de_sintaxe():
    ast.parse(_fonte(), filename=str(PAINEL_PATH))


def test_painel_importa_contrato_do_agendamento():
    fonte = _fonte()
    assert "from voofacil.engine.agendamento import" in fonte
    for nome in (
        "listar_horarios",
        "criar_horario",
        "atualizar_horario",
        "excluir_horario",
        "garantir_horarios_padrao",
        "alternar_ativo as alternar_ativo_horario",
    ):
        assert nome in fonte, f"{nome!r} não referenciado em Painel.py"


def test_painel_tem_aba_agendamento():
    fonte = _fonte()
    assert "aba_agendamento" in fonte
    assert "⏰ Agendamento" in fonte
    # os dois tipos do contrato (TipoHorario) precisam aparecer na UI
    assert '"VARREDURA"' in fonte
    assert '"EXPLORE"' in fonte


def test_painel_semeia_horarios_padrao_uma_vez_por_sessao():
    fonte = _fonte()
    assert "garantir_horarios_padrao(session)" in fonte
    assert "horarios_padrao_garantidos" in fonte


def test_painel_documenta_limitacao_do_intervalo_do_worker():
    """Nota sobre o delay real (~15 min via GitHub Actions) precisa existir."""
    fonte = _fonte()
    assert re.search(r"~?\s*15\s*min", fonte), "nota sobre intervalo do worker não encontrada"
    assert "GitHub Actions" in fonte
