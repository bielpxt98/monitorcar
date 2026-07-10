"""Testes unitários do gerador de relatório (sem acessar o site)."""

from datetime import datetime

from app.bot.report import (
    Position,
    build_narrative_report,
    extract_city,
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
    print(text)
