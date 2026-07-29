"""Smoke test da aba "Agendamento" no Painel Streamlit.

Painel.py é um script Streamlit — chama `main()` no nível do módulo, então
um `import` de verdade já dispararia efeitos colaterais (init_db, criação de
sessão, widgets fora do contexto de execução do Streamlit). Por isso os
testes daqui são TEXTUAIS/AST: leem o código-fonte, garantem que ele compila
e que a integração esperada com `voofacil.engine.agendamento` (e com o campo
`HorarioAgendado.ultima_execucao`) está de fato presente.

Para as funções PURAS do painel (as que não tocam em `st`), vamos um passo
além: extraímos só aquele nó da AST, compilamos isoladamente e exercitamos de
verdade — ver `_carregar_funcao_isolada`. É o que dá cobertura funcional real
da formatação de `ultima_execucao` sem importar o módulo inteiro.

Cobertura da lógica de agendamento em si (catch-up: `horarios_devidos`,
`marcar_executados`) vive em `tests/test_agendamento.py`.
"""
from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

PAINEL_PATH = Path(__file__).resolve().parent.parent / "voofacil" / "app" / "Painel.py"


def _fonte() -> str:
    return PAINEL_PATH.read_text(encoding="utf-8")


def _arvore() -> ast.Module:
    return ast.parse(_fonte(), filename=str(PAINEL_PATH))


def _funcao(nome: str) -> ast.FunctionDef:
    for no in ast.walk(_arvore()):
        if isinstance(no, ast.FunctionDef) and no.name == nome:
            return no
    raise AssertionError(f"função {nome!r} não encontrada em Painel.py")


def _constante(nome: str):
    for no in _arvore().body:
        if isinstance(no, ast.Assign) and any(
            isinstance(alvo, ast.Name) and alvo.id == nome for alvo in no.targets
        ):
            return ast.literal_eval(no.value)
    raise AssertionError(f"constante {nome!r} não encontrada em Painel.py")


def _carregar_funcao_isolada(nome: str):
    """Compila e executa APENAS a função `nome` (extraída da AST do Painel).

    Importar Painel.py rodaria `main()`; extrair o nó da função e compilá-lo
    sozinho dá o comportamento real da função sem nenhum efeito colateral. Só
    serve para funções puras (sem `st.`)."""
    modulo = ast.Module(body=[_funcao(nome)], type_ignores=[])
    ast.fix_missing_locations(modulo)
    escopo = {
        "datetime": datetime,
        "timedelta": timedelta,
        "timezone": timezone,
        "_OFFSET_UTC_PARA_BRT_HORAS": _constante("_OFFSET_UTC_PARA_BRT_HORAS"),
    }
    exec(compile(modulo, str(PAINEL_PATH), "exec"), escopo)  # noqa: S102
    return escopo[nome]


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


# --------------------------------------------------------------------------
# Visibilidade: a aba precisa MOSTRAR quando cada horário rodou de verdade
# --------------------------------------------------------------------------

def test_secao_horarios_le_ultima_execucao_e_formata():
    """A listagem de horários tem que ler `h.ultima_execucao` e passar pelo
    formatador — é o que prova ao usuário que a automação voltou a rodar."""
    secao = _funcao("_secao_horarios")
    atributos = {n.attr for n in ast.walk(secao) if isinstance(n, ast.Attribute)}
    assert "ultima_execucao" in atributos, "aba não lê HorarioAgendado.ultima_execucao"

    chamadas = {
        n.func.id
        for n in ast.walk(secao)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_texto_ultima_execucao" in chamadas


def test_texto_ultima_execucao_avisa_quando_nunca_rodou():
    formatar = _carregar_funcao_isolada("_texto_ultima_execucao")
    assert "ainda não rodou" in formatar(None)


def test_texto_ultima_execucao_converte_utc_para_brt():
    """14:37 UTC = 11:37 BRT — o horário exibido é o do Brasil, como o resto
    do projeto (o poll atrasado das 11:37 é justamente o caso real medido)."""
    formatar = _carregar_funcao_isolada("_texto_ultima_execucao")
    texto = formatar(datetime(2026, 7, 28, 14, 37, tzinfo=timezone.utc))
    assert "28/07 11:37" in texto


def test_texto_ultima_execucao_aceita_datetime_naive():
    """O SQLite devolve datetime NAIVE mesmo em coluna DateTime(timezone=True);
    o painel não pode quebrar nem exibir hora errada por causa disso."""
    formatar = _carregar_funcao_isolada("_texto_ultima_execucao")
    texto = formatar(datetime(2026, 7, 28, 14, 37))  # sem tzinfo, como vem do SQLite
    assert "28/07 11:37" in texto


def test_texto_ultima_execucao_vira_a_data_na_conversao():
    """01:00 UTC do dia 29 é 22:00 BRT do dia 28 — a DATA exibida acompanha."""
    formatar = _carregar_funcao_isolada("_texto_ultima_execucao")
    texto = formatar(datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc))
    assert "28/07 22:00" in texto


# --------------------------------------------------------------------------
# Honestidade do texto: o worker NÃO roda a cada ~15 min
# --------------------------------------------------------------------------

def test_painel_nao_promete_mais_execucao_a_cada_15_minutos():
    """Medição real: o GitHub Actions entrega 4-7 dos 96 disparos/dia pedidos
    pelo cron `*/15`. Prometer "~15 minutos" no painel é mentira — e foi essa
    promessa que escondeu por dias que a automação estava morta."""
    fonte = _fonte()
    assert not re.search(r"~\s*15\s*min", fonte), (
        "Painel.py ainda promete execução a cada ~15 minutos"
    )
    assert "a cada ~15 minutos" not in fonte


def test_painel_explica_o_modelo_catch_up_na_aba():
    fonte = _fonte()
    assert "GitHub Actions" in fonte
    assert "não antes de" in fonte, "falta explicar que o horário é um 'não antes de'"
    assert re.search(r"4 a 7", fonte), "falta o número real de execuções por dia"
