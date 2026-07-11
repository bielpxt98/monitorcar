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
    """Ao subir: deixa o Chrome logado em Veículos (testes rápidos)."""
    if sitrax_configured():

        def _warm():
            try:
                from app.bot.warm_pool import warm_pool

                logger.info("Aquecendo robô permanente (login → Veículos)…")
                warm_pool.start(headless=True, low_memory=True)
                logger.info("Robô permanente pronto: %s", warm_pool.message)
            except Exception as e:
                logger.exception("Falha ao aquecer robô no startup: %s", e)

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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "configured": sitrax_configured(),
            "today": date.today().isoformat(),
            "error": _JOB.get("error") if _JOB.get("done") and not _LAST_REPORT.get("pdf_bytes") else None,
            "texto": _LAST_REPORT.get("texto"),
            "has_pdf": bool(_LAST_REPORT.get("pdf_bytes")),
            "pdf_name": _LAST_REPORT.get("pdf_filename"),
            "job_msg": None,
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

    # ——— TODOS: frota 1 a 1; Chrome reinicia a cada N placas (evita tab crashed) ———
    from app.bot.summary_pdf import build_summary_pdf_bytes, safe_filename
    from app.bot.report import build_narrative_report
    from pypdf import PdfWriter, PdfReader
    import io
    import gc

    # Libera RAM: fecha sessão permanente antes da frota (Chrome extra)
    try:
        from app.bot.warm_pool import warm_pool

        warm_pool.stop()
    except Exception:
        pass

    debug_session.start_run(placa="FROTA")
    textos: list[str] = []
    pdf_writer = PdfWriter()
    total_pontos = 0
    # a cada quantas placas reabre o Chrome (RAM Railway)
    RESTART_EVERY = 3

    def _is_crash(err: BaseException) -> bool:
        s = str(err).lower()
        return any(
            x in s
            for x in (
                "tab crashed",
                "session deleted",
                "invalid session",
                "disconnected",
                "chrome not reachable",
                "no such window",
                "target window already closed",
            )
        )

    try:
        with TempWorkspace(prefix="sitrax_frota_") as tmp:
            bot: Optional[SitraxBot] = None

            def _new_bot() -> SitraxBot:
                b = SitraxBot(
                    headless=True,
                    download_dir=tmp,
                    quiet=True,
                    low_memory=True,
                )
                b.start()
                b.login()
                return b

            def _kill_bot() -> None:
                nonlocal bot
                if bot is not None:
                    try:
                        bot.close()
                    except Exception:
                        pass
                    bot = None
                gc.collect()

            try:
                bot = _new_bot()
                bot.open_posicoes()
                bot.open_vehicle_selector()
                bot.load_vehicle_list()
                vehicles = bot.list_plates()
                try:
                    bot._d().execute_script(
                        "if (typeof hideModalSearchVeiculo === 'function') "
                        "hideModalSearchVeiculo();"
                    )
                except Exception:
                    pass

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

                vehicles = [
                    v
                    for v in vehicles
                    if v.get("placa")
                    and v["placa"].upper() not in ("TODOS", "TODAS", "ALL", "FROTA")
                ]
                # Últimas placas primeiro — eram as que mais falhavam (fim da lista/DOM)
                vehicles = list(reversed(vehicles))
                debug_session.step(
                    "frota_lista",
                    f"Frota: {len(vehicles)} veículo(s) — 1 a 1, "
                    f"ÚLTIMAS primeiro "
                    f"(reinicia Chrome a cada {RESTART_EVERY}): "
                    + ", ".join(v["placa"] for v in vehicles[:25]),
                    ok=True,
                    screenshot=False,
                )
                _JOB["message"] = f"Frota: 0/{len(vehicles)}"

                for i, v in enumerate(vehicles):
                    pl = v["placa"]
                    _JOB["message"] = f"Processando {i+1}/{len(vehicles)}: {pl}"
                    debug_session.step(
                        f"frota_{i+1}_de_{len(vehicles)}_{pl}",
                        f"1 a 1 → ({i+1}/{len(vehicles)}) {pl} "
                        "(Filter → scrape tabela → resumo)",
                        ok=True,
                        screenshot=False,
                    )

                    # reinicia Chrome periodicamente (memória)
                    if i > 0 and i % RESTART_EVERY == 0:
                        debug_session.step(
                            "frota_restart_chrome",
                            f"Reiniciando Chrome após {i} veículos (anti tab-crash)",
                            ok=True,
                            screenshot=False,
                        )
                        _kill_bot()
                        bot = _new_bot()

                    if bot is None or not bot.alive():
                        _kill_bot()
                        bot = _new_bot()

                    attempts = 0
                    while attempts < 2:
                        attempts += 1
                        try:
                            # Frota: prioriza SCRAPE (menos RAM que baixar PDF 700+ pts)
                            positions = bot.get_positions_for_plate(
                                pl, data_ini=d_ini, data_fim=d_fim
                            )
                            n_pts = len([p for p in positions if p.when])
                            debug_session.step(
                                f"frota_ok_{pl}",
                                f"{pl}: {n_pts} ponto(s) via tabela",
                                ok=True,
                                screenshot=False,
                            )
                            total_pontos += n_pts
                            textos.append(
                                build_narrative_report(
                                    pl,
                                    positions,
                                    data_ref=data_ref,
                                    cliente=v.get("cliente", ""),
                                )
                            )
                            one = build_summary_pdf_bytes(
                                pl,
                                positions,
                                data_ref=data_ref,
                                cliente=v.get("cliente", ""),
                            )
                            reader = PdfReader(io.BytesIO(one))
                            for page in reader.pages:
                                pdf_writer.add_page(page)
                            break
                        except Exception as e:
                            logger.exception("Falha em %s (tentativa %s)", pl, attempts)
                            if _is_crash(e) and attempts < 2:
                                debug_session.step(
                                    f"frota_crash_{pl}",
                                    f"Chrome caiu em {pl}; reiniciando e tentando de novo",
                                    ok=False,
                                    screenshot=False,
                                )
                                _kill_bot()
                                bot = _new_bot()
                                continue
                            textos.append(f"📋 {pl}: erro — {e}")
                            debug_session.step(
                                f"frota_erro_{pl}",
                                str(e),
                                ok=False,
                                screenshot=False,
                            )
                            if _is_crash(e):
                                _kill_bot()
                                bot = _new_bot()
                            break
            finally:
                _kill_bot()
                # Reaquece sessão permanente para testes de 1 placa
                try:
                    from app.bot.warm_pool import warm_pool

                    warm_pool.start(headless=True, low_memory=True)
                except Exception as e:
                    logger.warning("Reaquecer WarmPool após frota: %s", e)

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
        raise


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


@app.post("/gerar", response_class=HTMLResponse)
async def gerar(
    request: Request,
    modo: str = Form(...),
    placa: str = Form(""),
    data_ini: str = Form(""),
    data_fim: str = Form(""),
):
    """
    1 placa: espera o resultado (minutos).
    Todos (frota): roda em BACKGROUND — evita "upstream error" do proxy Railway
    em jobs longos; acompanhe /debug e volte na home para baixar.
    """
    import asyncio

    error = None
    texto = None
    has_pdf = False
    pdf_name = None
    job_msg = None

    if not sitrax_configured():
        error = "Servidor sem credenciais Sitrax (.env). Contate o administrador."
    elif modo == "placa" and not placa.strip():
        error = "Informe a placa do veículo (ou escolha Todos)."
    else:
        d_ini = parse_date(data_ini) or date.today()
        d_fim = parse_date(data_fim) or d_ini
        modo_n = (modo or "placa").lower()
        placa_u = (placa or "").strip().upper()
        if placa_u in ("TODOS", "TODAS", "ALL", "FROTA", "*"):
            modo_n = "todos"

        if modo_n == "todos":
            # BACKGROUND — frota demora 10–30+ min; proxy corta conexão síncrona
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
                        "Quando terminar, volte aqui e use Baixar PDF do resumo. "
                        "A página atualiza sozinha a cada 15s."
                    )
                else:
                    job_msg = "Não foi possível iniciar o job (ocupado)."
        else:
            # 1 placa — espera (ainda pode demorar 1–3 min)
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(
                    executor,
                    lambda: _run_job("placa", placa, d_ini, d_fim),
                )
                _store_result(result)
                texto = result.texto
                has_pdf = True
                pdf_name = result.pdf_filename
            except Exception as e:
                logger.exception("Erro ao gerar")
                error = f"{type(e).__name__}: {e}"

    # se job terminou enquanto a home abre, mostra resultado
    if not texto and _LAST_REPORT.get("texto") and not _JOB.get("running"):
        texto = _LAST_REPORT.get("texto")
        has_pdf = bool(_LAST_REPORT.get("pdf_bytes"))
        pdf_name = _LAST_REPORT.get("pdf_filename")

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
            "job_msg": job_msg,
            "job_running": bool(_JOB.get("running")),
            "job_status": _JOB.get("message") or "",
        },
    )


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
    except Exception as e:
        logger.exception("Warm testar %s: %s", placa_u, e)
    return RedirectResponse(url="/debug", status_code=303)


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
