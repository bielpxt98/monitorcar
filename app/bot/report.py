"""Gera relatório narrativo a partir do histórico de posições."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass
class Position:
    data_gps: Optional[datetime]
    data_sistema: Optional[datetime]
    modo: str
    endereco: str
    referencia: str
    temperatura: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def when(self) -> Optional[datetime]:
        return self.data_gps or self.data_sistema

    @property
    def cidade(self) -> str:
        return extract_city(self.endereco, self.referencia)


@dataclass
class Segment:
    cidade: str
    inicio: datetime
    fim: datetime
    modos: list[str] = field(default_factory=list)

    def label(self) -> str:
        return format_range(self.inicio, self.fim)


# OFF/ON checados por regex (ordem importa: "desligada" contém "ligada")
_IGNITION_OFF_RE = re.compile(
    r"estacionado|parked|parado|ignition\s*off|"
    r"igni[cç][aã]o\s+desligad|desligada|desligado",
    re.I,
)
_IGNITION_ON_RE = re.compile(
    r"em\s+movimento|in\s+motion|(?<![a-z])movimento(?![a-z])|"
    r"igni[cç][aã]o\s+ligada|ignition\s*on|"
    r"(?<![a-z])normal(?![a-z])",
    re.I,
)
# Mode EN do Sitrax na frota: "Parked" / "In Motion" / "Normal"


def parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    for fmt in (
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def extract_city(endereco: str, referencia: str = "") -> str:
    """Tenta extrair cidade do endereço/referência do Sitrax."""
    text = f"{endereco} {referencia}".strip()
    if not text or re.search(r"local\s+desconhecido", text, re.I):
        return "Local desconhecido"

    # "Cidade (UF)" no fim ou no meio
    m = re.search(
        r"[-–,]?\s*([A-Za-zÀ-ú][A-Za-zÀ-ú\s]{1,40}?)\s*\(([A-Z]{2})\)\s*$",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip().title()

    m = re.search(
        r"\b([A-Za-zÀ-ú][A-Za-zÀ-ú\s]{1,40}?)\s*\(([A-Z]{2})\)",
        text,
    )
    if m:
        return m.group(1).strip().title()

    m = re.search(r"\bde\s+([A-ZÁÉÍÓÚÃÕÂÊÔÇ][A-Za-zÀ-ú\s]{2,40})\b", referencia)
    if m:
        return m.group(1).strip().title()

    # Cidades da Grande Recife / PE (mais longas primeiro)
    cities = [
        "Jaboatão dos Guararapes",
        "Jaboatao dos Guararapes",
        "São Lourenço da Mata",
        "Sao Lourenco da Mata",
        "Cabo de Santo Agostinho",
        "Vitória de Santo Antão",
        "Vitoria de Santo Antao",
        "Abreu e Lima",
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
        "Carpina",
        "Limoeiro",
    ]
    lower = text.lower()
    for city in cities:
        if city.lower() in lower:
            return (
                city.replace("Jaboatao dos Guararapes", "Jaboatão dos Guararapes")
                .replace("Jaboatao", "Jaboatão dos Guararapes")
                .replace("Jaboatão", "Jaboatão dos Guararapes")
                if "jaboat" in city.lower() and "guararapes" not in city.lower()
                else city.replace("Jaboatao dos Guararapes", "Jaboatão dos Guararapes")
                .replace("Sao Lourenco da Mata", "São Lourenço da Mata")
                .replace("Vitoria de Santo Antao", "Vitória de Santo Antão")
                .replace("Itamaraca", "Itamaracá")
                .replace("Aracoiaba", "Araçoiaba")
            )

    # Último segmento do endereço (após hífen)
    parts = re.split(r"[-–]", endereco)
    if parts:
        last = parts[-1].strip()
        last = re.sub(r"\s*\([A-Z]{2}\)\s*", "", last).strip()
        last = re.sub(r",\s*[A-Z]{2}\s*$", "", last).strip()
        if 2 < len(last) < 40 and not re.search(r"\d{2}/\d{2}", last):
            return last.title()

    return "Local desconhecido"


def is_ignition_on(modo: str) -> Optional[bool]:
    """
    True  = ignição ligada / em movimento
    False = estacionado / ignição desligada
    None  = modo desconhecido (ignora na transição)
    """
    m = (modo or "").strip()
    if not m:
        return None
    # OFF antes de ON — "desligada" contém a substring "ligada"
    if _IGNITION_OFF_RE.search(m):
        return False
    if _IGNITION_ON_RE.search(m):
        return True
    # Alerta de ignição ligada (sem deslig)
    ml = m.lower()
    if "alerta" in ml and "igni" in ml and "deslig" not in ml:
        return True
    return None


def format_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def format_range(inicio: datetime, fim: datetime) -> str:
    if inicio.date() == fim.date():
        return f"de {format_time(inicio)} às {format_time(fim)}"
    return (
        f"de {inicio.strftime('%d/%m %H:%M')} às {fim.strftime('%d/%m %H:%M')}"
    )


def _normalize_city(name: str) -> str:
    name = re.sub(r"\s+", " ", name.strip())
    # title case mas preserva "e", "da", "de", "do"
    parts = name.split(" ")
    small = {"e", "da", "de", "do", "das", "dos"}
    out = []
    for i, p in enumerate(parts):
        if i > 0 and p.lower() in small:
            out.append(p.lower())
        else:
            out.append(p[:1].upper() + p[1:].lower() if p else p)
    return " ".join(out)


def _is_unknown_city(name: str) -> bool:
    n = (name or "").strip().lower()
    return (not n) or n in ("local desconhecido", "desconhecido", "unknown")


def _same_city(a: str, b: str) -> bool:
    """Considera Jaboatão ≈ Jaboatão dos Guararapes, etc."""
    a = _normalize_city(a)
    b = _normalize_city(b)
    if a == b:
        return True
    # NÃO tratar "Local desconhecido" como igual a Paulista/Recife —
    # isso colava o segmento inteiro em "Local Desconhecido".
    if _is_unknown_city(a) or _is_unknown_city(b):
        return False
    # abreviações / variantes
    al, bl = a.lower(), b.lower()
    if al in bl or bl in al:
        return True
    aliases = {
        "jaboatao": "jaboatão dos guararapes",
        "jaboatão": "jaboatão dos guararapes",
    }
    al2 = aliases.get(al, al)
    bl2 = aliases.get(bl, bl)
    return al2 == bl2


def build_segments(
    positions: list[Position],
    min_minutes: int = 2,
) -> list[Segment]:
    """Agrupa posições consecutivas na mesma cidade (ignora blips curtos)."""
    ordered = sorted(
        [p for p in positions if p.when],
        key=lambda p: p.when,  # type: ignore[arg-type, return-value]
    )
    if not ordered:
        return []

    segments: list[Segment] = []
    current_city = _normalize_city(ordered[0].cidade)
    start = ordered[0].when
    end = ordered[0].when
    modos = [ordered[0].modo]

    for pos in ordered[1:]:
        city = _normalize_city(pos.cidade)
        # se atual é desconhecido e o novo tem cidade → assume mesma estadia, troca o nome
        if _is_unknown_city(current_city) and not _is_unknown_city(city):
            current_city = city
            end = pos.when
            if pos.modo and pos.modo not in modos:
                modos.append(pos.modo)
            continue
        # se novo é desconhecido e atual tem cidade → mantém cidade conhecida
        if _is_unknown_city(city) and not _is_unknown_city(current_city):
            end = pos.when
            if pos.modo and pos.modo not in modos:
                modos.append(pos.modo)
            continue
        if _same_city(city, current_city):
            end = pos.when
            if pos.modo and pos.modo not in modos:
                modos.append(pos.modo)
        else:
            segments.append(
                Segment(cidade=current_city, inicio=start, fim=end, modos=modos)
            )
            current_city = city
            start = pos.when
            end = pos.when
            modos = [pos.modo]

    segments.append(Segment(cidade=current_city, inicio=start, fim=end, modos=modos))

    # Mescla trechos muito curtos com o vizinho (ruído de GPS na fronteira)
    if len(segments) <= 1:
        return segments

    merged: list[Segment] = [segments[0]]
    for seg in segments[1:]:
        prev = merged[-1]
        dur = (seg.fim - seg.inicio).total_seconds() / 60.0
        if dur < min_minutes and _same_city(prev.cidade, seg.cidade) is False:
            # blip curto: estende o anterior até o fim do blip e descarta cidade do blip
            # se o próximo for igual ao prev, some tudo
            prev.fim = seg.fim
            continue
        if _same_city(prev.cidade, seg.cidade):
            prev.fim = seg.fim
            for m in seg.modos:
                if m not in prev.modos:
                    prev.modos.append(m)
        else:
            merged.append(seg)

    # segunda passagem: funde consecutivos iguais após limpeza
    final: list[Segment] = []
    for seg in merged:
        if final and _same_city(final[-1].cidade, seg.cidade):
            final[-1].fim = seg.fim
        else:
            # normaliza nome longo
            cidade = seg.cidade
            if "jaboat" in cidade.lower():
                cidade = "Jaboatão dos Guararapes"
            final.append(
                Segment(
                    cidade=cidade,
                    inicio=seg.inicio,
                    fim=seg.fim,
                    modos=seg.modos,
                )
            )
    return final


@dataclass
class DesligouEvent:
    """Um desligue real (transição ON → OFF) com a cidade naquele momento."""

    when: datetime
    cidade: str


def city_at_time(positions: list[Position], when: datetime) -> str:
    """Cidade do ponto GPS na hora do evento (ou o mais próximo anterior)."""
    ordered = sorted(
        [p for p in positions if p.when],
        key=lambda p: p.when,  # type: ignore[arg-type, return-value]
    )
    best = "local desconhecido"
    for pos in ordered:
        if pos.when is None:
            continue
        if pos.when <= when:
            c = _normalize_city(pos.cidade)
            if c and c.lower() != "local desconhecido":
                best = c
            elif best == "local desconhecido":
                best = c or best
        else:
            break
    if "jaboat" in best.lower():
        best = "Jaboatão dos Guararapes"
    return best


def find_all_desligou(positions: list[Position]) -> list[DesligouEvent]:
    """
    Todos os desligues do dia: cada transição ON → OFF.
    Cidade = onde estava no ponto do desligue (não a última cidade do dia).
    """
    ordered = sorted(
        [p for p in positions if p.when],
        key=lambda p: p.when,  # type: ignore[arg-type, return-value]
    )
    events: list[DesligouEvent] = []
    prev_on: Optional[bool] = None

    for pos in ordered:
        state = is_ignition_on(pos.modo)
        if state is None:
            continue
        if state is True:
            prev_on = True
        elif state is False:
            if prev_on is True and pos.when is not None:
                cidade = _normalize_city(pos.cidade)
                if "jaboat" in cidade.lower():
                    cidade = "Jaboatão dos Guararapes"
                if not cidade or cidade.lower() == "local desconhecido":
                    cidade = city_at_time(ordered, pos.when)
                events.append(DesligouEvent(when=pos.when, cidade=cidade))
            prev_on = False
    return events


def find_ignition_events(
    positions: list[Position],
) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    Compat: primeiro ligou + último desligue (lista completa em find_all_desligou).
    """
    ordered = sorted(
        [p for p in positions if p.when],
        key=lambda p: p.when,  # type: ignore[arg-type, return-value]
    )
    ligou: Optional[datetime] = None
    prev_on: Optional[bool] = None
    desligou: Optional[datetime] = None

    for pos in ordered:
        state = is_ignition_on(pos.modo)
        if state is None:
            continue
        if state is True:
            if ligou is None:
                ligou = pos.when
            prev_on = True
        elif state is False:
            if prev_on is True:
                desligou = pos.when
            prev_on = False

    return ligou, desligou


def group_positions_by_day(
    positions: list[Position],
) -> list[tuple[date, list[Position]]]:
    """Agrupa posições por dia civil (data GPS/sistema). Ordem cronológica."""
    buckets: dict[date, list[Position]] = {}
    for p in positions:
        if not p.when:
            continue
        d = p.when.date()
        buckets.setdefault(d, []).append(p)
    out: list[tuple[date, list[Position]]] = []
    for d in sorted(buckets.keys()):
        day_pos = sorted(buckets[d], key=lambda x: x.when)  # type: ignore[arg-type, return-value]
        out.append((d, day_pos))
    return out


def _narrative_for_day(ordered: list[Position]) -> list[str]:
    """Corpo do relatório de UM dia (já ordenado)."""
    lines: list[str] = []
    if not ordered:
        lines.append("ℹ️ Sem posições GPS neste dia.")
        return lines

    ligou, _ = find_ignition_events(ordered)
    desligues = find_all_desligou(ordered)
    segments = build_segments(ordered)

    if ligou:
        lines.append(f"🔑 Ligou às {format_time(ligou)}")
    else:
        first = ordered[0].when
        if first:
            lines.append(
                f"🔑 Primeiro registro às {format_time(first)} "
                f"(modo: {ordered[0].modo or 'n/d'})"
            )

    lines.append("")
    lines.append("📍 Resumo por cidade / horário:")
    for seg in segments:
        lines.append(
            f"   • {format_range(seg.inicio, seg.fim)} esteve em {seg.cidade}"
        )

    lines.append("")
    if desligues:
        if len(desligues) == 1:
            d = desligues[0]
            lines.append(
                f"🔒 Desligou às {format_time(d.when)} em {d.cidade}"
            )
        else:
            lines.append(f"🔒 Desligou ({len(desligues)}x):")
            for d in desligues:
                lines.append(
                    f"   • {format_time(d.when)} em {d.cidade}"
                )
    elif ordered:
        last = ordered[-1]
        if last.when:
            lines.append(
                f"ℹ️ Último registro: {format_time(last.when)} — "
                f"{last.cidade} ({last.modo or 'n/d'})"
            )

    lines.append(f"Pontos GPS do dia: {len(ordered)}")
    return lines


def build_narrative_report(
    placa: str,
    positions: list[Position],
    data_ref: str = "",
    cliente: str = "",
) -> str:
    """
    Exemplo (1 dia):
    - Ligou às 06:01
    - de 06:01 às 12:00 esteve em Abreu e Lima
    - Desligou às 18:00 em Paulista

    Multi-dia: um bloco por data (10/07, 11/07, …).
    """
    lines: list[str] = []
    header = f"📋 Relatório — placa {placa.upper()}"
    if cliente:
        header += f" ({cliente})"
    if data_ref:
        header += f"\n📅 Período: {data_ref}"
    lines.append(header)
    lines.append("")

    day_groups = group_positions_by_day(positions)
    if not day_groups:
        lines.append("ℹ️ Sem posições GPS no período (Sitrax: 0 registros).")
        lines.append("")
        lines.append("Total de pontos GPS: 0")
        return "\n".join(lines)

    total = 0
    for i, (day, day_pos) in enumerate(day_groups):
        total += len(day_pos)
        if len(day_groups) > 1:
            lines.append("─" * 36)
            lines.append(f"📆 Data: {day.strftime('%d/%m/%Y')}")
            lines.append("─" * 36)
        elif not data_ref:
            lines.append(f"📅 Data: {day.strftime('%d/%m/%Y')}")
            lines.append("")
        lines.extend(_narrative_for_day(day_pos))
        if i < len(day_groups) - 1:
            lines.append("")

    if len(day_groups) > 1:
        lines.append("")
        lines.append(
            f"Total de pontos GPS (todos os dias): {total} "
            f"· {len(day_groups)} dia(s)"
        )
    else:
        lines.append("")
        lines.append(f"Total de pontos GPS: {total}")
    return "\n".join(lines)


def positions_from_rows(rows: list[dict]) -> list[Position]:
    """Converte linhas scrapadas (dict) em Position."""
    result: list[Position] = []
    for row in rows:
        result.append(
            Position(
                data_gps=parse_dt(str(row.get("data_gps") or row.get("Data GPS") or "")),
                data_sistema=parse_dt(
                    str(row.get("data_sistema") or row.get("Data Sistema") or "")
                ),
                modo=str(row.get("modo") or row.get("Modo") or "").strip(),
                endereco=str(row.get("endereco") or row.get("Endereço") or "").strip(),
                referencia=str(
                    row.get("referencia") or row.get("Referência") or ""
                ).strip(),
                temperatura=str(
                    row.get("temperatura") or row.get("Temperatura") or ""
                ).strip(),
                raw=row,
            )
        )
    return result
