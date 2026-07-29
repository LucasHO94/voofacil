"""Testes de voofacil.worker.job_auto() — o job único do poll (GitHub
Actions/daemon local) que decide via banco se dispara job_varredura()/
job_explore() agora.

Modelo CATCH-UP (ver docstring de voofacil/worker.py): job_auto pega a LISTA
de horários devidos de cada tipo (horarios_devidos), roda o job UMA vez se a
lista não estiver vazia e só DEPOIS carimba aquela lista (marcar_executados).
A ordem "roda, depois marca" é o que dá semântica at-least-once — marcar antes
perderia o horário pelo resto do dia se o processo morresse no meio — e por
isso ela é verificada explicitamente aqui, com uma lista de eventos.

Usa fakes/monkeypatch para as peças que job_auto() coordena (SessionLocal,
garantir_horarios_padrao, horarios_devidos, listar_horarios, marcar_executados,
job_varredura, job_explore) — nada de rede real nem banco de verdade."""
from datetime import datetime, timezone

import voofacil.worker as worker_mod
from voofacil.models import TipoHorario

AGORA = datetime(2026, 7, 27, 14, 37, tzinfo=timezone.utc)  # segunda-feira (mon) em BRT


class _HorarioFake:
    """Dublê de HorarioAgendado — job_auto só lê id/hora/minuto/ultima_execucao."""

    def __init__(self, id_: int, hora: int, minuto: int, ultima_execucao=None):
        self.id = id_
        self.hora = hora
        self.minuto = minuto
        self.ultima_execucao = ultima_execucao


class _SessaoFake:
    """Dublê de SessionLocal(): suporta `with ... as session` e o
    `session.get(HorarioAgendado, id)` que _marcar_executados_por_id usa para
    recarregar os horários numa sessão nova."""

    def __init__(self, por_id: dict[int, _HorarioFake]):
        self._por_id = por_id
        self.fechada = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fechada = True
        return False

    def get(self, modelo, ident):
        return self._por_id.get(ident)


def _instalar_fakes(monkeypatch, *, devidos: dict[str, list[_HorarioFake]],
                    ativos: dict[str, list[_HorarioFake]] | None = None,
                    existentes: dict[int, _HorarioFake] | None = None,
                    sucesso_varredura: bool = True,
                    sucesso_explore: bool = True):
    """Instala os fakes comuns e devolve o registro de tudo que aconteceu.

    `devidos`: o que horarios_devidos() responde por tipo.
    `ativos`: o que listar_horarios() responde por tipo (só usado no log do
        caminho "nada devido"); default = os próprios devidos.
    `existentes`: mapa id -> horário que a sessão de marcação enxerga; default =
        todos os devidos (o caso normal). Passar um mapa menor simula um horário
        excluído no painel entre a leitura e a marcação.
    `sucesso_varredura`/`sucesso_explore`: o bool que o job devolve. job_auto só
        carimba o horário quando o job CONCLUIU; passar False simula rodada
        abortada (API fora do ar) e deve deixar o horário devido.

    O registro traz `eventos`, uma lista em ORDEM de tudo que job_auto fez de
    efeito colateral — é ela que prova que a marcação vem depois do job."""
    ativos = devidos if ativos is None else ativos
    if existentes is None:
        existentes = {h.id: h for lista in devidos.values() for h in lista}

    registro: dict = {
        "eventos": [],
        "garantir": 0,
        "consultas": [],      # tipos passados a horarios_devidos, em ordem
        "marcados": [],       # listas de ids passadas a marcar_executados
        "agora_marcado": [],  # agora_utc recebido por marcar_executados
        "sessoes": [],
    }

    def fake_session_local():
        sessao = _SessaoFake(existentes)
        registro["sessoes"].append(sessao)
        return sessao

    def fake_garantir(session):
        registro["garantir"] += 1

    def fake_horarios_devidos(session, tipo, agora_utc=None):
        registro["consultas"].append(tipo)
        return list(devidos.get(tipo, []))

    def fake_listar_horarios(session, tipo, **kwargs):
        return list(ativos.get(tipo, []))

    def fake_marcar_executados(session, horarios, agora_utc=None):
        ids = [h.id for h in horarios]
        registro["marcados"].append(ids)
        registro["agora_marcado"].append(agora_utc)
        registro["eventos"].append(f"marcar:{ids}")

    monkeypatch.setattr(worker_mod, "SessionLocal", fake_session_local)
    monkeypatch.setattr(worker_mod, "garantir_horarios_padrao", fake_garantir)
    monkeypatch.setattr(worker_mod, "horarios_devidos", fake_horarios_devidos)
    monkeypatch.setattr(worker_mod, "listar_horarios", fake_listar_horarios)
    monkeypatch.setattr(worker_mod, "marcar_executados", fake_marcar_executados)
    def fake_job_varredura():
        registro["eventos"].append("varredura")
        return sucesso_varredura

    def fake_job_explore():
        registro["eventos"].append("explore")
        return sucesso_explore

    monkeypatch.setattr(worker_mod, "job_varredura", fake_job_varredura)
    monkeypatch.setattr(worker_mod, "job_explore", fake_job_explore)
    return registro


# ---------- nada devido ----------

def test_job_auto_nao_roda_nada_quando_nada_esta_devido(monkeypatch, caplog):
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    registro = _instalar_fakes(
        monkeypatch,
        devidos={},
        ativos={
            TipoHorario.VARREDURA.value: [
                _HorarioFake(1, 6, 0, datetime(2026, 7, 27, 11, 37, tzinfo=timezone.utc)),
                _HorarioFake(2, 12, 30, datetime(2026, 7, 27, 16, 2, tzinfo=timezone.utc)),
            ],
            TipoHorario.EXPLORE.value: [_HorarioFake(3, 5, 0, None)],
        },
    )

    with caplog.at_level("INFO", logger="voofacil.worker"):
        worker_mod.job_auto(AGORA)

    assert registro["eventos"] == []          # nem job, nem marcação
    assert registro["marcados"] == []
    # garantir_horarios_padrao/horarios_devidos ainda rodam (baratos, só leitura/seed).
    assert registro["garantir"] == 1
    assert registro["consultas"] == [TipoHorario.VARREDURA.value, TipoHorario.EXPLORE.value]


def test_job_auto_loga_diagnostico_util_quando_nada_esta_devido(monkeypatch, caplog):
    """O log do caminho ocioso precisa dizer QUAIS horários existem e quando
    cada um rodou — a automação já ficou 4 dias morta logando só "nada
    agendado para agora", sem nenhum dado para investigar."""
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    _instalar_fakes(
        monkeypatch,
        devidos={},
        ativos={
            TipoHorario.VARREDURA.value: [
                _HorarioFake(1, 6, 0, datetime(2026, 7, 27, 11, 37, tzinfo=timezone.utc)),
            ],
            TipoHorario.EXPLORE.value: [_HorarioFake(3, 5, 0, None)],
        },
    )

    with caplog.at_level("INFO", logger="voofacil.worker"):
        worker_mod.job_auto(AGORA)

    texto = caplog.text
    assert "Nada devido agora" in texto
    assert "06:00" in texto and "2026-07-27 11:37" in texto   # última execução do horário
    assert "05:00" in texto and "nunca executou" in texto      # explore virgem
    assert "2026-07-27 14:37" in texto                         # o "agora" usado na decisão


def test_resumo_horarios_sem_nenhum_ativo():
    assert worker_mod._resumo_horarios([]) == "nenhum horário ativo"


# ---------- varredura / explore devidos ----------

def test_job_auto_varredura_devida_roda_uma_vez_e_marca(monkeypatch):
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    h = _HorarioFake(7, 6, 0)
    registro = _instalar_fakes(monkeypatch, devidos={TipoHorario.VARREDURA.value: [h]})

    worker_mod.job_auto(AGORA)

    assert registro["eventos"] == ["varredura", "marcar:[7]"]
    assert registro["marcados"] == [[7]]
    assert registro["agora_marcado"] == [AGORA]


def test_job_auto_explore_devido_roda_uma_vez_e_marca(monkeypatch):
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    h = _HorarioFake(9, 5, 0)
    registro = _instalar_fakes(monkeypatch, devidos={TipoHorario.EXPLORE.value: [h]})

    worker_mod.job_auto(AGORA)

    assert registro["eventos"] == ["explore", "marcar:[9]"]
    assert registro["marcados"] == [[9]]


def test_job_auto_ambos_devidos_rodam_os_dois_cada_um_com_a_sua_marcacao(monkeypatch):
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    registro = _instalar_fakes(monkeypatch, devidos={
        TipoHorario.VARREDURA.value: [_HorarioFake(1, 6, 0)],
        TipoHorario.EXPLORE.value: [_HorarioFake(2, 5, 0)],
    })

    worker_mod.job_auto(AGORA)

    assert registro["eventos"] == ["varredura", "marcar:[1]", "explore", "marcar:[2]"]
    # A varredura NÃO carimba o horário do explore (e vice-versa) — as listas
    # são independentes por tipo.
    assert registro["marcados"] == [[1], [2]]


def test_job_auto_marca_somente_depois_de_rodar_o_job(monkeypatch):
    """Ordem é o ponto: marcar ANTES daria at-most-once (processo morto no meio
    da varredura => horário perdido pelo resto do dia)."""
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    registro = _instalar_fakes(monkeypatch, devidos={
        TipoHorario.VARREDURA.value: [_HorarioFake(1, 6, 0)],
        TipoHorario.EXPLORE.value: [_HorarioFake(2, 5, 0)],
    })

    worker_mod.job_auto(AGORA)

    eventos = registro["eventos"]
    assert eventos.index("varredura") < eventos.index("marcar:[1]")
    assert eventos.index("explore") < eventos.index("marcar:[2]")


def test_job_auto_varios_horarios_devidos_rodam_uma_unica_varredura(monkeypatch):
    """Três horários atrasados (poll ficou 3h fora do ar) => UMA varredura só,
    porque uma varredura já cobre todos eles — mas os três são carimbados."""
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    registro = _instalar_fakes(monkeypatch, devidos={
        TipoHorario.VARREDURA.value: [
            _HorarioFake(1, 6, 0), _HorarioFake(2, 12, 30), _HorarioFake(3, 21, 0),
        ],
    })

    worker_mod.job_auto(AGORA)

    assert registro["eventos"].count("varredura") == 1
    assert registro["marcados"] == [[1, 2, 3]]


def test_job_auto_marca_em_sessao_nova_recarregando_por_id(monkeypatch):
    """Os HorarioAgendado são lidos numa sessão que fecha antes do job rodar;
    a marcação abre outra sessão e recarrega por id (evita
    DetachedInstanceError)."""
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    registro = _instalar_fakes(monkeypatch, devidos={TipoHorario.VARREDURA.value: [_HorarioFake(1, 6, 0)]})

    worker_mod.job_auto(AGORA)

    # 1 sessão de leitura/seed + 1 sessão de marcação, ambas fechadas no fim.
    assert len(registro["sessoes"]) == 2
    assert all(s.fechada for s in registro["sessoes"])


def test_job_auto_ignora_horario_excluido_entre_a_leitura_e_a_marcacao(monkeypatch):
    """Se o usuário apagar o horário no painel no meio da varredura, o
    session.get devolve None e ele apenas some da marcação — não estoura."""
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    vivo = _HorarioFake(1, 6, 0)
    registro = _instalar_fakes(
        monkeypatch,
        devidos={TipoHorario.VARREDURA.value: [vivo, _HorarioFake(2, 12, 30)]},
        existentes={1: vivo},  # o id 2 sumiu do banco
    )

    worker_mod.job_auto(AGORA)

    assert registro["eventos"] == ["varredura", "marcar:[1]"]


def test_job_auto_usa_relogio_real_quando_agora_utc_none(monkeypatch):
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    registro = _instalar_fakes(monkeypatch, devidos={TipoHorario.VARREDURA.value: [_HorarioFake(1, 6, 0)]})

    worker_mod.job_auto()

    assert registro["eventos"] == ["varredura", "marcar:[1]"]
    (agora,) = registro["agora_marcado"]
    assert agora is not None and agora.tzinfo is not None  # tz-aware, veio de now(timezone.utc)


# ---------- corte por dia da semana (inalterado pelo catch-up) ----------

def test_job_auto_respeita_scan_dias_semana_dia_nao_permitido(monkeypatch):
    """Se hoje (data BRT) não é um dia permitido por scan_dias_semana, job_auto
    nem consulta o banco (nem semeia) — sai direto."""
    # 2026-07-27 12:00 UTC é uma segunda-feira (mon) também em BRT (09:00).
    agora_utc = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "tue")  # só terça permitida

    registro = _instalar_fakes(monkeypatch, devidos={
        TipoHorario.VARREDURA.value: [_HorarioFake(1, 6, 0)],
        TipoHorario.EXPLORE.value: [_HorarioFake(2, 5, 0)],
    })

    worker_mod.job_auto(agora_utc)

    assert registro["garantir"] == 0
    assert registro["consultas"] == []
    assert registro["eventos"] == []   # nada rodou e nada foi carimbado


def test_job_auto_respeita_scan_dias_semana_dia_permitido(monkeypatch):
    """Mesmo cenário, mas hoje É um dia permitido — segue o fluxo normal."""
    agora_utc = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)  # segunda (mon) em BRT
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "mon")

    registro = _instalar_fakes(monkeypatch, devidos={TipoHorario.VARREDURA.value: [_HorarioFake(1, 6, 0)]})

    worker_mod.job_auto(agora_utc)

    assert registro["garantir"] == 1
    assert registro["eventos"] == ["varredura", "marcar:[1]"]


def test_dia_permitido_hoje_asterisco_sempre_true(monkeypatch):
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    assert worker_mod._dia_permitido_hoje() is True


def test_dia_permitido_hoje_virada_meia_noite_usa_dia_brt_nao_utc():
    """01:30 UTC de uma terça-feira é 22:30 BRT de SEGUNDA (dia anterior) —
    _dia_permitido_hoje deve considerar o dia BRT, não o dia UTC."""
    agora_utc = datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc)  # terça UTC, segunda BRT
    with_mon_only = _fake_settings_dias("mon")
    with with_mon_only:
        assert worker_mod._dia_permitido_hoje(agora_utc) is True
    with_tue_only = _fake_settings_dias("tue")
    with with_tue_only:
        assert worker_mod._dia_permitido_hoje(agora_utc) is False


class _fake_settings_dias:
    """Context manager simples para trocar settings.scan_dias_semana sem
    depender do fixture `monkeypatch` (usado fora de uma função de teste com
    parâmetro monkeypatch, por conveniência neste teste específico)."""

    def __init__(self, valor):
        self.valor = valor
        self._original = None

    def __enter__(self):
        self._original = worker_mod.settings.scan_dias_semana
        worker_mod.settings.scan_dias_semana = self.valor
        return self

    def __exit__(self, *exc):
        worker_mod.settings.scan_dias_semana = self._original
        return False


# ---------- rodada abortada NÃO consome o horário ----------

def test_job_auto_nao_marca_quando_a_varredura_aborta(monkeypatch):
    """job_varredura engole toda exceção internamente e devolve False. Se
    job_auto carimbasse mesmo assim, uma API fora do ar consumiria a ocorrência
    do dia e o horário só voltaria a ser tentado no dia seguinte."""
    agora_utc = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    registro = _instalar_fakes(
        monkeypatch,
        devidos={TipoHorario.VARREDURA.value: [_HorarioFake(1, 6, 0)]},
        sucesso_varredura=False,
    )

    worker_mod.job_auto(agora_utc)

    assert registro["eventos"] == ["varredura"]   # rodou...
    assert registro["marcados"] == []             # ...mas NÃO carimbou


def test_job_auto_nao_marca_quando_o_explore_aborta(monkeypatch):
    agora_utc = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    registro = _instalar_fakes(
        monkeypatch,
        devidos={TipoHorario.EXPLORE.value: [_HorarioFake(4, 5, 0)]},
        sucesso_explore=False,
    )

    worker_mod.job_auto(agora_utc)

    assert registro["eventos"] == ["explore"]
    assert registro["marcados"] == []


def test_job_auto_varredura_abortada_nao_impede_explore_de_marcar(monkeypatch):
    """Os dois tipos são independentes: a falha de um não contamina o outro."""
    agora_utc = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    registro = _instalar_fakes(
        monkeypatch,
        devidos={
            TipoHorario.VARREDURA.value: [_HorarioFake(1, 6, 0)],
            TipoHorario.EXPLORE.value: [_HorarioFake(4, 5, 0)],
        },
        sucesso_varredura=False,
    )

    worker_mod.job_auto(agora_utc)

    assert registro["eventos"] == ["varredura", "explore", "marcar:[4]"]
    assert registro["marcados"] == [[4]]
