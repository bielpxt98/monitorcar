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

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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


@app.get("/gerar")
@app.get("/gerar-pdf")
async def gerar_get():
    """Refresh/bookmark → volta para a home."""
    return RedirectResponse(url="/", status_code=303)


@app.post("/gerar-pdf", response_class=HTMLResponse)
async def gerar_pdf(
    request: Request,
    pdf: UploadFile = File(...),
    placa: str = Form(""),
    data: str = Form(""),
):
    """
    Caminho confiável na nuvem:
      - usuário envia o PDF baixado do Sitrax
      - servidor parseia em pasta TEMP
      - gera 1 PDF-resumo
      - apaga o bruto
    """
    import asyncio

    error = None
    texto = None
    has_pdf = False
    pdf_name = None

    name = (pdf.filename or "").lower()
    if not name.endswith(".pdf"):
        error = "Envie um arquivo .pdf (histórico do Sitrax)."
    else:
        d_ref = parse_date(data)
        data_ref = (
            d_ref.strftime("%d/%m/%Y") if d_ref else date.today().strftime("%d/%m/%Y")
        )
        try:
            raw = await pdf.read()
            if len(raw) < 500:
                raise ValueError("Arquivo PDF muito pequeno ou vazio.")

            def process():
                from app.bot.pipeline import TempWorkspace, report_from_sitrax_pdf

                with TempWorkspace(prefix="upload_") as tmp:
                    bruto = tmp / "sitrax_upload.pdf"
                    bruto.write_bytes(raw)
                    # ao sair do with, o PDF bruto é apagado do servidor
                    return report_from_sitrax_pdf(
                        bruto,
                        placa=placa.strip() or None,
                        data_ref=data_ref,
                    )

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(executor, process)
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
            logger.exception("Erro no upload PDF")
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
            "placa": placa,
        },
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


@app.get("/debug", response_class=HTMLResponse)
async def debug_panel(request: Request):
    """Painel de calibração: última execução do robô com fotos de cada passo."""
    from app.bot.debug_session import get_last_run

    run = get_last_run()
    return templates.TemplateResponse(
        request,
        "debug.html",
        {
            "run": run,
            "configured": sitrax_configured(),
        },
    )


@app.post("/calibrar", response_class=HTMLResponse)
async def calibrar(request: Request):
    """
    Roda o robô só até abrir o modal de veículos (sem gerar relatório).
    Serve para calibrar cliques olhando /debug.
    """
    import asyncio

    if not sitrax_configured():
        return RedirectResponse(url="/debug", status_code=303)

    def job():
        from app.bot import debug_session
        from app.bot.sitrax import SitraxBot
        from app.bot.pipeline import TempWorkspace

        debug_session.start_run(placa="CALIBRAGEM")
        try:
            with TempWorkspace(prefix="calib_") as tmp:
                with SitraxBot(headless=True, download_dir=tmp) as bot:
                    bot.login()
                    bot.open_posicoes()
                    bot.open_vehicle_selector()
                    bot.load_vehicle_list(placa="PCE7B03")
                    bot._trace(
                        "calibragem_ok",
                        "Chegou no modal de veículos — calibração parcial OK",
                    )
            debug_session.finish_run(ok=True)
        except Exception as e:
            debug_session.finish_run(ok=False, error=str(e))
            raise

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(executor, job)
    except Exception as e:
        logger.exception("Calibragem falhou: %s", e)

    return RedirectResponse(url="/debug", status_code=303)
