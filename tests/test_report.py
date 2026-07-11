"""Testes unitários do gerador de relatório (sem acessar o site)."""

from datetime import datetime

from app.bot.report import (
    Position,
    build_narrative_report,
    extract_city,
    find_all_desligou,
    find_ignition_events,
    is_ignition_on,
    parse_dt,
)


def test_parse_dt():
    assert parse_dt("10/07/2026 06:01:40").hour == 6
    assert parse_dt("10/07/2026 06:01").minute == 1


def test_extract_city():
    assert "Abreu" in extract_city("Avenida D - Abreu e Lima (PE)", "")
    assert extract_city("Rua Presidente Vargas - Paulista...", "389 Metros de PARATIBE") == "Paulista"


def test_narrative():
    positions = [
        Position(
            data_gps=datetime(2026, 7, 10, 6, 1, 40),
            data_sistema=datetime(2026, 7, 10, 6, 2, 7),
            modo="Alerta - Ignição Ligada",
            endereco="Av. D - Abreu e Lima (PE)",
            referencia="1.5Km de ABREU E LIMA",
        ),
        Position(
            data_gps=datetime(2026, 7, 10, 12, 0, 0),
            data_sistema=None,
            modo="Normal",
            endereco="Rua X - Paulista (PE)",
            referencia="",
        ),
        Position(
            data_gps=datetime(2026, 7, 10, 14, 0, 0),
            data_sistema=None,
            modo="Normal",
            endereco="Centro - Olinda (PE)",
            referencia="",
        ),
        Position(
            data_gps=datetime(2026, 7, 10, 16, 0, 0),
            data_sistema=None,
            modo="Normal",
            endereco="Boa Viagem - Recife (PE)",
            referencia="",
        ),
        Position(
            data_gps=datetime(2026, 7, 10, 20, 0, 0),
            data_sistema=None,
            modo="Estacionado",
            endereco="Rua Y - Paulista (PE)",
            referencia="",
        ),
    ]
    text = build_narrative_report("PCE7B03", positions, data_ref="10/07/2026")
    assert "PCE7B03" in text
    assert "Ligou" in text
    assert "Paulista" in text
    assert "Olinda" in text
    assert "Recife" in text
    assert "Desligou" in text
    # Desligou na 1ª transição para Estacionado (20:00), não “colar” em outro
    assert "20:00" in text
    print(text)


def test_is_ignition_desligada_not_ligada():
    assert is_ignition_on("Estacionado") is False
    assert is_ignition_on("Parked") is False
    assert is_ignition_on("Alerta - Ignição Desligada") is False
    assert is_ignition_on("Em Movimento") is True
    assert is_ignition_on("In Motion") is True
    assert is_ignition_on("Alerta - Ignição Ligada") is True
    assert is_ignition_on("Normal") is True


def test_desligou_is_transition_not_last_parked():
    """
    Caso PDY4D85: move 07:29 → estaciona 07:30 → vários parked até o fim.
    Desligou = 07:30 (transição), NÃO o último registro (07:39).
    """
    positions = [
        Position(
            data_gps=datetime(2026, 7, 11, 7, 29, 32),
            data_sistema=None,
            modo="Em Movimento",
            endereco="Paulista (PE)",
            referencia="",
        ),
        Position(
            data_gps=datetime(2026, 7, 11, 7, 29, 37),
            data_sistema=None,
            modo="Alerta - Ignição Digitada",
            endereco="Paulista (PE)",
            referencia="",
        ),
        Position(
            data_gps=datetime(2026, 7, 11, 7, 30, 5),
            data_sistema=None,
            modo="Estacionado",
            endereco="Paulista (PE)",
            referencia="",
        ),
        Position(
            data_gps=datetime(2026, 7, 11, 7, 32, 35),
            data_sistema=None,
            modo="Estacionado",
            endereco="Paulista (PE)",
            referencia="",
        ),
        Position(
            data_gps=datetime(2026, 7, 11, 7, 39, 0),
            data_sistema=None,
            modo="Estacionado",
            endereco="Paulista (PE)",
            referencia="",
        ),
    ]
    ligou, desligou = find_ignition_events(positions)
    assert ligou == datetime(2026, 7, 11, 7, 29, 32)
    assert desligou == datetime(2026, 7, 11, 7, 30, 5)
    assert desligou != datetime(2026, 7, 11, 7, 39, 0)

    text = build_narrative_report("PDY4D85", positions, data_ref="11/07/2026")
    assert "Ligou às 07:29" in text
    assert "Desligou às 07:30" in text
    assert "07:39" not in text.split("Desligou")[-1] or "Desligou às 07:39" not in text
    assert "esteve em" in text


def test_all_desligou_with_city():
    """Vários desligues no dia — lista todos com cidade, mantém esteve."""
    positions = [
        Position(
            data_gps=datetime(2026, 7, 11, 6, 0, 0),
            data_sistema=None,
            modo="Em Movimento",
            endereco="Paulista (PE)",
            referencia="",
        ),
        Position(
            data_gps=datetime(2026, 7, 11, 7, 26, 0),
            data_sistema=None,
            modo="Estacionado",
            endereco="Paulista (PE)",
            referencia="",
        ),
        Position(
            data_gps=datetime(2026, 7, 11, 7, 28, 0),
            data_sistema=None,
            modo="Alerta - Ignição Ligada",
            endereco="Olinda (PE)",
            referencia="",
        ),
        Position(
            data_gps=datetime(2026, 7, 11, 8, 0, 0),
            data_sistema=None,
            modo="Normal",
            endereco="Recife (PE)",
            referencia="",
        ),
        Position(
            data_gps=datetime(2026, 7, 11, 9, 0, 0),
            data_sistema=None,
            modo="Estacionado",
            endereco="Recife (PE)",
            referencia="",
        ),
    ]
    offs = find_all_desligou(positions)
    assert len(offs) == 2
    assert offs[0].when.hour == 7 and offs[0].cidade == "Paulista"
    assert offs[1].when.hour == 9 and offs[1].cidade == "Recife"

    text = build_narrative_report("PDX3G64", positions, data_ref="11/07/2026")
    assert "esteve em" in text
    assert "Desligou (2x" in text
    assert "07:26 em Paulista" in text
    assert "09:00 em Recife" in text
