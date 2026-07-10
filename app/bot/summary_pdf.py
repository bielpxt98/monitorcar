"""Gera o PDF-resumo (único arquivo que o usuário recebe)."""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.bot.report import (
    Position,
    build_segments,
    find_ignition_events,
    format_time,
)


def build_summary_pdf_bytes(
    placa: str,
    positions: list[Position],
    data_ref: str = "",
    cliente: str = "",
    titulo: str = "Resumo de Rota",
) -> bytes:
    """
    PDF limpo e curto: horários por cidade.
    NÃO é o histórico completo do Sitrax.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1.6 * cm,
        rightMargin=1.6 * cm,
        topMargin=1.4 * cm,
        bottomMargin=1.4 * cm,
        title=f"{titulo} — {placa}",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleBR",
        parent=styles["Heading1"],
        fontSize=16,
        alignment=TA_CENTER,
        spaceAfter=6,
        textColor=colors.HexColor("#1a1f35"),
    )
    sub_style = ParagraphStyle(
        "SubBR",
        parent=styles["Normal"],
        fontSize=10,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#4b5563"),
        spaceAfter=12,
    )
    body_style = ParagraphStyle(
        "BodyBR",
        parent=styles["Normal"],
        fontSize=11,
        leading=16,
        textColor=colors.HexColor("#111827"),
    )
    foot_style = ParagraphStyle(
        "FootBR",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#9ca3af"),
        alignment=TA_CENTER,
    )

    ordered = sorted([p for p in positions if p.when], key=lambda p: p.when)  # type: ignore
    segments = build_segments(ordered)
    ligou, desligou = find_ignition_events(ordered)

    story = []
    story.append(Paragraph(titulo, title_style))
    header_bits = [f"<b>Placa:</b> {placa.upper()}"]
    if cliente:
        header_bits.append(f"<b>Cliente:</b> {cliente}")
    if data_ref:
        header_bits.append(f"<b>Data:</b> {data_ref}")
    story.append(Paragraph(" &nbsp;|&nbsp; ".join(header_bits), sub_style))
    story.append(Spacer(1, 4 * mm))

    # Ignition line
    ign_lines = []
    if ligou:
        ign_lines.append(f"Ligou às <b>{format_time(ligou)}</b>")
    if desligou:
        city_off = segments[-1].cidade if segments else "—"
        ign_lines.append(f"Desligou às <b>{format_time(desligou)}</b> em {city_off}")
    if ign_lines:
        story.append(Paragraph(" &nbsp;·&nbsp; ".join(ign_lines), body_style))
        story.append(Spacer(1, 6 * mm))

    # Table of segments
    if not segments:
        story.append(
            Paragraph(
                "Nenhum registro de posição encontrado no período.",
                body_style,
            )
        )
    else:
        story.append(Paragraph("<b>Resumo por cidade / horário</b>", body_style))
        story.append(Spacer(1, 3 * mm))

        data = [["De", "Até", "Cidade / local"]]
        for seg in segments:
            data.append(
                [
                    format_time(seg.inicio),
                    format_time(seg.fim),
                    seg.cidade,
                ]
            )

        table = Table(data, colWidths=[2.8 * cm, 2.8 * cm, 11 * cm])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ff6b00")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("ALIGN", (0, 0), (1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e5e7eb")),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#fff7ed")],
                    ),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(table)

    story.append(Spacer(1, 10 * mm))
    story.append(
        Paragraph(
            f"Pontos GPS analisados: {len(ordered)} &nbsp;·&nbsp; "
            f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}<br/>"
            "Este PDF é um resumo. Não contém o histórico completo do rastreador.",
            foot_style,
        )
    )

    doc.build(story)
    return buffer.getvalue()


def safe_filename(placa: str, data_ref: str = "") -> str:
    placa_c = re.sub(r"[^A-Za-z0-9]", "", placa.upper()) or "VEICULO"
    data_c = re.sub(r"[^0-9]", "", data_ref)[:8] or date.today().strftime("%Y%m%d")
    return f"resumo_rota_{placa_c}_{data_c}.pdf"
