"""Gera o PDF-resumo (único arquivo que o usuário recebe)."""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.colors import Color
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.lib.utils import ImageReader
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

# Logo ANTONIO (pdf-fundo) — NÃO usar bg-hero dos headsets
_BG_CANDIDATES = (
    Path(__file__).resolve().parents[1] / "static" / "pdf-fundo.jpg",
    Path(__file__).resolve().parents[1] / "static" / "pdf-fundo.jpeg",
    Path(__file__).resolve().parents[1] / "static" / "pdf-fundo.png",
    Path(__file__).resolve().parents[2] / "app" / "static" / "pdf-fundo.jpg",
)


def _bg_image_path() -> Optional[Path]:
    for p in _BG_CANDIDATES:
        if p.is_file():
            return p
    return None


def _draw_page_background(canvas, doc) -> None:
    """
    Fundo do resumo:
      - cor sólida + logo ANTONIO em marca d'água
      - cartão claro para o texto legível
    """
    page_w, page_h = A4
    canvas.saveState()

    # fundo sólido (escuro suave)
    canvas.setFillColor(colors.HexColor("#0f1221"))
    canvas.rect(0, 0, page_w, page_h, fill=1, stroke=0)

    bg = _bg_image_path()
    if bg is not None:
        try:
            img = ImageReader(str(bg))
            iw, ih = img.getSize()
            if iw > 0 and ih > 0:
                # logo grande central como marca d'água (mantém proporção)
                max_side = min(page_w, page_h) * 0.72
                scale = max_side / max(iw, ih)
                tw, th = iw * scale, ih * scale
                x = (page_w - tw) / 2.0
                y = (page_h - th) / 2.0 - 0.4 * cm
                canvas.saveState()
                # reportlab não tem alpha em drawImage em todas versões:
                # desenhamos e cobrimos com véu claro no cartão
                canvas.drawImage(
                    img,
                    x,
                    y,
                    width=tw,
                    height=th,
                    mask="auto",
                    preserveAspectRatio=True,
                    anchor="c",
                )
                canvas.restoreState()
        except Exception:
            pass

    # véu escuro leve por cima do logo (borda ainda mostra a marca)
    canvas.setFillColor(Color(0.06, 0.07, 0.13, alpha=0.35))
    canvas.rect(0, 0, page_w, page_h, fill=1, stroke=0)

    # cartão branco onde o conteúdo fica legível
    margin_x = 1.05 * cm
    margin_y = 1.05 * cm
    canvas.setFillColor(Color(1, 1, 1, alpha=0.94))
    canvas.setStrokeColor(Color(1, 1, 1, alpha=0.55))
    canvas.setLineWidth(0.6)
    canvas.roundRect(
        margin_x,
        margin_y,
        page_w - 2 * margin_x,
        page_h - 2 * margin_y,
        14,
        fill=1,
        stroke=1,
    )

    # faixa laranja no topo do cartão
    canvas.setFillColor(colors.HexColor("#ff6b00"))
    canvas.roundRect(
        margin_x,
        page_h - margin_y - 0.45 * cm,
        page_w - 2 * margin_x,
        0.45 * cm,
        4,
        fill=1,
        stroke=0,
    )
    canvas.rect(
        margin_x,
        page_h - margin_y - 0.45 * cm,
        page_w - 2 * margin_x,
        0.22 * cm,
        fill=1,
        stroke=0,
    )

    # mini logo no canto inferior direito (dentro da margem, fora do cartão se couber)
    if bg is not None:
        try:
            img = ImageReader(str(bg))
            side = 1.35 * cm
            canvas.drawImage(
                img,
                page_w - margin_x - side - 0.15 * cm,
                margin_y + 0.15 * cm,
                width=side,
                height=side,
                mask="auto",
                preserveAspectRatio=True,
                anchor="c",
            )
        except Exception:
            pass

    canvas.restoreState()


def build_summary_pdf_bytes(
    placa: str,
    positions: list[Position],
    data_ref: str = "",
    cliente: str = "",
    titulo: str = "Resumo de Rota",
) -> bytes:
    """
    PDF limpo e curto: horários por cidade + foto de fundo do app.
    NÃO é o histórico completo do Sitrax.
    """
    buffer = io.BytesIO()
    # margens um pouco maiores por causa do cartão arredondado
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1.9 * cm,
        rightMargin=1.9 * cm,
        topMargin=2.0 * cm,
        bottomMargin=1.8 * cm,
        title=f"{titulo} — {placa}",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleBR",
        parent=styles["Heading1"],
        fontSize=17,
        alignment=TA_CENTER,
        spaceAfter=6,
        textColor=colors.HexColor("#1a1f35"),
        fontName="Helvetica-Bold",
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
        textColor=colors.HexColor("#6b7280"),
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

    ign_lines = []
    if ligou:
        ign_lines.append(f"Ligou às <b>{format_time(ligou)}</b>")
    if desligou:
        city_off = segments[-1].cidade if segments else "—"
        ign_lines.append(f"Desligou às <b>{format_time(desligou)}</b> em {city_off}")
    if ign_lines:
        story.append(Paragraph(" &nbsp;·&nbsp; ".join(ign_lines), body_style))
        story.append(Spacer(1, 6 * mm))

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

        table = Table(data, colWidths=[2.6 * cm, 2.6 * cm, 10.2 * cm])
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

    doc.build(
        story,
        onFirstPage=_draw_page_background,
        onLaterPages=_draw_page_background,
    )
    return buffer.getvalue()


def safe_filename(placa: str, data_ref: str = "") -> str:
    placa_c = re.sub(r"[^A-Za-z0-9]", "", placa.upper()) or "VEICULO"
    data_c = re.sub(r"[^0-9]", "", data_ref)[:8] or date.today().strftime("%Y%m%d")
    return f"resumo_rota_{placa_c}_{data_c}.pdf"
