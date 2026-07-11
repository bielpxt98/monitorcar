"""
Pool de Chromes permanentes no Sitrax.

- Sempre 2 navegadores logados, parados em Posições/Veículos.
- 1 placa: usa um dos dois (livre) e devolve a Veículos.
- Todos (frota): empresta os 2 em paralelo; no fim ambos voltam a aguardar.
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

PERMANENT_SLOTS = 2
_LOGIN_LOCK = threading.Lock()


class WarmSlot:
    def __init__(self, slot_id: int) -> None:
        self.slot_id = slot_id
        self.bot: Any = None
        self.tmp: Optional[Path] = None
        self.status: str = "off"  # off | starting | ready | busy | error
        self.message: str = "Desligado"
        self.starting_since: float = 0.0
        self.lock = threading.RLock()

    def alive(self) -> bool:
        try:
            return bool(self.bot and self.bot.alive())
        except Exception:
            return False

    def stuck_starting(self, max_sec: float = 120.0) -> bool:
        if self.status != "starting" or not self.starting_since:
            return False
        return (time.time() - self.starting_since) > max_sec


class WarmPool:
    def __init__(self, n_slots: int = PERMANENT_SLOTS) -> None:
        self._lock = threading.RLock()
        self._slots = [WarmSlot(i) for i in range(max(1, n_slots))]
        self.started_at: str = ""
        self.last_error: str = ""
        self.last_plate: str = ""
        self.plates_ok: int = 0
        self.plates_err: int = 0
        self._rr: int = 0

    # ——— status ———

    def snapshot(self) -> dict:
        with self._lock:
            slots_info = []
            ready_n = 0
            busy_n = 0
            starting_n = 0
            for s in self._slots:
                al = s.alive()
                st = s.status
                if st == "starting" and s.stuck_starting(90):
                    st = "error"
                    s.status = "error"
                    s.message = (
                        f"Chrome {s.slot_id + 1}: travou ao abrir "
                        f"(>{int(time.time() - s.starting_since)}s)"
                    )
                    self.last_error = s.message
                if st == "ready" and al:
                    ready_n += 1
                if st == "busy" and al:
                    busy_n += 1
                if st == "starting":
                    starting_n += 1
                slots_info.append(
                    {
                        "id": s.slot_id + 1,
                        "status": st,
                        "message": s.message,
                        "alive": al,
                        "ready": st == "ready" and al,
                    }
                )
            if ready_n == len(self._slots):
                status, msg = (
                    "ready",
                    f"{ready_n}/{len(self._slots)} Chromes prontos em Veículos",
                )
            elif starting_n and ready_n:
                status, msg = (
                    "partial",
                    f"{ready_n} pronto(s), {starting_n} abrindo…",
                )
            elif ready_n + busy_n > 0:
                status, msg = (
                    "partial",
                    f"{ready_n} prontos · {busy_n} ocupados / {len(self._slots)}",
                )
            elif starting_n:
                status, msg = "starting", "Aquecendo Chromes permanentes…"
            elif any(s.status == "error" for s in self._slots):
                status, msg = "error", self.last_error or "Erro no pool"
            else:
                status, msg = "off", "Desligado"
            return {
                "status": status,
                "message": msg,
                "started_at": self.started_at,
                "last_error": self.last_error,
                "last_plate": self.last_plate,
                "plates_ok": self.plates_ok,
                "plates_err": self.plates_err,
                "alive": ready_n + busy_n > 0,
                "ready": ready_n >= 1,
                "ready_count": ready_n,
                "slot_count": len(self._slots),
                "slots": slots_info,
            }

    def is_ready(self) -> bool:
        return bool(self.snapshot().get("ready"))

    # ——— lifecycle ———

    def start(self, headless: bool = True, low_memory: bool = True) -> dict:
        """Garante os 2 Chromes permanentes prontos (com retry no 2º)."""
        return self.ensure_both(headless=headless, low_memory=low_memory)

    def ensure_both(self, headless: bool = True, low_memory: bool = True) -> dict:
        """
        Sobe os 2 em sequência (Chrome 1 → espera → Chrome 2).
        Se o 2 falhar/travar, tenta mais 1 vez.
        """
        # 1º Chrome
        try:
            self._ensure_slot(0, headless=headless, low_memory=low_memory)
        except Exception as e:
            logger.exception("Falha ao aquecer slot 1: %s", e)
            self.last_error = str(e)

        # pequena folga de RAM/sessão antes do 2º
        time.sleep(4.0)

        # 2º Chrome (até 2 tentativas)
        for attempt in range(2):
            try:
                self._ensure_slot(1, headless=headless, low_memory=low_memory)
                if self._slots[1].alive() and self._slots[1].status == "ready":
                    break
            except Exception as e:
                logger.exception(
                    "Falha ao aquecer slot 2 (tentativa %s): %s", attempt + 1, e
                )
                self.last_error = str(e)
                with self._slots[1].lock:
                    self._close_slot_unlocked(self._slots[1], keep_status=True)
                    self._slots[1].status = "error"
                    self._slots[1].message = f"Chrome 2: falha — {e}"
                if attempt == 0:
                    time.sleep(5.0)

        if not self.started_at and any(s.alive() for s in self._slots):
            self.started_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        return self.snapshot()

    def _ensure_slot(
        self, idx: int, headless: bool = True, low_memory: bool = True
    ) -> None:
        slot = self._slots[idx]
        with slot.lock:
            if slot.alive() and slot.status in ("ready", "busy"):
                return
            # travou em "starting" → limpa e tenta de novo
            if slot.status == "starting" and slot.stuck_starting(90):
                logger.warning(
                    "Slot %s stuck starting — forçando restart", idx + 1
                )
                self._close_slot_unlocked(slot, keep_status=True)
            elif slot.status == "starting" and not slot.stuck_starting(90):
                # outro thread ainda abrindo — não invade
                return
            self._boot_slot_unlocked(slot, headless=headless, low_memory=low_memory)

    def _boot_slot_unlocked(
        self, slot: WarmSlot, headless: bool = True, low_memory: bool = True
    ) -> None:
        from app.bot.sitrax import SitraxBot
        from app.bot import debug_session

        self._close_slot_unlocked(slot, keep_status=True)
        slot.status = "starting"
        slot.starting_since = time.time()
        slot.message = f"Chrome {slot.slot_id + 1}: abrindo…"
        # Chrome 2 espera o 1 estabilizar (sessão Sitrax + RAM)
        if slot.slot_id > 0:
            time.sleep(5.0)

        tmp = Path(tempfile.mkdtemp(prefix=f"sitrax_warm{slot.slot_id + 1}_"))
        bot: Optional[Any] = None
        try:
            with _LOGIN_LOCK:
                bot = SitraxBot(
                    headless=headless,
                    download_dir=tmp,
                    quiet=True,
                    low_memory=True,  # sempre low mem no permanente
                )
                slot.message = f"Chrome {slot.slot_id + 1}: iniciando browser…"
                bot.start()
                slot.message = f"Chrome {slot.slot_id + 1}: login…"
                bot.login()
                time.sleep(1.2)
            slot.message = f"Chrome {slot.slot_id + 1}: Posições…"
            bot.open_posicoes()
            bot._sleep(0.8)
            bot.open_vehicle_selector()
            bot.load_vehicle_list()
            n = bot._count_vehicle_items()
            slot.bot = bot
            slot.tmp = tmp
            slot.status = "ready"
            slot.starting_since = 0.0
            slot.message = f"Chrome {slot.slot_id + 1}: Veículos ({n})"
            debug_session.step(
                f"warm{slot.slot_id + 1}_pronto",
                f"Chrome permanente {slot.slot_id + 1} em Veículos ({n} itens)",
                ok=True,
                screenshot=False,
            )
            logger.info("Warm slot %s ready (%s veículos)", slot.slot_id + 1, n)
        except Exception as e:
            slot.status = "error"
            slot.starting_since = 0.0
            slot.message = f"Chrome {slot.slot_id + 1}: falha — {e}"
            self.last_error = str(e)
            try:
                if bot is not None:
                    bot.close()
            except Exception:
                pass
            slot.bot = None
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            slot.tmp = None
            raise

    def stop(self) -> dict:
        with self._lock:
            for slot in self._slots:
                with slot.lock:
                    self._close_slot_unlocked(slot, keep_status=False)
            self.started_at = ""
            return self.snapshot()

    def _close_slot_unlocked(self, slot: WarmSlot, keep_status: bool = False) -> None:
        if slot.bot is not None:
            try:
                slot.bot.close()
            except Exception:
                pass
            slot.bot = None
        if slot.tmp is not None:
            try:
                shutil.rmtree(slot.tmp, ignore_errors=True)
            except Exception:
                pass
            slot.tmp = None
        if not keep_status:
            slot.status = "off"
            slot.message = "Desligado"

    def ensure_ready(self, headless: bool = True, low_memory: bool = True) -> None:
        """Compat: pelo menos 1 pronto (preferência: os 2)."""
        self.ensure_both(headless=headless, low_memory=low_memory)

    def _return_slot_to_vehicles(self, slot: WarmSlot) -> None:
        if not slot.alive():
            try:
                self._boot_slot_unlocked(slot)
            except Exception as e:
                slot.status = "error"
                slot.message = str(e)
                raise
            return
        try:
            slot.bot.return_to_vehicles_ready()
            n = slot.bot._count_vehicle_items()
            slot.status = "ready"
            slot.message = f"Chrome {slot.slot_id + 1}: Veículos ({n})"
        except Exception as e:
            logger.warning(
                "Slot %s return_to_vehicles: %s — reaquecendo",
                slot.slot_id + 1,
                e,
            )
            try:
                self._boot_slot_unlocked(slot)
            except Exception as e2:
                slot.status = "error"
                slot.message = str(e2)
                self.last_error = str(e2)
                raise

    # ——— 1 placa ———

    def _acquire_slot(self) -> WarmSlot:
        """Pega um slot ready (round-robin)."""
        self.ensure_both()
        with self._lock:
            n = len(self._slots)
            for off in range(n):
                idx = (self._rr + off) % n
                slot = self._slots[idx]
                with slot.lock:
                    if slot.alive() and slot.status == "ready":
                        slot.status = "busy"
                        slot.message = f"Chrome {slot.slot_id + 1}: pesquisando…"
                        self._rr = (idx + 1) % n
                        return slot
            # nenhum ready: força o primeiro
            slot = self._slots[0]
            with slot.lock:
                if not slot.alive():
                    self._boot_slot_unlocked(slot)
                slot.status = "busy"
                return slot

    def run_plate(
        self,
        placa: str,
        data_ini: Optional[date] = None,
        data_fim: Optional[date] = None,
        headless: bool = True,
    ):
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

        slot = self._acquire_slot()
        bot = slot.bot
        assert bot is not None
        self.last_plate = placa_u
        debug_session.start_run(placa=placa_u)
        t0 = time.time()
        try:
            rows = bot.fetch_positions_for_fleet_plate(
                placa_u,
                data_ini=data_ini,
                data_fim=data_fim,
                clear_previous=True,
            )
            positions = positions_from_rows(rows)
            n_pts = len([p for p in positions if p.when])
            debug_session.step(
                "warm_tabela",
                f"Chrome {slot.slot_id + 1}: {placa_u} → {n_pts} pts",
                ok=True,
                screenshot=False,
            )
            result = None
            if n_pts > 0:
                result = report_from_positions(placa_u, positions, data_ref=data_ref)
            else:
                try:
                    pdf = bot.download_historico_pdf(
                        placa_u,
                        data_ini=data_ini,
                        data_fim=data_fim,
                        dest_dir=slot.tmp,
                        already_filtered=True,
                    )
                    if pdf and Path(pdf).exists():
                        _, pdf_pos = positions_from_pdf(pdf)
                        if pdf_pos:
                            result = report_from_positions(
                                placa_u, pdf_pos, data_ref=data_ref
                            )
                except Exception as e:
                    logger.warning("Warm PDF fallback: %s", e)
                if result is None:
                    result = report_from_positions(
                        placa_u, positions, data_ref=data_ref
                    )

            self.plates_ok += 1
            debug_session.finish_run(ok=True)
            logger.info(
                "Warm C%s %s ok em %.1fs (%s pts)",
                slot.slot_id + 1,
                placa_u,
                time.time() - t0,
                result.pontos,
            )
            return result
        except Exception as e:
            self.plates_err += 1
            self.last_error = str(e)
            debug_session.finish_run(ok=False, error=str(e))
            logger.exception("Warm C%s falha em %s", slot.slot_id + 1, placa_u)
            raise
        finally:
            if slot.tmp and slot.tmp.exists():
                for f in slot.tmp.iterdir():
                    try:
                        if f.is_file():
                            f.unlink()
                    except Exception:
                        pass
            try:
                with slot.lock:
                    self._return_slot_to_vehicles(slot)
            except Exception as e:
                logger.warning("Pós-placa slot %s: %s", slot.slot_id + 1, e)

    # ——— frota: empresta os 2 ———

    def borrow_for_fleet(self) -> list[tuple[int, Any]]:
        """
        Garante 2 Chromes e marca busy.
        Retorna [(worker_id, bot), ...] para o fleet_workers.
        """
        self.ensure_both()
        out: list[tuple[int, Any]] = []
        with self._lock:
            for slot in self._slots:
                with slot.lock:
                    if not slot.alive():
                        try:
                            self._boot_slot_unlocked(slot)
                        except Exception:
                            continue
                    slot.status = "busy"
                    slot.message = f"Chrome {slot.slot_id + 1}: frota…"
                    out.append((slot.slot_id, slot.bot))
        return out

    def replace_fleet_bot(self, worker_id: int, bot: Any) -> None:
        """Atualiza o bot do slot após crash/restart na frota."""
        if worker_id < 0 or worker_id >= len(self._slots):
            return
        slot = self._slots[worker_id]
        with slot.lock:
            # não fecha o bot antigo se for o mesmo
            if slot.bot is not bot and slot.bot is not None:
                try:
                    if slot.bot is not bot:
                        # old already closed by fleet worker
                        pass
                except Exception:
                    pass
            slot.bot = bot
            slot.status = "busy"
            slot.message = f"Chrome {slot.slot_id + 1}: frota (reiniciado)"

    def release_after_fleet(self) -> dict:
        """Devolve os 2 a Veículos (permanentes continuam ligados)."""
        for slot in self._slots:
            try:
                with slot.lock:
                    self._return_slot_to_vehicles(slot)
            except Exception as e:
                logger.warning("release slot %s: %s", slot.slot_id + 1, e)
        return self.snapshot()

    def list_fleet_plates(self) -> list[dict]:
        """Usa Chrome 1 permanente para listar a frota (sem Chrome extra)."""
        self.ensure_both()
        slot = self._slots[0]
        with slot.lock:
            if not slot.alive():
                self._boot_slot_unlocked(slot)
            bot = slot.bot
            assert bot is not None
            was = slot.status
            slot.status = "busy"
            try:
                if not bot._on_posicoes_screen():
                    bot.open_posicoes()
                bot._close_date_popup_if_open()
                if not bot._vehicle_modal_open():
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
                return vehicles
            finally:
                try:
                    self._return_slot_to_vehicles(slot)
                except Exception:
                    slot.status = was


warm_pool = WarmPool(n_slots=PERMANENT_SLOTS)
