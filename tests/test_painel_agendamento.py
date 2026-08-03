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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from voofacil.alerts.mensagem import fmt_brl
from voofacil.engine.viabilidade import (
    motivo_inviavel_criterios,
    noites_implicitas_do_watch,
)

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


def _carregar_funcao_isolada(nome: str, extra_escopo: dict | None = None):
    """Compila e executa APENAS a função `nome` (extraída da AST do Painel).

    Importar Painel.py rodaria `main()`; extrair o nó da função e compilá-lo
    sozinho dá o comportamento real da função sem nenhum efeito colateral. Só
    serve para funções puras (sem `st.`). `extra_escopo` injeta dependências que
    a função referencia (constantes/helpers do módulo), permitindo isolar até
    funções puras que dependem de outras (ex.: `_criterios_watch` usa `fmt_brl`,
    `MESES_PT` e `_fmt_data`)."""
    modulo = ast.Module(body=[_funcao(nome)], type_ignores=[])
    ast.fix_missing_locations(modulo)
    escopo = {
        "datetime": datetime,
        "timedelta": timedelta,
        "timezone": timezone,
        "date": date,
        "_OFFSET_UTC_PARA_BRT_HORAS": _constante("_OFFSET_UTC_PARA_BRT_HORAS"),
    }
    if extra_escopo:
        escopo.update(extra_escopo)
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


# ==========================================================================
# D8 — o painel parou de MENTIR: montagem honesta do nome do watch
# ==========================================================================

def _nome_watch():
    return _carregar_funcao_isolada("_nome_watch")


def test_nome_watch_nao_repete_o_mesmo_dia_e_inclui_a_volta():
    """O caso REAL do chamado: RIO -> NAP, embarque exato 07/05/2027, volta exata
    22/05/2027. Antes saía '#2 RIO -> NAP 07/05-07/05/2027' (mesmo dia duas vezes,
    sem a volta). Agora o nome tem que refletir o que foi configurado."""
    nome = _nome_watch()(
        "RIO", "NAP", date(2027, 5, 7), date(2027, 5, 7),
        somente_ida=False, volta_ini=date(2027, 5, 22), volta_fim=None,
        tipo_data_ida="EXATA", tipo_data_volta="EXATA",
    )
    assert nome == "RIO -> NAP - 07/05/2027 a 22/05/2027 (ida e volta, datas exatas)"
    # a doença antiga: o mesmo dia impresso duas vezes
    assert "07/05/2027 a 07/05/2027" not in nome
    assert "22/05/2027" in nome, "a data de VOLTA precisa entrar no nome"


def test_nome_watch_mostra_cidade_de_volta_no_open_jaw():
    """Itinerário do print do usuário: chega em Nápoles e volta de Veneza.
    Sem a cidade de retorno o nome afirmaria uma viagem redonda RIO<->NAP,
    que não é a que está sendo monitorada."""
    nome = _nome_watch()(
        "RIO", "NAP", date(2027, 5, 7), date(2027, 5, 7),
        somente_ida=False, volta_ini=date(2027, 5, 22), volta_fim=None,
        tipo_data_ida="EXATA", tipo_data_volta="EXATA", origem_volta="VCE",
    )
    assert nome == "RIO -> NAP (volta VCE) - 07/05/2027 a 22/05/2027 (ida e volta, datas exatas)"
    assert len(nome) <= 80, "a coluna Watch.nome é String(80)"


def test_nome_watch_omite_cidade_de_volta_quando_e_o_proprio_destino():
    nome = _nome_watch()(
        "RIO", "NAP", date(2027, 5, 7), date(2027, 5, 7),
        somente_ida=False, volta_ini=date(2027, 5, 22), volta_fim=None,
        tipo_data_ida="EXATA", tipo_data_volta="EXATA", origem_volta="NAP",
    )
    assert "(volta NAP)" not in nome


def test_nome_watch_janela_de_ida_intervalo():
    nome = _nome_watch()(
        "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30),
        somente_ida=False, tipo_data_ida="INTERVALO", tipo_data_volta="INTERVALO",
    )
    assert "01/09 a 30/09/2026" in nome
    assert "(ida e volta)" in nome


def test_nome_watch_somente_ida():
    nome = _nome_watch()(
        "GRU", "LIS", date(2026, 9, 1), date(2026, 9, 30), somente_ida=True,
        tipo_data_ida="INTERVALO",
    )
    assert "(somente ida)" in nome
    assert "volta" not in nome


def test_nome_watch_trunca_em_80_caracteres():
    nome = _nome_watch()(
        "GRU", "10 destinos", date(2026, 9, 1), date(2026, 9, 30),
        somente_ida=False, tipo_data_ida="INTERVALO",
    )
    assert len(nome) <= 80


# ==========================================================================
# D8 — o painel parou de ESCONDER: critérios e selo de saúde
# ==========================================================================

def _criterios_watch():
    return _carregar_funcao_isolada("_criterios_watch", {
        "fmt_brl": fmt_brl,
        "MESES_PT": _constante("MESES_PT"),
        "_ROTULO_TIPO_DATA_CURTO": _constante("_ROTULO_TIPO_DATA_CURTO"),
        "_fmt_data": _carregar_funcao_isolada("_fmt_data"),
        "noites_implicitas_do_watch": noites_implicitas_do_watch,
    })


def _watch_falso(**kw):
    base = dict(
        data_ini=date(2027, 5, 7), data_fim=date(2027, 5, 7),
        tipo_data_ida="EXATA", tipo_data_volta="EXATA", ida_volta=True,
        data_volta_ini=date(2027, 5, 22), data_volta_fim=None,
        duracao_min=None, duracao_max=None, mes_especifico=None,
        preco_limite=Decimal("3500"), preco_minimo=None,
        preco_aproximado=Decimal("3000"), flexibilidade_dias=None,
        destino_iata="NAP", origem_volta_iata=None, passageiros=1,
    )
    base.update(kw)
    # `open_jaw` e `total_passageiros` são properties do ORM que o
    # SimpleNamespace não deriva sozinho — reproduzidas aqui com a mesma regra
    # de voofacil/models.py.
    base.setdefault(
        "open_jaw",
        bool(base["origem_volta_iata"]) and base["origem_volta_iata"] != base["destino_iata"],
    )
    base.setdefault("total_passageiros", base["passageiros"] or 1)
    return SimpleNamespace(**base)


def test_criterios_watch_mostra_tudo_com_travessao_quando_vazio():
    itens = dict(_criterios_watch()(_watch_falso()))
    assert itens["Preço-alvo (aproximado)"] == "R$ 3.000,00"
    assert itens["Preço máximo (limite)"] == "R$ 3.500,00"
    assert itens["Preço mínimo (piso)"] == "—"        # não definido => travessão
    assert itens["Duração da estadia"] == "—"
    assert itens["Mês específico"] == "—"
    # a volta (o dado que o painel escondia) aparece
    assert "22/05/2027" in itens["Janela de desembarque"]


def test_criterios_watch_mostra_a_duracao_implicita_pelas_datas():
    """O expander listava "Janela de desembarque: 22/05/2027" e "Duração da
    estadia: ..." como dois fatos independentes e igualmente calmos, tendo na
    mesma tela tudo o que era preciso para gritar a contradição do watch #4."""
    itens = dict(_criterios_watch()(_watch_falso(duracao_min=14, duracao_max=14)))

    assert itens["Duração da estadia"] == "14 noites (exata)"
    assert itens["Duração implícita pelas datas"].startswith("15 noites")


def test_criterios_watch_denuncia_tolerancia_zero_em_data_aproximada():
    """A aba dizia "aproximada" + "Flexibilidade: —", o que fazia o usuário
    acreditar que havia folga; na prática a janela vira um ponto."""
    itens = dict(_criterios_watch()(
        _watch_falso(tipo_data_ida="APROXIMADA", flexibilidade_dias=None)
    ))
    assert "EXATA" in itens["Flexibilidade (dias)"]


def test_criterios_watch_somente_ida_omite_campos_de_volta():
    itens = dict(_criterios_watch()(_watch_falso(ida_volta=False)))
    assert itens["Somente ida?"].startswith("sim")
    assert "Janela de desembarque" not in itens
    assert "Duração da estadia" not in itens


def _selo_saude():
    return _carregar_funcao_isolada("_selo_saude")


def test_selo_saude_verde_quando_coleta_nas_ultimas_24h():
    nivel, texto = _selo_saude()(5, 20)
    assert nivel == "verde"
    assert "24h" in texto


def test_selo_saude_amarelo_quando_so_ha_dados_na_semana():
    nivel, _ = _selo_saude()(0, 3)
    assert nivel == "amarelo"


def test_selo_saude_vermelho_denuncia_coleta_zerada():
    """O item de maior valor: zero por dias vira o alarme que teria denunciado
    sozinho a coleta zerada e o agendamento morto."""
    nivel, texto = _selo_saude()(0, 0)
    assert nivel == "vermelho"
    assert "não está coletando nada" in texto


def test_selo_saude_explica_criterios_impossiveis_em_vez_de_contar_snapshots():
    """"0 snapshots em 7 dias" é a MESMA frase para quatro causas com quatro
    correções diferentes. Quando a causa é conhecida, ela tem precedência — e
    aparece no primeiro segundo de vida do watch, não depois de 7 dias."""
    nivel, texto = _selo_saude()(0, 0, "As datas implicam exatamente 15 noites, mas...")
    assert nivel == "vermelho"
    assert "NUNCA vai coletar" in texto
    assert "15 noites" in texto


def test_selo_saude_com_motivo_ignora_ate_coleta_recente():
    nivel, _ = _selo_saude()(5, 20, "critérios contraditórios")
    assert nivel == "vermelho"


# ==========================================================================
# D13 — validação de entrada: watches não nascem mais mortos em silêncio
# ==========================================================================

def _validar_criterios():
    return _carregar_funcao_isolada("_validar_criterios", {
        "fmt_brl": fmt_brl,
        "motivo_inviavel_criterios": motivo_inviavel_criterios,
    })


def test_validar_criterios_recusa_data_fim_antes_da_ini():
    erro = _validar_criterios()(
        data_ini=date(2027, 9, 30), data_fim=date(2027, 9, 1), somente_ida=True,
    )
    assert erro and "antes" in erro


def test_validar_criterios_recusa_duracao_min_maior_que_max():
    erro = _validar_criterios()(
        data_ini=date(2027, 9, 1), data_fim=date(2027, 9, 30), somente_ida=False,
        duracao_min=20, duracao_max=7,
    )
    assert erro and "duração" in erro.lower()


def test_validar_criterios_recusa_piso_acima_do_teto():
    erro = _validar_criterios()(
        data_ini=date(2027, 9, 1), data_fim=date(2027, 9, 30), somente_ida=True,
        preco_minimo=Decimal("4000"), preco_limite=Decimal("3500"),
    )
    assert erro and "mínimo" in erro.lower()


def test_validar_criterios_recusa_destino_que_nao_e_iata():
    erro = _validar_criterios()(
        data_ini=date(2027, 9, 1), data_fim=date(2027, 9, 30), somente_ida=True,
        destinos_custom=["LIS", "MADRI"],   # "Lisboa, MADRI" digitado por nome
    )
    assert erro and "IATA" in erro


def test_validar_criterios_aceita_configuracao_valida():
    """CORRIGIDO: este teste afirmava que 07/05/2027 -> 22/05/2027 com duração
    7-14 noites era "configuração válida". Não é — são 15 noites, e o teto de
    14 torna o watch impossível. Era o teste que CONGELAVA o defeito: os 332
    casos verdes defendiam ativamente o bug do watch #4, e qualquer correção
    correta de `_validar_criterios` teria que quebrá-lo.

    A duração agora contém as 15 noites que as datas implicam, e a combinação
    passa a ser de fato coletável (é o watch irmão #5, que cotou R$ 5.410).
    """
    assert _validar_criterios()(
        data_ini=date(2027, 5, 7), data_fim=date(2027, 5, 7), somente_ida=False,
        data_volta_ini=date(2027, 5, 22), data_volta_fim=None,
        duracao_min=14, duracao_max=16,
        preco_minimo=Decimal("1500"), preco_limite=Decimal("3500"),
        destinos_custom=["LIS", "MAD"],
        tipo_data_ida="EXATA", tipo_data_volta="EXATA",
    ) is None


def test_validar_criterios_aceita_sem_restricao_de_duracao():
    """Sem duração fixada, o mesmo par de datas exatas é perfeitamente válido —
    a duração fica determinada pelas próprias datas."""
    assert _validar_criterios()(
        data_ini=date(2027, 5, 7), data_fim=date(2027, 5, 7), somente_ida=False,
        data_volta_ini=date(2027, 5, 22), data_volta_fim=None,
        tipo_data_ida="EXATA", tipo_data_volta="EXATA",
    ) is None


# --- o caso REAL do watch #4 (medido em produção: 0 snapshots desde sempre) ---

def test_validar_criterios_recusa_o_watch_4_citando_as_15_noites():
    """RIO -> NAP, embarque EXATO 07/05/2027, desembarque EXATO 22/05/2027,
    duração 14-14. São 15 noites; o painel aceitava e dizia "Monitoramento
    criado!". A recusa precisa DIZER o número que as datas implicam, senão o
    usuário não tem como saber qual dos três campos corrigir."""
    erro = _validar_criterios()(
        data_ini=date(2027, 5, 7), data_fim=date(2027, 5, 7), somente_ida=False,
        data_volta_ini=date(2027, 5, 22), data_volta_fim=None,
        duracao_min=14, duracao_max=14,
        tipo_data_ida="EXATA", tipo_data_volta="EXATA",
    )
    assert erro
    assert "15 noites" in erro
    assert "14 noites" in erro


def test_validar_criterios_aceita_o_watch_4_quando_a_ida_e_aproximada():
    """A contraparte que impede o falso positivo: EXATA e APROXIMADA gravam
    AMBAS data_ini == data_fim == <data alvo> (a tolerância mora em
    flexibilidade_dias). Com ±3 dias de folga na ida, 14 noites É alcançável
    (embarcando em 08/05) — recusar aqui seria trocar falso negativo por falso
    positivo e passar a barrar watches bons."""
    assert _validar_criterios()(
        data_ini=date(2027, 5, 7), data_fim=date(2027, 5, 7), somente_ida=False,
        data_volta_ini=date(2027, 5, 22), data_volta_fim=None,
        duracao_min=14, duracao_max=14,
        tipo_data_ida="APROXIMADA", tipo_data_volta="EXATA", flexibilidade_dias=3,
    ) is None


def test_validar_criterios_recusa_janela_de_volta_antes_da_de_ida():
    erro = _validar_criterios()(
        data_ini=date(2027, 5, 7), data_fim=date(2027, 5, 20), somente_ida=False,
        data_volta_ini=date(2027, 4, 1), data_volta_fim=date(2027, 4, 20),
    )
    assert erro and "antes" in erro


def test_validar_criterios_recusa_janela_de_volta_em_watch_somente_ida():
    """A busca sai (e é paga) em modo só-ida, a cotação volta sem retorno e o
    filtro de desembarque descarta 100% do resultado."""
    erro = _validar_criterios()(
        data_ini=date(2027, 5, 7), data_fim=date(2027, 5, 20), somente_ida=True,
        data_volta_ini=date(2027, 5, 22),
    )
    assert erro and "SOMENTE IDA" in erro


def test_validar_criterios_recusa_mes_fora_da_janela_de_embarque():
    """`mes_especifico` não chegava nem a ser parâmetro desta função: a chamada
    era feita, COBRADA, e depois 100% do resultado era descartado."""
    erro = _validar_criterios()(
        data_ini=date(2027, 5, 1), data_fim=date(2027, 5, 30), somente_ida=True,
        mes_especifico=8,
    )
    assert erro and "agosto" in erro


# ==========================================================================
# D8/D13 — a UI está PLUGADA nesses helpers (não basta existirem soltos)
# ==========================================================================

def test_aba_watches_usa_selo_de_saude_e_criterios():
    aba = _funcao("aba_watches")
    chamadas = {
        n.func.id for n in ast.walk(aba)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_selo_saude" in chamadas
    assert "_contagem_snapshots_recentes" in chamadas
    assert "_criterios_watch" in chamadas
    assert "_form_editar_watch" in chamadas
    assert "Critérios deste monitoramento" in _fonte()


def test_aba_watches_explica_o_watch_ja_gravado_que_e_impossivel():
    """Validar só a ENTRADA não conserta o #4: ele já está no banco e continua
    coletando zero. A aba precisa recalcular o veredito e mostrar a razão."""
    aba = _funcao("aba_watches")
    chamadas = {
        n.func.id for n in ast.walk(aba)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "motivo_inviavel" in chamadas, "a aba precisa dizer POR QUE o watch não coleta"
    assert "avisos_do_watch" in chamadas, "faltam os avisos de coleta reduzida"
    assert "critérios impossíveis" in _fonte()
    assert "Clique em ✏️ para corrigir" in _fonte(), "a razão precisa vir com o caminho da correção"


def test_formulario_deriva_o_default_de_duracao_das_datas_escolhidas():
    """CAUSA RAIZ da criação do #4: os defaults do bloco "Duração da estadia"
    eram os literais 7 e 7-14, nunca derivados das datas logo acima. Com ida
    exata 07/05 e volta exata 22/05, das 180 durações que o widget aceita só 15
    sobrevive — e os DOIS defaults do formulário estavam entre as 179 letais."""
    aba = _funcao("aba_watches")
    chamadas = {
        n.func.id for n in ast.walk(aba)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "noites_implicitas" in chamadas

    fonte = _fonte()
    assert 'st.number_input("Noites", min_value=1, max_value=180, value=7' not in fonte
    assert '"Máximo (noites)", min_value=1, max_value=180, value=14' not in fonte


def test_aba_watches_valida_e_protege_a_sessao_na_criacao():
    aba = _funcao("aba_watches")
    chamadas = {
        n.func.id for n in ast.walk(aba)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_validar_criterios" in chamadas, "criação precisa validar antes de gravar (D13)"
    assert "_nome_watch" in chamadas, "criação precisa montar o nome honesto (D8)"
    assert "_escrita_protegida" in chamadas, "escrita precisa proteger a Session do painel"


def test_form_editar_watch_valida_protege_e_chama_atualizar():
    edit = _funcao("_form_editar_watch")
    chamadas = {
        n.func.id for n in ast.walk(edit)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_validar_criterios" in chamadas, "edição também precisa validar (D13)"
    assert "_escrita_protegida" in chamadas
    atributos = {n.attr for n in ast.walk(edit) if isinstance(n, ast.Attribute)}
    # atualizar_watch é passado por nome ao wrapper _escrita_protegida
    assert "atualizar_watch" in {
        n.id for n in ast.walk(edit) if isinstance(n, ast.Name)
    }


def test_grafico_historico_usa_preco_aproximado():
    """D7: o preço-alvo deixou de ser decorativo — vira linha no gráfico."""
    grafico = _funcao("_grafico_historico")
    atributos = {n.attr for n in ast.walk(grafico) if isinstance(n, ast.Attribute)}
    assert "preco_aproximado" in atributos
