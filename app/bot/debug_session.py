"""
Sessão de debug do robô — fotos + logs de cada passo.
Fica em memória no servidor para o painel /debug (calibração).

Também guarda verificação por placa (site N vs pesquisa N) com foto,
limpando a cada nova busca de frota.
"""

from __future__ import annotations

import base64
import logging
import threading
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


@dataclass
class PlateVerify:
    """Comparação Sitrax (footer) x contagem da pesquisa + foto."""

    t: str
    placa: str
    site_count: int  # "Mostrando: N" / "Showing: N"
    scrape_count: int  # pontos lidos pelo robô
    ok: bool  # True se bate (ou vazio real)
    image_b64: Optional[str] = None
    message: str = ""


_lock = threading.Lock()
_LAST: Optional[DebugRun] = None
_CURRENT: Optional[DebugRun] = None
# Verificação da última frota/busca (substitui a cada nova)
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
        # nova frota / nova busca grande → limpa verificações antigas
        if (placa or "").upper() in ("FROTA", "TODOS", "ALL") or not placa:
            _VERIFY = []
    logger.info("Debug run started placa=%s", placa)
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
    """
    Registra 1 placa: quantos o site mostra x quantos a pesquisa leu.
    Foto opcional (recomendada). Substitui entrada da mesma placa se re-rodar.
    """
    global _VERIFY
    site_count = max(0, int(site_count or 0))
    # "Pesquisa" = registros (mesmo critério do rodapé), NÃO linhas cruas do DOM
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
    # Fotos desativadas na verificação (só tabela site × pesquisa)
    image_b64 = None

    entry = PlateVerify(
        t=datetime.now().strftime("%H:%M:%S"),
        placa=(placa or "").upper(),
        site_count=site_count,
        scrape_count=scrape_count,
        ok=ok,
        image_b64=image_b64,
        message=msg,
    )
    with _lock:
        # atualiza se mesma placa já existe (retry)
        replaced = False
        for i, old in enumerate(_VERIFY):
            if old.placa == entry.placa:
                _VERIFY[i] = entry
                replaced = True
                break
        if not replaced:
            _VERIFY.append(entry)
        # limite de segurança (frota grande)
        if len(_VERIFY) > 40:
            _VERIFY = _VERIFY[-40:]
    logger.info("VERIFY %s: site=%s scrape=%s ok=%s", entry.placa, site_count, scrape_count, ok)

    # também um step no log (sem encher com 13 fotos duplicadas se já tem verify)
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
        # passos de texto: mais folga; fotos grandes limitam via verify
        if len(run.steps) > 80:
            run.steps = run.steps[-80:]
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
