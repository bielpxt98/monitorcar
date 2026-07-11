"""
Frota com N Chromes permanentes (padrão 3) e divisão round-robin.

Placas 1-indexadas:
  Worker 1 → 1, 4, 7, 10, 13...
  Worker 2 → 2, 5, 8, 11, 14...
  Worker 3 → 3, 6, 9, 12, 15...

Cada worker: login 1x, fica em Posições; entre placas faz X do chip → próxima.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_WORKERS = 3


def partition_round_robin(
    vehicles: list[dict], n_workers: int = DEFAULT_WORKERS
) -> list[list[tuple[int, dict]]]:
    """
    Divide a lista: índice 0 → W0, 1 → W1, 2 → W2, 3 → W0...
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
) -> list[PlateResult]:
    """Um Chrome permanente processa sua fatia round-robin."""
    import time as _time

    from app.bot.sitrax import SitraxBot
    from app.bot.report import build_narrative_report, positions_from_rows
    from app.bot.summary_pdf import build_summary_pdf_bytes
    from app.bot import debug_session

    results: list[PlateResult] = []
    if not assigned:
        return results

    placas = [v["placa"] for _, v in assigned]
    logger.info(
        "Worker %s: %s placa(s) → %s",
        worker_id + 1,
        len(assigned),
        ", ".join(placas[:12]),
    )
    with progress_lock:
        progress[worker_id] = {
            "done": 0,
            "total": len(assigned),
            "placa": "",
            "plates": placas,
        }

    bot: Optional[SitraxBot] = None
    sub_tmp = download_dir / f"w{worker_id}"
    sub_tmp.mkdir(parents=True, exist_ok=True)

    def _start() -> SitraxBot:
        b = SitraxBot(
            headless=True,
            download_dir=sub_tmp,
            quiet=True,
            low_memory=True,
        )
        # Escalonado + lock: evita 3 logins simultâneos invalidarem cookie
        _time.sleep(worker_id * 4.0)
        with _LOGIN_LOCK:
            b.start()
            b.login()
            _time.sleep(1.5)
        b.open_posicoes()
        b._sleep(1.0)
        debug_session.step(
            f"worker{worker_id+1}_pronto",
            f"Chrome {worker_id+1} logado em Posições — "
            f"{len(assigned)} placa(s): {', '.join(placas[:8])}",
            ok=True,
            screenshot=False,
        )
        return b

    def _kill() -> None:
        nonlocal bot
        if bot is not None:
            try:
                bot.close()
            except Exception:
                pass
            bot = None

    try:
        bot = _start()
        for j, (order, v) in enumerate(assigned):
            pl = v["placa"]
            with progress_lock:
                progress[worker_id]["done"] = j
                progress[worker_id]["placa"] = pl

            debug_session.step(
                f"w{worker_id+1}_{j+1}_de_{len(assigned)}_{pl}",
                f"Chrome {worker_id+1} → ({j+1}/{len(assigned)}) {pl} "
                f"[posição lista #{order+1}]",
                ok=True,
                screenshot=False,
            )

            attempts = 0
            while attempts < 2:
                attempts += 1
                try:
                    if bot is None or not bot.alive():
                        _kill()
                        bot = _start()

                    assert bot is not None
                    # Fluxo robusto: chip + Filter + espera grade + scrape
                    rows = bot.fetch_positions_for_fleet_plate(
                        pl,
                        data_ini=data_ini,
                        data_fim=data_fim,
                        clear_previous=(j > 0),
                    )
                    positions = positions_from_rows(rows)
                    n_pts = len([p for p in positions if p.when])
                    # 0 pts com chip + mensagem Sitrax = dia sem GPS (válido)
                    empty_legit = n_pts == 0 and (
                        bot.sitrax_says_no_records()
                        or bot.vehicle_chip_has_plate(pl)
                    )

                    if n_pts == 0 and not empty_legit:
                        # só reabre se NÃO for zero legítimo do Sitrax
                        logger.warning(
                            "Worker %s %s: 0 pts suspeito — reabre Posições",
                            worker_id + 1,
                            pl,
                        )
                        try:
                            bot.open_posicoes()
                            bot._sleep(1)
                        except Exception:
                            pass
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
                            or bot.vehicle_chip_has_plate(pl)
                        )

                    texto = build_narrative_report(
                        pl,
                        positions,
                        data_ref=data_ref,
                        cliente=v.get("cliente", ""),
                    )
                    if n_pts == 0 and empty_legit:
                        # não tratar como erro — carro sem pontos no dia
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
                    # ok=True mesmo com 0 pts se Sitrax confirmou vazio
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
                            "(Sitrax sem registros no período — OK)"
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
                        _kill()
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
                        _kill()
                        try:
                            bot = _start()
                        except Exception:
                            bot = None
                    break
    finally:
        _kill()

    return results


def run_fleet_parallel(
    vehicles: list[dict],
    data_ini: date,
    data_fim: date,
    data_ref: str,
    download_dir: Path,
    n_workers: int = DEFAULT_WORKERS,
    job_message_cb: Optional[Any] = None,
) -> list[PlateResult]:
    """
    Sobe até n_workers Chromes, repartilha placas em round-robin, processa em paralelo.
    Retorna resultados ordenados pela posição original na lista de frota.
    """
    from app.bot import debug_session

    n_workers = max(1, min(int(n_workers), 3))  # cap 3 no Railway
    buckets = partition_round_robin(vehicles, n_workers)

    # remove workers vazios do pool (frota pequena)
    active = [(i, b) for i, b in enumerate(buckets) if b]
    if not active:
        return []

    debug_session.step(
        "frota_workers",
        f"{len(active)} Chrome(s) // round-robin: "
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
    return all_results
