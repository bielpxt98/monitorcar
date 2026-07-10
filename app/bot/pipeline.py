"""
Pipeline em nuvem:
  1) Trabalha só em pasta TEMP no servidor
  2) (Opcional) baixa PDF bruto do Sitrax → parse
  3) Gera 1 PDF-resumo
  4) Apaga TODO material temporário (brutos do Sitrax, etc.)
  5) Devolve só o resumo para o celular
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from app.bot.pdf_parser import positions_from_pdf
from app.bot.report import Position, build_narrative_report, positions_from_rows
from app.bot.summary_pdf import build_summary_pdf_bytes, safe_filename

logger = logging.getLogger(__name__)


@dataclass
class ReportResult:
    placa: str
    data_ref: str
    texto: str
    pdf_bytes: bytes
    pdf_filename: str
    pontos: int


class TempWorkspace:
    """Pasta temporária no servidor — sempre removida no finally."""

    def __init__(self, prefix: str = "sitrax_job_"):
        self.prefix = prefix
        self.path: Optional[Path] = None

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix=self.prefix))
        logger.info("Temp workspace: %s", self.path)
        return self.path

    def __exit__(self, *args) -> None:
        if self.path and self.path.exists():
            try:
                shutil.rmtree(self.path, ignore_errors=True)
                logger.info("Temp workspace apagado: %s", self.path)
            except Exception as e:
                logger.warning("Falha ao apagar temp %s: %s", self.path, e)
        self.path = None


def report_from_positions(
    placa: str,
    positions: list[Position],
    data_ref: str = "",
    cliente: str = "",
) -> ReportResult:
    data_ref = data_ref or date.today().strftime("%d/%m/%Y")
    texto = build_narrative_report(
        placa, positions, data_ref=data_ref, cliente=cliente
    )
    pdf_bytes = build_summary_pdf_bytes(
        placa, positions, data_ref=data_ref, cliente=cliente
    )
    return ReportResult(
        placa=placa.upper(),
        data_ref=data_ref,
        texto=texto,
        pdf_bytes=pdf_bytes,
        pdf_filename=safe_filename(placa, data_ref),
        pontos=len([p for p in positions if p.when]),
    )


def report_from_sitrax_pdf(
    pdf_path: str | Path,
    placa: Optional[str] = None,
    data_ref: str = "",
) -> ReportResult:
    """Parse de um PDF bruto (já no servidor) → resumo. Não envia o bruto ao usuário."""
    p_placa, positions = positions_from_pdf(pdf_path)
    if placa:
        p_placa = placa
    return report_from_positions(p_placa, positions, data_ref=data_ref)


def report_from_scraped_rows(
    placa: str,
    rows: list[dict],
    data_ref: str = "",
    cliente: str = "",
) -> ReportResult:
    positions = positions_from_rows(rows)
    return report_from_positions(placa, positions, data_ref=data_ref, cliente=cliente)


def generate_vehicle_report_cloud(
    placa: str,
    data_ini: Optional[date] = None,
    data_fim: Optional[date] = None,
    headless: bool = True,
) -> ReportResult:
    """
    Executa o robô no servidor:
      - downloads do Sitrax vão para pasta TEMP
      - gera só o PDF-resumo em memória
      - apaga a pasta TEMP (inclusive PDFs brutos)
    """
    from app.bot.sitrax import SitraxBot

    data_ini = data_ini or date.today()
    data_fim = data_fim or data_ini
    data_ref = data_ini.strftime("%d/%m/%Y")
    if data_fim != data_ini:
        data_ref += f" a {data_fim.strftime('%d/%m/%Y')}"

    with TempWorkspace(prefix=f"sitrax_{placa}_") as tmp:
        # Chrome baixa PDFs brutos só dentro de tmp
        with SitraxBot(headless=headless, download_dir=tmp) as bot:
            bot.login()
            # tenta fluxo com download de PDF do Sitrax; se falhar, usa scrape da tabela
            try:
                pdf_bruto = bot.download_historico_pdf(
                    placa, data_ini=data_ini, data_fim=data_fim, dest_dir=tmp
                )
                if pdf_bruto and Path(pdf_bruto).exists():
                    result = report_from_sitrax_pdf(
                        pdf_bruto, placa=placa, data_ref=data_ref
                    )
                else:
                    raise RuntimeError("PDF bruto não baixado")
            except Exception as e:
                logger.warning("Fallback scrape tabela: %s", e)
                positions = bot.get_positions_for_plate(
                    placa, data_ini=data_ini, data_fim=data_fim
                )
                result = report_from_positions(placa, positions, data_ref=data_ref)

        # ao sair do with TempWorkspace, tmp (e PDF bruto) são apagados
        # pdf_bytes do resumo já está em memória
        return result
