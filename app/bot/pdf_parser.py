"""Lê o PDF de Histórico de Posições do Sitrax e extrai posições (PT + EN)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from app.bot.report import Position, parse_dt

logger = logging.getLogger(__name__)

# Eventos em português e inglês
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
    "Alert - Ignition OFF",
    "Alert - Ignition Off",
    "Ignição Desligada",
    "Ignicao Desligada",
    "Ignition OFF",
    "Ignition Off",
    "Parked",
    "Estacionado",
    "In Motion",
    "Normal",
)

# Cidades conhecidas (PE / região) — usadas quando o PDF não traz "(UF)" limpo
KNOWN_CITIES = (
    "Jaboatão dos Guararapes",
    "Jaboatao dos Guararapes",
    "São Lourenço da Mata",
    "Sao Lourenco da Mata",
    "Cabo de Santo Agostinho",
    "Abreu e Lima",
    "Vitória de Santo Antão",
    "Vitoria de Santo Antao",
    "Camaragibe",
    "Igarassu",
    "Itamaracá",
    "Itamaraca",
    "Araçoiaba",
    "Aracoiaba",
    "Paulista",
    "Recife",
    "Olinda",
    "Moreno",
    "Ipojuca",
    "Goiana",
    "Paudalho",
    "Caruaru",
    "Petrolina",
    "Jaboatão",
    "Jaboatao",
    "Escada",
    "Gravatá",
    "Gravata",
    "Surubim",
    "Limoeiro",
    "Nazaré da Mata",
    "Nazare da Mata",
    "Timbaúba",
    "Timbauba",
    "Carpina",
    "São Lourenço",
)

DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}")
CITY_UF_RE = re.compile(
    r"([A-Za-zÀ-ú][A-Za-zÀ-ú\s\.\-]{1,50}?)\s*\(([A-Z]{2})\)",
    re.UNICODE,
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
    if re.search(r"ligada|ignition\s*on", ev) and "off" not in ev:
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


def _normalize_city_name(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip())
    # remove prefixos de evento colados
    n = re.sub(
        r"^(Automatic\s+Position|Posi[cç][aã]o\s+Autom[aá]tica)\s+",
        "",
        n,
        flags=re.I,
    ).strip()
    fixes = {
        "jaboatao dos guararapes": "Jaboatão dos Guararapes",
        "jaboatão dos guararapes": "Jaboatão dos Guararapes",
        "jaboatao": "Jaboatão dos Guararapes",
        "jaboatão": "Jaboatão dos Guararapes",
        "sao lourenco da mata": "São Lourenço da Mata",
        "são lourenço da mata": "São Lourenço da Mata",
        "vitoria de santo antao": "Vitória de Santo Antão",
        "vitória de santo antão": "Vitória de Santo Antão",
        "abreu e lima": "Abreu e Lima",
        "itamaraca": "Itamaracá",
        "aracoiaba": "Araçoiaba",
        "gravata": "Gravatá",
        "nazare da mata": "Nazaré da Mata",
        "timbauba": "Timbaúba",
        "paulista": "Paulista",
        "recife": "Recife",
        "olinda": "Olinda",
        "moreno": "Moreno",
    }
    low = n.lower()
    if low in fixes:
        return fixes[low]
    # title case mas preserva "e", "da", "de"
    parts = n.split(" ")
    small = {"e", "da", "de", "do", "das", "dos"}
    out = []
    for i, p in enumerate(parts):
        if i > 0 and p.lower() in small:
            out.append(p.lower())
        else:
            out.append(p[:1].upper() + p[1:].lower() if p else p)
    return " ".join(out)


def _find_city_in_window(window: str) -> Optional[str]:
    """Procura Cidade (UF) ou nome conhecido no trecho ao redor da data."""
    if not window:
        return None
    # 1) padrão Cidade (UF) — pega o último (costuma ser o mais próximo da data)
    matches = list(CITY_UF_RE.finditer(window))
    if matches:
        m = matches[-1]
        cidade = m.group(1).strip(" -–,\t|")
        # se veio endereço longo, pega as últimas palavras antes do (UF)
        parts = [p for p in re.split(r"\s+", cidade) if p]
        if len(parts) > 5:
            cidade = " ".join(parts[-4:])
        # remove lixo de evento no início
        cidade = re.sub(
            r"^(Automatic\s+Position|Posi[cç][aã]o\s+Autom[aá]tica|"
            r"Ignition\s+O[NF]{1,2}|Igni[cç][aã]o\s+\w+|Parked|Normal|In\s+Motion|"
            r"Alert\s*-\s*Ignition\s+O[NF]{1,2})\s+",
            "",
            cidade,
            flags=re.I,
        ).strip(" -–")
        # se ainda sobrou lixo + cidade conhecida no final
        known = _find_city_in_window_names_only(cidade)
        if known:
            cidade = known
        uf = m.group(2)
        if len(cidade) >= 2:
            return f"{_normalize_city_name(cidade)} ({uf})"

    # 2) nomes conhecidos (mais longos primeiro)
    return _find_city_in_window_names_only(window)


def _find_city_in_window_names_only(window: str) -> Optional[str]:
    if not window:
        return None
    lower = window.lower()
    for city in sorted(KNOWN_CITIES, key=len, reverse=True):
        if city.lower() in lower:
            return _normalize_city_name(city)
    return None


def _find_mode_in_window(window: str) -> str:
    w = (window or "").lower()
    if re.search(r"parked|estacionado|ignition\s*off|deslig", w):
        return "Estacionado"
    if re.search(r"ignition\s*on|igni[cç][aã]o\s+ligada", w):
        return "Ignição Ligada"
    if re.search(r"in\s*motion|movimento", w):
        return "Normal"
    if "normal" in w:
        return "Normal"
    return "Normal"


def _parse_event_pattern(text: str) -> list[Position]:
    """Formato clássico: Evento + local + data + Sim/Yes."""
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
        # reforça cidade no local
        city = _find_city_in_window(local) or local
        positions.append(
            Position(
                data_gps=parse_dt(data_gps),
                data_sistema=None,
                modo=_modo_from_event(evento, ign),
                endereco=city if city else local,
                referencia="",
                raw={"evento": evento, "ign": ign, "local": local, "via": "event"},
            )
        )
    return positions


def _parse_gps_anchors(text: str) -> list[Position]:
    """
    Estratégia principal para PDF EN/tabela:
    - acha todas as datas
    - agrupa GPS + System (par consecutivo = 1 ponto, não 2)
    - busca cidade num raio de ~180 chars ao redor
    """
    flat = text.replace("\r", "\n")
    # mantém quebras leves para contexto, mas unifica espaços múltiplos
    flat = re.sub(r"[ \t]+", " ", flat)

    dates = list(DATE_RE.finditer(flat))
    if not dates:
        return []

    positions: list[Position] = []
    i = 0
    while i < len(dates):
        m = dates[i]
        data_gps = m.group(0)
        data_sis = None
        # par GPS + System (datas bem próximas)
        if i + 1 < len(dates):
            nxt = dates[i + 1]
            gap = nxt.start() - m.end()
            if 0 <= gap <= 8:
                data_sis = nxt.group(0)
                i += 2
            else:
                i += 1
        else:
            i += 1

        # janela de contexto: antes e depois da data GPS
        left = max(0, m.start() - 160)
        right = min(len(flat), m.end() + 200)
        window = flat[left:right]

        endereco = _find_city_in_window(window) or ""
        if not endereco:
            # tenta só o trecho ANTES da data (endereço costuma vir antes)
            endereco = _find_city_in_window(flat[left : m.start()]) or ""
        if not endereco:
            endereco = "Local desconhecido"

        modo = _find_mode_in_window(window)

        positions.append(
            Position(
                data_gps=parse_dt(data_gps),
                data_sistema=parse_dt(data_sis) if data_sis else None,
                modo=modo,
                endereco=endereco,
                referencia="",
                raw={
                    "via": "gps_anchor",
                    "window_sample": window[:80],
                },
            )
        )

    return positions


def _parse_city_date_pattern(text: str) -> list[Position]:
    """Cidade (UF) colada/logo antes da data."""
    pattern = re.compile(
        r"([A-Za-zÀ-ú][A-Za-zÀ-ú\s\.\-]{2,60}?)\s*\(([A-Z]{2})\)\s*"
        r"(\d{0,3})?(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})",
        re.UNICODE,
    )
    positions: list[Position] = []
    seen: set[str] = set()
    for m in pattern.finditer(text):
        cidade = m.group(1).strip(" -–,\t")
        cidade = re.split(r"[\n\r]", cidade)[-1].strip()
        parts = [p for p in re.split(r"\s+", cidade) if p]
        if len(parts) > 5:
            cidade = " ".join(parts[-4:])
        uf = m.group(2)
        data_gps = m.group(4).strip()
        key = data_gps
        if key in seen:
            continue
        seen.add(key)
        start = max(0, m.start() - 40)
        ctx = text[start : m.start()]
        positions.append(
            Position(
                data_gps=parse_dt(data_gps),
                data_sistema=None,
                modo=_find_mode_in_window(ctx),
                endereco=f"{_normalize_city_name(cidade)} ({uf})",
                referencia="",
                raw={"via": "city_date", "cidade": cidade, "uf": uf},
            )
        )
    return positions


def _score_position(p: Position, strategy_priority: int) -> tuple:
    """Maior = melhor. Prefere endereço com cidade real."""
    end = (p.endereco or "").lower()
    has_city = 1
    if not end or "desconhecido" in end:
        has_city = 0
    elif re.search(r"\([a-z]{2}\)", end) or any(
        c.lower() in end for c in KNOWN_CITIES
    ):
        has_city = 2
    return (has_city, strategy_priority)


def parse_positions_from_pdf_text(text: str) -> list[Position]:
    raw = text.replace("\r", "\n")
    # versão "achatada" para alguns regex
    flat = re.sub(r"\n(?![A-ZÁÉÍÓÚ])", " ", raw)

    strategies = (
        ("event", 4, lambda: _parse_event_pattern(flat)),
        ("city_date", 3, lambda: _parse_city_date_pattern(flat)),
        ("gps_anchor", 2, lambda: _parse_gps_anchors(raw)),
        ("gps_anchor_flat", 1, lambda: _parse_gps_anchors(flat)),
    )

    by_time: dict = {}
    for name, prio, fn in strategies:
        try:
            found = fn()
        except Exception as e:
            logger.warning("Parser %s falhou: %s", name, e)
            found = []
        with_city = sum(
            1
            for p in found
            if p.endereco and "desconhecido" not in p.endereco.lower()
        )
        logger.info(
            "PDF parser %s: %s posições (%s com cidade)",
            name,
            len(found),
            with_city,
        )
        for p in found:
            if not p.when:
                continue
            key = p.when.strftime("%Y%m%d%H%M%S")
            score = _score_position(p, prio)
            prev = by_time.get(key)
            if prev is None or score > prev[0]:
                by_time[key] = (score, p)

    epoch = parse_dt("01/01/1970 00:00:00")
    best = [pair[1] for pair in by_time.values()]
    best.sort(key=lambda p: p.when or epoch)  # type: ignore[arg-type, return-value]

    n_city = sum(
        1 for p in best if p.endereco and "desconhecido" not in p.endereco.lower()
    )
    logger.info(
        "PDF parser final: %s pontos, %s com cidade (%.0f%%)",
        len(best),
        n_city,
        (100.0 * n_city / len(best)) if best else 0,
    )
    if not best:
        sample = re.sub(r"\s+", " ", raw)[:500]
        logger.warning("PDF sem posições. Amostra: %s", sample)
    elif n_city == 0 and best:
        sample = re.sub(r"\s+", " ", raw)[:600]
        logger.warning(
            "PDF com datas mas 0 cidades. Amostra texto: %s", sample
        )
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
    # amostra no log para calibrar parser em produção
    if text:
        logger.info("PDF amostra: %s", re.sub(r"\s+", " ", text)[:350])
    placa = parse_placa_from_pdf_text(text) or "DESCONHECIDA"
    positions = parse_positions_from_pdf_text(text)
    return placa, positions
