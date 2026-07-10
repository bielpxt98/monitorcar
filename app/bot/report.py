"""Gera relatório narrativo a partir do histórico de posições."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
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


IGNITION_ON_HINTS = (
    "ignição ligada",
    "ignicao ligada",
    "ignição lig",
    "ligada",
    "normal",
    "movimento",
    "em movimento",
)
IGNITION_OFF_HINTS = (
    "estacionado",
    "desligada",
    "ignição desligada",
    "ignicao desligada",
    "parado",
)


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
    if not text:
        return "Local desconhecido"

    # Padrões comuns: "Cidade (UF)" ou final do endereço
    m = re.search(
        r"[-–]\s*([A-Za-zÀ-ú\s]+?)\s*\(([A-Z]{2})\)\s*$",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip().title()

    m = re.search(r"\bde\s+([A-ZÁÉÍÓÚÃÕÂÊÔÇ][A-Za-zÀ-ú\s]{2,40})\b", referencia)
    if m:
        return m.group(1).strip().title()

    # Cidades da Grande Recife (fallback por palavra-chave)
    cities = [
        "Recife",
        "Olinda",
        "Paulista",
        "Abreu e Lima",
        "Igarassu",
        "Camaragibe",
        "Jaboatão",
        "Jaboatao",
        "São Lourenço da Mata",
        "Sao Lourenco da Mata",
        "Moreno",
        "Cabo de Santo Agostinho",
        "Ipojuca",
        "Goiana",
        "Itamaracá",
        "Itamaraca",
        "Araçoiaba",
        "Aracoiaba",
        "Paudalho",
        "Caruaru",
        "Petrolina",
    ]
    lower = text.lower()
    for city in cities:
        if city.lower() in lower:
            return city.replace("Jaboatao", "Jaboatão").replace(
                "Sao Lourenco da Mata", "São Lourenço da Mata"
            ).replace("Itamaraca", "Itamaracá").replace("Aracoiaba", "Araçoiaba")

    # Último segmento do endereço
    parts = re.split(r"[-–,]", endereco)
    if parts:
        last = parts[-1].strip()
        last = re.sub(r"\s*\([A-Z]{2}\)\s*", "", last).strip()
        if 2 < len(last) < 40:
            return last.title()

    return "Local desconhecido"


def is_ignition_on(modo: str) -> Optional[bool]:
    m = (modo or "").lower()
    if any(h in m for h in IGNITION_ON_HINTS) and "deslig" not in m:
        if "estacionado" in m:
            return False
        return True
    if any(h in m for h in IGNITION_OFF_HINTS):
        return False
    if "alerta" in m and "igni" in m:
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


def _same_city(a: str, b: str) -> bool:
    """Considera Jaboatão ≈ Jaboatão dos Guararapes, etc."""
    a = _normalize_city(a)
    b = _normalize_city(b)
    if a == b:
        return True
    if a == "Local Desconhecido" or b == "Local Desconhecido":
        return True
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


def find_ignition_events(
    positions: list[Position],
) -> tuple[Optional[datetime], Optional[datetime]]:
    ordered = sorted(
        [p for p in positions if p.when],
        key=lambda p: p.when,  # type: ignore[arg-type, return-value]
    )
    ligou: Optional[datetime] = None
    desligou: Optional[datetime] = None
    prev_on: Optional[bool] = None

    for pos in ordered:
        state = is_ignition_on(pos.modo)
        if state is None:
            continue
        if state and not prev_on:
            if ligou is None:
                ligou = pos.when
        if state is False and prev_on:
            desligou = pos.when
        if state is not None:
            prev_on = state

    # Se terminou estacionado, marca desligamento no último ponto off
    if ordered:
        last = ordered[-1]
        if is_ignition_on(last.modo) is False:
            desligou = last.when

    return ligou, desligou


def build_narrative_report(
    placa: str,
    positions: list[Position],
    data_ref: str = "",
    cliente: str = "",
) -> str:
    """
    Exemplo de saída:
    Veículo PCE7B03 (10/07/2026)
    - Ligou às 06:01
    - de 06:01 às 12:00 esteve em Abreu e Lima
    - de 12:00 às 14:00 esteve em Paulista
    - de 20:00 em Paulista e desligou
    """
    lines: list[str] = []
    header = f"📋 Relatório — placa {placa.upper()}"
    if cliente:
        header += f" ({cliente})"
    if data_ref:
        header += f"\n📅 Data: {data_ref}"
    lines.append(header)
    lines.append("")

    if not positions:
        lines.append("Nenhum registro de posição encontrado no período.")
        return "\n".join(lines)

    ordered = sorted(
        [p for p in positions if p.when],
        key=lambda p: p.when,  # type: ignore[arg-type, return-value]
    )
    ligou, desligou = find_ignition_events(ordered)
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
    for i, seg in enumerate(segments):
        lines.append(
            f"   • {format_range(seg.inicio, seg.fim)} esteve em {seg.cidade}"
        )

    if desligou:
        # cidade no horário do desligamento
        city_off = segments[-1].cidade if segments else "local desconhecido"
        lines.append("")
        lines.append(f"🔒 Desligou às {format_time(desligou)} em {city_off}")
    elif ordered:
        last = ordered[-1]
        if last.when:
            lines.append("")
            lines.append(
                f"ℹ️ Último registro: {format_time(last.when)} — "
                f"{last.cidade} ({last.modo or 'n/d'})"
            )

    lines.append("")
    lines.append(f"Total de pontos GPS: {len(ordered)}")
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
