"""
Site mobile (nuvem):
  - Login do app (opcional simples) / serviço no servidor
  - Escolhe: 1 placa ou todos
  - Gera 1 PDF-resumo
  - PDFs brutos do Sitrax ficam só no TEMP do servidor e são apagados
"""

from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sitrax-bot")

BASE = Path(__file__).resolve().parent
app = FastAPI(title="Resumo de Rota", version="2.0.0")
templates = Jinja2Templates(directory=str(BASE / "templates"))
executor = ThreadPoolExecutor(max_workers=1)  # 1 por vez no Chrome

static_dir = BASE / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# cache em memória do último PDF-resumo (não é o bruto do Sitrax)
_LAST_REPORT: dict = {}


def parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def sitrax_configured() -> bool:
    return bool(
        settings.sitrax_cliente and settings.sitrax_usuario and settings.sitrax_senha
    )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "configured": sitrax_configured(),
            "today": date.today().isoformat(),
            "error": None,
            "texto": None,
            "has_pdf": bool(_LAST_REPORT.get("pdf_bytes")),
            "pdf_name": _LAST_REPORT.get("pdf_filename"),
        },
    )


def _run_job(modo: str, placa: str, d_ini: date, d_fim: date):
    from app.bot.pipeline import (
        ReportResult,
        generate_vehicle_report_cloud,
        report_from_positions,
        TempWorkspace,
    )
    from app.bot.sitrax import SitraxBot

    data_ref = d_ini.strftime("%d/%m/%Y")
    if d_fim != d_ini:
        data_ref += f" a {d_fim.strftime('%d/%m/%Y')}"

    if modo == "placa":
        return generate_vehicle_report_cloud(
            placa=placa.strip(),
            data_ini=d_ini,
            data_fim=d_fim,
            headless=True,
        )

    # todos: gera um PDF multi-página em memória (ainda 1 arquivo)
    from app.bot.summary_pdf import build_summary_pdf_bytes, safe_filename
    from app.bot.report import build_narrative_report
    from pypdf import PdfWriter
    import io

    textos = []
    pdf_writer = PdfWriter()

    with TempWorkspace(prefix="sitrax_all_") as tmp:
        with SitraxBot(headless=True, download_dir=tmp) as bot:
            bot.login()
            vehicles = bot.get_all_plates()
            if not vehicles:
                # se modal listou vazio, tenta abrir de novo
                bot.open_posicoes()
                bot.open_vehicle_selector()
                bot.load_vehicle_list()
                vehicles = bot.list_plates()

            for v in vehicles:
                pl = v["placa"]
                try:
                    try:
                        pdf_bruto = bot.download_historico_pdf(
                            pl, data_ini=d_ini, data_fim=d_fim, dest_dir=tmp
                        )
                        from app.bot.pdf_parser import positions_from_pdf

                        _, positions = positions_from_pdf(pdf_bruto)
                    except Exception:
                        positions = bot.get_positions_for_plate(
                            pl, data_ini=d_ini, data_fim=d_fim
                        )

                    textos.append(
                        build_narrative_report(
                            pl, positions, data_ref=data_ref, cliente=v.get("cliente", "")
                        )
                    )
                    one = build_summary_pdf_bytes(
                        pl, positions, data_ref=data_ref, cliente=v.get("cliente", "")
                    )
                    from pypdf import PdfReader

                    reader = PdfReader(io.BytesIO(one))
                    for page in reader.pages:
                        pdf_writer.add_page(page)
                except Exception as e:
                    logger.exception("Falha em %s", pl)
                    textos.append(f"📋 {pl}: erro — {e}")

        # tmp apagado aqui (PDFs brutos do Sitrax sumiram)

    out = io.BytesIO()
    pdf_writer.write(out)
    pdf_bytes = out.getvalue()
    texto = "\n\n".join(textos) if textos else "Nenhum veículo processado."
    return ReportResult(
        placa="FROTA",
        data_ref=data_ref,
        texto=texto,
        pdf_bytes=pdf_bytes if pdf_bytes else build_summary_pdf_bytes("FROTA", [], data_ref),
        pdf_filename=safe_filename("FROTA", data_ref),
        pontos=0,
    )


@app.post("/gerar", response_class=HTMLResponse)
async def gerar(
    request: Request,
    modo: str = Form(...),
    placa: str = Form(""),
    data_ini: str = Form(""),
    data_fim: str = Form(""),
):
    import asyncio

    error = None
    texto = None
    has_pdf = False
    pdf_name = None

    if not sitrax_configured():
        error = "Servidor sem credenciais Sitrax (.env). Contate o administrador."
    elif modo == "placa" and not placa.strip():
        error = "Informe a placa do veículo."
    else:
        d_ini = parse_date(data_ini) or date.today()
        d_fim = parse_date(data_fim) or d_ini
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                executor,
                lambda: _run_job(modo, placa, d_ini, d_fim),
            )
            _LAST_REPORT.clear()
            _LAST_REPORT.update(
                {
                    "pdf_bytes": result.pdf_bytes,
                    "pdf_filename": result.pdf_filename,
                    "texto": result.texto,
                    "placa": result.placa,
                }
            )
            texto = result.texto
            has_pdf = True
            pdf_name = result.pdf_filename
        except Exception as e:
            logger.exception("Erro ao gerar")
            error = f"{type(e).__name__}: {e}"

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "configured": sitrax_configured(),
            "today": date.today().isoformat(),
            "error": error,
            "texto": texto,
            "has_pdf": has_pdf,
            "pdf_name": pdf_name,
            "modo": modo,
            "placa": placa,
            "data_ini": data_ini,
            "data_fim": data_fim,
        },
    )


@app.get("/baixar-resumo")
async def baixar_resumo():
    """Único PDF que o celular recebe: o resumo. Não é o histórico bruto do Sitrax."""
    data = _LAST_REPORT.get("pdf_bytes")
    name = _LAST_REPORT.get("pdf_filename") or "resumo_rota.pdf"
    if not data:
        return JSONResponse({"ok": False, "error": "Nenhum resumo gerado ainda"}, status_code=404)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/relatorio")
async def api_relatorio(
    modo: str = Form("placa"),
    placa: str = Form(""),
    data_ini: str = Form(""),
    data_fim: str = Form(""),
):
    """API JSON + PDF base64 do resumo (sem PDF bruto)."""
    import asyncio
    import base64

    if not sitrax_configured():
        return JSONResponse({"ok": False, "error": "Credenciais não configuradas"}, status_code=400)
    d_ini = parse_date(data_ini) or date.today()
    d_fim = parse_date(data_fim) or d_ini
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            executor, lambda: _run_job(modo, placa, d_ini, d_fim)
        )
        return {
            "ok": True,
            "placa": result.placa,
            "relatorio": result.texto,
            "pdf_filename": result.pdf_filename,
            "pdf_base64": base64.b64encode(result.pdf_bytes).decode("ascii"),
            "pontos": result.pontos,
        }
    except Exception as e:
        logger.exception("API erro")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "configured": sitrax_configured(),
        "mode": "cloud-summary-only",
    }
