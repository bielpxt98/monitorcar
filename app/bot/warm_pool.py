"""
Sessão permanente do Chrome no Sitrax.

Objetivo (testes rápidos / Etapa 1):
  - Chrome sempre aberto
  - Login já feito
  - Parado em Posições com modal Veículos pronto
  - Cada placa reutiliza a mesma sessão e volta para Veículos no fim
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class WarmPool:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bot: Any = None
        self._tmp: Optional[Path] = None
        self.status: str = "off"  # off | starting | ready | busy | error
        self.message: str = "Desligado"
        self.started_at: str = ""
        self.last_error: str = ""
        self.last_plate: str = ""
        self.plates_ok: int = 0
        self.plates_err: int = 0

    # ——— status ———

    def snapshot(self) -> dict:
        with self._lock:
            alive = False
            try:
                alive = bool(self._bot and self._bot.alive())
            except Exception:
                alive = False
            return {
                "status": self.status,
                "message": self.message,
                "started_at": self.started_at,
                "last_error": self.last_error,
                "last_plate": self.last_plate,
                "plates_ok": self.plates_ok,
                "plates_err": self.plates_err,
                "alive": alive,
                "ready": self.status == "ready" and alive,
            }

    def is_ready(self) -> bool:
        return bool(self.snapshot().get("ready"))

    # ——— lifecycle ———

    def start(self, headless: bool = True, low_memory: bool = True) -> dict:
        """Abre Chrome, login, Posições → modal Veículos com lista."""
        with self._lock:
            return self._start_unlocked(headless=headless, low_memory=low_memory)

    def _start_unlocked(self, headless: bool = True, low_memory: bool = True) -> dict:
        from app.bot.sitrax import SitraxBot
        from app.bot import debug_session

        self.status = "starting"
        self.message = "Abrindo Chrome e fazendo login…"
        self.last_error = ""
        self._stop_unlocked(keep_status=True)

        debug_session.start_run(placa="WARM")
        tmp = Path(tempfile.mkdtemp(prefix="sitrax_warm_"))
        bot: Optional[SitraxBot] = None
        try:
            bot = SitraxBot(
                headless=headless,
                download_dir=tmp,
                quiet=True,
                low_memory=low_memory,
            )
            bot.start()
            bot._trace("warm_chrome", "Chrome iniciado (sessão permanente)", ok=True)
            bot.login()
            bot._trace("warm_login", "Login OK", ok=True)
            bot.open_posicoes()
            bot._trace("warm_posicoes", "Tela Posições", ok=True)
            bot.open_vehicle_selector()
            bot.load_vehicle_list()
            n = bot._count_vehicle_items()
            bot._trace(
                "warm_veiculos",
                f"Modal Veículos aberto — {n} item(ns). Aguardando placa.",
                ok=True,
            )
            self._bot = bot
            self._tmp = tmp
            self.status = "ready"
            self.message = f"Pronto em Veículos ({n} na lista). Mande a placa."
            self.started_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            debug_session.finish_run(ok=True)
            logger.info("WarmPool ready — %s veículos na lista", n)
            return self.snapshot()
        except Exception as e:
            logger.exception("WarmPool start falhou")
            self.last_error = str(e)
            self.status = "error"
            self.message = f"Falha ao aquecer: {e}"
            try:
                if bot is not None:
                    bot.close()
            except Exception:
                pass
            self._bot = None
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            self._tmp = None
            try:
                debug_session.finish_run(ok=False, error=str(e))
            except Exception:
                pass
            raise

    def stop(self) -> dict:
        with self._lock:
            self._stop_unlocked(keep_status=False)
            return self.snapshot()

    def _stop_unlocked(self, keep_status: bool = False) -> None:
        if self._bot is not None:
            try:
                self._bot.close()
            except Exception:
                pass
            self._bot = None
        if self._tmp is not None:
            try:
                shutil.rmtree(self._tmp, ignore_errors=True)
            except Exception:
                pass
            self._tmp = None
        if not keep_status:
            self.status = "off"
            self.message = "Desligado"
            self.started_at = ""

    def ensure_ready(self, headless: bool = True, low_memory: bool = True) -> None:
        with self._lock:
            alive = False
            try:
                alive = bool(self._bot and self._bot.alive())
            except Exception:
                alive = False
            if self.status == "ready" and alive:
                return
            logger.info("WarmPool ensure_ready: reaquecendo (status=%s alive=%s)", self.status, alive)
            self._start_unlocked(headless=headless, low_memory=low_memory)

    def _return_to_vehicles(self) -> None:
        bot = self._bot
        if bot is None:
            return
        try:
            bot.return_to_vehicles_ready()
            n = bot._count_vehicle_items()
            self.status = "ready"
            self.message = f"De volta em Veículos ({n} na lista). Próxima placa?"
        except Exception as e:
            logger.warning("return_to_vehicles falhou: %s — reaquecendo", e)
            try:
                self._start_unlocked()
            except Exception as e2:
                self.status = "error"
                self.message = f"Sessão perdida: {e2}"
                self.last_error = str(e2)
                raise

    # ——— consulta 1 placa (rápida) ———

    def run_plate(
        self,
        placa: str,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
        headless: bool = True,
    ):
        """
        Pesquisa 1 placa na sessão quente e volta para o modal Veículos.
        Retorna ReportResult (mesmo tipo do pipeline).
        """
        from app.bot.pipeline import report_from_positions
        from app.bot.report import positions_from_rows
        from app.bot import debug_session
        from app.bot.pdf_parser import positions_from_pdf

        placa_u = (placa or "").strip().upper()
        if not placa_u:
            raise ValueError("Informe a placa.")

        data_ini = data_ini or date.today()
        data_fim = data_fim or data_ini
        data_ref = data_ini.strftime("%d/%m/%Y")
        if data_fim != data_ini:
            data_ref += f" a {data_fim.strftime('%d/%m/%Y')}"

        with self._lock:
            self.ensure_ready(headless=headless, low_memory=True)
            bot = self._bot
            assert bot is not None
            self.status = "busy"
            self.message = f"Pesquisando {placa_u}…"
            self.last_plate = placa_u

            debug_session.start_run(placa=placa_u)
            t0 = time.time()
            try:
                bot.prepare_historico_warm(placa_u, data_ini, data_fim)
                debug_session.step(
                    "warm_filtrado",
                    f"{placa_u} filtrado na sessão quente",
                    ok=True,
                    screenshot=False,
                )

                result = None
                # Preferência: tabela (mais leve). PDF se tabela vazia.
                bot.try_scroll_all()
                rows = bot.scrape_positions_table()
                positions = positions_from_rows(rows)
                n_pts = len([p for p in positions if p.when])
                debug_session.step(
                    "warm_tabela",
                    f"{placa_u}: {n_pts} ponto(s) via tabela",
                    ok=n_pts > 0,
                    screenshot=False,
                )

                if n_pts > 0:
                    result = report_from_positions(
                        placa_u, positions, data_ref=data_ref
                    )
                else:
                    # tenta PDF como fallback
                    try:
                        pdf = bot.download_historico_pdf(
                            placa_u,
                            data_ini=data_ini,
                            data_fim=data_fim,
                            dest_dir=self._tmp,
                            already_filtered=True,
                        )
                        if pdf and Path(pdf).exists():
                            _, pdf_pos = positions_from_pdf(pdf)
                            if pdf_pos:
                                result = report_from_positions(
                                    placa_u, pdf_pos, data_ref=data_ref
                                )
                                debug_session.step(
                                    "warm_pdf",
                                    f"{placa_u}: {len(pdf_pos)} pts via PDF",
                                    ok=True,
                                    screenshot=False,
                                )
                    except Exception as e:
                        logger.warning("Warm PDF fallback: %s", e)

                    if result is None:
                        result = report_from_positions(
                            placa_u, positions, data_ref=data_ref
                        )

                elapsed = time.time() - t0
                self.plates_ok += 1
                debug_session.finish_run(ok=True)
                logger.info(
                    "WarmPool %s ok em %.1fs (%s pts)",
                    placa_u,
                    elapsed,
                    result.pontos,
                )
                return result
            except Exception as e:
                self.plates_err += 1
                self.last_error = str(e)
                debug_session.finish_run(ok=False, error=str(e))
                logger.exception("WarmPool falha em %s", placa_u)
                raise
            finally:
                # limpa downloads do temp da sessão (mantém pasta)
                if self._tmp and self._tmp.exists():
                    for f in self._tmp.iterdir():
                        try:
                            if f.is_file():
                                f.unlink()
                        except Exception:
                            pass
                try:
                    self._return_to_vehicles()
                except Exception as e:
                    logger.warning("Pós-placa não voltou a Veículos: %s", e)
                    if self.status != "error":
                        self.status = "error"
                        self.message = f"Não voltou a Veículos: {e}"


# singleton do processo
warm_pool = WarmPool()
