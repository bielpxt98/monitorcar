"""
Persistência de placas da frota (botão i na home).

- Salva em data/plates.json (sobrevive a restart do processo enquanto o disco
  do deploy existir).
- Merge: novas placas descobertas no Sitrax ou digitadas na pesquisa.
- Nunca apaga placas já salvas (só acrescenta / atualiza display).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# data/ ao lado do pacote app/ (repo root / data/plates.json)
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_STORE_PATH = _DATA_DIR / "plates.json"
_lock = threading.RLock()


def _empty_payload() -> dict:
    return {"plates": [], "updated_at": ""}


def _normalize_item(v: dict) -> Optional[dict]:
    pl = (v.get("placa") or "").strip().upper()
    if not pl:
        return None
    if pl in ("TODOS", "TODAS", "ALL", "FROTA", "*"):
        return None
    return {
        "placa": pl,
        "display": (v.get("display") or "").strip(),
        "cliente": (v.get("cliente") or "").strip(),
    }


def load_plates() -> list[dict]:
    """Lê placas salvas do disco."""
    with _lock:
        if not _STORE_PATH.exists():
            return []
        try:
            raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            items = raw.get("plates") if isinstance(raw, dict) else raw
            out: list[dict] = []
            seen: set[str] = set()
            for v in items or []:
                if not isinstance(v, dict):
                    # string pura
                    if isinstance(v, str):
                        v = {"placa": v}
                    else:
                        continue
                item = _normalize_item(v)
                if not item or item["placa"] in seen:
                    continue
                seen.add(item["placa"])
                out.append(item)
            return out
        except Exception as e:
            logger.warning("plates_store load: %s", e)
            return []


def save_plates(plates: list[dict]) -> list[dict]:
    """Grava lista completa (já normalizada/mergeada)."""
    from datetime import datetime

    clean: list[dict] = []
    seen: set[str] = set()
    for v in plates or []:
        item = _normalize_item(v if isinstance(v, dict) else {"placa": str(v)})
        if not item or item["placa"] in seen:
            continue
        seen.add(item["placa"])
        clean.append(item)

    payload = {
        "plates": clean,
        "updated_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "count": len(clean),
    }
    with _lock:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            _STORE_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("plates_store: %s placa(s) em %s", len(clean), _STORE_PATH)
        except Exception as e:
            logger.warning("plates_store save: %s", e)
    return clean


def merge_plates(new_plates: list[Any]) -> list[dict]:
    """
    Une placas novas às já salvas (não remove nenhuma).
    Atualiza display/cliente se vierem preenchidos.
    """
    by_pl: dict[str, dict] = {}
    for v in load_plates():
        item = _normalize_item(v)
        if item:
            by_pl[item["placa"]] = item

    for v in new_plates or []:
        if isinstance(v, str):
            v = {"placa": v}
        if not isinstance(v, dict):
            continue
        item = _normalize_item(v)
        if not item:
            continue
        pl = item["placa"]
        if pl in by_pl:
            old = by_pl[pl]
            if item["display"]:
                old["display"] = item["display"]
            if item["cliente"]:
                old["cliente"] = item["cliente"]
        else:
            by_pl[pl] = item

    # ordem estável: alfabética (fácil achar no i)
    merged = sorted(by_pl.values(), key=lambda x: x["placa"])
    return save_plates(merged)


def remember_plate(placa: str, display: str = "", cliente: str = "") -> list[dict]:
    """Salva 1 placa pesquisada (ex.: digitada no formulário)."""
    pl = (placa or "").strip().upper()
    if not pl or pl in ("TODOS", "TODAS", "ALL", "FROTA", "*"):
        return load_plates()
    return merge_plates([{"placa": pl, "display": display, "cliente": cliente}])


def store_path() -> Path:
    return _STORE_PATH
