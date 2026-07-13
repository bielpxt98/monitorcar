"""
Pool de Chromes permanentes no Sitrax.

- Chrome 1: principal (1 placa e Todos).
- Chrome 2: standby em Veículos — só entra no Todos se o 1 cair.
- Entre placas: X no chip → próxima (sem login).
- Só reabre se a aba travar (tab crashed).
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

# 2 Chromes: 1 ativo + 1 standby (failover no Todos)
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
        self._keeper_stop = threading.Event()
        self._keeper_thread: Optional[threading.Thread] = None
        self._fleet_busy = False  # True enquanto Todos usa os Chromes
        # cache de placas (para UI escolher placa sem abrir Chrome de novo)
        self._plates_cache: list[dict] = []
        # carrega placas salvas no disco (botão i)
        try:
            from app.bot.plates_store import load_plates

            saved = load_plates()
            if saved:
                self._plates_cache = list(saved)
                logger.info(
                    "WarmPool: %s placa(s) carregadas do disco", len(saved)
                )
        except Exception as e:
            logger.warning("WarmPool load plates: %s", e)

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
                    f"{ready_n}/{len(self._slots)} Chromes em Veículos "
                    f"(1 ativo + {max(0, ready_n - 1)} standby)",
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
                "standby_ready": ready_n >= 2,
                "plates_cached": len(self._plates_cache),
                "slots": slots_info,
            }

    def is_ready(self) -> bool:
        return bool(self.snapshot().get("ready"))

    # ——— lifecycle ———

    def start(self, headless: bool = True, low_memory: bool = True) -> dict:
        """
        Garante Chrome 1 (ativo) já pronto e sobe Chrome 2 (standby) em
        background — não bloqueia pesquisa de 1 placa enquanto o 2 abre.
        """
        try:
            self._ensure_slot(0, headless=headless, low_memory=low_memory)
        except Exception as e:
            logger.exception("Falha ao aquecer Chrome 1: %s", e)
            self.last_error = str(e)
        if not self.started_at and any(s.alive() for s in self._slots):
            self.started_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        self.start_keeper()
        self._ensure_standby_bg(headless=headless, low_memory=low_memory)
        return self.snapshot()

    def _ensure_standby_bg(
        self, headless: bool = True, low_memory: bool = True
    ) -> None:
        """Sobe Chrome 2 sem bloquear a pesquisa."""
        if len(self._slots) < 2:
            return
        slot = self._slots[1]
        if slot.alive() and slot.status in ("ready", "busy"):
            return
        if slot.status == "starting" and not slot.stuck_starting(90):
            return

        def _boot() -> None:
            try:
                self._ensure_slot(1, headless=headless, low_memory=low_memory)
                logger.info("Chrome 2 (standby) pronto em background")
            except Exception as e:
                logger.warning("Chrome 2 standby (bg): %s", e)
                self.last_error = str(e)

        threading.Thread(
            target=_boot, name="warm-standby-bg", daemon=True
        ).start()

    def start_keeper(self, interval_sec: float = 45.0) -> None:
        """Religa sozinho se o Chrome cair (fora da frota)."""
        if self._keeper_thread and self._keeper_thread.is_alive():
            return

        self._keeper_stop.clear()

        def _loop() -> None:
            logger.info("WarmPool keeper ligado (intervalo %.0fs)", interval_sec)
            while not self._keeper_stop.wait(interval_sec):
                if self._fleet_busy:
                    continue
                try:
                    snap = self.snapshot()
                    ready = int(snap.get("ready_count") or 0)
                    total = int(snap.get("slot_count") or PERMANENT_SLOTS)
                    if ready >= total:
                        continue
                    logger.info(
                        "Keeper: %s/%s prontos — reaquecendo…", ready, total
                    )
                    self.ensure_both(headless=True, low_memory=True)
                except Exception as e:
                    logger.warning("Keeper ensure_both: %s", e)

        self._keeper_thread = threading.Thread(
            target=_loop, name="warm-pool-keeper", daemon=True
        )
        self._keeper_thread.start()

    def stop_keeper(self) -> None:
        self._keeper_stop.set()

    def ensure_primary(
        self, headless: bool = True, low_memory: bool = True
    ) -> dict:
        """Só Chrome 1 — o que importa para 1 placa."""
        try:
            self._ensure_slot(0, headless=headless, low_memory=low_memory)
        except Exception as e:
            logger.exception("Falha ao aquecer Chrome 1: %s", e)
            self.last_error = str(e)
            raise
        if not self.started_at and self._slots[0].alive():
            self.started_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        # standby segue em bg (não bloqueia)
        self._ensure_standby_bg(headless=headless, low_memory=low_memory)
        return self.snapshot()

    def ensure_both(self, headless: bool = True, low_memory: bool = True) -> dict:
        """
        Chrome 1 obrigatório; Chrome 2 best-effort (standby).
        Não trava eternamente no 2 se ele falhar.
        """
        try:
            self._ensure_slot(0, headless=headless, low_memory=low_memory)
        except Exception as e:
            logger.exception("Falha ao aquecer Chrome 1: %s", e)
            self.last_error = str(e)
            raise

        if len(self._slots) > 1:
            try:
                self._ensure_slot(1, headless=headless, low_memory=low_memory)
            except Exception as e:
                # standby opcional — frota roda só com o 1
                logger.warning("Chrome 2 standby indisponível: %s", e)
                self.last_error = str(e)

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
        t0 = slot.starting_since

        def _elapsed() -> str:
            return f"{int(time.time() - t0)}s"

        slot.message = f"Chrome {slot.slot_id + 1}: abrindo… ({_elapsed()})"
        # Chrome 2 espera o 1 estabilizar (sessão Sitrax + RAM)
        if slot.slot_id > 0:
            time.sleep(2.0)

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
                slot.message = (
                    f"Chrome {slot.slot_id + 1}: iniciando browser… ({_elapsed()})"
                )
                bot.start()
                slot.message = f"Chrome {slot.slot_id + 1}: login… ({_elapsed()})"
                bot.login()
            # PRONTO assim que chega em Posições (lista de placas pode vir depois)
            slot.message = f"Chrome {slot.slot_id + 1}: Posições… ({_elapsed()})"
            bot.open_posicoes()
            bot._sleep(0.25)

            slot.bot = bot
            slot.tmp = tmp
            slot.status = "ready"
            slot.starting_since = 0.0
            role = "ativo" if slot.slot_id == 0 else "standby"
            slot.message = (
                f"Chrome {slot.slot_id + 1}: PRONTO em Posições "
                f"({role}, {_elapsed()})"
            )
            debug_session.step(
                f"warm{slot.slot_id + 1}_pronto",
                f"Chrome permanente {slot.slot_id + 1} ({role}) em Posições "
                f"(aquecendo em {_elapsed()})",
                ok=True,
                screenshot=False,
            )
            logger.info(
                "Warm slot %s ready em %s (lista de placas em background)",
                slot.slot_id + 1,
                _elapsed(),
            )

            # Lista de placas: só se ninguém pesquisar em ~2s (não trava o gerar)
            def _load_plates() -> None:
                time.sleep(2.0)  # dá tempo do status PRONTO aparecer
                try:
                    # try_lock: se pesquisa já pegou, desiste da lista
                    got = slot.lock.acquire(blocking=False)
                    if not got:
                        logger.info(
                            "Warm slot %s: lista bg cancelada (lock ocupado)",
                            slot.slot_id + 1,
                        )
                        return
                    try:
                        if not slot.alive() or slot.bot is not bot:
                            return
                        if slot.status == "busy":
                            return
                        slot.message = (
                            f"Chrome {slot.slot_id + 1}: listando placas…"
                        )
                        bot.open_vehicle_selector()
                        bot.load_vehicle_list()
                        n = bot._count_vehicle_items()
                        try:
                            plates = []
                            if hasattr(bot, "list_plates"):
                                plates = bot.list_plates() or []
                            if not plates:
                                plates = self.list_plates_on_bot(bot) or []
                            if plates:
                                with self._lock:
                                    self._plates_cache = list(plates)
                        except Exception:
                            pass
                        self._close_vehicle_modal_safe(bot)
                        if slot.bot is bot and slot.status == "ready":
                            slot.message = (
                                f"Chrome {slot.slot_id + 1}: Veículos ({n})"
                            )
                        logger.info(
                            "Warm slot %s: lista %s veículos, modal fechado",
                            slot.slot_id + 1,
                            n,
                        )
                    finally:
                        slot.lock.release()
                except Exception as e:
                    logger.warning(
                        "Warm slot %s lista placas (bg): %s", slot.slot_id + 1, e
                    )
                    try:
                        if slot.bot is bot:
                            self._close_vehicle_modal_safe(bot)
                    except Exception:
                        pass
                    if slot.bot is bot and slot.status == "ready":
                        slot.message = (
                            f"Chrome {slot.slot_id + 1}: PRONTO"
                        )

            threading.Thread(
                target=_load_plates,
                name=f"warm-plates-{slot.slot_id + 1}",
                daemon=True,
            ).start()
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
        self.stop_keeper()
        self._fleet_busy = False
        with self._lock:
            for slot in self._slots:
                with slot.lock:
                    self._close_slot_unlocked(slot, keep_status=False)
            self.started_at = ""
            return self.snapshot()

    @staticmethod
    def _close_vehicle_modal_safe(bot: Any) -> None:
        """Fecha modal Select Vehicle / Selecione Veículo (PT e EN)."""
        try:
            bot._d().execute_script(
                """
                try {
                  if (typeof hideModalSearchVeiculo === 'function') hideModalSearchVeiculo();
                } catch(e) {}
                var body = (document.body && document.body.innerText) || '';
                if (!/Select Vehicle|Selecione Ve[ií]culo/i.test(body)) return 'closed';
                var nodes = document.querySelectorAll('button,a,input[type=button]');
                for (var i=0;i<nodes.length;i++){
                  var t = (nodes[i].innerText||nodes[i].value||'').replace(/\\s+/g,' ').trim();
                  if (t === 'Cancel' || t === 'Cancelar'){
                    var r = nodes[i].getBoundingClientRect();
                    if (r.width > 20 && r.height > 10) {
                      nodes[i].click();
                      return 'cancel';
                    }
                  }
                }
                return 'open';
                """
            )
            bot._sleep(0.3)
            try:
                from selenium.webdriver.common.by import By
                from selenium.webdriver.common.keys import Keys

                bot._d().find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except Exception:
                pass
            bot._sleep(0.2)
        except Exception as e:
            logger.warning("close vehicle modal: %s", e)

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
        """Pelo menos Chrome 1 pronto (standby em bg)."""
        self.ensure_primary(headless=headless, low_memory=low_memory)

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

    def force_abort(self) -> None:
        """
        Cancela pesquisa na hora: mata o Chrome ocupado e libera o pool.
        A thread do job pode morrer com WebDriverException — ok.
        """
        self._fleet_busy = False
        bots_to_quit: list[Any] = []
        with self._lock:
            for slot in self._slots:
                with slot.lock:
                    if slot.bot is not None and slot.status in (
                        "busy",
                        "starting",
                        "ready",
                        "error",
                    ):
                        bots_to_quit.append((slot, slot.bot))
                        slot.bot = None
                        slot.status = "error"
                        slot.message = (
                            f"Chrome {slot.slot_id + 1}: abortado (cancelar)"
                        )
        for slot, bot in bots_to_quit:
            try:
                bot.close()
            except Exception as e:
                logger.warning(
                    "force_abort close slot %s: %s", slot.slot_id + 1, e
                )
        # reaquece em background para a próxima busca
        def _reheat() -> None:
            try:
                time.sleep(0.4)
                self.ensure_both()
            except Exception as e:
                logger.warning("force_abort reheat: %s", e)

        threading.Thread(target=_reheat, name="warm-abort-reheat", daemon=True).start()
        logger.info("WarmPool force_abort: %s Chrome(s) mortos", len(bots_to_quit))

    # ——— 1 placa ———

    def _acquire_slot(self) -> WarmSlot:
        """
        Chrome 1 para pesquisa de 1 placa.
        NÃO espera o standby (Chrome 2) — ele sobe em background.
        """
        self.ensure_primary()
        slot = self._slots[0]
        with slot.lock:
            if not slot.alive() or slot.status not in ("ready", "busy"):
                if slot.status == "starting" and not slot.stuck_starting(90):
                    # outro thread ainda abrindo o 1 — espera um pouco
                    pass
                if not slot.alive():
                    self._boot_slot_unlocked(slot)
            slot.status = "busy"
            slot.message = f"Chrome {slot.slot_id + 1}: pesquisando…"
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
        Chrome 1 obrigatório; Chrome 2 se já estiver pronto (não espera min).
        Frota processa só no 1; o 2 fica standby até o 1 cair.
        """
        self._fleet_busy = True
        # 1 tem que estar pronto; 2 é best-effort rápido
        self.ensure_primary()
        # se standby já estiver quase pronto, tenta; senão frota segue só com 1
        if len(self._slots) > 1:
            s1 = self._slots[1]
            if not (s1.alive() and s1.status in ("ready", "busy")):
                # tenta subir rápido; se falhar, ok
                try:
                    self._ensure_slot(1, headless=True, low_memory=True)
                except Exception as e:
                    logger.warning("borrow fleet: standby falhou: %s", e)

        out: list[tuple[int, Any]] = []
        with self._lock:
            for slot in self._slots:
                with slot.lock:
                    if not slot.alive() or slot.bot is None:
                        if slot.slot_id == 0:
                            try:
                                self._boot_slot_unlocked(slot)
                            except Exception:
                                continue
                        else:
                            continue
                    if slot.bot is None:
                        continue
                    slot.status = "busy"
                    if slot.slot_id == 0:
                        slot.message = f"Chrome {slot.slot_id + 1}: frota (ativo)…"
                    else:
                        slot.message = (
                            f"Chrome {slot.slot_id + 1}: frota (standby)…"
                        )
                    out.append((slot.slot_id, slot.bot))
        return out

    def replace_fleet_bot(self, worker_id: int, bot: Any) -> None:
        """
        Atualiza o bot do slot após crash/restart na frota.
        Se o bot vinha do standby (slot 1), limpa o slot 1 para não
        devolver o mesmo browser duas vezes.
        """
        if worker_id < 0 or worker_id >= len(self._slots):
            return
        with self._lock:
            # se este bot era o do standby, libera o slot antigo
            for s in self._slots:
                if s.slot_id == worker_id:
                    continue
                with s.lock:
                    if s.bot is bot:
                        s.bot = None
                        s.status = "error"
                        s.message = (
                            f"Chrome {s.slot_id + 1}: promovido ao ativo"
                        )
            slot = self._slots[worker_id]
            with slot.lock:
                slot.bot = bot
                slot.status = "busy"
                slot.message = f"Chrome {slot.slot_id + 1}: frota (ativo)"

    def release_after_fleet(self) -> dict:
        """Devolve os 2 a Veículos (permanentes continuam ligados)."""
        try:
            for slot in self._slots:
                try:
                    with slot.lock:
                        self._return_slot_to_vehicles(slot)
                except Exception as e:
                    logger.warning("release slot %s: %s", slot.slot_id + 1, e)
            return self.snapshot()
        finally:
            self._fleet_busy = False
            self.start_keeper()

    def list_fleet_plates(self) -> list[dict]:
        """Lista frota com Chrome 1 (modo isolado — devolve a Veículos)."""
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
                return self.list_plates_on_bot(bot)
            finally:
                try:
                    self._return_slot_to_vehicles(slot)
                except Exception:
                    slot.status = was

    def list_plates_on_bot(self, bot: Any) -> list[dict]:
        """
        Lista placas usando um bot já emprestado (não devolve o slot).
        Limpa filtro de placa residual (pesquisa 1 veículo anterior).
        """
        if not bot._on_posicoes_screen():
            bot.open_posicoes()
        bot._close_date_popup_if_open()
        if not bot._vehicle_modal_open():
            bot.open_vehicle_selector()
        # load_vehicle_list() sem placa → limpa filtro e lista frota inteira
        bot.load_vehicle_list()
        vehicles = bot.list_plates()
        # se ainda veio 1 só, tenta limpar de novo e recolher
        if len(vehicles) <= 1:
            try:
                bot._clear_modal_plate_filter()
                vehicles = bot.list_plates()
            except Exception as e:
                logger.warning("list_plates retry clear: %s", e)
        try:
            bot._d().execute_script(
                "if (typeof hideModalSearchVeiculo === 'function') "
                "hideModalSearchVeiculo();"
            )
        except Exception:
            pass
        self.set_plates_cache(vehicles)
        logger.info("list_plates_on_bot: %s placa(s)", len(vehicles))
        return vehicles

    def set_plates_cache(self, vehicles: list[dict]) -> None:
        """Atualiza cache em memória e persiste no disco (merge)."""
        clean = []
        seen = set()
        for v in vehicles or []:
            pl = (v.get("placa") or "").strip().upper()
            if not pl or pl in seen:
                continue
            if pl in ("TODOS", "TODAS", "ALL", "FROTA"):
                continue
            seen.add(pl)
            clean.append(
                {
                    "placa": pl,
                    "display": (v.get("display") or "").strip(),
                    "cliente": (v.get("cliente") or "").strip(),
                }
            )
        # merge com o que já estava (não perde placas antigas)
        try:
            from app.bot.plates_store import merge_plates, load_plates

            if clean:
                merge_plates(clean)
            merged = load_plates()
            if merged:
                clean = merged
            elif not clean:
                clean = load_plates()
        except Exception as e:
            logger.warning("set_plates_cache persist: %s", e)
            with self._lock:
                # une com cache atual se disco falhar
                for old in self._plates_cache:
                    pl = (old.get("placa") or "").upper()
                    if pl and pl not in seen:
                        seen.add(pl)
                        clean.append(old)
                clean = sorted(clean, key=lambda x: x.get("placa") or "")

        with self._lock:
            self._plates_cache = clean

    def remember_plate(self, placa: str) -> None:
        """Salva 1 placa pesquisada (digitada ou escolhida no i)."""
        try:
            from app.bot.plates_store import remember_plate, load_plates

            remember_plate(placa)
            with self._lock:
                self._plates_cache = load_plates()
        except Exception as e:
            logger.warning("remember_plate: %s", e)

    def get_plates_cache(self) -> list[dict]:
        with self._lock:
            if self._plates_cache:
                return list(self._plates_cache)
        # fallback disco
        try:
            from app.bot.plates_store import load_plates

            saved = load_plates()
            if saved:
                with self._lock:
                    self._plates_cache = list(saved)
                return list(saved)
        except Exception:
            pass
        with self._lock:
            return list(self._plates_cache)


warm_pool = WarmPool(n_slots=PERMANENT_SLOTS)
