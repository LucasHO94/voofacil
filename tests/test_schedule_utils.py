"""Testes de voofacil/schedule_utils.py — parsing de horários e validação de
dias da semana (usados como seed do agendamento dinâmico em banco)."""
import pytest

from voofacil.schedule_utils import dias_semana_apscheduler, parse_horarios


class TestParseHorarios:
    def test_csv_valido_padrao(self):
        assert parse_horarios("06:00,12:30,21:00") == [(6, 0), (12, 30), (21, 0)]

    def test_tolera_espacos_em_volta_dos_itens(self):
        assert parse_horarios(" 06:00 , 12:30 ,21:00 ") == [(6, 0), (12, 30), (21, 0)]

    def test_um_horario_soh(self):
        assert parse_horarios("05:00") == [(5, 0)]

    def test_n_horarios(self):
        csv = "00:00,03:15,06:30,09:45,12:00,15:15,18:30,21:45"
        assert parse_horarios(csv) == [
            (0, 0), (3, 15), (6, 30), (9, 45),
            (12, 0), (15, 15), (18, 30), (21, 45),
        ]

    def test_string_vazia_levanta_value_error(self):
        with pytest.raises(ValueError):
            parse_horarios("")

    def test_apenas_espacos_levanta_value_error(self):
        with pytest.raises(ValueError):
            parse_horarios("   ")

    def test_formato_invalido_levanta_value_error(self):
        with pytest.raises(ValueError):
            parse_horarios("6h00")

    def test_hora_fora_do_intervalo_levanta_value_error(self):
        with pytest.raises(ValueError):
            parse_horarios("24:00")

    def test_minuto_fora_do_intervalo_levanta_value_error(self):
        with pytest.raises(ValueError):
            parse_horarios("10:60")


class TestDiasSemanaApscheduler:
    def test_asterisco_significa_todos_os_dias(self):
        assert dias_semana_apscheduler("*") == "*"

    def test_asterisco_com_espacos(self):
        assert dias_semana_apscheduler("  *  ") == "*"

    def test_csv_valido(self):
        assert dias_semana_apscheduler("mon,tue,wed,thu,fri") == "mon,tue,wed,thu,fri"

    def test_case_insensitive_e_tolera_espacos(self):
        assert dias_semana_apscheduler(" MON, Tue ,WED") == "mon,tue,wed"

    def test_um_dia_soh(self):
        assert dias_semana_apscheduler("sat") == "sat"

    def test_dia_invalido_levanta_value_error(self):
        with pytest.raises(ValueError):
            dias_semana_apscheduler("mon,xyz")

    def test_string_vazia_levanta_value_error(self):
        with pytest.raises(ValueError):
            dias_semana_apscheduler("")

    def test_none_levanta_value_error(self):
        with pytest.raises(ValueError):
            dias_semana_apscheduler(None)
