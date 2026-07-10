"""Lê o PDF de Histórico de Posições do Sitrax e extrai posições (PT + EN)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from app.bot.report import Position, parse_dt

logger = logging.getLogger(__name__)

# Eventos em português e inglês (UI do Sitrax muda por idioma)
EVENTOS = (
    "Posição Automática",
    "Posicao Automatica",
    "Automatic Position",
    "Position Automatic",
    "Ignição Ligada",
    "Ignicao Ligada",
    "Ignition ON",
    "Ignition On",
    "Alert - Ignition ON",
    "Alert - Ignition On",
    "Ignição Desligada",
    "Ignicao Desligada",
    "Ignition OFF",
    "Ignition Off",
    "Alert - Ignition OFF",
    "Alert - Ignition Off",
    "Parked",
    "Estacionado",
    "In Motion",
    "Normal",
)


def extract_pdf_text(path: str | Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def _modo_from_event(evento: str, ign: str = "") -> str:
    ev = (evento or "").lower()
    ig = (ign or "").lower()
    if re.search(r"deslig|off", ev):
        return "Estacionado"
    if re.search(r"ligada|ignition\s*on|\bon\b", ev) and "off" not in ev:
        return "Ignição Ligada"
    if re.search(r"parked|estacion", ev):
        return "Estacionado"
    if re.search(r"motion|movimento", ev):
        return "Normal"
    if ig in ("sim", "yes", "y", "true"):
        return "Normal"
    if ig in ("não", "nao", "no", "n", "false"):
        return "Estacionado"
    if "normal" in ev:
        return "Normal"
    return evento or "Normal"


def _parse_event_pattern(text: str) -> list[Position]:
    """
    Formato clássico Sitrax (texto colado):
      Posição Automática Rua X - Paulista (PE)010/07/2026 06:07:23 Sim ...
      Automatic Position BR101 - Paulista (PE) 10/07/2026 06:18:28 Yes ...
    """
    event_alt = "|".join(re.escape(e) for e in EVENTOS)
    pattern = re.compile(
        rf"({event_alt})\s+(.+?)"
        rf"(\d{{0,3}})(\d{{2}}/\d{{2}}/\d{{4}}\s+\d{{2}}:\d{{2}}:\d{{2}})\s+"
        rf"(Sim|Não|Nao|Yes|No)\b",
        re.IGNORECASE,
    )
    positions: list[Position] = []
    for m in pattern.finditer(text):
        evento = m.group(1).strip()
        local = m.group(2).strip()
        local = re.sub(r"[\d.\-]+$", "", local).strip()
        data_gps = m.group(4).strip()
        ign = m.group(5).strip()
        positions.append(
            Position(
                data_gps=parse_dt(data_gps),
                data_sistema=None,
                modo=_modo_from_event(evento, ign),
                endereco=local,
                referencia="",
                raw={"evento": evento, "ign": ign, "local": local, "via": "event"},
            )
        )
    return positions


def _parse_city_date_pattern(text: str) -> list[Position]:
    """
    Fallback: cidade (UF) + data/hora, comum em PDF EN/PT mal extraído.
      ... Paulista (PE) 10/07/2026 06:07:23 ...
      ... Recife (PE)10/07/2026 06:07:23 ...
    """
    pattern = re.compile(
        r"([A-Za-zÀ-ú][A-Za-zÀ-ú\s\.\-]{2,60}?)\s*\(([A-Z]{2})\)\s*"
        r"(\d{0,3})?(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})",
        re.UNICODE,
    )
    positions: list[Position] = []
    seen: set[str] = set()
    for m in pattern.finditer(text):
        cidade = m.group(1).strip(" -–,\t")
        # limpa lixo à esquerda (trecho de evento/endereço longo)
        cidade = re.split(r"[\n\r]", cidade)[-1].strip()
        # pega últimos 2–4 palavras se veio endereço inteiro
        parts = [p for p in re.split(r"\s+", cidade) if p]
        if len(parts) > 5:
            cidade = " ".join(parts[-4:])
        uf = m.group(2)
        data_gps = m.group(4).strip()
        key = f"{data_gps}|{cidade}"
        if key in seen:
            continue
        seen.add(key)
        # contexto para modo
        start = max(0, m.start() - 40)
        ctx = text[start : m.start()].lower()
        modo = "Normal"
        if re.search(r"parked|estacion|deslig|off", ctx):
            modo = "Estacionado"
        elif re.search(r"motion|movimento|ligada|ignition\s*on", ctx):
            modo = "Normal"
        local = f"{cidade} ({uf})"
        positions.append(
            Position(
                data_gps=parse_dt(data_gps),
                data_sistema=None,
                modo=modo,
                endereco=local,
                referencia="",
                raw={"via": "city_date", "cidade": cidade, "uf": uf},
            )
        )
    return positions


def _parse_date_only_rows(text: str) -> list[Position]:
    """
    Último recurso: lista de datas GPS (tabela em texto).
    Usa a data como âncora; endereço fica genérico se não houver cidade.
    """
    # duas datas seguidas (GPS + System) como na tela
    pattern = re.compile(
        r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})\s+"
        r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})?"
        r"\s*(Parked|Normal|In Motion|Estacionado|Movimento|"
        r"Igni[cç][aã]o\s+Ligada|Igni[cç][aã]o\s+Desligada|"
        r"Ignition\s+ON|Ignition\s+OFF)?",
        re.IGNORECASE,
    )
    positions: list[Position] = []
    seen: set[str] = set()
    for m in pattern.finditer(text):
        data_gps = m.group(1).strip()
        if data_gps in seen:
            continue
        seen.add(data_gps)
        modo = (m.group(3) or "Normal").strip()
        # tenta cidade no trecho seguinte
        tail = text[m.end() : m.end() + 120]
        city_m = re.search(
            r"([A-Za-zÀ-ú][A-Za-zÀ-ú\s]{2,40})\s*\(([A-Z]{2})\)",
            tail,
        )
        if city_m:
            endereco = f"{city_m.group(1).strip()} ({city_m.group(2)})"
        else:
            # trecho antes
            head = text[max(0, m.start() - 80) : m.start()]
            city_m2 = re.search(
                r"([A-Za-zÀ-ú][A-Za-zÀ-ú\s]{2,40})\s*\(([A-Z]{2})\)\s*$",
                head,
            )
            if city_m2:
                endereco = f"{city_m2.group(1).strip()} ({city_m2.group(2)})"
            else:
                endereco = "Local desconhecido"
        positions.append(
            Position(
                data_gps=parse_dt(data_gps),
                data_sistema=parse_dt((m.group(2) or "").strip()) if m.group(2) else None,
                modo=_modo_from_event(modo),
                endereco=endereco,
                referencia="",
                raw={"via": "date_row"},
            )
        )
    return positions


def parse_positions_from_pdf_text(text: str) -> list[Position]:
    text = text.replace("\r", "\n")
    # junta quebras no meio da linha (exceto se parece novo registro)
    text = re.sub(r"\n(?![A-ZÁÉÍÓÚ])", " ", text)

    strategies = (
        ("event", _parse_event_pattern),
        ("city_date", _parse_city_date_pattern),
        ("date_row", _parse_date_only_rows),
    )
    # prioridade: event > city_date > date_row; merge + dedupe por horário
    by_time: dict = {}
    priority = {"event": 3, "city_date": 2, "date_row": 1}
    for name, fn in strategies:
        try:
            found = fn(text)
        except Exception as e:
            logger.warning("Parser %s falhou: %s", name, e)
            found = []
        logger.info("PDF parser %s: %s posições", name, len(found))
        for p in found:
            if not p.when:
                continue
            key = p.when.strftime("%Y%m%d%H%M%S")
            prev = by_time.get(key)
            if prev is None or priority[name] > prev[0]:
                by_time[key] = (priority[name], p)
            elif priority[name] == prev[0]:
                if "desconhecido" in (prev[1].endereco or "").lower() and p.endereco:
                    by_time[key] = (priority[name], p)

    epoch = parse_dt("01/01/1970 00:00:00")
    best = [pair[1] for pair in by_time.values()]
    best.sort(key=lambda p: p.when or epoch)  # type: ignore[arg-type, return-value]
    if not best:
        sample = re.sub(r"\s+", " ", text)[:400]
        logger.warning("PDF sem posições parseadas. Amostra: %s", sample)
    return best


def parse_placa_from_pdf_text(text: str) -> Optional[str]:
    m = re.search(r"\b([A-Z]{3}\d[A-Z0-9]\d{2}|[A-Z]{3}\d{4})\b", text)
    return m.group(1) if m else None


def positions_from_pdf(path: str | Path) -> tuple[str, list[Position]]:
    text = extract_pdf_text(path)
    logger.info(
        "PDF texto extraído: %s chars de %s",
        len(text),
        Path(path).name,
    )
    placa = parse_placa_from_pdf_text(text) or "DESCONHECIDA"
    positions = parse_positions_from_pdf_text(text)
    return placa, positions
