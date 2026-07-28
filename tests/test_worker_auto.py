"""Testes de voofacil.worker.job_auto() — o job único do poll (GitHub
Actions/daemon local) que decide via banco (deve_rodar_agora) se dispara
job_varredura()/job_explore() agora.

Usa fakes/mocks (monkeypatch) para as peças que job_auto() coordena
(SessionLocal, garantir_horarios_padrao, deve_rodar_agora, job_varredura,
job_explore) — nada de rede real nem banco de verdade."""
from datetime import datetime, timezone

import voofacil.worker as worker_mod
from voofacil.models import TipoHorario


class _SessaoFake:
    """Dublê de SessionLocal(): só precisa suportar `with ... as session`.
    O valor de `session` não importa aqui porque garantir_horarios_padrao e
    deve_rodar_agora também são substituídos por fakes nestes testes."""

    def __enter__(self):
        return "sessao-fake"

    def __exit__(self, *exc):
        return False


def _instalar_fakes(monkeypatch, *, deve_rodar):
    """Configura os fakes comuns aos testes de job_auto.

    `deve_rodar` é uma função (tipo: str) -> bool que simula deve_rodar_agora
    (o valor de `agora_utc` recebido é ignorado, propositalmente simples)."""
    chamadas: dict[str, list] = {"garantir": 0, "deve_rodar_agora": [], "varredura": 0, "explore": 0}

    monkeypatch.setattr(worker_mod, "SessionLocal", lambda: _SessaoFake())

    def fake_garantir(session):
        chamadas["garantir"] += 1

    def fake_deve_rodar_agora(session, tipo, agora_utc=None):
        chamadas["deve_rodar_agora"].append(tipo)
        return deve_rodar(tipo)

    monkeypatch.setattr(worker_mod, "garantir_horarios_padrao", fake_garantir)
    monkeypatch.setattr(worker_mod, "deve_rodar_agora", fake_deve_rodar_agora)
    monkeypatch.setattr(worker_mod, "job_varredura", lambda: chamadas.update(varredura=chamadas["varredura"] + 1))
    monkeypatch.setattr(worker_mod, "job_explore", lambda: chamadas.update(explore=chamadas["explore"] + 1))

    return chamadas


def test_job_auto_dispara_varredura_quando_deve_rodar_varredura(monkeypatch):
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    chamadas = _instalar_fakes(monkeypatch, deve_rodar=lambda tipo: tipo == TipoHorario.VARREDURA.value)

    worker_mod.job_auto()

    assert chamadas["garantir"] == 1
    assert set(chamadas["deve_rodar_agora"]) == {TipoHorario.VARREDURA.value, TipoHorario.EXPLORE.value}
    assert chamadas["varredura"] == 1
    assert chamadas["explore"] == 0


def test_job_auto_dispara_explore_quando_deve_rodar_explore(monkeypatch):
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    chamadas = _instalar_fakes(monkeypatch, deve_rodar=lambda tipo: tipo == TipoHorario.EXPLORE.value)

    worker_mod.job_auto()

    assert chamadas["varredura"] == 0
    assert chamadas["explore"] == 1


def test_job_auto_dispara_os_dois_quando_ambos_batem(monkeypatch):
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    chamadas = _instalar_fakes(monkeypatch, deve_rodar=lambda tipo: True)

    worker_mod.job_auto()

    assert chamadas["varredura"] == 1
    assert chamadas["explore"] == 1


def test_job_auto_nao_dispara_nada_quando_nenhum_bate(monkeypatch, caplog):
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "*")
    chamadas = _instalar_fakes(monkeypatch, deve_rodar=lambda tipo: False)

    with caplog.at_level("INFO", logger="voofacil.worker"):
        worker_mod.job_auto()

    assert chamadas["varredura"] == 0
    assert chamadas["explore"] == 0
    # garantir_horarios_padrao/deve_rodar_agora ainda rodam (são baratos,
    # só leitura/seed) — o que não pode rolar é job_varredura/job_explore.
    assert chamadas["garantir"] == 1
    assert "nada agendado para agora" in caplog.text


def test_job_auto_respeita_scan_dias_semana_dia_nao_permitido(monkeypatch):
    """Se hoje (BRT) não é um dia permitido por scan_dias_semana, job_auto
    nem chama deve_rodar_agora (nem garantir_horarios_padrao) — sai direto."""
    # 2026-07-27 é uma segunda-feira (mon) em UTC e também em BRT (a diferença
    # de 3h não muda o dia da semana para este horário do meio-dia UTC).
    agora_utc = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)  # segunda (mon) em BRT
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "tue")  # só terça permitida

    chamadas = _instalar_fakes(monkeypatch, deve_rodar=lambda tipo: True)

    worker_mod.job_auto(agora_utc)

    assert chamadas["garantir"] == 0
    assert chamadas["deve_rodar_agora"] == []
    assert chamadas["varredura"] == 0
    assert chamadas["explore"] == 0


def test_job_auto_respeita_scan_dias_semana_dia_permitido(monkeypatch):
    """Mesmo cenário, mas hoje É um dia permitido — segue o fluxo normal."""
    agora_utc = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)  # segunda (mon) em BRT
    monkeypatch.setattr(worker_mod.settings, "scan_dias_semana", "mon")

    chamadas = _instalar_fakes(monkeypatch, deve_rodar=lambda tipo: tipo == TipoHorario.VARREDURA.value)

    worker_mod.job_auto(agora_utc)

    assert chamadas["garantir"] == 1
    assert chamadas["varredura"] == 1
    assert chamadas["explore"] == 0


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
