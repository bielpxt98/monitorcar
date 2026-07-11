"""
Frota com 1 Chrome permanente (estável / menos perda de pontos).

- Todas as placas em sequência no mesmo browser.
- Entre placas: X no chip → próxima (sem login).
- Só reabre se tab crashed / sessão morta.
- Placas que falharam por crash são re-tentadas no fim.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 1 worker = sem briga de RAM; total de pontos mais completo
DEFAULT_WORKERS = 1
RESTART_ONLY_ON_CRASH = True


def partition_round_robin(
    vehicles: list[dict], n_workers: int = DEFAULT_WORKERS
) -> list[list[tuple[int, dict]]]:
    """
    Divide a lista: índice 0 → W0, 1 → W1, 2 → W0...
    Retorna lista de buckets; cada item é (ordem_original, vehicle_dict).
    """
    n = max(1, int(n_workers))
    buckets: list[list[tuple[int, dict]]] = [[] for _ in range(n)]
    for i, v in enumerate(vehicles):
        buckets[i % n].append((i, v))
    return buckets


@dataclass
class PlateResult:
    order: int
    placa: str
    texto: str
    pdf_bytes: bytes
    pontos: int
    worker_id: int
    ok: bool = True
    error: str = ""


@dataclass
class WorkerState:
    worker_id: int
    plates: list[str] = field(default_factory=list)
    done: int = 0
    message: str = ""


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


# Login serializado — Sitrax costuma derrubar sessão se 3 logins batem juntos
_LOGIN_LOCK = threading.Lock()


def _run_one_worker(
    worker_id: int,
    assigned: list[tuple[int, dict]],
    data_ini: date,
    data_fim: date,
    data_ref: str,
    download_dir: Path,
    progress: dict,
    progress_lock: threading.Lock,
    existing_bot: Any = None,
    keep_alive: bool = False,
    on_bot_replaced: Optional[Any] = None,
    start_barrier: Optional[threading.Barrier] = None,
) -> list[PlateResult]:
    """Um Chrome processa sua fatia; preferência: bot permanente (keep_alive)."""
    import time as _time

    from app.bot.sitrax import SitraxBot
    from app.bot.report import build_narrative_report, positions_from_rows
    from app.bot.summary_pdf import build_summary_pdf_bytes
    from app.bot import debug_session

    results: list[PlateResult] = []
    if not assigned:
        if start_barrier is not None:
            try:
                start_barrier.wait(timeout=120)
            except Exception:
                pass
        return results

    placas = [v["placa"] for _, v in assigned]
    logger.info(
        "Worker %s: %s placa(s) → %s (permanente=%s)",
        worker_id + 1,
        len(assigned),
        ", ".join(placas[:12]),
        bool(existing_bot),
    )
    with progress_lock:
        progress[worker_id] = {
            "done": 0,
            "total": len(assigned),
            "placa": "",
            "plates": placas,
        }

    bot: Optional[Any] = existing_bot if existing_bot is not None else None
    sub_tmp = download_dir / f"w{worker_id}"
    sub_tmp.mkdir(parents=True, exist_ok=True)

    def _start() -> Any:
        nonlocal bot
        # reutiliza permanente se ainda vivo — SEM sleep (partida juntos)
        if bot is not None:
            try:
                if bot.alive():
                    debug_session.step(
                        f"worker{worker_id+1}_reuso",
                        f"Chrome {worker_id+1} permanente reutilizado — "
                        f"{len(assigned)} placa(s): {', '.join(placas[:8])}",
                        ok=True,
                        screenshot=False,
                    )
                    return bot
            except Exception:
                pass
        b = SitraxBot(
            headless=True,
            download_dir=sub_tmp,
            quiet=True,
            low_memory=True,
        )
        # só atrasa login FRIOS (permanente morto); permanentes não passam aqui
        with _LOGIN_LOCK:
            b.start()
            b.login()
            _time.sleep(0.8)
        b.open_posicoes()
        b._sleep(0.5)
        debug_session.step(
            f"worker{worker_id+1}_pronto",
            f"Chrome {worker_id+1} logado em Posições — "
            f"{len(assigned)} placa(s): {', '.join(placas[:8])}",
            ok=True,
            screenshot=False,
        )
        bot = b
        if on_bot_replaced:
            try:
                on_bot_replaced(worker_id, b)
            except Exception:
                pass
        return b

    def _kill() -> None:
        nonlocal bot
        if bot is None:
            return
        if keep_alive and bot is existing_bot:
            return
        try:
            bot.close()
        except Exception:
            pass
        bot = None

    try:
        bot = _start()
        # Barreira: os 2 saem da largada no mesmo instante
        if start_barrier is not None:
            try:
                debug_session.step(
                    f"w{worker_id+1}_barreira",
                    f"Chrome {worker_id+1} pronto — aguarda largada simultânea",
                    ok=True,
                    screenshot=False,
                )
                start_barrier.wait(timeout=180)
            except Exception as e:
                logger.warning("Barreira worker %s: %s", worker_id + 1, e)

        for j, (order, v) in enumerate(assigned):
            pl = v["placa"]
            with progress_lock:
                progress[worker_id]["done"] = j
                progress[worker_id]["placa"] = pl

            debug_session.step(
                f"w{worker_id+1}_{j+1}_de_{len(assigned)}_{pl}",
                f"Chrome {worker_id+1} → ({j+1}/{len(assigned)}) {pl} "
                f"[posição lista #{order+1}] (mesmo browser, sem reinício periódico)",
                ok=True,
                screenshot=False,
            )

            attempts = 0
            while attempts < 2:
                attempts += 1
                try:
                    # Só reabre Chrome se a aba/sessão morreu — NUNCA por contagem de placas
                    if bot is None or not bot.alive():
                        logger.warning(
                            "Worker %s: sessão morta antes de %s — reinício por crash",
                            worker_id + 1,
                            pl,
                        )
                        if bot is not None:
                            try:
                                bot.close()
                            except Exception:
                                pass
                            bot = None
                        existing_bot = None
                        bot = _start()

                    assert bot is not None
                    # Mesmo Chrome: 1ª placa ou X do chip + próxima (sem fechar browser)
                    rows = bot.fetch_positions_for_fleet_plate(
                        pl,
                        data_ini=data_ini,
                        data_fim=data_fim,
                        clear_previous=(j > 0),
                    )
                    positions = positions_from_rows(rows)
                    n_pts = len([p for p in positions if p.when])
                    # 0 legítimo SÓ com confirmação explícita do Sitrax
                    empty_legit = n_pts == 0 and (
                        bot.sitrax_says_no_records()
                        or bot.showing_zero_records()
                    )

                    # Se 0 pts SEM confirmação de vazio → retry (pode ser scrape/filter)
                    if n_pts == 0 and not empty_legit:
                        logger.warning(
                            "Worker %s %s: 0 pts sem 'Mostrando: 0' — 1 retry",
                            worker_id + 1,
                            pl,
                        )
                        rows = bot.fetch_positions_for_fleet_plate(
                            pl,
                            data_ini=data_ini,
                            data_fim=data_fim,
                            clear_previous=True,
                        )
                        positions = positions_from_rows(rows)
                        n_pts = len([p for p in positions if p.when])
                        empty_legit = n_pts == 0 and (
                            bot.sitrax_says_no_records()
                            or bot.showing_zero_records()
                        )

                    texto = build_narrative_report(
                        pl,
                        positions,
                        data_ref=data_ref,
                        cliente=v.get("cliente", ""),
                    )
                    if n_pts == 0 and empty_legit:
                        pass
                    elif n_pts == 0:
                        texto = (
                            f"📋 {pl}: 0 posições após Filter "
                            f"(Chrome {worker_id+1}). "
                            "Falha ao aplicar filtro?\n\n" + texto
                        )

                    pdf_b = build_summary_pdf_bytes(
                        pl,
                        positions,
                        data_ref=data_ref,
                        cliente=v.get("cliente", ""),
                    )
                    ok_result = n_pts > 0 or empty_legit
                    results.append(
                        PlateResult(
                            order=order,
                            placa=pl,
                            texto=texto,
                            pdf_bytes=pdf_b,
                            pontos=n_pts,
                            worker_id=worker_id,
                            ok=ok_result,
                            error="" if ok_result else "0 posições (não confirmado)",
                        )
                    )
                    if n_pts > 0:
                        step_name = f"w{worker_id+1}_ok_{pl}"
                        step_msg = f"Chrome {worker_id+1}: {pl} → {n_pts} pts"
                    elif empty_legit:
                        step_name = f"w{worker_id+1}_vazio_ok_{pl}"
                        step_msg = (
                            f"Chrome {worker_id+1}: {pl} → 0 pts "
                            "(Sitrax Mostrando: 0 — OK, sem retry)"
                        )
                    else:
                        step_name = f"w{worker_id+1}_zero_{pl}"
                        step_msg = f"Chrome {worker_id+1}: {pl} → 0 pts"
                    debug_session.step(
                        step_name,
                        step_msg,
                        ok=ok_result,
                        screenshot=False,
                    )
                    with progress_lock:
                        progress[worker_id]["done"] = j + 1
                    break
                except Exception as e:
                    logger.exception(
                        "Worker %s falha em %s (tentativa %s)",
                        worker_id + 1,
                        pl,
                        attempts,
                    )
                    if _is_crash(e) and attempts < 2:
                        debug_session.step(
                            f"w{worker_id+1}_crash_{pl}",
                            f"Chrome {worker_id+1} caiu em {pl}; reiniciando",
                            ok=False,
                            screenshot=False,
                        )
                        # fecha morto e sobe de novo (atualiza pool se permanente)
                        if bot is not None:
                            try:
                                bot.close()
                            except Exception:
                                pass
                            bot = None
                        existing_bot = None  # força cold start
                        bot = _start()
                        continue
                    results.append(
                        PlateResult(
                            order=order,
                            placa=pl,
                            texto=f"📋 {pl}: erro (Chrome {worker_id+1}) — {e}",
                            pdf_bytes=b"",
                            pontos=0,
                            worker_id=worker_id,
                            ok=False,
                            error=str(e),
                        )
                    )
                    debug_session.step(
                        f"w{worker_id+1}_erro_{pl}",
                        str(e),
                        ok=False,
                        screenshot=False,
                    )
                    if _is_crash(e):
                        if bot is not None:
                            try:
                                bot.close()
                            except Exception:
                                pass
                            bot = None
                        existing_bot = None
                        try:
                            bot = _start()
                        except Exception:
                            bot = None
                    break
    finally:
        if not keep_alive:
            if bot is not None:
                try:
                    bot.close()
                except Exception:
                    pass
        # keep_alive: WarmPool.release_after_fleet devolve a Veículos

    return results


def run_fleet_parallel(
    vehicles: list[dict],
    data_ini: date,
    data_fim: date,
    data_ref: str,
    download_dir: Path,
    n_workers: int = DEFAULT_WORKERS,
    job_message_cb: Optional[Any] = None,
    existing_bots: Optional[list[tuple[int, Any]]] = None,
    keep_alive: bool = False,
    on_bot_replaced: Optional[Any] = None,
) -> list[PlateResult]:
    """
    2 Chromes em paralelo (round-robin).
    existing_bots: bots permanentes [(id, bot), ...] — sem login de novo.
    """
    from app.bot import debug_session

    n_workers = max(1, min(int(n_workers), 1))  # frota estável = 1 Chrome
    buckets = partition_round_robin(vehicles, n_workers)

    bot_by_id: dict[int, Any] = {}
    if existing_bots:
        for wid, b in existing_bots:
            bot_by_id[int(wid)] = b

    active = [(i, b) for i, b in enumerate(buckets) if b]
    if not active:
        return []

    debug_session.step(
        "frota_workers",
        f"{len(active)} Chrome(s) permanentes // round-robin: "
        + " | ".join(
            f"W{i+1}=[{', '.join(v['placa'] for _, v in b[:6])}"
            f"{'…' if len(b) > 6 else ''}]"
            for i, b in active
        ),
        ok=True,
        screenshot=False,
    )

    progress: dict = {}
    progress_lock = threading.Lock()
    all_results: list[PlateResult] = []

    def _tick():
        if not job_message_cb:
            return
        with progress_lock:
            parts = []
            total_done = 0
            total = len(vehicles)
            for wid, st in progress.items():
                total_done += int(st.get("done") or 0)
                pl = st.get("placa") or "…"
                parts.append(f"C{wid+1}:{st.get('done', 0)}/{st.get('total', 0)} {pl}")
            job_message_cb(
                f"Frota {total_done}/{total} — " + " · ".join(parts)
            )

    # Largada simultânea: os N workers esperam uns aos outros e saem juntos
    start_barrier = threading.Barrier(len(active), timeout=180)

    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        futs = {
            pool.submit(
                _run_one_worker,
                wid,
                bucket,
                data_ini,
                data_fim,
                data_ref,
                download_dir,
                progress,
                progress_lock,
                bot_by_id.get(wid),
                keep_alive,
                on_bot_replaced,
                start_barrier,
            ): wid
            for wid, bucket in active
        }
        # poll progress while waiting
        import time

        pending = set(futs.keys())
        while pending:
            _tick()
            done_now = []
            for fut in list(pending):
                if fut.done():
                    done_now.append(fut)
            for fut in done_now:
                pending.discard(fut)
                wid = futs[fut]
                try:
                    all_results.extend(fut.result())
                except Exception as e:
                    logger.exception("Worker %s morreu: %s", wid + 1, e)
                    debug_session.step(
                        f"worker{wid+1}_morto",
                        str(e),
                        ok=False,
                        screenshot=False,
                    )
            if pending:
                time.sleep(1.0)
        _tick()

    all_results.sort(key=lambda r: r.order)

    # Reprocessa placas que falharam por crash/erro (não vazio legítimo 0 pts)
    failed = [
        r
        for r in all_results
        if (not r.ok) and r.pontos == 0 and "erro" in (r.texto or "").lower()
    ]
    # também: ok=False com error preenchido
    failed_orders = {r.order for r in all_results if not r.ok and r.error and "0 posições" not in (r.error or "")}
    retry_vehicles = [
        (i, v)
        for i, v in enumerate(vehicles)
        if i in failed_orders
        or any(
            r.order == i and not r.ok and r.pontos == 0 and r.error
            for r in all_results
        )
    ]
    # dedupe
    seen_o = set()
    retry_list: list[tuple[int, dict]] = []
    for item in retry_vehicles:
        if item[0] not in seen_o:
            seen_o.add(item[0])
            retry_list.append(item)

    if retry_list and existing_bots:
        debug_session.step(
            "frota_retry_falhas",
            f"Reprocessando {len(retry_list)} placa(s) que falharam: "
            + ", ".join(v["placa"] for _, v in retry_list[:15]),
            ok=True,
            screenshot=False,
        )
        # 1 worker com o bot 0 (ou cold)
        bot0 = bot_by_id.get(0) or (existing_bots[0][1] if existing_bots else None)
        retry_results = _run_one_worker(
            0,
            retry_list,
            data_ini,
            data_fim,
            data_ref,
            download_dir,
            progress,
            progress_lock,
            bot0,
            keep_alive,
            on_bot_replaced,
            None,  # sem barreira
        )
        by_order = {r.order: r for r in all_results}
        for r in retry_results:
            # só substitui se o retry foi melhor (mais pontos ou ok)
            old = by_order.get(r.order)
            if old is None or (r.ok and (not old.ok or r.pontos >= old.pontos)):
                by_order[r.order] = r
        all_results = sorted(by_order.values(), key=lambda x: x.order)

    return all_results
