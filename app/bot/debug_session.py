"""
Sessão de debug do robô — fotos + logs de cada passo.
Fica em memória no servidor para o painel /debug (calibração).
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


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


_lock = threading.Lock()
_LAST: Optional[DebugRun] = None
_CURRENT: Optional[DebugRun] = None


def start_run(placa: str = "") -> DebugRun:
    global _CURRENT, _LAST
    run = DebugRun(
        started=datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        placa=placa,
        status="running",
    )
    with _lock:
        _CURRENT = run
        _LAST = run
    logger.info("Debug run started placa=%s", placa)
    return run


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

    url = title = ""
    image_b64 = None
    html_snip = ""
    if driver is not None:
        try:
            url = driver.current_url or ""
        except Exception:
            pass
        try:
            title = driver.title or ""
        except Exception:
            pass
        if screenshot:
            try:
                png = driver.get_screenshot_as_png()
                image_b64 = base64.b64encode(png).decode("ascii")
            except Exception as e:
                message = f"{message} [screenshot falhou: {e}]"
        if html:
            try:
                src = driver.page_source or ""
                html_snip = src[:4000]
            except Exception:
                pass

    s = DebugStep(
        t=datetime.now().strftime("%H:%M:%S"),
        name=name,
        message=message,
        url=url,
        title=title,
        ok=ok,
        image_b64=image_b64,
        html_snip=html_snip,
    )
    with _lock:
        run.steps.append(s)
        # limita memória (últimos 40 passos)
        if len(run.steps) > 40:
            run.steps = run.steps[-40:]
    logger.info("DEBUG step %s: %s", name, message)


def finish_run(ok: bool = True, error: str = "") -> None:
    global _CURRENT, _LAST
    with _lock:
        run = _CURRENT
        if run:
            run.finished = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            run.status = "ok" if ok else "error"
            run.error = error or ""
            _LAST = run
            _CURRENT = None


def get_last_run() -> Optional[DebugRun]:
    with _lock:
        return _LAST
