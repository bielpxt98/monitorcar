"""Round-robin da frota (3 workers)."""

from app.bot.fleet_workers import partition_round_robin


def test_round_robin_one_worker():
    vehicles = [{"placa": f"P{i:02d}"} for i in range(1, 6)]
    buckets = partition_round_robin(vehicles, 1)
    assert len(buckets) == 1
    assert [v["placa"] for _, v in buckets[0]] == ["P01", "P02", "P03", "P04", "P05"]


def test_empty_and_small():
    assert partition_round_robin([], 1) == [[]]
    b = partition_round_robin([{"placa": "AAA1B23"}], 1)
    assert len(b[0]) == 1
