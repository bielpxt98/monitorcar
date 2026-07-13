"""Persistência de placas (botão i)."""

from pathlib import Path

import app.bot.plates_store as store


def test_merge_and_load(tmp_path, monkeypatch):
    path = tmp_path / "plates.json"
    monkeypatch.setattr(store, "_STORE_PATH", path)
    monkeypatch.setattr(store, "_DATA_DIR", tmp_path)

    assert store.load_plates() == []

    out = store.merge_plates(
        [
            {"placa": "PCE7B03", "display": "A", "cliente": ""},
            {"placa": "abc1d23"},
            {"placa": "TODOS"},  # ignorado
        ]
    )
    assert [p["placa"] for p in out] == ["ABC1D23", "PCE7B03"]
    assert path.exists()

    # segunda merge não apaga as antigas
    out2 = store.merge_plates([{"placa": "RZN4132"}])
    assert [p["placa"] for p in out2] == ["ABC1D23", "PCE7B03", "RZN4132"]

    store.remember_plate("xyz9a99")
    loaded = store.load_plates()
    assert "XYZ9A99" in [p["placa"] for p in loaded]
    assert len(loaded) == 4


def test_partition_still_one_worker():
    from app.bot.fleet_workers import partition_round_robin, DEFAULT_WORKERS

    assert DEFAULT_WORKERS == 1
    vehicles = [{"placa": f"P{i:02d}"} for i in range(1, 14)]
    buckets = partition_round_robin(vehicles, 1)
    assert len(buckets) == 1
    assert len(buckets[0]) == 13
