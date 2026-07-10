"""Lê o PDF de Histórico de Posições do Sitrax e extrai posições."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from app.bot.report import Position, parse_dt


EVENTOS = (
    "Posição Automática",
    "Ignição Ligada",
    "Ignição Desligada",
    "Ignicao Ligada",
    "Ignicao Desligada",
)


def extract_pdf_text(path: str | Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def parse_positions_from_pdf_text(text: str) -> list[Position]:
    """
    O PDF do Sitrax cola Vel + DataGPS, ex.:
      Posição Automática Rua X - Paulista (PE)010/07/2026 06:07:23 Sim -7.93 -34.89 10/07/2026 06:07:26
      Posição Automática BR101 - Paulista (PE)3010/07/2026 06:18:28 Sim ...
    """
    # normaliza espaços
    text = text.replace("\r", "\n")
    # junta quebras estranhas no meio da linha
    text = re.sub(r"\n(?![A-ZÁÉÍÓÚ])", " ", text)

    event_alt = "|".join(re.escape(e) for e in EVENTOS)
    # Evento + Local + (vel opcional colada) + DataGPS + Sim/Não
    pattern = re.compile(
        rf"({event_alt})\s+(.+?)"
        rf"(\d{{0,3}})(\d{{2}}/\d{{2}}/\d{{4}}\s+\d{{2}}:\d{{2}}:\d{{2}})\s+"
        rf"(Sim|Não|Nao)\b",
        re.IGNORECASE,
    )

    positions: list[Position] = []
    for m in pattern.finditer(text):
        evento = m.group(1).strip()
        local = m.group(2).strip()
        # remove lixo residual no fim do local
        local = re.sub(r"[\d.\-]+$", "", local).strip()
        data_gps = m.group(4).strip()
        ign = m.group(5).strip()

        # modo a partir do evento + ignição
        modo = evento
        if re.search(r"igni", evento, re.I):
            modo = evento
        elif ign.lower() in ("sim",):
            modo = "Normal"
        else:
            modo = "Estacionado"

        positions.append(
            Position(
                data_gps=parse_dt(data_gps),
                data_sistema=None,
                modo=modo,
                endereco=local,
                referencia="",
                raw={"evento": evento, "ign": ign, "local": local},
            )
        )

    # ordena por tempo
    positions.sort(key=lambda p: p.when or parse_dt("01/01/1970 00:00:00"))
    return positions


def parse_placa_from_pdf_text(text: str) -> Optional[str]:
    m = re.search(r"\b([A-Z]{3}\d[A-Z0-9]\d{2}|[A-Z]{3}\d{4})\b", text)
    return m.group(1) if m else None


def positions_from_pdf(path: str | Path) -> tuple[str, list[Position]]:
    text = extract_pdf_text(path)
    placa = parse_placa_from_pdf_text(text) or "DESCONHECIDA"
    positions = parse_positions_from_pdf_text(text)
    return placa, positions
