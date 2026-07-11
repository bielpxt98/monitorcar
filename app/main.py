"""
Site mobile (nuvem):
  - Login do app (cadeado: usuário/senha)
  - Escolhe: 1 placa ou todos
  - Gera 1 PDF-resumo
  - PDFs brutos do Sitrax ficam só no TEMP do servidor e são apagados
  - Cancelar pesquisa em andamento
"""

from __future__ import annotations

import logging
import secrets
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sitrax-bot")

BASE = Path(__file__).resolve().parent
# 2: após cancel, a thread morta pode limpar enquanto a nova já sobe
executor = ThreadPoolExecutor(max_workers=2)

# rotas abertas sem login
_PUBLIC_PREFIXES = (
    "/login",
    "/logout",
    "/static",
    "/health",
    "/favicon.ico",
)


def sitrax_configured() -> bool:
    return bool(
        settings.sitrax_cliente and settings.sitrax_usuario and settings.sitrax_senha
    )


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("auth_user"))


def check_app_credentials(user: str, password: str) -> bool:
    u = (user or "").strip()
    p = password or ""
    return (
        secrets.compare_digest(u, settings.app_user)
        and secrets.compare_digest(p, settings.app_password)
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Ao subir: 1 Chrome permanente em Veículos + keeper que religa sozinho.
    Não precisa clicar em “Ligar” no /debug.
    """
    if sitrax_configured():

        def _warm():
            try:
                from app.bot.warm_pool import warm_pool

                logger.info(
                    "Auto-start: 1 Chrome permanente (login → Veículos)…"
                )
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
    "cancel": False,
    "id": 0,  # gera a cada start; cancel/nova busca invalidam a thread antiga
}
_JOB_ID_SEQ = 0
# flash one-shot (POST /gerar → redirect /) para a URL voltar à home
_FLASH: dict = {"error": None, "job_msg": None, "placa": None}


def job_cancelled() -> bool:
    return bool(_JOB.get("cancel"))


def _force_cancel_now(message: str = "Pesquisa cancelada. Pode buscar outra placa.") -> None:
    """Marca cancel + libera UI na hora + mata Chrome ocupado."""
    global _JOB_ID_SEQ
    # invalida a thread antiga para ela não sobrescrever a próxima busca
    _JOB_ID_SEQ += 1
    _JOB["id"] = _JOB_ID_SEQ
    _JOB["cancel"] = True
    _JOB["running"] = False
    _JOB["done"] = True
    _JOB["error"] = ""
    _JOB["message"] = message
    try:
        from app.bot.warm_pool import warm_pool

        warm_pool.force_abort()
    except Exception as e:
        logger.warning("force_abort no cancel: %s", e)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path or "/"
    if any(path == p or path.startswith(p + "/") for p in _PUBLIC_PREFIXES):
        return await call_next(request)
    if path.startswith("/static"):
        return await call_next(request)
    if is_logged_in(request):
        return await call_next(request)
    # APIs JSON → 401
    if path.startswith("/api/") or path in (
        "/job-status",
        "/warm/status",
        "/baixar-resumo",
    ):
        return JSONResponse(
            {"ok": False, "error": "Não autenticado. Faça login."},
            status_code=401,
        )
    nxt = path
    if request.url.query:
        nxt = f"{path}?{request.url.query}"
    return RedirectResponse(
        url=f"/login?next={quote(nxt, safe='')}",
        status_code=303,
    )


# SessionMiddleware por último = executa primeiro no request (sessão pronta no auth)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="resumo_rota_sess",
    max_age=60 * 60 * 24 * 14,  # 14 dias
    same_site="lax",
    https_only=False,
)


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


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if is_logged_in(request):
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": None,
            "next": next or "/",
        },
    )


@app.post("/login")
async def login_submit(
    request: Request,
    usuario: str = Form(""),
    senha: str = Form(""),
    next: str = Form("/"),
):
    if check_app_credentials(usuario, senha):
        request.session["auth_user"] = (usuario or "").strip()
        dest = next if next and next.startswith("/") and not next.startswith("//") else "/"
        logger.info("Login app OK: %s", request.session["auth_user"])
        return RedirectResponse(url=dest, status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": "Usuário ou senha incorretos.",
            "next": next or "/",
        },
        status_code=401,
    )


@app.get("/logout")
@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    flash = _pop_flash()
    job_err = (
        _JOB.get("error")
        if _JOB.get("done") and not _LAST_REPORT.get("pdf_bytes")
        else None
    )
    verify = []
    try:
        from app.bot.debug_session import get_verify

        verify = [
            {
                "placa": v.placa,
                "site_count": v.site_count,
                "scrape_count": v.scrape_count,
                "ok": v.ok,
                "message": v.message,
                "t": v.t,
                "image_b64": v.image_b64,
            }
            for v in get_verify()
        ]
    except Exception:
        pass
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
            "verify": verify,
            "auth_user": request.session.get("auth_user") or "",
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

    # ——— TODOS: 1 Chrome permanente (estável; mais pontos completos) ———
    from app.bot.summary_pdf import build_summary_pdf_bytes, safe_filename
    from app.bot.fleet_workers import run_fleet_parallel, DEFAULT_WORKERS
    from app.bot.warm_pool import warm_pool
    from pypdf import PdfWriter, PdfReader
    import io

    if job_cancelled():
        raise JobCancelled("Cancelado pelo usuário")

    debug_session.start_run(placa="FROTA")
    textos: list[str] = []
    pdf_writer = PdfWriter()
    total_pontos = 0
    N_WORKERS = DEFAULT_WORKERS  # 1

    try:
        with TempWorkspace(prefix="sitrax_frota_") as tmp:
            if job_cancelled():
                raise JobCancelled("Cancelado pelo usuário")
            borrowed = warm_pool.borrow_for_fleet()
            _JOB["message"] = "Frota: listando placas (1 Chrome estável)…"

            bot0 = borrowed[0][1] if borrowed else None
            if bot0 is None:
                raise RuntimeError("Nenhum Chrome permanente disponível para frota.")
            vehicles = warm_pool.list_plates_on_bot(bot0)
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

            debug_session.step(
                "frota_lista",
                f"Frota: {len(vehicles)} veículo(s) — 1 Chrome permanente "
                f"(sequencial, X entre placas, reinicia só se travar): "
                + ", ".join(v["placa"] for v in vehicles[:25]),
                ok=True,
                screenshot=False,
            )
            _JOB["message"] = f"Frota: 0/{len(vehicles)} — 1 Chrome estável…"

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
                    cancel_check=job_cancelled,
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

            # resumo verificação site x pesquisa (para home)
            try:
                ver = debug_session.get_verify()
                if ver:
                    lines_v = ["📊 Verificação Sitrax × pesquisa:"]
                    for v in ver:
                        mark = "✅" if v.ok else "❌"
                        lines_v.append(
                            f"  {mark} {v.placa}: site {v.site_count} · "
                            f"pesquisa {v.scrape_count}"
                        )
                    textos.insert(0, "\n".join(lines_v))
            except Exception:
                pass

        if job_cancelled():
            # devolve o que já coletou (parcial) e marca cancelado
            if textos:
                out = io.BytesIO()
                if len(pdf_writer.pages) > 0:
                    pdf_writer.write(out)
                    pdf_b = out.getvalue()
                else:
                    pdf_b = build_summary_pdf_bytes("FROTA", [], data_ref=data_ref)
                _store_result(
                    ReportResult(
                        placa="FROTA",
                        data_ref=data_ref,
                        texto="⏹ Cancelado.\n\n" + "\n\n".join(textos),
                        pdf_bytes=pdf_b,
                        pdf_filename=safe_filename("FROTA", data_ref),
                        pontos=total_pontos,
                    )
                )
            raise JobCancelled("Cancelado pelo usuário")

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


class JobCancelled(Exception):
    """Pesquisa cancelada pelo usuário."""


def _start_bg_job(modo: str, placa: str, d_ini: date, d_fim: date) -> bool:
    """Inicia job em background. False se já houver um rodando."""
    if _JOB.get("running"):
        return False

    global _JOB_ID_SEQ
    _JOB_ID_SEQ += 1
    my_id = _JOB_ID_SEQ

    def work():
        _JOB.update(
            {
                "running": True,
                "done": False,
                "cancel": False,
                "error": "",
                "modo": modo,
                "placa": placa,
                "started": datetime.now().strftime("%H:%M:%S"),
                "message": "Iniciando…",
                "id": my_id,
            }
        )

        def _still_mine() -> bool:
            return _JOB.get("id") == my_id

        try:
            if job_cancelled() or not _still_mine():
                raise JobCancelled("Cancelado antes de iniciar")
            result = _run_job(modo, placa, d_ini, d_fim)
            if not _still_mine():
                return  # cancelou / outra busca já mandou
            if job_cancelled():
                _JOB["message"] = (
                    "Pesquisa cancelada. Pode buscar outra placa."
                )
                _JOB["error"] = ""
                _JOB["done"] = True
                return
            _store_result(result)
            _JOB["message"] = (
                f"Concluído — {result.placa} ({result.pontos} pontos). "
                "Baixe o PDF na home."
            )
            _JOB["done"] = True
        except JobCancelled as e:
            logger.info("Job cancelado: %s", e)
            if _still_mine():
                _JOB["error"] = ""
                _JOB["message"] = "Pesquisa cancelada. Pode buscar outra placa."
                _JOB["done"] = True
            try:
                from app.bot.warm_pool import warm_pool

                warm_pool.release_after_fleet()
            except Exception:
                pass
        except Exception as e:
            # driver morto por force_abort → trata como cancel
            err_l = str(e).lower()
            aborted = (
                job_cancelled()
                or "cancelad" in err_l
                or "invalid session" in err_l
                or "session deleted" in err_l
                or "no such window" in err_l
                or "chrome not reachable" in err_l
                or "disconnected" in err_l
            )
            if not _still_mine():
                return
            if aborted:
                logger.info("Job interrompido: %s", e)
                _JOB["error"] = ""
                _JOB["message"] = "Pesquisa cancelada. Pode buscar outra placa."
            else:
                logger.exception("Job background falhou")
                _JOB["error"] = f"{type(e).__name__}: {e}"
                _JOB["message"] = f"Erro: {e}"
            _JOB["done"] = True
        finally:
            if _still_mine():
                _JOB["running"] = False
                _JOB["cancel"] = False

    executor.submit(work)
    return True


@app.post("/cancelar")
async def cancelar_pesquisa(request: Request):
    """Cancela na hora: libera busca e mata o Chrome da pesquisa atual."""
    if not _JOB.get("running") and not _JOB.get("cancel"):
        _FLASH["job_msg"] = "Nenhuma pesquisa em andamento."
        return RedirectResponse(url="/", status_code=303)
    logger.info(
        "Cancelamento IMEDIATO (job desde %s)", _JOB.get("started")
    )
    _force_cancel_now()
    _FLASH["job_msg"] = (
        "Pesquisa cancelada. Pode buscar outra placa agora."
    )
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/cancelar")
async def api_cancelar():
    if not _JOB.get("running") and not _JOB.get("cancel"):
        return {"ok": False, "error": "Nenhuma pesquisa em andamento"}
    _force_cancel_now()
    return {"ok": True, "message": _JOB["message"]}


@app.post("/gerar")
async def gerar(
    request: Request,
    modo: str = Form(...),
    placa: str = Form(""),
    data_ini: str = Form(""),
    data_fim: str = Form(""),
):
    """
    1 placa ou frota: roda em BACKGROUND e redireciona para / —
    evita upstream error e permite Cancelar.
    """
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

        if _JOB.get("running"):
            job_msg = (
                f"Já há uma pesquisa em andamento desde {_JOB.get('started')}: "
                f"{_JOB.get('message')}. Use Cancelar para parar e buscar outra."
            )
        else:
            ok = _start_bg_job(modo_n, placa, d_ini, d_fim)
            if ok:
                if modo_n == "todos":
                    job_msg = (
                        "Frota iniciada em segundo plano. "
                        "Acompanhe em /debug. Pode cancelar a qualquer momento "
                        "para buscar outra placa. A página atualiza sozinha."
                    )
                else:
                    job_msg = (
                        f"Buscando {placa_u or 'placa'}… "
                        "Pode cancelar se demorar e buscar outra. "
                        "A página atualiza sozinha."
                    )
            else:
                job_msg = "Não foi possível iniciar a pesquisa (ocupado)."

    _FLASH["error"] = error
    _FLASH["job_msg"] = job_msg
    _FLASH["placa"] = placa_u if placa_u else None
    # URL volta para a home (não fica em /gerar)
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/placas")
async def api_placas():
    """
    Lista placas conhecidas (cache da última listagem no Sitrax).
    Usado pelo botão (i) na home para o usuário escolher e pesquisar.
    """
    plates: list = []
    source = "empty"
    try:
        from app.bot.warm_pool import warm_pool

        plates = warm_pool.get_plates_cache()
        if plates:
            source = "cache"
    except Exception:
        pass
    # fallback: placas da última verificação de frota
    if not plates:
        try:
            from app.bot.debug_session import get_verify

            seen = set()
            for v in get_verify():
                pl = (v.placa or "").upper()
                if pl and pl not in seen:
                    seen.add(pl)
                    plates.append({"placa": pl, "display": "", "cliente": ""})
            if plates:
                source = "verify"
        except Exception:
            pass
    return {
        "plates": plates,
        "source": source,
        "count": len(plates),
    }


@app.get("/job-status")
async def job_status():
    return {
        "running": bool(_JOB.get("running")),
        "done": bool(_JOB.get("done")),
        "cancel": bool(_JOB.get("cancel")),
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
    from app.bot.debug_session import get_last_run, get_verify
    from app.bot.warm_pool import warm_pool

    run = get_last_run()
    verify = get_verify()
    return templates.TemplateResponse(
        request,
        "debug.html",
        {
            "run": run,
            "configured": sitrax_configured(),
            "warm": warm_pool.snapshot(),
            "today": date.today().isoformat(),
            "verify": verify,
        },
    )


@app.get("/warm/status")
async def warm_status():
    from app.bot.warm_pool import warm_pool

    return warm_pool.snapshot()


@app.post("/warm/start")
async def warm_start():
    """Liga os 2 Chromes permanentes (em background — página atualiza sozinha)."""
    if not sitrax_configured():
        return RedirectResponse(url="/debug", status_code=303)

    def job():
        try:
            from app.bot.warm_pool import warm_pool

            warm_pool.start(headless=True, low_memory=True)
        except Exception as e:
            logger.exception("Warm start: %s", e)

    # não espera os 2 terminarem — evita “travar” a tela em 1/2
    executor.submit(job)
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
