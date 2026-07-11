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
from contextlib import asynccontextmanager
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
executor = ThreadPoolExecutor(max_workers=1)  # 1 por vez no Chrome


def sitrax_configured() -> bool:
    return bool(
        settings.sitrax_cliente and settings.sitrax_usuario and settings.sitrax_senha
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ao subir: 2 Chromes permanentes logados em Veículos."""
    if sitrax_configured():

        def _warm():
            try:
                from app.bot.warm_pool import warm_pool

                logger.info("Aquecendo 2 Chromes permanentes (login → Veículos)…")
                snap = warm_pool.start(headless=True, low_memory=True)
                logger.info("Pool permanente: %s", snap.get("message"))
            except Exception as e:
                logger.exception("Falha ao aquecer pool no startup: %s", e)

        executor.submit(_warm)
    yield
    try:
        from app.bot.warm_pool import warm_pool

        warm_pool.stop()
    except Exception:
        pass


app = FastAPI(title="Resumo de Rota", version="2.0.0", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE / "templates"))

static_dir = BASE / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# cache em memória do último PDF-resumo (não é o bruto do Sitrax)
_LAST_REPORT: dict = {}
# job em background (frota demora → evita "upstream error" do Railway)
_JOB: dict = {
    "running": False,
    "modo": "",
    "placa": "",
    "started": "",
    "message": "",
    "error": "",
    "done": False,
}
# flash one-shot (POST /gerar → redirect /) para a URL voltar à home
_FLASH: dict = {"error": None, "job_msg": None, "placa": None}


def _pop_flash() -> dict:
    out = {
        "error": _FLASH.get("error"),
        "job_msg": _FLASH.get("job_msg"),
        "placa": _FLASH.get("placa"),
    }
    _FLASH["error"] = None
    _FLASH["job_msg"] = None
    _FLASH["placa"] = None
    return out


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


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    flash = _pop_flash()
    job_err = (
        _JOB.get("error")
        if _JOB.get("done") and not _LAST_REPORT.get("pdf_bytes")
        else None
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "configured": sitrax_configured(),
            "today": date.today().isoformat(),
            "error": flash.get("error") or job_err,
            "texto": _LAST_REPORT.get("texto"),
            "has_pdf": bool(_LAST_REPORT.get("pdf_bytes")),
            "pdf_name": _LAST_REPORT.get("pdf_filename"),
            "placa": flash.get("placa") or _LAST_REPORT.get("placa") or "",
            "job_msg": flash.get("job_msg"),
            "job_running": bool(_JOB.get("running")),
            "job_status": _JOB.get("message") or "",
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
    from app.bot import debug_session

    data_ref = d_ini.strftime("%d/%m/%Y")
    if d_fim != d_ini:
        data_ref += f" a {d_fim.strftime('%d/%m/%Y')}"

    placa_u = (placa or "").strip().upper()
    # se digitar TODOS/ALL no campo placa, trata como frota inteira
    if placa_u in ("TODOS", "TODAS", "ALL", "FROTA", "*"):
        modo = "todos"
        placa_u = ""

    if modo == "placa":
        if not placa_u:
            raise ValueError("Informe a placa do veículo.")
        return generate_vehicle_report_cloud(
            placa=placa_u,
            data_ini=d_ini,
            data_fim=d_fim,
            headless=True,
        )

    # ——— TODOS: 2 Chromes permanentes em paralelo (round-robin) ———
    from app.bot.summary_pdf import build_summary_pdf_bytes, safe_filename
    from app.bot.fleet_workers import run_fleet_parallel, DEFAULT_WORKERS
    from app.bot.warm_pool import warm_pool
    from pypdf import PdfWriter, PdfReader
    import io

    debug_session.start_run(placa="FROTA")
    textos: list[str] = []
    pdf_writer = PdfWriter()
    total_pontos = 0
    N_WORKERS = DEFAULT_WORKERS  # 2

    try:
        with TempWorkspace(prefix="sitrax_frota_") as tmp:
            # Lista frota com Chrome permanente (sem 3º browser)
            _JOB["message"] = "Listando frota (Chrome permanente)…"
            vehicles = warm_pool.list_fleet_plates()
            vehicles = [
                v
                for v in vehicles
                if v.get("placa")
                and v["placa"].upper() not in ("TODOS", "TODAS", "ALL", "FROTA")
            ]
            if not vehicles:
                debug_session.step(
                    "frota_vazia",
                    "Nenhum veículo listado no modal",
                    ok=False,
                    screenshot=False,
                )
                raise RuntimeError(
                    "Não achei a lista de veículos da frota. "
                    "Tente 1 placa ou use o upload de PDF."
                )

            w_preview = [[] for _ in range(N_WORKERS)]
            for i, v in enumerate(vehicles):
                w_preview[i % N_WORKERS].append(v["placa"])
            debug_session.step(
                "frota_lista",
                f"Frota: {len(vehicles)} veículo(s) — {N_WORKERS} Chromes "
                f"PERMANENTES round-robin. "
                + " | ".join(
                    f"C{i+1}: {', '.join(w_preview[i][:8])}"
                    + ("…" if len(w_preview[i]) > 8 else "")
                    for i in range(N_WORKERS)
                    if w_preview[i]
                ),
                ok=True,
                screenshot=False,
            )

            # Empresta os 2 já logados (sem fechar / sem login de novo)
            borrowed = warm_pool.borrow_for_fleet()
            _JOB["message"] = (
                f"Frota: 0/{len(vehicles)} — 2 Chromes permanentes em paralelo…"
            )

            def _msg(m: str) -> None:
                _JOB["message"] = m

            def _on_replaced(wid: int, bot) -> None:
                warm_pool.replace_fleet_bot(wid, bot)

            try:
                plate_results = run_fleet_parallel(
                    vehicles=vehicles,
                    data_ini=d_ini,
                    data_fim=d_fim,
                    data_ref=data_ref,
                    download_dir=tmp,
                    n_workers=N_WORKERS,
                    job_message_cb=_msg,
                    existing_bots=borrowed,
                    keep_alive=True,
                    on_bot_replaced=_on_replaced,
                )
            finally:
                # Devolve os 2 a Veículos (continuam ligados)
                try:
                    snap = warm_pool.release_after_fleet()
                    logger.info("Após frota: %s", snap.get("message"))
                except Exception as e:
                    logger.warning("release_after_fleet: %s", e)

            for r in plate_results:
                total_pontos += r.pontos
                textos.append(r.texto)
                if r.pdf_bytes:
                    try:
                        reader = PdfReader(io.BytesIO(r.pdf_bytes))
                        for page in reader.pages:
                            pdf_writer.add_page(page)
                    except Exception as e:
                        logger.warning("PDF %s: %s", r.placa, e)

        out = io.BytesIO()
        if len(pdf_writer.pages) == 0:
            one = build_summary_pdf_bytes("FROTA", [], data_ref=data_ref)
            pdf_bytes = one
        else:
            pdf_writer.write(out)
            pdf_bytes = out.getvalue()

        texto = "\n\n".join(textos) if textos else "Nenhum veículo processado."
        debug_session.finish_run(ok=True)
        return ReportResult(
            placa="FROTA",
            data_ref=data_ref,
            texto=texto,
            pdf_bytes=pdf_bytes,
            pdf_filename=safe_filename("FROTA", data_ref),
            pontos=total_pontos,
        )
    except Exception as e:
        debug_session.finish_run(ok=False, error=str(e))
        try:
            warm_pool.release_after_fleet()
        except Exception:
            pass
        raise


@app.get("/gerar")
@app.get("/gerar-pdf")
async def gerar_get():
    """Refresh/bookmark → volta para a home."""
    return RedirectResponse(url="/", status_code=303)


@app.post("/gerar-pdf")
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
      - redireciona para a home (/)
    """
    import asyncio

    error = None
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
                    return report_from_sitrax_pdf(
                        bruto,
                        placa=placa.strip() or None,
                        data_ref=data_ref,
                    )

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(executor, process)
            _store_result(result)
        except Exception as e:
            logger.exception("Erro no upload PDF")
            error = f"{type(e).__name__}: {e}"

    _FLASH["error"] = error
    _FLASH["placa"] = (placa or "").strip().upper() or None
    return RedirectResponse(url="/", status_code=303)


def _store_result(result) -> None:
    _LAST_REPORT.clear()
    _LAST_REPORT.update(
        {
            "pdf_bytes": result.pdf_bytes,
            "pdf_filename": result.pdf_filename,
            "texto": result.texto,
            "placa": result.placa,
        }
    )


def _start_bg_job(modo: str, placa: str, d_ini: date, d_fim: date) -> bool:
    """Inicia job em background. False se já houver um rodando."""
    if _JOB.get("running"):
        return False

    def work():
        _JOB.update(
            {
                "running": True,
                "done": False,
                "error": "",
                "modo": modo,
                "placa": placa,
                "started": datetime.now().strftime("%H:%M:%S"),
                "message": "Iniciando…",
            }
        )
        try:
            result = _run_job(modo, placa, d_ini, d_fim)
            _store_result(result)
            _JOB["message"] = (
                f"Concluído — {result.placa} ({result.pontos} pontos). "
                "Baixe o PDF na home."
            )
            _JOB["done"] = True
        except Exception as e:
            logger.exception("Job background falhou")
            _JOB["error"] = f"{type(e).__name__}: {e}"
            _JOB["message"] = f"Erro: {e}"
            _JOB["done"] = True
        finally:
            _JOB["running"] = False

    executor.submit(work)
    return True


@app.post("/gerar")
async def gerar(
    request: Request,
    modo: str = Form(...),
    placa: str = Form(""),
    data_ini: str = Form(""),
    data_fim: str = Form(""),
):
    """
    1 placa: espera o resultado e redireciona para / (home).
    Todos (frota): BACKGROUND e redireciona para / — evita ficar em /gerar.
    """
    import asyncio

    error = None
    job_msg = None
    placa_u = (placa or "").strip().upper()

    if not sitrax_configured():
        error = "Servidor sem credenciais Sitrax (.env). Contate o administrador."
    elif modo == "placa" and not placa.strip():
        error = "Informe a placa do veículo (ou escolha Todos)."
    else:
        d_ini = parse_date(data_ini) or date.today()
        d_fim = parse_date(data_fim) or d_ini
        modo_n = (modo or "placa").lower()
        if placa_u in ("TODOS", "TODAS", "ALL", "FROTA", "*"):
            modo_n = "todos"

        if modo_n == "todos":
            if _JOB.get("running"):
                job_msg = (
                    f"Já há um job em andamento desde {_JOB.get('started')}: "
                    f"{_JOB.get('message')}. Acompanhe em /debug."
                )
            else:
                ok = _start_bg_job("todos", placa, d_ini, d_fim)
                if ok:
                    job_msg = (
                        "Frota iniciada em segundo plano (evita erro upstream). "
                        "Acompanhe os passos em Calibração (/debug). "
                        "Quando terminar, baixe o PDF do resumo aqui. "
                        "A página atualiza sozinha a cada 15s."
                    )
                else:
                    job_msg = "Não foi possível iniciar o job (ocupado)."
        else:
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(
                    executor,
                    lambda: _run_job("placa", placa, d_ini, d_fim),
                )
                _store_result(result)
            except Exception as e:
                logger.exception("Erro ao gerar")
                error = f"{type(e).__name__}: {e}"

    _FLASH["error"] = error
    _FLASH["job_msg"] = job_msg
    _FLASH["placa"] = placa_u if placa_u else None
    # URL volta para a home (não fica em /gerar)
    return RedirectResponse(url="/", status_code=303)


@app.get("/job-status")
async def job_status():
    return {
        "running": bool(_JOB.get("running")),
        "done": bool(_JOB.get("done")),
        "message": _JOB.get("message") or "",
        "error": _JOB.get("error") or "",
        "has_pdf": bool(_LAST_REPORT.get("pdf_bytes")),
        "pdf_name": _LAST_REPORT.get("pdf_filename"),
    }


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
    warm = {}
    try:
        from app.bot.warm_pool import warm_pool

        warm = warm_pool.snapshot()
    except Exception:
        pass
    return {
        "status": "ok",
        "configured": sitrax_configured(),
        "mode": "cloud-summary-only",
        "warm": warm,
    }


@app.get("/debug", response_class=HTMLResponse)
async def debug_panel(request: Request):
    """Painel de calibração: última execução do robô com fotos de cada passo."""
    from app.bot.debug_session import get_last_run
    from app.bot.warm_pool import warm_pool

    run = get_last_run()
    return templates.TemplateResponse(
        request,
        "debug.html",
        {
            "run": run,
            "configured": sitrax_configured(),
            "warm": warm_pool.snapshot(),
            "today": date.today().isoformat(),
        },
    )


@app.get("/warm/status")
async def warm_status():
    from app.bot.warm_pool import warm_pool

    return warm_pool.snapshot()


@app.post("/warm/start")
async def warm_start():
    """Liga o Chrome permanente (login → Posições → modal Veículos)."""
    import asyncio

    if not sitrax_configured():
        return RedirectResponse(url="/debug", status_code=303)

    def job():
        from app.bot.warm_pool import warm_pool

        warm_pool.start(headless=True, low_memory=True)

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(executor, job)
    except Exception as e:
        logger.exception("Warm start: %s", e)
    return RedirectResponse(url="/debug", status_code=303)


@app.post("/warm/stop")
async def warm_stop():
    import asyncio
    from app.bot.warm_pool import warm_pool

    def job():
        warm_pool.stop()

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(executor, job)
    return RedirectResponse(url="/debug", status_code=303)


@app.post("/warm/testar")
async def warm_testar(
    request: Request,
    placa: str = Form(...),
    data_ini: str = Form(""),
    data_fim: str = Form(""),
):
    """Teste rápido de 1 placa na sessão permanente (volta para Veículos)."""
    import asyncio

    if not sitrax_configured():
        return RedirectResponse(url="/debug", status_code=303)

    d_ini = parse_date(data_ini) or date.today()
    d_fim = parse_date(data_fim) or d_ini
    placa_u = (placa or "").strip().upper()

    def job():
        from app.bot.warm_pool import warm_pool

        return warm_pool.run_plate(placa_u, d_ini, d_fim, headless=True)

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(executor, job)
        _store_result(result)
        _FLASH["error"] = None
        _FLASH["placa"] = placa_u
    except Exception as e:
        logger.exception("Warm testar %s: %s", placa_u, e)
        _FLASH["error"] = f"{type(e).__name__}: {e}"
        _FLASH["placa"] = placa_u
    # Volta para a home (não fica em /debug nem /gerar)
    return RedirectResponse(url="/", status_code=303)


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
                    n = bot._count_vehicle_items()
                    bot._trace(
                        "calibragem_lista",
                        f"Lista com {n} veículo(s) — lupa só se estiver vazia",
                    )
                    bot.select_vehicle_by_plate("PCE7B03")
                    bot._trace(
                        "calibragem_select",
                        "Select PCE7B03 OK",
                    )
                    # Filter + nuvem de download (próximo passo crítico)
                    bot.click_filtrar()
                    bot._trace("calibragem_filter", "Clicou Filter")
                    bot._sleep(2)

                    try:
                        # nuvem → Export → PDF file → espera arquivo no temp
                        pdf = bot.download_historico_pdf(
                            "PCE7B03",
                            dest_dir=tmp,
                            timeout=120,
                            already_filtered=True,
                        )
                        if pdf and pdf.exists():
                            bot._trace(
                                "calibragem_ok",
                                f"PDF baixado: {pdf.name} ({pdf.stat().st_size} bytes) — ciclo completo!",
                            )
                        else:
                            raise TimeoutError("PDF vazio")
                    except Exception as e:
                        bot._trace(
                            "calibragem_download_falhou",
                            f"Download PDF falhou: {e} — tentando ler tabela na tela",
                            ok=False,
                        )
                        # Plano B: dados já estão na tela (Filter OK)
                        try:
                            bot.try_scroll_all()
                            rows = bot.scrape_positions_table()
                            n = len(rows)
                            if n > 0:
                                bot._trace(
                                    "calibragem_ok_tabela",
                                    f"PDF nao baixou, mas tabela com {n} linha(s) lida — resumo automatico usara a tabela",
                                    ok=True,
                                )
                            else:
                                bot._trace(
                                    "calibragem_download_timeout",
                                    "Nem PDF nem linhas da tabela — ver logs",
                                    ok=False,
                                )
                        except Exception as e2:
                            bot._trace(
                                "calibragem_scrape_falhou",
                                str(e2),
                                ok=False,
                            )
                            raise e
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
