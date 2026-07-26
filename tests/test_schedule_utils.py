"""Testes de voofacil/schedule_utils.py — parsing de horários, validação de
dias da semana e geração das linhas de cron (UTC) para o GitHub Actions."""
import pytest

from voofacil.schedule_utils import (
    dias_semana_apscheduler,
    gerar_cron_actions,
    parse_horarios,
)


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


class TestGerarCronActions:
    def test_formato_de_saida_e_quantidade_de_linhas(self):
        linhas = gerar_cron_actions([(6, 0), (12, 30)], "*", (5, 0))
        assert len(linhas) == 3  # 2 horários de varredura + 1 explore
        for linha in linhas:
            assert linha.startswith("- cron: '")
            assert linha.endswith("'")

    def test_conversao_brt_para_utc_sem_virada_de_dia(self):
        # 06:00 BRT -> 09:00 UTC ; 12:30 BRT -> 15:30 UTC ; explore 05:00 -> 08:00
        linhas = gerar_cron_actions([(6, 0), (12, 30)], "*", (5, 0))
        assert linhas[0] == "- cron: '0 9 * * *'"
        assert linhas[1] == "- cron: '30 15 * * *'"
        assert linhas[2] == "- cron: '0 8 * * *'"

    def test_virada_de_dia_21h_brt_vira_meia_noite_utc(self):
        # 21:00 BRT -> 00:00 UTC do dia seguinte (o caso citado no contrato)
        linhas = gerar_cron_actions([(21, 0)], "*", (5, 0))
        assert linhas[0] == "- cron: '0 0 * * *'"

    def test_virada_de_dia_desloca_o_dia_da_semana(self):
        # segunda (mon) às 21:00 BRT cai terça (tue) de madrugada em UTC.
        # cron POSIX: dom=0, seg=1, ter=2, ... -> terça = 2
        linhas = gerar_cron_actions([(21, 0)], "mon", (5, 0))
        assert linhas[0] == "- cron: '0 0 * * 2'"
        # o explore, sem virada de dia, permanece segunda (1)
        assert linhas[1] == "- cron: '0 8 * * 1'"

    def test_virada_de_dia_com_multiplos_dias_da_semana(self):
        # sexta/sábado/domingo às 22:00 BRT -> 01:00 UTC do dia seguinte;
        # sex->sab(6), sab->dom(0), dom->seg(1)
        linhas = gerar_cron_actions([(22, 0)], "fri,sat,sun", (5, 0))
        assert linhas[0] == "- cron: '0 1 * * 0,1,6'"

    def test_asterisco_nao_e_afetado_pela_virada_de_dia(self):
        linhas = gerar_cron_actions([(23, 30)], "*", (5, 0))
        assert linhas[0] == "- cron: '30 2 * * *'"

    def test_horario_sem_madrugada_nao_desloca_dia(self):
        # 20:59 BRT ainda não vira o dia em UTC (soma 3h e fica em 23:59 UTC)
        linhas = gerar_cron_actions([(20, 59)], "mon", (5, 0))
        assert linhas[0] == "- cron: '59 23 * * 1'"

    def test_dias_semana_invalido_levanta_value_error(self):
        with pytest.raises(ValueError):
            gerar_cron_actions([(6, 0)], "segunda", (5, 0))
