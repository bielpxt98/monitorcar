"""
Sessão de debug do robô — passos + (opcional) fotos.
Fica em memória no servidor para o painel /debug.

Modo leve (DEBUG_LIGHT=1, padrão):
  - sem screenshots (exceto erros críticos se driver passar e light=False no step)
  - limita quantidade de passos e fotos
  - menos RAM → menos tab crash / mais chance de 2 Chromes
"""

from __future__ import annotations

import base64
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from app.config import settings as _settings
except Exception:  # pragma: no cover
    _settings = None


def _light() -> bool:
    if _settings is None:
        return True
    return bool(getattr(_settings, "debug_light", True))


def _max_steps() -> int:
    if _settings is None:
        return 40
    return max(10, int(getattr(_settings, "debug_max_steps", 40) or 40))


def _max_photos() -> int:
    if _settings is None:
        return 6
    return max(0, int(getattr(_settings, "debug_max_photos", 6) or 6))


@dataclass
class DebugStep:
    t: str
    name: str
    message: str
    url: str = ""
    title: str = ""
    ok: bool = True
    image_b64: Optional[str] = None  # PNG base64
    html_snip: str = ""


@dataclass
class DebugRun:
    started: str
    finished: str = ""
    placa: str = ""
    status: str = "running"  # running | ok | error
    error: str = ""
    steps: list[DebugStep] = field(default_factory=list)


@dataclass
class PlateVerify:
    """Comparação Sitrax (footer) x contagem da pesquisa."""

    t: str
    placa: str
    site_count: int
    scrape_count: int
    ok: bool
    image_b64: Optional[str] = None
    message: str = ""


_lock = threading.Lock()
_LAST: Optional[DebugRun] = None
_CURRENT: Optional[DebugRun] = None
_VERIFY: list[PlateVerify] = []


def start_run(placa: str = "") -> DebugRun:
    global _CURRENT, _LAST, _VERIFY
    run = DebugRun(
        started=datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        placa=placa,
        status="running",
    )
    with _lock:
        _CURRENT = run
        _LAST = run
        if (placa or "").upper() in ("FROTA", "TODOS", "ALL") or not placa:
            _VERIFY = []
    logger.info("Debug run started placa=%s light=%s", placa, _light())
    return run


def clear_verify() -> None:
    global _VERIFY
    with _lock:
        _VERIFY = []


def add_plate_verify(
    placa: str,
    site_count: int,
    scrape_count: int,
    *,
    driver: Any = None,
    message: str = "",
) -> PlateVerify:
    """Registra 1 placa: site N × pesquisa N (sem foto no modo leve)."""
    global _VERIFY
    site_count = max(0, int(site_count or 0))
    scrape_count = max(0, int(scrape_count or 0))
    if site_count == 0 and scrape_count == 0:
        ok = True
        status = "vazio OK"
    elif scrape_count == 0 and site_count > 0:
        ok = False
        status = "ERRO: site tem dados, pesquisa 0"
    elif site_count == scrape_count:
        ok = True
        status = "OK"
    elif site_count > 0 and abs(site_count - scrape_count) <= 1:
        ok = True
        status = "OK (~igual)"
    elif site_count > 0 and scrape_count >= max(1, int(site_count * 0.9)):
        ok = True
        status = "OK"
    else:
        ok = False
        status = "divergente"

    msg = message or f"Site: {site_count} · Pesquisa: {scrape_count} · {status}"
    entry = PlateVerify(
        t=datetime.now().strftime("%H:%M:%S"),
        placa=(placa or "").upper(),
        site_count=site_count,
        scrape_count=scrape_count,
        ok=ok,
        image_b64=None,
        message=msg,
    )
    with _lock:
        replaced = False
        for i, old in enumerate(_VERIFY):
            if old.placa == entry.placa:
                _VERIFY[i] = entry
                replaced = True
                break
        if not replaced:
            _VERIFY.append(entry)
        if len(_VERIFY) > 40:
            _VERIFY = _VERIFY[-40:]
    logger.info(
        "VERIFY %s: site=%s scrape=%s ok=%s",
        entry.placa,
        site_count,
        scrape_count,
        ok,
    )
    step(
        f"verify_{entry.placa}",
        msg,
        driver=None,
        ok=ok,
        screenshot=False,
    )
    return entry


def get_verify() -> list[PlateVerify]:
    with _lock:
        return list(_VERIFY)


def _trim_run_steps(run: DebugRun) -> None:
    """Mantém só os últimos N passos e no máx. M fotos (economiza RAM)."""
    max_s = _max_steps()
    max_p = _max_photos()
    if len(run.steps) > max_s:
        run.steps = run.steps[-max_s:]
    # remove fotos antigas se passar do limite
    with_img = [i for i, s in enumerate(run.steps) if s.image_b64]
    if len(with_img) > max_p:
        drop = with_img[: len(with_img) - max_p]
        for i in drop:
            run.steps[i].image_b64 = None


def step(
    name: str,
    message: str = "",
    *,
    driver: Any = None,
    ok: bool = True,
    screenshot: bool = True,
    html: bool = False,
) -> None:
    global _CURRENT
    with _lock:
        run = _CURRENT
    if run is None:
        return

    # modo leve: sem foto/HTML (salvo se for erro E screenshot explicitamente pedido
    # e DEBUG_LIGHT=0 — em light sempre desliga foto)
    if _light():
        screenshot = False
        html = False
        driver = None  # nem pede URL/title do driver se não precisa

    # mensagem curta
    if message and len(message) > 280:
        message = message[:277] + "…"

    url = title = ""
    image_b64 = None
    html_snip = ""
    if driver is not None:
        try:
            url = (driver.current_url or "")[:200]
        except Exception:
            pass
        try:
            title = (driver.title or "")[:120]
        except Exception:
            pass
        if screenshot:
            try:
                png = driver.get_screenshot_as_png()
                # redimensionar mentalmente: se > 400KB, descarta (RAM)
                if png and len(png) < 450_000:
                    image_b64 = base64.b64encode(png).decode("ascii")
                else:
                    message = f"{message} [foto grande omitida]"
            except Exception as e:
                message = f"{message} [screenshot falhou: {e}]"
        if html:
            try:
                src = driver.page_source or ""
                html_snip = src[:1500]
            except Exception:
                pass

    s = DebugStep(
        t=datetime.now().strftime("%H:%M:%S"),
        name=(name or "")[:60],
        message=message or "",
        url=url,
        title=title,
        ok=ok,
        image_b64=image_b64,
        html_snip=html_snip,
    )
    with _lock:
        run.steps.append(s)
        _trim_run_steps(run)
    logger.info("DEBUG step %s: %s", name, message)


def finish_run(ok: bool = True, error: str = "") -> None:
    global _CURRENT, _LAST
    with _lock:
        run = _CURRENT
        if run:
            run.finished = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            run.status = "ok" if ok else "error"
            run.error = (error or "")[:500]
            _trim_run_steps(run)
            _LAST = run
            _CURRENT = None


def get_last_run() -> Optional[DebugRun]:
    with _lock:
        return _LAST
